"""Calling a customer-owned web address from a workflow step.

Every call carries the connection's token twice: as a bearer token, and as
an HMAC-SHA256 signature of the exact body, so a receiver can check both who
sent it and that nothing changed on the way.

A webhook usually makes something happen to a real person (an email with
their policy documents), so it obeys the same test-mode rail as WhatsApp
sends: outside live mode only allow-listed numbers get through.
"""

import hashlib
import hmac
import json
import logging
import time

import requests
from django.utils import timezone

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 10
ATTEMPTS = 2


def signature(token, body):
    return "sha256=" + hmac.new(token.encode(), body, hashlib.sha256).hexdigest()


def payload_for(run, step_id, step):
    contact = run.contact
    return {
        "event": "workflow.webhook",
        "sent_at": timezone.now().isoformat(),
        "workspace": run.workspace.name,
        "workflow": run.workflow.name,
        "step": step.get("name") or step_id,
        "conversation_id": str(run.conversation_id),
        "contact": {
            "number": contact.pretty_number,
            "wa_id": contact.wa_id,
            "name": contact.name,
            "details": contact.attributes or {},
        },
        "answers": run.variables or {},
    }


def call(connection, data, *, wa_id):
    """POST `data`. Returns (ok, detail dict). Never raises."""
    from apps.channels_wa.comms_guard import outbound_blocked

    blocked = outbound_blocked(wa_id)
    if blocked:
        return False, {"reason": blocked, "blocked": True}
    if not connection.is_active:
        return False, {"reason": f"The webhook '{connection.name}' is switched off."}

    body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {connection.token}",
        "X-Cinagi-Signature": signature(connection.token, body),
        "User-Agent": "Cinagi-Workflows/1",
    }
    last = {}
    for attempt in range(1, ATTEMPTS + 1):
        try:
            response = requests.post(connection.url, data=body, headers=headers, timeout=TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            last = {"reason": "The web address could not be reached.", "error": exc.__class__.__name__}
            logger.warning("webhook unreachable connection=%s attempt=%s", connection.pk, attempt)
        else:
            if response.status_code < 300:
                try:
                    returned = response.json()
                except ValueError:
                    returned = None
                return True, {"status": response.status_code, "response": returned}
            last = {"reason": f"The web address answered with an error ({response.status_code}).",
                    "status": response.status_code}
            logger.warning("webhook error connection=%s status=%s", connection.pk, response.status_code)
            if response.status_code < 500:
                break  # a 4xx will not get better by asking again
        if attempt < ATTEMPTS:
            time.sleep(1)
    return False, last
