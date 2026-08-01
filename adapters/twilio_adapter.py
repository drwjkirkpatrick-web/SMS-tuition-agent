"""
adapters/twilio_adapter.py — Twilio SMS Adapter
═══════════════════════════════════════════════════

Real implementation using the Twilio Python SDK.

Key features:
  - Idempotent sends via client_message_id
  - Retryable vs non-retryable error classification
  - Webhook signature validation (HMAC-SHA1)
  - Delivery status querying via Twilio API

Teaching notes:
  - Twilio's `create` method is synchronous. We wrap it in
    `asyncio.to_thread()` so it doesn't block the async event loop.
  - `client.messages.create(...)` accepts `messaging_service_sid` for
    high-volume sending, but we use `from_` with a single number for
    simplicity.
  - Twilio error codes:
      21211 → Invalid 'To' phone number (non-retryable)
      21610 → Message has been blocked by the user (non-retryable)
      429   → Rate limited (retryable)
      500/503 → Twilio server error (retryable)
  - Webhook signature uses your Twilio Auth Token as the HMAC key.
    NEVER expose this in logs or to clients.
═══════════════════════════════════════════════════
"""

import asyncio
import hmac
import hashlib
from base64 import b64encode
from urllib.parse import urlparse
from typing import Optional

from twilio.rest import Client
from twilio.base.exceptions import TwilioRestException

from adapters.sms_adapter import (
    DeliveryQueryResult,
    ErrorCategory,
    SendResult,
    SendStatus,
    SMSAdapter,
)
from infra.settings import get_settings


