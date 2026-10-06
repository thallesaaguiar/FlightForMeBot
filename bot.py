# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp>=1.2,<2", "httpx", "anthropic>=1,<2", "openai>=3,<4"]
# ///
"""Telegram front-end: alert commands (no LLM) + optional free-text chat with the flight agent (llm.py)."""
import os
import re
import time

import httpx

import alerts
import llm
import search
import server  # noqa: F401 - loads .env and db
from alerts import tr

TG = f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}"
ALLOWED = {int(c) for c in os.environ.get("TELEGRAM_CHAT_ID", "").split(",") if c.strip()}
OPEN_SIGNUP = os.environ.get("OPEN_SIGNUP") == "1"   # anyone can use /alerta; LLM chat stays ALLOWED-only
CHAT_LLM = os.environ.get("CHAT_LLM", "1") == "1" and bool(llm.ORDER)  # 0 = commands only, zero LLM spend
CHECK_EVERY = 6 * 3600
# idle time before a chat forgets the search; a message naming a new route replaces it anyway
SESSION_TTL = int(os.environ.get("SESSION_MINUTES", "60")) * 60
CHAT_MODE = os.environ.get("CHAT_MODE", "buscar")  # what free text does outside a session: buscar | agente
sessions: dict[int, dict] = {}   # chat -> {"mode": "buscar" | "agente", "q": last validated search or None}
last_seen: dict[int, float] = {}
DEEP = re.compile(r"data por data|dia a dia|dia por dia|todas as datas|todas as combina|cada data|varredura"
                  r"|every date|day by day|all dates")
MORE = re.compile(r"mais op|outras op|outras data|outras combina|mais combina|mais barat|outras busca|mais resultad"
                  r"|more option|other date|cheaper|more result")
LANG_CHOICES = [("🇧🇷 Português", "lang:pt"), ("🇬🇧 English", "lang:en")]


def help_text(level: str, lang: str | None) -> str:
    """level: basic (alerts only), user (guided search), admin (plus /agente and /comparar)."""
    text = tr(lang,
              "Comandos:\n/alerta - criar alerta de preço (mande só /alerta para ver o formato)\n"
              "/alertas - listar os seus\n/remover N - apagar o alerta N\n/plano - ver o seu limite",
              "Commands:\n/alerta - create a price alert (send just /alerta to see the format)\n"
              "/alertas - list yours\n/remover N - delete alert N\n/plano - see your limits")
    text += tr(lang, "\n/idioma - mudar o idioma", "\n/idioma - change the language")
    if level in ("user", "admin"):
        text += tr(lang,
                   "\n/buscar PEDIDO - nova busca de passagens\n/new - começar do zero\n\n"
                   "Ou escreva direto, ex: \"Madrid para Tóquio em abril, 15 dias\". Depois é só ajustar "
                   f"(\"e em maio?\"). A conversa zera após {SESSION_TTL // 60} min parado.",
                   "\n/buscar REQUEST - new flight search\n/new - start over\n\n"
                   "Or just write, e.g. \"Madrid to Tokyo in April, 15 days\". Then refine it "
                   f"(\"what about May?\"). The chat resets after {SESSION_TTL // 60} idle minutes.")
    if level == "admin":
        text += tr(lang,
                   f"\n\nAdmin:\n/agente PERGUNTA - a IA planeja a busca (gasta até {llm.MAX_SEARCHES} buscas)"
                   "\n/comparar PERGUNTA - roda todas as IAs e compara\n/falhas - pedidos que o bot não entendeu",
                   f"\n\nAdmin:\n/agente QUESTION - the AI plans the search (up to {llm.MAX_SEARCHES} searches)"
                   "\n/comparar QUESTION - run every AI and compare\n/falhas - requests the bot did not understand")
    return text


