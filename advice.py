"""On-demand trip advice after a search: self-transfer and hidden-city checks (paid), and the whole trip's cost (free).
Each one works on the last search's best option, q["_best"], set by search.run."""
from collections import Counter

import search
import server
from alerts import money, tr

TRANSFER_WARNING = ("⚠️ São passagens separadas: se o 1º voo atrasar e você perder o 2º, a companhia do 2º não tem obrigação "
                    "de te remarcar. A mala sai na conexão e precisa ser despachada de novo, e você pode precisar de visto "
                    "para entrar no país da conexão.",
                    "⚠️ These are separate tickets: if the first flight is late and you miss the second, its airline owes "
                    "you nothing. Bags come out at the hub and must be checked in again, and you may need a visa to enter "
                    "the hub's country.")
HIDDEN_WARNING = ("⚠️ Cidade oculta: você compra até o destino final e desce na escala. Só funciona com passagem só de ida "
                  "(ou no último trecho): ao faltar a um voo, a companhia cancela o resto da reserva, inclusive a volta. "
                  "Só bagagem de mão, porque a despachada segue até o destino final. Não use seu número de fidelidade: "
                  "é contra as regras da maioria das companhias, que podem cobrar a diferença ou bloquear a conta. Se a "
                  "companhia mudar a rota, a escala pode deixar de ser a sua cidade.",
                  "⚠️ Hidden city: you buy to the final destination and get off at the layover. Only on a one-way ticket "
                  "(or the last leg): skip a flight and the airline cancels the rest of the booking, return included. "
                  "Carry-on only, since checked bags go to the final destination. Don't add your frequent-flyer number: "
                  "most airlines forbid it and may charge the difference or close the account. If the airline reroutes "
                  "you, the layover may no longer be your city.")


def _need_search(lang: str | None) -> str:
    return tr(lang, "Primeiro faça uma busca de voo (ex: \"Madrid para Tóquio em maio, 20 dias\"), depois peço isso "
                    "sobre a melhor opção.",
              "Search for a flight first (e.g. \"Madrid to Tokyo in May, 20 days\"), then ask again about the best option.")


def self_transfer(q: dict | None, lang: str | None) -> str:
    """The best option's dates rebuilt as separate tickets through the cheapest hub the cache knows, each way.
    Costs about 4 paid searches per direction (direct + leg 1 + leg 2 on the day and the next)."""
    b = (q or {}).get("_best")
    if not b:
        return _need_search(lang)
    legs = [(q["origin_code"], q["destination_code"], q["origin_airports"], q["destination_airports"], b["dep"],
             tr(lang, "Ida", "Out"))]
    if b["ret"]:
        legs.append((q.get("_return_code") or q["destination_code"], q["origin_code"],
                     q.get("_return_airports") or q["destination_airports"], q["origin_airports"], b["ret"],
                     tr(lang, "Volta", "Back")))
    before, lines, total = server._month_calls(), [], 0.0
    for oc, dc, oa, da, day, label in legs:
        hubs = search.hubs_for(oc, dc, day[:7], 1)
        # ponytail: one hub per direction (4 paid searches); compare_split screens its default list when the cache has none
        hub = [(search.resolve(None, hubs[0][0], None) or ("", hubs[0][0]))[1]] if hubs else None
        r = server.compare_split(oa, da, day, hubs=hub, max_hubs=1)
        direct = r["direct"][0]["price"] if r["direct"] else None
        if not r["splits"]:
            lines.append(f"{label} {search._fmt(day)}: " + tr(lang, "nenhuma conexão por conta própria com 3 a 24h de espera.",
                                                              "no self-transfer with a 3 to 24h layover."))
            total = float("inf")
            continue
        s = r["splits"][0]
        total += s["price"]
        lines.append(f"{label} {search._fmt(day)}: {money(s['price'])} "
                     + tr(lang, f"via {s['hub']} ({s['leg1']['route']} {money(s['leg1']['price'])} "
                                f"{', '.join(s['leg1']['airlines'])} + {s['leg2']['route']} {money(s['leg2']['price'])} "
                                f"{', '.join(s['leg2']['airlines'])}, conexão de {s['connection_hours']:g}h)",
                          f"via {s['hub']} ({s['leg1']['route']} {money(s['leg1']['price'])} "
                          f"{', '.join(s['leg1']['airlines'])} + {s['leg2']['route']} {money(s['leg2']['price'])} "
                          f"{', '.join(s['leg2']['airlines'])}, {s['connection_hours']:g}h layover)")
                     + (tr(lang, f", contra {money(direct)} num bilhete só de ida.", f", against {money(direct)} on one "
                           "one-way ticket.") if direct else "."))
    if total < float("inf"):
        diff = b["price"] - total
        lines.append(tr(lang, f"\nTotal por conta própria: {money(total)}, ", f"\nSelf-transfer total: {money(total)}, ")
                     + (tr(lang, f"{money(diff)} a menos que a opção 1 ({money(b['price'])}).",
                           f"{money(diff)} less than option 1 ({money(b['price'])}).") if diff > 0 else
                        tr(lang, f"não compensa: a opção 1 sai {money(-diff)} mais barata.",
                           f"not worth it: option 1 is {money(-diff)} cheaper.")))
    used = server._month_calls() - before
    return (tr(lang, "🔀 Conexão por conta própria nas datas da opção 1:\n", "🔀 Self-transfer on option 1's dates:\n")
            + "\n".join(lines) + "\n\n" + tr(lang, *TRANSFER_WARNING)
            + tr(lang, f"\n({used} buscas pagas)", f"\n({used} paid searches)"))


