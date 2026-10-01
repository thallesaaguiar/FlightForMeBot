"""Price alerts: /alerta parsing, storage and the periodic check. Channel-agnostic (Telegram today)."""
import re
import sqlite3
import time
from datetime import date, timedelta

import server
from server import CURRENCY, db

# ponytail: plans are set by hand in sqlite until payments exist
PLAN_LIMITS = {"free": 2, "pro": 10}
PLAN_BUSCAS = {"free": 5, "pro": 30}  # guided searches per month (each costs BUSCAR_LIVE paid searches)


def tr(lang: str | None, pt: str, en: str) -> str:
    return en if lang == "en" else pt
LIVE_EVERY = 24 * 3600  # at most 1 exploratory live search per alert per day
EXPLORE_TOP = 10        # exploratory searches rotate over the 10 best-estimated combos
MAX_WINDOW_DAYS = 120

db.executescript("""
create table if not exists users (chat_id integer primary key, name text, plan text default 'free', created real);
create table if not exists alerts (
  id integer primary key, chat_id int, origin text, dest text,
  dep_from text, dep_to text, ret_from text, ret_to text, min_days int, max_days int,
  target real, spec text, created real,
  last_price real, last_checked real, last_live real default 0, live_n int default 0,
  confirm_key text, alerted_price real);
create table if not exists buscas (chat_id int, month text, n int, primary key (chat_id, month));
create table if not exists misses (ts real, chat_id int, text text, reason text);
""")
try:
    db.execute("alter table users add column lang text")  # added after the first release
except sqlite3.OperationalError:
    pass

def usage(lang: str | None = None) -> str:
    return tr(lang,
              "Formato: /alerta ORIGEM DESTINO DATAS PREÇO\n\n"
              "/alerta GRU LIS 20/01 1500 - só ida\n"
              "/alerta GRU LIS 20/01 05/02 3000 - ida e volta\n"
              "/alerta GRU LIS 20/01 05/02 ±5 3000 - cada data ±5 dias\n"
              "/alerta GRU LIS 01/01-28/02 7-10d 3000 - viagem de 7 a 10 dias na janela\n\n"
              f"Preço-alvo em {CURRENCY}, total da viagem, 1 adulto.",
              "Format: /alerta FROM TO DATES PRICE (dates are day/month)\n\n"
              "/alerta GRU LIS 20/01 1500 - one way\n"
              "/alerta GRU LIS 20/01 05/02 3000 - round trip\n"
              "/alerta GRU LIS 20/01 05/02 ±5 3000 - each date ±5 days\n"
              "/alerta GRU LIS 01/01-28/02 7-10d 3000 - 7 to 10 day trip inside the window\n\n"
              f"Target price in {CURRENCY}, whole trip, 1 adult.")


def log_miss(chat_id: int, text: str, reason: str):
    """Requests the bot had to question or failed on: reviewed by hand and turned into extract_eval cases."""
    db.execute("insert into misses values (?,?,?,?)", (time.time(), chat_id, text[:500], reason[:300]))
    db.commit()


def recent_misses(limit: int = 20) -> str:
    rows = db.execute("select ts, chat_id, text, reason from misses order by ts desc limit ?", (limit,)).fetchall()
    if not rows:
        return "Nenhum pedido mal entendido registrado."
    return "\n\n".join(f"{time.strftime('%d/%m %H:%M', time.localtime(ts))} · {chat}\n\"{text}\"\n→ {reason}"
                       for ts, chat, text, reason in rows)


def get_lang(chat_id: int) -> str | None:
    row = db.execute("select lang from users where chat_id=?", (chat_id,)).fetchone()
    return row[0] if row else None


def set_lang(chat_id: int, name: str, lang: str):
    db.execute("insert into users (chat_id, name, created, lang) values (?,?,?,?) "
               "on conflict(chat_id) do update set lang=excluded.lang", (chat_id, name, time.time(), lang))
    db.commit()


def money(v: float) -> str:
    return f"{'R$' if CURRENCY == 'BRL' else CURRENCY} {v:,.0f}".replace(",", ".")


def _date(s: str, after: date, lang: str | None = None) -> date:
    try:
        d, m = map(int, s.split("/"))
        x = date(after.year, m, d)
        return x if x >= after else date(after.year + 1, m, d)
    except ValueError:
        raise ValueError(tr(lang, f"Data inválida: {s}", f"Invalid date: {s}"))


