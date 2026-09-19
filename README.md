# FULIFENG AUTO Telegram V1

FastAPI + python-telegram-bot, SQLAlchemy persistence, APScheduler, Russian OpenAI content with offline fallback. Run one process and one Railway replica.

## Railway

Connect this repository's `main` branch to `telegram-bot-v1`. Dockerfile supplies the build; listen on Railway `PORT` (default 8080). Set healthcheck /health, timeout 120, one replica and disable service sleeping in Railway service settings. The requested railway.toml is included for legacy compatibility; Railway's current API no longer permits opting new services into this deprecated format, so deployment does not rely on it.

1. Attach a persistent volume at `/app/data` and set `DATABASE_URL=sqlite:////app/data/bot.db`, or provide a PostgreSQL DATABASE_URL. Do not use ephemeral SQLite for production. PostgreSQL URLs are normalized to psycopg.
2. Privately set `BOT_TOKEN` and `ADMIN_USER_IDS` (comma-separated numeric personal Telegram IDs) in Railway Variables. Never put secrets in GitHub or chat. Optional: `OPENAI_API_KEY`, `OPENAI_MODEL` (default gpt-4.1-mini). No key or API error uses Russian template fallback.
3. Set `TZ=Europe/Moscow`: daily 09:00, 13:00, 18:00, 21:00.
4. Deploy; inspect logs and `/health`. Missing bot credentials allows bootstrap HTTP 200 with `bot=not_configured`, `ready=false`; this is NOT an operational bot. Working polling reports `bot=running`, `ready=true`, `scheduler=true`. Database failure or bot runtime failure returns 503.
5. With BOT_TOKEN configured, /help shows your personal numeric ID even before ADMIN_USER_IDS is set. Add that ID to Variables. Add the bot as an administrator of the target channel/group with posting rights. In private admin chat: `/setchat @channel`, then `/resume`. Initial state is paused and persists across restarts.

On Railway, SQLite bot operation is blocked until Railway reports a volume mounted at /app/data. Health then says needs_persistent_volume and ready=false. Do not manually spoof RAILWAY_VOLUME_MOUNT_PATH; it is supplied by Railway when storage is attached.

Existing webhooks are not deleted automatically. Disable a previous webhook before using polling. Do not run another service with the same bot token. Keep a single replica; deployment overlap can briefly cause polling conflicts.

## Commands

| Command | Behavior |
| --- | --- |
| /start | Russian welcome; clears caller's prior opt-out |
| /help | Help and caller's Telegram numeric ID |
| /today | Admin: schedule, target, pause state |
| /stock | Inventory; admin `/stock set TEXT` updates it |
| /price | Price information; admin `/price set TEXT` updates it |
| /post | Admin: private preview |
| /post send | Admin: publish once to target; requires resumed state |
| /setchat @channel | Admin: validate posting permissions and save target |
| /pause | Admin: stop publishing and persist state |
| /resume | Admin: enable schedule after target setup |
| /stats | Admin: lead grades, sent and uncertain publication counts |

Admin commands require an ID in ADMIN_USER_IDS and private chat. No first-user-admin shortcut. Until real inventory/prices are supplied, fallback honestly says they need confirmation. OpenAI receives only catalog facts, never lead messages or identities. Review content with /post before enabling the schedule.

## Lead scoring

Private incoming non-command messages: purchase language 30, budget 25, timing 20, logistics 15, automotive/model mentions 10. Grades: A+ >=85, A >=65, B >=40, C >=15, D <15. These transparent V1 heuristics in app/scoring.py are not a validated sales prediction model.

Opt-out always means D and persists until /start. **D is recorded only, with no reply, notification or proactive outreach.** A+/A/B/C receive an acknowledgement to their incoming private inquiry. V1 does not scrape groups or proactively message strangers. Records contain Telegram ID, username, latest message (max 4000 characters), score, reasons and timestamp. Protect the database and manage retention for your business.

Unique database date/slot keys prevent duplicate scheduled posts. Manual sends use Telegram update IDs. Ambiguous sends are marked `uncertain` and never automatically retried. A crash between sending and DB acknowledgement can leave `pending`; check the channel before manually retrying. Downtime schedules are not replayed. /pause waits for an in-flight send to finish before acknowledging.

## Development and testing

Python 3.12+. Create/activate a virtual environment, then:

```sh
pip install -r requirements-dev.txt
uvicorn app.main:app --host 127.0.0.1 --port 8080
python -m pytest -q
```

.env.example documents variables; set them in the process environment (.env is not auto-loaded). Tables are created on startup; future schema changes require migrations. Back up storage before upgrades.

Tests cover all eleven handlers, admin denial, lead tiers/opt-out, D silence, persistence, duplicate prevention, uncertain sends, offline/API-error fallback, Moscow scheduling, and bootstrap health. Telegram transport is mocked, not a live test.

Live acceptance: /start /help /stock /price, then admin /today /setchat /resume /post /post send /stats /pause. Confirm exactly one test post in the intended channel and health ready=true. Resume only when target and content are ready.

References: [OpenAI text generation](https://developers.openai.com/api/docs/guides/text), [python-telegram-bot](https://docs.python-telegram-bot.org/en/stable/telegram.ext.application.html), [APScheduler](https://apscheduler.readthedocs.io/en/3.x/modules/schedulers/asyncio.html).