def start_messages(level: str, lang: str | None, name: str) -> list[str]:
    hello = tr(lang,
               f"Olá{', ' + name if name else ''}! Eu procuro passagens baratas e aviso quando o preço cai.\n"
               "Mande /comandos para ver tudo o que eu faço.\n\n"
               "Os pedidos que eu não entender ficam guardados para melhorar o bot.",
               f"Hi{' ' + name if name else ''}! I look for cheap flights and tell you when prices drop.\n"
               "Send /comandos to see everything I do.\n\n"
               "Requests I fail to understand are saved to improve the bot.")
    if level == "basic":
        return [hello, tr(lang,
                          "Para eu avisar quando o preço cair, crie um alerta. Exemplos:\n\n"
                          "/alerta GRU LIS 20/01 1500 - só ida\n"
                          "/alerta GRU LIS 20/01 05/02 ±5 3000 - ida e volta, cada data ±5 dias\n"
                          "/alerta GRU LIS 01/01-28/02 7-10d 3000 - viagem de 7 a 10 dias na janela",
                          "To hear when a price drops, create an alert (dates are day/month). Examples:\n\n"
                          "/alerta GRU LIS 20/01 1500 - one way\n"
                          "/alerta GRU LIS 20/01 05/02 ±5 3000 - round trip, each date ±5 days\n"
                          "/alerta GRU LIS 01/01-28/02 7-10d 3000 - 7 to 10 day trip inside the window")]
    return [hello, tr(lang,
                      "É só escrever o que procura. Alguns exemplos:\n\n"
                      "Madrid para Tóquio em abril ou maio de 2027, ficando pelo menos 15 dias, até 1 escala\n\n"
                      "São Paulo para Lisboa, só ida, dia 20 de janeiro\n\n"
                      "Rio para Paris, vou 10/12 e volto 28/12\n\n"
                      "Saindo de Madrid, 10 dias em qualquer lugar da Europa em novembro, até R$ 1.500\n\n"
                      "Porto para Nova York em julho, entre 10 e 14 dias, só voo direto\n\n"
                      "Depois de uma busca dá para ajustar sem repetir tudo:\n"
                      "\"e em junho?\" · \"aceito 2 escalas\" · \"e saindo de Lisboa?\" · \"fico só 10 dias\"\n\n"
                      "Para acompanhar um preço, crie um alerta (mande /alerta para ver o formato).",
                      "Just write what you are looking for. Some examples:\n\n"
                      "Madrid to Tokyo in April or May 2027, staying at least 15 days, up to 1 stop\n\n"
                      "Sao Paulo to Lisbon, one way, January 20\n\n"
                      "Rio to Paris, leaving 10/12 and back 28/12\n\n"
                      "From Madrid, 10 days anywhere in Europe in November, under R$ 1,500\n\n"
                      "Porto to New York in July, 10 to 14 days, nonstop only\n\n"
                      "After a search you can refine it without repeating everything:\n"
                      "\"what about June?\" · \"2 stops is fine\" · \"from Lisbon instead?\" · \"only 10 days\"\n\n"
                      "To track a price, create an alert (send /alerta to see the format).")]


def send(chat_id: int, text: str, links: list[dict] | None = None, choices: list[tuple[str, str]] | None = None):
    """Plain text; links become URL buttons and choices callback buttons under the last chunk
    (links fall back to text if Telegram rejects them)."""
    chunks = [text[i:i + 4000] for i in range(0, len(text), 4000)] or [""]
    for i, chunk in enumerate(chunks):
        body = {"chat_id": chat_id, "text": chunk}
        if i == len(chunks) - 1 and (links or choices):
            rows = [[{"text": l["label"][:60], "url": l["url"]}] for l in (links or [])[:8]]
            if choices:
                rows.append([{"text": label, "callback_data": data} for label, data in choices])
            body["reply_markup"] = {"inline_keyboard": rows}
        r = httpx.post(f"{TG}/sendMessage", json=body, timeout=30).json()
        if not r.get("ok") and links and "reply_markup" in body:
            print("buttons rejected:", r.get("description"))
            send(chat_id, chunk)
            for l in links[:8]:
                if len(l["url"]) < 3900:  # booking URLs can exceed a whole message
                    send(chat_id, f"{l['label']}:\n{l['url']}")


def typing(chat_id: int):
    httpx.post(f"{TG}/sendChatAction", json={"chat_id": chat_id, "action": "typing"}, timeout=10)


def ask_language(chat: int):
    send(chat, "Escolha o idioma · Choose your language", choices=LANG_CHOICES)


