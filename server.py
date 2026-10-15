# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp>=1.2,<2", "httpx"]
# ///
"""Flight search MCP server: live Google Flights (SerpApi) + free cached prices (Travelpayouts/Aviasales)."""
import json
import logging
import os
import re
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from datetime import date as date_cls
from pathlib import Path

import httpx
from mcp.server.fastmcp import FastMCP

for _name in ("httpx", "httpx2"):  # INFO logs full URLs, which carry API keys/tokens
    logging.getLogger(_name).setLevel(logging.WARNING)

ROOT = Path(__file__).parent
if (ROOT / ".env").exists():
    for line in (ROOT / ".env").read_text().splitlines():
        k, _, v = line.partition("=")
        if k.strip() and not k.lstrip().startswith("#"):
            os.environ.setdefault(k.strip(), v.split(" #")[0].strip())  # allow trailing "  # comment"

CURRENCY = os.environ.get("CURRENCY", "BRL")
SERP_MONTHLY_CAP = int(os.environ.get("SERPAPI_MONTHLY_CAP", "250"))
CACHE_TTL = 6 * 3600
SEATS_KEY = os.environ.get("SEATS_AERO_KEY")  # unset = award (miles/points) search off
FP_KEY = os.environ.get("FLIGHTPOWERS_KEY")  # unset = FlightPowers off, SerpApi only
FP_CAP = int(os.environ.get("FLIGHTPOWERS_MONTHLY_CAP", "2500"))
FP_HOST = "google-flights-live-api.p.rapidapi.com"
FP_MAX_PAIRS = int(os.environ.get("FLIGHTPOWERS_MAX_PAIRS", "6"))  # 1 call per airport pair: SAO x TYO is 3x2
CABINS = {"economy": "Y", "premium": "W", "business": "J", "first": "F"}
PROGRAMS = ("smiles azul flyingblue aeroplan united american delta emirates etihad qatar turkish lufthansa singapore "
            "virginatlantic alaska qantas velocity jetblue connectmiles ethiopian saudia finnair eurobonus aeromexico").split()
# ponytail: static hub list tuned for Brazil <-> Europe/Americas; pass hubs= for other regions
DEFAULT_HUBS = ["LIS", "OPO", "MAD", "BCN", "CDG", "AMS", "FRA", "FCO", "LHR", "MIA", "JFK", "PTY", "BOG", "SCL", "EZE"]

db = sqlite3.connect(ROOT / "flights.db", check_same_thread=False)
db.executescript("""
create table if not exists cache (key text primary key, ts real, body text);
create table if not exists usage (month text primary key, calls int);
""")

mcp = FastMCP("flights", instructions="""
Cheap flight finder. Strategy, cheapest first:
1. price_calendar (free, cached up to 7 days old) to find cheap dates for a month.
2. search_flights (costs 1 SerpApi search) on the 1-3 best dates to get live prices.
   Pass several airports comma-separated (e.g. "GRU,VCP,CGH") - still 1 search.
3. compare_split (costs ~1 + 3 per hub) only when the user wants to beat the direct fare.
Always search 1 adult at a time (fare buckets: multi-seat searches can push every seat to a pricier bucket).
Split tickets are separate bookings: a delay on leg 1 does NOT protect leg 2. Always say so.
Report how many paid searches were used (usage tool) and prefer booking on the airline site.
Price alerts are created only with Telegram commands (/alerta, /alertas, /remover); point the user there.
4. booking_options (1 paid search, 2 for round trips) when the user wants to buy: pass the option number
   from search_flights. Links reach the user as buttons automatically - never write or invent URLs.
5. country (default "br") is the point of sale. Compare countries only when asked; each one is 1 paid search.
   Prices always come converted to BRL; paying in foreign currency adds IOF (~3.5%) plus card spread.
Never compute how far away a date is yourself: quote days_until_departure from the tool results.
""")


_db_lock = threading.RLock()  # SerpApi and FlightPowers run in parallel threads on one connection


