# Duplicate Prevention Architecture

**Document:** `design/duplicate-prevention.md`  
**Version:** 1.0  
**Purpose:** Explain the 12 layers of duplicate prevention for engineers.

---

## The Problem

Parents receiving duplicate tuition reminders destroys trust. Causes:

1. **Scheduler re-run:** Cron job runs twice because the previous instance didn't finish
2. **Worker restart:** Celery worker killed mid-send, message picked up by another worker
3. **Network timeout:** HTTP request to Twilio times out; did the message go through?
4. **Provider callback delay:** Twilio delivery receipt arrives late; worker retries prematurely
5. **Database rollback:** Transaction rolled back after message sent (impossible to prevent 100%, but minimized)

---

## The 12 Defense Layers

| Layer | Location | Mechanism | What It Stops |
|-------|----------|-----------|---------------|
| 1 | Scheduler | Deterministic message_key | Same reminder generating different keys |
| 2 | Database | UNIQUE(message_key) | Inserting duplicate rows |
| 3 | Scheduler | ON CONFLICT DO NOTHING | Duplicate scheduler runs |
| 4 | Outbox | Transactional insert | Orphaned messages (send without record) |
| 5 | Worker | SELECT ... FOR UPDATE SKIP LOCKED | Two workers claiming same message |
| 6 | Worker | Status = SENDING before send | Re-claiming mid-send messages |
| 7 | Provider | client_message_id = message_key | Provider-side deduplication |
| 8 | Provider | Idempotent API behavior | Twilio ignores duplicate client IDs |
| 9 | State machine | SENT → no retry | Sending a delivered message again |
| 10 | Reconciliation | Query before retry | Unknown delivery → confirm before retry |
| 11 | Webhook | provider_event_id UNIQUE | Duplicate delivery receipts |
| 12 | Suppression | Paid invoice → no candidates | Reminders for already-paid invoices |

---

## Layer-by-Layer Deep Dive

### Layer 1–3: Scheduler Side (Prevent Duplicates at Source)

```python
key = f"{school}:{student}:{guardian}:{invoice}:{type}:{due_date}:v1"
# Always the same for the same logical reminder

# Insert with database-level deduplication
INSERT INTO outbound_messages (message_key, ...)
VALUES (key, ...)
ON CONFLICT (message_key) DO NOTHING;
```

**Guarantee:** Running the scheduler 100 times on the same day produces the same key, and 99 of those inserts are silently ignored.

### Layer 4–6: Worker Side (Prevent Double-Processing)

```python
# Worker A claims a message
BEGIN;
SELECT * FROM outbound_messages
WHERE status = 'pending'
ORDER BY scheduled_at
FOR UPDATE SKIP LOCKED
LIMIT 1;

UPDATE outbound_messages SET status = 'sending' WHERE id = 42;
COMMIT;

# Worker B tries to claim the same message
# → Row is locked by Worker A, SKIP LOCKED causes B to skip it
# → B sees zero available messages and goes idle
```

**Guarantee:** Only one worker can send a message at a time.

### Layer 7–8: Provider Side (Twilio Deduplication)

```python
# Our client_message_id is the same as our message_key
adapter.send(
    to="+15551234567",
    body="Reminder...",
    client_message_id="1:201:101:1001:due_14:2026-05-15:v1",
)

# If we retry with the same client_message_id, Twilio returns
# the SAME Message SID instead of sending a second SMS.
```

**Guarantee:** Even if Layers 1–6 fail, Twilio won't send twice.

### Layer 9–10: Post-Send Safety (No Retries from Terminal States)

```
State Machine:
  pending → sending → sent → delivered
                        ↘ failed → pending (retry, if budget left)
                        ↘ unknown_delivery → reconcile → sent | failed | pending
```

A message in `sent` or `delivered` can NEVER transition back to `pending`. The reconciliation loop must query the provider before any retry from `unknown_delivery`.

### Layer 11: Webhook Deduplication

```python
# Twilio sends delivery receipt
INSERT INTO delivery_callbacks (provider_event_id, ...)
VALUES ('SM1234567890:delivered', ...)
ON CONFLICT (provider_event_id) DO NOTHING;
```

Twilio may send the same callback twice (network retry). The unique constraint ignores the second.

### Layer 12: Business Logic Suppression

```python
if invoice.status == InvoiceStatus.PAID:
    # Don't even create a candidate
    return []  # no reminders
```

Before any key is computed, check if the invoice is already paid.

---

## Failure Scenarios and Outcomes

| Scenario | What Happens | Result |
|----------|-------------|--------|
| Scheduler runs twice in one minute | Second run: all ON CONFLICT DO NOTHING | Zero duplicates |
| Worker crashes after `sending` | Message stays `sending`; reconciliation loop checks provider | Resolved, not duplicated |
| Worker crashes after Twilio accepts but before DB commit | Provider has message; DB has `sending` | Reconciliation finds `sent` |
| Twilio timeout (no response) | Worker marks `unknown_delivery` | Reconciliation loop queries |
| Twilio sends callback twice | Second callback: ON CONFLICT DO NOTHING | Status updated once |
| Parent pays during scheduler run | Suppression rule skips candidate | Zero duplicates |
| Two workers start simultaneously | FOR UPDATE SKIP LOCKED serializes | Only one worker sends |

---

## Testing Concurrency

See `tests/integration/test_duplicate_prevention.py` for:
- 3 workers racing for 10 messages → exactly 10 sends
- Scheduler run twice → no duplicate rows
- Worker killed mid-send → reconciliation resolves

---

## Monitoring

Alert if:
- `unknown_delivery` count > 50 (stuck messages)
- `sending` count > 10 for > 5 minutes (dead workers)
- `delivery_callbacks` with status `failed` rate > 5%
