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
from difflib import SequenceMatcher

import httpx

import alerts
import server
from alerts import money, tr

LIVE = int(os.environ.get("BUSCAR_LIVE", "3"))  # paid searches per /buscar
DEEP = int(os.environ.get("BUSCAR_DEEP", "8"))  # dates checked when an admin asks "data por data" / "todas as datas"
DEFAULT_WINDOW = (13, 103)  # days after tomorrow searched when the user gives no dates (~2 weeks to ~3 months)
MUCH_CHEAPER = 0.8  # a flight with more stops than asked is shown only if it costs <= 80% of the best allowed one
MAX_WINDOW = 190  # departure window for searches (alerts keep their own, smaller one): "April to September" fits
SEP_PAIRS = 2  # date pairs priced as two one-way tickets (3 paid searches each: round trip, outbound, return)
SPREAD = (0, 0.5, 1, 0.25, 0.75, 0.125, 0.375, 0.625, 0.875)  # evenly spaced dates when the cache runs out

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


_countries = None


def countries() -> tuple[dict, dict]:
    """(normalized English name -> ISO code, ISO code -> name) from Travelpayouts (free, cached in data/)."""
    global _countries
    if _countries is None:
        f = server.ROOT / "data" / "countries.json"
        if not f.exists():
            f.parent.mkdir(exist_ok=True)
            r = httpx.get("https://api.travelpayouts.com/data/en/countries.json", timeout=120)
            r.raise_for_status()
            f.write_bytes(r.content)
        raw = json.loads(f.read_text())
        _countries = ({_norm(n): c["code"] for c in raw for n in {c["name"], (c.get("name_translations") or {}).get("en")} - {None}},
                      {c["code"]: c["name"] for c in raw})
    return _countries


_airlines = None


def airline_names() -> list[str]:
    """Airline names from Travelpayouts (free, cached in data/), for spotting a preferred airline in a message."""
    global _airlines
    if _airlines is None:
        f = server.ROOT / "data" / "airlines.json"
        if not f.exists():
            f.parent.mkdir(exist_ok=True)
            r = httpx.get("https://api.travelpayouts.com/data/en/airlines.json", timeout=120)
            r.raise_for_status()
            f.write_bytes(r.content)
        _airlines = sorted({a["name"] for a in json.loads(f.read_text()) if a.get("name")})
    return _airlines


def region_name(region: str, lang: str | None) -> str:
    if region.startswith("country:"):
        return countries()[1].get(region[8:], region[8:])
    return REGION_NAMES[region][lang == "en"]


def allowed_countries(region: str, origin: str) -> set | None:
    if region.startswith("country:"):
        return {region[8:]}
    if region == "domestic":
        return {places()[1].get(origin, {}).get("country_code")}
    return REGIONS[region]


def airline_match(o: dict, q: dict) -> bool:
    """Preferred airline by name ("Air China" ~ "AirChina")."""
    want = [_norm(a).replace(" ", "") for a in q.get("airlines") or []]
    return any(w and w in _norm(x).replace(" ", "") for w in want for x in o.get("airlines", []))


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
    elif hint in cities or hint in airport_city:
        code = hint if hint in cities else airport_city[hint]
        given, real = _norm(str(city or "")), _norm(cities.get(code, {}).get("name", ""))
        # a name that isn't a city plus an unrelated code ("Japan" + JPN, a Washington heliport) is a guess, not a place
        if given and given not in real and real not in given and SequenceMatcher(None, given, real).ratio() < 0.5:
            return None
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
    q["_explore"] = ((region in REGIONS or region.startswith("country:"))
                     and not (q.get("destination_city") or q.get("destination_code")))
    for side in ("origin", "destination"):
        if side == "destination" and q["_explore"]:
            q["destination_region"] = region
            continue
        if side == "destination":  # "para o Japão": a country is a region of one country (checked before cities)
            names = [q.get("destination_city"), q.get("destination_text")]
            iso = next((countries()[0][_norm(str(n))] for n in names if n and _norm(str(n)) in countries()[0]), None)
            if iso:
                q.update(_explore=True, destination_region=f"country:{iso}")
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
    q["airlines"] = [str(a) for a in q.get("airlines") or [] if a][:3]
    q["separate_tickets"] = bool(q.get("separate_tickets"))
    q["points"] = bool(q.get("points"))
    q["cabin"] = q.get("cabin") if q.get("cabin") in server.CABINS else "economy"
    q["award_programs"] = [p for p in q.get("award_programs") or [] if p in server.PROGRAMS]
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
    if (d1 - d0).days > MAX_WINDOW:
        return tr(lang, f"O período de ida pode ter no máximo {MAX_WINDOW} dias (uns 6 meses).",
                  f"The departure period can be at most {MAX_WINDOW} days (about 6 months).")
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