def parse_alert(text: str, today: date | None = None, lang: str | None = None) -> dict:
    today = today or date.today()
    tok = text.split()[1:]
    if len(tok) < 4:
        raise ValueError(usage(lang))
    origin, dest = tok[0].upper(), tok[1].upper()
    if not (re.fullmatch(r"[A-Z]{3}", origin) and re.fullmatch(r"[A-Z]{3}", dest)) or origin == dest:
        raise ValueError(tr(lang, "Use códigos IATA de 3 letras diferentes, ex: GRU LIS", "Use two different 3-letter IATA codes, e.g. GRU LIS"))
    try:
        target = float(tok[-1].replace(".", "").replace(",", "."))
    except ValueError:
        raise ValueError(tr(lang, "O último valor deve ser o preço-alvo, ex: 3000", "The last value must be the target price, e.g. 3000") + "\n\n" + usage(lang))
    if target <= 0:
        raise ValueError(tr(lang, "Preço-alvo tem de ser positivo", "The target price must be positive"))

    dates, flex, window, stay = [], 0, None, None
    for t in tok[2:-1]:
        if m := re.fullmatch(r"(?:±|\+-|\+/-)(\d{1,2})", t):
            flex = int(m[1])
        elif m := re.fullmatch(r"(\d{1,3})(?:-(\d{1,3}))?d", t.lower()):
            stay = (int(m[1]), int(m[2] or m[1]))
        elif m := re.fullmatch(r"(\d{1,2}/\d{1,2})-(\d{1,2}/\d{1,2})", t):
            window = m[1], m[2]
        elif re.fullmatch(r"\d{1,2}/\d{1,2}", t):
            dates.append(t)
        else:
            raise ValueError(tr(lang, f"Não entendi '{t}'.", f"I didn't understand '{t}'.") + "\n\n" + usage(lang))

    tomorrow = today + timedelta(days=1)
    ret = None
    if window and not dates:
        a = _date(window[0], tomorrow, lang)
        b = _date(window[1], a, lang)
        if stay:  # round trip: leave and come back inside the window
            if stay[0] > stay[1] or stay[0] < 1:
                raise ValueError(tr(lang, "Duração inválida, ex: 7-10d", "Invalid trip length, e.g. 7-10d"))
            dep, ret = (a, b - timedelta(days=stay[0])), (a + timedelta(days=stay[0]), b)
        else:     # one-way, any day in the window
            dep = (a, b)
    elif len(dates) in (1, 2) and not window:
        d1 = _date(dates[0], tomorrow, lang)
        dep = (max(d1 - timedelta(days=flex), tomorrow), d1 + timedelta(days=flex))
        if len(dates) == 2:
            d2 = _date(dates[1], d1, lang)
            ret = (d2 - timedelta(days=flex), d2 + timedelta(days=flex))
    else:
        raise ValueError(usage(lang))

    if (dep[1] - dep[0]).days > MAX_WINDOW_DAYS or (ret and (ret[1] - ret[0]).days > MAX_WINDOW_DAYS):
        raise ValueError(tr(lang, f"Janela máxima de {MAX_WINDOW_DAYS} dias", f"The window can be at most {MAX_WINDOW_DAYS} days"))
    if dep[0] > dep[1] or (ret and ret[0] > ret[1]):
        raise ValueError(tr(lang, "Janela curta demais para essa duração", "The window is too short for that trip length"))
    min_days, max_days = stay or (1, 365)
    return {"origin": origin, "dest": dest, "dep_from": dep[0].isoformat(), "dep_to": dep[1].isoformat(),
            "ret_from": ret and ret[0].isoformat(), "ret_to": ret and ret[1].isoformat(),
            "min_days": min_days, "max_days": max_days, "target": target, "spec": " ".join(tok)}


def _days(a: str, b: str) -> list[date]:
    start, end = date.fromisoformat(a), date.fromisoformat(b)
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def combos(a: dict, today: date | None = None) -> list[tuple[str, str | None]]:
    """Every (departure, return) pair the alert covers, from tomorrow on."""
    tomorrow = (today or date.today()) + timedelta(days=1)
    deps = [d for d in _days(a["dep_from"], a["dep_to"]) if d >= tomorrow]
    if not a["ret_from"]:
        return [(d.isoformat(), None) for d in deps]
    rets = _days(a["ret_from"], a["ret_to"])
    return [(d.isoformat(), r.isoformat()) for d in deps for r in rets if a["min_days"] <= (r - d).days <= a["max_days"]]


