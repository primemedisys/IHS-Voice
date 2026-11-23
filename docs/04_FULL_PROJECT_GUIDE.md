# Orthopedic AI Voice Agent — Complete Project Guide

> **What this document is:** a full, single-file explanation of the project: what it does, every
> folder and file, how an **inbound** phone call and an **outbound reminder** call actually flow
> through the system, how Plivo and Sarvam are wired in, how the AI agents make decisions, how the
> appointment sheet drives reminder calls, how to set it up from zero, and the known problems.
>
> Written for a developer who is new to the codebase. Read top to bottom the first time; use the
> section list as a reference afterwards.
>
> Code state: commit `46223a8` plus the current working-tree changes, which add **outbound
> appointment reminder calls** and finish the **Amazon RDS** migration.
> Last updated: 19 November 2026.
>
> **Where this project runs today.** It is live, not a prototype: an AWS EC2 instance in
> Sydney (`ap-southeast-2`) runs the FastAPI app as one Docker container behind Nginx with a
> Let's Encrypt certificate, and it answers real Plivo calls on
> `voice.indiahealthsystem.com`. PostgreSQL now lives in **Amazon RDS**, not in a container on
> the app server: `docker-compose.yaml` no longer starts a `db` service (section 18).
> The deployment itself is described in section 17.
>
> **Two call directions.** (1) **Inbound**: a patient dials the clinic number and the AI
> receptionist answers. (2) **Outbound**: a member of staff uploads an Excel appointment sheet to
> `/appointments/import`, and a background scheduler calls each patient about 24 hours before the
> visit to remind them, ask how they feel, and record whether they will come. Both directions share
> the same speech pipeline and the same database.

---

## Table of contents

