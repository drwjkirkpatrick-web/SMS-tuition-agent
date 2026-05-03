"""
domain/masking.py — PII masking utilities
═══════════════════════════════════════════════════

Every log message, error report, and metric label that could contain
sensitive data must pass through these functions.

This is a direct implementation of the policy defined in
`docs/02-security-policy.md` §3.1.
═══════════════════════════════════════════════════
"""


def mask_phone(phone: str | None) -> str:
    """
    Mask a phone number, showing only the last 4 digits.
    
    Examples:
      +15551234567 → XXXXXXXXX4567
      555-123-4567 → XXXXXXX4567
    """
    if not phone:
        return "XXXX"
    if len(phone) >= 4:
        return "X" * (len(phone) - 4) + phone[-4:]
    return "X" * len(phone)


def mask_name(name: str | None) -> str:
    """
    Mask a name, showing only the first letter.
    
    Examples:
      Emma → E***
      Li → L*
    """
    if not name:
        return "*"
    if len(name) > 1:
        return name[0] + "*" * (len(name) - 1)
    return name[0] if name else "*"


def mask_amount(amount: float | None) -> str:
    """
    Mask a dollar amount in logs (show $XXX.XX).
    Full amounts live only in the database audit table.
    """
    if amount is None:
        return "$XXX.XX"
    return "$XXX.XX"  # always masked; never show even partial digits