def _months(pairs, i) -> list[str]:
    return sorted({p[i][:7] for p in pairs})


def cached_best(a: dict, cs: list) -> tuple[float, tuple] | None:
    """Cheapest cached Aviasales fare matching one of the combos (free)."""
    ok, best = set(cs), None
    ret_months = _months(cs, 1) if a["ret_from"] else [None]
    for dm in _months(cs, 0):
        for rm in ret_months:
            if rm and rm < dm:
                continue
            for t in server.tp_month(a["origin"], a["dest"], dm, rm):
                # ponytail: return_at comes in UTC, may be 1 day off local for late-night returns
                key = (t["departure_at"][:10], t["return_at"][:10] if rm else None)
                if key in ok and (best is None or t["price"] < best[0]):
                    best = (t["price"], key)
    return best


def ranked(a: dict, cs: list) -> list:
    """Combos ordered by cached one-way estimates (outbound + return), unknown last. Free."""
    return [c for c, _ in scored(a, cs)]


def scored(a: dict, cs: list) -> list[tuple[tuple, float]]:
    """(combo, estimated price) sorted cheapest first; inf when the cache has no data for it."""
    def cheapest_by_day(o, d, months):
        out = {}
        for m in months:
            for t in server.tp_month(o, d, m):
                day = t["departure_at"][:10]
                out[day] = min(out.get(day, t["price"]), t["price"])
        return out
    inf = float("inf")
    out = cheapest_by_day(a["origin"], a["dest"], _months(cs, 0))
    back = cheapest_by_day(a["dest"], a["origin"], _months(cs, 1)) if a["ret_from"] else {}
    est = [(c, out.get(c[0], inf) + (back.get(c[1], inf) if c[1] else 0)) for c in cs]
    return sorted(est, key=lambda x: x[1])


def round_trip_ideas(origin: str, dest: str, dep_from: str, dep_to: str, min_days: int, max_days: int,
                     limit: int = 8) -> dict:
    """Free: every round-trip date pair in the window, scored from the Aviasales cache."""
    a = {"origin": origin, "dest": dest, "dep_from": dep_from, "dep_to": dep_to,
         "ret_from": (date.fromisoformat(dep_from) + timedelta(days=min_days)).isoformat(),
         "ret_to": (date.fromisoformat(dep_to) + timedelta(days=max_days)).isoformat(),
         "min_days": min_days, "max_days": max_days}
    cs = combos(a)
    ok, found = set(cs), {}
    for dm in _months(cs, 0):
        for rm in _months(cs, 1):
            if rm < dm:
                continue
            for t in server.tp_month(origin, dest, dm, rm):
                key = (t["departure_at"][:10], t["return_at"][:10])
                if key in ok and (key not in found or t["price"] < found[key]["price"]):
                    found[key] = {"depart": key[0], "return": key[1], "price": t["price"], "airline": t.get("airline"),
                                  "stops_out": t.get("transfers"), "stops_back": t.get("return_transfers")}
    return {"date_pairs_in_window": len(cs), "days_until_first_departure": server.days_until(dep_from),
            "cached_round_trips": sorted(found.values(), key=lambda x: x["price"])[:limit],
            "best_pairs_from_one_way_estimates": [{"depart": d, "return": r} for d, r in ranked(a, cs)[:limit]],
            "note": "cached prices are hints up to 7 days old; confirm the best 2-3 pairs with search_flights"}


def add_alert(chat_id: int, name: str, text: str, lang: str | None = None) -> str:
    spec = parse_alert(text, lang=lang)
    db.execute("insert or ignore into users (chat_id, name, created) values (?,?,?)", (chat_id, name, time.time()))
    plan = db.execute("select plan from users where chat_id=?", (chat_id,)).fetchone()[0]
    count = db.execute("select count(*) from alerts where chat_id=?", (chat_id,)).fetchone()[0]
    if count >= PLAN_LIMITS.get(plan, 0):
        raise ValueError(tr(lang, f"Limite do plano {plan}: {PLAN_LIMITS.get(plan, 0)} alertas. Remova um com /remover ou veja /plano",
                            f"Your {plan} plan allows {PLAN_LIMITS.get(plan, 0)} alerts. Delete one with /remover or see /plano"))
    cur = db.execute("""insert into alerts (chat_id, origin, dest, dep_from, dep_to, ret_from, ret_to, min_days, max_days,
                        target, spec, created) values (:chat_id, :origin, :dest, :dep_from, :dep_to, :ret_from, :ret_to,
                        :min_days, :max_days, :target, :spec, :created)""",
                     {**spec, "chat_id": chat_id, "created": time.time()})
    db.commit()
    n = len(combos(spec))
    rt = bool(spec["ret_from"])
    return tr(lang,
              f"Alerta #{cur.lastrowid} criado: {spec['origin']}→{spec['dest']} ({'ida e volta' if rt else 'só ida'}), "
              f"{n} combinações de datas, alvo {money(spec['target'])}.\nVerifico a cada 6h e aviso aqui quando achar.",
              f"Alert #{cur.lastrowid} created: {spec['origin']}→{spec['dest']} ({'round trip' if rt else 'one way'}), "
              f"{n} date combinations, target {money(spec['target'])}.\nI check every 6h and tell you here when I find it.")


