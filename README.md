# Smart Solution Bot

Smart Solution is a Telegram bot that helps competition organisers manage users, teams, tracks and submissions in a single place. The bot is built on top of **aiogram 3** and uses an asynchronous PostgreSQL backend accessed through SQLAlchemy.

## Key Features

- Interactive Telegram interface for admins and contestants.
- Multilingual localisation (English/Russian) with dynamic keyboards.
- Team and competition management, including leaderboards and submission limits.
- Automated judging pipeline with pluggable scorers per track.
- Persistent action registry with automatic serialisation.
- Rich middleware stack: rate limiting, whitelisting, per-user context, etc.

## Getting Started

### Prerequisites

- Python 3.11+
- PostgreSQL database (URL configured via `DATABASE_URL` or `.env`)
- Telegram bot token (`BOT_TOKEN`)
- Docker (required for the sample auto-judge)
- Optional: local telegram-bot-api instance for large file uploads (see below)

### Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and fill in the values:

```bash
cp .env.example .env
```

Environment variables overview:

- `DATABASE_URL` — async SQLAlchemy DSN (PostgreSQL).
- `DEFAULT_LANGUAGE` — language name present in the `language` table.
- `WHITELIST` — comma-separated Telegram usernames allowed to interact with the bot (optional).
- `BOT_TOKEN` — Telegram bot token from BotFather.
- `MAX_REQUESTS`, `BAN_THRESHOLD`, `BAN_DURATION_SECONDS`, `PERIOD` — rate limiting configuration.

Ensure the database already contains a `language` table with at least two rows
for English and Russian (columns: `id` UUID, `name` varchar, `title` varchar).
Insert them manually, choosing your own UUIDs:

```sql
INSERT INTO language (id, name, title) VALUES
  ('<english-uuid>', 'english', 'English 🇬🇧'),
  ('<russian-uuid>', 'russian', 'Русский 🇷🇺')
ON CONFLICT (id) DO NOTHING;
```

These records are required for localisation lookups during bot start-up. Update
`.env` (`DEFAULT_LANGUAGE`) to match the language name you configured.

Initialise the database schema (runs automatically on bot start-up):

```bash
python -m smart_solution.bot.run_bot --help  # optional sanity check
```

### Running the Bot

```bash
python -m smart_solution.bot.run_bot
```

The dispatcher automatically loads all routers and middlewares. Auto-judging services register during import, so ensure the relevant modules remain importable (`bot/services/auto_judge_first_track.py`).

If you plan to download large files, run your own `telegram-bot-api` gateway. Helpful setup guides:

- [Quick start on Habrahabr (Russian)](https://habr.com/ru/articles/840982/)
- [Official build instructions](https://tdlib.github.io/telegram-bot-api/build.html)

## Auto-Judging Pipeline

The auto-judge service is a singleton registry (`bot/services/auto_judge.py`) that maps track slugs to asynchronous scorers. New scorers are implemented as standalone modules and imported in `bot/run_bot.py`. Each scorer receives:

- Absolute archive path
- Submission metadata (`SubmissionRead`)
- Team metadata
- Track metadata

Scorers return an `AutoJudgeResult` that encapsulates status, numeric value, and optional message. The result is persisted and temporarily cached so the contestant receives immediate feedback via `SubmissionNotificationService`.

The sample scorer for `test_competition` / `first_track` is documented in [`data/auto_judge/test_competition/first_track/README.md`](data/auto_judge/test_competition/first_track/README.md).

## Development Notes

- All services are implemented as lightweight singletons; avoid manual instantiation unless necessary.
- Use `SubmissionService.create_submission` for contestant uploads—auto-judging and notifications are wired in through decorators.
- Localisation files live in `data/locales/<language>/*.json` and are resolved through `Localizer`.
- Keep router modules side-effect free; import-time registration should be idempotent.

## Repository Structure

```
├─ bot/
│  ├─ routers/        # Aiogram routers (core flows, submissions, team mode, etc.)
│  ├─ services/       # Domain services (users, teams, auto-judge, notifications)
│  ├─ keyboards/      # Reply / inline keyboard factories
│  └─ run_bot.py      # Entry point that wires middlewares and routers
├─ data/
│  ├─ auto_judge/     # Artefacts for automatic scoring (input data, README)
│  ├─ locales/        # Localisation packs per language
│  └─ submissions/    # Stored contestant submissions (generated at runtime)
├─ db/                # Async SQLAlchemy database layer and Pydantic schemas
├─ i18n.py            # Localisation helper used across the bot
└─ requirements.txt
```

## License

This project is distributed under the MIT license. See `LICENSE` for details.