1. [What this project is](#1-what-this-project-is)
2. [What the bot can and cannot do](#2-what-the-bot-can-and-cannot-do)
3. [Technology stack](#3-technology-stack)
4. [Project structure](#4-project-structure)
5. [File-by-file reference](#5-file-by-file-reference)
6. [End-to-end data flow](#6-end-to-end-data-flow)
7. [How Plivo is used](#7-how-plivo-is-used)
8. [How Sarvam is used (and Bodhan)](#8-how-sarvam-is-used-and-bodhan)
9. [How the LangGraph agent works](#9-how-the-langgraph-agent-works)
10. [Data files](#10-data-files)
11. [Database](#11-database)
12. [Configuration reference](#12-configuration-reference)
13. [Setup from zero](#13-setup-from-zero)
14. [Running and testing](#14-running-and-testing)
15. [Troubleshooting](#15-troubleshooting)
16. [Known issues and TODO](#16-known-issues-and-todo)
17. [Production deployment on AWS](#17-production-deployment-on-aws)
18. [The Amazon RDS database (migration completed)](#18-the-amazon-rds-database-migration-completed)
19. [Handover checklist](#19-handover-checklist)
20. [Glossary](#20-glossary)

---

## 1. What this project is

An **AI phone receptionist** for an orthopedic clinic (Dr. Pushpak Samal, Bhubaneswar).

A patient calls the clinic's phone number. A Plivo number receives the call, streams the live audio to
this FastAPI server over a WebSocket, and the server:

1. listens to the caller (voice activity detection),
2. turns speech into text (Sarvam speech-to-text),
3. thinks with a LangGraph agent backed by OpenAI,
4. turns the reply back into speech (Sarvam text-to-speech),
5. plays that audio back into the phone call.

Along the way it looks the caller up in PostgreSQL, verifies existing patients, detects emergencies
with plain rules (never the LLM), saves callback requests, transfers true emergencies to the doctor,
and writes a short summary when the call ends.

The same server also makes **outbound reminder calls**:

1. staff upload an Excel sheet of appointments (`name, phone, date, time, language, consent`),
2. the rows become `appointments` rows in PostgreSQL,
3. a scheduler (APScheduler, started in `main.py`) picks the visits that are ~24 hours away and
   places a Plivo call to each patient during calling hours (10:00–19:00 IST),
4. a **separate, smaller LangGraph** runs the reminder conversation: confirm the appointment, ask
   how the patient feels, record `confirmed / reschedule / cancel / wrong_person`, say goodbye and
   hang up,
5. the call is recorded (Plivo recording API) and the outcome, the patient's words and the recording
   URL are stored on the appointment row.

There is also a **text-only chat API** (`/chat/*`) that drives the inbound agent without any phone,
and a **staff API** (`/appointments/*`, protected by `X-API-Key`) for Excel import, listing and
"call this patient now".

Two important properties of the design:

- **The phone provider is swappable.** Routes and the call session talk to an abstract
  `TelephonyAdapter`, never to Plivo or Twilio directly. Plivo and Twilio implementations both exist.
- **The speech provider is swappable.** Everything calls `app/speech/factory.py`, which chooses Sarvam
  or Bodhan based on `.env`.

---

## 2. What the bot can and cannot do

**It can:**

- greet the caller in Odia, Hindi or English and follow the caller's language,
- recognise an existing patient by phone number,
- verify an existing patient by full name + date of birth before sharing anything personal,
- answer clinic questions (address, timings, fees, procedure costs, FAQs) **only** from
  `data/clinic_info.json`,
- collect name, date of birth and the problem from a new caller,
- save a callback request for the doctor's team,
- detect an emergency with approved keyword rules and transfer the call to the doctor,
- write a short call summary and alert the doctor,
- **import an appointment sheet and call patients automatically ~24 hours before the visit**,
- **hold a short reminder conversation, note the outcome (coming / reschedule / cancel / wrong
  person) and how the patient feels, record the call, then hang up**,
- **place a reminder call immediately on demand** (`POST /appointments/{id}/call-now`).

**It must never:**

- diagnose a condition,
- suggest or prescribe medicine,
- say whether surgery is needed,
- give treatment advice,
- reveal another patient's information,
- claim an appointment is booked unless a real booking system confirmed it,
- **confirm a new appointment time during a reminder call** — it may only note the patient's
  preference and say the clinic team will call back.

These limits are enforced in three places: the system prompts (`app/agent/intake/prompts.py` and
`app/agent/reminders/graph.py`), the approved data files (`data/`), and the code paths that route every
unanswerable question to `request_callback`.

---

## 3. Technology stack

| Layer | Technology | Used for |
|---|---|---|
| Web framework | **FastAPI** + Uvicorn | HTTP webhooks, WebSocket audio, `/chat` and `/appointments` APIs |
| Agent orchestration | **LangGraph** (`StateGraph`, `MessagesState`, `ToolNode`) | inbound call logic **and** the outbound reminder conversation |
| Agent memory | **LangGraph Postgres checkpointer** (`PostgresSaver`) | call state survives a restart; one shared checkpointer for both graphs |
| LLM | **OpenAI** via `langchain-openai` (`ChatOpenAI`) | replies, identity extraction, summaries, reminder conversation |
| Speech-to-text | **Sarvam Saaras v3** (default) or **Bodhan indic-transcribe** | caller audio → text |
| Text-to-speech | **Sarvam Bulbul v3** (default) or **Bodhan indic-speak** | reply text → 8 kHz mu-law audio |
| Telephony | **Plivo** (default) or **Twilio** | phone number, webhooks, live audio stream, call transfer, outbound calls, recording |
| Database | **PostgreSQL 16 on Amazon RDS** | patients, call logs, callbacks, summaries, appointments, agent checkpoints |
| DB access | **SQLAlchemy 2** (sync) + **asyncpg** (health pool) + **psycopg 3** (checkpointer) | ORM, health check, LangGraph storage |
| Scheduling | **APScheduler** (`AsyncIOScheduler`) | reminder "cron": one pass every `REMINDER_POLL_MINUTES` |
| Excel | **openpyxl** | reading the staff appointment sheet |
| Audio | **audioop** | mu-law ⇄ PCM, 8 kHz ⇄ 16 kHz, RMS |
| Container runtime | **Docker + Docker Compose v2** | the app as one container on the server |
| Reverse proxy | **Nginx** | TLS termination, HTTP → container, WebSocket upgrade |
| TLS certificate | **Let's Encrypt (Certbot)** | free HTTPS for `voice.indiahealthsystem.com`, auto-renewed |
| Hosting | **AWS EC2 `t4g.small`** (Ubuntu 24.04, Arm64, Sydney) | the production machine |
| Tunnelling (dev only) | **ngrok** | exposes localhost:8000 to Plivo during local development; **not used in production** |

---

## 4. Project structure

```text
Orthopedic-AI-Voice-Agent/
│
├── app/                              # all application code
│   ├── main.py                       # FastAPI app, startup/shutdown, routers, / and /health
│   ├── config.py                     # Settings (pydantic-settings) loaded from .env
│   ├── database.py                   # asyncpg pool + SQLAlchemy engine + Base + get_session()
│   ├── __init__.py
│   │
│   ├── agent/                        # the "brains" (LangGraph)
│   │   ├── graph.py                  # INBOUND graph: lookup → urgency → verify/escalate/assistant
│   │   ├── reminder_graph.py         # OUTBOUND graph: reminder conversation + outcome tool
│   │   ├── nodes.py                  # inbound steps: lookup, urgency, verify, assistant, escalate
│   │   ├── state.py                  # CallState: the memory of one call (both graphs)
│   │   ├── tools.py                  # request_callback tool the LLM can call
│   │   ├── prompts.py                # builds the inbound system prompt for each turn
│   │   ├── rules.py                  # emergency/urgent keyword detection (no LLM)
│   │   ├── loader.py                 # loads + validates data/*.json, pick_reply(), language context
│   │   ├── llm.py                    # shared ChatOpenAI clients
│   │   └── __init__.py
│   │
│   ├── routes/
│   │   ├── v1/
│   │   │   ├── chat.py               # POST /chat/start, /chat/message, /chat/end
│   │   │   ├── voice_plivo.py        # POST /voice/plivo/answer, WS /stream, /stream-status,
│   │   │   │                         #      /transfer, /transfer-status
│   │   │   ├── voice_plivo_outbound.py # POST /voice/plivo/outbound/{answer,hangup,recording}
│   │   │   ├── voice_twilio.py       # same five inbound endpoints under /voice/twilio
│   │   │   ├── appointments.py       # staff API: /appointments/import, list, /{id}/call-now
│   │   │   ├── health.py             # legacy /health router (NOT mounted — see issues)
│   │   │   └── __init__.py
│   │   └── __init__.py
│   │
│   ├── voice/
│   │   ├── session.py                # CallSession: one live call, inbound OR reminder mode
│   │   └── __init__.py
│   │
│   ├── speech/                       # everything about audio in/out
│   │   ├── factory.py                # transcribe(), synthesize_ulaw(), prewarm, provider switch
│   │   ├── base.py                   # SpeechError, QuotaError, Transcript
│   │   ├── cache.py                  # disk+memory cache for fixed lines
│   │   ├── text_utils.py             # language detection/requests, sentence splitting
│   │   ├── audio_utils.py            # mu-law/PCM/WAV conversion + VoiceActivityDetector
│   │   ├── sarvam/
│   │   │   ├── client.py             # shared httpx client + 429 handling
│   │   │   ├── sarvam_stt.py         # Saaras speech-to-text
│   │   │   ├── sarvam_tts.py         # Bulbul text-to-speech → mu-law
│   │   │   └── __init__.py
│   │   └── bodhan/
│   │       ├── client.py             # shared httpx client + retry
│   │       ├── bodhan_stt.py         # indic-transcribe
│   │       ├── bodhan_tts.py         # indic-speak
│   │       └── __init__.py
│   │
│   ├── telephony/                    # provider abstraction
│   │   ├── base.py                   # TelephonyAdapter ABC + StreamEvent + audio format constants
│   │   ├── __init__.py               # get_adapter() factory
│   │   ├── plivo/
│   │   │   ├── plivo_adapter.py      # XML, signatures, REST transfer, outbound call, recording
│   │   │   └── __init__.py
│   │   └── twilio/
│   │       ├── twilio_adapter.py     # TwiML, signatures, REST transfer, stream parsing
│   │       ├── factory.py            # legacy factory (broken — see issues)
│   │       └── __init__.py
│   │
│   ├── services/                     # business logic, all DB writes live here
│   │   ├── call_service.py           # start_call, end_call, create_callback
│   │   ├── patient_service.py        # normalize_phone, find_by_phone, verify
│   │   ├── summary_service.py        # finish_call: LLM summary + save + alert
│   │   ├── notification_service.py   # notify_doctor (logs only, for now)
│   │   ├── appointment_service.py    # Excel import, due-list, outcomes, recordings, housekeeping
│   │   ├── outbound_service.py       # place one reminder call through Plivo
│   │   ├── reminder_scheduler.py     # APScheduler job every REMINDER_POLL_MINUTES
│   │   ├── clinic_service.py         # legacy/unused (broken — see issues)
│   │   └── __init__.py
│   │
│   ├── models/                       # SQLAlchemy tables
│   │   ├── patient.py                # patients
│   │   ├── call_logs.py              # call_logs
│   │   ├── callback.py               # callbacks
│   │   ├── call_summary.py           # call_summaries
│   │   ├── appointment.py            # appointments (outbound reminders)
│   │   └── __init__.py               # imports all models so create_all sees them
│   │
│   ├── scripts/                      # dev utilities
│   │   ├── simulate_call.py          # fake Plivo client: sends WAVs, saves bot audio
│   │   ├── test_sarvam.py            # compare Sarvam voices, save samples, STT round trip
│   │   ├── make_voices.py            # legacy (broken import)
│   │   ├── test_speech.py            # legacy (broken import)
│   │   ├── test_voices.py            # legacy (broken import)
│   │   └── __init__.py
│   │
│   └── docs/                         # documentation
│       ├── 01_PROJECT_GUIDE_SETUP_AND_STRUCTURE.md
│       ├── 02_ADD_NEW_TELEPHONY_PLATFORM.md
│       ├── 03_PROJECT_OVERVIEW_AND_WORKFLOW.md
│       ├── 04_FULL_PROJECT_GUIDE.md   # this file
│       └── 05_FULL_PROJECT_CODE.md    # every source file, in full
│
├── data/                             # doctor-approved content (edit these, not the code)
│   ├── clinic_info.json              # address, timings, fees, procedures, FAQs
│   ├── emergency_rules.json          # emergency + urgent phrases
│   └── fixed_replies.json            # every fixed spoken line, in en / hi / or
│
├── scripts/                          # root helper scripts (run from project root)
│   ├── make_demo_sheet.py            # builds demo_data/appointments_demo.xlsx
│   └── set_patient_name.py           # changes the name a reminder call says (DB + demo sheet)
│
├── demo_data/
│   └── appointments_demo.xlsx        # sample sheet for testing the outbound flow
│
├── migrations/                       # empty (Alembic not set up yet)
├── voice_bodhan_samples/             # generated Bodhan voice samples (WAV)
├── voice_sarvam_samples/             # generated Sarvam voice samples (WAV)
├── .cache/tts/                       # generated TTS cache (.ulaw files)
│
├── .env                              # real secrets (git-ignored)
├── .env.example                      # template (out of date — see issues)
├── .gitignore
├── requirements.txt                  # includes apscheduler + openpyxl now
├── Dockerfile                        # python:3.11-slim → uvicorn on 0.0.0.0:8000
├── docker-compose.yaml               # agent service only; database is on RDS (section 17)
├── .dockerignore                     # keeps .env, .venv, .cache, WAVs and docs out of the image
└── README.md
```

**Files that live on the server, not in the repository**

| Server path | Purpose |
|---|---|
| `/etc/nginx/sites-available/voice` | the Nginx site: TLS, HTTP proxy, WebSocket upgrade |
| `/opt/orthopedic/` | the deployed copy of this project |
| `/opt/orthopedic/.cache/` | TTS cache, now mounted into the container |
| Amazon RDS | the PostgreSQL 16 instance (no `db` container any more — section 18) |

The TTS cache **is now persisted**: `docker-compose.yaml` mounts `./.cache:/app/.cache`, so a rebuild
no longer re-synthesises the fixed lines.

See section 17 for the full deployment picture.

---

## 5. File-by-file reference

### 5.1 Root files

| File | What it does |
|---|---|
| `README.md` | Quick start, scenarios to test, roadmap. Still describes Phase 1 only (no reminders, no RDS) — out of date. |
| `requirements.txt` | Python dependencies; now also `alembic`, `apscheduler` and `openpyxl`. Still fully unpinned. |
| `Dockerfile` | `python:3.11-slim`, installs requirements, copies the project, runs uvicorn on 0.0.0.0:8000 with proxy headers. |
| `docker-compose.yaml` | **One service only**, `agent`, built from this directory, with `./.cache:/app/.cache` mounted. PostgreSQL is on RDS (section 18). See section 17.4. |
| `.dockerignore` | Keeps `.env`, `.venv`, `.cache`, WAVs, markdown and `app/docs/` out of the image (README is kept). A wrong line here breaks the build — see section 17.9. |
| `.env` | Real secrets and settings. Git-ignored. |
| `.env.example` | Template for `.env`. Still stale: no Sarvam keys, no `ADMIN_API_KEY`, no outbound/reminder settings. |
| `.gitignore` | Ignores `.venv/`, `.env`, `__pycache__/`, `.pytest_cache/`, `*.py[cod]`. Does **not** ignore `.cache/`, `demo_data/` or the `voice_*` folders. |

### 5.2 `app/` core

#### `app/main.py` — the entry point

Creates the FastAPI app and runs every startup check. If a check fails, the app refuses to start.

Startup (`lifespan`), in order:

1. `init_db()` — creates the asyncpg pool and verifies it with `SELECT 1`.
2. `check_db_connection()` — a second `SELECT 1` through SQLAlchemy.
3. `reminder_scheduler.start()` — starts the APScheduler job that places due reminder calls.
4. `Base.metadata.create_all(engine)` — creates any missing app tables (patients, call_logs,
   callbacks, call_summaries, appointments). This fixed the old "schema is never created" trap.
5. `validate_data_files()` — loads and validates the three JSON files.
6. Creates a `psycopg_pool.ConnectionPool` (max 10) and a LangGraph `PostgresSaver`, then
   `checkpointer.setup()` which creates the checkpoint tables on first run.
7. `build_graph(checkpointer)` → `app.state.graph`.
   `build_reminder_graph(checkpointer)` → `app.state.reminder_graph` (same checkpointer).
8. Warns about missing voice/speech settings (does not crash — chat must still work).
9. Starts `prewarm_fixed_lines()` as a background task (generates and caches the common spoken lines).

Shutdown stops the scheduler, closes the checkpoint pool, the speech HTTP clients, and the DB pool.

Also defines:

- `GET /` — service info + which provider is active + endpoint list.
- `GET /health` — checks the DB, returns 200 or 503.

Routers mounted: `chat`, `voice_plivo`, `voice_plivo_outbound`, `voice_twilio`, `appointments`
(both voice routers are always mounted).

#### `app/config.py` — settings

A single `Settings` class (pydantic-settings) reading `.env`, exposed as `settings`.

Groups of settings:

- **App:** `APP_ENV`, `PUBLIC_BASE_URL`, `DEFAULT_LANGUAGE`
- **Database:** `DATABASE_URL`
- **Staff API:** `ADMIN_API_KEY` (empty = `/appointments/*` answers 503)
- **Outbound calls:** `OUTBOUND_ENABLED`, `OUTBOUND_START_HOUR`, `OUTBOUND_END_HOUR`,
  `OUTBOUND_MAX_PER_RUN`, `OUTBOUND_MAX_CONCURRENT`, `OUTBOUND_MAX_ATTEMPTS`,
  `OUTBOUND_RETRY_MINUTES`
- **Reminder window:** `REMINDER_POLL_MINUTES`, `REMINDER_HOURS_BEFORE`, `REMINDER_MIN_LEAD_HOURS`
- **Telephony:** `TELEPHONY_PROVIDER`, Twilio keys, Plivo keys, signature flags, fallback `<Speak>` voice
- **Speech:** `STT_PROVIDER`, `TTS_PROVIDER`, Sarvam keys/model/speaker/pace, Bodhan keys/voice
- **Voice pipeline:** `VAD_*`, `BARGE_IN_MS`, `LOG_TRANSCRIPTS`
- **Latency:** `FILLER_*`, `REPLY_MAX_TOKENS`
- **AI:** `OPENAI_API_KEY`, `OPENAI_MODEL` (default `gpt-5.4-mini`)
- **Clinic:** `DOCTOR_NAME`, `ESCALATION_PHONE`, `BACKUP_PHONE`, `TRANSFER_RING_SECONDS`

Derived properties:

- `psycopg_url` — strips `+psycopg` so asyncpg/psycopg can use it.
- `sqlalchemy_url` — forces `postgresql+psycopg://` for SQLAlchemy.
- `stt_api_key` / `tts_api_key` — Bodhan's two separate keys.
- `bodhan_ready` — true when both Bodhan keys and a voice are set.
- `missing_voice_settings()` — names of empty settings, used for a startup warning.

> ⚠️ **Security:** the API keys now default to empty strings (good), but the database URL with its
> password and the doctor's phone number are still hardcoded as defaults in this file. See
> [Known issues](#16-known-issues-and-todo).

#### `app/database.py` — data access

Two connections to the same PostgreSQL (now the RDS instance):

1. **asyncpg pool** — `init_db()`, `get_db()`, `close_db()`. Used by `/health`.
2. **SQLAlchemy engine + `SessionLocal`** — used by models, services, and agent nodes.
   `get_session()` is a context manager that commits on success, rolls back and re-raises on error.

`Base` is the declarative base every model inherits from.

### 5.3 `app/agent/` — the brains

Both graphs consume the **same** `CallState` and share one checkpointer, so a call id is still the
memory key. Inbound and outbound calls never mix: the session picks the graph by mode.

#### `state.py`

`CallState(MessagesState)` — the memory of one call, saved after every turn by the checkpointer.

| Field | Meaning |
|---|---|
| `messages` | conversation history (from `MessagesState`) |
| `caller_phone` | E.164 number |
| `caller_type` | `"existing"` or `"new"` |
| `patient_id` | DB id of the matched patient, or `None` |
| `is_verified` | identity confirmed |
| `verify_attempts` | failed verification count |
| `given_name` / `given_dob` | partial identity captured across turns |
| `urgency` | highest level so far: routine / urgent / emergency |
| `turn_level` | level of **this** message only |
| `action` | `"transfer"` (call the doctor) or `"hangup"` (reminder finished — the phone layer closes the call) |

#### `graph.py`

Wires the nodes and defines routing:

```text
START → lookup_patient → urgency_check ─┬→ escalate ─────────────→ END
                                        ├→ verify_identity ──────→ END
                                        └→ assistant ⇄ tools → callback_reply → END
```

`route_after_urgency()` picks the branch:

1. `turn_level == "emergency"` → `escalate`
2. existing patient, not verified, attempts < 2 → `verify_identity`
3. otherwise → `assistant`

`assistant` uses `tools_condition`: if the LLM produced a tool call, go to `tools`; otherwise END.
After any tool, `callback_reply` produces a fixed confirmation line (saves an extra LLM round trip).

#### `nodes.py`

| Node | What it does |
|---|---|
| `lookup_patient` | Runs once per call. Normalises the phone, looks up the patient. On DB error, treats the caller as **new** so nothing private leaks. |
| `urgency_check` | Runs `classify()` on **every** message before the LLM sees it. On error, assumes `urgent`. Keeps the highest level seen. |
| `escalate` | Emergency path: loads the patient name if known, saves an emergency callback, alerts the doctor, returns the approved `emergency_transfer` line and `action="transfer"`. Always transfers, even if saving failed. |
| `verify_identity` | Asks for name + DOB. Uses an LLM with **structured output** (`Identity`) to extract them in any of the three languages. Merges across turns. Compares with `patient_service.verify()`. Stops after `MAX_VERIFY_ATTEMPTS = 2`. |
| `assistant` | Normal conversation. Builds the system prompt, trims history to the last 30 messages, binds the tool, calls the LLM. On failure, speaks the approved `error` line. |
| `callback_reply` | After a tool call, chooses a fixed line (`callback_saved`, `callback_failed`, `medical_advice_decline`) instead of asking the LLM again. |

#### `tools.py`

One tool: `request_callback(name, issue, urgency, config, date_of_birth="")`.
`config` is injected by LangGraph and is **not** shown to the LLM; it carries `thread_id` (the call id)
and `caller_phone`. The tool writes to `callbacks` and returns "Callback saved." or
"The callback could NOT be saved."

#### `prompts.py`

Builds the system prompt fresh every turn:

- **How to speak:** warm, 1–2 short sentences, one question at a time, natural contractions,
  acknowledge pain, vary wording, no markdown/emoji, say "I am an AI" if asked.
- **Language:** reply in the caller's language, in its own script; always the respectful "you"
  (ଆପଣ / आप).
- **Who is calling:** three variants (verified existing / unverified existing / new).
- **Urgent block:** if `urgency == "urgent"`, instructs the LLM to call `request_callback`.
- **Strict rules:** no diagnosis, no medicine, no surgery advice, answer only from approved data,
  never reveal other patients.
- **Approved clinic information:** `clinic_info.json` embedded as JSON (keys starting with `_` hidden).

#### `rules.py`

Emergency detection in plain Python. Normalises text (lowercase, strip apostrophes/punctuation),
then checks each approved phrase with a whole-word regex, emergency list first, then urgent.
No LLM involved — predictable and testable.

#### `loader.py`

- `DATA_DIR` points at `<project>/data`.
- `load_clinic()`, `load_rules()`, `load_replies()` — cached JSON loaders; raise a clear error if a
  file is missing or invalid.
- `REQUIRED_REPLIES` — the keys the code depends on. Missing ones fail startup.
- `set_reply_language()` / `current_language()` — a `ContextVar` holding the caller's language for the
  current turn. `pick_reply()` uses it to choose the right language, falling back to English.
- `pick_reply(key, lang=None)` — returns a random approved variant in the right language.
- `iter_variants()` — flattens string / list / dict-of-languages structures.

#### `llm.py`

Two cached `ChatOpenAI` clients:

- `get_llm()` — temperature 0.3, used for identity extraction and summaries.
- `get_agent_llm()` — temperature 0.5 + `max_tokens=REPLY_MAX_TOKENS`, intended for live replies.

Both use a 15 s timeout and 2 retries. `temperature` is omitted for `gpt-5*` models (they reject it).

> ⚠️ `nodes.assistant()` currently calls `get_llm()`, not `get_agent_llm()`, so the reply length cap
> is not applied.

#### `reminder_graph.py` — the outbound brain

A separate, smaller graph so the inbound flow is untouched:

```text
START → urgency_check ─┬→ escalate → END                (emergency words → transfer to the doctor)
                       └→ assistant ⇄ tools → END       (the reminder conversation)
```

| Piece | What it does |
|---|---|
| `reminder_greeting(lang, name)` | the opening line ("Am I speaking with {name}? This call may be recorded.") in en/hi/or. Spoken by the session **before** the graph runs. |
| `build_reminder_prompt(appointment)` | instructions: remind of the appointment time, ask if they can come, ask how they feel, then save a decision. Forbids medical advice and forbids confirming a new time. |
| `record_reminder_outcome(outcome, config, feeling, note)` | the tool the LLM calls. Writes `confirmed / reschedule / cancel / wrong_person` to the appointment; for reschedule/cancel it also creates a callback for the clinic team. |
| `reminder_escalate` | emergency words during a reminder call: save an emergency callback, alert the doctor, `action="transfer"`. |
| `reminder_assistant` | calls the LLM; when the outcome is saved **and** the reply is a real goodbye (no question), sets `action="hangup"`. Validation is in `_said_goodbye()`. |
| `build_reminder_graph(checkpointer)` | compiles the graph with the same checkpointer. |

The appointment id arrives in `config["configurable"]["appt_id"]`; the session provides it.
`REMINDER_TOOLS = [record_reminder_outcome, request_callback]`.

### 5.4 `app/routes/v1/`

#### `chat.py` — text testing

| Endpoint | Purpose |
|---|---|
| `POST /chat/start` `{phone}` | normalises the phone, creates a call id, logs the call, returns the greeting |
| `POST /chat/message` `{call_id, phone, text}` | invokes the graph once, returns reply + urgency + verified + action |
| `POST /chat/end` `{call_id}` | writes the summary and alerts the doctor if needed |

Uses `request.app.state.graph` — the exact same graph the inbound phone call uses.

#### `voice_plivo.py` — Plivo inbound endpoints

| Endpoint | Purpose |
|---|---|
| `POST /voice/plivo/answer` | Answer URL. Reads the form, remembers the caller's number, returns XML that opens a two-way audio stream. |
| `WS /voice/plivo/stream` | The live audio socket. Accepts, builds a `CallSession` with both graphs, copies the query string into `session.query_params` (reminder tags), then hands everything over. |
| `POST /voice/plivo/stream-status` | Plivo's stream events (start/stop/failure). Logged only. |
| `POST /voice/plivo/transfer?attempt=1` | XML that dials the doctor (attempt 1) or the backup (attempt 2). |
| `POST /voice/plivo/transfer-status?attempt=1` | Result of the dial: answered → hang up; not answered → try backup → then "doctor is busy" line + alert. |

Signature validation is applied to every POST when `PLIVO_VALIDATE_SIGNATURE=true`.

#### `voice_plivo_outbound.py` — Plivo outbound endpoints

| Endpoint | Purpose |
|---|---|
| `POST /voice/plivo/outbound/answer?appt=ID` | The patient picked up. Loads the appointment, stores the call UUID, returns the same `<Stream>` XML but with `mode=reminder&apptId=ID` in the WebSocket URL **and** in `extraHeaders`. Unknown appointment → hang up. |
| `POST /voice/plivo/outbound/hangup?appt=ID` | Plivo reports the end of the call. `CallStatus=completed` with duration > 0 counts as answered → `unconfirmed`; otherwise a retry or `no_answer`. A decision saved during the call is never overwritten. |
| `POST /voice/plivo/outbound/recording?appt=ID` | Plivo finished the recording; stores `RecordUrl` on the appointment. |

All three reuse `_verified_form()` from `voice_plivo.py`, so signature checking applies here too.

#### `appointments.py` — staff API

Prefix `/appointments`, tag `appointments`. **Every endpoint** depends on `_require_key`, which
checks the `X-API-Key` header against `settings.ADMIN_API_KEY` with `hmac.compare_digest`:

| Endpoint | Purpose |
|---|---|
| `POST /appointments/import` | multipart upload of the `.xlsx` sheet → `appointment_service.import_excel` (runs in a thread) |
| `GET /appointments?status=pending` | list appointments newest first (default limit 100) |
| `POST /appointments/{id}/call-now` | claim one appointment and place the reminder call immediately, ignoring hours/window (testing) |

If `ADMIN_API_KEY` is empty, every endpoint answers **503 "Admin API is off"**; a wrong key is **401**.

#### `voice_twilio.py` — Twilio endpoints

The same five inbound endpoints under `/voice/twilio`, using TwiML (`<Connect><Stream>`, `<Dial>`,
`<Say>`) and Twilio's `X-Twilio-Signature`. There is **no** Twilio outbound/reminder route yet.

> Both routers are mounted at all times, and each module instantiates its adapter at import time.
> That is why the `twilio` package must be installed even when running Plivo.

#### `health.py`

A legacy `/health` router. **Not mounted** in `main.py`; `/health` is defined directly in `main.py`.

### 5.5 `app/voice/session.py` — one live call

`CallSession` owns everything that happens on one WebSocket — **inbound or reminder**.

**Main loop:** read JSON messages → adapter translates them into neutral `StreamEvent`s → route by
kind (media / start / mark / dtmf / stop).

**On `start`:**
- store `call_id` and `stream_id`,
- verify the audio format is mu-law 8 kHz (close the socket with 1003 if not),
- recover the caller's phone from the stream (or from the number saved by `/answer`),
- **detect the mode**: `mode=reminder` + `apptId` may arrive in the stream headers or in the
  WebSocket query string. In reminder mode it loads the appointment, switches `self.graph` to the
  reminder graph, sets the phone and language from the appointment, queues the greeting and starts
  the recording in the background,
- `call_service.start_call()` in a thread,
- queue a greeting (`None` in the turn queue).

**On `media`:**
- convert mu-law → PCM,
- feed the `VoiceActivityDetector`,
- if the bot is speaking and the caller has spoken for `BARGE_IN_MS`, send `clearAudio` and bump the
  generation counter so any reply still being built is dropped,
- if the VAD says the sentence ended, queue the utterance.

**Turn worker (one at a time):**

1. `transcribe(utterance_to_wav(pcm))` → `Transcript(text, language)`; empty text → `not_understood`.
2. Update the call language (first clear sentence wins; a switch needs two in a row). Reminder calls
   keep the appointment language until the patient explicitly asks for another one
   (`text_utils.requested_language`).
3. `set_reply_language()` so prompts and fixed lines follow the caller.
4. `graph.invoke({"messages": [("user", text)], ...}, config={thread_id: call_id, caller_phone, appt_id?})`
   in a thread (LangGraph is sync).
5. Speak the last message: split into sentences, synthesise in parallel, play in order, then send a
   **marker** so we know when playback finished.
6. If `result["action"] == "transfer"`, wait for the marker, then call
   `adapter.transfer_call(call_id, PUBLIC_BASE_URL/voice/<provider>/transfer)`.
7. If `result["action"] == "hangup"` **and** the mode is `reminder`, estimate how long the goodbye
   still needs (2–6 s) and close the socket.

**Filler:** if a reply is slow (`FILLER_AFTER_MS`) and the caller said something long
(`FILLER_MIN_SPEECH_S`), a short cached acknowledgement plays so the line is never silent. Limited to
`FILLER_MAX_PER_CALL` and never two turns in a row.

**Reminder extras:** `_greet()` uses `reminder_greeting(...)` instead of the fixed greeting;
`_start_recording()` calls the Plivo record API with the recording callback URL (max 900 s, mp3).

**On close:** in reminder mode the session only logs (the outcome is already on the appointment);
otherwise `summary_service.finish_call()` runs in a thread.

**Audio out:** mu-law in 3200-byte chunks (= 400 ms at 8 kHz).

> Note: this file still contains commented-out copies of `_on_start`, `_greet`, `_handle_turn` and
> `_finish` above the live versions. Only the second, uncommented definition runs.
> See [Known issues](#16-known-issues-and-todo).

### 5.6 `app/speech/`

#### `factory.py` — the only speech entry point

- `transcribe(wav)` → `Transcript` using `STT_PROVIDER`.
- `synthesize_ulaw(text, lang=None)` → 8 kHz mu-law using `TTS_PROVIDER`; fixed lines hit the cache.
- `_limit()` — semaphore limiting concurrent TTS requests (`TTS_CONCURRENCY`).
- `_voice_id()` — cache key component: `model:speaker:pace` (Sarvam) or `voice:style` (Bodhan).
- `prewarm_fixed_lines()` — pre-generates the common lines, default language first, then or/hi/en;
  stops on a quota error or 3 consecutive failures.
- `missing_speech_settings()` — startup warning helper.
- `close_speech_clients()` — closes both HTTP clients on shutdown.

#### `base.py`

`SpeechError`, `QuotaError` (rate limit / credits), and the `Transcript` dataclass
(`text`, `language`).

#### `cache.py`

Disk + memory cache for **fixed lines only** (patient-specific text is never cached). Because of
that, the **reminder greeting** (built in `reminder_graph.py`, not listed in `fixed_replies.json`) is
never cached and costs one live TTS request before the first audio. File name = SHA-1 of
`[provider, voice, language, text]`, stored as `.cache/tts/<hash>.ulaw` (or `TTS_CACHE_DIR`).
`is_fixed(text)` checks the text against every piece of every fixed reply.

#### `text_utils.py`

- `detect_language(text)` — by script: Odia → `or`, Devanagari → `hi`, else `en`.
- `detect_caller_language(text)` — same idea for transcripts; returns `None` under 4 words.
- `requested_language(text)` — matches explicit requests ("speak in English", "ଓଡ଼ିଆ", "हिंदी") and
  returns `en` / `hi` / `or`. Reminder calls use it so a patient can switch language mid-call.
- `split_sentences(text)` — first sentence alone (plays sooner), everything else as one piece
  (wrapped at 400 characters); never splits after `Dr.`, `Mr.`, etc.

#### `audio_utils.py`

- `ulaw_to_pcm` / `pcm_to_wav` / `wav_to_ulaw_8k` — format conversions.
- `utterance_to_wav(pcm_8k)` — upsamples 8 kHz → 16 kHz and wraps in WAV for STT.
- `_read_wav` — tolerant WAV parser (handles placeholder header sizes from streaming APIs).
- `VoiceActivityDetector` — energy-based: feed 20 ms frames; returns a finished utterance when
  silence lasts `end_silence_ms` or the buffer hits `max_utterance_ms`. Keeps 200 ms of pre-roll so
  the first syllable is not lost. Drops utterances shorter than `min_speech_ms`.

#### `sarvam/`

- `client.py` — shared `httpx.AsyncClient` with `api-subscription-key`; retries once on HTTP 429 if
  the wait is ≤ 3 s.
- `sarvam_stt.py` — `POST /speech-to-text` with `saaras:v3`; sends `mode=codemix` and
  `language_code`; maps the returned code (`od-IN`, `hi-IN`, `en-IN`) to `or`/`hi`/`en`.
- `sarvam_tts.py` — `POST /text-to-speech` with `bulbul:v3`, the chosen speaker, pace, temperature,
  `speech_sample_rate=8000`, `output_audio_codec=wav`, preprocessing on; base64-decodes the audio and
  converts to mu-law.

#### `bodhan/`

The alternative provider, kept working but not active:

- `client.py` — shared client + 429 retry with backoff.
- `bodhan_stt.py` — `POST /audio/transcriptions` with `indic-transcribe`; returns plain text.
- `bodhan_tts.py` — `POST /audio/speech` with `indic-speak`; serialises requests with a semaphore and
  1 s spacing; caches fixed lines in memory.

### 5.7 `app/telephony/`

#### `base.py`

`TelephonyAdapter` — the contract every provider implements:

```text
stream_xml, dial_xml, speak_hangup_xml, hangup_xml, transfer_call,
validate_signature, parse_stream_event, audio_message, mark_message, clear_message,
make_call, start_recording
```

The last two are **not** abstract: they raise `NotImplementedError`, so a provider that does not
support outbound calling still satisfies the interface.

Plus `StreamEvent` (provider-neutral event: kind, call_id, stream_id, caller_phone, audio, name,
format_ok, **extra**) and the audio constants (`audio/x-mulaw`, 8000 Hz). `is_expected_format()` now
accepts `mulaw`, `ulaw` or `pcmu` and tolerates a missing sample-rate field.

#### `plivo/plivo_adapter.py`

- `stream_xml(ws_url, status_url, caller_phone, extra=None)` — `<Stream bidirectional keepCallAlive
  contentType=audio/x-mulaw;rate=8000>` with a status callback and `extraHeaders`. Plivo only accepts
  `[A-Za-z0-9]` there, so the leading `+` is stripped and `mode` / `apptId` are appended as plain
  `key=value;...` pairs.
- `dial_xml()` — `<Dial timeout=... callerId=... action=...>` (caller id without the `+`).
- `speak_hangup_xml()` / `hangup_xml()`.
- `transfer_call()` — Plivo REST `calls.transfer(legs="aleg", aleg_url=...)` in a thread.
- `validate_signature()` — Plivo V3 signature over method + URL + nonce + params.
- `parse_stream_event()` — `start` (reads custom headers from `extra_headers` **or**
  `start.extraHeaders`, restores the missing `+` on the phone, fills `extra`), `media` (inbound
  only), `playedStream` → mark, `dtmf`.
- `audio_message()` / `mark_message()` / `clear_message()` — `playAudio`, `checkpoint`, `clearAudio`.
- `make_call()` — REST `calls.create` for outbound reminder calls; returns the request UUID.
- `start_recording()` — REST `calls.record` (time limit 900 s, mp3, callback URL).

#### `twilio/twilio_adapter.py`

Same inbound interface with TwiML: `<Connect><Stream>` + `<Parameter name="callerPhone">`, `<Dial>`,
`<Say>`, REST `calls(sid).update(url=...)`, `RequestValidator`, and `media` / `mark` / `clear`
messages.

> ⚠️ The Twilio adapter's `stream_xml()` still takes only three arguments (no `extra`), unlike the
> base class and the Plivo adapter, and it has no `make_call` / `start_recording`. Outbound reminders
> are Plivo-only today.

#### `telephony/__init__.py`

`get_adapter()` returns the adapter for `TELEPHONY_PROVIDER`. (The route modules currently construct
their adapter directly instead of using this; `outbound_service.py` also builds a `PlivoAdapter`.)

### 5.8 `app/services/`

| File | Responsibility |
|---|---|
| `call_service.py` | `start_call()` inserts a `call_logs` row; `end_call()` fills in caller type / patient / urgency / end time; `create_callback()` inserts a `callbacks` row and returns True/False. Every DB error is logged, never raised. |
| `patient_service.py` | `normalize_phone()` (phonenumbers, region IN, E.164); `find_by_phone()`; `verify()` — exact DOB **and** fuzzy full-name match (`SequenceMatcher ≥ 0.8`, because speech-to-text misspells). |
| `summary_service.py` | `finish_call()` — idempotent (returns the existing summary if called twice), builds the transcript, asks the LLM for a 3–5 line summary, falls back to the last 6 lines on failure, saves it, closes the call log, and alerts the doctor when urgency is not routine or the caller was new. |
| `notification_service.py` | `notify_doctor(message, level)` — currently only writes a log line with an emoji prefix. WhatsApp/SMS is planned here. |
| `appointment_service.py` | All reminder data: `import_excel()` (headers `name, phone, date, time` + optional `language, consent`; dates in several formats; phone normalised; duplicates skipped; past rows refused), `housekeeping()` (expire visits too close, unstick calls stuck in `calling` for 20 min), `claim_due()` (select due rows with `FOR UPDATE SKIP LOCKED`, respect `OUTBOUND_MAX_CONCURRENT`, mark `calling`), `claim_one()`, `get_brief()`, `set_call_uuid()`, `finish_attempt()`, `record_outcome()`, `set_summary()`, `set_recording()`, `list_appointments()`. |
| `outbound_service.py` | `place_call(appointment)` — builds the answer/hangup URLs with `?appt=<id>`, calls `adapter.make_call()`, stores the request UUID. On failure it marks the attempt failed so the row can retry. |
| `reminder_scheduler.py` | `start()` / `stop()` / `run_due_reminders()`. One APScheduler interval job; skips when `OUTBOUND_ENABLED=false`, runs housekeeping, checks calling hours, claims up to `OUTBOUND_MAX_PER_RUN` rows and places the calls 2 s apart. |
| `clinic_service.py` | Legacy helper for an older `clinic.json` format. **Unused and broken** (reads a setting that no longer exists). |

### 5.9 `app/models/`

| Model | Table | Columns |
|---|---|---|
| `Patient` | `patients` | id, full_name, phone_number (unique, indexed), date_of_birth, created_at |
| `CallLog` | `call_logs` | id, call_id (unique), caller_phone, patient_id (FK), caller_type, urgency, started_at, ended_at |
| `Callback` | `callbacks` | id, call_id, name, phone, date_of_birth, issue, urgency, status (default `pending`), created_at |
| `CallSummary` | `call_summaries` | id, call_id (unique), summary, urgency, created_at |
| `Appointment` | `appointments` | id, patient_name, phone (indexed), appointment_at (tz-aware, indexed), language, status (default `pending`, indexed), attempts, last_attempt_at, next_attempt_at, call_uuid (indexed), feeling_note, summary, recording_url, created_at. Unique on `(phone, appointment_at)`. |

Status values: `pending → calling → confirmed / reschedule / cancelled`, plus `unconfirmed`
(answered, no decision), `no_answer` (attempts used up) and `expired` (too close to the visit).

LangGraph also creates its own checkpoint tables automatically on startup. All app tables are
created automatically too (`Base.metadata.create_all` in `main.py`).

### 5.10 Scripts

`app/scripts/`

| Script | Purpose |
|---|---|
| `simulate_call.py` | Pretends to be Plivo: connects to `ws://localhost:8000/voice/plivo/stream`, sends WAV files as caller audio, saves the bot's replies as `sim_greeting.wav` / `sim_reply_N.wav`. Needs `websockets` (now in requirements). |
| `test_sarvam.py` | Generates Sarvam samples for one or more speakers in en/hi/or, prints timings, saves WAVs, and does one STT round trip per language. |
| `make_voices.py` | Older voice-comparison tool. **Broken import** (`app.speech.client` no longer exists). |
| `test_speech.py` | Older Bodhan test. **Broken import** (`app.speech.bodhan_stt` / `bodhan_tts` moved). |
| `test_voices.py` | Older Bodhan voice test. **Broken import**. |

`scripts/` (project root, run from the project root)

| Script | Purpose |
|---|---|
| `make_demo_sheet.py` | Writes `demo_data/appointments_demo.xlsx` with one row for tomorrow (name, phone, date, time, language, consent). Edit the row before running it — it contains a real-looking phone number. |
| `set_patient_name.py` | `python scripts/set_patient_name.py "Full Name" [--id N]` — changes the name the reminder call says, in the `appointments` table **and** in the demo sheet. |

---

## 6. End-to-end data flow

### 6.1 A live phone call, step by step

```text
Patient's phone
      │  dials the Plivo number
      ▼
Plivo  ──POST /voice/plivo/answer──►  FastAPI
      ◄── XML: <Stream> ws://.../voice/plivo/stream ──
      │
      │  opens WebSocket
      ▼
WS /voice/plivo/stream  ──►  CallSession.run()
                               │
                               ├─ event: start
                               │    • call_id, stream_id
                               │    • format check (mu-law 8 kHz)
                               │    • caller phone from extraHeaders
                               │    • call_service.start_call()  → DB row
                               │    • queue greeting
                               │
                               ├─ greeting → TTS (cache hit) → audio chunks → Plivo
                               │
                               ├─ event: media  (caller speaking)
                               │    mu-law → PCM → VAD
                               │    sentence end → queue utterance
                               │
                               ├─ turn worker
                               │    1. PCM 8k → WAV 16k
                               │    2. Sarvam STT  → text + language
                               │    3. graph.invoke(thread_id = call_id)
                               │         lookup_patient → urgency_check → branch
                               │    4. reply text → split sentences
                               │    5. Sarvam TTS (parallel) → mu-law
                               │    6. send playAudio chunks + checkpoint marker
                               │
                               ├─ event: mark (playedStream)
                               │    playback finished → allow next turn
                               │
                               └─ event: stop / socket closed
                                    summary_service.finish_call()
```

**Timings from a real test call** (from the server log):

| Stage | Typical time |
|---|---|
| Greeting first audio | 0.0–0.1 s (fully cached) |
| Speech-to-text | 0.2–1.2 s |
| Agent (LLM) | 1.0–3.1 s |
| First reply audio after speech ended | 1.6–5.0 s (includes the 0.6 s end-of-speech wait) |

### 6.2 Text chat flow (no phone)

```text
POST /chat/start   → call_id + greeting
POST /chat/message → graph.invoke() → reply, urgency, verified, action
POST /chat/end     → summary + doctor alert
```

Same graph, same services, same data files. Only the audio layer is skipped.

### 6.3 Emergency flow

```text
caller says "I fell and I can't walk"
        │
        ▼
urgency_check → rules.classify() → "emergency"   (plain Python, no LLM)
        │
        ▼
escalate
   ├─ create_callback(urgency="emergency")
   ├─ notify_doctor("🚨 EMERGENCY | ...")
   ├─ say the approved emergency_transfer line
   └─ action = "transfer"
        │
        ▼
CallSession sees action == "transfer"
   └─ waits for playback marker
      → Plivo REST: transfer aleg to /voice/plivo/transfer
        │
        ▼
POST /voice/plivo/transfer?attempt=1  → <Dial> doctor's number (25 s)
        │
        ▼
POST /voice/plivo/transfer-status?attempt=1
   ├─ answered → <Hangup/>
   └─ no answer → <Dial> BACKUP_PHONE (attempt 2)
        └─ still no answer → alert doctor + speak transfer_failed + hang up
```

The callback is saved **before** the transfer, so nothing is lost if the doctor cannot be reached.

### 6.4 Post-call flow

**Inbound call:**

```text
socket closes
   └─ summary_service.finish_call(graph, call_id)
        ├─ already summarised? return it
        ├─ read final state from the checkpointer
        ├─ LLM summary (3–5 lines) or fallback (last 6 messages)
        ├─ insert call_summaries row
        ├─ call_service.end_call() → fill call_logs
        └─ if urgency != routine or caller is new → notify_doctor()
```

**Reminder call:** the session skips `finish_call` (that is for the inbound graph). The outcome,
feelings and note are already on the `appointments` row, and the recording URL arrives later from
Plivo's recording callback.

### 6.5 Who calls whom

```text
routes/voice_plivo.py  →  voice/session.py  →  agent/graph.py  →  agent/nodes.py
                                             →  services/*      →  models/*  →  PostgreSQL (RDS)
                                             →  speech/factory.py → speech/sarvam/*  →  Sarvam API
                                             →  agent/llm.py      →  OpenAI API
voice/session.py       →  telephony/plivo/plivo_adapter.py → Plivo REST + WebSocket

services/reminder_scheduler.py → appointment_service → outbound_service → PlivoAdapter.make_call
routes/v1/voice_plivo_outbound.py → voice/session.py (reminder mode) → agent/reminder_graph.py
routes/v1/appointments.py → appointment_service / outbound_service
```

### 6.6 Outbound reminder call, step by step

```text
staff uploads Excel                    scheduler (every REMINDER_POLL_MINUTES)
      │                                      │
POST /appointments/import              claim_due(OUTBOUND_MAX_PER_RUN)
      ▼                                      ▼
appointment_service.import_excel       rows marked status='calling'
      │  validates name/phone/date/time       │
      ▼  skips duplicates/past/no-consent     ▼
appointments table (pending)           outbound_service.place_call()
                                             │ REST calls.create(from=PLIVO_NUMBER, to=patient,
                                             │   answer_url=.../outbound/answer?appt=ID,
                                             │   hangup_url=.../outbound/hangup?appt=ID)
                                             ▼
                                       patient's phone rings
                                             │ answers
                                             ▼
POST /voice/plivo/outbound/answer?appt=ID
   ├─ loads the appointment (name/phone/language)
   ├─ stores CallUUID
   └─ XML: <Stream> ws://.../voice/plivo/stream?mode=reminder&apptId=ID
             + extraHeaders "callerPhone=...;mode=reminder;apptId=ID"
                                             │
                                             ▼
CallSession (mode="reminder", graph=reminder_graph)
   ├─ greeting: "Am I speaking with <name>? This call may be recorded."
   ├─ starts Plivo recording (callback → /outbound/recording?appt=ID)
   ├─ turn loop: STT → reminder graph → TTS → audio
   │     ├─ not the patient → outcome 'wrong_person', retry later
   │     ├─ will come        → outcome 'confirmed'
   │     ├─ wants another time → outcome 'reschedule' + callback row
   │     ├─ will not come    → outcome 'cancel' + callback row
   │     └─ severe pain / emergency words → callback + transfer to doctor
   ├─ after the goodbye → action="hangup" → wait 2–6 s → close socket
   └─ socket closed → Plivo POSTs /outbound/hangup?appt=ID
           ├─ status completed & duration > 0 → leave the saved outcome (else 'unconfirmed')
           └─ not answered → retry (next_attempt_at) or 'no_answer'
```

### 6.7 Staff API flow

```text
POST /appointments/import  (X-API-Key)
   → {"imported": N, "skipped": [{row, reason}, ...]}

GET /appointments?status=pending
   → newest first, with status, attempts, feeling_note, summary, has_recording

POST /appointments/{id}/call-now
   → claims the row and calls immediately (ignores calling hours and the 24 h window)
   → {"appointment": id, "call_placed": true|false}
```

---

## 7. How Plivo is used

### 7.1 The pieces

| Plivo feature | Where it is used |
|---|---|
| Answer URL (POST webhook) | `/voice/plivo/answer` (inbound), `/voice/plivo/outbound/answer` (outbound) |
| `<Stream bidirectional>` | opens the audio WebSocket |
| `extraHeaders` | carries the caller's phone number (and reminder tags) into the stream |
| WebSocket events | `start`, `media`, `playedStream`, `dtmf`, `stop` |
| `playAudio` / `clearAudio` / `checkpoint` | send audio, interrupt, detect playback end |
| REST `calls.transfer` | move a live call to the doctor |
| REST `calls.create` | place an outbound reminder call |
| REST `calls.record` | record a reminder call (mp3, 900 s limit, callback URL) |
| Hangup URL | `/voice/plivo/outbound/hangup?appt=ID` reports whether the reminder call connected |
| `<Dial action=...>` | ring the doctor and report the result |
| `<Speak>` / `<Hangup>` | fallback lines when transfer is impossible |
| V3 signature | webhook authentication |

### 7.2 Number setup in the Plivo console

1. Buy/rent an Indian number (E.164, e.g. `+918031772845`).
2. Create an Application with:
   - **Answer URL:** `https://<your-public-url>/voice/plivo/answer`, method **POST**
   - **Hangup URL:** not required for inbound; outbound calls set their own hangup URL per call
3. Attach the application to the number.
4. Put the same number in `PLIVO_NUMBER` and the public URL in `PUBLIC_BASE_URL`.

In production the public URL is `https://voice.indiahealthsystem.com`, so the Answer URL is
`https://voice.indiahealthsystem.com/voice/plivo/answer`. During local development it is the ngrok
URL instead (section 13.6).

**For outbound reminders** no extra console setup is needed beyond the number itself: the code
passes `from_=PLIVO_NUMBER`, `answer_url=.../voice/plivo/outbound/answer?appt=ID` and
`hangup_url=.../voice/plivo/outbound/hangup?appt=ID` on every `calls.create`. Make sure the Plivo
account is allowed to call Indian mobiles (DND / outbound permissions) and that the number can
terminate calls, not only receive them.

### 7.3 How the caller's number travels

The Answer webhook has `From`, so the number is stored in `_CALLERS[call_id]`. At the same time the
XML includes `extraHeaders=callerPhone=...`. When the stream starts, Plivo echoes those headers, so
`CallSession` gets the number even if the in-memory map was lost. If both fail, the caller is treated
as **new** (safe default).

For a reminder call the same mechanism carries the mode: `stream_xml(..., extra={"mode": "reminder",
"apptId": ID})` puts `mode=reminder;apptId=ID` into `extraHeaders`, and the same values are also in
the WebSocket query string (`.../stream?mode=reminder&apptId=ID`). `CallSession` checks the headers
first and the query string second. Plivo's `extraHeaders` only accepts `[A-Za-z0-9]`, so the leading
`+` of the phone number is stripped on the way in and restored on the way out.

### 7.4 Signature checking

`PLIVO_VALIDATE_SIGNATURE=false` during development. Set it to `true` only after a real call works,
because the signature is computed over the exact public URL — a wrong `PUBLIC_BASE_URL` makes every
request fail with 403.

⚠️ A leading space in `.env` (`PUBLIC_BASE_URL= https://...`) breaks this silently: pydantic keeps
the space, and Plivo's signature is computed over a URL with `%20` in it. This is the current state
of the local `.env` — see section 16.

### 7.5 Twilio differences (same interface)

| Thing | Plivo | Twilio |
|---|---|---|
| Stream XML | `<Stream>` | `<Connect><Stream>` |
| Caller number | `extraHeaders` | `<Parameter name="callerPhone">` |
| Playback marker | `checkpoint` → `playedStream` | `mark` → `mark` |
| Clear audio | `clearAudio` | `clear` |
| Transfer | REST `calls.transfer` | REST `calls(sid).update(url=...)` |
| Signature | V3 (method+URL+nonce+params) | `X-Twilio-Signature` (URL+params) |
| Transfer result | `DialBLegUUID` | `DialCallStatus` |
| Outbound call | REST `calls.create` | not implemented |
| Recording | REST `calls.record` | not implemented |
| Reminder webhooks | `/voice/plivo/outbound/*` | none |

**Outbound reminders are Plivo-only.** `TwilioAdapter.stream_xml()` also has a different signature
(three arguments, no `extra`), so it does not match the base contract. Inbound Twilio still works;
adding reminders to Twilio means implementing `make_call`, `start_recording` and the `extra`
argument first.

---

## 8. How Sarvam is used (and Bodhan)

### 8.1 Which provider is active

`STT_PROVIDER` and `TTS_PROVIDER` in `.env` (currently both `sarvam`). `factory.py` reads them on
every call, so switching is a config change, not a code change.

### 8.2 Speech-to-text (Sarvam Saaras v3)

```text
8 kHz mu-law caller audio
   → ulaw_to_pcm
   → ratecv 8k → 16k
   → WAV wrapper
   → POST https://api.sarvam.ai/speech-to-text
        model=saaras:v3
        mode=codemix          (keeps English words in English)
        language_code=unknown (auto-detect)
   → {"transcript": "...", "language_code": "od-IN"|"hi-IN"|"en-IN"}
   → Transcript(text, "or"|"hi"|"en")
```

`codemix` matters here: callers mix Odia/Hindi with English words like "X-ray" or "left leg", and
`codemix` keeps those English words readable instead of transliterating them.

### 8.3 Text-to-speech (Sarvam Bulbul v3)

```text
reply text
   → split_sentences()  (first sentence alone, rest together)
   → POST https://api.sarvam.ai/text-to-speech  (one request per piece, in parallel)
        model=bulbul:v3, speaker=ritu, pace=1.0, temperature=0.6
        target_language_code=od-IN|hi-IN|en-IN
        speech_sample_rate=8000
        output_audio_codec=wav
        enable_preprocessing=true   (numbers and dates read naturally)
   → base64 → WAV → wav_to_ulaw_8k → mu-law
   → played in order, with a checkpoint after the last piece
```

### 8.4 The cache (why the greeting is instant)

Only **fixed lines** are cached: every piece of every reply in `data/fixed_replies.json`, in all
three languages. The cache key includes provider, model, speaker, pace, language and text, so changing
the voice automatically produces new audio.

- `prewarm_fixed_lines()` runs at startup and fills the cache.
- At call time the greeting, filler, verification prompts, refusals and confirmations are served from
  disk in milliseconds (measured: 0.0–0.1 s to first audio).
- LLM-generated replies are **never** cached (they contain patient-specific text).
- The **reminder greeting is not cached**: it is built in `reminder_graph.reminder_greeting()` and
  includes the patient's name, so it is not in `fixed_replies.json`. The first reminder call after a
  restart pays one live TTS request before audio starts.

### 8.5 Rate limits and failures

- HTTP 429 → `QuotaError`; prewarm stops, live calls still work from cache.
- Any other non-200 → `SpeechError`; the session falls back to an approved line instead of crashing.
- A semaphore (`TTS_CONCURRENCY=3`) limits parallel requests.

### 8.6 Language during a call

The call starts in `DEFAULT_LANGUAGE` (Odia). After each transcript the session asks
`detect_caller_language()`; the first confident sentence sets the call language and a switch needs two
sentences in a row, so one English word ("X-ray") does not flip the call.

A **reminder** call keeps the language stored on the appointment, even if the patient's first words
are short or noisy. If the patient explicitly asks ("speak in English", "ଓଡ଼ିଆ କୁହନ୍ତୁ"), 
`text_utils.requested_language()` returns that language and the session switches immediately.

### 8.7 Bodhan (the alternative)

Same interface, different API:

- STT: `POST /audio/transcriptions`, `indic-transcribe`, Bearer key, returns plain text (no language).
- TTS: `POST /audio/speech`, `indic-speak`, `voice` + `instructions` JSON, returns WAV.
- Serialised with a semaphore + 1 s spacing, in-memory cache for fixed lines.

Set `STT_PROVIDER=bodhan` / `TTS_PROVIDER=bodhan` and fill the two Bodhan keys to use it.

---

## 9. How the LangGraph agent works

### 9.1 Memory

Every call is a **thread** in the checkpointer, keyed by `call_id` (`thread_id`). After each turn the
whole `CallState` is saved to PostgreSQL, so a server restart does not lose a live call, and each new
message continues from the previous state.

### 9.2 The turn

```text
new caller message
   │
   ├─ lookup_patient    (only once; skipped if caller_type already known)
   ├─ urgency_check     (every message; rules only)
   └─ route:
        emergency            → escalate           → END
        existing & unverified→ verify_identity    → END
        otherwise            → assistant
                                 ├─ no tool  → END
                                 └─ tool     → tools → callback_reply → END
```

### 9.3 Identity verification

1. The LLM extracts `{full_name, date_of_birth}` with structured output, transliterating names to
   Latin letters and normalising dates to `YYYY-MM-DD`.
2. Partial results are kept in `given_name` / `given_dob` so "name now, DOB next turn" works.
3. `patient_service.verify()` requires an **exact DOB** and a **fuzzy name match ≥ 0.8**
   (`SequenceMatcher`), because STT misspells names.
4. Two failures → `verify_failed` line and the caller is offered a callback. Verification is never
   retried forever (by design).

### 9.4 Safety behaviour

| Situation | Behaviour |
|---|---|
| DB lookup fails | caller treated as **new**; no private data can leak |
| Urgency rule check fails | treated as **urgent** |
| LLM fails | approved `error` line, call continues |
| TTS fails | approved line or silence, call continues |
| Summary LLM fails | fallback summary from the last messages |
| Emergency | callback saved first, then transfer, then backup, then "doctor is busy" |

### 9.5 The reminder graph (`app/agent/reminders/graph.py`)

A separate, much smaller graph — the inbound flow is untouched.

```text
new patient message
   │
   ├─ urgency_check     (same rule engine, same emergency_rules.json)
   └─ route:
        emergency words        → escalate (save emergency callback + alert + transfer) → END
        everything else        → assistant
                                   ├─ no tool  → END
                                   └─ record_reminder_outcome → END
```

| Piece | Behaviour |
|---|---|
| `reminder_greeting(lang, name)` | "Am I speaking with {name}? This call may be recorded." in en/hi/or. Spoken by the session before the graph runs. |
| `build_reminder_prompt(appointment)` | tells the LLM the appointment time, to confirm attendance, ask how the patient feels, and to save exactly one outcome. Forbids medical advice and forbids promising a new slot. |
| `record_reminder_outcome(outcome, config, feeling, note)` | the only decision tool. `outcome` ∈ `confirmed / reschedule / cancel / wrong_person`. It writes the row, and for reschedule/cancel also inserts a `callbacks` row so the clinic team phones back. |
| `reminder_escalate` | emergency during a reminder: `call_service.create_callback(urgency="emergency")`, `notify_doctor`, `action="transfer"`. |
| `reminder_assistant` | runs the LLM; after the outcome is saved **and** the reply contains a farewell phrase and no `?` (`_said_goodbye()`), it sets `action="hangup"`. |
| `REMINDER_TOOLS` | `[record_reminder_outcome, request_callback]` |

The appointment id arrives in `config["configurable"]["appt_id"]`; `CallSession` supplies it from
the stream headers. The same `PostgresSaver` is used, so the reminder conversation also survives a
restart mid-call.

`_said_goodbye()` is deliberately conservative: a question mark or a follow-up phrase keeps the call
open. If it never matches, the call simply ends when the patient hangs up (Plivo then hits the
hangup webhook, which marks it `unconfirmed`).

---

## 10. Data files

All three live in `data/` and are loaded once at startup. The app **will not start** if one is
missing, invalid, or missing a required reply key.

### 10.1 `clinic_info.json`

The only source of clinic facts. Keys starting with `_` are notes for humans and are hidden from the
prompt.

```json
{
  "doctor_name": "Dr. Pushpak Samal",
  "specialty": "...",
  "address": "...",
  "timings": "Monday to Saturday, 10 AM to 6 PM. Closed on Sundays.",
  "fees": { "consultation": "...", "follow_up_visit": "..." },
  "procedures": [ { "name": "...", "usual_cost": "...", "note": "..." } ],
  "faqs": [ { "q": "...", "a": "..." } ]
}
```

Currently **demo data**: only the address and the ACL/MCL cost are real. Everything else must be
approved by the doctor.

### 10.2 `emergency_rules.json`

```json
{
  "emergency": ["cant walk", "bone sticking out", "heavy bleeding", "road accident", ...],
  "urgent":    ["getting worse", "new swelling", "fever after surgery", ...]
}
```

Matching is whole-word and case/punctuation-insensitive. Emergency is checked before urgent.

### 10.3 `fixed_replies.json`

Every line the bot says without the LLM, in **three languages** (`en`, `hi`, `or`), each an array of
variants. Required keys:

```text
greeting, ask_identity, ask_name, ask_dob, verified, verify_retry, verify_failed,
not_understood, medical_advice_decline, emergency_transfer, transfer_failed,
error, filler, callback_saved, callback_failed
```

`verified` contains `{first_name}` and is filled in by `verify_identity`.

### 10.4 The appointment sheet (Excel) — not a JSON file, but approved data

Staff upload an `.xlsx` workbook to `POST /appointments/import` (multipart field `file`). The parser
reads the **first worksheet** and matches headings case-insensitively:

| Column | Accepted headings | Required | Notes |
|---|---|---|---|
| name | `name`, `patient name`, `patient`, `full name` | yes | spoken in the greeting |
| phone | `phone`, `mobile`, `phone number`, `mobile number`, `contact` | yes | normalised to E.164 (+91 assumed); invalid → skipped |
| date | `date`, `appointment date` | yes | `DD/MM/YYYY`, `DD-MM-YYYY`, `YYYY-MM-DD`, `DD.MM.YYYY`, `DD Mon YYYY`, `DD Month YYYY`; a real Excel date cell also works |
| time | `time`, `appointment time` | yes | `10:30 AM`, `10:30AM`, `10:30`, `10:30:00`, `10 AM` |
| language | `language`, `lang` | no | `or`/`odia`/`oriya` (default when blank), `hi`/`hindi`, `en`/`english` |
| consent | `consent`, `reminder consent` | no | an explicit no (`no`, `n`, `false`, `0`, `declined`) skips the row; blank means allowed |

Example (the same shape `scripts/make_demo_sheet.py` writes):

```text
name            phone              date         time       language   consent
Patient Name    +91XXXXXXXXXX      20/11/2026   10:30 AM   or         yes
```

Rules applied on import:

- Rows are **skipped** (and reported in the response, with the row number and reason) when: the
  name is blank, the phone cannot be normalised, the date/time cannot be parsed, consent is
  explicitly negative, the appointment is already in the past, or the same
  `(phone, appointment_at)` already exists (`"already imported"`).
- Blank rows are ignored silently and do not appear in `skipped`.
- Times are interpreted as **IST** (`Asia/Kolkata`, UTC+05:30, no DST).
- The appointment is stored with `status="pending"`; nothing is called at import time.
- The scheduler only calls rows that are inside the reminder window (about 24 h before the visit,
  and never closer than `REMINDER_MIN_LEAD_HOURS`), during `OUTBOUND_START_HOUR`–`OUTBOUND_END_HOUR`.
- A missing required column (name/phone/date/time) rejects the **whole file** with a `ValueError`
  naming the missing columns; it is not a per-row skip.

The response looks like:

```json
{"imported": 3, "skipped": [{"row": 4, "reason": "already imported"}, {"row": 5, "reason": "appointment is in the past"}]}
```

### 10.5 Rules for editing

- Keep the JSON valid (the app fails fast otherwise).
- Keep all three languages present; missing ones fall back to English.
- No emojis in spoken lines.
- Get the doctor's approval before changing medical or emergency wording.
- Restart the app after editing (the loaders are cached).
- Delete `.cache/tts` if you change wording, or stale audio may be reused — actually the cache key
  includes the text, so old files are simply unused; deleting is only for disk space.

---

## 11. Database

### 11.1 Tables

```text
patients          id, full_name, phone_number(unique), date_of_birth, created_at
call_logs         id, call_id(unique), caller_phone, patient_id→patients, caller_type,
                  urgency, started_at, ended_at
callbacks         id, call_id, name, phone, date_of_birth, issue, urgency,
                  status(pending|done), created_at
call_summaries    id, call_id(unique), summary, urgency, created_at
appointments      id, patient_name, phone(idx), appointment_at(tz-aware, idx),
                  language, status(default pending, idx), attempts, last_attempt_at,
                  next_attempt_at, call_uuid(idx), feeling_note, summary, recording_url,
                  created_at      UNIQUE(phone, appointment_at)
```

Plus LangGraph's checkpoint tables (`checkpoints`, `checkpoint_blobs`, `checkpoint_writes`,
`checkpoint_migrations`), created automatically by `checkpointer.setup()`.

**All app tables are created automatically.** `main.py` calls `Base.metadata.create_all(engine)` on
startup, so a fresh database (local Docker or a brand-new RDS instance) gets the schema without any
manual command. This used to be a manual step; it no longer is. `alembic` is in
`requirements.txt` but `migrations/` is still empty, so schema *changes* to an existing table
(e.g. adding a column) are not handled — drop/recreate or add a migration.

### 11.2 How rows are written

| Moment | Row |
|---|---|
| call starts | `call_logs` insert |
| callback requested / emergency | `callbacks` insert |
| call ends (inbound) | `call_summaries` insert + `call_logs` update |
| Excel import | `appointments` insert (`pending`) |
| reminder call placed | `appointments.status → calling`, `attempts+1`, `last_attempt_at` |
| patient decides | `appointments.status`: `confirmed` / `reschedule` / `cancelled`; `wrong_person` returns it to `pending` for a retry; `feeling_note` saved |
| reminder call ends | `appointments.status → unconfirmed` or retry / `no_answer` |
| recording ready | `appointments.recording_url` |

All writes go through `app/services/*` and are wrapped in try/except: **a database failure never
crashes a live call**.

### 11.3 Where the database runs today

| | Local development | Production (live) |
|---|---|---|
| Host | Docker container `orthopedic-db` on your laptop | **Amazon RDS** (PostgreSQL 16) |
| Address | `127.0.0.1:5433` | `orthopedic-db.<id>.ap-southeast-2.rds.amazonaws.com:5432` (private) |
| User / password | `appuser` / (local password) | `appuser` / (RDS password, in `.env` only) |
| Database | `orthopedic_voice` | `orthopedic_voice` |
| TLS | `sslmode=require` | `sslmode=require` |
| Backups | none | automated RDS snapshots |

Production URL shape (no real password here):

```dotenv
DATABASE_URL=postgresql+psycopg://appuser:<password>@<rds-endpoint>:5432/orthopedic_voice?sslmode=require
```

Local development uses the container:

```dotenv
DATABASE_URL=postgresql+psycopg://appuser:<password>@127.0.0.1:5433/orthopedic_voice?sslmode=require
```

The `postgresql+psycopg://` prefix matters: `settings.sqlalchemy_url` normalises to it and
`settings.psycopg_url` strips it for asyncpg/psycopg. A bare `postgresql://` still works because of
that normalisation, but writing the driver explicitly avoids surprises.

**Tables are created automatically** (see 11.1). If you ever see
`UndefinedTable: relation "call_logs" does not exist`, the app started before the database was
reachable, or `create_all` was removed — check the startup log.

### 11.4 Inspecting data

Production (RDS). Either install `psql` locally and connect over the private network / SSM session,
or run it from the EC2 box (the RDS security group allows that instance):

```bash
psql "host=<rds-endpoint> dbname=orthopedic_voice user=appuser sslmode=require" \
  -c "SELECT id, name, phone, issue, urgency, status FROM callbacks ORDER BY id DESC LIMIT 20;"

psql "... " -c "SELECT call_id, urgency, left(summary, 120) FROM call_summaries ORDER BY id DESC LIMIT 10;"

psql "... " -c "SELECT id, patient_name, phone, appointment_at, status, attempts, feeling_note
                FROM appointments ORDER BY appointment_at DESC LIMIT 20;"

psql "... " -c "\dt"
```

Local development:

```powershell
docker exec -it orthopedic-db psql -U appuser -d orthopedic_voice -c "SELECT id, patient_name, phone, appointment_at, status FROM appointments ORDER BY id DESC LIMIT 20;"
```

Remember there is **no `db` container** in production any more: `docker exec db psql ...` fails on
the server, and that is correct.

### 11.5 The RDS migration (completed)

The container database was deliberately temporary — no automated backups, same disk as the app.
It has now been moved to **Amazon RDS**, and `docker-compose.yaml` no longer defines a `db` service.
Section 18 is the full record of what was done and how to repeat it.

---

## 12. Configuration reference

`.env` (values are examples — never commit real keys):

```dotenv
# ── App ──────────────────────────────────────────────
APP_ENV=production                      # "development" on a laptop
PUBLIC_BASE_URL=https://voice.example.com   # no trailing slash, no leading space

# ── Database ─────────────────────────────────────────
# production: Amazon RDS
DATABASE_URL=postgresql+psycopg://appuser:<password>@<rds-endpoint>:5432/orthopedic_voice?sslmode=require
# local development (Docker container published on 5433)
# DATABASE_URL=postgresql+psycopg://appuser:<password>@127.0.0.1:5433/orthopedic_voice?sslmode=require

# ── Staff API (appointments) ─────────────────────────
ADMIN_API_KEY=<long-random-string>      # empty = /appointments/* answers 503

# ── Outbound reminder calls ──────────────────────────
OUTBOUND_ENABLED=true                   # false = the scheduler does nothing
OUTBOUND_START_HOUR=10                  # IST, inclusive
OUTBOUND_END_HOUR=19                    # IST, exclusive
OUTBOUND_MAX_PER_RUN=5                  # calls placed per scheduler pass
OUTBOUND_MAX_CONCURRENT=2               # rows allowed in "calling" at once
OUTBOUND_MAX_ATTEMPTS=3                 # then status becomes no_answer
OUTBOUND_RETRY_MINUTES=30               # wait before the next attempt

# ── Reminder window ──────────────────────────────────
REMINDER_POLL_MINUTES=5                 # how often the scheduler runs
REMINDER_HOURS_BEFORE=24                # call this long before the visit
REMINDER_MIN_LEAD_HOURS=2               # never call closer than this

# ── Telephony ────────────────────────────────────────
TELEPHONY_PROVIDER=plivo                # plivo | twilio (reminders: plivo only)
PLIVO_AUTH_ID=
PLIVO_AUTH_TOKEN=
PLIVO_NUMBER=+91XXXXXXXXXX
PLIVO_VALIDATE_SIGNATURE=false          # true only after a successful real call
PLIVO_SPEAK_VOICE=                      # optional fallback <Speak> voice
PLIVO_SPEAK_LANGUAGE=                   # optional, e.g. en-IN

TWILIO_ACCOUNT_SID=
TWILIO_AUTH_TOKEN=
TWILIO_NUMBER=
TWILIO_VALIDATE_SIGNATURE=false
TWILIO_VOICE=Polly.Aditi
SPEECH_LANGUAGE=en-IN

# ── Speech providers ─────────────────────────────────
STT_PROVIDER=sarvam                     # sarvam | bodhan
TTS_PROVIDER=sarvam                     # sarvam | bodhan
TTS_CACHE_DIR=                          # empty = <project>/.cache/tts
TTS_CONCURRENCY=3

SARVAM_API_KEY=
SARVAM_API_BASE_URL=https://api.sarvam.ai
SARVAM_STT_MODEL=saaras:v3
SARVAM_STT_MODE=codemix                 # transcribe | codemix
SARVAM_STT_LANGUAGE=unknown             # unknown = auto-detect
SARVAM_TTS_MODEL=bulbul:v3
SARVAM_TTS_SPEAKER=ritu                 # case-sensitive
SARVAM_TTS_PACE=1.0
SARVAM_TTS_TEMPERATURE=0.6

BODHAN_API_BASE_URL=https://api.bodhan.ai/v1
BODHAN_TRANSCRIBE_API_KEY=
BODHAN_SPEAK_API_KEY=
BODHAN_VOICE=Kavya
BODHAN_TTS_STYLE=
BODHAN_STT_LANGUAGE=

# ── Voice pipeline tuning ────────────────────────────
DEFAULT_LANGUAGE=or                     # greeting language before the caller speaks
VAD_RMS_THRESHOLD=450                   # lower = more sensitive
VAD_END_SILENCE_MS=600                  # silence that means "finished speaking"
VAD_MIN_SPEECH_MS=300                   # shorter sounds ignored as noise
VAD_MAX_UTTERANCE_S=25                  # STT limit is 30 s per request
BARGE_IN_MS=400                         # caller speech needed to interrupt the bot
LOG_TRANSCRIPTS=false                   # true only for testing (privacy)

# ── Latency ──────────────────────────────────────────
FILLER_ENABLED=true
FILLER_AFTER_MS=1500
FILLER_MIN_SPEECH_S=2.5
FILLER_MAX_PER_CALL=2
REPLY_MAX_TOKENS=220

# ── AI ───────────────────────────────────────────────
OPENAI_API_KEY=
OPENAI_MODEL=gpt-5.4-mini

# ── Clinic ───────────────────────────────────────────
DOCTOR_NAME=Dr. Pushpak Samal
ESCALATION_PHONE=+91XXXXXXXXXX          # doctor's number for transfers
BACKUP_PHONE=                           # optional second number
TRANSFER_RING_SECONDS=25
```

**Notes**

- `PUBLIC_BASE_URL` must match the tunnel exactly, including `https://`, with **no trailing slash
  and no leading space**. In production it is the real domain; if it is wrong, Plivo receives URLs
  it cannot fetch, the greeting is silent and signature checks return 403. A leading space is a
  silent killer — `PUBLIC_BASE_URL= https://...` looks fine but is not. Strip it.
- `DATABASE_URL` must use the `postgresql+psycopg://` driver (or be normalised by
  `settings.sqlalchemy_url`) so the LangGraph checkpointer can use it. In production the host is the
  RDS endpoint; on a laptop it is `127.0.0.1:5433`.
- `ADMIN_API_KEY` protects `/appointments/*`. If it is empty the endpoints return 503; if it is
  wrong they return 401. Generate it with `python -c "import secrets; print(secrets.token_hex(32))"`.
- `OUTBOUND_ENABLED=false` is the safe switch: the scheduler still runs, but every pass returns
  immediately. Turn it off while testing inbound calls so a reminder does not fire unexpectedly.
- `OUTBOUND_*` hours are **IST** (`Asia/Kolkata`), not UTC. `OUTBOUND_MAX_CONCURRENT` caps how many
  rows can sit in `calling`; `OUTBOUND_MAX_ATTEMPTS` caps retries before `no_answer`.
- `REMINDER_HOURS_BEFORE` + `REMINDER_MIN_LEAD_HOURS` define the window. A visit 5 hours away is not
  called (too close); a visit 3 days away is not called yet.
- `LOG_TRANSCRIPTS` must stay `false` in production — `true` writes patient speech into the logs.
- `ESCALATION_PHONE` must be a **different** number from the one that forwards to the bot, otherwise
  the transfer loops back into the AI.
- `DEFAULT_LANGUAGE` only affects the first greeting and the `/chat` endpoints; after the first
  sentence the bot follows the caller. Reminder calls start in the language stored on the
  appointment.
- `OPENAI_MODEL=gpt-5.4-mini` is the current default in `config.py`. The model helper skips the
  `temperature` parameter for models whose name starts with `gpt-5`, because those models reject it.

---

## 13. Setup from zero

### 13.1 Prerequisites

- Python **3.11**
- Docker Desktop (for the local PostgreSQL) or any PostgreSQL 14+
- An OpenAI API key
- A Sarvam API key (or Bodhan keys)
- A Plivo account + number (or Twilio for inbound only)
- ngrok (for development)
- Excel (`openpyxl` is in `requirements.txt`) — only needed for reminder import

### 13.2 Create the environment

```powershell
cd C:\Users\<you>\Freelance\Orthopedic-AI-Voice-Agent
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

`requirements.txt` now includes `twilio`, `websockets`, `apscheduler`, `openpyxl`, `alembic` and
`langgraph`, so a clean install is enough.

### 13.3 Start PostgreSQL

```powershell
docker run --name orthopedic-db `
  -e POSTGRES_USER=appuser -e POSTGRES_PASSWORD=orthovoice2026 `
  -e POSTGRES_DB=orthopedic_voice -p 5433:5432 -d postgres:16
```

The password above is a throwaway local value. It is the same one the local `DATABASE_URL` line
expects; production uses RDS and a different password.

### 13.4 Create a test patient (tables are automatic)

You no longer need `create_all`: `main.py` creates every table on startup. Start the server once
(13.7), then insert a patient:

```powershell
docker exec -it orthopedic-db psql -U appuser -d orthopedic_voice -c "INSERT INTO patients (full_name, phone_number, date_of_birth) VALUES ('Rahul Sharma', '+919999900001', '1990-03-12');"
```

If you want the schema without starting the app:

```powershell
python -c "import app.models; from app.database import Base, engine; Base.metadata.create_all(engine)"
```

`create_all` only creates **missing** tables; it never alters an existing one. `migrations/` is
still empty, so a column change on an existing database needs a manual `ALTER TABLE` for now.

### 13.5 Create `.env`

```powershell
Copy-Item .env.example .env
notepad .env
```

`.env.example` is stale — it has no Sarvam keys and no `ADMIN_API_KEY` / outbound settings. Fill at
least: `DATABASE_URL`, `PUBLIC_BASE_URL`, `PLIVO_AUTH_ID`, `PLIVO_AUTH_TOKEN`, `PLIVO_NUMBER`,
`SARVAM_API_KEY`, `OPENAI_API_KEY`, `ESCALATION_PHONE`, and for reminders `ADMIN_API_KEY` plus the
`OUTBOUND_*` / `REMINDER_*` block. Section 12 is the full list.

### 13.6 Start the tunnel

```powershell
ngrok http 8000
```

Copy the `https://...ngrok-free.app` URL into `PUBLIC_BASE_URL`. **It changes every restart on the
free plan**, so re-check it each time. Do not paste a space after `=`.

### 13.7 Start the server

```powershell
uvicorn app.main:app --port 8000 --reload --reload-dir app
```

`--reload-dir app` stops the reloader from watching `.venv`, `.cache` and the WAV folders.

Healthy startup looks like:

```text
✅ Database initialized
✅ Data files loaded
✅ Agent graph ready
✅ Reminder graph ready
✅ Speech: STT=sarvam | TTS=sarvam | default language=or
✅ [TTS] Prewarm done: 0 new, 68 already saved, 68 lines in total
INFO: Application startup complete.
```

> If `OUTBOUND_ENABLED=false`, the scheduler starts but does nothing. That is the safe setting for
> working on inbound calls.

### 13.8 Point Plivo at the server

Plivo console → your number → Application → **Answer URL**:
`https://<ngrok>/voice/plivo/answer` (POST). Call the number. Outbound reminders need no console
change; the code passes the answer/hangup URLs on every call.

### 13.9 First real call checklist

- [ ] `PUBLIC_BASE_URL` matches the current ngrok URL, no space, no trailing slash
- [ ] Server shows `POST /voice/plivo/answer 200` and `WS /voice/plivo/stream 101`
- [ ] Greeting is heard within ~0.5 s
- [ ] Caller speech is transcribed (check the log line `Caller spoke (N characters)`)
- [ ] `ESCALATION_PHONE` is the doctor's own number, not the clinic line
- [ ] Test an emergency phrase and confirm the transfer rings the doctor
- [ ] `GET /health` returns 200

### 13.10 Deploying to a real server

Steps 13.1–13.9 describe a laptop plus a tunnel, which is how the project was developed. The live
deployment is different in four ways:

1. the app runs as a Docker container on the server (the database is on RDS), not in a virtualenv;
2. Nginx terminates TLS and forwards traffic to the container, so no tunnel is involved;
3. `PUBLIC_BASE_URL` is the real domain, not an ngrok URL;
4. the image is built **on the server**, because the server is Arm64 and a laptop-built image will
   not run there.

Section 17 covers the whole deployment, section 18 the database, and section 19 is a handover
checklist.

---

## 14. Running and testing

### 14.1 Text chat (no phone, cheapest)

Use Swagger at `/docs`:

1. `POST /chat/start` `{ "phone": "+919999900001" }` → copy `call_id`.
2. `POST /chat/message` `{ "call_id": "...", "phone": "+919999900001", "text": "..." }` repeatedly.
3. `POST /chat/end` `{ "call_id": "..." }`.

| Scenario | Send | Expect |
|---|---|---|
| Existing patient | "I am Rahul Sharma, born 12 March 1990" | asks name/DOB, then `verified: true` |
| Medical advice | "Do I need surgery?" | declines, saves a callback |
| Emergency | "I fell down and I cant walk" | `action: transfer`, `urgency: emergency` |
| New patient | any unknown number | collects name/DOB/issue, saves callback |

> ⚠️ `/chat` replies use `DEFAULT_LANGUAGE` (`or`), so fixed lines come back in Odia.

### 14.2 Simulated phone call (no Plivo needed)

```powershell
python -m app.scripts.simulate_call +919999900001 caller_1.wav caller_2.wav
```

Sends each WAV as caller audio and writes `sim_greeting.wav`, `sim_reply_1.wav`, ... so you can
listen to the bot.

### 14.3 Voice comparison

```powershell
python -m app.scripts.test_sarvam ritu shubh
```

Saves `voice_notes/sarvam_<speaker>_<lang>.wav` and prints synthesis timings.

### 14.4 Health

```powershell
curl http://127.0.0.1:8000/health
```

Returns 200 when the DB is reachable, 503 otherwise.

### 14.5 Reminder calls, end to end

**1. Build a demo sheet (optional).**

```powershell
python scripts/make_demo_sheet.py
```

Writes `demo_data/appointments_demo.xlsx` with one row for tomorrow. Edit the phone number before
uploading it, or change the name the call says:

```powershell
python scripts/set_patient_name.py "Subham Nayak"          # every row
python scripts/set_patient_name.py "Subham Nayak" --id 1   # one row
```

(`set_patient_name.py` updates both the database and the demo sheet — handy for checking that the
greeting pronounces a name correctly.)

**2. Import it.**

```powershell
$key = "<ADMIN_API_KEY from .env>"
curl -X POST "http://127.0.0.1:8000/appointments/import" `
  -H "X-API-Key: $key" -F "file=@demo_data/appointments_demo.xlsx"
```

Expected: `{"imported":1,"skipped":[]}`.

**3. List what is pending.**

```powershell
curl "http://127.0.0.1:8000/appointments?status=pending" -H "X-API-Key: $key"
```

**4. Call one patient now** (ignores calling hours and the 24 h window — this is the testing path):

```powershell
curl -X POST "http://127.0.0.1:8000/appointments/1/call-now" -H "X-API-Key: $key"
```

Expected: `{"appointment":1,"call_placed":true}`, then a real phone call, then the appointment row
moves to `confirmed` / `reschedule` / `cancelled` / `unconfirmed` (or back to `pending` after
`wrong_person`, then `no_answer` when retries run out).

**5. Check the result and the recording.**

```powershell
docker exec -it orthopedic-db psql -U appuser -d orthopedic_voice `
  -c "SELECT id, patient_name, status, attempts, feeling_note, recording_url FROM appointments ORDER BY id DESC LIMIT 5;"
```

**6. Let the scheduler do it.** With `OUTBOUND_ENABLED=true` and a visit inside the window, the job
runs every `REMINDER_POLL_MINUTES`. Watch the log for `voice.reminders`. Set
`OUTBOUND_ENABLED=false` when you are finished testing so it does not call patients by accident.

> Testing tip: use your own number as the patient's number, and remember the call is **recorded**
> (the greeting says so).

---

## 15. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `GET /v1/models 404` every 30–60 s | OpenCode's vLLM auto-discovery probing port 8000 | add `"plugins": ["-opencode.provider.vllm"]` to `~/.config/opencode/opencode.json` |
| `HEAD /https%3A//...` 404 | internet scanner hitting the public tunnel | harmless; ignore |
| `403 Invalid Plivo signature` | `PUBLIC_BASE_URL` doesn't match the real URL | fix the URL or set `PLIVO_VALIDATE_SIGNATURE=false` |
| Call connects but no audio | wrong stream format, or `PUBLIC_BASE_URL` not `wss`-able | check `Unexpected audio format` log; verify the tunnel |
| Bot silent, no error | TTS failing | check `❌ [Voice] Text-to-speech failed`; check Sarvam key/credits |
| Caller never understood | VAD too strict | lower `VAD_RMS_THRESHOLD` (e.g. 300) |
| Bot talks over the caller | barge-in threshold | lower `BARGE_IN_MS` |
| Replies in the wrong language | `DEFAULT_LANGUAGE` or mixed STT output | check `🌐 [Voice] Call language:` log |
| Plivo says "application error" | server not reachable | is uvicorn running? is ngrok alive? is the URL current? |
| `ModuleNotFoundError: twilio` | stale virtualenv | `pip install -r requirements.txt` |
| `ModuleNotFoundError: websockets` | stale virtualenv | `pip install -r requirements.txt` |
| Health 503 | PostgreSQL down | `docker start orthopedic-db` (local) or check the RDS instance (production) |
| Callback saved twice | LLM called the tool twice | see Known issues |
| Startup fails on data files | invalid JSON or missing reply key | fix `data/*.json`; the error names the file |
| `503 Admin API is off` | `ADMIN_API_KEY` is empty | set it in `.env` and restart |
| `401` on `/appointments/*` | wrong or missing `X-API-Key` header | use the exact `ADMIN_API_KEY` value |
| Import says every row skipped | headings not recognised, dates in the past, or duplicate | use the headings in section 10.4; check the `reason` in the response |
| Import works, nothing is ever called | `OUTBOUND_ENABLED=false`, outside calling hours, or the visit is outside the window | check the three settings and `voice.reminders` logs |
| Reminder call placed but greeting is silence | `PUBLIC_BASE_URL` wrong, so Plivo cannot fetch `/voice/plivo/outbound/answer` | fix `.env`; the URL must be the public one |
| Reminder call answers but the bot uses the wrong name | the appointment row has the old name | `python scripts/set_patient_name.py "Correct Name" --id N` |
| Patient says "wrong person" but status stays `calling` | the call never reached the outcome tool, or the row is stuck | `housekeeping()` unsticks rows after 20 min; or rerun `call-now` |
| Reminder stuck in `calling` forever | the process died mid-call | housekeeping resets it after 20 minutes; no action needed |
| No recording URL | recording not enabled on the Plivo account, or the callback could not reach the server | check the `voice.outbound` logs and the public URL |

Production-specific problems (the live server, not a laptop):

| Symptom | Likely cause | Fix |
|---|---|---|
| `ssh: connect to host ... port 22: Connection timed out` | the security group still allows only an old public IP | update the SSH rule source to your current IP, or to your ISP's range |
| `ModuleNotFoundError: No module named 'app.voice'` | `.dockerignore` contains a bare `voice/` line, which removed `app/voice/` from the image | change it to `/voice_sarvam/` and rebuild |
| `UndefinedTable: relation "call_logs" does not exist` | the app started before `create_all` could run, or an old image is running | rebuild (`docker compose up -d --build`); check that a `Database initialized` line appears |
| Code changes have no effect | `docker compose up -d` without `--build` | always `docker compose up -d --build` |
| Call drops at almost exactly 60 s | Nginx default read timeout closes the WebSocket | `proxy_read_timeout 3600s` in the site file, then reload Nginx |
| `502 Bad Gateway` from Nginx | the `agent` container is down or still starting | `docker compose ps`, then `docker compose logs --tail 50 agent` |
| TTS cache empty after every rebuild | the volume is missing | compose already mounts `./.cache:/app/.cache`; confirm with `docker compose config` |
| `429 Too Many Requests` during startup | the prewarm hits the Sarvam rate limit | normal; it retries and completes |
| Greeting is silent but the call connects | `PUBLIC_BASE_URL` is not the real HTTPS domain | fix `.env` and restart the container |
| `Public Key Retrieval is not allowed` / SSL error | RDS URL missing `?sslmode=require` | add it |
| `could not connect to server` at startup | RDS security group does not allow the EC2 instance | allow 5432 from the instance's security group, not from the internet |
| Reminder calls never start on the server | `OUTBOUND_ENABLED=false` in the deployed `.env`, or the container was not restarted | edit `.env`, `docker compose up -d --build` |

---

## 16. Known issues and TODO

> Re-checked on 19 November 2026, after the outbound reminder feature and the RDS migration.

### Fixed since the previous edition

| Was | Now |
|---|---|
| `twilio` and `websockets` missing from `requirements.txt`; a clean install crashed at startup | both listed, plus `apscheduler`, `openpyxl`, `alembic` |
| no `.dockerignore`, so `.env`, `.venv` and `.cache` were baked into the image | `.dockerignore` added |
| `docker-compose.yaml` was empty | one `agent` service with a cache volume (the database moved to RDS) |
| `LOG_TRANSCRIPTS=true` wrote patient speech into the log | `false` in the deployed `.env` |
| tables were never created, so a fresh database silently saved nothing | `Base.metadata.create_all(engine)` runs in `main.py` startup |
| the database had no backups and shared the app server's disk | moved to **Amazon RDS** with automated snapshots (section 18) |
| the TTS cache was lost on every rebuild | `./.cache:/app/.cache` volume in compose |
| the greeting promised recording but nothing was recorded | reminder calls really record (Plivo `calls.record`); the inbound greeting still over-promises — see below |
| no way to call patients proactively | outbound reminder graph, scheduler, Excel import and staff API |
| most API keys removed from `config.py` defaults | keys now default to empty; the DB password and doctor phone are still hardcoded (see Critical) |

### Critical (still open)

1. **`app/config.py` still hardcodes the database password and the doctor's phone number** as
   defaults, and those values are in git history. The API keys now default to empty. Remove these
   two defaults, rotate the RDS password, and set both only in `.env`.
2. **`request_callback` lost its `reason` parameter**, but `tools.py`'s docstring, `prompts.py` and
   `callback_reply` still use it. Result: `medical_advice_decline` is never chosen, and the LLM may
   try to pass an argument that no longer exists.
3. **`/chat/*` has no authentication** and always replies in Odia (never calls `set_reply_language`).
4. **The `.env` used for local development has a leading space** in
   `PUBLIC_BASE_URL= https://...`. pydantic keeps the space, so Plivo builds URLs with `%20` and
   signature checks fail. Strip it. (Not a code bug, but it silently breaks the next person.)
5. **Twilio cannot place reminder calls.** `TwilioAdapter` has no `make_call`/`start_recording`, and
   its `stream_xml()` signature does not match the base class. Outbound is Plivo-only.

### High

6. `verify_identity` does not increment `verify_attempts` when the caller gives no name/DOB, so a
   caller who refuses to identify can loop there forever and never reach `assistant`.
7. `assistant()` uses `get_llm()` instead of `get_agent_llm()`, so `REPLY_MAX_TOKENS` is ignored.
8. `trim_messages(token_counter=len, max_tokens=30)` counts messages, not tokens, and can cut an
   assistant tool call away from its tool result (OpenAI 400 on long calls). The reminder graph has
   the same construct.
9. Webhook signature validation is off in production (`PLIVO_VALIDATE_SIGNATURE=false`), and the
   Twilio WebSocket check can never pass as written.
10. The **inbound** greeting says the call "may be recorded", but only reminder calls are recorded.
11. `notify_doctor()` only writes a log line — the doctor is never actually alerted, for emergencies
    either.
12. The outbound scheduler is **enabled in development** (`OUTBOUND_ENABLED=true`). Starting the app
    on a laptop with a pending row can place a real call. Default it to `false` while testing.
13. The reminder greeting is not in `fixed_replies.json`, so it is re-synthesised on the first call
    after each restart (one live TTS request before audio).

### Medium / cleanup

14. Dead commented-out code: large blocks in `voice/session.py` (`_on_start`, `_greet`,
    `_handle_turn`, `_finish`), `speech/factory.py` (the first ~150 lines), `main.py`, `llm.py`, and
    `bodhan_tts.py`.
15. Dead or broken modules: `telephony/twilio/factory.py` (wrong settings names and constructor),
    `services/clinic_service.py` (reads a setting that doesn't exist), `routes/v1/health.py`
    (never mounted), and three scripts with broken imports (`make_voices.py`, `test_speech.py`,
    `test_voices.py`).
16. Duplicated helpers in `bodhan_stt.py` / `bodhan_tts.py` that already exist in `text_utils.py`.
17. `.env.example` is stale: no Sarvam keys, no `ADMIN_API_KEY`, no `OUTBOUND_*` / `REMINDER_*`.
18. `migrations/` is empty and there is no `alembic.ini`, although `alembic` is now a dependency.
    `create_all` never alters existing tables, so column changes need manual SQL.
19. `.cache/`, `demo_data/` and the `voice_*/` WAV folders are not git-ignored.
20. No tests at all (no `pytest`, no CI) — including no test that the reminder status machine
    (`pending → calling → confirmed/…`) behaves.
21. `requirements.txt` is fully unpinned.
22. `README.md` describes only Phase 1 (chat), says Twilio is planned, and mentions none of the
    reminders, RDS or staff API.
23. `app/scripts/simulate_call.py` only simulates the **inbound** stream; there is no local simulator
    for a reminder call.
24. `scripts/make_demo_sheet.py` contains a real-looking phone number; it must be edited before use.

### Suggested next steps

1. Remove the hardcoded DB password and doctor phone from `app/config.py`; rotate the RDS password.
2. Fix the `reason` mismatch (add the parameter back, or remove all references).
3. Add auth (a shared secret header) to `/chat/*` and set `set_reply_language("en")` there.
4. Fix `verify_identity` to count "no details given" as an attempt.
5. Switch `assistant()` to `get_agent_llm()` and use a real token counter in `trim_messages`.
6. Enable signature validation once a real call works, and fix the leading space in `.env`.
7. Default `OUTBOUND_ENABLED` to `false` and make the reminder scheduler opt-in.
8. Put the reminder greeting in `fixed_replies.json` (or pre-generate it at startup).
9. Add `make_call` / `start_recording` and the `extra` argument to the Twilio adapter if Twilio
   outbound is ever needed.
10. Adopt Alembic (`alembic init migrations`) so schema changes are versioned.
11. Add pytest tests for `rules.classify`, `patient_service.verify`, graph routing and the appointment
    status transitions.
12. Delete the dead code and broken modules; refresh `.env.example` and `README.md`.

---

## 17. Production deployment on AWS

The project is live. This section describes the machine it runs on, how the pieces are wired, and
how to operate it. It is written so the same machine can be rebuilt from scratch.

### 17.1 The machine

| Field | Value |
|---|---|
| Cloud | AWS EC2 |
| Region | `ap-southeast-2` — Sydney (`ap-southeast-2b`) |
| Instance | `i-01ecc5206bebc0341` — `t4g.small`, 2 vCPU, 2 GB RAM, **Arm64** |
| OS | Ubuntu Server 24.04 LTS |
| Disk | 30 GiB `gp3` |
| Public IP | `16.178.233.207` (Elastic IP — survives a stop/start) |
| Domain | `voice.indiahealthsystem.com` (GoDaddy A record, TTL 600) |
| SSH | `ssh voice` (alias in `~/.ssh/config` → `ubuntu@16.178.233.207`) |
| Security group | `orthopedic-sg` — 22 from a restricted source, 80 and 443 from anywhere |

**Why Sydney and not Mumbai.** The AWS Free plan is locked to `ap-southeast-2`; Mumbai
(`ap-south-1`) requires the paid plan. Mumbai is the better region for latency to Odisha and is a
planned migration, not a completed one.

**Why `t4g` (Arm).** Roughly 20% cheaper than the equivalent x86 `t3`, at the cost of having to
build the Docker image on the server (see 17.5).

### 17.2 What runs where

| Component | Runs as | Listens on |
|---|---|---|
| Nginx | host service | `0.0.0.0:80`, `0.0.0.0:443` (public) |
| `agent` — FastAPI/Uvicorn | Docker container | `127.0.0.1:8000` (not public) |
| PostgreSQL 16 | **Amazon RDS** (private subnet) | `5432`, reachable only from `orthopedic-sg` |
| Certbot renewal | host systemd timer | — |

Only **one** container runs on the EC2 instance now. Port 8000 is deliberately **not** opened in the
security group; Nginx is the only public entry point, so the app cannot be reached directly from the
internet.

### 17.3 The request path

```text
Caller dials +91 80317 72845
        │
        ▼
   Plivo  ── HTTPS POST /voice/plivo/answer ─────────────►  Nginx :443
        │                                                    │  (TLS, Let's Encrypt)
        │  ◄── XML that opens the audio stream ──────────────┘
        │
        ▼
   WebSocket /voice/plivo/stream  ──►  Nginx  ──►  agent container :8000
                                                     │
                            caller audio (8 kHz mu-law)
                                                     │
                        ┌────────────────────────────┼───────────────────────────┐
                        ▼                            ▼                           ▼
                  Sarvam STT                 LangGraph agent                Sarvam TTS
             (speech → text)                 (OpenAI decides)            (text → mu-law)
                        └────────────────────────────┼───────────────────────────┘
                                                     ▼
                                          Amazon RDS (PostgreSQL :5432)
                                     patients · call_logs · callbacks · summaries · appointments
```

Outbound reminder (the other direction):

```text
APScheduler in the agent container
        │ claims a due appointment
        ▼
  Plivo REST calls.create ──► patient's phone
        │ answers
        ▼
  HTTPS POST /voice/plivo/outbound/answer?appt=ID ──► Nginx ──► agent
        │  ◄── <Stream> ws://.../stream?mode=reminder&apptId=ID
        ▼
  same WebSocket pipeline, reminder graph, recording started over REST
```

### 17.4 What `docker-compose.yaml` does

One service:

| Service | Image / build | Purpose |
|---|---|---|
| `agent` | `build: .` | the application; reads `.env`; mounts `./.cache` for the TTS cache |

```yaml
services:
  agent:
    build: .
    container_name: agent
    restart: always
    env_file:
      - .env
    ports:
      - "127.0.0.1:8000:8000"
    volumes:
      - ./.cache:/app/.cache
```

`restart: always` means a server reboot brings the container back without help. There is **no `db`
service** any more; PostgreSQL is on RDS and is reached through `DATABASE_URL`. The bind mount keeps
the TTS cache on the host, so a rebuild no longer re-synthesises the fixed lines.

### 17.5 Why the image is built on the server

| Build location | Time | Result |
|---|---|---|
| Laptop (x86_64) targeting this Arm64 host | ~9 minutes | needs QEMU emulation, fragile |
| Server (native Arm64) | ~60 seconds | correct architecture, layer cache |

So the laptop uploads **source**, and the server builds the **image**:

```bash
cd /opt/orthopedic
docker compose up -d --build
```

`docker compose up -d` **without** `--build` silently reuses the previous image, which makes code
changes look like they had no effect. This is the most common deployment mistake on this project.

### 17.6 The Nginx site

The file lives at `/etc/nginx/sites-available/voice` and is symlinked into `sites-enabled/`. The
default site is removed.

```nginx
map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}

server {
    listen 80;
    server_name voice.indiahealthsystem.com;

    location / {
        proxy_pass         http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header   Upgrade $http_upgrade;
        proxy_set_header   Connection $connection_upgrade;
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;
        proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }
}
```

Two things matter more than the rest:

- `proxy_http_version 1.1` + the two `Upgrade` headers — without them the Plivo WebSocket never
  upgrades and there is no audio.
- `proxy_read_timeout 3600s` — the default 60 s kills the stream mid-call. A call that drops at
  almost exactly one minute is this line missing.

### 17.7 TLS

```bash
sudo certbot --nginx -d voice.indiahealthsystem.com
```

Certbot rewrites the site to add `listen 443 ssl`, installs the certificate, and adds the
HTTP → HTTPS redirect. The certificate is valid until **13 February 2027** and renews
automatically via `certbot.timer`.

```bash
sudo certbot certificates          # what is installed
systemctl status certbot.timer     # is renewal scheduled
```

### 17.8 Plivo configuration

| Setting | Value |
|---|---|
| Application → Answer URL | `https://voice.indiahealthsystem.com/voice/plivo/answer` |
| Method | `POST` |
| Hangup URL | *(empty)* — outbound calls pass their own |
| Phone number | `+918031772845`, attached to that application |
| Outbound caller ID | the same `PLIVO_NUMBER` (set in `.env`) |
| Outbound answer URL | `https://voice.indiahealthsystem.com/voice/plivo/outbound/answer?appt=ID` (built by the code) |
| Outbound hangup URL | `https://voice.indiahealthsystem.com/voice/plivo/outbound/hangup?appt=ID` |
| Recording callback | `https://voice.indiahealthsystem.com/voice/plivo/outbound/recording?appt=ID` |

Make sure the Plivo number is enabled for **outbound** calls and that the account can terminate to
Indian mobiles (DND permissions). Inbound can work perfectly while outbound is blocked.

`PLIVO_VALIDATE_SIGNATURE` is currently **`false`**. Turn it on only after a successful call, and
only once `PUBLIC_BASE_URL` is guaranteed correct — otherwise Plivo gets a 403 and the call fails
before the greeting. Remember the outbound webhooks are signed too.

### 17.9 Deploying a change

```powershell
# laptop
cd C:\Users\TUF\Freelance
tar -czf ortho.tar.gz --exclude=.venv --exclude=.git --exclude=.cache `
  --exclude=__pycache__ --exclude=voice_notes --exclude=voice_samples `
  --exclude=voice_sarvam -C Orthopedic-AI-Voice-Agent .
scp ortho.tar.gz voice:/home/ubuntu/
```

```bash
# server
cd /opt/orthopedic
tar -xzf /home/ubuntu/ortho.tar.gz
docker compose up -d --build
docker compose logs -f agent
```

> **Excluding `voice_sarvam` is not optional** — it holds hundreds of megabytes of sample audio.
> `app/voice/` is source code and must be uploaded. A `.dockerignore` line of `voice/` (without the
> leading slash and without `_sarvam`) removes `app/voice/` from the image and the container dies
> with `ModuleNotFoundError: No module named 'app.voice'`.

### 17.10 Day-2 commands

```bash
ssh voice                       # enter the server
cd /opt/orthopedic              # project directory

docker compose ps               # is the agent container healthy?
docker compose logs -f agent    # live logs (Ctrl+C to exit)
docker compose logs --tail 100 agent
docker compose restart agent    # restart without rebuilding
docker compose down             # stop everything
docker compose up -d            # start everything

df -h /                         # disk
docker stats --no-stream        # memory
```

Database (RDS — no `db` container, so `docker exec db ...` no longer works):

```bash
psql "host=<rds-endpoint> dbname=orthopedic_voice user=appuser sslmode=require" -c "\dt"
psql "... " -c "SELECT id, patient_name, appointment_at, status FROM appointments ORDER BY id DESC LIMIT 10;"
```

Reminder operations:

```bash
docker compose logs --tail 200 agent | grep -i reminder   # scheduler activity
curl -s https://voice.indiahealthsystem.com/health
```

### 17.11 Measured performance

From a real call on this machine:

| Stage | Time |
|---|---|
| Greeting first audio | 0.0 s (served from the TTS cache) |
| Speech-to-text | 0.6 – 1.4 s |
| Agent (LLM) | 1.3 – 2.2 s |
| First reply audio after the caller stops speaking | 3.8 – 5.7 s |

Most of the delay is the Sydney ↔ India round trip plus the LLM turn. Moving the machine to Mumbai
should remove roughly 100–150 ms from every external hop.

A reminder call has the same STT/LLM/TTS timings, plus:

| Stage | Time |
|---|---|
| Scheduler pass → Plivo `calls.create` | < 1 s |
| Phone ringing → patient answers | network-dependent |
| First reminder greeting audio | one live TTS request on the first call after a restart (not cached) |
| Recording start (`calls.record`) | background, does not delay audio |

The recording upload happens on Plivo's side; the app only stores the URL when the recording
callback arrives.

### 17.12 Cost

| Item | Monthly |
|---|---|
| EC2 `t4g.small` | $15.48 |
| EBS 30 GiB `gp3` | $2.90 |
| Elastic IP | $0.00 (free while attached to a running instance) |
| Let's Encrypt | $0.00 |
| Amazon RDS `db.t4g.micro` + 20 GiB | ≈ $14 |
| **Total today** | **≈ $32** |

Reminder calls add Plivo per-minute charges for the outbound leg and recording storage; both are
small at this volume but are not free.

The account runs on AWS credits; when they run out, this becomes real money.

---

## 18. The Amazon RDS database (migration completed)

The database now lives in **Amazon RDS**. This section records what was done, why, and how to
repeat it on a fresh account.

### 18.1 Why it was moved

The old database was a Docker container on the application server:

- **no automated backups** — a corrupted volume or a deleted instance loses every call record;
- **it competed for RAM** with the app on a 2 GB machine;
- **it could not be resized independently** — growing the database meant growing the whole server;
- **it was a single point of failure** shared with the app: rebuilding the instance meant restoring
  `/opt/pgdata` by hand.

RDS gives automated daily backups with point-in-time restore, managed patching, and a database that
survives the app server being replaced.

### 18.2 What changed

| | Before | Now |
|---|---|---|
| Database host | container `db` on the EC2 instance | `<name>.<id>.ap-southeast-2.rds.amazonaws.com` |
| `DATABASE_URL` | `...@db:5432/orthopedic_voice` | `...@<rds-endpoint>:5432/orthopedic_voice?sslmode=require` |
| `db` service in compose | ran PostgreSQL | **removed** — compose has only `agent` |
| Backups | none | automated RDS snapshots |
| Data location | `/opt/pgdata` on the instance | managed by AWS |
| Application code | — | **no changes** (only `.env`) |

`docker-compose.yaml` is now:

```yaml
services:
  agent:
    build: .
    container_name: agent
    restart: always
    env_file:
      - .env
    ports:
      - "127.0.0.1:8000:8000"
    volumes:
      - ./.cache:/app/.cache
```

### 18.3 The instance

| Setting | Value |
|---|---|
| Engine | PostgreSQL **16** (same major version as the old container) |
| Instance class | `db.t4g.micro` (2 vCPU, 1 GB) — enough for this workload |
| Storage | 20 GiB `gp3`, autoscaling on |
| Region / AZ | `ap-southeast-2`, same VPC as the EC2 instance |
| Public access | **No** |
| VPC security group | `rds-sg` — inbound 5432 **from `orthopedic-sg` only** |
| Initial database name | `orthopedic_voice` |
| Master username | `appuser` |
| Automated backups | 7 days |
| Deletion protection | **on** |

### 18.4 How the migration was done

A dump/restore, taken at a quiet moment (a call in progress can miss one row; nothing is corrupted).

```bash
# 1. dump the old container database (inside the container, so no client tools are needed on the host)
docker exec db pg_dump -U appuser -d orthopedic_voice -Fc -f /tmp/ortho.dump

# 2. keep a copy on the server, outside Docker
docker cp db:/tmp/ortho.dump /opt/ortho-backup-$(date +%F).dump

# 3. restore into RDS (the old db container had pg_restore; the app image does not)
docker exec db pg_restore \
  -h <rds-endpoint> -U appuser -d orthopedic_voice \
  --no-owner --no-privileges /tmp/ortho.dump
```

If you are starting clean instead of copying data, skip step 3: the app creates the app tables
itself (`Base.metadata.create_all` in `main.py`), and the checkpointer creates its tables through
`checkpointer.setup()`. Both happen on the first successful startup against the empty RDS database.

Then switch the application over:

```bash
# 1. edit .env
nano /opt/orthopedic/.env
#    DATABASE_URL=postgresql+psycopg://appuser:<password>@<rds-endpoint>:5432/orthopedic_voice?sslmode=require

# 2. restart the app (configuration change, no rebuild needed)
docker compose up -d agent

# 3. verify
docker compose logs --tail 40 agent
```

A healthy start shows `✅ Database initialized`, then `✅ Data files loaded`, `✅ Agent graph ready`
and `✅ Reminder graph ready`.

### 18.5 Cutover checklist (as executed)

- [x] RDS instance is `Available` and in the same VPC
- [x] `rds-sg` allows 5432 from `orthopedic-sg` only
- [x] RDS is reachable from the EC2 host
- [x] all app tables (`patients`, `call_logs`, `callbacks`, `call_summaries`, `appointments`) **and**
      the `checkpoint*` tables are present
- [x] `.env` updated, `sslmode=require` included
- [x] `agent` restarted and logs show a successful connection
- [x] a test call saves rows: `select * from call_logs order by id desc limit 3;`
- [x] a reminder call saves an appointment outcome
- [x] the old `db` container stopped

If you are doing this again: **keep the old container and `/opt/pgdata` for at least a week.**

### 18.6 Rollback

The rollback is one line, which is why the old container and `/opt/pgdata` are kept:

```bash
nano /opt/orthopedic/.env     # point DATABASE_URL back at @db:5432
docker compose up -d agent
```

Anything written to RDS after the switch is not in the container. If that matters, dump RDS first
and restore it into the container before switching back.

### 18.7 Cost

| Item | Monthly |
|---|---|
| `db.t4g.micro`, single-AZ | ≈ $13 |
| 20 GiB gp3 storage | ≈ $2 |
| Automated backups (under 20 GiB) | $0 |
| **Increase** | **≈ +$15** |
| New total (EC2 + RDS) | **≈ $32** |

### 18.8 Gotchas

- **RDS starts empty.** The app now creates its tables automatically, so this is no longer a
  manual step — but a *restored* database must include the `checkpoint*` tables, or in-flight call
  memory is lost. `pg_dump` includes them; `create_all` does not, the checkpointer does.
- **Use the `postgresql+psycopg://` driver and `?sslmode=require`.** Without SSL, RDS refuses the
  connection.
- **Do not make RDS public** to "test it quickly". Reach it from the EC2 host over the private
  network instead.
- **Match the engine version.** Dumping from PostgreSQL 16 and restoring into 15 fails.
- **RDS has no `postgres` superuser.** `pg_restore --no-owner --no-privileges` avoids ownership
  errors during the restore.
- **`docker exec db psql ...` no longer works in production** — there is no `db` container. Use a
  `psql` client or a bastion/SSM session.
- **The RDS password lives only in `.env`.** It is not in `docker-compose.yaml` any more; keep it
  out of git.

---

## 19. Handover checklist

For whoever picks this up next. Read sections 1, 6, 11, 17 and 18 in that order.

### 19.1 Access you need

| System | What for |
|---|---|
| AWS console (account that owns the instance) | EC2, security groups, RDS, billing |
| SSH key `orthopedic-key.pem` | `ssh voice` |
| GoDaddy DNS for `indiahealthsystem.com` | the `voice` A record |
| Plivo console | Answer URL, phone number, call logs, recordings |
| Sarvam dashboard | speech API key and usage |
| OpenAI dashboard | LLM key and usage |
| RDS credentials | the `appuser` password (in `.env` on the server) |

### 19.2 Where everything lives

| Thing | Location |
|---|---|
| Application source | `/opt/orthopedic` on the server; the same tree is in this repository |
| Secrets | `/opt/orthopedic/.env` (never committed) |
| Database | **Amazon RDS** (section 18) |
| Appointment sheet | uploaded through `/appointments/import`; rows in the `appointments` table |
| Call recordings | URLs stored in `appointments.recording_url`; audio lives in Plivo |
| TTS cache | `/opt/orthopedic/.cache` on the host, mounted into the container |
| Nginx site | `/etc/nginx/sites-available/voice` |
| TLS certificate | `/etc/letsencrypt/live/voice.indiahealthsystem.com/` |
| Logs | `docker compose logs agent`, `/var/log/nginx/` |

### 19.3 Is it healthy?

```bash
ssh voice
docker compose ps                                    # agent Up
curl -I https://voice.indiahealthsystem.com/health    # HTTP/2 200
docker compose logs --tail 20 agent                   # no tracebacks
docker compose logs --tail 100 agent | grep -i reminder
```

### 19.4 If the bot stops answering

1. `docker compose ps` — is `agent` up?
2. `docker compose logs --tail 100 agent` — a traceback names the real problem.
3. `curl -I https://voice.indiahealthsystem.com/health` — 502 means Nginx is fine but the container
   is not; 200 from inside but not outside means DNS or the security group.
4. Check RDS is `Available` and the security group still allows the EC2 instance.
5. Check the Plivo console → the number is still attached to the application.
6. Check the Sarvam and OpenAI dashboards for exhausted credits.

### 19.5 If reminder calls stop

1. `OUTBOUND_ENABLED=true` in the deployed `.env`?
2. Is the current IST time inside `OUTBOUND_START_HOUR`–`OUTBOUND_END_HOUR`?
3. Are there `pending` rows inside the window?
   `SELECT id, patient_name, appointment_at, status, attempts FROM appointments WHERE status='pending';`
4. Are rows stuck in `calling`? Housekeeping resets them after 20 minutes; `OUTBOUND_MAX_CONCURRENT`
   may be blocking new calls until then.
5. Do the logs show a Plivo error? A number not enabled for outbound looks like a silent failure.
6. `PUBLIC_BASE_URL` correct? If Plivo cannot fetch `/voice/plivo/outbound/answer`, the patient hears
   silence or the call drops immediately.

### 19.6 Open decisions

| Decision | State |
|---|---|
| Database on RDS | **done** (section 18) |
| Move the VM to Mumbai (`ap-south-1`) | planned; needs the paid AWS plan, would cut latency |
| Doctor-availability + transfer to the doctor's mobile | designed, **not implemented**; today only emergencies transfer |
| Receptionist fallback when the doctor does not answer | not implemented (`BACKUP_PHONE` exists in `.env`) |
| Inbound call recording | not implemented, although the inbound greeting says calls may be recorded |
| Reminder call recording | **implemented** (Plivo `calls.record`, URL saved on the appointment) |
| Signature validation on Plivo webhooks | still `false` |
| `/chat/*` authentication | not implemented — it is publicly reachable |
| Staff UI for appointments | not implemented; the API + Swagger are the interface today |

### 19.7 Things that will bite a new person

- **`docker compose up -d` without `--build`** — the image is stale and nothing you changed takes
  effect.
- **`.dockerignore`** — one wrong line removes `app/voice/` and the container will not start.
- **Port 8000 is closed on purpose** — test through `https://`, not by hitting the container.
- **The SSH security group rule follows your IP** — a new network means a connection timeout.
- **`PUBLIC_BASE_URL` must be the public HTTPS domain, with no leading space** — a stale ngrok URL
  or a stray space makes the greeting silent while everything else looks fine.
- **Reminders are live** — `OUTBOUND_ENABLED=true` plus a due appointment means a real phone call.
  Set it to `false` while experimenting.
- **`docker exec db psql` does not exist in production** — the database is on RDS.

---

## 20. Glossary

| Term | Meaning |
|---|---|
| **mu-law (ulaw)** | 8-bit phone audio encoding, 8 kHz — what Plivo/Twilio streams carry |
| **PCM** | uncompressed 16-bit audio samples |
| **VAD** | Voice Activity Detection — decides when the caller started/stopped speaking |
| **STT** | Speech-to-text |
| **TTS** | Text-to-speech |
| **Barge-in** | The caller talks over the bot, so the bot stops immediately |
| **Marker / checkpoint** | A message that tells us when previously sent audio finished playing |
| **DTMF** | Keypad tones |
| **Thread / thread_id** | LangGraph's key for one conversation; here it is the call id |
| **Checkpointer** | LangGraph's persistence layer for conversation state |
| **A-leg / B-leg** | The caller's leg / the doctor's leg of a transferred call |
| **ngrok** | Tool that exposes a local port through a public HTTPS URL |
| **E.164** | International phone format, e.g. `+919876543210` |
| **codemix** | Sarvam STT mode that keeps English words in English inside Indian-language speech |
| **Fixed line** | A pre-approved sentence from `fixed_replies.json`, cached as audio |
| **Callback** | A saved request for the doctor's team to call the patient back |
| **EC2** | Amazon's virtual-machine service — one rented Linux server |
| **Elastic IP** | A static public IPv4 address that stays attached across stop/start |
| **RDS** | Amazon Relational Database Service — managed PostgreSQL (section 18) |
| **Reverse proxy** | A server (Nginx here) that takes public traffic and forwards it to the app |
| **Docker Compose** | Runs the app container (and, historically, the database container) |
| **Container** | A packaged, isolated process; here the app runs in one |
| **APScheduler** | In-process job scheduler used for the reminder "cron" |
| **openpyxl** | Python library that reads the staff `.xlsx` appointment sheet |
| **Outbound call** | A call the system places to a patient (reminder), as opposed to an inbound call |
| **Reminder call** | The outbound conversation that confirms an appointment and records how the patient feels |
| **`appointments` row** | One visit: patient name, phone, time, language and status |
| **`claim_due()`** | The function that atomically picks due appointments and marks them `calling` |
| **Housekeeping** | Scheduler cleanup: expires too-close visits and unsticks rows left in `calling` |
| **`confirmed` / `reschedule` / `cancel` / `wrong_person`** | The four outcomes the reminder conversation can save |
| **`unconfirmed`** | The patient answered but no decision was saved |
| **`no_answer`** | All retry attempts were used without the patient answering |
| **`expired`** | Too close to the visit to remind any more |
| **Staff API** | `/appointments/*`, protected by the `X-API-Key` header (`ADMIN_API_KEY`) |
| **`extraHeaders`** | Plivo feature that carries small `key=value` tags (phone, mode, appointment id) into the stream |
