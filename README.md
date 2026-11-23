# IHS-Voice

An AI voice receptionist for Dr. Pushpak Samal's orthopedic clinic, built with Python, FastAPI, LangGraph and PostgreSQL. It answers real phone calls in Odia, Hindi and English (Plivo + Sarvam speech + OpenAI), places appointment-reminder calls, and gives the clinic staff a small dashboard to follow up.

## Features

**Inbound calls (patients phone the clinic)**
- Look up the caller by phone number (existing vs new patient) and verify existing patients by name and date of birth before sharing anything personal
- Natural conversation in Odia, Hindi or English, using doctor-approved clinic information only
- Rule-based emergency detection (no LLM) with transfer to the doctor
- Collects the problem, symptoms and a preferred appointment day and time (a request, never a booking)
- Offers to connect the receptionist; if nobody answers, the bot resumes and takes the request
- Saves callbacks, a per-call inquiry record, a summary and the recording link in PostgreSQL
- Alerts the doctor about urgent calls and new patients (currently logged; WhatsApp/SMS planned)

**Outbound reminder calls**
- Upload an Excel sheet of appointments; a scheduler calls each patient about 24 hours before the visit
- The patient confirms, asks to reschedule, or cancels; the result is saved
- Calling hours 10:00 to 19:00 IST, up to 3 attempts, retry after 30 minutes

**Staff dashboard** (`/dashboard/`)
- Follow-up list of callback requests with Call, Approve, Decline and Reopen buttons
- Inbound call history and reminder-call results, with a "Call now" button
- Protected by the admin key

> **Medical safety:** This agent is for receptionist and administrative tasks only. It must not diagnose conditions, prescribe medication, or say whether surgery is needed. Emergency rules and clinic answers must be approved by the doctor. Use synthetic patient data during development.

## How a call flows

```
START -> lookup_patient -> urgency_check -+-> escalate ---------> END    (emergency: callback + doctor alert + transfer)
                                          +-> verify_identity --> END    (existing patient, not verified yet)
                                          +-> assistant <-> tools -> END (normal conversation)
```

Phone path: `Plivo number -> POST /voice/plivo/answer -> WebSocket /voice/plivo/stream -> Sarvam speech-to-text -> LangGraph agent (OpenAI) -> Sarvam text-to-speech -> caller`.

## Requirements

- Python 3.11
- PostgreSQL 14+ (a local Docker Postgres, or the RDS database through an SSH tunnel)
- OpenAI API key
- Plivo account and number, Sarvam API key
- Docker (to build the image for deployment)
- A public HTTPS URL for Plivo's webhooks (ngrok locally, a domain in production)

## Local Setup (Windows PowerShell)

Run these commands from the project root:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
```

### 1. Database

Either start a temporary local PostgreSQL:

```powershell
docker run --name orthopedic-db -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=orthopedic_voice -p 5433:5432 -d postgres:16
```

or reach the RDS database through an SSH tunnel through the VM (keep the window open):

```powershell
ssh -N -L 5435:<rds-endpoint>:5432 prime-prod
```

Use the same local port in `DATABASE_URL`, and `127.0.0.1` (not `localhost`). A first start over the tunnel is slow (about 50 seconds); on the VM it takes a couple of seconds.

### 2. Configure `.env`

`.env.example` is a short template. The full list of settings, with defaults and comments, is in `app/config.py`. The important ones:

```dotenv
APP_ENV=development
DATABASE_URL=postgresql+psycopg://user:password@127.0.0.1:5433/orthopedic_voice
PUBLIC_BASE_URL=https://your-public-url        # no trailing slash

TELEPHONY_PROVIDER=plivo
PLIVO_AUTH_ID=
PLIVO_AUTH_TOKEN=
PLIVO_NUMBER=+91XXXXXXXXXX
PLIVO_VALIDATE_SIGNATURE=false                  # turn on after the first successful call

STT_PROVIDER=sarvam
TTS_PROVIDER=sarvam
SARVAM_API_KEY=
OPENAI_API_KEY=
OPENAI_MODEL=