def _cached(key: str, fetch):
    with _db_lock:
        row = db.execute("select ts, body from cache where key=?", (key,)).fetchone()
    if row and time.time() - row[0] < CACHE_TTL:
        return json.loads(row[1])
    body = fetch()
    with _db_lock:
        db.execute("replace into cache values (?,?,?)", (key, time.time(), json.dumps(body)))
        db.commit()
    return body


def _count(key: str):
    with _db_lock:
        db.execute("insert into usage values (?,1) on conflict(month) do update set calls=calls+1", (key,))
        db.commit()


def _month_calls() -> int:
    with _db_lock:  # guided searches run their live calls in parallel threads
        row = db.execute("select calls from usage where month=?", (date.today().strftime("%Y-%m"),)).fetchone()
    return row[0] if row else 0


def serp(**params) -> dict:
    params = {"engine": "google_flights", "currency": CURRENCY, "hl": "en", "gl": "br", **params}

    def fetch():
        if _month_calls() >= SERP_MONTHLY_CAP:
            raise RuntimeError(f"SerpApi monthly cap reached ({SERP_MONTHLY_CAP}); raise SERPAPI_MONTHLY_CAP after upgrading")
        r = httpx.get("https://serpapi.com/search.json", params={**params, "api_key": os.environ["SERPAPI_KEY"]}, timeout=90)
        _count(date.today().strftime("%Y-%m"))
        body = r.json()
        if "error" in body:
            if "hasn't returned any results" in body["error"]:
                return {}
            raise RuntimeError(body["error"])
        return body

    return _cached("serp:" + json.dumps(params, sort_keys=True), fetch)


def tp_month(origin: str, dest: str | None, month: str, return_month: str | None = None) -> list[dict]:
    """Cached Aviasales tickets departing in month (YYYY-MM); round trips when return_month is given.
    dest=None returns fares to every destination the cache has from origin."""
    params = {"origin": origin, "departure_at": month, "one_way": "false" if return_month else "true",
              "sorting": "price", "limit": 1000, "currency": CURRENCY.lower(),
              **({"destination": dest} if dest else {}), **({"return_at": return_month} if return_month else {})}

    def fetch():
        r = httpx.get("https://api.travelpayouts.com/aviasales/v3/prices_for_dates",
                      params=params, headers={"X-Access-Token": os.environ["TRAVELPAYOUTS_TOKEN"]}, timeout=30)
        if r.status_code == 400:  # e.g. return month > 30 days after the departure month: no data, not a failure
            return []
        r.raise_for_status()
        return r.json().get("data") or []

    return _cached("tp:" + json.dumps(params, sort_keys=True), fetch)


def itineraries(body: dict) -> list[dict]:
    out = []
    for it in body.get("best_flights", []) + body.get("other_flights", []):
        legs = it["flights"]
        if "price" not in it:
            continue
        out.append({
            "price": it["price"],
            "depart": legs[0]["departure_airport"]["time"],
            "arrive": legs[-1]["arrival_airport"]["time"],
            "route": "-".join([legs[0]["departure_airport"]["id"]] + [l["arrival_airport"]["id"] for l in legs]),
            "airlines": sorted({l["airline"] for l in legs}),
            "flights": [l.get("flight_number") for l in legs],
            "stops": len(legs) - 1,
            "minutes": it.get("total_duration"),
            "_booking_token": it.get("booking_token"),
            "_departure_token": it.get("departure_token"),
        })
    return sorted(out, key=lambda x: x["price"])


def _price(it: dict) -> float | None:
    for k in ("total_price_as_number", "price_as_number", "total_price", "price"):
        v = it.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
    return None


