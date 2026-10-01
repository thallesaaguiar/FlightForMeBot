# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp>=1.2,<2", "httpx"]
# ///
"""Flight search MCP server: live Google Flights (SerpApi) + free cached prices (Travelpayouts/Aviasales)."""
import json
import logging
import os
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta
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


def _cached(key: str, fetch):
    row = db.execute("select ts, body from cache where key=?", (key,)).fetchone()
    if row and time.time() - row[0] < CACHE_TTL:
        return json.loads(row[1])
    body = fetch()
    db.execute("replace into cache values (?,?,?)", (key, time.time(), json.dumps(body)))
    db.commit()
    return body


def _month_calls() -> int:
    row = db.execute("select calls from usage where month=?", (date.today().strftime("%Y-%m"),)).fetchone()
    return row[0] if row else 0


def serp(**params) -> dict:
    params = {"engine": "google_flights", "currency": CURRENCY, "hl": "en", "gl": "br", **params}

    def fetch():
        if _month_calls() >= SERP_MONTHLY_CAP:
            raise RuntimeError(f"SerpApi monthly cap reached ({SERP_MONTHLY_CAP}); raise SERPAPI_MONTHLY_CAP after upgrading")
        r = httpx.get("https://serpapi.com/search.json", params={**params, "api_key": os.environ["SERPAPI_KEY"]}, timeout=90)
        db.execute("insert into usage values (?,1) on conflict(month) do update set calls=calls+1",
                   (date.today().strftime("%Y-%m"),))
        db.commit()
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
                   max_hours: float | None = None) -> dict:
    """LIVE Google Flights, costs 1 paid search. IATA codes, comma-separated for several airports (GRU,VCP,CGH).
    Dates YYYY-MM-DD. With return_date, prices are round-trip totals and flights shown are the outbound.
    country = 2-letter point of sale (br, pt, us...). max_hours caps each flight's duration (outbound and return)."""
    body = _search(origin, destination, date, return_date, adults, max_stops, country,
                   **({"max_duration": int(max_hours * 60)} if max_hours else {}))
    return {"currency": CURRENCY, "country": country, "options": public(itineraries(body))[:limit],
            "days_until_departure": days_until(date),
            "price_insights": body.get("price_insights"),
            "google_flights_url": body.get("search_metadata", {}).get("google_flights_url"),
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
    print("selftest ok")


if __name__ == "__main__":
    if sys.argv[1:2] == ["selftest"]:
        selftest()
    else:
        mcp.run()
