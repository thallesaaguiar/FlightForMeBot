"""/buscar: an LLM only turns the request into parameters; the search strategy below is fixed code.

Strategy: score every date pair on free cached prices, pick up to BUSCAR_LIVE well-spread candidates
(cached fares first, then one-way estimates, then evenly spaced dates when the cache is empty),
confirm them with live searches and filter by stops. The reply is built here, not by a model.
"""
import json
import math
import os
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from difflib import SequenceMatcher

import httpx

import alerts
import server
from alerts import money, tr

LIVE = int(os.environ.get("BUSCAR_LIVE", "3"))  # paid searches per /buscar
DEEP = int(os.environ.get("BUSCAR_DEEP", "8"))  # dates checked when an admin asks "data por data" / "todas as datas"
DEFAULT_WINDOW = (13, 103)  # days after tomorrow searched when the user gives no dates (~2 weeks to ~3 months)
SUGGEST = 10  # destinations an open search ("anywhere", "Europe") lists
PER_COUNTRY = 2  # at most this many of them in one country
FAR_KM = 3000  # "furthest for the money" only ranks trips at least this long
PLANE_SHARE = 0.2  # open searches skip round trips flying more than 20% of the stay (2 days in the air for 10 away)
TOP_DATES = 2  # cheapest date pairs listed by a normal search (a day-by-day scan lists all of them)
# Google Flights lists flights about 11 months ahead; airline sites sell a bit further. Measured 2026-10-14:
# 321 days out had fares, 334 came back empty. Searching past it spends paid searches on empty answers.
HORIZON = int(os.environ.get("GOOGLE_HORIZON_DAYS", "330"))
MAX_WINDOW = 190  # departure window for searches (alerts keep their own, smaller one): "April to September" fits
SEP_PAIRS = 2  # date pairs priced as round trip + one-way each way; the one-way legs are then crossed (see cross)
FASTER = (1.15, 120)  # a faster option is shown when it costs at most 15% more and saves at least 2h
MAX_ROWS = 5  # result lines when crossing one-way legs adds rows beyond the date pairs searched
NEARBY_KM = 250  # other airports this close to the origin or destination are screened on the free cache
CHEAPER_BY = 0.85  # a nearby airport or a self-transfer hub is mentioned when its cached fare is <= 85% of the route's
PEAK = 1.4  # a cached day costing 40%+ over the window's median is a peak to avoid
PRICE_LEVEL = {"low": ("baixo", "low"), "typical": ("normal", "typical"), "high": ("alto", "high")}
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


_names: dict[str, set] = {}  # city code -> normalized English and Portuguese names, filled by places()


def mentioned(city: str | None, t: str) -> bool:
    """Is this city in the (normalized) message under any of its names? "Cape Town" matches "cidade do cabo"."""
    place = resolve(city, None, None)
    return bool(place) and any(re.search(rf"\b{re.escape(n)}\b", t) for n in _names.get(place[0], ()))


def places():
    """Travelpayouts city/airport reference data (free, downloaded once into data/)."""
    global _places
    if _places is None:
        raw = {}
        for kind, lang, name in (("cities", "en", "cities"), ("airports", "en", "airports"), ("cities", "pt", "cities_pt")):
            f = server.ROOT / "data" / f"{name}.json"
            if not f.exists():
                f.parent.mkdir(exist_ok=True)
                r = httpx.get(f"https://api.travelpayouts.com/data/{lang}/{kind}.json", timeout=120)
                r.raise_for_status()
                f.write_bytes(r.content)
            raw[name] = json.loads(f.read_text())
        pt = {c["code"]: c["name"] for c in raw["cities_pt"] if c.get("name")}  # Xangai, Cidade do Cabo, Quioto
        by_name: dict[str, list] = {}
        for c in raw["cities"]:
            for n in {c["name"], (c.get("name_translations") or {}).get("en"), pt.get(c["code"])} - {None}:
                by_name.setdefault(_norm(n), []).append(c)
                _names.setdefault(c["code"], set()).add(_norm(n))
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


def airline_codes(names: list[str]) -> str:
    """IATA codes for airline names ("Air China" -> "CA"), for Google's airline filter."""
    global _codes
    if _codes is None:
        airline_names()  # downloads airlines.json when missing
        _codes = {_norm(a["name"]).replace(" ", ""): a["code"] for a in json.loads((server.ROOT / "data" / "airlines.json")
                  .read_text()) if a.get("name") and a.get("code")}
    return ",".join(c for c in (_codes.get(_norm(n).replace(" ", "")) for n in names) if c)


_codes = None


_lowcost = None


def lowcost(o: dict) -> bool:
    """Any low-cost carrier on the option (Travelpayouts flag): bags and seats usually cost extra."""
    global _lowcost
    if _lowcost is None:
        airline_names()
        _lowcost = {_norm(a["name"]).replace(" ", "") for a in json.loads((server.ROOT / "data" / "airlines.json").read_text())
                    if a.get("is_lowcost") and a.get("name")}
    return any(_norm(a).replace(" ", "") in _lowcost for a in o.get("airlines", []))


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
        if side == "destination":  # "Tokyo or Osaka": one search over every airport of both cities
            airports = place[1].split(",")
            for city in q.get("destination_also") or []:
                airports += (resolve(city, None, None) or ("", ""))[1].split(",")
            q["destination_airports"] = ",".join(dict.fromkeys(a for a in airports if a))
            q["_multi"] = q["destination_airports"] != place[1]
    # open jaw: the return leaves from another city, priced only as one-way legs
    back = resolve(q.get("return_from"), None, None) if q.get("round_trip") and not q["_explore"] else None
    if back and back[0] not in (q["destination_code"], q["origin_code"]):
        q["_return_code"], q["_return_airports"] = back
        q.update(separate_tickets=True, _multi=True)
    else:
        q["return_from"] = q["_return_code"] = q["_return_airports"] = None
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
    if d0 < (today or date.today()).replace(day=1):  # "agosto a outubro" in late September means next year's
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
    return horizon_cut(q, today or date.today(), lang)


