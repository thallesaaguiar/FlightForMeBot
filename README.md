# FlightForMeBot

A Telegram bot and MCP server that looks for cheap flights and watches prices for you.

You write what you want ("Madrid to Tokyo from April 2027, at least 15 days, up to one stop") and the bot searches the best date combinations, compares round trips with separate one-way tickets, and replies with prices and booking buttons. You can also set price alerts and get a message when a fare drops under your target.

It runs fine on a local model through [Ollama](https://ollama.com), with no paid LLM. Fares come from free cached prices from Aviasales (Travelpayouts Data API) and live Google Flights prices, from SerpApi and, optionally, FlightPowers (both searched together, cheapest wins).

`/start` lets each user pick Portuguese or English. Prices are shown in BRL by default. The code and the prompts are in English.

## What it does

Searching

- Plain messages start a guided search. The bot picks the most promising dates from the free cache, checks 3 of them live and lists the results.
- Follow-up messages refine the last search: "what about June?", "2 stops is fine", "only 10 days", "up to 19h per flight", "under R$ 3,600".
- "More options" or "other dates" checks dates not checked yet in the session. "Day by day" (admins) checks 8 at once.
- Departure windows of up to about 6 months ("April to September"). Without dates, it searches the next 3 months and says so.
- Open destinations: "from Madrid, 10 days anywhere in Europe in November". A country also works ("to Japan"), so Tokyo and Osaka compete.
- Separate tickets: prices the same dates as two one-way tickets next to the round trip.
- A preferred airline ("Air China") gets its own line. A budget is reported as met or missed, with the closest price.
- Booking buttons open the airline or agency page directly.
- Two live sources at once: with `FLIGHTPOWERS_KEY` set, every live search asks SerpApi and FlightPowers in parallel and merges the answers, one row per itinerary at the lower price. If one source fails or runs out of quota, the other keeps working.
- "Searching, one moment…" is sent as soon as the request is understood, before the slow part.

Hotels

- After the flight results the bot asks whether you want hotels, and for which of the listed date pairs (buttons).
- It shows 3 picks from Google Hotels (1 SerpApi search): best reviewed, best location and best value (cheapest among well-rated). Hostels and capsules are left out, and hotels need at least 100 reviews.
- "More hotels" shows the next 3 from the same cached search, no extra cost.
- "Which days are cheaper?" prices the same stay moved by -7, -3, +3 and +7 days (up to 4 paid searches) and offers to redo the flight search on the cheaper dates.

Alerts

- `/alerta GRU LIS 20/01 05/02 ±5 3000` watches 121 date combinations and tells you when a round trip drops to R$ 3,000 or less.
- An alert can cover one date, each date ±N days, or any trip of N to M days inside a window.
- Checks run every 6 hours and use no LLM.

Also

- Miles and points through Seats.aero (optional, off by default, see below).
- Agent mode (`/agente`): the LLM plans the search itself. It works well with Claude and poorly with small local models.
- The same search tools run as an MCP server for Claude Code and Claude Desktop.

## How a search works

```
message ──▶ local LLM reads it into parameters (route, dates, stay, stops, budget)
        ──▶ code checks them against the message and reference lists
        ──▶ free cache scores every date pair
        ──▶ 3 live Google Flights searches (SerpApi + FlightPowers) confirm the best ones
        ──▶ code writes the reply (no LLM)
```

The model only reads the request. Everything else is fixed code, which is why a 20B local model is enough. The code also double-checks the model:

- Places come from the Travelpayouts city, airport and country lists, never from codes the model makes up. Without this, Porto became POR (an airport in Finland) and "Japan" plus the code JPN became a heliport in Washington.
- A place, region or airline counts only if it appears in the message. A refinement that names no date keeps the previous dates.
- Months, "3 weeks", "10 to 14 days" and "from April" are also read from the text when the model misses them.
- Requests for miles, programs and cabins are read from the text by code alone.

## Example conversation

A real chat, translated notes in brackets. This run used SerpApi only; the searches show 0 paid searches because the results were still in the 6 hour cache.

```
You: Quero viajar de Madrid para Tóquio, no Japão, a partir de abril de 2027. Quero ficar pelo menos 15 dias
     lá, sem contar as horas de voo. Pode ser ida e volta juntos ou bilhetes separados, até 1 escala.
     Mostre as melhores combinações de datas.
Bot: Buscando, aguarde um momento…   [searching, one moment]
Bot: MAD → HND,NRT, ida e volta, 15-25 dias, até 1 escala(s)
     Ida entre 01/04 e 31/05 (faltam 171 dias). Cache: 4 preços; testei 2 datas novas ao vivo (0 buscas pagas).

     1) 15/04 → 05/05 (20 dias): R$ 5.217 · Air China · 1 escala(s) · 17h00
        ↳ Separadas: R$ 6.957 (ida R$ 3.545 China Southern + volta R$ 3.412 Cathay Pacific)

     2) 11/04 → 05/05 (24 dias): R$ 5.602 · Cathay Pacific · 1 escala(s) · 19h05
        ↳ Separadas: R$ 5.582 (ida R$ 2.170 Air China + volta R$ 3.412 Cathay Pacific)

     Preços por adulto, ida e volta.
     Interpretado por claude.

You: Refaça a busca com as melhores combinações   [redo the search with the best combinations]
Bot: 1) 13/05 → 28/05 (15 dias): R$ 3.468 · Air China · 1 escala(s) · 21h00
        ↳ Separadas: R$ 4.793 (ida R$ 2.170 Air China + volta R$ 2.623 Air China)

     2) 12/04 → 05/05 (23 dias): R$ 5.836 · Iberia · direto · 14h00
        ↳ Separadas: R$ 6.220 (ida R$ 2.808 ITA + volta R$ 3.412 Cathay Pacific)

Bot: Quer que eu busque hotéis em Tokyo? Em quais datas?   [want hotels? which dates?]  [🏨 ①] [🏨 ②] [Não]
You: (taps ①)
Bot: Hotéis em Tokyo, 13/05 → 28/05 (15 noites):

     ⭐ Melhor avaliado
     Ryokan Sawanoya Tokyo
     4.8 (374 avaliações) · localização 3.2
     R$ 294/noite (total R$ 4.413)

     📍 Melhor localização
     ONE@Tokyo by insomnia
     4.3 (1010 avaliações) · localização 4.6 · 3★
     R$ 608/noite (total R$ 9.116)

     💰 Melhor custo-benefício
     plat hostel keikyu asakusa karin
     4.8 (318 avaliações) · localização 3.8 · 2★
     R$ 99/noite (total R$ 1.487)
```

The last pick is a hostel; this run came before hostels were filtered out.

## What it costs

| Source | Cost | Used for |
|---|---|---|
| Travelpayouts Data API | free | price calendars, date scoring, destinations from an origin |
| SerpApi (Google Flights) | 1 search per request, 250 free per month | live prices, booking links |
| FlightPowers (optional) | free plan 10 searches per month, then US$ 10 for 2,500 on RapidAPI | second live Google Flights source |
| Ollama | free, runs on your machine | reading requests |
| Seats.aero (optional) | US$ 9.99 per month | miles and points |

A guided search uses 3 paid SerpApi searches (and, with FlightPowers on, about the same number of FlightPowers calls, one per airport pair). Separate tickets use 3 per date pair (round trip, outbound, return), and a day-by-day scan up to 12. Each alert uses about 30 per month, plus 1 or 2 for booking links when it fires. `SERPAPI_MONTHLY_CAP` stops all paid searches once the monthly count is reached.

The cache only has fares someone searched on Aviasales in the last few days. Popular routes are well covered. Far-off dates, exact stay lengths and small airports often have gaps, and then the bot spreads its live searches across the window.

## Setup

You need Python 3.11+, [uv](https://docs.astral.sh/uv/), a [SerpApi](https://serpapi.com) key, a [Travelpayouts](https://www.travelpayouts.com) token and a bot token from [@BotFather](https://t.me/BotFather). For the local model:

```bash
brew install ollama
brew services start ollama
ollama pull gpt-oss:20b
```

Then:

```bash
git clone https://github.com/thallesaaguiar/FlightForMeBot.git && cd FlightForMeBot
cp .env.example .env        # fill in the keys; set LLM=ollama and OLLAMA_MODEL=gpt-oss:20b
uv run server.py selftest
uv run --with "mcp>=1.2,<2" --with httpx python alerts.py
uv run --with "mcp>=1.2,<2" --with httpx python search.py
uv run bot.py
```

Each script declares its own dependencies, so there is nothing else to install. Send `/id` to your bot, put the number in `TELEGRAM_CHAT_ID` and restart to become an admin.

For the command menu in Telegram, send `/setcommands` to @BotFather and paste:

```
buscar - nova busca de passagens
alerta - criar alerta de preço
alertas - listar meus alertas
remover - apagar um alerta
plano - ver meu limite
idioma - mudar idioma / change language
comandos - ver o que o bot faz
```

## Configuration

Everything lives in `.env`. [.env.example](.env.example) has the full list with comments.

| Variable | Default | Meaning |
|---|---|---|
| `TELEGRAM_CHAT_ID` | | admin chat ids, comma separated: `/agente`, `/comparar`, `/falhas`, day-by-day scans, no search quota |
| `OPEN_SIGNUP` | `0` | `1` lets anyone use the bot, with the free plan limits |
| `LLM` | `gemini,deepseek,claude` | providers to try in order; providers without a key or model are skipped. `ollama` for local only |
| `OLLAMA_MODEL` | | local model such as `gpt-oss:20b`; setting it enables Ollama |
| `CHAT_LLM` | `1` | `0` turns searching by message off; alerts keep working |
| `CHAT_MODE` | `buscar` | what a plain message starts: `buscar` (guided search) or `agente` |
| `SESSION_MINUTES` | `60` | idle minutes before a chat forgets the current search |
| `BUSCAR_LIVE` | `3` | dates a guided search checks live |
| `BUSCAR_DEEP` | `8` | dates a day-by-day scan checks live |
| `LLM_MAX_SEARCHES` | `10` | paid searches allowed per `/agente` question |
| `EXTRACT_EFFORT` | `low` | reasoning effort for reading requests (`low`, `medium`, `high`) |
| `SEATS_AERO_KEY` | | enables miles and points |
| `FLIGHTPOWERS_KEY` | | RapidAPI key; enables FlightPowers as a second live source next to SerpApi |
| `FLIGHTPOWERS_MONTHLY_CAP` | `2500` | monthly hard stop for FlightPowers calls |
| `SERPAPI_MONTHLY_CAP` | `250` | monthly hard stop for paid searches; `0` blocks every search |
| `CURRENCY` | `BRL` | currency for prices and messages |

The LLM fallback moves to the next provider only when one fails (error, timeout, no credit), not when an answer is poor. Reading a request follows the same order as `LLM`: with `LLM=claude,ollama`, Claude reads it and Ollama is only the backup. Put `ollama` first to read requests locally.

## Telegram commands

```
/buscar <request>                           new search, forgetting the previous one
/alerta GRU LIS 20/01 1500                 one way on 20/01
/alerta GRU LIS 20/01 05/02 3000           round trip, fixed dates
/alerta GRU LIS 20/01 05/02 ±5 3000        each date ±5 days (+-5 also works)
/alerta GRU LIS 01/01-28/02 7-10d 3000     7 to 10 day trip inside the window
/alertas                                    list your alerts
/remover 3                                  delete alert #3
/plano                                      your limits and usage
/idioma                                     pick Portuguese or English
/new                                        start over
/id                                         your chat id
/comandos                                   the command list
/agente <question>                          the LLM plans the search (admins)
/comparar <question>                        every LLM on the same question (admins)
/falhas                                     requests the bot did not understand (admins)
```

Free users get 2 alerts and 5 searches per month, `pro` users 10 alerts and 30 searches, admins no search limit. A clarifying question from the bot does not use the quota. There are no payments yet, so plans are changed by hand:

```bash
sqlite3 flights.db "update users set plan='pro' where chat_id=123456789"
```

Alert target prices are the total for one adult.

## Miles and points (optional, off by default)

Mentioning miles in a search ("com milhas", "usar Smiles", "executiva com pontos") adds a block with the cheapest award seat each way, the cheapest round trip inside one program, and what a thousand miles are worth against the cash fare. It reads cached award availability from [Seats.aero](https://seats.aero): 26 programs including GOL Smiles, Azul, Flying Blue, Aeroplan, United, Qatar and Turkish. LATAM Pass and Iberia Avios are not covered.

It stays off until `SEATS_AERO_KEY` is set; until then a request for miles gets a one-line notice. The key needs a Seats.aero Pro plan (1,000 calls per day, personal use only; commercial use needs an agreement with them). Lookups do not use the SerpApi quota. Award taxes are not shown, because the cached search does not return them. This part has only been tested against sample responses so far.

## Using it from Claude Code or Claude Desktop

```bash
claude mcp add flights -s user -- uv run --script /path/to/FlightForMeBot/server.py
```

For Claude Desktop, add this to `~/Library/Application Support/Claude/claude_desktop_config.json`, with the full path to `uv` (`which uv`):

```json
{
  "mcpServers": {
    "flights": {
      "command": "/opt/homebrew/bin/uv",
      "args": ["run", "--script", "/path/to/FlightForMeBot/server.py"]
    }
  }
}
```

| Tool | Paid searches | Returns |
|---|---|---|
| `price_calendar` | 0 | cheapest cached one-way fare per day of a month, with Aviasales links |
| `round_trip_calendar` | 0 | scored round-trip date pairs for a window and a length of stay |
| `search_flights` | 1 | live Google Flights options, numbered |
| `compare_split` | 1 + 3 per hub | one ticket vs. two tickets through a hub |
| `booking_options` | 1 (2 for round trips) | sellers for one flight, with booking links |
| `award_search` | 0 | award seats in miles (only when `SEATS_AERO_KEY` is set) |
| `usage` | 0 | paid searches used this month |

Booking links are 3,000 to 6,500 characters long. The bot sends them as Telegram buttons and never passes them through the model, because models corrupt long URLs when they copy them.

## Local model quality

`extract_eval.py` scores how requests are read on 26 fixed cases: new searches, refinements, regions, a country, budgets and real conversations that went wrong. It runs free on Ollama in about 3 minutes:

```bash
LLM=ollama uv run --with "mcp>=1.2,<2" --with httpx --with "anthropic>=1,<2" --with "openai>=3,<4" python extract_eval.py
```

With gpt-oss:20b on an M5 Pro (32 GB):

| Setup | Correct | Average time |
|---|---|---|
| plain prompt (first 13 cases) | 11/13 | 20 s |
| examples in the prompt, low effort | 9/13 | 6 s |
| plus the checks in code | 12/13 | 7 s |
| plus city lookup by name, temperature 0 (23 cases) | 23/23 | 7.5 s |
| plus country, airline, budget and separate tickets (26 cases) | 26/26 | 7.6 s |

Most of the gains came from code, not the prompt. A bigger prompt made the 20B model drop months it used to read and copy values from its own examples. Checking its answer against the message fixed that. Temperature 0 keeps the same message reading the same way.

Requests the bot had to question or failed on are saved and listed with `/falhas`. Each one worth fixing becomes a case in `extract_eval.py`.

For comparison, the same Madrid to Tokyo request in agent mode cost about US$ 0.14 and 6 to 25 paid searches on Claude Sonnet 5. The guided search found an equivalent R$ 3,740 fare with 3 paid searches and no LLM cost.

## Limitations

- FlightPowers is read from its documented response and from responses captured on the free plan (10 searches per month); the free plan runs out after a few searches, so a paid RapidAPI plan is needed for regular use.
- Hotel search needs a SerpApi search each and only covers Google Hotels prices.
- It finds fares and links to the booking page. It does not buy tickets.
- Separate tickets are separate bookings: if the first flight is late, the second airline does not have to rebook you.
- On round trips, the stops and duration shown are for the outbound flight. Google Flights only details the return after the outbound is chosen.
- Cached prices can be up to 7 days old. Alerts fire only after a live search confirms the price.
- Prices are converted to BRL. Paying in another currency adds IOF (about 3.5% on Brazilian cards) and the card's spread.
- Country names show in English in Portuguese replies ("Japan").
- Everything runs where `bot.py` runs. On a laptop, it has to stay on and awake.
- The Gemini free tier failed every test with 503 and 429 errors, so that path is untested.
- Check the SerpApi, Travelpayouts and Seats.aero terms before offering this to other people or charging for it.

## Data stored

`flights.db` is a SQLite file next to the code:

- `users`: chat id, first name, plan, language, sign-up time
- `alerts`: route, dates, target price, last prices seen
- `buscas`: searches per user per month
- `misses`: text of requests the bot did not understand, with the chat id (users are told on `/start`)
- `cache`: raw API responses for 6 hours, not linked to any user
- `usage`: paid searches per month

Search sessions and agent chats stay in memory and are lost when the bot restarts. `data/` holds the downloaded city, airport, country and airline lists.

## Files

| File | Role |
|---|---|
| `bot.py` | Telegram polling, commands, sessions, languages |
| `search.py` | guided search: validation, places, date selection, live checks, reply, miles block |
| `llm.py` | request extraction and its text checks, agent tool loop, provider fallback |
| `alerts.py` | alerts, plans and quotas, `/alerta` parsing, periodic check |
| `server.py` | data sources (SerpApi, FlightPowers, Travelpayouts, Hotels), cache, MCP tools |
| `extract_eval.py` | extraction accuracy check |

## License

MIT
