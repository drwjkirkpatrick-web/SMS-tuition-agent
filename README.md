# SMS-First School Tuition Agent

> Headless, SMS-only backend for small schools to automate tuition reminders, payment confirmations, and hardship request routing.

Built for **Raspberry Pi 4/5** and **NVIDIA Jetson Orin Nano Super** (ARM64).

**Repository:** [github.com/drwjkirkpatrick-web/SMS-tuition-agent](https://github.com/drwjkirkpatrick-web/SMS-tuition-agent)

---

## Quick Start

```bash
git clone https://github.com/drwjkirkpatrick-web/SMS-tuition-agent.git
cd sms-tuition-agent
cp .env.example .env  # fill in your Twilio credentials

docker compose up --build -d
```

Verify: `curl http://localhost:8000/health`

---

## Architecture

```
School SIS (CSV) → FastAPI API → PostgreSQL → Redis → Twilio → Parent Phone
                                      ↑         ↓
                                 Celery Workers (Beat scheduler)
```

| Component | Technology | Purpose |
|-----------|-----------|---------|
| Web Framework | FastAPI + Uvicorn | REST API, webhooks, admin endpoints |
| Task Queue | Celery + Redis | Background jobs (reminders, sends, reconciliation) |
| Database | PostgreSQL 16 | State, outbox, audit log |
| SMS | Twilio | Send/receive SMS |
| Scheduler | Celery Beat | Daily reminder computation |
| Deployment | Docker Compose | ARM64-ready containers |

---

## Key Features

- **Duplicate-proof delivery** — 12 defense layers prevent double SMS
- **Transactional outbox** — every send decision is persisted atomically
- **Idempotent provider sends** — Twilio deduplicates by our message key
- **Inbound keyword parsing** — PAID, STATUS, CALL, EXTENSION, HELP, STOP, START
- **Director-configurable policy** — reminder timing, quiet hours, tone
- **Audit trail** — every action logged immutably
- **FERPA-aware** — data minimization, masking, retention limits

---

## Project Structure

```
sms-tuition-agent/
├── prd/                # Product requirements
├── docs/               # Security policy, runbook, deployment guides
├── design/             # Architecture decisions (duplicate prevention)
├── api/                # FastAPI routers (webhooks, admin)
├── workers/            # Celery tasks (reminders, sends, reconciliation, inbound)
├── domain/             # Business logic (invoices, reminders, templates, outbox)
├── adapters/           # External integrations (Twilio, SIS CSV)
├── infra/              # Database, Redis, settings, audit logger
├── tests/              # Unit and integration tests
├── alembic/            # Database migrations
├── scripts/            # Utility scripts
└── deploy/             # ARM64 deployment configs
```

---

## Development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

pytest tests/unit/ -v          # run unit tests
pytest tests/integration/ -v   # run integration tests (needs DB)
ruff check .                   # lint
ruff format .                  # format
```

---

## License

MIT — see [LICENSE](LICENSE)