def list_alerts(chat_id: int, lang: str | None = None) -> str:
    rows = db.execute("select id, origin, dest, spec, target, last_price, last_checked from alerts where chat_id=? order by id",
                      (chat_id,)).fetchall()
    if not rows:
        return tr(lang, "Nenhum alerta. Crie com /alerta", "No alerts yet. Create one with /alerta")
    lines = []
    for id_, o, d, spec, target, last, checked in rows:
        hours = int((time.time() - checked) / 3600) if checked else None
        seen = tr(lang, f"último {money(last)}", f"last {money(last)}") if last else tr(lang, "sem preço ainda", "no price yet")
        ago = tr(lang, f" (há {hours}h)", f" ({hours}h ago)") if hours is not None else ""
        lines.append(f"#{id_} {spec}\n   {tr(lang, 'alvo', 'target')} {money(target)} · {seen}{ago}")
    return "\n".join(lines)


def remove_alert(chat_id: int, alert_id: int) -> bool:
    cur = db.execute("delete from alerts where id=? and chat_id=?", (alert_id, chat_id))
    db.commit()
    return cur.rowcount > 0


def _plan(chat_id: int) -> str:
    row = db.execute("select plan from users where chat_id=?", (chat_id,)).fetchone()
    return row[0] if row else "free"


def buscas_used(chat_id: int) -> tuple[int, int]:
    """(guided searches used this month, monthly limit of the user's plan)."""
    row = db.execute("select n from buscas where chat_id=? and month=?",
                     (chat_id, date.today().strftime("%Y-%m"))).fetchone()
    return (row[0] if row else 0), PLAN_BUSCAS.get(_plan(chat_id), 0)


def count_busca(chat_id: int, name: str):
    db.execute("insert or ignore into users (chat_id, name, created) values (?,?,?)", (chat_id, name, time.time()))
    db.execute("insert into buscas values (?,?,1) on conflict(chat_id, month) do update set n=n+1",
               (chat_id, date.today().strftime("%Y-%m")))
    db.commit()


def plan_info(chat_id: int, lang: str | None = None) -> str:
    plan = _plan(chat_id)
    used = db.execute("select count(*) from alerts where chat_id=?", (chat_id,)).fetchone()[0]
    b_used, b_limit = buscas_used(chat_id)
    return tr(lang,
              f"Plano {plan}: {used}/{PLAN_LIMITS.get(plan, 0)} alertas, {b_used}/{b_limit} buscas neste mês.\n"
              "Assinatura com mais alertas e buscas: em breve.",
              f"Plan {plan}: {used}/{PLAN_LIMITS.get(plan, 0)} alerts, {b_used}/{b_limit} searches this month.\n"
              "A subscription with more alerts and searches is coming soon.")


