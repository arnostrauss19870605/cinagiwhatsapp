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


def outbound_blocked(to_number: str):
    """Return a reason string when the send must not happen, else None."""
    mode = getattr(settings, "OUTBOUND_COMMS_MODE", SUPPRESS)
    if mode == LIVE:
        return None
    if mode == ALLOWLIST:
        allowed = {normalise(n) for n in getattr(settings, "OUTBOUND_ALLOWLIST", [])}
        if normalise(to_number) in allowed:
            return None
        return (
            "Test mode: this number is not on the allowlist, so nothing was sent. "
            "Add it to OUTBOUND_ALLOWLIST or switch OUTBOUND_COMMS_MODE to live."
        )
    return (
        "Sending is switched off in this environment (OUTBOUND_COMMS_MODE=suppress), "
        "so nothing was sent."
    )