def key(dep: str, ret: str | None) -> str:
    return f"{dep}|{ret}"


def candidates(q: dict, exclude=frozenset(), one_way_first: bool = False, n: int = LIVE) -> tuple[list[tuple], int]:
    """Up to n (departure, return) pairs at least 3 days apart, skipping pairs already checked live,
    and how many cached fares were found. one_way_first ranks by one-way fares (for separate tickets)."""
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
        if (pair in cs and key(*pair) not in exclude
                and all(abs((dep - date.fromisoformat(p[0])).days) >= 3 for p in picked)):
            picked.append(pair)

    estimated = [pair for pair, est in alerts.scored(a, cs) if est != float("inf")]
    for pair in (estimated if one_way_first else []) + [(c["depart"], c["return"]) for c in cached] + estimated:
        add(pair)
    span = (d1 - d0).days
    for f in SPREAD:  # cache exhausted or empty: sample the window evenly
        dep = d0 + timedelta(days=round(span * f))
        add((dep.isoformat(), (dep + timedelta(days=q["min_days"])).isoformat() if rt else None))
    return picked[:n], len(cached)


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
    allowed = allowed_countries(q["destination_region"], origin)
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


def run_explore(q: dict, lang: str | None, exclude=frozenset()) -> tuple[str, list[dict], list[str]]:
    """'Anywhere in Europe': rank destinations on the free cache, confirm the LIVE cheapest ones live."""
    fares = [f for f in explore_fares(q) if f"dest:{f['dest']}" not in exclude]
    where = region_name(q["destination_region"], lang)
    rt, ms = bool(q.get("round_trip")), q.get("max_stops")
    d0, d1 = _fmt(q["depart_from"]), _fmt(q["depart_to"])
    if not fares:
        return tr(lang, f"Não achei preços em cache saindo de {q['origin_airports']} para {where} entre {d0} e {d1}"
                        + (f" até {money(q['max_price'])}" if q.get("max_price") else "")
                        + ". Tente outro período, um orçamento maior ou diga um destino.",
                  f"I found no cached fares from {q['origin_airports']} to {where} between {d0} and {d1}"
                  + (f" under {money(q['max_price'])}" if q.get("max_price") else "")
                  + ". Try another period, a bigger budget or name a destination."), [], []
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
            + (tr(lang, f"ida e volta, {stay(q)} dias", f"round trip, {stay(q)} days")
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
    return "\n".join([head, "", *lines, tail]), links, [f"dest:{f['dest']}" for f in fares[:LIVE]]


def award_lines(q: dict, cash: float, lang: str | None) -> list[str]:
    """Miles block for a route search: cheapest award each way, the best same-program round trip, and what a
    thousand miles are worth against the cash fare. Seats.aero cache only: costs no SerpApi search."""
    if not server.SEATS_KEY:
        return [tr(lang, "\nBusca com milhas: desativada (precisa da chave SEATS_AERO_KEY no .env).",
                   "\nMiles search: off (needs SEATS_AERO_KEY in .env).")]
    cabin, progs = q["cabin"], q["award_programs"] or None
    out = server.awards(q["origin_airports"], q["destination_airports"], q["depart_from"], q["depart_to"], cabin, progs)
    if q.get("round_trip"):
        d0, d1 = date.fromisoformat(q["depart_from"]), date.fromisoformat(q["depart_to"])
        back = server.awards(q["destination_airports"], q["origin_airports"],
                             (d0 + timedelta(days=q["min_days"])).isoformat(),
                             (d1 + timedelta(days=q["max_days"])).isoformat(), cabin, progs)
    else:
        back = []
    k = lambda m: f"{m / 1000:g} mil" if lang != "en" else f"{m / 1000:g}k"
    seg = lambda a: f"{k(a['miles'])} {a['program']} ({_fmt(a['date'])}, {a['airlines'] or '?'}{', direto' if a['direct'] and lang != 'en' else ', nonstop' if a['direct'] else ''})"
    pt_cabin = {"economy": "econômica", "premium": "premium", "business": "executiva", "first": "primeira classe"}[cabin]
    label = tr(lang, f"\nCom milhas ({pt_cabin}, por pessoa, sem taxas):", f"\nWith miles ({cabin}, per person, taxes not included):")
    if not out:
        return [label, tr(lang, "  nenhum assento de resgate no cache nessas datas.", "  no award seats cached on these dates.")]
    lines = [label, tr(lang, "  ida: ", "  out: ") + seg(out[0])]
    total = out[0]["miles"]
    if q.get("round_trip"):
        if not back:
            return lines + [tr(lang, "  volta: nenhum assento de resgate no cache.", "  back: no award seats cached.")]
        lines.append(tr(lang, "  volta: ", "  back: ") + seg(back[0]))
        # same program both ways (one account), with a return inside the requested stay
        pairs = [(a, b) for a in out for b in back if a["program"] == b["program"]
                 and q["min_days"] <= (date.fromisoformat(b["date"]) - date.fromisoformat(a["date"])).days <= q["max_days"]]
        if pairs:
            a, b = min(pairs, key=lambda p: p[0]["miles"] + p[1]["miles"])
            total = a["miles"] + b["miles"]
            lines.append(tr(lang, f"  ida e volta pelo {a['program']}: {k(total)} ({_fmt(a['date'])} → {_fmt(b['date'])})",
                            f"  round trip on {a['program']}: {k(total)} ({_fmt(a['date'])} → {_fmt(b['date'])})"))
        else:
            total = out[0]["miles"] + back[0]["miles"]
            if out[0]["program"] != back[0]["program"]:
                lines.append(tr(lang, f"  (programas diferentes na ida e na volta: {k(total)} no total)",
                                f"  (different programs each way: {k(total)} in total)"))
    if cash < float("inf"):
        lines.append(tr(lang, f"  cada mil milhas valem ~{money(cash / total * 1000)} contra o preço em dinheiro "
                              "(sem descontar as taxas do resgate).",
                        f"  each thousand miles is worth ~{money(cash / total * 1000)} against the cash fare "
                        "(before award taxes)."))
    return lines


def stay(q: dict) -> str:
    return str(q["min_days"]) if q["min_days"] == q["max_days"] else f"{q['min_days']}-{q['max_days']}"


def run(q: dict, lang: str | None = None, exclude=frozenset(), n: int = LIVE) -> tuple[str, list[dict], list[str]]:
    """Execute a validated request, skipping date pairs (or destinations) already checked in this session.
    Returns (message, link buttons, keys of what was checked live)."""
    if q.get("_explore"):
        return run_explore(q, lang, exclude)
    rt = bool(q.get("round_trip"))
    sep = rt and q.get("separate_tickets")
    # separate tickets cost 3 searches per pair, so a deep scan prices half as many pairs that way
    picks, n_cached = candidates(q, exclude, one_way_first=sep, n=max(SEP_PAIRS, n // 2) if sep else n)
    before = server._month_calls()
    ms, mh = q.get("max_stops"), q.get("max_hours")
    results = []
    for dep, ret in picks:
        r = server.search_flights(q["origin_airports"], q["destination_airports"], dep, ret, limit=30, max_hours=mh)
        opts = r["options"]
        ok = [o for o in opts if fits(o, q)]
        row = {"dep": dep, "ret": ret, "ok": ok[0] if ok else None, "any": opts[0] if opts else None,
               "pref": next((o for o in ok if airline_match(o, q)), None), "url": r["google_flights_url"], "split": None}
        if sep:  # same dates as two one-way tickets
            out = [o for o in server.search_flights(q["origin_airports"], q["destination_airports"], dep,
                                                    limit=30, max_hours=mh)["options"] if fits(o, q)]
            back = [o for o in server.search_flights(q["destination_airports"], q["origin_airports"], ret,
                                                     limit=30, max_hours=mh)["options"] if fits(o, q)]
            if out and back:
                row["split"] = (out[0], back[0])
        results.append(row)
    used = server._month_calls() - before

    total = lambda x: min(x["ok"]["price"] if x["ok"] else float("inf"),
                          sum(o["price"] for o in x["split"]) if x["split"] else float("inf"))
    d0, d1, left = _fmt(q["depart_from"]), _fmt(q["depart_to"]), server.days_until(q["depart_from"])
    head = (f"{q['origin_airports']} → {q['destination_airports']}, "
            + (tr(lang, f"ida e volta, {stay(q)} dias", f"round trip, {stay(q)} days")
               if rt else tr(lang, "só ida", "one way"))
            + conditions(q, lang)
            + tr(lang, f"\nIda entre {d0} e {d1} (faltam {left} dias). "
                       f"Cache: {n_cached} preços; testei {len(picks)} datas novas ao vivo ({used} buscas pagas).",
                       f"\nDeparting between {d0} and {d1} ({left} days from now). "
                       f"Cache: {n_cached} fares; checked {len(picks)} new dates live ({used} paid searches)."))
    found = sorted((x for x in results if total(x) < float("inf")), key=total)
    lines = []
    for i, x in enumerate(found, 1):
        lines.append(f"{i}) " + (_line(x["ok"], x["dep"], x["ret"], lang) if x["ok"]
                                 else f"{_fmt(x['dep'])} → {_fmt(x['ret'])}: " + tr(lang, "sem ida e volta", "no round trip")))
        if x["split"]:
            o, b = x["split"]
            lines.append(tr(lang, "   separadas: ", "   separate tickets: ") + money(o["price"] + b["price"])
                         + tr(lang, f" (ida {money(o['price'])} {', '.join(o['airlines'])} + volta {money(b['price'])} {', '.join(b['airlines'])})",
                              f" (out {money(o['price'])} {', '.join(o['airlines'])} + back {money(b['price'])} {', '.join(b['airlines'])})"))
    if not lines:
        lines = [tr(lang, "Nenhum voo encontrado com essas condições nas datas testadas.",
                    "No flights matched these conditions on the dates checked.")]
    best = total(found[0]) if found else float("inf")
    if q.get("airlines"):
        names = ", ".join(q["airlines"])
        pref = min((x for x in results if x["pref"]), key=lambda x: x["pref"]["price"], default=None)
        lines.append(tr(lang, f"\nCom {names}: ", f"\nWith {names}: ")
                     + (_line(pref["pref"], pref["dep"], pref["ret"], lang) if pref
                        else tr(lang, "nenhum voo nas datas testadas.", "no flights on the dates checked.")))
    extra = [x for x in results if x["any"] and x["any"]["stops"] > (ms if ms is not None else 99)
             and x["any"]["price"] <= MUCH_CHEAPER * best]
    if extra:
        x = min(extra, key=lambda x: x["any"]["price"])
        lines.append(tr(lang, "\nCom mais escalas, bem mais barato: ", "\nWith more stops, much cheaper: ")
                     + _line(x["any"], x["dep"], x["ret"], lang))
    if q.get("points"):
        lines += award_lines(q, best, lang)
    mp = q.get("max_price")
    if mp and best > mp:
        lines.append(tr(lang, f"\nNada até {money(mp)} nessas datas" + (f": o mais perto foi {money(best)}." if found else ".")
                              + " Mande \"mais opções\" para eu testar outras datas, ou crie um /alerta para eu avisar se baixar.",
                        f"\nNothing under {money(mp)} on these dates" + (f": the closest was {money(best)}." if found else ".")
                        + " Send \"more options\" to try other dates, or create an /alerta and I'll tell you if it drops."))
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
    tail += tr(lang, "\nPara ver outras datas, mande \"mais opções\".", "\nFor other dates, send \"more options\".")
    links = [{"label": f"Google Flights {_fmt(x['dep'])}" + (f"→{_fmt(x['ret'])}" if x["ret"] else ""), "url": x["url"]}
             for x in found if x["url"]]
    return "\n".join([head, "", *lines, tail]), links, [key(dep, ret) for dep, ret in picks]


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
    assert resolve("Japan", "JPN", "NRT,HND") is None  # not Washington
    assert resolve("Toquio", "TYO", None) == ("TYO", "HND,NRT")  # Portuguese spelling, code agrees
    assert resolve("Rio", "RIO", None)[0] == "RIO"  # short form of Rio de Janeiro
    jp = {**q, "destination_city": "Japan", "destination_code": "JPN"}
    assert validate(jp, t) is None and jp["_explore"] and jp["destination_region"] == "country:JP"
    print("search selftest ok")


if __name__ == "__main__":
    selftest()