def check_alert(a: dict, now: float) -> tuple[str, list[dict]] | None:
    """Returns (message, booking links) when the user must be told something."""
    cs = combos(a)
    lang = get_lang(a["chat_id"])
    if not cs:
        db.execute("delete from alerts where id=?", (a["id"],))
        return tr(lang, f"Alerta #{a['id']} ({a['spec']}) expirou: as datas já passaram.",
                  f"Alert #{a['id']} ({a['spec']}) expired: the dates have passed."), []
    best = cached_best(a, cs)
    upd = {"last_checked": now}
    pick = None
    if best and best[0] <= a["target"] and a["confirm_key"] != f"{best[1]}|{best[0]}":
        pick = best[1]  # cache says it's cheap: confirm live
        upd["confirm_key"] = f"{best[1]}|{best[0]}"
    elif now - (a["last_live"] or 0) >= LIVE_EVERY:
        top = ranked(a, cs)[:EXPLORE_TOP]
        pick = top[(a["live_n"] or 0) % len(top)]  # rotate so days without cache still get sampled
        upd.update(last_live=now, live_n=(a["live_n"] or 0) + 1)

    msg, price = None, best and best[0]
    if pick:
        r = server.search_flights(a["origin"], a["dest"], pick[0], pick[1], limit=1)
        if r["options"]:
            price = r["options"][0]["price"]
            if price <= a["target"] and (a["alerted_price"] is None or price < a["alerted_price"]):
                upd["alerted_price"] = price
                when = (tr(lang, "ida ", "out ") + f"{pick[0][8:]}/{pick[0][5:7]}"
                        + (tr(lang, ", volta ", ", back ") + f"{pick[1][8:]}/{pick[1][5:7]}" if pick[1] else ""))
                opt = r["options"][0]
                stops = tr(lang, f"{opt['stops']} escala(s)", f"{opt['stops']} stop(s)") if opt["stops"] else tr(lang, "voo direto", "nonstop")
                links = [{"label": tr(lang, "Ver no Google Flights", "Open in Google Flights"), "url": r["google_flights_url"]}] if r["google_flights_url"] else []
                try:  # 1-2 extra paid searches, only when the alert actually fires
                    links = server.booking_options(a["origin"], a["dest"], pick[0], pick[1])["links"][:5] + links
                except Exception as e:
                    print(f"alert #{a['id']} booking links failed: {type(e).__name__}: {e}")
                msg = (f"✈️ {a['origin']}→{a['dest']} {when}: {money(price)} ({tr(lang, 'alvo', 'target')} {money(a['target'])})\n"
                       f"{', '.join(opt['airlines'])} · {stops}\n#{a['id']} · /remover {a['id']}", links)
    if price:
        upd["last_price"] = price
    db.execute(f"update alerts set {', '.join(f'{k}=:{k}' for k in upd)} where id=:id", {**upd, "id": a["id"]})
    db.commit()
    return msg


def check_all() -> list[tuple[int, str, list[dict]]]:
    cols = [c[1] for c in db.execute("pragma table_info(alerts)")]
    out = []
    for row in db.execute("select * from alerts").fetchall():
        a = dict(zip(cols, row))
        try:
            if res := check_alert(a, time.time()):
                out.append((a["chat_id"], *res))
        except Exception as e:
            print(f"alert #{a['id']} failed: {type(e).__name__}: {e}")
    db.commit()
    return out


def selftest():
    t = date(2026, 9, 28)
    one = parse_alert("/alerta gru lis 20/01 1.500", t)
    assert one["dep_from"] == "2027-01-20" and one["ret_from"] is None and one["target"] == 1500
    assert len(combos(one, t)) == 1

    flex = parse_alert("/alerta GRU LIS 20/01 05/02 ±5 3000", t)
    assert (flex["dep_from"], flex["dep_to"], flex["ret_from"], flex["ret_to"]) == \
        ("2027-01-15", "2027-01-25", "2027-01-31", "2027-02-10")
    assert len(combos(flex, t)) == 121

    win = parse_alert("/alerta GRU LIS 01/01-28/02 7-10d 3000", t)
    cs = combos(win, t)
    assert all(7 <= (date.fromisoformat(r) - date.fromisoformat(d)).days <= 10 for d, r in cs)
    assert cs[0] == ("2027-01-01", "2027-01-08") and cs[-1][1] == "2027-02-28"

    assert parse_alert("/alerta GRU LIS 30/12 05/01 3000", t)["ret_from"] == "2027-01-05"  # year rollover
    assert parse_alert("/alerta GRU LIS 29/09 +-3 900", t)["dep_from"] == "2026-09-29"    # clamped to tomorrow
    for bad in ["/alerta GRU LIS 20/01", "/alerta GRU GRU 20/01 100", "/alerta GRU LIS 31/02 100",
                "/alerta GRU LIS 20/01 abc 100", "/alerta GRUX LIS 20/01 100", "/alerta GRU LIS 01/01-28/06 100"]:
        try:
            parse_alert(bad, t)
            raise AssertionError(bad)
        except ValueError:
            pass
    print("alerts selftest ok")


if __name__ == "__main__":
    selftest()
