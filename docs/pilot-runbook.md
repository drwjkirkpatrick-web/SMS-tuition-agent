# Pilot Runbook — SMS-First School Tuition Agent

**Version:** 1.0  
**Date:** 2026-05-02  
**Audience:** System administrators deploying to a single school

---

## Pre-Flight Checklist

Before going live with real parents, verify:

### Hardware
- [ ] Raspberry Pi 4/5 or Jetson Orin Nano Super with 4GB+ RAM
- [ ] 32GB+ microSD or NVMe SSD (for database durability)
- [ ] Reliable internet (Ethernet preferred over WiFi)
- [ ] UPS or battery backup (prevents corruption during power outage)

### Software
- [ ] Docker Engine 24.0+ installed
- [ ] Docker Compose plugin installed
- [ ] Git installed

### Twilio Account
- [ ] Twilio account created
- [ ] Phone number purchased and SMS-enabled
- [ ] `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_PHONE_NUMBER` configured
- [ ] Webhook URLs configured in Twilio console:
  - Status callback: `https://your-domain.com/webhooks/twilio/status`
  - Inbound SMS: `https://your-domain.com/webhooks/twilio/inbound`

### School Data
- [ ] `students.csv`, `guardians.csv`, `invoices.csv` exported from SIS
- [ ] CSV files placed in `/data/sis_exports/` on host
- [ ] Phone numbers in E.164 format (`+15551234567`)
- [ ] First-name-only for students (FERPA compliance)

### Security
- [ ] `.env` file created from `.env.example` (NEVER committed)
- [ ] `ADMIN_TOKEN_HASH` generated with bcrypt
- [ ] `BACKUP_ENCRYPTION_KEY` generated (32-byte hex)
- [ ] Database backups configured (cron job to encrypted USB)

---

## Deployment Steps

### 1. Clone Repository
```bash
cd /opt
git clone https://github.com/YOURNAME/sms-tuition-agent.git
cd sms-tuition-agent
cp .env.example .env
nano .env  # fill in all required values
```

### 2. Build and Start
```bash
docker compose up --build -d
```

### 3. Run Database Migrations
```bash
docker compose exec api alembic upgrade head
```

### 4. Verify Health
```bash
curl http://localhost:8000/health
```
Expected: `{"status":"healthy","db":"connected","redis":"connected"}`

### 5. Sync School Data
```bash
# Place CSV files in /data/sis_exports on host
docker compose exec api python -m scripts.sync_sis
```

### 6. Send Test SMS
```bash
# Trigger a test reminder for one guardian
docker compose exec api python -m scripts.send_test   --guardian-id 1 --template reminder_due_14
```

### 7. Verify Webhooks
- Send a test SMS to your Twilio number
- Check logs: `docker compose logs -f api`
- Verify inbound message appears in database

---

## Daily Operations

### Check Dashboard
```bash
curl -H "X-Admin-Token: YOUR_TOKEN" http://localhost:8000/admin/dashboard/stats
```

### View Queue
```bash
curl -H "X-Admin-Token: YOUR_TOKEN" http://localhost:8000/admin/queue/hardship
curl -H "X-Admin-Token: YOUR_TOKEN" http://localhost:8000/admin/queue/callback
```

### Monitor Workers
```bash
docker compose logs -f worker
docker compose logs -f beat
```

### Backup Database
```bash
docker compose exec db pg_dump -U sms_user sms_tuition > backup.sql
```

---

## Troubleshooting

### No reminders sent
1. Check scheduler: `docker compose logs -f beat`
2. Check invoices: `curl ... /admin/invoices`
3. Verify guardian opt-in: query `guardians.sms_opt_in`

### Duplicate messages
1. Check `outbound_messages` for duplicate `message_key` values
2. Verify `ON CONFLICT DO NOTHING` is working
3. Check worker logs for concurrent sends

### Messages stuck in `sending`
1. Check if worker crashed: `docker compose ps`
2. Reconciliation will resolve within 10 minutes
3. Manual fix: update status to `pending` and restart worker

### Webhook signature invalid
1. Verify `TWILIO_AUTH_TOKEN` matches Twilio console
2. Check URL in webhook config matches actual endpoint
3. Ensure no reverse proxy is rewriting the URL

### Parent texts not processed
1. Check `inbound_messages` table for new rows
2. Verify `workers.inbound` Celery task is running
3. Check keyword matching in `workers/inbound.py`

---

## Rollback Plan

If critical failure during pilot:

1. **Stop all sends:** `docker compose stop worker`
2. **Notify school director:** send manual email/SMS
3. **Export queue status:** `curl ... /admin/dashboard/stats > snapshot.json`
4. **Investigate:** check logs, database, webhook history
5. **Fix and resume:** restart workers after fix verified
6. **Post-mortem:** document in `docs/incidents/`

---

## Scaling Beyond 500 Students

- **Add workers:** `docker compose up -d --scale worker=4`
- **Use Redis Cluster:** for > 1000 concurrent tasks
- **PostgreSQL tuning:** increase `pool_size` to 20
- **Twilio Messaging Service:** for higher throughput and multiple numbers