DOCTOR_NAME=
ESCALATION_PHONE=+91XXXXXXXXXX                  # doctor, used for emergency transfer
RECEPTIONIST_PHONE=+91XXXXXXXXXX                # used for "connect me to the receptionist"
ADMIN_API_KEY=                                  # protects /appointments and /staff; empty = switched off

OUTBOUND_ENABLED=false                          # master switch for reminder calls
```

Rules for the `.env` file:
- Never commit it, and never put API keys, database passwords or tokens in git.
- Put comments on their own line. Docker's `--env-file` does not strip a trailing `# comment`, so `PLIVO_NUMBER=+91... # note` breaks the value.
- Each variable must appear once.

### 3. Create or update the tables (Alembic)

Alembic is the only way the schema changes. Run this once per database, and again after pulling a release that contains a new migration:

```powershell
python -m alembic upgrade head
python -m alembic current        # shows the latest revision, marked (head)
```

A database that was created by the old `create_all` and has no `alembic_version` table must be stamped once before upgrading:

```powershell
python -m alembic stamp 0ea121cd49b5
python -m alembic upgrade head
```

Always take a database snapshot before changing a live schema, and never run `alembic downgrade` on production. More detail is in `docs/` and in the migration guides kept with the deployment notes.

The LangGraph checkpoint tables are created automatically on startup.

### 4. Doctor-approved data files

The `data/` folder holds the content the bot may use. Edit these files, not the code:

| File | Purpose |
|---|---|
| `data/clinic_info.json` | Address, timings, fees, FAQs |
| `data/doctor_answers.json` | Approved answers to common medical-adjacent questions |
| `data/emergency_rules.json` | Emergency and urgent phrases (must be approved by the doctor) |
| `data/fixed_replies.json` | Exact lines the bot says (greeting, verification, refusals, errors) |

The app will not start if one of these files is missing or invalid. Fixed lines are turned into audio once at startup and cached in `.cache/tts` (a volume in Docker).

## Run the API

```powershell
python -m uvicorn app.main:app --reload --port 8000
```

Startup should log `✅ Connected to PostgreSQL`, `✅ Data files loaded`, `✅ Agent graph ready`, `✅ Reminder graph ready` and `✅ Speech: STT=sarvam | TTS=sarvam`. The reminder scheduler logs `calls are ON` or `calls are OFF`.

- API root: `/`
- Health check: `/health` (returns 503 if the database is down)
- Interactive API docs: `/docs`
- Staff dashboard: `/dashboard/`

## Test with a real phone call (ngrok)

1. Start the app, then `ngrok http 8000`.
2. Put the ngrok URL in `PUBLIC_BASE_URL` (no trailing slash) and restart the app. The free ngrok URL changes every time ngrok restarts.
3. In the Plivo console, set the application's Answer URL to `<PUBLIC_BASE_URL>/voice/plivo/answer` (method POST) and attach your number to it.
4. Call the number and watch the log: `Incoming call`, `WebSocket ... [accepted]`, the greeting, then each caller and bot turn.

Outbound calls do not use the Plivo application. Their answer and hangup URLs are sent with each call by the code.

## Outbound reminder calls

1. Make a test sheet, or use your own (columns `name`, `phone`, `date`, `time`; optional `language` and `consent`):

   ```powershell
   python app\scripts\make_demo_sheet.py
   ```

2. Upload it with the admin key:

   ```powershell
   $key = ((Select-String -Path .env -Pattern '^ADMIN_API_KEY=').Line -split '=',2)[1].Trim()
   curl.exe -X POST -H "X-API-Key: $key" -F "file=@demo_data\appointments_demo.xlsx" <PUBLIC_BASE_URL>/appointments/import
   ```

   Rows with an invalid phone, date or time, a past appointment, `consent` set to no, or a row already imported are skipped, and the reply says why.

3. List the saved rows and note an `id`:

   ```powershell
   curl.exe -H "X-API-Key: $key" <PUBLIC_BASE_URL>/appointments
   ```

4. Trigger one call right now (ignores the calling hours, the 24-hour window and the scheduler switch):

   ```powershell
   curl.exe -X POST -H "X-API-Key: $key" <PUBLIC_BASE_URL>/appointments/<ID>/call-now
   ```

