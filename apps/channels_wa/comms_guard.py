"""The outbound safety rail.

Nothing reaches a real phone unless the deployment is explicitly in live mode.
This has already saved one production embarrassment on a sister project; it is
not optional and it is checked inside the transport, not by the caller.
"""

import logging

from django.conf import settings

logger = logging.getLogger(__name__)

SUPPRESS = "suppress"
ALLOWLIST = "allowlist"
LIVE = "live"


def normalise(number: str) -> str:
    return "".join(ch for ch in str(number or "") if ch.isdigit())


def _matches(candidate: str, allowed: str) -> bool:
    """Compare two numbers written in different but equivalent ways.

    People write a test number as 0726124698, 27726124698 or +27 72 612 4698
    and all three mean the same phone. A strict string compare silently blocks
    every test send, and the usual "fix" for that is to switch the whole
    deployment to live mode - which is a far worse outcome than being generous
    here. So: exact digits match, or the last nine significant digits match.
    """
    candidate, allowed = normalise(candidate), normalise(allowed)
    if not candidate or not allowed:
        return False
    if candidate == allowed:
        return True
    return len(candidate) >= 9 and len(allowed) >= 9 and candidate[-9:] == allowed[-9:]


def outbound_blocked(to_number: str):
    """Return a reason string when the send must not happen, else None."""
    mode = getattr(settings, "OUTBOUND_COMMS_MODE", SUPPRESS)
    if mode == LIVE:
        return None
    if mode == ALLOWLIST:
        for allowed in getattr(settings, "OUTBOUND_ALLOWLIST", []):
            if _matches(to_number, allowed):
                return None
        return (
            f"Test mode: {normalise(to_number)} is not on the allowlist, so nothing was "
            "sent. Add it to OUTBOUND_ALLOWLIST (international form, no plus, e.g. "
            "27726124698) or switch OUTBOUND_COMMS_MODE to live."
        )
    return (
        "Sending is switched off in this environment (OUTBOUND_COMMS_MODE=suppress), "
        "so nothing was sent."
    )
