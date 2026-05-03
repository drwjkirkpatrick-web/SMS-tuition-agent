"""
domain/templates.py — SMS message templates with placeholder rendering
═══════════════════════════════════════════════════

Templates are plain text with Python f-string style placeholders:
  "Hi {guardian_name}, this is a reminder for {student_name}'s tuition..."

We use Python's `str.format()` for safe rendering (no code execution).
All templates are stored in code for version control and review.

Teaching notes:
  - Templates are deterministic: same inputs always produce same output.
  - We check character count to enforce max segments (default 2 = 306 chars).
  - The `render()` function handles missing placeholders gracefully.
  - Quiet hours are enforced at send time, not template time.
═══════════════════════════════════════════════════
"""

from dataclasses import dataclass
from typing import Optional

from infra.settings import get_settings


@dataclass(frozen=True)
class MessageTemplate:
    """A template with metadata."""
    name: str
    body: str
    max_segments: int = 2


# ═══════════════════════════════════════════════════
# Template Library
# ═══════════════════════════════════════════════════

TEMPLATES: dict[str, MessageTemplate] = {
    "reminder_due_14": MessageTemplate(
        name="reminder_due_14",
        body="Hi {guardian_name}, {school_name} reminder: {student_name}'s tuition of ${amount_due} is due {due_date}. Questions? Reply HELP or CALL. Reply STOP to opt out.",
    ),
    "reminder_due_3": MessageTemplate(
        name="reminder_due_3",
        body="Hi {guardian_name}, {school_name}: {student_name}'s tuition of ${amount_due} is due in 3 days ({due_date}). Reply HELP for options. Reply STOP to opt out.",
    ),
    "reminder_due_today": MessageTemplate(
        name="reminder_due_today",
        body="Hi {guardian_name}, {school_name}: {student_name}'s tuition of ${amount_due} is due TODAY. Reply CALL to speak with us. Reply STOP to opt out.",
    ),
    "reminder_late": MessageTemplate(
        name="reminder_late",
        body="Hi {guardian_name}, {school_name}: {student_name}'s tuition of ${amount_due} was due {due_date} and is now overdue. Please reply CALL to discuss. Reply STOP to opt out.",
    ),
    "payment_confirmed": MessageTemplate(
        name="payment_confirmed",
        body="Hi {guardian_name}, {school_name}: Thank you! We've received ${amount_paid} for {student_name}'s tuition. Remaining balance: ${balance}. Reply STOP to opt out.",
    ),
    "callback_ack": MessageTemplate(
        name="callback_ack",
        body="{school_name}: We received your message and will follow up shortly. Reply STOP to opt out.",
    ),
    "hardship_ack": MessageTemplate(
        name="hardship_ack",
        body="{school_name}: Thank you for reaching out. We're reviewing your request and will contact you within 24 hours. Reply STOP to opt out.",
    ),
    "status_reply": MessageTemplate(
        name="status_reply",
        body="{school_name} balance for {student_name}: ${balance} due {due_date}. Reply PAID if you've submitted payment. Reply STOP to opt out.",
    ),
    "help_reply": MessageTemplate(
        name="help_reply",
        body="{school_name} SMS commands: STATUS (check balance), PAID (confirm payment), CALL (request callback), EXTENSION (request extension), STOP (opt out), START (opt back in).",
    ),
    "opt_out_confirm": MessageTemplate(
        name="opt_out_confirm",
        body="You've been opted out of {school_name} SMS reminders. Reply START to resubscribe.",
    ),
    "opt_in_confirm": MessageTemplate(
        name="opt_in_confirm",
        body="Welcome back! You're now subscribed to {school_name} SMS reminders.",
    ),
}


class TemplateRenderer:
    """
    Renders templates with data validation and segment counting.
    """

    def __init__(self) -> None:
        self.settings = get_settings()

    def render(
        self,
        template_name: str,
        context: dict,
        force_max_segments: Optional[int] = None,
    ) -> str:
        """
        Render a template with the given context.
        
        Args:
            template_name: key in TEMPLATES dict
            context: dict of placeholder values
            force_max_segments: override default max segments
        
        Returns:
            Rendered message body
        
        Raises:
            ValueError: if template not found or exceeds max segments
        """
        template = TEMPLATES.get(template_name)
        if not template:
            raise ValueError(f"Template '{template_name}' not found")

        # Render with graceful fallback for missing keys
        try:
            body = template.body.format(**context)
        except KeyError as exc:
            # If a key is missing, replace with empty string
            body = template.body.format(**{k: context.get(k, "") for k in self._extract_keys(template.body)})

        # Check segment count
        max_seg = force_max_segments or template.max_segments or self.settings.max_sms_segments
        segments = self._count_segments(body)
        if segments > max_seg:
            raise ValueError(
                f"Rendered message exceeds {max_seg} segments ({segments}). "
                f"Body: {body[:50]}..."
            )

        return body

    def _extract_keys(self, template_body: str) -> set[str]:
        """Extract {placeholder} names from a template string."""
        import re
        return set(re.findall(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", template_body))

    def _count_segments(self, body: str) -> int:
        """
        Count GSM-7 SMS segments.
        
        - 1 segment = 160 characters (GSM-7)
        - 2 segments = 306 characters (153 * 2, due to UDH)
        - 3+ segments follow same pattern
        
        For simplicity, we assume GSM-7 (not UCS-2/Unicode).
        If body contains non-GSM-7 chars, we use 70 chars per segment.
        """
        # Simple check for non-GSM-7 characters
        gsm7_chars = set(
            "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞ\x1bÆæßÉ !"#¤%&'()*+,-./0123456789:;"
            "<=>?¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
        )
        
        is_gsm7 = all(c in gsm7_chars for c in body)
        
        if is_gsm7:
            chars_per_seg = 160 if len(body) <= 160 else 153
        else:
            chars_per_seg = 70 if len(body) <= 70 else 67
        
        if len(body) <= chars_per_seg:
            return 1
        return (len(body) + chars_per_seg - 1) // chars_per_seg

    def list_templates(self) -> list[str]:
        """Return all available template names."""
        return list(TEMPLATES.keys())


# Convenience function

def render_template(template_name: str, context: dict) -> str:
    """Quick render without creating a renderer instance."""
    renderer = TemplateRenderer()
    return renderer.render(template_name, context)
