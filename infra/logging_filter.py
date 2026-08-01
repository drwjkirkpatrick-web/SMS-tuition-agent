"""
infra/logging_filter.py — Structured logging PII masking filter
═══════════════════════════════════════════════════

Even with careful application-level masking, phone numbers and email
addresses can leak into log messages via stack traces, exception
reprs, and third-party library warnings. This module installs a
``logging.Filter`` on the root logger that scans every record's
formatted message and masks PII before it reaches any handler.

Teaching notes:
  - A ``logging.Filter`` runs *before* handlers format the record,
    so we mutate ``record.msg`` (the raw template) and
    ``record.args`` to ensure the masked version is what gets emitted.
    For records that are already fully formatted strings (common with
    ``logger.info("…")`` with no args), we simply replace ``record.msg``.
  - We delegate phone masking to ``domain.masking.mask_phone`` so the
    mask format is consistent with the rest of the application.
  - Email masking replaces the local part with ``***`` and keeps the
    domain, which is safe for debugging (the domain is not PII for a
    school's own staff) while hiding the individual address.
  - Call ``setup_pii_logging()`` once at application startup (e.g., in
    ``api/main.py`` lifespan) — it is idempotent and will not add a
    duplicate filter on repeated calls.
═══════════════════════════════════════════════════
"""

import logging
import re
from typing import Any

from domain.masking import mask_phone

# Match E.164-ish phone numbers: optional +, 10–15 consecutive digits.
# This intentionally avoids matching short numeric IDs by requiring
# a minimum of 10 digits.
_PHONE_RE = re.compile(r"\+?\d{10,15}")

# Match common email formats: local@domain.tld
_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")


class PIIMaskingFilter(logging.Filter):
    """
    A ``logging.Filter`` that masks phone numbers and email addresses
    in log records before they are emitted by handlers.

    The filter is attached to the *root* logger so it sees records from
    all loggers in the process (library code included).
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """
        Mask PII in the record's message. Always returns ``True``
        (we never suppress records — only sanitize them).
        """
        # Build the full message string from msg + args
        try:
            message = record.getMessage()
        except Exception:
            # If formatting itself fails, let the handler deal with it
            return True

        masked = self._mask_text(message)
        if masked != message:
            # Replace the record's msg with the masked string and clear
            # args so handlers don't re-format and reintroduce PII.
            record.msg = masked
            record.args = None

        return True

    @staticmethod
    def _mask_text(text: str) -> str:
        """Mask phone numbers and emails in *text*."""

        def _mask_phone_match(m: re.Match) -> str:
            """Convert a regex phone match to the project mask format."""
            raw = m.group()
            # mask_phone expects the full number; it returns X's + last 4
            return mask_phone(raw)

        def _mask_email_match(m: re.Match) -> str:
            """Mask the local part of an email, keeping the domain."""
            email = m.group()
            local, _, domain = email.partition("@")
            if len(local) <= 1:
                masked_local = "*"
            else:
                masked_local = local[0] + "*" * (len(local) - 1)
            return f"{masked_local}@{domain}"

        text = _PHONE_RE.sub(_mask_phone_match, text)
        text = _EMAIL_RE.sub(_mask_email_match, text)
        return text


def setup_pii_logging() -> None:
    """
    Attach the ``PIIMaskingFilter`` to the root logger.

    Call this once at application startup::

        from infra.logging_filter import setup_pii_logging
        setup_pii_logging()

    The function is idempotent: it checks whether a
    ``PIIMaskingFilter`` is already attached before adding a new one,
    so repeated calls (e.g., in tests) do not create duplicates.
    """
    root = logging.getLogger()
    for existing in root.filters:
        if isinstance(existing, PIIMaskingFilter):
            return  # already installed

    root.addFilter(PIIMaskingFilter())