def horizon_cut(q: dict, today: date, lang: str | None) -> str | None:
    """Ends the departure window where the whole trip (return included) still fits in Google's horizon.
    A trip entirely past it gets an /alerta suggestion instead of searches that can only come back empty."""
    horizon = today + timedelta(days=HORIZON)
    rt = bool(q.get("round_trip"))
    last = horizon - timedelta(days=q["min_days"] if rt else 0)
    d0, d1 = date.fromisoformat(q["depart_from"]), date.fromisoformat(q["depart_to"])
    if d1 > last:
        q["_horizon"] = horizon.isoformat()
    if d0 <= last:
        q["depart_to"] = min(d1, last).isoformat()
        return None
    end = min(d1 + timedelta(days=q["max_days"] if rt else 0), d0 + timedelta(days=alerts.MAX_WINDOW_DAYS))
    example = (f"/alerta {q['origin_code']} {q.get('destination_code') or '???'} {d0:%d/%m}-{end:%d/%m}"
               + (f" {stay(q)}d" if rt else "") + tr(lang, " PREÇO", " PRICE"))
    return tr(lang, f"O Google Flights só mostra voos até {horizon:%d/%m/%Y} (uns {HORIZON} dias à frente)"
                    + (", e a volta também precisa caber nisso" if rt else "") + ". O site da companhia às vezes vende "
                    "um pouco mais longe, mas eu ainda não consigo ver esses preços. As datas abrem aos poucos: crie um "
                    f"alerta e eu aviso quando aparecer preço, ex:\n{example}",
              f"Google Flights only lists flights up to {horizon:%d/%m/%Y} (about {HORIZON} days ahead)"
              + (", and the return has to fit too" if rt else "") + ". Airline sites sometimes sell a little further, "
              "but I can't see those prices yet. Dates open gradually: create an alert and I'll tell you when prices "
              f"show up, e.g.:\n{example}")


def horizon_note(q: dict, lang: str | None) -> str:
    if not q.get("_horizon"):
        return ""
    h = _fmt(q["_horizon"])
    return tr(lang, f"\nO Google Flights só mostra voos até {h}, então a ida ficou até {_fmt(q['depart_to'])}. "
                    "Para datas depois disso, crie um /alerta.",
              f"\nGoogle Flights only lists flights up to {h}, so departures stop at {_fmt(q['depart_to'])}. "
              "For later dates, create an /alerta.")


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


def _hm(minutes: int) -> str:
    return f"{minutes // 60}h{minutes % 60:02d}"


def _days(dep: str, ret: str) -> int:
    return (date.fromisoformat(ret) - date.fromisoformat(dep)).days


def _when(dep: str, ret: str | None, lang: str | None) -> str:
    return f"{_fmt(dep)} → {_fmt(ret)} ({_days(dep, ret)} {tr(lang, 'dias', 'days')})" if ret else _fmt(dep)


def _flight(o: dict, lang: str | None, route: bool = False) -> str:
    """route: show the airports flown (MAD-PEK-KIX) when the destination has several cities."""
    stops = tr(lang, f"{o['stops']} escala(s)", f"{o['stops']} stop(s)") if o["stops"] else tr(lang, "direto", "nonstop")
    hours = f" · {_hm(o['minutes'])}" if o.get("minutes") else ""
    path = f" · {o['route']}" if route and o.get("route") else ""
    cheap = tr(lang, " · low-cost: mala e assento à parte", " · low-cost: bags and seats extra") if lowcost(o) else ""
    return f"{money(o['price'])} · {', '.join(o['airlines'])} · {stops}{hours}{path}{cheap}"


def _line(o: dict, dep: str, ret: str | None, lang: str | None, route: bool = False) -> str:
    return f"{_when(dep, ret, lang)}: {_flight(o, lang, route)}"


def _split(pair: tuple[dict, dict], lang: str | None, route: bool = False) -> str:
    o, b = pair
    leg = lambda x: f"{money(x['price'])} {', '.join(x['airlines'])}" + (f" {x['route']}" if route and x.get("route") else "")
    return money(o["price"] + b["price"]) + tr(lang, f" (ida {leg(o)} + volta {leg(b)})", f" (out {leg(o)} + back {leg(b)})")


def faster(ok: list[dict]) -> dict | None:
    """Among options cheapest first, the quickest one costing at most FASTER[0] x the cheapest and saving
    FASTER[1]+ minutes. Round trips only report the outbound duration, so this compares outbounds."""
    if not ok or not ok[0].get("minutes"):
        return None
    top, (most, saves) = ok[0], FASTER
    quick = [o for o in ok[1:] if o.get("minutes") and o["price"] <= top["price"] * most
             and top["minutes"] - o["minutes"] >= saves]
    return min(quick, key=lambda o: (o["minutes"], o["price"]), default=None)


def cross(outs: dict, backs: dict, lo: int, hi: int) -> dict[tuple, tuple]:
    """Every outbound one-way (date -> option) with every return that keeps the stay in lo..hi days:
    2 + 2 one-way searches price up to 4 date pairs instead of 2."""
    return {(d, r): (o, b) for d, o in outs.items() for r, b in backs.items() if o and b and lo <= _days(d, r) <= hi}


def arrival_city(o: dict) -> str | None:
    """City name of the airport an option lands at (MAD-PEK-HND -> Tokyo); None when the source gives no route."""
    _, cities, airport_city, _ = places()
    code = airport_city.get(str(o.get("route") or "").split("-")[-1])
    return cities.get(code, {}).get("name") if code else None


def cheapest_with(rows: list[dict], q: dict, stops: int) -> tuple[dict, dict] | None:
    """(row, option): the cheapest flight with exactly `stops` stops on any date searched, inside the hour limit.
    Live searches ask for every stop count (the stop limit is applied afterwards), so this costs nothing."""
    any_stops = {**q, "max_stops": None}
    return min(((x, o) for x in rows for o in x["opts"] if o["stops"] == stops and fits(o, any_stops)),
               key=lambda xo: xo[1]["price"], default=None)


def live_searches(calls: list[tuple], max_hours: float | None) -> list[dict]:
    """server.search_flights(origin, destination, date[, return]) per call, in parallel: each one waits on the network."""
    # ponytail: 6 threads; the monthly cap is checked before each call, so parallel calls can pass it by a few
    with ThreadPoolExecutor(min(6, max(1, len(calls)))) as ex:
        return list(ex.map(lambda c: server.search_flights(*c, limit=30, max_hours=max_hours), calls))


def fits(o: dict, q: dict) -> bool:
    """Stops and duration limits on a live option (Google applies max_duration too; this double-checks the outbound)."""
    ms, mh = q.get("max_stops"), q.get("max_hours")
    return (ms is None or o["stops"] <= ms) and (not mh or not o.get("minutes") or o["minutes"] <= mh * 60)