With `OUTBOUND_ENABLED=true` the scheduler runs every 5 minutes and calls patients whose appointment is between 2 and 24 hours away, from 10:00 to 19:00 IST. Status flow: `pending -> calling -> confirmed | reschedule | cancelled | unconfirmed`, or back to `pending` for a retry and finally `no_answer`.

**Warning:** every app that shares a database also shares its appointments. If two deployments both have `OUTBOUND_ENABLED=true`, either can place the call. Give each deployment its own database, or keep the switch on in only one.

## Staff dashboard

Open `/dashboard/` and sign in with the `ADMIN_API_KEY`. The server address is filled in automatically when the page is served by the app.

| Tab | Shows |
|---|---|
| To follow up | Callback requests, urgent first, with the caller's words, preferred slot, summary and recording link. Buttons: Call patient, Approve, Decline, Reopen |
| Inbound calls | Recent calls with what the caller wanted and how each call ended |
| Reminder calls | Reminder appointments and results, with a Call now button |

Approve and Decline only change `callbacks.status` (`pending`, `approved`, `declined`). Nothing is deleted.

Endpoints behind it (all need the header `X-API-Key`):

| Endpoint | Purpose |
|---|---|
| `GET /staff/overview` | Counts for the summary cards |
| `GET /staff/callbacks?status=` | The follow-up list |
| `POST /staff/callbacks/{id}/status` | Set `pending`, `approved` or `declined` |
| `GET /staff/inquiries` | Recent inbound calls |
| `POST /appointments/import` | Upload the Excel sheet |
| `GET /appointments` | List reminder appointments |
| `POST /appointments/{id}/call-now` | Place a reminder call now |

## Test the agent in chat (no phone needed)

Use Swagger at `/docs`:

1. `POST /chat/start` with `{ "phone": "+919999900001" }`, then copy the `call_id`.
2. `POST /chat/message` with `call_id`, `phone` and `text`. Repeat for each caller message.
3. `POST /chat/end` with `call_id` to save the summary and alert the doctor if needed.

Scenarios to try:

| Scenario | What to send | Expected |
|---|---|---|
| Existing patient | Ask about your knee, then give name and date of birth | Bot asks for name and DOB, then `verified: true` |
| Medical advice | "Do I need surgery?" | Bot declines and saves a callback |
| Emergency | "I fell down and I cant walk" | `action: transfer`, `urgency: emergency` |
| New patient | Use a number not in the database | Bot collects name, DOB and issue, then saves a callback |
| Wrong details | Wrong DOB twice | Bot stops verifying and offers a callback |

## Tests

Offline tests need no phone, database or API keys. Each prints one `PASS` or `FAIL` line per check:

```powershell
python tests\test_booking_flow_offline.py
python tests\test_session_ending_offline.py
python tests\test_dial_status_offline.py
python tests\test_doctor_answers_offline.py
python tests\test_filler_offline.py
```

Run them before every deployment. A real test call is still needed to check the audio.

## Deployment (Docker image on an ARM VM)

The VMs are `linux/arm64`, so images must be built for ARM, otherwise the container fails with `exec format error`.

```powershell
# laptop: build and push
docker login -u primemedisysdocker
docker buildx build --platform linux/arm64 -t primemedisysdocker/ihs:dev-voice -t primemedisysdocker/ihs:dev-voice-vN --push .
```

```bash
# VM: pull, migrate, run
docker pull primemedisysdocker/ihs:dev-voice
docker run --rm --env-file /ihs/dev-voice/.env primemedisysdocker/ihs:dev-voice alembic upgrade head
docker rm -f ihs-dev-voice
docker run -d --name ihs-dev-voice --restart unless-stopped \
  -p 127.0.0.1:8000:8000 --env-file /ihs/dev-voice/.env \
  -v /ihs/dev-voice/tts-cache:/app/.cache \
  --memory 700m --memory-swap 1400m \
  primemedisysdocker/ihs:dev-voice
```