def command(chat: int, name: str, text: str, level: str, lang: str | None) -> str | list[str]:
    """Reply to a slash command; a list means several messages in a row."""
    args = text.split()
    cmd = args[0].split("@")[0].lower()  # "/alerta@MeuBot" in groups
    try:
        if cmd == "/alerta":
            return alerts.add_alert(chat, name, text, lang)
        if cmd == "/alertas":
            return alerts.list_alerts(chat, lang)
        if cmd == "/remover":
            if len(args) != 2 or not args[1].lstrip("#").isdigit():
                return tr(lang, "Uso: /remover N (veja os números em /alertas)", "Usage: /remover N (numbers are in /alertas)")
            n = int(args[1].lstrip("#"))
            if alerts.remove_alert(chat, n):
                return tr(lang, f"Alerta #{n} removido.", f"Alert #{n} deleted.")
            return tr(lang, f"Alerta #{n} não encontrado.", f"Alert #{n} not found.")
        if cmd == "/plano":
            return alerts.plan_info(chat, lang)
        if cmd == "/new":
            llm.reset(chat)
            return tr(lang, "Começando do zero.", "Starting over.")
        if cmd == "/id":
            return tr(lang, f"O seu chat id é {chat}.", f"Your chat id is {chat}.")
        if cmd == "/comandos":
            return help_text(level, lang)
    except ValueError as e:
        return str(e)
    return tr(lang, "Comando desconhecido. Mande /comandos para ver a lista.", "Unknown command. Send /comandos for the list.")


def buscar(chat: int, name: str, text: str, s: dict, admin: bool, lang: str | None):
    """Guided search; inside a session the message refines the previous search. Non-admins have a monthly quota."""
    used, limit = alerts.buscas_used(chat)
    if not admin and used >= limit:
        send(chat, tr(lang, f"Você já usou as {limit} buscas deste mês. Os alertas continuam funcionando (/alertas).",
                      f"You have used your {limit} searches this month. Alerts keep working (/alertas)."))
        return
    try:
        q = llm.extract(text, s["q"])
        problem = search.validate(q, lang=lang)
        if problem:
            s["q"] = q  # keep the half-filled draft: the answer completes it instead of starting over
            alerts.log_miss(chat, text, f"asked: {problem}")
            send(chat, problem)  # clarifying costs no quota
            return
        deep = admin and bool(DEEP.search(search._norm(text)))  # 8 dates at once costs 8 searches: admins only
        more = deep or bool(MORE.search(search._norm(text)))  # "mais opções", "outras datas", "mais barato"
        same = lambda d: {k: v for k, v in (d or {}).items() if not k.startswith("_") and not k.endswith("_text") and k != "missing"}
        if s["q"] and same(q) == same(s["q"]) and not more:
            alerts.log_miss(chat, text, "refinement changed nothing")
            send(chat, tr(lang, "Não entendi o que mudar nessa busca. Posso ajustar: datas ou mês, dias de viagem, escalas, "
                                "duração máxima do voo (ex: \"até 19h de voo\"), orçamento, companhia, origem, destino "
                                "ou região. Para ver outras datas, mande \"mais opções\". Para uma busca nova, use /buscar.",
                          "I couldn't tell what to change in this search. I can adjust: dates or month, trip length, stops, "
                          "maximum flight time (e.g. \"up to 19h per flight\"), budget, airline, origin, destination or "
                          "region. For other dates, send \"more options\". For a new search, use /buscar."))
            return
        s["q"] = q
        tried = s.setdefault("tried", set())  # date pairs / destinations already checked live in this session
        if not more:
            tried.clear()
        msg, links, checked = search.run(q, lang, frozenset(tried), search.DEEP if deep else search.LIVE)
        tried.update(checked)
        alerts.count_busca(chat, name)
        send(chat, msg + tr(lang, "\n\nPode ajustar em texto (ex: \"e em junho?\", \"aceito 2 escalas\"). ",
                            "\n\nYou can refine it in text (e.g. \"what about June?\", \"2 stops is fine\"). ")
             + tr(lang, f"Interpretado por {q['_provider']}.", f"Read by {q['_provider']}."), links)
    except Exception as e:
        alerts.log_miss(chat, text, f"error: {type(e).__name__}: {e}")
        send(chat, tr(lang, "Erro", "Error") + f": {type(e).__name__}: {str(e)[:300]}")


def on_callback(cb: dict):
    """Inline button presses; today only the language picker."""
    httpx.post(f"{TG}/answerCallbackQuery", json={"callback_query_id": cb["id"]}, timeout=10)
    chat = (cb.get("message") or {}).get("chat", {}).get("id")
    data = cb.get("data") or ""
    if not chat or not data.startswith("lang:") or (chat not in ALLOWED and not OPEN_SIGNUP):
        return
    lang, name = data[5:], cb.get("from", {}).get("first_name", "")
    alerts.set_lang(chat, name, lang)
    for msg in start_messages(level_of(chat), lang, name):
        send(chat, msg)


