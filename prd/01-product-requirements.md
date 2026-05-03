# SMS-First School Tuition Agent — Product Requirements Document (PRD)

**Version:** 1.0  
**Date:** 2026-05-02  
**Author:** Hermes (for Walker's friend)  
**Target Hardware:** Raspberry Pi 4/5, NVIDIA Jetson Orin Nano Super (ARM64)

---

## 1. What Are We Building?

A **headless, SMS-only backend service** that helps small private schools remind parents about upcoming tuition payments, confirm payments, and escalate hardship requests — **all via text message**.

> **Teaching note:** "Headless" means no website or app for parents. The only interface parents see is SMS. The school director interacts with the system through a lightweight web dashboard (Mission Control) or API calls.

---

## 2. Why SMS-First?

- **Reach:** Every parent has a phone number. No app installs, no passwords, no forgotten logins.
- **Reliability:** SMS works on $30 feature phones and in areas with weak data coverage.
- **Simplicity:** One communication channel reduces cognitive load for school staff and parents.
- **Cost:** For 200–500 students, SMS is cheaper than building and maintaining a parent portal.

---

## 3. Personas & User Stories

### Persona A: School Director (Mrs. Chen)
> Runs a K–8 Montessori school with 180 students. Not technical. Wants to stop chasing late payments manually.

**Stories:**
- As a director, I want tuition reminders sent automatically so I don't have to call 40 families each month.
- As a director, I want to see which reminders were delivered and which failed, so I know who to call personally.
- As a director, I want to configure reminder timing (e.g., remind 14 days, 3 days, and on due date), so families get gentle nudges without feeling harassed.
- As a director, I want parents who reply "CALL" or "EXTENSION" to be queued for my staff, so no request falls through cracks.

### Persona B: Parent / Guardian (Mr. Williams)
> Works shifts, checks phone during breaks. Doesn't use apps for school.

**Stories:**
- As a parent, I want to text "STATUS" and receive my current balance, so I know what I owe without calling the office.
- As a parent, I want to text "PAID" after sending a check or Venmo, so the school knows to expect it.
- As a parent, I want to text "EXTENSION" if I'm struggling this month, so I can request a plan without shame or paperwork.
- As a parent, I want to text "STOP" to opt out, and "START" to opt back in, so I control the messages I receive.

### Persona C: System Administrator (Walker's friend — YOU)
> Deploys and maintains the service on a Raspberry Pi or Jetson at the school or in the cloud.

**Stories:**
- As an admin, I want the system to run on ARM64 boards I already own, so I don't buy new hardware.
- As an admin, I want Docker Compose to start everything with one command, so deployment is repeatable.
- As an admin, I want logs and health checks, so I know if the SMS queue is backed up or the database is down.

---

## 4. Functional Requirements

| ID | Requirement | Priority |
|----|-------------|----------|
| FR-1 | Sync student, guardian, and invoice data from the school's SIS (Student Information System) | Must |
| FR-2 | Compute reminder eligibility daily based on invoice due dates and payment status | Must |
| FR-3 | Send tuition reminder SMS at configurable intervals before due date | Must |
| FR-4 | Send a single late notice after the due date if unpaid, with configurable cadence | Must |
| FR-5 | Send payment confirmation SMS when a payment is reconciled | Must |
| FR-6 | Parse inbound SMS keywords: `STATUS`, `PAID`, `CALL`, `EXTENSION`, `HELP`, `STOP`, `START` | Must |
| FR-7 | Queue `CALL` and `EXTENSION` requests for school staff with SLA timers | Must |
| FR-8 | Provide a read-only dashboard (Mission Control) showing queue status, delivery stats, and recent messages | Should |
| FR-9 | Allow director to configure reminder policies (timing, tone, quiet hours, max attempts) | Should |
| FR-10 | Generate monthly audit report of all messages sent, failed, and suppressed | Should |

> **Teaching note:** "Must" = MVP (Minimum Viable Product). "Should" = Phase 2. We will build all "Must" items in this project.

---

## 5. Non-Functional Requirements

| ID | Requirement | Rationale |
|----|-------------|-----------|
| NFR-1 | **No duplicate SMS delivery** — even if a worker restarts, network times out, or a callback is delayed | Trust destruction if parents get 3 identical reminders |
| NFR-2 | **Remote-area resilient** — must tolerate spotty internet, power outages, and delayed provider callbacks | Rural schools and home-based microschools |
| NFR-3 | **ARM64 compatible** — runs on Raspberry Pi 4/5 and Jetson Orin Nano Super without emulation | Target hardware |
| NFR-4 | **FERPA-aware** — minimize data collection, mask PII in logs, encrypt at rest, role-based access | Legal compliance for US schools |
| NFR-5 | **Idempotent operations** — safe to retry any job or API call without side effects | Reliable scheduling and recovery |
| NFR-6 | **Time-zone aware** — respect school's local timezone for "quiet hours" and due dates | Parents shouldn't get texts at midnight |
| NFR-7 | **Horizontal worker scaling** — add more Celery workers if queue grows | Support up to 500 students on one Pi, more with clustering |
| NFR-8 | **Audit trail** — every send, retry, suppression, and inbound message is logged immutably | Dispute resolution and transparency |

---

## 6. The "No Duplicate" Requirement (Expanded)

This is the **most critical** reliability requirement. Parents will lose trust immediately if they receive the same reminder twice.

### 6.1 Deterministic Message Key
Every intended reminder gets a unique key computed from:
```
key = school_id + student_id + guardian_id + invoice_id + reminder_type + due_date + policy_version
```
This means the same reminder for the same invoice to the same guardian will **always** generate the same key.

### 6.2 Database-Level Enforcement
- A `UNIQUE` constraint on the message key in the `outbound_messages` table.
- Duplicate insert attempts fail safely with `ON CONFLICT DO NOTHING`.

### 6.3 Transactional Outbox
- The reminder decision and the outbound message record are saved in **one database transaction**.
- If the transaction fails, no message is recorded and no SMS is sent.

### 6.4 Worker Row Locking
- Celery workers claim pending messages using `SELECT ... FOR UPDATE SKIP LOCKED`.
- Two workers cannot pick up the same message simultaneously.

### 6.5 Idempotent Provider Submit
- The SMS provider (e.g., Twilio) receives a `client_message_id` equal to our internal key.
- Retries reuse the same ID. Most providers ignore duplicate sends with the same client reference.

### 6.6 Unknown-Delivery Reconciliation
- If a send times out (network blip), the message is marked `UNKNOWN_DELIVERY` — **not** failed.
- A background reconciliation loop queries the provider: "Did you accept message XYZ?"
- Only if the provider says "no record" do we retry.

### 6.7 Delivery Callback Dedupe
- Provider webhooks (delivery receipts) are deduplicated by `provider_event_id` with a unique index.

### 6.8 Reminder Suppression
- Once an invoice is fully paid, all future reminders for that invoice are suppressed before they even reach the outbox.

---

## 7. System Boundaries (What's In / Out)

### In Scope (We Build)
- FastAPI REST API (internal + webhook endpoints)
- PostgreSQL database (all state, audit, queue)
- Redis (Celery broker + result backend + caching)
- Celery workers (reminders, sends, reconciliation, inbound parsing)
- Beat scheduler (cron-like job runner)
- SMS adapter layer (Twilio implementation)
- SIS adapter stub (CSV import + generic REST interface)
- Mission Control read-only dashboard (Next.js or plain HTML)
- Docker Compose deployment
- ARM64-ready images

### Out of Scope (We Don't Build)
- Native mobile app for parents
- Payment processing (Stripe / Square integration) — we reconcile payments, we don't collect them
- Full SIS integration (we provide a generic interface; each school builds their own adapter)
- Multi-tenancy at scale (assumes one school per deployment)
- Voice calls (SMS only; "CALL" means a human calls the parent)

---

## 8. Acceptance Criteria (Definition of Done)

### A. Reminder Accuracy
- Given an invoice due on May 15, the system sends reminders on May 1 (DUE_14), May 12 (DUE_3), and May 15 (DUE_TODAY).
- If the invoice is paid in full on May 10, no further reminders are sent.
- If the invoice is unpaid on May 16, one late notice is sent (configurable cadence).

### B. Duplicate Prevention
- Running the scheduler twice in the same day produces zero duplicate SMS messages.
- Restarting a Celery worker mid-send does not produce duplicates.
- A network timeout during send does not produce duplicates after reconciliation.

### C. Inbound Handling
- Texting "STATUS" returns the guardian's current unpaid balance within 30 seconds.
- Texting "PAID" creates a payment reconciliation task and sends an acknowledgment.
- Texting "EXTENSION" creates a staff-facing ticket with a 24-hour SLA.
- Texting "STOP" updates the guardian's preference and suppresses all future sends.

### D. Deployment
- `docker compose up` on a Raspberry Pi 4 starts all services in under 2 minutes.
- The system processes 500 students' reminders in under 5 minutes.
- Health check endpoint returns 200 when all services are healthy.

---

## 9. Glossary

| Term | Meaning |
|------|---------|
| **SIS** | Student Information System — the school's software for rosters, grades, billing |
| **Outbox** | Database table that holds "messages we intend to send" — workers read from here |
| **Idempotency** | Doing the same operation twice has the same effect as doing it once |
| **Celery** | Python task queue — handles background jobs like sending SMS |
| **Beat** | Celery's built-in scheduler that triggers periodic tasks |
| **Webhook** | HTTP callback from Twilio telling us "message delivered" or "message failed" |
| **Reconciliation** | Checking with the SMS provider to resolve ambiguous delivery states |
| **FERPA** | US law protecting student education records |
| **PII** | Personally Identifiable Information — names, phone numbers, addresses |
| **ARM64** | CPU architecture for Raspberry Pi and Jetson (not Intel x86) |

---

## 10. Architecture Sketch (High Level)

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│  School SIS     │────▶│  FastAPI API    │────▶│  PostgreSQL     │
│  (CSV/REST)     │     │  (Sync + Admin) │     │  (State + Outbox│
└─────────────────┘     └─────────────────┘     └─────────────────┘
                               │                       │
                               ▼                       ▼
                        ┌─────────────┐         ┌─────────────┐
                        │  Celery     │◀────────│  Redis      │
                        │  Workers    │         │  (Queue)    │
                        └─────────────┘         └─────────────┘
                               │
                               ▼
                        ┌─────────────┐
                        │  Twilio     │
                        │  (SMS)      │
                        └─────────────┘
                               │
                               ▼
                        ┌─────────────┐
                        │  Parent     │
                        │  Phone      │
                        └─────────────┘
```

> **Teaching note:** This is a "pipes and filters" architecture. Data flows in one direction: SIS → API → Database → Queue → SMS Provider → Parent. Webhooks flow back: Provider → API → Database. Each layer is isolated and can be tested independently.

---

## Next Steps

1. **Step 2:** Security & FERPA policy document
2. **Step 3:** Scaffold the project structure with Docker Compose (ARM64)
3. **Step 4–20:** Implement each layer with tests

---

*End of PRD — Ready for Step 2 when you give the word.*