| Item | Value |
|---|---|
| Image | `primemedisysdocker/ihs:dev-voice` (keep a versioned tag such as `dev-voice-v2` for rollback) |
| Environment file on the VM | `/ihs/dev-voice/.env` (read when the container is created, so recreate the container after editing it) |
| Container port | 8000, published on `127.0.0.1` only |
| nginx | A separate site file in `/etc/nginx/sites-available/`, with the WebSocket upgrade headers and `proxy_read_timeout 3600s`. The default 60 seconds would end every call after a minute |
| HTTPS | `sudo certbot --nginx -d <domain>` |
| TTS cache | `/ihs/dev-voice/tts-cache` (otherwise the fixed lines are regenerated on every start) |
| Rollback | Run the same `docker run` with the previous version tag |

The `.env` is never baked into the image (`.dockerignore` excludes it). `docker-compose.yaml` in the project builds and runs the app locally (`docker compose up -d --build`); the VM uses `docker run` as shown above.

After deploying, check `/health`, make one test call, and look at `docker logs -f ihs-dev-voice`.

## Project Structure

```text
app/
├── agent/          # LangGraph graphs: identity, intake, booking, reminders, shared rules, prompts, tools
├── models/         # SQLAlchemy models (patients, call logs, callbacks, call summaries, appointments, inbound inquiries)
├── routes/v1/      # chat.py (text testing), appointments.py (admin), staff.py (dashboard API),
│                   #   inbound/ and outbound/ (Plivo and Twilio webhooks)
├── services/       # inbound, outbound (scheduler, calls) and shared business logic
├── speech/         # Speech-to-text and text-to-speech providers (Sarvam, Bodhan) and the audio cache
├── telephony/      # Phone provider adapters (Plivo, Twilio)
├── voice/          # Live call session: audio stream, barge-in, silence, transfer, hang-up, wrap-up
├── scripts/        # Development scripts (demo sheet, test patients, simulate a call, voice tests)
├── utils/          # Shared helpers
├── config.py       # Application settings (.env)
├── database.py     # asyncpg pool + SQLAlchemy session
└── main.py         # FastAPI entry point, startup checks, health check

dashboard/          # Staff dashboard (single HTML page, served at /dashboard/)
data/               # Doctor-approved JSON content (clinic info, doctor answers, emergency rules, fixed replies)
demo_data/          # Sample Excel sheet for reminder calls
docs/               # Project guides
migrations/         # Alembic migrations (the only way the schema changes)
tests/              # Offline tests
.env                # Local secrets (do not commit)
.env.example        # Environment-variable template
alembic.ini         # Alembic config (the database URL comes from .env, not from this file)
requirements.txt    # Python dependencies
Dockerfile
docker-compose.yaml
```

## Development Notes

- Emergency detection is plain Python in `app/agent/shared/rules.py`. The LLM never decides emergencies alone.
- Spoken replies must not contain emojis. Emojis are used only in logs, doctor alerts and API messages.
- A database or LLM failure must never crash a live call. The caller hears an approved fallback line.
- Do not claim an appointment is booked. A preferred day and time is only a request, saved in `inbound_inquiries.preferred_*` and `callbacks.preferred_slot`. The `appointments` table is for reminder appointments only.
- The agent must not promise a receptionist transfer or a callback unless it has been saved.
- New database columns must be nullable or have a default, because production tables already hold rows.
- `LOG_TRANSCRIPTS=true` writes what callers say into the logs. Use it for testing only and switch it off in production.
- Test urgent-call routing and fallback behavior before connecting a real clinic phone number.

## Roadmap

- [x] Phase 1: text agent with verification, emergency rules, callbacks and summaries
- [x] Phase 2: Plivo inbound calls, receptionist transfer with fallback, call recording, per-call inquiry records
- [x] Phase 3: natural streaming voice (Sarvam speech-to-text and text-to-speech, LLM) in Odia, Hindi and English
- [x] Outbound appointment-reminder calls from an Excel sheet
- [x] Staff dashboard for follow-up (approve / decline)
- [x] Alembic migrations and Docker deployment on an ARM VM behind nginx and HTTPS
- [ ] Doctor approval of `data/emergency_rules.json` and the clinic data files
- [ ] Plivo signature validation switched on
- [ ] Per-user login for the dashboard (today it is a single shared admin key)
- [ ] WhatsApp/SMS doctor alerts, PDF summaries
- [ ] Data retention policy and recording storage

## Status

Running on an AWS VM with real phone calls (inbound and outbound). The items above marked open are still to do before a full clinic rollout.