def _minutes(text: str | None, seconds=None) -> int | None:
    """"21 hr 30 min" / "27 hr" -> minutes (round trips only give the outbound as text); one-way gives seconds."""
    if isinstance(seconds, (int, float)):
        return int(seconds // 60)
    m = re.fullmatch(r"\s*(?:(\d+)\s*hr)?\s*(?:(\d+)\s*min)?\s*", str(text or ""))
    return int(m[1] or 0) * 60 + int(m[2] or 0) if m and (m[1] or m[2]) else None


def fp_parse(body, max_hours: float | None = None) -> list[dict]:
    """FlightPowers response -> the itinerary shape itineraries() returns (no clock times or booking tokens).
    ponytail: field names follow the public docs, not a captured response; check against a real payload."""
    if isinstance(body, dict):
        body = next((v for k in ("flights", "itineraries", "results", "data") if isinstance(v := body.get(k), list)), [])
    out = []
    for it in body or []:
        price = _price(it)
        if price is None:
            continue
        rt = "departure_flight_airline" in it  # round trip: outbound fields carry a departure_flight_ prefix
        pre = "departure_flight_" if rt else ""
        minutes = _minutes(it.get("departure_flight_duration") if rt else None, it.get("duration_seconds"))
        if max_hours and minutes and minutes > max_hours * 60:
            continue
        names = re.split(r"[|,]", str(it.get(pre + "airline") or ""))
        out.append({"price": price, "depart": None, "arrive": None, "route": "",
                    "airlines": sorted({n.strip() for n in names if n.strip()}), "flights": [],
                    "stops": int(it.get(pre + "stops") or 0), "minutes": minutes, "source": "flightpowers",
                    "buy_link": it.get("buy_link"), "_booking_token": None, "_departure_token": None})
    return sorted(out, key=lambda x: x["price"])


def _fp_call(frm: str, to: str, date: str, return_date: str | None, adults: int, max_stops: int | None) -> object:
    payload = {"departure_date": date, "from_airport": frm, "to_airport": to, "currency": CURRENCY, "limit": 15,
               "sort_type": "Price", "seat_type": 1, "passengers": [1] * adults,
               **({"max_stops": max_stops} if max_stops is not None else {}),
               **({"return_date": return_date} if return_date else {})}

    def fetch():
        month = "fp:" + date_cls.today().strftime("%Y-%m")
        with _db_lock:
            row = db.execute("select calls from usage where month=?", (month,)).fetchone()
        if row and row[0] >= FP_CAP:
            raise RuntimeError(f"FlightPowers monthly cap reached ({FP_CAP}); raise FLIGHTPOWERS_MONTHLY_CAP")
        r = httpx.post(f"https://{FP_HOST}/api/google_flights/{'roundtrip' if return_date else 'oneway'}/v1", json=payload,
                       headers={"X-RapidAPI-Key": FP_KEY, "X-RapidAPI-Host": FP_HOST}, timeout=90)
        _count(month)
        if r.headers.get("X-Search-Status") == "empty" or r.status_code == 404:
            return []
        r.raise_for_status()
        return r.json()

    return _cached("fp:" + json.dumps(payload, sort_keys=True), fetch)


def fp_flights(origin: str, destination: str, date: str, return_date: str | None = None, adults: int = 1,
               max_stops: int | None = None, max_hours: float | None = None) -> list[dict]:
    """FlightPowers (Google Flights, live). One call per airport pair, at most FP_MAX_PAIRS, in parallel.
    Pairs past the cap are covered by SerpApi only (it takes every airport in one search)."""
    pairs = [(a, b) for a in origin.split(",") for b in destination.split(",")][:FP_MAX_PAIRS]
    with ThreadPoolExecutor(len(pairs)) as ex:
        bodies = list(ex.map(lambda ab: _fp_call(ab[0], ab[1], date, return_date, adults, max_stops), pairs))
    return sorted((o for b in bodies for o in fp_parse(b, max_hours)), key=lambda x: x["price"])


def merge_options(serp_its: list[dict], fp_its: list[dict]) -> list[dict]:
    """Cheapest first, one row per itinerary. FlightPowers has no clock times, so the same carriers + stops
    (+ duration within 15 min when both know it) count as the same flight; the lower price wins. Rows keep the
    SerpApi option number (booking_options needs it); FlightPowers-only rows have option None and a buy_link."""
    norm = lambda it: ("".join(sorted(a.lower().replace(" ", "") for a in it["airlines"])), it["stops"])
    out = [{**it, "source": "serpapi", "option": n} for n, it in enumerate(serp_its, 1)]
    for f in fp_its:
        same = next((o for o in out if norm(o) == norm(f) and (not o["minutes"] or not f["minutes"]
                                                                or abs(o["minutes"] - f["minutes"]) <= 15)), None)
        if not same:
            out.append({**f, "option": None})
        elif f["price"] < same["price"]:
            same.update(price=f["price"], source="both", buy_link=f["buy_link"])
    return sorted(out, key=lambda x: x["price"])


def days_until(d: str) -> int:
    """Models get date arithmetic wrong; hand them the number."""
    return (datetime.strptime(d[:10], "%Y-%m-%d").date() - date.today()).days


def public(its: list[dict]) -> list[dict]:
    """Numbered itineraries without the long internal tokens (the model only needs the option number)."""
    return [{"option": n, **{k: v for k, v in it.items() if not k.startswith("_")}} for n, it in enumerate(its, 1)]


def _t(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M")


def best_splits(first: list[dict], second: list[dict], min_h: float, max_h: float, top: int = 3) -> list[dict]:
    """Cheapest leg2 for each leg1 whose departure leaves min_h..max_h after leg1 lands (both hub-local times)."""
    combos = []
    for a in first:
        gap = lambda b: (_t(b["depart"]) - _t(a["arrive"])).total_seconds() / 3600
        ok = [b for b in second if min_h <= gap(b) <= max_h]
        if ok:
            b = min(ok, key=lambda x: x["price"])
            combos.append({"price": a["price"] + b["price"], "connection_hours": round(gap(b), 1), "leg1": a, "leg2": b})
    return sorted(combos, key=lambda c: c["price"])[:top]


@mcp.tool()
def price_calendar(origin: str, destination: str, month: str) -> dict:
    """FREE. Cheapest cached one-way price per day for a month (YYYY-MM), from Aviasales searches up to 7 days old.
    Use to pick dates before spending live searches. Prices are hints, not bookable quotes."""
    days: dict[str, dict] = {}
    for t in tp_month(origin, destination, month):
        d = t["departure_at"][:10]
        if d not in days or t["price"] < days[d]["price"]:
            days[d] = {"price": t["price"], "transfers": t.get("transfers"), "airline": t.get("airline"),
                       "_link": t.get("link")}
    cheapest = sorted(days.items(), key=lambda kv: kv[1]["price"])[:5]
    marker = os.environ.get("TRAVELPAYOUTS_MARKER")
    links = [{"label": f"Aviasales {d[8:]}/{d[5:7]} {CURRENCY} {v['price']}",
              "url": f"https://www.aviasales.com{v['_link']}" + (f"&marker={marker}" if marker else "")}
             for d, v in cheapest if v["_link"]]
    strip = lambda v: {k: x for k, x in v.items() if not k.startswith("_")}
    return {"currency": CURRENCY, "days": {d: strip(v) for d, v in sorted(days.items())},
            "cheapest": [(d, strip(v)) for d, v in cheapest], "links": links}


def _search(origin, destination, date, return_date=None, adults=1, max_stops=None, country="br", **extra) -> dict:
    return serp(departure_id=origin, arrival_id=destination, outbound_date=date, adults=adults, gl=country.lower(),
                type=1 if return_date else 2, stops={None: 0, 0: 1, 1: 2}.get(max_stops, 3),
                **({"return_date": return_date} if return_date else {}), **extra)


@mcp.tool()
def search_flights(origin: str, destination: str, date: str, return_date: str | None = None,
                   adults: int = 1, max_stops: int | None = None, limit: int = 5, country: str = "br",
                   max_hours: float | None = None, airlines: str | None = None) -> dict:
    """LIVE Google Flights, costs 1 paid search (plus FlightPowers calls when FLIGHTPOWERS_KEY is set: both sources are
    searched at once and merged, cheapest first; options with option=None come only from FlightPowers, use buy_link).
    IATA codes, comma-separated for several airports (GRU,VCP,CGH).
    Dates YYYY-MM-DD. With return_date, prices are round-trip totals and flights shown are the outbound.
    country = 2-letter point of sale (br, pt, us...). max_hours caps each flight's duration (outbound and return).
    airlines = comma-separated IATA airline codes (CA,QR) to see only those carriers."""
    serp_kw = dict(**({"max_duration": int(max_hours * 60)} if max_hours else {}),
                   **({"include_airlines": airlines} if airlines else {}))
    if not FP_KEY:
        body = _search(origin, destination, date, return_date, adults, max_stops, country, **serp_kw)
        its, errors = public(itineraries(body)), []
    else:  # both sources at once; one failing is not fatal
        with ThreadPoolExecutor(2) as ex:
            fp = ex.submit(fp_flights, origin, destination, date, return_date, adults, max_stops, max_hours)
            body, errors = {}, []
            try:
                body = _search(origin, destination, date, return_date, adults, max_stops, country, **serp_kw)
            except Exception as e:
                errors.append(f"serpapi: {type(e).__name__}: {str(e)[:150]}")
            try:
                fp_its = fp.result()
            except Exception as e:
                fp_its = []
                errors.append(f"flightpowers: {type(e).__name__}: {str(e)[:150]}")
        if len(errors) == 2:
            raise RuntimeError("; ".join(errors))
        its = [{k: v for k, v in o.items() if not k.startswith("_")} for o in merge_options(itineraries(body), fp_its)]
    url = body.get("search_metadata", {}).get("google_flights_url") or next((o.get("buy_link") for o in its if o.get("buy_link")), None)
    return {"currency": CURRENCY, "country": country, "options": its[:limit],
            "days_until_departure": days_until(date),
            "price_insights": body.get("price_insights"),
            "google_flights_url": url,
            **({"source_errors": errors} if errors else {}),
            "paid_searches_this_month": _month_calls()}


def pick_hotels(props: list[dict], min_reviews: int = 100, exclude=()) -> list[dict]:
    """Three picks from Google Hotels results: best reviewed, best location, best value (cheapest well-rated).
    ponytail: field names (overall_rating, location_rating, rate_per_night) follow SerpApi's google_hotels docs."""
    rows = []
    for h in props:
        price = (h.get("rate_per_night") or {}).get("extracted_lowest")
        if (price and (h.get("reviews") or 0) >= min_reviews and h.get("overall_rating")
                and not re.search(r"hostel|hostal|capsule|cápsula|dormitor", str(h.get("name")), re.I)):
            rows.append({"name": h.get("name"), "rating": h["overall_rating"], "reviews": h["reviews"],
                         "location": h.get("location_rating"), "stars": h.get("extracted_hotel_class"), "per_night": price,
                         "total": (h.get("total_rate") or {}).get("extracted_lowest"), "link": h.get("link")})
    picks, used = [], set(exclude)
    for kind, key in (("rating", lambda r: (r["rating"], r["reviews"])), ("location", lambda r: (r["rating"] >= 4.0, r["location"] or 0, r["rating"])),
                      ("value", lambda r: (r["rating"] >= 4.2, -r["per_night"]))):
        r = next((r for r in sorted(rows, key=key, reverse=True) if r["name"] not in used), None)
        if r:
            used.add(r["name"])
            picks.append({"kind": kind, **r})
    return picks


@mcp.tool()
def hotels(city: str, check_in: str, check_out: str, adults: int = 2, exclude: list[str] | None = None,
           near: bool = False) -> dict:
    """LIVE Google Hotels via SerpApi, costs 1 paid search. Returns 3 picks: best reviewed, best location, best value
    (cheapest among well-rated). exclude = names already shown, to get the next best ones (same search, cached).
    Dates YYYY-MM-DD; prices in the configured currency, per night and total."""
    body = serp(engine="google_hotels", q=f"hotels {'near' if near else 'in'} {city}", check_in_date=check_in, check_out_date=check_out,
                adults=adults)
    return {"currency": CURRENCY, "picks": pick_hotels(body.get("properties") or [], exclude=exclude or ()),
            "paid_searches_this_month": _month_calls()}


@mcp.tool()
def compare_split(origin: str, destination: str, date: str, hubs: list[str] | None = None, max_hubs: int = 3,
                  min_connection_hours: float = 3, max_connection_hours: float = 24) -> dict:
    """One-way: single ticket vs two separate tickets via a hub (origin->hub + hub->destination).
    Without hubs, screens DEFAULT_HUBS on free cached prices and keeps the best max_hubs.
    Costs 1 + 3*hubs paid searches (leg2 is searched on date and date+1)."""
    before = _month_calls()
    notes = []
    if not hubs:
        month = date[:7]
        score = {}
        for h in DEFAULT_HUBS:
            if h in (origin, destination):
                continue
            a, b = tp_month(origin, h, month), tp_month(h, destination, month)
            if a and b:
                score[h] = min(t["price"] for t in a) + min(t["price"] for t in b)
        hubs = sorted(score, key=score.get)[:max_hubs] or DEFAULT_HUBS[:max_hubs]
        notes.append(f"hubs screened on cached prices: {hubs}" if score else "no cached data, using default hubs")

    direct = itineraries(serp(departure_id=origin, arrival_id=destination, outbound_date=date, type=2))
    next_day = (datetime.strptime(date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    splits = []
    for h in hubs:
        leg1 = itineraries(serp(departure_id=origin, arrival_id=h, outbound_date=date, type=2))
        leg2 = [i for d in (date, next_day)
                for i in itineraries(serp(departure_id=h, arrival_id=destination, outbound_date=d, type=2))]
        splits += [{"hub": h, **c} for c in best_splits(leg1, leg2, min_connection_hours, max_connection_hours)]
    splits.sort(key=lambda c: c["price"])
    best_direct = direct[0]["price"] if direct else None
    return {
        "currency": CURRENCY,
        "direct": public(direct[:3]),
        "splits": [{**s, "leg1": public([s["leg1"]])[0], "leg2": public([s["leg2"]])[0]} for s in splits[:5]],
        "saving_vs_direct": best_direct - splits[0]["price"] if direct and splits else None,
        "warning": "Separate tickets: missed connection on leg 2 is your loss; checked bags must be re-checked.",
        "notes": notes,
        "paid_searches_used": _month_calls() - before,
        "paid_searches_this_month": _month_calls(),
    }


@mcp.tool()
def round_trip_calendar(origin: str, destination: str, depart_from: str, depart_to: str,
                        min_days: int, max_days: int = 30) -> dict:
    """FREE. For flexible round trips: scores every (departure, return) pair with departure in
    depart_from..depart_to (YYYY-MM-DD) and a stay of min_days..max_days, using cached prices.
    Use it first for 'best dates in a period' requests, then confirm the top 2-3 pairs with search_flights.
    One code per side: airport or city code (TYO = all Tokyo airports, SAO, LON...)."""
    import alerts  # lazy: alerts imports this module
    if "," in origin + destination:
        raise ValueError("one code per side here; use the city code (e.g. TYO for HND+NRT)")
    return alerts.round_trip_ideas(origin, destination, depart_from, depart_to, min_days, max_days)


@mcp.tool()
def booking_options(origin: str, destination: str, date: str, return_date: str | None = None, option: int = 1,
                    return_option: int = 1, country: str = "br") -> dict:
    """Where to buy search_flights result number `option` (same args as that search): airline site and agencies,
    each with price and a direct booking link. Costs 1 paid search (one-way) or 2 (round trip: the return
    flight `return_option` must be picked first)."""
    its = itineraries(_search(origin, destination, date, return_date, country=country))
    if not 1 <= option <= len(its):
        raise ValueError(f"option must be 1..{len(its)}")
    it = its[option - 1]
    if return_date:
        rets = itineraries(_search(origin, destination, date, return_date, country=country,
                                   departure_token=it["_departure_token"]))
        if not 1 <= return_option <= len(rets):
            raise ValueError(f"return_option must be 1..{len(rets)}")
        it = rets[return_option - 1]
    body = _search(origin, destination, date, return_date, country=country, booking_token=it["_booking_token"])
    sellers, links = [], []
    for b in body.get("booking_options", []):
        o = b.get("together") or b.get("departing") or {}
        req = o.get("booking_request") or {}
        sellers.append({"seller": o.get("book_with"), "airline_site": o.get("airline", False), "price": o.get("price"),
                        "local_prices": o.get("local_prices"), "baggage": o.get("baggage_prices")})
        if req.get("url") and o.get("price"):
            # Google's click-through accepts the POST payload as a GET query and redirects to the seller
            links.append({"label": f"{o.get('book_with')} {CURRENCY} {o.get('price')}" + (" (oficial)" if o.get("airline") else ""),
                          "url": f"{req['url']}?{req.get('post_data', '')}"})
    return {"currency": CURRENCY, "flight": public([it])[0], "sellers": sellers, "links": links,
            "paid_searches_this_month": _month_calls()}


def parse_awards(data: list[dict], cabin: str) -> list[dict]:
    """Seats.aero availability objects -> bookable award seats in one cabin, fewest miles first."""
    c = CABINS[cabin]
    out = []
    for a in data:
        try:
            miles = int(a.get(f"{c}MileageCost") or 0)
        except (TypeError, ValueError):
            continue
        if a.get(f"{c}Available") and miles > 0:
            out.append({"date": a["Date"], "program": a["Source"], "miles": miles, "airlines": a.get(f"{c}Airlines") or "",
                        "direct": bool(a.get(f"{c}Direct")), "seats": a.get(f"{c}RemainingSeats"),
                        "from": a["Route"]["OriginAirport"], "to": a["Route"]["DestinationAirport"]})
    return sorted(out, key=lambda x: x["miles"])


def awards(origin: str, dest: str, start: str, end: str, cabin: str = "economy", programs: list[str] | None = None) -> list[dict]:
    """Cached award availability (Seats.aero Pro: 1,000 calls/day, no SerpApi quota). [] when SEATS_AERO_KEY is unset.
    ponytail: miles only; taxes need include_trips (per-flight data), add if people book from here"""
    if not SEATS_KEY:
        return []
    params = {"origin_airport": origin, "destination_airport": dest, "start_date": start, "end_date": end,
              "cabins": cabin, "order_by": "lowest_mileage", "take": 500,
              **({"sources": ",".join(programs)} if programs else {})}

    def fetch():
        r = httpx.get("https://seats.aero/partnerapi/search", params=params,
                      headers={"Partner-Authorization": SEATS_KEY}, timeout=30)
        r.raise_for_status()
        return r.json().get("data") or []

    return parse_awards(_cached("seats:" + json.dumps(params, sort_keys=True), fetch), cabin)


def award_search(origin: str, destination: str, date_from: str, date_to: str, cabin: str = "economy",
                 programs: list[str] | None = None) -> dict:
    """Award seats bought with miles/points (Seats.aero cache, no paid SerpApi search). IATA codes, comma-separated
    for several airports; dates YYYY-MM-DD; cabin economy|premium|business|first; programs e.g. ["smiles","flyingblue"].
    Miles are per person, one way, taxes not included."""
    return {"options": awards(origin, destination, date_from, date_to, cabin, programs)[:15],
            "note": "miles per person, one way, taxes not included"}


if SEATS_KEY:  # the tool only exists for the models when the key is configured
    mcp.tool()(award_search)


@mcp.tool()
def usage() -> dict:
    """Paid SerpApi searches used this month vs cap."""
    return {"month": date.today().strftime("%Y-%m"), "used": _month_calls(), "cap": SERP_MONTHLY_CAP}


def selftest():
    body = {"best_flights": [{"price": 900, "total_duration": 600, "flights": [
        {"departure_airport": {"id": "GRU", "time": "2026-11-10 22:00"},
         "arrival_airport": {"id": "LIS", "time": "2026-11-11 11:00"}, "airline": "TAP", "flight_number": "TP 82"}]}],
        "other_flights": [{"flights": []}]}
    leg1 = itineraries(body)
    assert leg1[0]["route"] == "GRU-LIS" and leg1[0]["stops"] == 0
    leg2 = [{"price": 50, "depart": "2026-11-11 12:00"},   # 1h: too tight
            {"price": 120, "depart": "2026-11-11 15:00"},  # 4h: ok
            {"price": 80, "depart": "2026-11-12 18:00"}]   # 31h: too long
    s = best_splits(leg1, leg2, 3, 24)
    assert s[0]["price"] == 1020 and s[0]["connection_hours"] == 4.0, s
    sample = [{"Date": "2027-04-12", "Source": "smiles", "Route": {"OriginAirport": "MAD", "DestinationAirport": "NRT"},
               "YAvailable": True, "YMileageCost": "35000", "YAirlines": "AF", "YDirect": False, "YRemainingSeats": 4},
              {"Date": "2027-04-13", "Source": "united", "Route": {"OriginAirport": "MAD", "DestinationAirport": "NRT"},
               "YAvailable": False, "YMileageCost": "0"},
              {"Date": "2027-04-14", "Source": "flyingblue", "Route": {"OriginAirport": "MAD", "DestinationAirport": "HND"},
               "YAvailable": True, "YMileageCost": "30000", "YAirlines": "KL", "YDirect": False}]
    got = parse_awards(sample, "economy")
    assert [(a["program"], a["miles"]) for a in got] == [("flyingblue", 30000), ("smiles", 35000)], got
    fp = fp_parse([{"total_price_as_number": 4800, "departure_flight_airline": "Air China | Iberia", "departure_flight_stops": 1,
                    "departure_flight_duration": "17 hr 5 min", "buy_link": "https://g/x"},
                   {"price_as_number": 5200, "airline": "Iberia", "stops": 0, "duration_seconds": 50400}])
    assert (fp[0]["airlines"], fp[0]["minutes"], fp[1]["stops"]) == (["Air China", "Iberia"], 1025, 0), fp
    twin = [{**leg1[0], "airlines": ["Air China", "Iberia"], "stops": 1, "minutes": 1010, "price": 5217}]
    m = merge_options(twin, fp)
    assert [(o["airlines"], o["price"], o["source"], o["option"]) for o in m] == [
        (["Air China", "Iberia"], 4800, "both", 1), (["Iberia"], 5200, "flightpowers", None)], m  # twin merged, cheaper wins
    assert fp_parse([{"price_as_number": 100, "airline": "X", "stops": 0, "duration_seconds": 99999}], max_hours=10) == []
    assert _minutes("27 hr") == 1620 and _minutes("nope") is None
    props = [{"name": "A", "overall_rating": 4.8, "reviews": 900, "location_rating": 3.0, "rate_per_night": {"extracted_lowest": 900}},
             {"name": "B", "overall_rating": 4.3, "reviews": 500, "location_rating": 4.9, "rate_per_night": {"extracted_lowest": 700}},
             {"name": "C", "overall_rating": 4.4, "reviews": 300, "location_rating": 4.0, "rate_per_night": {"extracted_lowest": 300}},
             {"name": "Sky Hostel", "overall_rating": 4.9, "reviews": 900, "location_rating": 4.9, "rate_per_night": {"extracted_lowest": 90}},
             {"name": "D", "overall_rating": 5.0, "reviews": 3, "rate_per_night": {"extracted_lowest": 50}}]
    assert [(h["kind"], h["name"]) for h in pick_hotels(props)] == [("rating", "A"), ("location", "B"), ("value", "C")]
    assert pick_hotels(props, exclude=["A", "B", "C"]) == []
    print("selftest ok")


if __name__ == "__main__":
    if sys.argv[1:2] == ["selftest"]:
        selftest()
    else:
        mcp.run()
