# SMS-First School Tuition Agent

Headless, SMS-only backend for small schools to automate tuition reminders, payment confirmations, and hardship request routing.

## Quick Start (Raspberry Pi 4/5 or Jetson)

```bash
# 1. Clone
git clone https://github.com/YOURNAME/sms-tuition-agent.git
cd sms-tuition-agent

# 2. Configure
cp .env.example .env
# Edit .env with your Twilio credentials and timezone

# 3. Build and run
docker compose up --build -d

# 4. Verify
open http://localhost:8000/health
```

## Architecture

- **FastAPI** — web framework (async, high concurrency on ARM64)
- **Celery + Redis** — background task queue
- **PostgreSQL** — relational database (state, audit, outbox)
- **Twilio** — SMS provider (swappable adapter)

See `prd/` for requirements and `docs/` for security policy.

## Development

```bash
# Install locally (no Docker)
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Run tests
pytest

# Lint
ruff check .
ruff format .
```

## License

MIT — see LICENSE
