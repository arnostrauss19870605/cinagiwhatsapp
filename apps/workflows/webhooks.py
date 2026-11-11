"""Calling a customer-owned web address from a workflow step.

Every call carries the connection's token twice: as a bearer token (or in
whichever header the receiver names), and as an HMAC-SHA256 signature of the
exact body, so a receiver can check both who sent it and that nothing changed
on the way. Receivers with a fixed format of their own (Xealth's BIMS) get a
body built from the connection's template instead of ours.

A webhook usually makes something happen to a real person (an email with
their policy documents), so it obeys the same test-mode rail as WhatsApp
sends: outside live mode only allow-listed numbers get through.
"""

import hashlib
import hmac
import json
import logging
import re
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


def values_for(run):
    """What a body template's {{placeholders}} can name: answers win over contact details."""
    contact = run.contact
    local = contact.wa_id[2:] if contact.wa_id.startswith("27") else contact.wa_id
    return {
        "wa_id": contact.wa_id,
        "number": contact.pretty_number,
        "local_number": "0" + local if contact.wa_id.startswith("27") else contact.wa_id,
        "name": contact.name,
        **{str(k): v for k, v in (contact.attributes or {}).items()},
        **{str(k): v for k, v in (run.variables or {}).items()},
    }


def international(text):
    """A South African local number (0721234567) as 27721234567; anything else as its digits."""
    digits = re.sub(r"\D", "", text)
    if len(digits) == 10 and digits.startswith("0"):
        return "27" + digits[1:]
    return digits


FILTERS = {"international": international}


def _placeholder(inner, values):
    name, _, filter_name = inner.partition("|")
    name, filter_name = name.strip(), filter_name.strip()
    if filter_name and filter_name not in FILTERS:
        raise ValueError(f"'{filter_name}' is not something a value can be turned into. Use: {', '.join(FILTERS)}.")
    value = values.get(name)
    if value is not None and filter_name:
        value = FILTERS[filter_name](str(value))
    return value


def render_template(template, values):
    """Fill a JSON body template. Returns the parsed body; raises ValueError if it is not JSON.

    Inside quotes ("{{x}}", "Hi {{name}}") the value is text. A bare {{x}} becomes a number
    when the value is all digits without a leading zero, so a receiver that wants
    PolicyNumber as a number gets one; otherwise it is text, or null when there is no value.
    {{x|international}} writes a local cell number the way Xealth wants it (27...).
    """
    out, i, in_string = [], 0, False
    while i < len(template):
        if template.startswith("{{", i):
            end = template.find("}}", i)
            if end < 0:
                out.append(template[i:])
                break
            value = _placeholder(template[i + 2:end], values)
            text = "" if value is None else str(value)
            if in_string:
                out.append(json.dumps(text, ensure_ascii=False)[1:-1])
            elif re.fullmatch(r"[1-9]\d{0,17}", text):
                out.append(text)
            else:
                out.append("null" if value is None else json.dumps(text, ensure_ascii=False))
            i = end + 2
            continue
        char = template[i]
        if in_string and char == "\\":
            out.append(template[i:i + 2])
            i += 2
            continue
        if char == '"':
            in_string = not in_string
        out.append(char)
        i += 1
    try:
        return json.loads("".join(out))
    except json.JSONDecodeError as exc:
        raise ValueError(f"The body is not valid JSON (line {exc.lineno}, column {exc.colno}).") from exc


def template_problem(template):
    """Plain-language reason a body template cannot be used, or ''."""
    if not template.strip():
        return ""
    try:
        render_template(template, {})
    except ValueError as exc:
        return str(exc)
    return ""


def body_for(connection, run, step_id, step):
    if connection.body_template.strip():
        return render_template(connection.body_template, values_for(run))
    return payload_for(run, step_id, step)


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
        "X-Cinagi-Signature": signature(connection.token, body),
        "User-Agent": "Cinagi-Workflows/1",
    }
    if connection.token_header:
        headers[connection.token_header] = connection.token
    else:
        headers["Authorization"] = f"Bearer {connection.token}"
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