def level_of(chat: int) -> str:
    return "admin" if CHAT_LLM and chat in ALLOWED else "user" if CHAT_LLM else "basic"


def main():
    offset, last_check = 0, 0.0
    print(f"bot running, llm={CHAT_LLM and llm.ORDER}, open_signup={OPEN_SIGNUP}, allowed={ALLOWED or 'nobody'}")
    while True:
        if time.time() - last_check > CHECK_EVERY:
            last_check = time.time()
            for chat, msg, links in alerts.check_all():
                send(chat, msg, links)
        try:
            updates = httpx.get(f"{TG}/getUpdates", params={"offset": offset, "timeout": 30}, timeout=40).json()["result"]
        except (httpx.HTTPError, KeyError) as e:
            print("poll failed:", e)
            time.sleep(5)
            continue
        for u in updates:
            offset = u["update_id"] + 1
            if u.get("callback_query"):
                on_callback(u["callback_query"])
                continue
            m = u.get("message") or {}
            chat, text = m.get("chat", {}).get("id"), (m.get("text") or "").strip()
            if not chat or not text:
                continue
            if chat not in ALLOWED and not OPEN_SIGNUP:
                send(chat, f"Bot privado. O seu chat id é {chat}. · Private bot. Your chat id is {chat}.")
                continue
            sender = m.get("from", {})
            name = sender.get("first_name", "")
            # chosen language, else guess from the Telegram app language
            lang = alerts.get_lang(chat) or ("pt" if str(sender.get("language_code", "pt")).startswith("pt") else "en")
            level = level_of(chat)
            admin, can_search = level == "admin", level != "basic"
            cmd = text.split()[0].split("@")[0].lower()
            if can_search:
                now = time.time()
                if now - last_seen.get(chat, 0) > SESSION_TTL:  # idle: forget the search and the agent chat
                    sessions.pop(chat, None)
                    llm.reset(chat)
                last_seen[chat] = now
            if cmd in ("/start", "/idioma", "/language"):
                ask_language(chat)
                continue
            if cmd == "/falhas" and admin:
                send(chat, alerts.recent_misses())
                continue
            if cmd in ("/agente", "/comparar", "/falhas") and not admin:
                send(chat, tr(lang, "Esse comando é só para o administrador. Use /buscar ou escreva o que procura.",
                              "That command is for the admin only. Use /buscar or just write what you are looking for."))
                continue
            if can_search and cmd in ("/buscar", "/agente"):
                request = text.partition(" ")[2].strip()
                mode = cmd[1:]
                if not request:
                    sessions[chat] = {"mode": mode, "q": None}
                    send(chat, tr(lang, "O que procura?", "What are you looking for?"))
                    continue
                llm.reset(chat)
                sessions[chat] = {"mode": mode, "q": None}  # explicit command = fresh start
                text, cmd = request, ""
            if admin and cmd == "/comparar":
                question = text.partition(" ")[2].strip()
                if not question:
                    send(chat, "Uso: /comparar voos GRU→LIS dia 20/01")
                    continue
                send(chat, f"Comparando {', '.join(llm.ORDER)}... (as buscas repetidas saem do cache, "
                           "então as primeiras IAs gastam mais cota)")
                for report, links in llm.compare(question):
                    send(chat, report, links)
                    typing(chat)
                continue
            if cmd.startswith("/"):
                if cmd == "/new":
                    sessions.pop(chat, None)
                reply = command(chat, name, text, level, lang)
                for msg in [reply] if isinstance(reply, str) else reply:
                    send(chat, msg)
                continue
            if not can_search:
                send(chat, tr(lang, "A busca por conversa não está ativa. Use /alerta para monitorar preços ou /comandos.",
                              "Search by chat is off. Use /alerta to track prices, or /comandos."))
                continue
            s = sessions.setdefault(chat, {"mode": CHAT_MODE if admin else "buscar", "q": None})
            typing(chat)
            if s["mode"] == "agente" and admin:
                send(chat, *llm.ask(chat, text))
            else:
                buscar(chat, name, text, s, admin, lang)


if __name__ == "__main__":
    main()