def beyond(dest_code: str, origin_code: str, month: str, top: int = 4) -> list[str]:
    """Cities the cache shows most flights to from the destination: likely onward legs of itineraries that
    connect there. One airport each, for a single live search."""
    seen = Counter(t["destination"] for t in server.tp_month(dest_code, None, month)
                   if t["destination"] not in (origin_code, dest_code))
    return [(search.resolve(None, c, None) or ("", c))[1].split(",")[0] for c, _ in seen.most_common(top)]


def hidden_city(q: dict | None, lang: str | None) -> str:
    """One-way tickets to cities past the destination whose route stops there and cost less than flying to it.
    2 paid searches: the one-way to the destination (often cached) and one search to every onward city at once."""
    b = (q or {}).get("_best")
    if not b:
        return _need_search(lang)
    dest_air = set(q["destination_airports"].split(","))
    onward = beyond(q["destination_code"], q["origin_code"], b["dep"][:7])
    if not onward:
        return tr(lang, "Não sei para onde se voa a partir desse destino (cache vazio), então não dá para procurar cidade "
                        "oculta.", "The cache knows no onward flights from that destination, so I can't look for a hidden city.")
    before = server._month_calls()
    direct = min((o["price"] for o in server.search_flights(q["origin_airports"], q["destination_airports"], b["dep"],
                                                            limit=30)["options"]), default=None)
    far = server.search_flights(q["origin_airports"], ",".join(onward), b["dep"], limit=100)
    hits = sorted((o for o in far["options"] if o.get("route") and set(o["route"].split("-")[1:-1]) & dest_air
                   and (direct is None or o["price"] < direct)), key=lambda o: o["price"])[:2]
    used = server._month_calls() - before
    head = tr(lang, f"🕵️ Cidade oculta, só ida em {search._fmt(b['dep'])} (testei voos para {', '.join(onward)} com escala "
                    f"em {q['destination_airports']}):\n",
              f"🕵️ Hidden city, one way on {search._fmt(b['dep'])} (checked flights to {', '.join(onward)} stopping at "
              f"{q['destination_airports']}):\n")
    if not hits:
        body = tr(lang, "Nenhuma passagem passando pelo seu destino saiu mais barata"
                        + (f" que o só ida direto ({money(direct)})." if direct else "."),
                  "No ticket stopping at your destination came out cheaper"
                  + (f" than the straight one-way ({money(direct)})." if direct else "."))
    else:
        body = "\n".join(tr(lang, f"• {money(o['price'])} · {', '.join(o['airlines'])} · {o['route']}: desça em "
                                  f"{next(a for a in o['route'].split('-')[1:-1] if a in dest_air)}"
                                  + (f", {money(direct - o['price'])} a menos que o só ida direto ({money(direct)})" if direct else ""),
                            f"• {money(o['price'])} · {', '.join(o['airlines'])} · {o['route']}: get off at "
                            f"{next(a for a in o['route'].split('-')[1:-1] if a in dest_air)}"
                            + (f", {money(direct - o['price'])} less than the straight one-way ({money(direct)})" if direct else ""))
                         for o in hits)
    note = tr(lang, "\nNuma ida e volta, use só na ida comprada como passagem só de ida; a volta vai num bilhete separado.",
              "\nOn a round trip, use it only for the outbound bought as a one-way; the return goes on a separate ticket."
              ) if b["ret"] else ""
    return head + body + note + "\n\n" + tr(lang, *HIDDEN_WARNING) + tr(lang, f"\n({used} buscas pagas)", f"\n({used} paid searches)")


