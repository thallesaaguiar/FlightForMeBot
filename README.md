# flight-agent

A Telegram bot and MCP server that looks for cheap flights and watches prices for you.

It mixes two data sources: free cached fares from Aviasales (through the Travelpayouts Data API) and live Google Flights prices (through SerpApi). Cached prices are used to narrow down dates for free, and live searches are spent only to confirm the promising ones. You can set price alerts with plain commands, or ask in natural language and let an LLM (Claude, Gemini, DeepSeek or a local Ollama model) run the search.

`/start` asks each user to pick Portuguese or English, and the bot answers in that language from then on. Prices are shown in BRL by default. The code and the prompts are in English.

## What it does

- Price alerts from Telegram commands. An alert can cover a single date, each date ±N days, or any trip of N to M days inside a window. For example, `/alerta GRU LIS 20/01 05/02 ±5 3000` watches 121 date combinations and tells you when a round trip drops to R$ 3,000 or less.
- Alerts are checked every 6 hours. When one fires, the message comes with buttons that open the airline or agency booking page directly.
- Guided search, the default for plain messages: an LLM only turns the request into parameters (route, date window, length of stay, stops). Fixed code then picks the most promising dates from the free cache, confirms 3 of them live and writes the reply. A local 20B model is enough for this, and each search costs 3 paid requests. Follow-up messages refine the last search ("and in June?", "2 stops is fine") without repeating the whole request.
- Open destination: "from Madrid, 10 days anywhere in Europe in November, under R$ 1,500". The cache lists fares from the origin to every destination it knows; code keeps the ones inside the region (a fixed country list per region), the dates, stay, stops and budget, then confirms the 3 cheapest live. Regions: Europe, South America, North and Central America with the Caribbean, Asia, Middle East, Africa, Oceania, same country, anywhere.
- Maximum flight time ("up to 19h per flight") goes to Google Flights as `max_duration`, which applies to the outbound and return flights.
- Agent mode with `/agente`: "cheapest dates Madrid to Tokyo in April or May 2027, staying at least 15 days". Here the LLM also plans the search, which works well with Claude and poorly with small local models.
- Conversations reset after 5 minutes without messages, so the next message starts a new search. Partial answers are kept inside a session: if the bot asks for the destination, the reply fills in that gap instead of starting over.
- Without dates ("find me the cheapest dates"), it searches departures in the next 3 months and says so. For several adults it shows the price per person and the estimated total.
- Split tickets: compares one ticket against two separate ones through a hub (for example GRU to LIS, then LIS to the destination), with a minimum connection time.
- Point of sale: a search can be made as if you were buying from another country. In my tests Google Flights returned the same converted price for Brazil and Portugal, so don't expect a difference every time.
- The same tools run as an MCP server, so Claude Code and Claude Desktop can use them without the bot.

The alert checks don't use an LLM. They are plain code, so you can run the bot with `CHAT_LLM=0` and spend nothing on model APIs.

## How searches are paid for

| Source | Cost | Used for |
|---|---|---|
| Travelpayouts Data API | free | price calendars, round-trip date scoring, screening hubs for split tickets |
| SerpApi (Google Flights) | 1 search per request, 250 free per month | live prices, booking links |

Travelpayouts only has fares that someone searched on Aviasales in the last few days. Popular routes are well covered. Far-off dates and small airports often have gaps, and then the bot has to rely on live searches.

Each alert makes one exploratory live search per day, rotating through its 10 most promising date pairs, plus a confirmation when the cache shows a price under the target. That is roughly 30 searches per alert per month. When an alert fires, getting the booking links costs 1 more search (2 for round trips). On the free SerpApi plan this is enough for about 4 people with 2 alerts each.

Free-text questions are capped at 10 paid searches each (`LLM_MAX_SEARCHES`). `SERPAPI_MONTHLY_CAP` stops all paid searches once the monthly count is reached.

## Tools

| Tool | Paid searches | What it returns |
|---|---|---|
| `price_calendar` | 0 | cheapest cached one-way fare per day of a month, plus Aviasales links for the 5 cheapest days |
| `round_trip_calendar` | 0 | scored round-trip date pairs for a departure window and a length of stay |
| `search_flights` | 1 | live Google Flights options, numbered, with price insights |
| `compare_split` | 1 + 3 per hub | single ticket vs. two tickets through a hub |
| `booking_options` | 1 (2 for round trips) | sellers for one flight, with price and a booking link |
| `usage` | 0 | paid searches used this month |

