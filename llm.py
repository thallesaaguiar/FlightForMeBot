"""Flight agent over several LLM providers: fallback chain for chat, side-by-side for /comparar."""
import asyncio
import json
import os
import re
import time
import unicodedata
from datetime import date, timedelta

import anthropic
import openai

import server

_tools = asyncio.run(server.mcp.list_tools())
FUNCS = {t.name: getattr(server, t.name) for t in _tools}
CLAUDE_TOOLS = [{"name": t.name, "description": t.description, "input_schema": t.inputSchema} for t in _tools]
OAI_TOOLS = [{"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.inputSchema}}
             for t in _tools]
MAX_SEARCHES = int(os.environ.get("LLM_MAX_SEARCHES", "10"))  # paid SerpApi searches per question
PAID = {"search_flights", "compare_split", "booking_options"}
SYSTEM = (server.mcp.instructions + "\nReply in the user's language, plain text (no Markdown), short."
          f"\nBudget: at most {MAX_SEARCHES} paid searches per question. Plan them; once refused, answer with what you have.")
MAX_HISTORY = 60  # ponytail: reset instead of trimming; add compaction if long chats matter
MAX_STEPS = 15    # tool-call rounds per question, stops runaway loops burning SerpApi quota

# name: (key env var, OpenAI-compatible base_url or None for the Anthropic SDK, model)
PROVIDERS = {
    "claude": ("ANTHROPIC_API_KEY", None, os.environ.get("CLAUDE_MODEL", "claude-sonnet-5")),
    "gemini": ("GEMINI_API_KEY", "https://generativelanguage.googleapis.com/v1beta/openai/",
               os.environ.get("GEMINI_MODEL", "gemini-flash-latest")),
    "deepseek": ("DEEPSEEK_API_KEY", "https://api.deepseek.com", os.environ.get("DEEPSEEK_MODEL", "deepseek-flash")),
    # local, no key: setting OLLAMA_MODEL enables it (its value doubles as the ignored api_key)
    "ollama": ("OLLAMA_MODEL", os.environ.get("OLLAMA_URL", "http://localhost:11434/v1"), os.environ.get("OLLAMA_MODEL")),
}
ORDER = [p for p in (x.strip() for x in os.environ.get("LLM", "gemini,deepseek,claude").split(","))
         if p in PROVIDERS and os.environ.get(PROVIDERS[p][0])]
# short timeout + 1 retry: a stuck or rate-limited provider should fall through fast, not block the bot
_clients = {p: anthropic.Anthropic(timeout=120, max_retries=1) if PROVIDERS[p][1] is None
            else openai.OpenAI(api_key=os.environ[PROVIDERS[p][0]], base_url=PROVIDERS[p][1],
                                 timeout=180 if p == "ollama" else 60, max_retries=1)  # local model: cold load + slower
            for p in ORDER}
# ponytail: history is per provider (message formats differ); a fallback mid-chat starts that provider fresh
history: dict[tuple[int, str], list] = {}


def call_tool(name: str, args, stats: dict) -> tuple[str, bool]:
    stats["tools"].append(name)
    # ponytail: checked before the call, so one compare_split (1 + 3/hub) can overshoot the budget a bit
    if name in PAID and server._month_calls() - stats["serp"] >= MAX_SEARCHES:
        return f"Refused: the {MAX_SEARCHES} paid-search budget for this question is used up. Answer with what you have.", True
    try:
        if isinstance(args, str):
            args = json.loads(args or "{}")
        out = FUNCS[name](**args)
        if isinstance(out, dict) and "links" in out:  # long URLs go to the user as buttons, never through the model
            stats["links"] += out.pop("links")
            out["links_sent_to_user"] = True
        return json.dumps(out, ensure_ascii=False, default=str), False
    except Exception as e:
        return f"{type(e).__name__}: {e}", True


def _run_claude(msgs: list, stats: dict) -> str:
    for _ in range(MAX_STEPS):
        r = _clients["claude"].beta.messages.create(
            model=PROVIDERS["claude"][2], max_tokens=16000, system=SYSTEM, tools=CLAUDE_TOOLS, messages=msgs,
            cache_control={"type": "ephemeral"},
            betas=["server-side-fallback-2026-07-01"], fallbacks="default",
        )
        u = r.usage
        stats["in"] += u.input_tokens + (u.cache_read_input_tokens or 0) + (u.cache_creation_input_tokens or 0)
        stats["out"] += u.output_tokens
        msgs.append({"role": "assistant", "content": r.content})
        if r.stop_reason != "tool_use":
            return "".join(b.text for b in r.content if b.type == "text") or f"(sem resposta: {r.stop_reason})"
        results = []
        for b in r.content:
            if b.type == "tool_use":
                out, err = call_tool(b.name, b.input, stats)
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": out, "is_error": err})
        msgs.append({"role": "user", "content": results})
    raise RuntimeError(f"passou de {MAX_STEPS} rodadas de ferramentas")


def _run_openai(p: str, msgs: list, stats: dict) -> str:
    for _ in range(MAX_STEPS):
        r = _clients[p].chat.completions.create(
            model=PROVIDERS[p][2], messages=[{"role": "system", "content": SYSTEM}, *msgs], tools=OAI_TOOLS)
        if r.usage:
            stats["in"] += r.usage.prompt_tokens
            stats["out"] += r.usage.completion_tokens
        m = r.choices[0].message
        msgs.append(m.model_dump(exclude_none=True))
        if not m.tool_calls:
            return m.content or "(sem resposta)"
        for tc in m.tool_calls:
            out, _ = call_tool(tc.function.name, tc.function.arguments, stats)
            msgs.append({"role": "tool", "tool_call_id": tc.id, "content": out})
    raise RuntimeError(f"passou de {MAX_STEPS} rodadas de ferramentas")


def run(p: str, msgs: list) -> tuple[str, dict]:
    stats = {"in": 0, "out": 0, "tools": [], "links": [], "serp": server._month_calls(), "t": time.time()}
    text = _run_claude(msgs, stats) if p == "claude" else _run_openai(p, msgs, stats)
    stats["serp"] = server._month_calls() - stats["serp"]
    stats["t"] = time.time() - stats["t"]
    return text, stats


def _user(text: str) -> dict:
    return {"role": "user", "content": f"[today {date.today()}] {text}"}


def _norm(s: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFKD", s) if not unicodedata.combining(ch)).lower()


def plain(text: str) -> str:
    """Models still emit some Markdown; Telegram gets plain text, so drop the markers."""
    return re.sub(r"^#{1,6}\s*", "", text.replace("**", "").replace("__", ""), flags=re.M)


def ask(chat: int, text: str) -> tuple[str, list[dict]]:
    """First provider in ORDER that answers wins; failures fall through to the next. Returns (answer, links)."""
    errors = []
    for p in ORDER:
        msgs = history.setdefault((chat, p), [])
        if len(msgs) > MAX_HISTORY:
            msgs.clear()
        n = len(msgs)
        msgs.append(_user(text))
        try:
            answer, s = run(p, msgs)
            return f"{plain(answer)}\n\n— {p}", s["links"]
        except Exception as e:
            del msgs[n:]  # drop the unfinished turn so this provider's history stays valid
            errors.append(f"{p}: {type(e).__name__}: {str(e)[:150]}")
    return "Nenhuma IA respondeu:\n" + "\n".join(errors), []


def compare(text: str):
    """Yield (report, links) per provider for the same question (fresh context each)."""
    for p in ORDER:
        try:
            answer, s = run(p, [_user(text)])
            yield (f"[{p} · {PROVIDERS[p][2]}] {s['t']:.0f}s · {s['in']:,}/{s['out']:,} tokens · "
                   f"{len(s['tools'])} chamadas ({', '.join(s['tools']) or '-'}) · {s['serp']} buscas pagas\n\n{plain(answer)}",
                   s["links"])
        except Exception as e:
            yield f"[{p}] falhou: {type(e).__name__}: {str(e)[:200]}", []


EXTRACT_PROMPT = """Today is {today}. Turn the user's flight request into JSON, and reply with the JSON object only:
{{"origin_text": the exact words of THIS message that name the origin, or null when it doesn't name one,
 "origin_city": the origin city name in English (e.g. "Sao Paulo", "Tokyo", "Porto"),
 "origin_code": one IATA code for the cached-price lookup; use the city code when the city has several airports
   (SAO, RIO, TYO, LON, PAR, NYC, MIL, ROM, MOW, CHI, WAS, BUE, STO, OSA, SEL, BJS), else the airport code,
 "origin_airports": comma-separated airport codes for the live search (e.g. "GRU,VCP,CGH" or "HND,NRT"),
 "destination_text": the exact words of THIS message that name the destination or region, or null,
 "destination_city": same as origin_city, "destination_code": same rule, "destination_airports": same rule,
 "depart_from": "YYYY-MM-DD", "depart_to": "YYYY-MM-DD": the window for the OUTBOUND flight only, never the return date
   (a month or month range covers whole months; a fixed outbound date means depart_from = depart_to;
   "from April onward" with no end means April 1 to the last day of May),
 "round_trip": true or false,
 "min_days": length of stay in days or null (fixed outbound and return dates: the exact difference),
 "max_days": longest stay or null ("at least N days" leaves it null),
 "max_stops": the stops the user normally accepts: 0, 1, 2 or null for any,
 "more_stops_if_much_cheaper": true when more stops are accepted only if much cheaper (keep max_stops at the lower number),
 "adults": number of adult travellers (default 1),
 "destination_region": null, or when the user wants a region or "anywhere" instead of one destination: "europe",
   "south_america", "north_america" (includes Central America and the Caribbean), "asia", "middle_east", "africa",
   "oceania", "domestic" (same country as the origin) or "anywhere"; then the destination_* fields are null,
 "max_price": budget per person as a number, or null,
 "max_hours": longest acceptable duration of each flight in hours (e.g. "até 19h de voo" -> 19), or null,
 "missing": "" or a short question in the user's language ONLY when the origin is unknown, or when there is neither
   a destination nor a region}}
When the user gives no dates or wants the cheapest dates, leave depart_from and depart_to null: never ask for dates.

Examples (today 2026-09-29):
"Recife pra Roma em março de 2027, uns 12 a 16 dias, no máximo 1 escala" ->
{{"origin_text":"Recife","destination_text":"Roma","origin_city":"Recife","origin_code":"REC","origin_airports":"REC","destination_city":"Rome","destination_code":"ROM","destination_airports":"FCO,CIA",
  "depart_from":"2027-03-01","depart_to":"2027-03-31","round_trip":true,"min_days":12,"max_days":16,
  "max_stops":1,"more_stops_if_much_cheaper":false,"missing":""}}
"Londres para Buenos Aires, vou 3/2 e volto 20/2" ->
{{"origin_text":"Londres","destination_text":"Buenos Aires","origin_city":"London","origin_code":"LON","origin_airports":"LHR,LGW","destination_city":"Buenos Aires","destination_code":"BUE","destination_airports":"EZE,AEP",
  "depart_from":"2027-02-03","depart_to":"2027-02-03","round_trip":true,"min_days":17,"max_days":17,
  "max_stops":null,"more_stops_if_much_cheaper":false,"missing":""}}
These examples only show the format: take every value from the user's message, never from the examples."""
# the extraction is easy, so try the free local model first
EXTRACT_ORDER = sorted(ORDER, key=lambda p: p != "ollama")
EXTRACT_EFFORT = os.environ.get("EXTRACT_EFFORT", "low")  # gpt-oss thinking level for extraction; "" = model default


# ponytail: English "may" left out (it's also a verb); add it if English users hit it
MONTHS = {m: i for i, names in enumerate([
    "janeiro january enero", "fevereiro february febrero", "marco march marzo", "abril april", "maio mayo",
    "junho june junio", "julho july julio", "agosto august", "setembro september septiembre", "outubro october octubre",
    "novembro november noviembre", "dezembro december diciembre"], 1) for m in names.split()}
NUMBERS = {"um": 1, "uma": 1, "one": 1, "dois": 2, "duas": 2, "two": 2, "tres": 3, "three": 3, "quatro": 4, "four": 4}


def _month_end(y: int, m: int) -> date:
    return date(y + m // 12, m % 12 + 1, 1) - timedelta(days=1)


REGION_WORDS = [("america do sul", "south_america"), ("south america", "south_america"), ("europa", "europe"),
                ("europe", "europe"), ("asia", "asia"), ("oriente medio", "middle_east"), ("middle east", "middle_east"),
                ("africa", "africa"), ("oceania", "oceania"), ("caribe", "north_america"), ("caribbean", "north_america"),
                ("america central", "north_america"), ("america do norte", "north_america"),
                ("north america", "north_america"), ("nacional", "domestic"), ("dentro do pais", "domestic"),
                ("domestic", "domestic"), ("qualquer lugar", "anywhere"), ("anywhere", "anywhere")]


def named(q: dict, side: str, t: str) -> bool:
    """Does the (normalized) message actually mention this side's place? Checks the quoted words, city and code."""
    said = str(q.get(f"{side}_text") or "")
    if len(said) >= 3 and not re.search(r"\d", said) and _norm(said) in t:  # "fico só 10 dias" is not a place
        return True
    return any(len(x) >= 3 and _norm(x) in t for x in (str(q.get(f"{side}_{k}") or "") for k in ("city", "code")))


def _fill_from_text(q: dict, text: str, today: date):
    """Deterministic backup for what small models drop: regions, month names and lengths of stay in the message."""
    t = _norm(text)
    region = next((r for w, r in REGION_WORDS if re.search(rf"\b{w}\b", t)), None)
    if q.get("destination_region") and not region:
        q["destination_region"] = None  # a region the message never names is invented
    if not q.get("destination_region"):
        if region:
            q["destination_region"] = region
            if not named(q, "destination", t):  # "e qualquer lugar da Ásia?" keeps no city from before
                q.update(destination_city=None, destination_code=None, destination_airports=None)
    months = list(dict.fromkeys(MONTHS[w] for w in re.findall(r"[a-z]+", t) if w in MONTHS))
    onward = bool(re.search(r"a partir d|from .*(onward|on\b)|desde", t))
    if months and not q.get("depart_from"):
        y = int(m.group(1)) if (m := re.search(r"\b(20\d\d)\b", t)) else None
        m0, m1 = months[0], months[-1]
        y0 = y or (today.year if date(today.year, m0, 1) >= today.replace(day=1) else today.year + 1)
        start = date(y0, m0, 1)
        end = _month_end(y0 + (m1 < m0), m1)
        q["depart_from"], q["depart_to"] = start.isoformat(), end.isoformat()
    if onward and months and q.get("depart_from"):
        d0 = date.fromisoformat(q["depart_from"])
        end = _month_end(d0.year + (d0.month == 12), d0.month % 12 + 1).isoformat()
        if (q.get("depart_to") or "") < end:  # "a partir de abril" = April and May, not just April 1
            q["depart_to"] = end
    if not q.get("min_days"):
        if m := re.search(r"(\d+|um|uma|one|dois|duas|two|tres|three|quatro|four)\s*(semanas?|weeks?)", t):
            n = int(m.group(1)) if m.group(1).isdigit() else NUMBERS[m.group(1)]
            q["min_days"] = q["max_days"] = 7 * n
        elif m := re.search(r"(\d+)\s*(?:a|e|-|to|and)\s*(\d+)\s*(dias|days)", t):  # "entre 10 e 14 dias"
            q["min_days"], q["max_days"] = int(m.group(1)), int(m.group(2))
        elif m := re.search(r"(\d+)\s*(dias|days)", t):
            q["min_days"] = int(m.group(1))


def extract(text: str, current: dict | None = None, today: date | None = None) -> dict:
    """Request -> search parameters. With `current`, the message refines that search. Adds '_provider'."""
    system = EXTRACT_PROMPT.format(today=(today or date.today()).isoformat())
    if current:
        system += ("\n\nThe user is refining this search. Return it updated with the message, keeping every field "
                   "the user doesn't change; replace it entirely only for a clearly new trip:\n"
                   + json.dumps({k: v for k, v in current.items() if not k.startswith("_")}, ensure_ascii=False))
    errors = []
    for p in EXTRACT_ORDER:
        try:
            if p == "claude":
                r = _clients[p].messages.create(model=PROVIDERS[p][2], max_tokens=8000, system=system,
                                                messages=[{"role": "user", "content": text}])
                raw = "".join(b.text for b in r.content if b.type == "text")
            else:
                effort = {"reasoning_effort": EXTRACT_EFFORT} if p == "ollama" and EXTRACT_EFFORT else {}
                r = _clients[p].chat.completions.create(  # temperature 0: same message, same reading
                    model=PROVIDERS[p][2], response_format={"type": "json_object"}, temperature=0, seed=1, **effort,
                    messages=[{"role": "system", "content": system}, {"role": "user", "content": text}])
                raw = r.choices[0].message.content or ""
            q = json.loads(raw[raw.find("{"):raw.rfind("}") + 1])
            _fill_from_text(q, text, today or date.today())
            if current:
                # small models drop fields while refining ("e saindo de Lisboa?" came back without a destination),
                # and a half-filled draft must keep what earlier answers already said
                moved = {side for side in ("origin", "destination")
                         if (q.get(f"{side}_code") and q.get(f"{side}_code") != current.get(f"{side}_code"))
                         or (q.get(f"{side}_city") and _norm(str(q[f"{side}_city"])) != _norm(str(current.get(f"{side}_city") or "")))}
                if q.get("destination_region") and not (q.get("destination_city") or q.get("destination_code")):
                    moved.add("destination")  # "e qualquer lugar da Ásia?": don't pull the old city back in
                for side in list(moved):
                    # a place the message never mentions is the model copying an example (origin became Lisbon)
                    if not named(q, side, _norm(text)) and not (side == "destination" and q.get("destination_region")):
                        moved.discard(side)
                        q.update({k: v for k, v in current.items() if k.startswith(side)})
                        if side == "destination":
                            q["destination_region"] = current.get("destination_region")
                q.update({k: v for k, v in current.items()
                          if not k.startswith("_") and k != "missing" and q.get(k) in (None, "")
                          and k.split("_")[0] not in moved})  # a new origin must not inherit the old city name
                t = _norm(text)
                if not any(w in MONTHS for w in re.findall(r"[a-z]+", t)) and not re.search(r"\d{1,2}/\d{1,2}", t):
                    # no month or date in the message: dates can't have changed (the model copied the example's March)
                    q["depart_from"], q["depart_to"] = current.get("depart_from"), current.get("depart_to")
            else:
                if q.get("min_days") and not q.get("round_trip"):
                    q["round_trip"] = True  # "viagem de pelo menos 15 dias" read as one-way: a stay implies a return
                for side in ("origin", "destination"):
                    if not named(q, side, _norm(text)):  # invented place ("quero viajar pra Europa" got an origin)
                        q.update({f"{side}_city": None, f"{side}_code": None, f"{side}_airports": None})
            return {**q, "_provider": p}
        except Exception as e:
            errors.append(f"{p}: {type(e).__name__}: {str(e)[:120]}")
    raise RuntimeError("não consegui interpretar o pedido:\n" + "\n".join(errors))


def reset(chat: int):
    for p in PROVIDERS:
        history.pop((chat, p), None)
