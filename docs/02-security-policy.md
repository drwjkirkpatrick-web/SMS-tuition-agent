# Security, Privacy, and FERPA Policy

**Version:** 1.0  
**Date:** 2026-05-02  
**Applies To:** SMS-First School Tuition Agent (all deployments)  
**Audience:** Developers, School Directors, System Administrators

---

## 1. Why This Document Exists

Schools handle **education records** protected by FERPA (Family Educational Rights and Privacy Act). This system processes student names, guardian contact information, and financial data. A single leaked text log or misconfigured database could expose sensitive family information and create legal liability.

This policy tells you:
- What data we collect (minimum necessary)
- How we protect it (technical controls)
- Who can access it (role-based access)
- How long we keep it (retention limits)
- What happens when things go wrong (incident response)

---

## 2. Data Minimization Principle

### 2.1 What We Store

| Data Element | Why Needed | FERPA Classification |
|--------------|-----------|---------------------|
| Student first name | Reminder personalization ("Hi, this is a reminder for Emma's tuition...") | Education record |
| Guardian phone number | SMS delivery target | PII (not education record, but sensitive) |
| Invoice amount and due date | Compute reminder eligibility | Financial record (school business record) |
| Payment status (paid/unpaid/partial) | Suppress reminders, send confirmations | Financial record |
| Message content (template-rendered) | Audit trail, dispute resolution | Communication record |
| Opt-in/opt-out preference | Legal compliance (TCPA) | Consent record |

### 2.2 What We Do NOT Store

- Student last names (use first name + ID only)
- Social Security Numbers
- Full birth dates (age/birth month is sufficient for grade grouping)
- Payment instrument details (credit cards, bank accounts)
- Location / GPS data
- Message content from parents beyond the last 30 days (rotate logs)

> **Teaching note:** "Data minimization" is a legal requirement under FERPA and a security best practice. Every field you store is a field you can leak. If you don't need it, don't store it.

---

## 3. Masking and Pseudonymization

### 3.1 Logs

All application logs must mask:
- Phone numbers: `+1-XXX-XXX-1234` (show last 4 only)
- Student names: `E***` (first letter + asterisks)
- Invoice amounts: `$XXX.XX` (mask in debug logs; show full in audit table only)

```python
# Example: masking function we'll use in the codebase
def mask_phone(phone: str) -> str:
    if len(phone) >= 4:
        return phone[:-4].replace(phone[:-4], "X" * len(phone[:-4])) + phone[-4:]
    return "XXXX"

def mask_name(name: str) -> str:
    if len(name) > 1:
        return name[0] + "*" * (len(name) - 1)
    return "*"
```

### 3.2 Database

- Internal IDs are auto-incrementing integers — not exposed externally.
- External references (e.g., Twilio message SID) are stored in a separate table, not the primary records.
- Guardian phone numbers are hashed with HMAC-SHA256 using a server-side secret for deduplication queries (not for display).

---

## 4. Retention Policy

