"""/buscar: an LLM only turns the request into parameters; the search strategy below is fixed code.

Strategy: score every date pair on free cached prices, pick up to BUSCAR_LIVE well-spread candidates
(cached fares first, then one-way estimates, then evenly spaced dates when the cache is empty),
confirm them with live searches and filter by stops. The reply is built here, not by a model.
"""
import json
import os
import re
import unicodedata
from datetime import date, timedelta

import httpx

import alerts
import server
from alerts import money, tr

LIVE = int(os.environ.get("BUSCAR_LIVE", "3"))  # paid searches per /buscar
DEFAULT_WINDOW = (13, 103)  # days after tomorrow searched when the user gives no dates (~2 weeks to ~3 months)
MUCH_CHEAPER = 0.8  # a flight with more stops than asked is shown only if it costs <= 80% of the best allowed one

# "anywhere in Europe" requests: region -> ISO country codes. Fixed in code so the model can't invent a country.
# ponytail: no themes (beach, ski); add a hand-picked city list per theme if people ask for it
REGIONS = {
    "europe": set("AL AD AT BE BA BG HR CY CZ DK EE FI FR DE GR HU IS IE IT XK LV LI LT LU MT MD MC ME NL MK NO PL PT "
                  "RO SM RS SK SI ES SE CH TR UA GB".split()),
    "south_america": set("AR BO BR CL CO EC GY PY PE SR UY VE GF".split()),
    "north_america": set("US CA MX GT BZ SV HN NI CR PA CU DO HT JM PR BS BB TT AW CW BQ KY TC VG VI LC GD VC AG KN DM "
                         "MQ GP SX MF BL".split()),
    "asia": set("JP CN KR TW HK MO MN TH VN LA KH MM MY SG ID PH BN TL IN PK BD LK NP BT MV KZ UZ KG TJ TM".split()),
    "middle_east": set("AE SA QA BH KW OM JO LB IL IQ IR".split()),
    "africa": set("DZ AO BJ BW BF BI CM CV CF TD KM CG CD CI DJ EG GQ ER SZ ET GA GM GH GN GW KE LS LR LY MG MW ML MR "
                  "MU MA MZ NA NE NG RW ST SN SC SL SO ZA SS SD TZ TG TN UG ZM ZW RE YT".split()),
    "oceania": set("AU NZ FJ PF NC PG WS TO VU".split()),
    "domestic": None,  # same country as the origin
    "anywhere": None,
}
REGION_NAMES = {"europe": ("a Europa", "Europe"), "south_america": ("a América do Sul", "South America"),
                "north_america": ("a América do Norte e Central", "North and Central America"), "asia": ("a Ásia", "Asia"),
                "middle_east": ("o Oriente Médio", "the Middle East"), "africa": ("a África", "Africa"),
                "oceania": ("a Oceania", "Oceania"), "domestic": ("destinos no mesmo país", "the same country"),
                "anywhere": ("qualquer lugar", "anywhere")}


_places = None