def record_hotel(s: dict, place: str, check_in: str, check_out: str, picks: list[dict]):
    """Remember a hotel search for the trip total: the best-value pick (else the first) of that stay."""
    pick = next((p for p in picks if p["kind"] == "value"), picks[0] if picks else None)
    if not pick:
        return
    nights = search._days(check_in, check_out)
    hotels = s.setdefault("trip", {}).setdefault("hotels", {})
    hotels[(place, check_in, check_out)] = {"name": pick["name"], "total": pick.get("total") or pick["per_night"] * nights,
                                            "cheapest": min(p.get("total") or p["per_night"] * nights for p in picks)}


def trip_total(s: dict, lang: str | None) -> str:
    """Flight + hotels searched in this chat, and where the numbers say it can get cheaper. Free."""
    trip = s.get("trip") or {}
    f, hotels = trip.get("flight"), trip.get("hotels") or {}
    if not f and not hotels:
        return tr(lang, "Ainda não tenho voo nem hotel nesta conversa para somar. Busque um voo e hotéis primeiro.",
                  "No flight or hotel in this chat to add up yet. Search a flight and hotels first.")
    lines, total, save = [tr(lang, "💰 Custo estimado da viagem:", "💰 Estimated trip cost:")], 0.0, []
    if f:
        n = f["adults"]
        total += f["price"] * n
        lines.append(tr(lang, f"Voo: {money(f['price'] * n)}" + (f" ({n} × {money(f['price'])})" if n > 1 else ""),
                        f"Flight: {money(f['price'] * n)}" + (f" ({n} × {money(f['price'])})" if n > 1 else ""))
                     + f" · {f['label']}")
        save += [(tr(lang, f"Voo: {what}", f"Flight: {what}"), amount * n) for what, amount in f["savings"] if amount > 0]
    for (place, a, b), h in sorted(hotels.items(), key=lambda kv: kv[0][1]):
        total += h["total"]
        lines.append(tr(lang, f"Hotel {place} {search._fmt(a)} → {search._fmt(b)}: {money(h['total'])} ({h['name']})",
                        f"Hotel {place} {search._fmt(a)} → {search._fmt(b)}: {money(h['total'])} ({h['name']})"))
        if h["cheapest"] < h["total"]:
            save.append((tr(lang, f"Hotel {place}: outra das 3 opções", f"Hotel {place}: another of the 3 picks"),
                         h["total"] - h["cheapest"]))
    lines.append(tr(lang, f"Total: {money(total)}", f"Total: {money(total)}"))
    if save:
        save.sort(key=lambda x: -x[1])
        best = sum(a for _, a in save[:3])
        lines.append(tr(lang, f"\nOnde economizar (até {money(best)}, {round(100 * best / total)}% do total):",
                        f"\nWhere to save (up to {money(best)}, {round(100 * best / total)}% of the total):"))
        lines += [f"• {what}: -{money(amount)}" for what, amount in save[:3]]
    lines.append(tr(lang, "\nHotéis: depois de uma busca de hotel, pergunte \"quais dias estão mais baratos?\" para comparar "
                          "datas. Preços de hotel são o total da estadia; o voo é por adulto vezes o número de adultos.",
                    "\nHotels: after a hotel search, ask \"which days are cheaper?\" to compare dates. Hotel prices are the "
                    "whole stay; the flight is per adult times the number of adults."))
    return "\n".join(lines)