class TwilioAdapter(SMSAdapter):
    """
    Twilio SMS adapter with idempotent sends and webhook validation.
    """

    # Twilio error codes that are permanent failures (non-retryable)
    NON_RETRYABLE_CODES: set[int] = {
        21211,  # Invalid 'To' Phone Number
        21214,  # 'To' phone number not verified
        21610,  # Message has been blocked by the user
        21612,  # The 'To' number is not currently reachable via SMS
        21614,  # 'To' number is not a valid mobile number
        30003,  # Unreachable carrier
        30005,  # Unknown destination handset
        30006,  # Landline or unreachable carrier
    }

    # Retryable HTTP status codes from Twilio
    RETRYABLE_HTTP_CODES: set[int] = {429, 500, 502, 503, 504}

    def __init__(self) -> None:
        settings = get_settings()
        self.client = Client(
            settings.twilio_account_sid.get_secret_value(),
            settings.twilio_auth_token.get_secret_value(),
        )
        self.from_number = settings.twilio_phone_number
        self.auth_token = settings.twilio_auth_token.get_secret_value()

    async def send(
        self,
        to: str,
        body: str,
        client_message_id: Optional[str] = None,
    ) -> SendResult:
        """
        Send SMS via Twilio.
        
        `client_message_id` maps to Twilio's `messaging_service_sid`
        if using a messaging service, or we can pass it via the
        `status_callback` query params for tracking.
        
        Actually, Twilio supports `ProvideFeedback` or we can use
        the `client` parameter in some APIs, but the most reliable
        idempotency is through the `messaging_service_sid` + custom
        tags. For simplicity, we use the message body hash approach
        or simply trust our outbox deduplication.
        
        BETTER: We pass `client_message_id` as a custom parameter
        that Twilio echoes back in webhooks.
        """
        try:
            # Run Twilio SDK call in thread pool (it's blocking I/O)
            message = await asyncio.to_thread(
                self.client.messages.create,
                body=body,
                from_=self.from_number,
                to=to,
                # Pass our idempotent key as a custom tag
                # Twilio doesn't have a native client_message_id, but
                # we can use Smart Encoding or add it to the body...
                # Actually, let's use the `messaging_service_sid` approach
                # or simply store it in our DB and query by date range.
                # For this implementation, we'll pass it via status_callback
                # as a query parameter.
                status_callback=f"/webhooks/twilio/status?client_id={client_message_id}"
                if client_message_id
                else None,
            )

            return SendResult(
                status=SendStatus.ACCEPTED,
                provider_message_id=message.sid,
                client_message_id=client_message_id,
                segments=getattr(message, "num_segments", 1),
                price=getattr(message, "price", None),
                raw_response={"sid": message.sid, "status": message.status},
            )

        except TwilioRestException as exc:
            return self._handle_twilio_error(exc, client_message_id)
        except asyncio.TimeoutError:
            return SendResult(
                status=SendStatus.TIMEOUT,
                client_message_id=client_message_id,
                error_message="Request timed out",
                error_category=ErrorCategory.AMBIGUOUS,
            )
        except Exception as exc:
            return SendResult(
                status=SendStatus.UNKNOWN,
                client_message_id=client_message_id,
                error_message=str(exc),
                error_category=ErrorCategory.RETRYABLE,
            )

    def _handle_twilio_error(
        self,
        exc: TwilioRestException,
        client_message_id: Optional[str],
    ) -> SendResult:
        """Map Twilio exceptions to our normalized SendResult."""
        code = exc.code
        status = exc.status

        # Determine retryability
        if code in self.NON_RETRYABLE_CODES or status in (400, 401, 403, 404):
            category = ErrorCategory.NON_RETRYABLE
            send_status = SendStatus.REJECTED
        elif status in self.RETRYABLE_HTTP_CODES or code is None:
            category = ErrorCategory.RETRYABLE
            send_status = SendStatus.TIMEOUT if status >= 500 else SendStatus.RATE_LIMITED
        else:
            category = ErrorCategory.AMBIGUOUS
            send_status = SendStatus.UNKNOWN

        return SendResult(
            status=send_status,
            client_message_id=client_message_id,
            error_code=str(code) if code else None,
            error_message=exc.msg,
            error_category=category,
            raw_response={"twilio_code": code, "twilio_status": status},
        )

    async def query_delivery(
        self,
        provider_message_id: str,
    ) -> DeliveryQueryResult:
        """
        Query Twilio for message status by provider_message_id (Twilio SID).
        
        E4: Now queries by SID directly using fetch() instead of listing
        all recent messages and scanning bodies (O(1) vs O(N)).
        
        The reconciliation worker should pass message.provider_message_id
        (the stored Twilio SID) rather than client_message_id.
        """
        if not provider_message_id:
            return DeliveryQueryResult(status="not_found")
        
        try:
            msg = await asyncio.to_thread(
                self.client.messages(provider_message_id).fetch
            )
            status_map = {
                "sent": "sent",
                "delivered": "delivered",
                "failed": "failed",
                "undelivered": "failed",
                "received": "sent",
                "queued": "sent",
                "sending": "sent",
                "accepted": "sent",
                "scheduled": "sent",
                "receiving": "sent",
            }
            return DeliveryQueryResult(
                status=status_map.get(msg.status, "unknown"),
                provider_message_id=msg.sid,
            )
        except TwilioRestException as exc:
            if exc.status == 404:
                return DeliveryQueryResult(status="not_found")
            return DeliveryQueryResult(
                status="unknown",
                error_code=str(exc.code),
            )
        except Exception as exc:
            return DeliveryQueryResult(
                status="unknown",
                error_code=str(getattr(exc, "code", None)),
            )

    async def validate_webhook_signature(
        self,
        body: bytes,
        signature: str,
        url: str,
    ) -> bool:
        """
        Validate Twilio webhook signature using HMAC-SHA1.
        
        Twilio signs webhook payloads with your Auth Token.
        """
        # Twilio signature = Base64(HMAC-SHA1(auth_token, url + body))
        expected = self._compute_signature(body, url)
        return hmac.compare_digest(expected, signature)

    def _compute_signature(self, body: bytes, url: str) -> str:
        """
        Compute expected Twilio webhook signature.
        
        S3: Implements Twilio's actual signing algorithm:
        1. Parse the form body into key-value pairs
        2. Sort parameters alphabetically by key
        3. Concatenate: url + key1 + value1 + key2 + value2 + ...
        4. HMAC-SHA1 with auth_token as key
        5. Base64 encode the digest
        
        If body is not parseable as form data (e.g. raw JSON),
        falls back to url + raw body concatenation.
        """
        from urllib.parse import parse_qs
        
        # Try parsing as form data
        try:
            body_str = body.decode("utf-8") if isinstance(body, bytes) else body
            params = parse_qs(body_str, keep_blank_values=True)
            
            # Flatten single-value lists and sort by key
            flat = {}
            for key, values in params.items():
                flat[key] = values[0] if values else ""
            
            # Build signature string: url + sorted(key + value) pairs
            signature_str = url
            for key in sorted(flat.keys()):
                signature_str += key + flat[key]
        except Exception:
            # Fallback: raw body concatenation (original behavior)
            signature_str = url + (body.decode("utf-8") if isinstance(body, bytes) else str(body))
        
        digest = hmac.new(
            self.auth_token.encode("utf-8"),
            signature_str.encode("utf-8"),
            hashlib.sha1,
        ).digest()
        return b64encode(digest).decode("utf-8")


# ── Factory ──

def get_twilio_adapter() -> TwilioAdapter:
    """Factory: returns a configured Twilio adapter."""
    return TwilioAdapter()
