"""
Task 3: PII Regex Patterns & Redaction Utilities
Detects:
1. Emails (RFC 5322 compatible common format)
2. Social Security Numbers (SSN: formatted XXX-XX-XXXX and raw 9-digit)
3. Credit Card Numbers (CCN: Visa, Mastercard, Amex, Discover with optional Luhn validation)
"""

import re
from typing import Tuple

# Redaction replacement placeholder
REDACTED_TAG = "[REDACTED]"

# 1. Email Pattern
EMAIL_REGEX = re.compile(
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
)

# 2. SSN Patterns (Formatted: 3-2-4, and Raw 9-digit with word boundary)
SSN_FORMATTED_REGEX = re.compile(
    r"\b(?!000|666|9\d{2})\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b"
)
SSN_RAW_REGEX = re.compile(
    r"\b(?!000|666|9\d{2})\d{3}(?!00)\d{2}(?!0000)\d{4}\b"
)

# 3. Credit Card Number Patterns (13-19 digits, formatted with spaces/hyphens or raw)
# Formatted 16-digit (Visa, Mastercard, etc.) or 15-digit (Amex: 4-6-5)
CCN_FORMATTED_REGEX = re.compile(
    r"\b(?:\d{4}[ -]\d{4}[ -]\d{4}[ -]\d{4}|\d{4}[ -]\d{6}[ -]\d{5})\b"
)
CCN_RAW_REGEX = re.compile(
    r"\b(?:4[0-9]{12}(?:[0-9]{3})?|5[1-5][0-9]{14}|3[47][0-9]{13}|6(?:011|5[0-9]{2})[0-9]{12}|[0-9]{16})\b"
)


def luhn_checksum_valid(number_str: str) -> bool:
    """Validates credit card number digits using Luhn algorithm (mod 10)."""
    digits = [int(c) for c in number_str if c.isdigit()]
    if len(digits) < 13 or len(digits) > 19:
        return False
    checksum = 0
    reverse_digits = digits[::-1]
    for i, d in enumerate(reverse_digits):
        if i % 2 == 1:
            doubled = d * 2
            checksum += doubled - 9 if doubled > 9 else doubled
        else:
            checksum += d
    return (checksum % 10) == 0


def redact_pii_text(text: str) -> str:
    """
    Applies comprehensive PII redaction across full text:
    Replaces valid Emails, SSNs, and Credit Cards with [REDACTED].
    """
    if not text:
        return text

    # Redact Emails
    text = EMAIL_REGEX.sub(REDACTED_TAG, text)

    # Redact Formatted SSN
    text = SSN_FORMATTED_REGEX.sub(REDACTED_TAG, text)

    # Redact Formatted Credit Cards
    text = CCN_FORMATTED_REGEX.sub(REDACTED_TAG, text)

    # Redact Raw Credit Cards (with Luhn check to prevent false-positive number redactions)
    def ccn_replacer(match: re.Match) -> str:
        matched_str = match.group(0)
        digits_only = re.sub(r"\D", "", matched_str)
        if len(digits_only) in (15, 16) and luhn_checksum_valid(digits_only):
            return REDACTED_TAG
        return matched_str

    text = CCN_RAW_REGEX.sub(ccn_replacer, text)

    # Redact Raw SSN (9 digits not caught as credit card)
    text = SSN_RAW_REGEX.sub(REDACTED_TAG, text)

    return text