| Data Type | Retention Period | Action After Expiry |
|-----------|-----------------|---------------------|
| Sent SMS messages | 90 days | Soft-delete (mark archived) |
| Failed SMS messages | 30 days | Hard-delete |
| Delivery callbacks | 30 days | Hard-delete |
| Audit events | 1 year | Export to cold storage, then hard-delete |
| Payment records | 7 years | Archive to encrypted cold storage (school's responsibility) |
| Inbound parent messages | 30 days | Hard-delete |
| System health logs | 14 days | Rotate and compress |

> **Teaching note:** Retention limits reduce the "blast radius" of a breach. If someone gains access to your database, they can only see 90 days of messages, not 5 years.

---

## 5. Role-Based Access Control (RBAC)

### 5.1 Roles

| Role | Permissions | Typical User |
|------|------------|--------------|
| **System Admin** | Full database access, config changes, deployment | Walker's friend (you) |
| **School Director** | View dashboard, configure reminder policies, view queue status | Mrs. Chen |
| **School Staff** | View `CALL`/`EXTENSION` queue, mark requests resolved | Office assistant |
| **Worker Process** | Read outbox, write message status, no human login | Celery workers |
| **API Client** | Sync SIS data, read invoice status, no message access | SIS integration scripts |

### 5.2 Authentication

- **Dashboard:** Token-gated (pre-shared URL token, no passwords stored)
- **API:** HMAC-signed webhooks from Twilio; API key + secret for SIS sync
- **Database:** Unix socket or TLS-encrypted TCP; no plaintext passwords in config
- **Redis:** Unix socket or AUTH password; bind to localhost only

---

## 6. Audit Logging

Every security-relevant event is logged to the `audit_events` table:

| Event Type | What Is Logged | Who Can View |
|-----------|----------------|-------------|
| `message.send_attempt` | message_key, provider, masked_phone, template_id | Admin, Director |
| `message.delivered` | message_key, provider_event_id, timestamp | Admin, Director |
| `message.failed` | message_key, error_code, retry_count | Admin |
| `reminder.suppressed` | message_key, suppression_reason (e.g., "invoice_paid") | Admin, Director |
| `policy.changed` | field_name, old_value, new_value, changed_by | Admin |
| `guardian.opt_out` | guardian_id, timestamp, source (STOP keyword) | Admin |
| `sis.sync` | records_synced, checksum, duration_ms | Admin |
| `login.failure` | ip_address, timestamp, reason | Admin |

> **Teaching note:** Audit logs are append-only. No user (not even an admin) can modify or delete them through the application. They must be purged through direct database operations with documented approval.

---

## 7. SMS-Safe Content Rules

### 7.1 Message Length
- Reminder messages: ≤ 160 characters (1 SMS segment)
- Acknowledgments: ≤ 160 characters
- Status replies: ≤ 320 characters (2 segments) if itemized

### 7.2 Prohibited Content
- No URLs in reminders (prevents phishing suspicion)
- No requests for payment instrument details via SMS
- No threats or shaming language ("Your child will be removed if unpaid")
- No ALL CAPS (feels aggressive)
- Required opt-out language: "Reply STOP to opt out" on first message to new guardians

### 7.3 Quiet Hours
- No outbound SMS between 9:00 PM and 8:00 AM in the school's timezone.
- Reminders scheduled during quiet hours are deferred to 8:00 AM.
- Late notices have a separate quiet-hours override (configurable by director).

---

## 8. Encryption

### 8.1 At Rest
- PostgreSQL data directory on encrypted volume (LUKS on Raspberry Pi, hardware encryption on Jetson SSD)
- Database backups encrypted with AES-256-GCM using a key from environment variable `BACKUP_ENCRYPTION_KEY`

### 8.2 In Transit
- All API endpoints: TLS 1.2+ (Let's Encrypt or school-managed certificate)
- Twilio webhooks: validate signature using `TWILIO_AUTH_TOKEN`
- Redis: TLS tunnel or Unix socket (no plaintext TCP across networks)
- SIS sync: HTTPS only; reject self-signed certificates unless explicitly whitelisted

---

## 9. Incident Response

### 9.1 Data Breach Detection
- Automated alert if `audit_events` shows > 10 failed logins in 5 minutes
- Automated alert if outbound message volume exceeds 2x daily average (indicates misconfiguration or abuse)

### 9.2 Breach Response Playbook
1. **Isolate:** Stop Celery workers to halt further SMS sends
2. **Assess:** Query `audit_events` for scope (which guardians, what data)
3. **Notify:** School director within 24 hours; legal counsel if > 500 records affected
4. **Remediate:** Rotate API keys, force guardian re-opt-in if phone numbers exposed
5. **Document:** Write post-incident report stored in `docs/incidents/`

---

## 10. Compliance Checklist

Before going live, verify:

- [ ] All phone numbers masked in logs
- [ ] `audit_events` table created with append-only access
- [ ] Quiet hours configured for school's timezone
- [ ] `STOP` and `START` keywords implemented and tested
- [ ] Backup encryption key set in environment
- [ ] TLS certificate installed and auto-renewing
- [ ] Director has read and signed off on this policy
- [ ] Retention cron jobs scheduled

---

## 11. Implementation Notes for Developers

### 11.1 Where This Policy Lives in Code

```
sms-tuition-agent/
├── docs/
│   └── 02-security-policy.md      # <-- this file
├── domain/
│   └── masking.py                 # mask_phone, mask_name functions
├── infra/
│   └── audit_logger.py            # append-only audit_events writer
├── api/
│   └── middleware/
│       └── audit_middleware.py      # auto-log API calls
└── alembic/versions/
    └── ...audit_events_table.py     # migration for audit table
```

### 11.2 Key Files to Create in Later Steps

- `domain/masking.py` — reusable masking utilities
- `infra/audit_logger.py` — async audit event writer
- `api/middleware/audit_middleware.py` — logs every request/response
- `api/middleware/quiet_hours.py` — blocks sends during quiet hours
- `api/middleware/twilio_signature.py` — validates Twilio webhook signatures

---

*End of Security Policy — Ready for Step 3 (Scaffold) when you confirm.*