def conditions(q: dict, lang: str | None) -> str:
    ms, mh, mp = q.get("max_stops"), q.get("max_hours"), q.get("max_price")
    return ((tr(lang, f", até {ms} escala(s)", f", up to {ms} stop(s)") if ms is not None else "")
            + (tr(lang, f", até {mh:g}h de voo", f", up to {mh:g}h per flight") if mh else "")
            + (tr(lang, f", até {money(mp)}", f", under {money(mp)}") if mp else ""))


def _km(a: dict, b: dict) -> int:
    """Great-circle distance between two Travelpayouts coordinates."""
    la1, lo1, la2, lo2 = map(math.radians, (a["lat"], a["lon"], b["lat"], b["lon"]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return round(12742 * math.asin(math.sqrt(h)))


def explore_fares(q: dict) -> list[dict]:
    """Cheapest cached fare per destination from the origin, inside the region, window, stay, stops, hours and budget.
    A round trip that spends more than PLANE_SHARE of the stay in the air is left out."""
    _, cities, _, _ = places()
    origin, rt, ms, maxp = q["origin_code"], bool(q.get("round_trip")), q.get("max_stops"), q.get("max_price")
    mh, here = q.get("max_hours"), cities.get(origin, {}).get("coordinates")
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
                city = cities.get(dest, {})
                ret = (t.get("return_at") or "")[:10] if rt else None
                if dest == origin or (allowed is not None and city.get("country_code") not in allowed):
                    continue
                if not q["depart_from"] <= dep <= q["depart_to"] or (maxp and t["price"] > maxp):
                    continue
                if rt and not (ret and q["min_days"] <= _days(dep, ret) <= q["max_days"]):
                    continue
                stops = max(t.get("transfers") or 0, (t.get("return_transfers") or 0) if rt else 0)
                if ms is not None and stops > ms:
                    continue
                out_h = (t.get("duration_to") or 0) / 60
                air_h = out_h + ((t.get("duration_back") or 0) / 60 if rt else 0)
                if (mh and out_h > mh) or (rt and air_h > PLANE_SHARE * q["min_days"] * 24):
                    continue
                if dest not in best or t["price"] < best[dest]["price"]:
                    best[dest] = {"dest": dest, "name": city.get("name", dest), "country": city.get("country_code", "?"),
                                  "dep": dep, "ret": ret, "price": t["price"], "stops": stops, "hours": out_h,
                                  "km": _km(here, city["coordinates"]) if here and city.get("coordinates") else 0}
    return sorted(best.values(), key=lambda f: f["price"])


GROUPS = {"cheap": ("🏷️ Mais baratos", "🏷️ Cheapest"), "far": ("💎 Mais longe pelo preço", "💎 Furthest for the money"),
          "direct": ("✈️ Voos diretos", "✈️ Nonstop")}


def suggest(fares: list[dict], n: int = SUGGEST) -> list[tuple[str, dict]]:
    """Up to n (group, fare): the cheapest, the lowest price per km among trips over FAR_KM, and the cheapest
    nonstops, at most PER_COUNTRY per country so the list isn't six Spanish cities. Spare slots go to the cheapest.
    # ponytail: price per km is a rough "good deal"; compare against each route's usual price if it misleads"""
    taken: list[tuple[str, dict]] = []
    per: dict[str, int] = {}

    def take(pool, k, group):
        for f in pool:
            if k <= 0 or len(taken) >= n:
                return
            if any(f is t for _, t in taken) or per.get(f["country"], 0) >= PER_COUNTRY:
                continue
            taken.append((group, f))
            per[f["country"]] = per.get(f["country"], 0) + 1
            k -= 1

    take(fares, 4, "cheap")
    take(sorted((f for f in fares if f["km"] >= FAR_KM), key=lambda f: f["price"] / f["km"]), 3, "far")
    take([f for f in fares if f["stops"] == 0], 3, "direct")
    take(fares, n, "cheap")
    order = list(GROUPS)
    return sorted(taken, key=lambda gf: order.index(gf[0]))  # stable: each group keeps its own order


def run_explore(q: dict, lang: str | None, exclude=frozenset()) -> tuple[str, list[dict], list[str]]:
    """'Anywhere' or 'anywhere in Europe': SUGGEST destinations in groups from the free cache; the first of each
    group is confirmed live. q["_dests"] gets (city code, name) per line, for one button each."""
    fares = [f for f in explore_fares(q) if f"dest:{f['dest']}" not in exclude]
    where = region_name(q["destination_region"], lang)
    rt = bool(q.get("round_trip"))
    d0, d1 = _fmt(q["depart_from"]), _fmt(q["depart_to"])
    q["_dests"] = []
    if not fares:
        return tr(lang, f"Não achei preços em cache saindo de {q['origin_airports']} para {where} entre {d0} e {d1}"
                        + (f" até {money(q['max_price'])}" if q.get("max_price") else "")
                        + ". Tente outro período, um orçamento maior ou diga um destino.",
                  f"I found no cached fares from {q['origin_airports']} to {where} between {d0} and {d1}"
                  + (f" under {money(q['max_price'])}" if q.get("max_price") else "")
                  + ". Try another period, a bigger budget or name a destination."), [], []
    picks = suggest(fares)
    check = [f for k, (g, f) in enumerate(picks) if k == 0 or picks[k - 1][0] != g][:LIVE]  # first of each group
    before = server._month_calls()
    calls = [(q["origin_airports"], (resolve(None, f["dest"], None) or (f["dest"], f["dest"]))[1], f["dep"], f["ret"])
             for f in check]
    live = {}
    for f, r in zip(check, live_searches(calls, q.get("max_hours"))):
        ok = [o for o in r["options"] if fits(o, q)]
        live[f["dest"]] = (ok[0] if ok else None, r["google_flights_url"])
    used = server._month_calls() - before
    head = (tr(lang, f"Saindo de {q['origin_airports']} para {where}, ", f"From {q['origin_airports']} to {where}, ")
            + (tr(lang, f"ida e volta, {stay(q)} dias", f"round trip, {stay(q)} days")
               if rt else tr(lang, "só ida", "one way"))
            + conditions(q, lang)
            + tr(lang, f"\nIda entre {d0} e {d1}. Cache: {len(fares)} destinos; confirmei {len(check)} ao vivo "
                       f"({used} buscas pagas).",
                 f"\nDeparting between {d0} and {d1}. Cache: {len(fares)} destinations; checked {len(check)} live "
                 f"({used} paid searches)."))
    lines, group = [], None
    for i, (g, f) in enumerate(picks, 1):
        if g != group:
            lines += ["", GROUPS[g][lang == "en"]]
            group = g
        name = f"{i}) {f['name']} ({f['country']})"
        ok = live.get(f["dest"], (None,))[0]
        if ok:
            text = f"{name}: {_line(ok, f['dep'], f['ret'], lang)} ✓"
        else:
            stops = tr(lang, f"{f['stops']} escala(s)", f"{f['stops']} stop(s)") if f["stops"] else tr(lang, "direto", "nonstop")
            hours = f" · {_hm(round(f['hours'] * 60))}" if f["hours"] else ""
            gone = tr(lang, " (não confirmado ao vivo)", " (not confirmed live)") if f["dest"] in live else ""
            text = f"{name}: ~{money(f['price'])} · {_when(f['dep'], f['ret'], lang)} · {stops}{hours}{gone}"
        lines.append(text + (f" · {f['km']:,} km".replace(",", ".") if g == "far" else ""))
        q["_dests"].append((f["dest"], f["name"]))
    tail = tr(lang, "\n✓ = preço confirmado agora. ~ = preço do cache (buscas dos últimos dias), pode ter mudado.\n"
                    "Preços por adulto. Toque num destino para eu buscar as melhores datas para ele.",
              "\n✓ = price checked now. ~ = cached price (searches from the last few days), it may have changed.\n"
              "Prices per adult. Tap a destination and I'll look for its best dates.")
    tail += horizon_note(q, lang)
    if q.get("_default_dates"):
        tail += tr(lang, "\nSem datas, busquei com ida nos próximos 3 meses.", "\nNo dates given, so I searched the next 3 months.")
    links = [{"label": f"{f['name']} {_fmt(f['dep'])}" + (f"→{_fmt(f['ret'])}" if f["ret"] else ""), "url": live[f["dest"]][1]}
             for f in check if live[f["dest"]][0] and live[f["dest"]][1]]
    return "\n".join([head, *lines, tail]), links, [f"dest:{f['dest']}" for _, f in picks]


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


def deal(price: float, pi: dict, days_left: int) -> tuple[int, str, str] | None:
    """(score 1-10, verdict, trend) for a live price, from Google's price insights of that search: the share of the
    last ~60 days it was cheaper (or where the price sits in the usual range), the last week against the one before,
    and how close the trip is. Verdicts: buy, wait (create an alert) or change (other dates or airports)."""
    hist = [p for _, p in pi.get("price_history") or [] if p]
    lo, hi = (pi.get("typical_price_range") or [None, None])[:2]
    if len(hist) >= 7:
        score = round(10 - 9 * sum(p < price for p in hist) / len(hist))
    elif lo and hi and hi > lo:
        score = 9 if price <= lo else 2 if price > hi * 1.15 else 3 if price > hi else round(8 - 4 * (price - lo) / (hi - lo))
    else:
        return None
    trend = "flat"
    if len(hist) >= 14:
        week, before = sum(hist[-7:]) / 7, sum(hist[-14:-7]) / 7
        trend = "down" if week < before * 0.97 else "up" if week > before * 1.03 else "flat"
    if days_left <= 21 or score >= 8 or (trend == "up" and score >= 5):
        verdict = "buy"  # close to departure fares rarely drop; a good or rising price won't wait
    elif score <= 3:
        verdict = "wait" if days_left > 45 and trend != "up" else "change"
    else:
        verdict = "wait" if days_left > 60 else "buy"
    return score, verdict, trend


def deal_lines(x: dict, pi: dict, q: dict, lang: str | None) -> list[str]:
    o, left = x["ok"], server.days_until(x["dep"])
    got = deal(o["price"], pi, left)
    if not got:
        return []
    score, verdict, trend = got
    lo, hi = (pi.get("typical_price_range") or [None, None])[:2]
    level = PRICE_LEVEL.get(pi.get("price_level"), (None, None))[lang == "en"]
    hist = [p for _, p in pi.get("price_history") or [] if p]
    trend_txt = {"down": tr(lang, "caindo", "falling"), "up": tr(lang, "subindo", "rising"),
                 "flat": tr(lang, "estável", "steady")}[trend]
    facts = ([tr(lang, f"Google diz preço {level}", f"Google calls it {level}")] if level else []) + (
        [tr(lang, f"faixa comum {money(lo)} a {money(hi)}", f"usual {money(lo)} to {money(hi)}")] if lo and hi else []) + (
        [tr(lang, f"mais barato que hoje em {round(100 * sum(p < o['price'] for p in hist) / len(hist))}% dos últimos "
                  f"{len(hist)} dias, preço {trend_txt}",
            f"cheaper than now on {round(100 * sum(p < o['price'] for p in hist) / len(hist))}% of the last {len(hist)} days, "
            f"{trend_txt}")] if len(hist) >= 7 else [])
    target = round((lo or o["price"] * 0.9) / 10) * 10
    alert = (f"/alerta {q['origin_code']} {q.get('destination_code')} {_fmt(x['dep'])}"
             + (f" {_fmt(x['ret'])}" if x["ret"] else "") + f" {target:.0f}")
    say = {"buy": tr(lang, f"👉 RESERVE AGORA: preço bom ou em alta, faltando {left} dias.",
                     f"👉 BOOK NOW: a good or rising price, {left} days out."),
           "wait": tr(lang, f"👉 ESPERE: ainda faltam {left} dias e o preço está acima do normal. Deixe um alerta:\n{alert}",
                      f"👉 WAIT: {left} days to go and the price is above usual. Leave an alert:\n{alert}"),
           "change": tr(lang, "👉 MUDE O PLANO: preço alto e pouco tempo para cair. Tente outras datas (\"mais opções\") ou "
                              "outros aeroportos.",
                        "👉 CHANGE PLANS: a high price with little time to drop. Try other dates (\"more options\") or "
                        "other airports.")}[verdict]
    return [tr(lang, f"\n📊 Nota da opção 1: {score}/10 (", f"\n📊 Option 1 deal score: {score}/10 (")
            + "; ".join(facts) + ").", say]


def _months_between(d0: str, d1: str, limit: int = 2) -> list[str]:
    a, b = date.fromisoformat(d0), date.fromisoformat(d1)
    return sorted({(a + timedelta(days=i)).isoformat()[:7] for i in range((b - a).days + 1)})[:limit]


def cached_min(origin: str, dest: str, q: dict) -> float | None:
    """Cheapest cached fare inside the window (and stay, for round trips). Free; first 2 departure months only."""
    rt, best = bool(q.get("round_trip")), None
    for dm in _months_between(q["depart_from"], q["depart_to"]):
        rms = _months_between(dm + "-01", (date.fromisoformat(dm + "-01") + timedelta(days=31 + (q.get("max_days") or 0)))
                              .isoformat(), 3) if rt else [None]
        for rm in rms:
            for t in server.tp_month(origin, dest, dm, rm):
                dep, ret = t["departure_at"][:10], (t.get("return_at") or "")[:10]
                if not q["depart_from"] <= dep <= q["depart_to"]:
                    continue
                if rt and not (ret and q["min_days"] <= _days(dep, ret) <= q["max_days"]):
                    continue
                best = t["price"] if best is None else min(best, t["price"])
    return best


def nearby(code: str, km: int = NEARBY_KM, top: int = 4) -> list[tuple[str, int]]:
    """(city code, km) of other cities with an airport within km of this one, nearest first."""
    _, cities, _, _ = places()
    here = cities.get(code, {}).get("coordinates")
    if not here:
        return []
    near = [(c, _km(here, x["coordinates"])) for c, x in cities.items()
            if c != code and x.get("has_flightable_airport") and x.get("coordinates")]
    return sorted((c for c in near if c[1] <= km), key=lambda c: c[1])[:top]


def nearby_lines(q: dict, lang: str | None) -> list[str]:
    """Other airports near the origin or the destination whose cached fare beats the route's by 15%+. Free."""
    org, dst = q["origin_code"], q["destination_code"]
    base = cached_min(org, dst, q)
    if base is None:
        return []
    alts = [("origin", c, km) for c, km in nearby(org)] + [("destination", c, km) for c, km in nearby(dst)]
    with ThreadPoolExecutor(8) as ex:
        prices = list(ex.map(lambda a: cached_min(a[1], dst, q) if a[0] == "origin" else cached_min(org, a[1], q), alts))
    cities, out = places()[1], []
    for (side, c, km), p in sorted(zip(alts, prices), key=lambda ap: ap[1] if ap[1] is not None else 1e18):
        if p is None or p > base * CHEAPER_BY:
            continue
        name, near_to = cities[c]["name"], cities.get(org if side == "origin" else dst, {}).get("name")
        q.setdefault("_savings", []).append((tr(lang, f"saindo de {name}" if side == "origin" else f"indo para {name}",
                                                f"from {name}" if side == "origin" else f"to {name}"), base - p))
        out.append(tr(lang, f"📍 {'Saindo de' if side == 'origin' else 'Chegando em'} {name} ({c}, {km} km de {near_to}): "
                            f"~{money(p)} no cache, {money(base - p)} a menos. Conte o transporte até lá. Diga "
                            f"\"{'saindo de' if side == 'origin' else 'para'} {name}\" para eu buscar.",
                      f"📍 {'From' if side == 'origin' else 'To'} {name} ({c}, {km} km from {near_to}): ~{money(p)} cached, "
                      f"{money(base - p)} less. Count the ground transport. Say \"{'from' if side == 'origin' else 'to'} "
                      f"{name}\" and I'll search it."))
        if len(out) == 2:
            break
    return ["\n" + out[0], *out[1:]] if out else []


def hubs_for(origin: str, dest: str, month: str, top: int = 3) -> list[tuple[str, float, float]]:
    """(hub, cached origin->hub, cached hub->dest) one-way, cheapest total first: hubs are the cheapest places the
    cache knows from the origin that also fly on to the destination. Free."""
    first: dict[str, float] = {}
    for t in server.tp_month(origin, None, month):
        if t["destination"] != dest:
            first[t["destination"]] = min(first.get(t["destination"], t["price"]), t["price"])
    cands = sorted(first, key=first.get)[:15]
    with ThreadPoolExecutor(8) as ex:
        second = list(ex.map(lambda h: min((t["price"] for t in server.tp_month(h, dest, month)), default=None), cands))
    combos = [(h, first[h], b) for h, b in zip(cands, second) if b is not None]
    return sorted(combos, key=lambda c: c[1] + c[2])[:top]


def transfer_hint(q: dict, lang: str | None) -> list[str]:
    """One line when a self-transfer through a hub looks 15%+ cheaper than flying straight, on the one-way cache."""
    month = q["depart_from"][:7]
    direct = min((t["price"] for t in server.tp_month(q["origin_code"], q["destination_code"], month)), default=None)
    hubs = hubs_for(q["origin_code"], q["destination_code"], month, 1)
    if not direct or not hubs or sum(hubs[0][1:]) > direct * CHEAPER_BY:
        return []
    h, a, b = hubs[0]
    name = places()[1].get(h, {}).get("name", h)
    q.setdefault("_savings", []).append((tr(lang, f"conexão por conta própria via {name}", f"self-transfer via {name}"),
                                         direct - a - b))
    return [tr(lang, f"🔀 Conexão por conta própria via {name}: ~{money(a + b)} só ida no cache ({money(a)} + {money(b)}) "
                     f"contra ~{money(direct)} direto. Peça \"conexão por conta própria\" para eu conferir ao vivo.",
               f"🔀 Self-transfer via {name}: ~{money(a + b)} one way cached ({money(a)} + {money(b)}) against "
               f"~{money(direct)} straight. Ask for a \"self-transfer\" and I'll check it live.")]


def peak_days(q: dict, lang: str | None) -> list[str]:
    """Cached outbound days costing PEAK x the window's median: dates to avoid. Needs 10+ days of cache. Free."""
    days: dict[str, float] = {}
    for m in _months_between(q["depart_from"], q["depart_to"], 7):
        for t in server.tp_month(q["origin_code"], q["destination_code"], m):
            d = t["departure_at"][:10]
            if q["depart_from"] <= d <= q["depart_to"]:
                days[d] = min(days.get(d, t["price"]), t["price"])
    if len(days) < 10:
        return []
    median = sorted(days.values())[len(days) // 2]
    peaks = sorted((d for d, p in days.items() if p >= median * PEAK), key=lambda d: -days[d])[:4]
    return [tr(lang, "📈 Dias de ida mais caros no cache (evite): ", "📈 Priciest departure days in the cache (avoid): ")
            + ", ".join(f"{_fmt(d)} ~{money(days[d])}" for d in sorted(peaks))
            + tr(lang, f" (normal ~{money(median)}).", f" (usual ~{money(median)}).")] if peaks else []


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
    mh, mr = q.get("max_hours"), bool(q.get("_multi"))
    org, dst = q["origin_airports"], q["destination_airports"]
    jaw = q.get("_return_airports")  # a round trip to dst can't price an open jaw: one-way legs only
    deps, rets = (sorted({d for d, _ in picks}), sorted({r for _, r in picks})) if sep else ([], [])
    rts = [] if jaw else picks
    res = live_searches([(org, dst, d, r) for d, r in rts] + [(org, dst, d) for d in deps]
                        + [(jaw or dst, org, r) for r in rets], mh)
    rows: dict[tuple, dict] = {}
    for (dep, ret), r in zip(rts, res):
        opts = r["options"]
        ok = [o for o in opts if fits(o, q)]
        rows[(dep, ret)] = {"dep": dep, "ret": ret, "ok": ok[0] if ok else None, "fast": faster(ok),
                            "opts": opts, "pref": next((o for o in ok if airline_match(o, q)), None),
                            "url": r["google_flights_url"], "insights": r.get("price_insights"), "split": None}
    if sep:
        first = lambda r: next((o for o in r["options"] if fits(o, q)), None)
        outs = {d: first(r) for d, r in zip(deps, res[len(rts):])}
        backs = {d: first(r) for d, r in zip(rets, res[len(rts) + len(deps):])}
        for (dep, ret), pair in cross(outs, backs, q["min_days"], q["max_days"]).items():
            rows.setdefault((dep, ret), {"dep": dep, "ret": ret, "ok": None, "fast": None, "opts": [], "pref": None,
                                         "url": None, "insights": None})["split"] = pair
    results = list(rows.values())
    used = server._month_calls() - before

    total = lambda x: min(x["ok"]["price"] if x["ok"] else float("inf"),
                          sum(o["price"] for o in x["split"]) if x["split"] else float("inf"))
    d0, d1, left = _fmt(q["depart_from"]), _fmt(q["depart_to"]), server.days_until(q["depart_from"])
    head = (f"{q['origin_airports']} → {q['destination_airports']}, "
            + (tr(lang, f"volta {jaw} → {org}, {stay(q)} dias", f"back {jaw} → {org}, {stay(q)} days") if jaw else
               tr(lang, f"ida e volta, {stay(q)} dias", f"round trip, {stay(q)} days")
               if rt else tr(lang, "só ida", "one way"))
            + conditions(q, lang)
            + tr(lang, f"\nIda entre {d0} e {d1} (faltam {left} dias). "
                       f"Cache: {n_cached} preços; testei {len(picks)} datas novas ao vivo ({used} buscas pagas).",
                       f"\nDeparting between {d0} and {d1} ({left} days from now). "
                       f"Cache: {n_cached} fares; checked {len(picks)} new dates live ({used} paid searches)."))
    found = sorted((x for x in results if total(x) < float("inf")), key=total)[:TOP_DATES if n <= LIVE else max(len(picks), MAX_ROWS)]
    direct = cheapest_with(results, q, 0)
    lines = []
    for i, x in enumerate(found, 1):
        if i > 1:
            lines.append("")  # blank line between options
        if not x["ok"]:  # a crossed date pair only priced as two one-way tickets
            lines.append(f"{i}) {_when(x['dep'], x['ret'], lang)}: " + tr(lang, "bilhetes separados ", "separate tickets ")
                         + _split(x["split"], lang, mr))
            continue
        lines.append(f"{i}) " + _line(x["ok"], x["dep"], x["ret"], lang, mr))
        if x["fast"] and x["fast"] is not (direct or [None, None])[1]:  # the nonstop gets its own line below
            f = x["fast"]
            lines.append(tr(lang, f"   ↳ Mais rápido (+{money(f['price'] - x['ok']['price'])}, "
                                  f"{_hm(x['ok']['minutes'] - f['minutes'])} a menos): ",
                            f"   ↳ Faster (+{money(f['price'] - x['ok']['price'])}, "
                            f"{_hm(x['ok']['minutes'] - f['minutes'])} less): ") + _flight(f, lang, mr))
        if x["split"]:
            lines.append(tr(lang, "   ↳ Separadas: ", "   ↳ Separate tickets: ") + _split(x["split"], lang, mr))
    best = total(found[0]) if found else float("inf")
    pi = found[0]["insights"] if found else None
    if pi and found[0]["ok"] and found[0]["ok"]["price"] == best:
        lines += deal_lines(found[0], pi, q, lang)
    q["_savings"] = [(tr(lang, "bilhetes separados", "separate tickets"), x["ok"]["price"] - sum(o["price"] for o in x["split"]))
                     for x in found[:1] if x["ok"] and x["split"] and sum(o["price"] for o in x["split"]) < x["ok"]["price"]]
    if found:
        x = found[0]
        q["_best"] = {"dep": x["dep"], "ret": x["ret"], "price": best,
                      "label": (_line(x["ok"], x["dep"], x["ret"], lang) if x["ok"] and x["ok"]["price"] == best
                                else f"{_when(x['dep'], x['ret'], lang)}: " + _split(x["split"], lang))}
    if not lines:
        lines = [tr(lang, "Nenhum voo encontrado com essas condições nas datas testadas.",
                    "No flights matched these conditions on the dates checked.")]
    q["_options"] = [(x["dep"], x["ret"]) for x in found[:3]] if rt else []  # date pairs the hotel offer is based on
    q["_hotel_cities"] = [arrival_city(x["ok"] or x["split"][0]) for x in found[:3]] if rt else []
    if q.get("airlines"):
        names = ", ".join(q["airlines"])
        pref = min((x for x in results if x["pref"]), key=lambda x: x["pref"]["price"], default=None)
        codes = airline_codes(q["airlines"])
        if not pref and codes and (found or results) and not jaw:
            # Google lists only some itineraries per search: ask once more for just that airline on the best dates
            x = (found or results)[0]
            r = server.search_flights(org, dst, x["dep"], x["ret"], limit=30, max_hours=mh, airlines=codes)
            o = next((o for o in r["options"] if fits(o, q) and airline_match(o, q)), None)
            pref = o and {**x, "pref": o}
            names += tr(lang, " (1 busca paga extra só com ela)", " (1 extra paid search for it alone)")
        lines.append(tr(lang, f"\nCom {names}: ", f"\nWith {names}: ")
                     + (_line(pref["pref"], pref["dep"], pref["ret"], lang, mr) if pref
                        else tr(lang, "nenhum voo nas datas testadas.", "no flights on the dates checked.")))
    vs = lambda o: tr(lang, f" ({'+' if o['price'] >= best else '-'}{money(abs(o['price'] - best))} que a opção 1)",
                      f" ({'+' if o['price'] >= best else '-'}{money(abs(o['price'] - best))} vs option 1)")
    if direct and found and all(direct[1] is not x["ok"] for x in found):
        x, o = direct
        lines.append(tr(lang, "\n✈️ Voo direto: ", "\n✈️ Nonstop: ") + _line(o, x["dep"], x["ret"], lang, mr) + vs(o))
    two = cheapest_with(results, q, 2)
    if two and two[1]["price"] < best:  # a pricier 2-stop flight is never worth a line
        x, o = two
        lines.append(tr(lang, "\n🔁 Com 2 escalas: ", "\n🔁 With 2 stops: ") + _line(o, x["dep"], x["ret"], lang, mr) + vs(o))
        q["_savings"].append((tr(lang, "aceitar 2 escalas", "taking 2 stops"), best - o["price"]))
    # free hints from the cache: dates to avoid, cheaper airports nearby, a self-transfer through a hub
    hints = peak_days(q, lang) + (nearby_lines(q, lang) if not q.get("_multi") else []) + transfer_hint(q, lang)
    if hints:
        lines += ["", *[h.lstrip("\n") for h in hints]]
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
    tail += horizon_note(q, lang)
    if q.get("_default_dates"):
        tail += tr(lang, "\nVocê não deu datas, então busquei com ida nos próximos 3 meses. Diga um período para refinar "
                         "(ex: \"em abril\").",
                   "\nYou gave no dates, so I searched departures in the next 3 months. Name a period to refine "
                   "(e.g. \"in April\").")
    tail += tr(lang, "\nPara ver outras datas, mande \"mais opções\".", "\nFor other dates, send \"more options\".")
    links = [{"label": f"Google Flights {_fmt(x['dep'])}" + (f"→{_fmt(x['ret'])}" if x["ret"] else ""), "url": x["url"]}
             for x in found if x["url"]]
    return "\n".join([head, "", *lines, tail]), links, [key(dep, ret) for dep, ret in picks]


DATE_RE = r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b"
FILLER = (r"\b(dias?|days?|noites?|nights?|de|do|da|dos|das|desde|ate|a|ao|e|entre|from|to|until|and|um|uma|hote(l|is)|"
          r"hotels?|hospedagem|proxim[oa]s?|perto|near|close|ao lado|em|no|na|nos|nas|in|at|the|quero|busque|procure)\b")


def hotel_requests(text: str, today: date | None = None) -> list[dict]:
    """Hotel stays written one per line: "17/05 até 19/05 um hotel perto da Disneyland Tóquio".
    A line with exactly two dates is a stay; what's left after dates and filler words is the place.
    # ponytail: line-based; "Osaka de 21 a 25 de maio" (dates in words) isn't read, add the LLM if people write that"""
    tomorrow = (today or date.today()) + timedelta(days=1)
    out = []
    for line in re.split(r"[\n;]+", text):
        ds = re.findall(DATE_RE, line)
        if len(ds) != 2:
            continue
        try:
            when = []
            for d, m, y in ds:
                after = when[0] + timedelta(days=1) if when else tomorrow
                x = date(int(y) + (2000 if len(y) == 2 else 0), int(m), int(d)) if y else date(after.year, int(m), int(d))
                when.append(x if y or x >= after else x.replace(year=x.year + 1))
        except ValueError:
            continue
        rest = _norm(re.sub(DATE_RE, " ", line))
        place = " ".join(re.sub(FILLER, " ", re.sub(r"[^\w\s]", " ", rest)).split())
        if place and when[1] > when[0]:
            out.append({"place": place.title(), "near": bool(re.search(r"\b(proxim|perto|near|close|ao lado)", rest)),
                        "in": when[0].isoformat(), "out": when[1].isoformat()})
    return out


HOTEL_KINDS = {"rating": ("⭐ Melhor avaliado", "⭐ Best reviewed"), "location": ("📍 Melhor localização", "📍 Best location"),
               "value": ("💰 Melhor custo-benefício", "💰 Best value")}


def hotel_text(city: str, picks: list[dict], check_in: str, check_out: str, lang: str | None,
               more: bool = False) -> tuple[str, list[dict]]:
    nights = (date.fromisoformat(check_out) - date.fromisoformat(check_in)).days
    head = tr(lang, f"{'Mais hotéis' if more else 'Hotéis'} em {city}, {_fmt(check_in)} → {_fmt(check_out)} ({nights} noites):",
              f"{'More hotels' if more else 'Hotels'} in {city}, {_fmt(check_in)} → {_fmt(check_out)} ({nights} nights):")
    if not picks:
        return head + "\n" + tr(lang, "Não tenho mais opções com avaliações suficientes nessas datas." if more
                                 else "Não achei hotéis com avaliações suficientes nessas datas.",
                                 "No more options with enough reviews on those dates." if more
                                 else "No hotels with enough reviews on those dates."), []
    blocks, links = [], []
    for h in picks:
        title = HOTEL_KINDS[h["kind"]][0 if lang != "en" else 1]
        loc = f" · {tr(lang, 'localização', 'location')} {h['location']}" if h.get("location") else ""
        stars = f" · {h['stars']}★" if h.get("stars") else ""
        total = f" ({tr(lang, 'total', 'total')} {money(h['total'])})" if h.get("total") else ""
        blocks.append(f"{title}\n{h['name']}\n{h['rating']} ({h['reviews']} {tr(lang, 'avaliações', 'reviews')}){loc}{stars}"
                      f"\n{money(h['per_night'])}/{tr(lang, 'noite', 'night')}{total}")
        if h.get("link"):
            links.append({"label": h["name"], "url": h["link"]})
    return head + "\n\n" + "\n\n".join(blocks), links


def hotel_dates_text(city: str, rows: list[dict], lang: str | None) -> str:
    """rows: {in, out, pick (best-value hotel), base}; cheapest total first, with the saving against the current dates."""
    base = next(r for r in rows if r["base"])
    lines = []
    for r in sorted(rows, key=lambda r: r["total"]):
        h, diff = r["pick"], r["total"] - base["total"]
        tag = tr(lang, " (datas atuais)", " (current dates)") if r["base"] else (
            f" ({'-' if diff < 0 else '+'}{money(abs(diff))})")
        lines.append(f"{_fmt(r['in'])} → {_fmt(r['out'])}: {money(r['total'])} · {h['name']} · {h['rating']}{tag}")
    return (tr(lang, f"Hotéis em {city}: melhor custo-benefício por datas (mesmas noites).\n\n",
               f"Hotels in {city}: best value by dates (same nights).\n\n") + "\n".join(lines))


def flight_on(q: dict, dep: str, ret: str, lang: str | None) -> tuple[str, list[dict]]:
    """Live flights for one exact date pair (used when the user moves the trip to cheaper hotel dates)."""
    r = server.search_flights(q["origin_airports"], q["destination_airports"], dep, ret, limit=30, max_hours=q.get("max_hours"))
    ok = [o for o in r["options"] if fits(o, q)][:3]
    lines = [f"{i}) " + _line(o, dep, ret, lang) for i, o in enumerate(ok, 1)] or [
        tr(lang, "Nenhum voo encontrado com essas condições nessas datas.", "No flights matched these conditions on those dates.")]
    head = f"{q['origin_airports']} → {q['destination_airports']}" + conditions(q, lang)
    url = r.get("google_flights_url")
    return head + "\n\n" + "\n".join(lines), ([{"label": f"Google Flights {_fmt(dep)}→{_fmt(ret)}", "url": url}] if url else [])


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
    late_start = {**q, "depart_from": "2026-08-01", "depart_to": "2026-10-31"}  # August is gone: next year's
    assert validate(late_start, t) is None and late_start["depart_from"] == "2027-08-01"
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
    two = {**q, "destination_city": "Tokyo", "destination_also": ["Osaka", "Tokyo", "Atlantis"]}
    assert validate(two, t) is None and two["destination_code"] == "TYO" and two["_multi"]
    assert two["destination_airports"] == "HND,NRT," + resolve("Osaka", None, None)[1]  # dupes and unknowns dropped
    jaw = {**q, "destination_city": "Shanghai", "destination_code": "SHA", "destination_airports": None,
           "return_from": "Tokyo"}
    assert validate(jaw, t) is None and jaw["_return_airports"] == "HND,NRT" and jaw["separate_tickets"] and jaw["_multi"]
    same = {**q, "return_from": "Toquio"}  # back from the destination itself is a plain round trip
    assert validate(same, t) is None and not same["_return_airports"]
    f = lambda d, price, country, km=500, stops=1: {"dest": d, "price": price, "country": country, "km": km, "stops": stops}
    fares = [f("A", 100, "ES"), f("B", 110, "ES"), f("C", 120, "ES"), f("D", 130, "IT", stops=0), f("E", 900, "JP", 10000),
             f("F", 300, "MA", 3500), f("G", 140, "PT", stops=0), f("H", 150, "FR")]
    got = [(g, x["dest"]) for g, x in suggest(sorted(fares, key=lambda x: x["price"]), 7)]
    assert got == [("cheap", "A"), ("cheap", "B"), ("cheap", "D"), ("cheap", "G"), ("cheap", "H"),
                   ("far", "F"), ("far", "E")], got  # C: third Spanish city; F is cheaper per km than E
    hr = hotel_requests("Agora quero hotéis nas seguintes datas:\n\nDias 17/05 até 19/05 um hotel proximo da Disneyland Toquio"
                        "\n\nDias 21/05 a 25/05 um Hotel em Osaka\ndias 26/05 a 28/05 um hotel em Kyoto\n"
                        "Dias 31/05/2027 até 07/06/2027 um hotel em Tokyo\nsó 1 data 02/06 aqui", t)
    assert [(h["place"], h["near"], h["in"], h["out"]) for h in hr] == [
        ("Disneyland Toquio", True, "2027-05-17", "2027-05-19"), ("Osaka", False, "2027-05-21", "2027-05-25"),
        ("Kyoto", False, "2027-05-26", "2027-05-28"), ("Tokyo", False, "2027-05-31", "2027-06-07")], hr
    assert hotel_requests("28/12 a 03/01 em Lisboa", t)[0]["out"] == "2027-01-03"  # across new year
    h = lambda prices: {"price_history": [[i, p] for i, p in enumerate(prices)], "typical_price_range": [3950, 5300]}
    assert deal(4000, h([5000] * 20), 200) == (10, "buy", "flat")  # never cheaper in 20 days
    assert deal(5600, h([4000] * 20), 200) == (1, "wait", "flat")  # always cheaper, far away: alert
    assert deal(5600, h([4000] * 20), 30) == (1, "change", "flat")  # always cheaper, little time: other dates
    assert deal(5600, h([4000] * 20), 10)[1] == "buy"  # 10 days out: fares rarely drop
    assert deal(4500, h([4000] * 7 + [4800] * 7), 200)[2] == "up"
    assert deal(3900, {"typical_price_range": [3950, 5300]}, 200)[0] == 9 and deal(4000, {}, 200) is None
    assert lowcost({"airlines": ["Ryanair"]}) and not lowcost({"airlines": ["Iberia"]})
    far = {**q, "depart_from": "2027-08-01", "depart_to": "2027-10-31", "min_days": 20, "max_days": 30}
    assert validate(far, t) is None and far["depart_to"] == "2027-08-05" and far["_horizon"] == "2027-08-25"
    late = {**q, "depart_from": "2027-09-01", "depart_to": "2027-10-10", "min_days": 20, "max_days": 30}
    assert "/alerta MAD TYO 01/09-09/11 20-30d" in validate(late, t)  # whole trip past the horizon: no search
    near = {**q, "depart_from": "2027-04-01", "depart_to": "2027-05-31"}
    assert validate(near, t) is None and "_horizon" not in near
    o = lambda price, minutes: {"price": price, "minutes": minutes}
    assert faster([o(1000, 1200), o(1100, 900), o(1140, 840), o(1300, 600)]) == o(1140, 840)  # 1300 is > +15%
    assert faster([o(1000, 1200), o(1100, 1150)]) is None  # saves under 2h
    assert faster([o(1000, None), o(1010, 300)]) is None
    f = lambda price, stops, minutes=600: {"price": price, "stops": stops, "minutes": minutes}
    rows = [{"opts": [f(900, 2), f(1000, 1), f(1500, 0, 900)]}, {"opts": [f(800, 2, 3000), f(1400, 0)]}]
    assert cheapest_with(rows, {"max_stops": 1}, 0)[1]["price"] == 1400  # stop limit ignored, cheapest nonstop
    assert cheapest_with(rows, {"max_hours": 20}, 2)[1]["price"] == 900  # 800 is 50h, over the limit
    assert cheapest_with(rows, {}, 3) is None
    pairs = cross({"2027-04-01": "A", "2027-04-05": "B", "2027-04-09": None}, {"2027-04-20": "X", "2027-04-28": "Y"}, 15, 25)
    assert pairs == {("2027-04-01", "2027-04-20"): ("A", "X"), ("2027-04-05", "2027-04-20"): ("B", "X"),
                     ("2027-04-05", "2027-04-28"): ("B", "Y")}  # 01->28 is 27 days, 09 has no fare
    print("search selftest ok")


if __name__ == "__main__":
    selftest()