def _norm(s: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFKD", s) if not unicodedata.combining(ch)).lower().strip()


def places():
    """Travelpayouts city/airport reference data (free, downloaded once into data/)."""
    global _places
    if _places is None:
        raw = {}
        for kind in ("cities", "airports"):
            f = server.ROOT / "data" / f"{kind}.json"
            if not f.exists():
                f.parent.mkdir(exist_ok=True)
                r = httpx.get(f"https://api.travelpayouts.com/data/en/{kind}.json", timeout=120)
                r.raise_for_status()
                f.write_bytes(r.content)
            raw[kind] = json.loads(f.read_text())
        by_name: dict[str, list] = {}
        for c in raw["cities"]:
            for n in {c["name"], (c.get("name_translations") or {}).get("en")} - {None}:
                by_name.setdefault(_norm(n), []).append(c)
        city_airports: dict[str, list] = {}
        for a in raw["airports"]:
            if a.get("iata_type") == "airport" and a.get("flightable"):
                city_airports.setdefault(a["city_code"], []).append(a["code"])
        _places = (by_name, {c["code"]: c for c in raw["cities"]}, {a["code"]: a["city_code"] for a in raw["airports"]},
                   city_airports)
    return _places


def resolve(city: str | None, code_hint: str | None, airports_hint: str | None) -> tuple[str, str] | None:
    """(city code, airports for the live search) from the city NAME; the model's code only breaks ties.
    Models invent plausible codes (Porto -> POR, which is Pori in Finland), the name lookup doesn't."""
    by_name, cities, airport_city, city_airports = places()
    hint = str(code_hint or "").upper().strip()
    matches = by_name.get(_norm(str(city or "")), [])
    if matches:
        c = (next((c for c in matches if c["code"] == hint), None)
             or next((c for c in matches if c.get("has_flightable_airport")), matches[0]))
        code = c["code"]
    elif hint in cities:
        code = hint
    elif hint in airport_city:
        code = airport_city[hint]
    else:
        return None
    all_airports = city_airports.get(code) or [code]
    wanted = [x for x in re.split(r"[,\s]+", str(airports_hint or "").upper()) if x in all_airports]
    return code, ",".join(wanted or all_airports)


def validate(q: dict, today: date | None = None, lang: str | None = None) -> str | None:
    """Normalize the extracted request in place; return a question for the user when it can't be searched."""
    tomorrow = (today or date.today()) + timedelta(days=1)
    region = str(q.get("destination_region") or "").lower().strip()
    # a named destination always wins over a region left over from an earlier "anywhere in Europe" search
    q["_explore"] = region in REGIONS and not (q.get("destination_city") or q.get("destination_code"))
    for side in ("origin", "destination"):
        if side == "destination" and q["_explore"]:
            q["destination_region"] = region
            continue
        place = resolve(q.get(f"{side}_city"), q.get(f"{side}_code"), q.get(f"{side}_airports"))
        if not place:
            return q.get("missing") or tr(
                lang, f"Não entendi {'a origem' if side == 'origin' else 'o destino'}. Pode dizer a cidade, o aeroporto "
                      "ou uma região (ex: Europa, América do Sul)?",
                f"I couldn't tell the {'origin' if side == 'origin' else 'destination'}. Which city, airport "
                "or region (e.g. Europe, South America)?")
        q[f"{side}_code"], q[f"{side}_airports"] = place
    try:
        q["max_price"] = float(q["max_price"]) if q.get("max_price") else None
    except (TypeError, ValueError):
        q["max_price"] = None
    try:
        q["max_hours"] = float(q["max_hours"]) if q.get("max_hours") and 1 <= float(q["max_hours"]) <= 60 else None
    except (TypeError, ValueError):
        q["max_hours"] = None
    q["missing"] = ""  # route is known: whatever the model wanted to ask is answered by the defaults below
    try:
        q["adults"] = max(1, min(9, int(q.get("adults") or 1)))
    except (TypeError, ValueError):
        q["adults"] = 1
    if not q.get("depart_from"):  # "no dates, find the cheapest": the next ~3 months
        q["depart_from"] = (tomorrow + timedelta(days=DEFAULT_WINDOW[0])).isoformat()
        q["depart_to"] = (tomorrow + timedelta(days=DEFAULT_WINDOW[1])).isoformat()
        q["_default_dates"] = True
    try:
        d0, d1 = date.fromisoformat(q["depart_from"]), date.fromisoformat(q.get("depart_to") or q["depart_from"])
    except (TypeError, ValueError):
        return tr(lang, "Não entendi as datas. Diga uma data ou um período de ida, ex: 'em março' ou '10/03'.",
                  "I couldn't read the dates. Give a departure date or period, e.g. 'in March' or '10/03'.")
    for _ in range(3):  # "20 de janeiro" read as a past year (models even say 2024): people mean the next one
        if d1 >= tomorrow:
            break
        try:
            d0, d1 = d0.replace(year=d0.year + 1), d1.replace(year=d1.year + 1)
        except ValueError:  # 29/02
            d0, d1 = d0 + timedelta(days=365), d1 + timedelta(days=365)
    d0 = max(d0, tomorrow)
    if d1 < d0:
        return tr(lang, "As datas de ida já passaram ou estão invertidas.", "The departure dates are in the past or reversed.")
    if (d1 - d0).days > alerts.MAX_WINDOW_DAYS:
        return tr(lang, f"O período de ida pode ter no máximo {alerts.MAX_WINDOW_DAYS} dias.",
                  f"The departure period can be at most {alerts.MAX_WINDOW_DAYS} days.")
    q["depart_from"], q["depart_to"] = d0.isoformat(), d1.isoformat()
    if q.get("round_trip"):
        try:
            lo = int(q.get("min_days") or 0)
            hi = int(q.get("max_days") or lo + 10)
        except (TypeError, ValueError):
            lo = hi = 0
        if not 1 <= lo <= hi <= 60:
            return tr(lang, "Quantos dias quer ficar? Ex: 'de 10 a 15 dias'.", "How many days do you want to stay? E.g. '10 to 15 days'.")
        if (d1 - d0).days == lo:  # models put the return date in depart_to: "ida 10/12 volta 28/12"
            q["depart_to"], hi = q["depart_from"], lo
        q["min_days"], q["max_days"] = lo, hi
    if q.get("max_stops") not in (0, 1, 2, None):
        q["max_stops"] = None
    return None


def _cached_fares(q: dict, a: dict, cs: list) -> list[dict]:
    ms = q.get("max_stops")
    fits = lambda *stops: ms is None or all(s is None or s <= ms for s in stops)
    if a["ret_from"]:
        ideas = alerts.round_trip_ideas(a["origin"], a["dest"], a["dep_from"], a["dep_to"], a["min_days"], a["max_days"], 50)
        return [c for c in ideas["cached_round_trips"] if fits(c["stops_out"], c["stops_back"])]
    ok, out = set(cs), []
    for m in sorted({c[0][:7] for c in cs}):
        for t in server.tp_month(a["origin"], a["dest"], m):
            if (t["departure_at"][:10], None) in ok and fits(t.get("transfers")):
                out.append({"depart": t["departure_at"][:10], "return": None, "price": t["price"]})
    return sorted(out, key=lambda c: c["price"])


def candidates(q: dict) -> tuple[list[tuple], int]:
    """Up to LIVE (departure, return) pairs at least 3 days apart, and how many cached fares were found."""
    rt = bool(q.get("round_trip"))
    d0, d1 = date.fromisoformat(q["depart_from"]), date.fromisoformat(q["depart_to"])
    a = {"origin": q["origin_code"], "dest": q["destination_code"], "dep_from": q["depart_from"], "dep_to": q["depart_to"],
         "ret_from": rt and (d0 + timedelta(days=q["min_days"])).isoformat(),
         "ret_to": rt and (d1 + timedelta(days=q["max_days"])).isoformat(),
         "min_days": q.get("min_days") or 1, "max_days": q.get("max_days") or 365}
    if not rt:
        a["ret_from"] = a["ret_to"] = None
    cs = alerts.combos(a)
    cached = _cached_fares(q, a, cs)
    picked: list[tuple] = []

    def add(pair):
        dep = date.fromisoformat(pair[0])
        if pair in cs and all(abs((dep - date.fromisoformat(p[0])).days) >= 3 for p in picked):
            picked.append(pair)

    for c in cached:
        add((c["depart"], c["return"]))
    for pair, est in alerts.scored(a, cs):
        if est != float("inf"):
            add(pair)
    span = (d1 - d0).days
    for f in (0, 0.5, 1):  # empty cache: sample the window evenly
        dep = d0 + timedelta(days=round(span * f))
        add((dep.isoformat(), (dep + timedelta(days=q["min_days"])).isoformat() if rt else None))
    return picked[:LIVE], len(cached)


def _fmt(d: str) -> str:
    return f"{d[8:]}/{d[5:7]}"


def _line(o: dict, dep: str, ret: str | None, lang: str | None) -> str:
    days = (date.fromisoformat(ret) - date.fromisoformat(dep)).days if ret else 0
    when = f"{_fmt(dep)} → {_fmt(ret)} ({days} {tr(lang, 'dias', 'days')})" if ret else _fmt(dep)
    stops = tr(lang, f"{o['stops']} escala(s)", f"{o['stops']} stop(s)") if o["stops"] else tr(lang, "direto", "nonstop")
    hours = f" · {o['minutes'] // 60}h{o['minutes'] % 60:02d}" if o.get("minutes") else ""
    return f"{when}: {money(o['price'])} · {', '.join(o['airlines'])} · {stops}{hours}"


def fits(o: dict, q: dict) -> bool:
    """Stops and duration limits on a live option (Google applies max_duration too; this double-checks the outbound)."""
    ms, mh = q.get("max_stops"), q.get("max_hours")
    return (ms is None or o["stops"] <= ms) and (not mh or not o.get("minutes") or o["minutes"] <= mh * 60)


def conditions(q: dict, lang: str | None) -> str:
    ms, mh, mp = q.get("max_stops"), q.get("max_hours"), q.get("max_price")
    return ((tr(lang, f", até {ms} escala(s)", f", up to {ms} stop(s)") if ms is not None else "")
            + (tr(lang, f", até {mh:g}h de voo", f", up to {mh:g}h per flight") if mh else "")
            + (tr(lang, f", até {money(mp)}", f", under {money(mp)}") if mp else ""))


def explore_fares(q: dict) -> list[dict]:
    """Cheapest cached fare per destination from the origin, inside the region, window, stay, stops and budget."""
    _, cities, _, _ = places()
    origin, rt, ms, maxp = q["origin_code"], bool(q.get("round_trip")), q.get("max_stops"), q.get("max_price")
    home = cities.get(origin, {}).get("country_code")
    allowed = {home} if q["destination_region"] == "domestic" else REGIONS[q["destination_region"]]
    d0, d1 = date.fromisoformat(q["depart_from"]), date.fromisoformat(q["depart_to"])
    dep_months = sorted({(d0 + timedelta(days=i)).isoformat()[:7] for i in range((d1 - d0).days + 1)})
    ret_months = sorted({(d0 + timedelta(days=i + q["min_days"])).isoformat()[:7]
                         for i in range((d1 - d0).days + q["max_days"] - q["min_days"] + 1)}) if rt else [None]
    best: dict[str, dict] = {}
    for dm in dep_months:
        for rm in ret_months:
            if rm and rm < dm:
                continue
            for t in server.tp_month(origin, None, dm, rm):
                dest, dep = t["destination"], t["departure_at"][:10]
                ret = (t.get("return_at") or "")[:10] if rt else None
                if dest == origin or (allowed is not None and cities.get(dest, {}).get("country_code") not in allowed):
                    continue
                if not q["depart_from"] <= dep <= q["depart_to"] or (maxp and t["price"] > maxp):
                    continue
                if rt and not (ret and q["min_days"] <= (date.fromisoformat(ret) - date.fromisoformat(dep)).days <= q["max_days"]):
                    continue
                if ms is not None and ((t.get("transfers") or 0) > ms or (rt and (t.get("return_transfers") or 0) > ms)):
                    continue
                if dest not in best or t["price"] < best[dest]["price"]:
                    best[dest] = {"dest": dest, "name": cities.get(dest, {}).get("name", dest), "dep": dep, "ret": ret,
                                  "price": t["price"]}
    return sorted(best.values(), key=lambda f: f["price"])


def run_explore(q: dict, lang: str | None) -> tuple[str, list[dict]]:
    """'Anywhere in Europe': rank destinations on the free cache, confirm the LIVE cheapest ones live."""
    fares = explore_fares(q)
    where = REGION_NAMES[q["destination_region"]][lang == "en"]
    rt, ms = bool(q.get("round_trip")), q.get("max_stops")
    d0, d1 = _fmt(q["depart_from"]), _fmt(q["depart_to"])
    if not fares:
        return tr(lang, f"Não achei preços em cache saindo de {q['origin_airports']} para {where} entre {d0} e {d1}"
                        + (f" até {money(q['max_price'])}" if q.get("max_price") else "")
                        + ". Tente outro período, um orçamento maior ou diga um destino.",
                  f"I found no cached fares from {q['origin_airports']} to {where} between {d0} and {d1}"
                  + (f" under {money(q['max_price'])}" if q.get("max_price") else "")
                  + ". Try another period, a bigger budget or name a destination."), []
    before = server._month_calls()
    confirmed = []
    for f in fares[:LIVE]:
        airports = (resolve(None, f["dest"], None) or (f["dest"], f["dest"]))[1]
        r = server.search_flights(q["origin_airports"], airports, f["dep"], f["ret"], limit=30, max_hours=q.get("max_hours"))
        ok = [o for o in r["options"] if fits(o, q)]
        confirmed.append({**f, "ok": ok[0] if ok else None, "url": r["google_flights_url"]})
    used = server._month_calls() - before
    live = sorted((c for c in confirmed if c["ok"]), key=lambda c: c["ok"]["price"])
    head = (tr(lang, f"Saindo de {q['origin_airports']} para {where}, ", f"From {q['origin_airports']} to {where}, ")
            + (tr(lang, f"ida e volta, {q['min_days']}-{q['max_days']} dias", f"round trip, {q['min_days']}-{q['max_days']} days")
               if rt else tr(lang, "só ida", "one way"))
            + conditions(q, lang)
            + tr(lang, f"\nIda entre {d0} e {d1}. Cache: {len(fares)} destinos; confirmei {len(fares[:LIVE])} ao vivo "
                       f"({used} buscas pagas).",
                 f"\nDeparting between {d0} and {d1}. Cache: {len(fares)} destinations; checked {len(fares[:LIVE])} live "
                 f"({used} paid searches)."))
    lines = [f"{i}) {c['name']}: {_line(c['ok'], c['dep'], c['ret'], lang)}" for i, c in enumerate(live, 1)]
    if not lines:
        lines = [tr(lang, "Os preços do cache não se confirmaram ao vivo nas datas testadas.",
                    "The cached prices did not hold up live on the dates checked.")]
    more = [f for f in fares[LIVE:LIVE + 5]]
    if more:
        lines.append(tr(lang, "\nMais ideias pelo cache (não confirmadas): ", "\nMore ideas from the cache (not checked live): ")
                     + ", ".join(f"{f['name']} ~{money(f['price'])} ({_fmt(f['dep'])})" for f in more))
    tail = tr(lang, "\nPreços por adulto. Para ver um destino em detalhe, diga o nome dele (ex: \"pode ser Roma\").",
              "\nPrices per adult. To look at one destination in detail, name it (e.g. \"Rome then\").")
    if q.get("_default_dates"):
        tail += tr(lang, "\nSem datas, busquei com ida nos próximos 3 meses.", "\nNo dates given, so I searched the next 3 months.")
    links = [{"label": f"{c['name']} {_fmt(c['dep'])}" + (f"→{_fmt(c['ret'])}" if c["ret"] else ""), "url": c["url"]}
             for c in live if c["url"]]
    return "\n".join([head, "", *lines, tail]), links


def run(q: dict, lang: str | None = None) -> tuple[str, list[dict]]:
    """Execute a validated request. Returns (message, link buttons)."""
    if q.get("_explore"):
        return run_explore(q, lang)
    picks, n_cached = candidates(q)
    before = server._month_calls()
    ms = q.get("max_stops")
    results = []
    for dep, ret in picks:
        r = server.search_flights(q["origin_airports"], q["destination_airports"], dep, ret, limit=30,
                                  max_hours=q.get("max_hours"))
        opts = r["options"]
        ok = [o for o in opts if fits(o, q)]
        results.append({"dep": dep, "ret": ret, "ok": ok[0] if ok else None, "any": opts[0] if opts else None,
                        "url": r["google_flights_url"]})
    used = server._month_calls() - before

    rt = bool(q.get("round_trip"))
    d0, d1, left = _fmt(q["depart_from"]), _fmt(q["depart_to"]), server.days_until(q["depart_from"])
    head = (f"{q['origin_airports']} → {q['destination_airports']}, "
            + (tr(lang, f"ida e volta, {q['min_days']}-{q['max_days']} dias", f"round trip, {q['min_days']}-{q['max_days']} days")
               if rt else tr(lang, "só ida", "one way"))
            + conditions(q, lang)
            + tr(lang, f"\nIda entre {d0} e {d1} (faltam {left} dias). "
                       f"Cache: {n_cached} preços; confirmei {len(picks)} datas ao vivo ({used} buscas pagas).",
                       f"\nDeparting between {d0} and {d1} ({left} days from now). "
                       f"Cache: {n_cached} fares; checked {len(picks)} dates live ({used} paid searches)."))
    found = sorted((x for x in results if x["ok"]), key=lambda x: x["ok"]["price"])
    lines = [f"{i}) {_line(x['ok'], x['dep'], x['ret'], lang)}" for i, x in enumerate(found, 1)]
    if not lines:
        lines = [tr(lang, "Nenhum voo encontrado com essas condições nas datas testadas.",
                    "No flights matched these conditions on the dates checked.")]
    best = found[0]["ok"]["price"] if found else float("inf")
    extra = [x for x in results if x["any"] and x["any"]["stops"] > (ms if ms is not None else 99)
             and x["any"]["price"] <= MUCH_CHEAPER * best]
    if extra:
        x = min(extra, key=lambda x: x["any"]["price"])
        lines.append(tr(lang, "\nCom mais escalas, bem mais barato: ", "\nWith more stops, much cheaper: ")
                     + _line(x["any"], x["dep"], x["ret"], lang))
    n = q.get("adults") or 1
    tail = tr(lang, "\nPreços por adulto" + (", ida e volta" if rt else "") + ".",
              "\nPrices per adult" + (", round trip" if rt else "") + ".")
    if n > 1 and found:
        tail += tr(lang, f" Para {n} adultos, a melhor opção dá cerca de {money(best * n)}. Buscar e comprar 1 passagem "
                         f"por vez costuma sair mais barato que {n} de uma vez (as tarifas são vendidas em lotes).",
                   f" For {n} adults the best option comes to about {money(best * n)}. Searching and buying one ticket "
                   f"at a time is often cheaper than {n} at once (fares are sold in buckets).")
    if q.get("_default_dates"):
        tail += tr(lang, "\nVocê não deu datas, então busquei com ida nos próximos 3 meses. Diga um período para refinar "
                         "(ex: \"em abril\").",
                   "\nYou gave no dates, so I searched departures in the next 3 months. Name a period to refine "
                   "(e.g. \"in April\").")
    tail += tr(lang, "\nPara acompanhar o preço, crie um /alerta.", "\nTo track the price, create an /alerta.")
    links = [{"label": f"Google Flights {_fmt(x['dep'])}" + (f"→{_fmt(x['ret'])}" if x["ret"] else ""), "url": x["url"]}
             for x in found if x["url"]]
    return "\n".join([head, "", *lines, tail]), links


def selftest():
    t = date(2026, 9, 29)
    q = {"origin_code": "mad", "origin_airports": "MAD", "destination_code": "TYO", "destination_airports": "hnd, nrt",
         "depart_from": "2027-04-01", "depart_to": "2027-05-31", "round_trip": True, "min_days": 15, "max_days": None,
         "max_stops": 1, "missing": ""}
    assert validate(q, t) is None
    assert q["origin_code"] == "MAD" and q["destination_airports"] == "HND,NRT" and q["max_days"] == 25
    assert validate({**q, "origin_code": "", "missing": "De onde?"}, t) == "De onde?"
    known = {**q, "missing": "Quais as datas?", "depart_from": None, "depart_to": None, "adults": "2"}
    assert validate(known, t) is None and known["_default_dates"] and known["adults"] == 2  # no dates: default window
    assert known["depart_from"] == "2026-10-13" and known["depart_to"] == "2027-01-11"
    assert validate({**q, "destination_code": "Tokyo"}, t)
    past = {**q, "round_trip": False, "depart_from": "2026-01-20", "depart_to": "2026-01-20"}
    assert validate(past, t) is None and past["depart_from"] == "2027-01-20"  # rolled to next year
    old = {**q, "round_trip": False, "depart_from": "2024-12-10", "depart_to": "2024-12-10"}
    assert validate(old, t) is None and old["depart_from"] == "2026-12-10"  # model wrote 2024
    assert validate({**q, "min_days": 0}, t)
    fixed = {**q, "depart_from": "2026-12-10", "depart_to": "2026-12-28", "min_days": 18, "max_days": 28}
    assert validate(fixed, t) is None and fixed["depart_to"] == "2026-12-10" and fixed["max_days"] == 18
    ow = {**q, "round_trip": False, "depart_to": None}
    assert validate(ow, t) is None and ow["depart_to"] == "2027-04-01"
    print("search selftest ok")


if __name__ == "__main__":
    selftest()
