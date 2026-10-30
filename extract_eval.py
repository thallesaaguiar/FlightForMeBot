"""Scores request extraction (llm.extract + search.validate) on fixed cases. Free with Ollama.

    LLM=ollama uv run --with "mcp>=1.2,<2" --with httpx --with "anthropic>=1,<2" --with "openai>=3,<4" python extract_eval.py
"""
import time
from datetime import date

import llm
import search

TODAY = date(2026, 9, 29)
MADRID = {"origin_code": "MAD", "origin_airports": "MAD", "destination_code": "TYO", "destination_airports": "HND,NRT",
          "depart_from": "2027-04-01", "depart_to": "2027-05-31", "round_trip": True, "min_days": 15, "max_days": 25,
          "max_stops": 1, "more_stops_if_much_cheaper": True, "missing": ""}

EUROPE = {"origin_city": "Madrid", "origin_code": "MAD", "origin_airports": "MAD", "destination_region": "europe",
          "destination_city": None, "destination_code": None, "destination_airports": None, "depart_from": "2026-11-01",
          "depart_to": "2026-11-30", "round_trip": True, "min_days": 10, "max_days": 10, "max_stops": None,
          "max_price": None, "missing": ""}

# (message, current search or None, expected fields after validation)
CASES = [
    ("Madrid para Tóquio, HND ou NRT, entre abril e maio de 2027, ficando pelo menos 15 dias. Direto ou 1 escala, 2 só se compensar muito",
     None, {"origin_code": "MAD", "destination_code": "TYO", "depart_from": "2027-04-01", "depart_to": "2027-05-31",
            "round_trip": True, "min_days": 15, "max_stops": 1}),
    ("só ida de São Paulo pra Lisboa dia 20 de janeiro",
     None, {"origin_code": "SAO", "destination_code": "LIS", "depart_from": "2027-01-20", "depart_to": "2027-01-20",
            "round_trip": False}),
    ("Rio pra Paris, ida 10/12 volta 28/12",
     None, {"origin_code": "RIO", "destination_code": "PAR", "depart_from": "2026-12-10", "depart_to": "2026-12-10",
            "round_trip": True, "min_days": 18, "max_days": 18}),
    ("Lisboa para Nova York em julho de 2027, entre 10 e 14 dias, só voo direto",
     None, {"origin_code": "LIS", "destination_code": "NYC", "depart_from": "2027-07-01", "depart_to": "2027-07-31",
            "round_trip": True, "min_days": 10, "max_days": 14, "max_stops": 0}),
    ("Porto para Tóquio saindo entre 1 e 15 de novembro, voltando 3 semanas depois",
     None, {"origin_code": "OPO", "destination_code": "TYO", "depart_from": "2026-11-01", "depart_to": "2026-11-15",
            "round_trip": True, "min_days": 21, "max_days": 21}),
    ("voo de Brasília pra Salvador dia 5/11, só ida",
     None, {"origin_code": "BSB", "destination_code": "SSA", "depart_from": "2026-11-05", "round_trip": False}),
    ("quero viajar pra Europa", None, {"_asks": True}),
    ("e se for em junho?", MADRID,
     {"origin_code": "MAD", "destination_code": "TYO", "depart_from": "2027-06-01", "depart_to": "2027-06-30",
      "min_days": 15, "max_stops": 1}),
    ("aceito 2 escalas", MADRID,
     {"depart_from": "2027-04-01", "depart_to": "2027-05-31", "max_stops": 2, "destination_code": "TYO"}),
    ("e saindo de Lisboa?", MADRID,
     {"origin_code": "LIS", "destination_code": "TYO", "depart_from": "2027-04-01", "min_days": 15}),
    ("fico só 10 dias", MADRID, {"min_days": 10, "max_days": 10, "depart_from": "2027-04-01", "destination_code": "TYO"}),
    ("na verdade só ida", MADRID, {"round_trip": False, "origin_code": "MAD", "depart_from": "2027-04-01"}),
    ("mas só na primeira quinzena de maio", MADRID, {"depart_from": "2027-05-01", "depart_to": "2027-05-15", "min_days": 15}),
    # a real chat: no dates at first, then the gaps filled in over several messages
    ("faca uma busca de melhores combinações de datas para uma viagem de pelo menos 15 dias sem contar com voo. "
     "origem: Madrid, Destino: Tokyo. 2 pessoas adultas",
     None, {"origin_code": "MAD", "destination_code": "TYO", "round_trip": True, "min_days": 15, "adults": 2,
            "_default_dates": True}),
    ("destino: Narita ou Haneda em Toquio", {**MADRID, "destination_code": "", "destination_airports": "",
                                              "depart_from": None, "depart_to": None},
     {"origin_code": "MAD", "destination_code": "TYO", "min_days": 15, "_default_dates": True}),
    ("entre Abril e Maio de 2027", {**MADRID, "depart_from": "2026-10-13", "depart_to": "2027-01-11"},
     {"origin_code": "MAD", "destination_code": "TYO", "depart_from": "2027-04-01", "depart_to": "2027-05-31"}),
    ("faca uma busca de melhores combinações de datas a a partir de Abril de 2027 para uma viagem de pelo menos 15 dias "
     "sem contar com voo. origem: Madrid, Destino: Tokyo. 2 pessoas adultas",
     None, {"origin_code": "MAD", "destination_code": "TYO", "depart_from": "2027-04-01", "depart_to": "2027-05-31",
            "min_days": 15, "adults": 2}),
    ("procure por voos com ate 19h de duracao tanto pra ida quanto para volta", MADRID,
     {"max_hours": 19.0, "depart_from": "2027-04-01", "destination_code": "TYO", "origin_code": "MAD"}),
    # open destination: a region instead of a city
    ("quero uma viagem saindo de Madrid, 10 dias pela Europa em novembro",
     None, {"origin_code": "MAD", "destination_region": "europe", "_explore": True, "depart_from": "2026-11-01",
            "depart_to": "2026-11-30", "round_trip": True, "min_days": 10}),
    ("de São Paulo pra qualquer lugar da América do Sul em dezembro, uma semana, até 1500 reais",
     None, {"origin_code": "SAO", "destination_region": "south_america", "_explore": True, "min_days": 7,
            "max_price": 1500.0}),
    ("e em dezembro?", EUROPE, {"destination_region": "europe", "_explore": True, "origin_code": "MAD",
                                "depart_from": "2026-12-01", "depart_to": "2026-12-31"}),
    ("pode ser Roma", EUROPE, {"destination_code": "ROM", "_explore": False, "origin_code": "MAD", "min_days": 10}),
    # a country as destination, a 6-month window, airline preference, budget with separate tickets
    ("Quais são as melhores datas para viajar para o japao saindo de Madrid, viagem a partir de Abril até setembro de "
     "2027? Quero ficar pelo menos 15 dias no Japão",
     None, {"origin_code": "MAD", "_explore": True, "destination_region": "country:JP", "depart_from": "2027-04-01",
            "depart_to": "2027-08-10", "min_days": 15}),  # cut where the return still fits Google's ~330 days
    ("Consegue pesquisar voo mais baratos? AirChina estava ficando bem barato", MADRID,
     {"origin_code": "MAD", "destination_code": "TYO",
      "airlines": lambda v: bool(v) and "airchina" in "".join(v).lower().replace(" ", "")}),
    ("ajuste as datas para outras opcoes que fique mais baratos. Minha meta é pagar até uns R$3.600 por pessoa.\n\n"
     "Faça buscas com ida e volta separadas", MADRID,
     {"max_price": 3600.0, "separate_tickets": True, "destination_code": "TYO", "origin_code": "MAD"}),
    # several destination cities, in to one and back from the other (open jaw)
    ("A Origem sempre vai ser Madrid na Espanha e o Destino é Tokyo ou Osaka no japao. Quero ida e volta para ambas as "
     "cidades para ver qual opcao mais barata. Pode ser indo para Tokyo e voltando por osaka. Entre agosto e outubro, "
     "pelo menos 20 dias",
     None, {"origin_code": "MAD", "destination_code": "TYO", "separate_tickets": True, "depart_from": "2027-08-01",
            "depart_to": "2027-08-05", "min_days": 20,  # cut where the return fits Google's ~330 days
            "destination_airports": lambda v: "KIX" in str(v) and "HND" in str(v)}),
    ("faça buscas indo e voltando por Osaka tambem", MADRID,
     {"origin_code": "MAD", "destination_code": "TYO", "separate_tickets": True,
      "destination_airports": lambda v: "KIX" in str(v) and "NRT" in str(v), "depart_from": "2027-04-01"}),
    ("Faça uma busca de passagem saindo de Madrid para Joanesburgo ou Cidade do cabo. Pode ser indo por um aeroporto e "
     "voltando por outro tbm. Pesquise a partir de Abril de 2027, ficando de 15 a 20 dias",
     None, {"origin_code": "MAD", "destination_code": "JNB", "separate_tickets": True, "depart_from": "2027-04-01",
            "depart_to": "2027-05-31", "min_days": 15, "destination_airports": lambda v: "CPT" in str(v)}),
    # strict open jaw: in to one city, back from another (2026-10-30, the return city was dropped)
    ("Quero que você faça uma busca de passagens saindo de MADRID para Xangai na China na ida e a volta Tokyo no Japão "
     "para MADRID. Viagem de pelo menos 20 dias. Faça buscas a partir de Maio de 2027",
     None, {"origin_code": "MAD", "destination_code": lambda v: v in ("SHA", "PVG"), "_return_code": "TYO",
            "separate_tickets": True, "depart_from": "2027-05-01", "min_days": 20}),
    ("Acho que você não entendeu, quero chegar por Xangai e voltar por Tokyo",
     {**MADRID, "destination_code": "SHA", "destination_airports": "PVG,SHA", "destination_city": "Shanghai",
      "depart_from": "2027-05-01", "depart_to": "2027-06-30", "min_days": 20},
     {"origin_code": "MAD", "destination_code": lambda v: v in ("SHA", "PVG"), "_return_code": "TYO",
      "_return_airports": "HND,NRT", "depart_from": "2027-05-01"}),
    # trip ideas: no destination, the bot suggests
    ("quero viajar 10 dias saindo de Madrid para qualquer destino em novembro",
     None, {"origin_code": "MAD", "destination_region": "anywhere", "_explore": True, "depart_from": "2026-11-01",
            "min_days": 10}),
    ("me sugere um lugar pra ir saindo de São Paulo em dezembro, uma semana",
     None, {"origin_code": "SAO", "destination_region": "anywhere", "_explore": True, "min_days": 7}),
    ("e qualquer lugar da Ásia?", MADRID, {"destination_region": "asia", "_explore": True, "origin_code": "MAD",
                                          "depart_from": "2027-04-01"}),
]


def main():
    passed, times = 0, []
    for text, current, want in CASES:
        t = time.time()
        try:
            q = llm.extract(text, dict(current) if current else None, TODAY)
            problem = search.validate(q, TODAY)
            got = {**q, "_asks": bool(problem)}
            ok = lambda k, v: v(got.get(k)) if callable(v) else got.get(k) == v
            bad = {k: (got.get(k), "check" if callable(v) else v) for k, v in want.items() if not ok(k, v)}
            if problem and not want.get("_asks"):
                bad["validate"] = problem
        except Exception as e:
            bad = {"error": f"{type(e).__name__}: {e}"[:120]}
        times.append(time.time() - t)
        passed += not bad
        print(f"{'OK  ' if not bad else 'FAIL'} {times[-1]:5.1f}s  {text[:60]!r}" + (f"\n      {bad}" if bad else ""))
    print(f"\n{passed}/{len(CASES)} passed · avg {sum(times) / len(times):.1f}s · providers {llm.EXTRACT_ORDER}")


if __name__ == "__main__":
    main()