Booking links are 3,000 to 6,500 characters long. The bot sends them as Telegram buttons and never passes them through the model, because models tend to corrupt long URLs when they copy them.

## Requirements

- Python 3.11 or newer and [uv](https://docs.astral.sh/uv/). Each script declares its own dependencies, so there is nothing to install by hand.
- A [SerpApi](https://serpapi.com) key.
- A [Travelpayouts](https://www.travelpayouts.com) API token.
- A Telegram bot token from [@BotFather](https://t.me/BotFather).
- For free-text chat, at least one of: an Anthropic API key, a Gemini API key, a DeepSeek API key, or [Ollama](https://ollama.com) running locally.

## Setup

```bash
git clone <this repo> && cd flight-agent
cp .env.example .env        # then fill in the keys
uv run server.py selftest
uv run --with "mcp>=1.2,<2" --with httpx python alerts.py   # alert parser self-test
uv run --with "mcp>=1.2,<2" --with httpx python search.py   # /buscar validation self-test
uv run bot.py
```

Send any message to your bot. If your chat id is not in `TELEGRAM_CHAT_ID`, the bot replies with it. Put it in `.env` and restart.

To get the command menu in Telegram, send `/setcommands` to @BotFather and paste:

```
alerta - criar alerta de preço
alertas - listar meus alertas
remover - apagar um alerta
plano - ver meu limite
comandos - ver o que o bot faz
```

## Configuration

Everything lives in `.env`. See [.env.example](.env.example) for the full list.

| Variable | Default | Meaning |
|---|---|---|
| `TELEGRAM_CHAT_ID` | | admin chat ids, comma separated. Admins get `/agente`, `/comparar` and no search quota |
| `OPEN_SIGNUP` | `0` | `1` lets anyone use the alert commands |
| `CHAT_LLM` | `1` | `0` turns free-text chat off |
| `LLM` | `gemini,deepseek,claude` | providers to try, in order. Providers without a key are skipped |
| `LLM_MAX_SEARCHES` | `10` | paid searches allowed per free-text question |
| `BUSCAR_LIVE` | `3` | date pairs `/buscar` confirms live (one paid search each) |
| `CHAT_MODE` | `buscar` | what a plain message starts: `buscar` (guided search) or `agente` |
| `SESSION_MINUTES` | `5` | idle minutes before a chat forgets the current search |
| `EXTRACT_EFFORT` | `low` | reasoning effort Ollama uses to read requests (`low`, `medium`, `high`) |
| `OLLAMA_MODEL` | | a local model such as `gpt-oss:20b`. Setting it enables Ollama |
| `SERPAPI_MONTHLY_CAP` | `250` | monthly hard stop for paid searches. `0` blocks every search |
| `CURRENCY` | `BRL` | currency for prices and messages |

The fallback moves to the next provider only when one fails (an error, a timeout, no credit). It does not judge answer quality. Put the provider you trust most first.

## Telegram commands

```
/alerta GRU LIS 20/01 1500                 one way on 20/01
/alerta GRU LIS 20/01 05/02 3000           round trip, fixed dates
/alerta GRU LIS 20/01 05/02 ±5 3000        each date ±5 days (+-5 also works)
/alerta GRU LIS 01/01-28/02 7-10d 3000     7 to 10 day trip inside the window
/alertas                                    list your alerts
/remover 3                                  delete alert #3
/plano                                      show your alert limit
/comandos                                   show the command list
/buscar <request>                           new guided search, 3 paid searches
/agente <question>                          agent mode for the rest of the session (admins)
/comparar <question>                        run every LLM on the same question (admins)
/new                                        start over
/id                                         show your chat id
/idioma                                     pick Portuguese or English
/falhas                                     requests the bot did not understand (admins)
```

The target price is the total for one adult. Free users get 2 alerts and 5 guided searches per month; `pro` users get 10 alerts and 30 searches. Admins have no search limit. Asking a clarifying question does not use the quota. There are no payments yet, so plans are changed by hand:

```bash
sqlite3 flights.db "update users set plan='pro' where chat_id=123456789"
```

## Using it from Claude Code or Claude Desktop

```bash
claude mcp add flights -s user -- uv run --script /path/to/flight-agent/server.py
```

For Claude Desktop, add this to `~/Library/Application Support/Claude/claude_desktop_config.json`, with the full path to `uv` (run `which uv`):

```json
{
  "mcpServers": {
    "flights": {
      "command": "/opt/homebrew/bin/uv",
      "args": ["run", "--script", "/path/to/flight-agent/server.py"]
    }
  }
}
```

Alerts are managed only from Telegram. The MCP server exposes the search tools.

## Choosing a model

I ran the same request ("best dates Madrid to Tokyo in April and May 2027, at least 15 days, direct or one stop") on two models:

| Model | Time | Paid searches | Result |
|---|---|---|---|
| Claude Sonnet 5 | 109 s | 6 | found 06/05 to 22/05 for R$ 3,693 and compared April with May |
| gpt-oss:20b on Ollama (M5 Pro, 32 GB) | 97 s | 2 | suggested dates from one-way estimates without confirming a price |

That complex request cost about US$ 0.14 on Sonnet. The local model handled simple questions well ("cheapest day to fly GRU to LIS in November" in 15 seconds) but struggled with multi-step planning. Use `/comparar` to test the providers on your own questions.

`/buscar` exists because of that gap. The same Madrid to Tokyo request, with gpt-oss:20b only extracting the parameters, found 21/04 to 08/05 for R$ 3,740 with 3 paid searches. In a later free-text run Claude found an equivalent R$ 3,740 fare but used 25 searches. The extraction takes about 30 seconds on the local model. Code checks its output and fixes the common mistakes: reading the return date as the end of the departure window, putting a past date in the current year, dropping fields while refining a search, and reading "a trip of at least 15 days" as one way.

The model never picks airport codes on its own. It returns city names in English, and the code looks them up in the Travelpayouts city and airport lists (downloaded once into `data/`). Before this, the model turned Porto into POR and Salvador into SAL, which are real airports in Finland and El Salvador.

`extract_eval.py` scores the extraction on 13 fixed requests (new searches and refinements) and costs nothing with Ollama. Results with gpt-oss:20b:

| Setup | Correct | Average time |
|---|---|---|
| plain prompt | 11/13 | 20 s |
| examples in the prompt, low reasoning effort | 9/13 | 6 s |
| same, plus the checks in code | 12/13 | 7 s |
| same, medium reasoning effort | 12/13 | 22 s |
| low effort, checks, city lookup by name, 3 extra multi-message cases | 16/16 | 6 s |
| plus open destinations, flight time and "from April": 23 cases, temperature 0 | 23/23 | 7.5 s |

The default is low effort (`EXTRACT_EFFORT=low`) at temperature 0, so the same message always gets the same reading. Run the eval again after changing the prompt or the model.

Most of the gains came from code, not the prompt. Adding fields and examples to the prompt made the 20B model drop months it used to read and copy values from the examples. What fixed it was checking the model's answer against the message: a place counts only if its name, code or quoted words appear in the text; a region only if a region word does; months, "3 weeks" and "10 to 14 days" are also read from the text when the model misses them; and a refinement that names no date keeps the previous dates.

Requests the bot had to question or failed on are saved in the `misses` table. Admins list them with `/falhas`, and each one worth fixing becomes a case in `extract_eval.py`.

The Gemini free tier returned 503 and 429 errors during every test, so the Gemini path is untested.

## Limitations

- The bot finds fares and links to the booking page. It does not buy tickets.
- Split tickets are separate bookings. If the first flight is late, the second airline does not have to rebook you, and checked bags have to be collected and checked in again.
- Prices are converted to BRL. Paying in another currency adds IOF (about 3.5% on Brazilian cards) and the card's exchange spread.
- Cached prices can be up to 7 days old. The bot only alerts after a live search confirms the price.
- Everything runs on the machine where `bot.py` runs. If that is your laptop, it has to stay on and awake.
- Check the SerpApi and Travelpayouts terms before you offer this to other people or charge for it.

## Data stored

`flights.db` is a SQLite file next to the code:

- `users`: Telegram chat id, first name, plan and sign-up time
- `alerts`: route, dates, target price and the last prices seen
- `cache`: raw API responses for 6 hours, not linked to any user
- `misses`: text of requests the bot did not understand, with the chat id, for review (users are told on `/start`)
- `usage`: paid searches per month

Chat history with the LLM stays in memory and is lost when the bot restarts. Messages sent to a hosted model go through that provider's API.

## Files

| File | Role |
|---|---|
| `server.py` | data sources, caching, search tools, MCP server |
| `alerts.py` | `/alerta` parsing, storage and the periodic check |
| `llm.py` | tool loop for each LLM provider, fallback, `/comparar` and request extraction for `/buscar` |
| `search.py` | the fixed `/buscar` strategy: validation, date selection, live confirmation, reply |
| `extract_eval.py` | accuracy and speed check for request extraction |
| `bot.py` | Telegram polling, commands and message delivery |
