# SMS-First School Tuition Agent

> Headless, SMS-only backend for small schools to automate tuition reminders, payment confirmations, and hardship request routing. Built for **Raspberry Pi 4/5** and **NVIDIA Jetson** (ARM64).

**Repository:** [github.com/drwjkirkpatrick-web/SMS-tuition-agent](https://github.com/drwjkirkpatrick-web/SMS-tuition-agent)

---

## Quick Start

```bash
git clone https://github.com/drwjkirkpatrick-web/SMS-tuition-agent.git
cd SMS-tuition-agent
cp .env.example .env  # fill in your Twilio credentials

docker compose up --build -d
```

Verify: `curl http://localhost:8000/health`

**Production with TLS:**
```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

---

## Architecture

```
School SIS (CSV) → FastAPI API → PostgreSQL → Redis → Twilio → Parent Phone
                                      ↑         ↓
                                 Celery Workers (Beat scheduler)
                                      ↓
                        Maintenance (retention, alerts, backup)
```

| Component | Technology | Purpose |
|-----------|-----------|---------|
| Web Framework | FastAPI + Uvicorn | REST API, webhooks, admin endpoints |
| Task Queue | Celery + Redis | Background jobs (reminders, sends, reconciliation, maintenance) |
| Database | PostgreSQL 16 | State, outbox, audit log, dead letter queue |
| SMS | Twilio | Send/receive SMS with webhook validation |
| Scheduler | Celery Beat | Daily reminders, retention purge, alert checks, backups |
| Deployment | Docker Compose | ARM64-ready containers with production TLS override |

---

## Key Features

### Core (v1)
- **12-layer duplicate-proof delivery** — deterministic keys, DB ON CONFLICT, FOR UPDATE SKIP LOCKED, provider idempotency, webhook dedup, state machine, business-logic suppression
- **Transactional outbox** — every send decision persisted atomically
- **Inbound keyword parsing** — PAID, STATUS, CALL, EXTENSION, HELP, STOP, START
- **Director-configurable policy** — reminder timing, quiet hours, tone, max attempts
- **Audit trail** — every action logged immutably with PII masking
- **FERPA-aware** — data minimization, masking, retention limits

### v2 Improvements (30 Enhancements)

#### Efficiency (10)
| # | Improvement | Description |
|---|-------------|-------------|
| E1 | Bulk guardian loading | Single query vs per-row in reminder worker |
| E2 | Accurate insert/duplicate counting | RETURNING clause in dispatch service |
| E3 | TemplateRenderer in send worker | Real templates replace hardcoded strings |
| E4 | Twilio delivery query by SID | O(1) fetch instead of O(N) message scan |
| E5 | GROUP BY dashboard stats | 2 queries instead of 15+ COUNT queries |
| E6 | Redis-cached school policy | 5-min TTL, invalidated on save |
| E7 | Configurable DB pool sizing | Env vars for pool_size and max_overflow |
| E8 | JOIN guardian in outbox poll | Eliminates per-message guardian query |
| E9 | Composite + partial indexes | Faster common query patterns |
| E10 | Celery time limits | 300s hard / 240s soft prevents hung tasks |

#### Security (10)
| # | Improvement | Description |
|---|-------------|-------------|
| S1 | Rate limiting on admin API | 60 req/min per IP, Redis-backed, 429 + Retry-After |
| S2 | Enforce admin token at startup | Production refuses to start without ADMIN_TOKEN_HASH |
| S3 | Twilio webhook full algorithm | Sorts form params alphabetically per Twilio spec |
| S4 | Input sanitization for SMS | Strips control chars, caps at 1600 chars |
| S5 | Dynamic school_id resolution | No more hardcoded school_id=1 |
| S6 | CORS middleware | Configurable allowed origins |
| S7 | Transactional audit logging | Audit events share caller's session (rollback-safe) |
| S8 | TLS in production | docker-compose.prod.yml with SSL certificates |
| S9 | E.164 phone validation | Rejects invalid phone numbers on inbound |
| S10 | PII masking in logs | Structured logging filter auto-masks phone numbers |

#### Resilience (10)
| # | Improvement | Description |
|---|-------------|-------------|
| R1 | Persistent event loop | Replaces asyncio.run() per-call overhead |
| R2 | Worker health check | Celery inspect-based, Docker healthcheck-ready |
| R3 | Graceful shutdown | SIGTERM finishes current task, reject on worker lost |
| R4 | Dead letter queue | Poison messages moved to dedicated table with replay |
| R5 | Quiet hours enforcement | Send worker defers messages during quiet hours |
| R6 | Data retention purge | Daily job purges old sent/failed/callback/inbound records |
| R7 | Circuit breaker for Twilio | 5 failures → OPEN 60s → HALF_OPEN test → CLOSED |
| R8 | Reconciliation max-age | 72h cutoff, older messages marked FAILED |
| R9 | Failure threshold alerting | 20% failure rate triggers SMS alert to admin |
| R10 | Automated DB backup | Nightly encrypted pg_dump with 7+4 retention |

---

## Project Structure

```
SMS-tuition-agent/
├── prd/                        # Product requirements
├── docs/                       # Security policy, runbook, 30-improvements
├── design/                     # Architecture decisions (duplicate prevention)
├── api/                        # FastAPI routers
│   ├── main.py                 # App entry point, CORS, lifespan, admin router
│   ├── admin.py                # Dashboard stats, queue, invoices (rate-limited)
│   └── webhooks/twilio.py      # Status callbacks, inbound SMS (validated)
├── workers/                    # Celery tasks
│   ├── celery_app.py           # Config, beat schedule, time limits
│   ├── reminders.py            # Daily reminder computation (bulk-loaded)
│   ├── sends.py                # Outbox → SMS dispatch (quiet hours, circuit breaker)
│   ├── reconciliation.py       # Unknown delivery + payment sync (SID query, max-age)
│   ├── inbound.py              # Inbound keyword parsing and action dispatch
│   └── maintenance.py          # Retention purge, alert check, backup (R6/R9/R10)
├── domain/                     # Business logic
│   ├── models.py               # ORM models + DeadLetterMessage (R4)
│   ├── reminder_service.py     # Eligibility + message key computation
│   ├── dispatch_service.py     # Outbox insertion with accurate counts (E2)
│   ├── outbox.py               # State machine, row locking, reconciliation
│   ├── invoice_service.py      # Invoice lifecycle, payment recording
│   ├── reconciliation_service.py # Payment + delivery callback reconciliation
│   ├── templates.py            # SMS templates with segment counting
│   ├── policy_service.py       # Director policy with Redis cache (E6)
│   ├── hardship_service.py     # Hardship request lifecycle
│   ├── masking.py              # PII masking utilities
│   ├── dead_letter.py          # Dead letter queue service (R4)
│   ├── alerting.py             # Failure threshold alerting (R9)
│   ├── retention.py            # Data retention purge service (R6)
│   └── quiet_hours.py          # Quiet hours enforcement (R5)
├── infra/                      # Infrastructure
│   ├── database.py             # Async PostgreSQL (configurable pool, E7)
│   ├── redis_pool.py           # Async Redis client
│   ├── settings.py             # Pydantic settings (v2 env vars)
│   ├── audit_logger.py         # Transactional audit logging (S7)
│   ├── rate_limiter.py         # Redis rate limiter (S1)
│   ├── circuit_breaker.py      |  Twilio API circuit breaker (R7)
│   ├── logging_filter.py       # PII masking log filter (S10)
│   └── backup.py               # Encrypted DB backup utilities (R10)
├── adapters/                   # External integrations
│   ├── sms_adapter.py          # Abstract SMS adapter interface
│   ├── twilio_adapter.py       # Twilio (correct signature S3, SID query E4)
│   ├── mock_adapter.py         # Mock for testing
│   ├── csv_connector.py        # CSV SIS import
│   ├── sis_connector.py        # SIS connector interface
│   └── connector_factory.py    # Factory pattern
├── alembic/                    # Database migrations
│   └── versions/
│       ├── 001_initial_schema.py    # Initial 15 tables
│       └── 002_add_indexes_and_dead_letter.py # Indexes + dead letter (E9/R4)
├── tests/                      # Unit and integration tests
│   ├── unit/
│   │   ├── test_reminder_engine.py  # 30+ reminder logic tests
│   │   └── test_efficiency.py       # v2 efficiency improvement tests
│   └── integration/
│       └── test_duplicate_prevention.py # Concurrency + dedup tests
├── scripts/                    # Utility scripts
│   ├── sync_sis.py             # Manual SIS sync trigger
│   ├── health_check.py         # Worker health check (R2)
│   └── backup.py               # Backup runner (R10)
├── deploy/                     # ARM64 deployment guides
├── docker-compose.yml          # Development compose
├── docker-compose.prod.yml     # Production compose with TLS (S8)
├── Dockerfile                  # ARM64-optimized
├── requirements.txt            # Python dependencies
└── .env.example                # Environment variable template
```

---

## Development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install pytest pytest-asyncio

pytest tests/unit/ -v          # run unit tests
pytest tests/integration/ -v   # run integration tests (needs DB)
```

---

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `DATABASE_URL` | Yes | — | PostgreSQL async connection string |
| `REDIS_URL` | Yes | — | Redis connection string |
| `TWILIO_ACCOUNT_SID` | Yes | — | Twilio Account SID |
| `TWILIO_AUTH_TOKEN` | Yes | — | Twilio Auth Token |
| `TWILIO_PHONE_NUMBER` | Yes | — | Twilio phone number (E.164) |
| `WEBHOOK_SECRET` | Yes | — | Webhook signature secret |
| `APP_ENV` | No | development | development/staging/production |
| `ADMIN_TOKEN_HASH` | Prod | — | bcrypt hash of admin token |
| `DATABASE_POOL_SIZE` | No | 5 | SQLAlchemy pool size |
| `DATABASE_MAX_OVERFLOW` | No | 10 | Extra connections under load |
| `CORS_ALLOWED_ORIGINS` | No | localhost | Comma-separated CORS origins |
| `ADMIN_ALERT_PHONE` | No | — | Phone for failure alerts (E.164) |
| `BACKUP_OUTPUT_DIR` | No | /data/backups | Backup output directory |
| `BACKUP_ENCRYPTION_KEY` | Yes | — | 32-byte hex key for backup encryption |

---

## License

MIT — see [LICENSE](LICENSE)