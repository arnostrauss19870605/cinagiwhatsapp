"""Keep credentials out of the logs, always - not just when someone remembers."""

import logging
import re

_PATTERNS = [
    re.compile(r"(EAA[A-Za-z0-9]{10})[A-Za-z0-9_\-]+"),          # Meta tokens
    re.compile(r"(?i)(authorization\"?\s*[:=]\s*\"?bearer\s+)\S+"),
    re.compile(r"(?i)(\"?access_token\"?\s*[:=]\s*\"?)[^\"\s,}]+"),
    re.compile(r"(?i)(\"?app_secret\"?\s*[:=]\s*\"?)[^\"\s,}]+"),
]


def redact(text: str) -> str:
    for pattern in _PATTERNS:
        text = pattern.sub(lambda m: f"{m.group(1)}[redacted]", text)
    return text


def mask(value: str, keep: int = 4) -> str:
    """Show enough of a credential to identify it, never enough to use it."""
    if not value:
        return ""
    if len(value) <= keep + 2:
        return "*" * len(value)
    return f"{value[:keep]}...{value[-2:]}"


class RedactFilter(logging.Filter):
    def filter(self, record):
        try:
            record.msg = redact(str(record.msg))
        except Exception:  # pragma: no cover - never break logging
            pass
        return True
