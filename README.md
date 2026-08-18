# Cinagi WhatsApp Platform

A multi-workspace WhatsApp business messaging platform. Each **workspace** is one WhatsApp number
with its own credentials, team, saved replies, templates, working hours and chats. Nothing crosses
between workspaces.

Built on Django 6, PostgreSQL, Celery, Django Channels, HTMX, Alpine and Tailwind. Deploys with
Docker to a Linux host.

---

## Getting started (about ten minutes)

```bash
cp .env.example .env          # then edit it - at minimum set DJANGO_SECRET_KEY
docker compose up --build     # starts Postgres, Redis, the app, two workers and beat
```

Then, in a second terminal:

```bash
docker compose exec web python manage.py migrate
docker compose exec web python manage.py createsuperuser
docker compose exec web python manage.py seed_schedules
```

Open <http://localhost:8000> and sign in. You will be asked to create a workspace, then to connect
a WhatsApp number.

**Nothing can be sent to a real customer yet.** `OUTBOUND_COMMS_MODE` defaults to `suppress` and a
banner across the top of the app says so. Set it to `allowlist` (with `OUTBOUND_ALLOWLIST=27821234567`)
while testing, and only to `live` in production.

### Without Docker, on Windows, against a local PostgreSQL

You need **Python 3.13** (Django 6 requires 3.12+) and a PostgreSQL database. No Redis needed:
set `USE_REDIS=False` and background jobs run inline, live updates use an in-process channel layer
and the cache is local memory.

```powershell
cd "C:\Projects\Cinagi WhatsApp"
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# .env should have: DB_HOST=localhost  DB_USER=postgres  DB_NAME=cinagi_wa
#                   DB_PASSWORD=<your password>  USE_REDIS=False
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

Then open <http://localhost:8000>.

With `USE_REDIS=False` you do **not** run the Celery worker or beat - `seed_schedules` is only
needed once Redis is in play. Everything else behaves normally.

### Without Docker, quickest possible (sqlite)

```bash
export DB_ENGINE=sqlite
python manage.py migrate && python manage.py createsuperuser && python manage.py runserver
```

---

## Connecting a WhatsApp number

You need these four values from Meta. The app's connect screen repeats these instructions.

| What | Where to find it |
|---|---|
| Phone number ID | WhatsApp Manager → your number → API Setup |
| WhatsApp Business Account ID | Same page (needed to load approved templates) |
| Access token | Business Settings → System users → Generate token (permanent, with `whatsapp_business_messaging` and `whatsapp_business_management`) |
| App secret | App Dashboard → Settings → Basic |

Then, in Meta's app dashboard under **WhatsApp → Configuration**, set:

- **Callback URL** — `https://your-domain/wa/webhook/`
- **Verify token** — shown on the number's check screen in this app
- Subscribe to the **messages** field.

The check screen confirms the credentials work, shows whether messages are arriving, and can send
you a real test message.

Credentials are stored **encrypted in the database**, per workspace — never in `.env`. That is what
lets one deployment serve many numbers.

---

## Testing

```bash
./scripts/test.sh              # 38 tests, sqlite, no Redis, no network
python scripts/smoke.py        # renders every page against a throwaway database
```

The isolation suite (`tests/test_workspace_isolation.py`) proves one workspace cannot see or touch
another's data. If it ever fails, fix that before anything else ships.

---

## What is built (phases 0, 1 and the inbox)

- Workspaces, roles (owner / admin / supervisor / agent / viewer), workspace switcher
- Per-workspace WhatsApp credentials, encrypted at rest, with a guided connect-and-verify wizard
- Meta Cloud API transport: text, media, templates, buttons, lists, media upload and download
- Webhook receiver: signature verification, instant 200, Celery dispatch, duplicate protection
- Agent inbox: three panes, live updates over websockets with an 8-second poll fallback, delivery
  and read ticks, attachments, internal notes, claim / hand back / resolve
- 24-hour window enforced in the composer, with approved templates offered when it closes
- Quick replies (shared and personal) with `/` search, and read-only template sync from Meta
- Working hours, holidays, after-hours auto-reply
- Agent availability, capacity, and automatic allocation (sticky agent, then least busy)
- One audit log for every significant action

## What is next

Phase 4 (allocation polish, SLA, escalation), phase 5 (visual automation builder), phase 6 (AI
resolver), phase 7 (reporting and hardening), phase 8 (Xealth/BIMS). See the technical plan in the
Claude project for the full roadmap.

---

## Deployment

```bash
git pull
docker compose -f docker-compose.prod.yml up -d --build
```

nginx re-resolves the app container at request time (`resolver 127.0.0.11`), so a redeploy does not
leave it pointing at a dead IP.
