"""Meta's webhook endpoint.

Rules, learned the hard way on a sister project:
  * always answer 200 quickly - Meta retries anything else and a slow handler
    turns one message into a storm;
  * verify X-Hub-Signature-256 before trusting a byte;
  * do the real work in Celery;
  * never say anything useful in an error response.
"""

import hashlib
import hmac
import json
import logging

from django.http import HttpResponse, HttpResponseForbidden
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

logger = logging.getLogger(__name__)


def _channels_in_payload(payload):
    from apps.channels_wa.models import WhatsAppChannel

    ids = set()
    for entry in payload.get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            phone_number_id = ((change.get("value") or {}).get("metadata") or {}).get(
                "phone_number_id"
            )
            if phone_number_id:
                ids.add(phone_number_id)
    if not ids:
        return []
    return list(WhatsAppChannel.objects.filter(phone_number_id__in=ids, is_active=True))


def _signature_ok(raw_body, header, channels):
    if not header or not header.startswith("sha256="):
        return False
    provided = header.split("=", 1)[1]
    secrets_to_try = {c.app_secret for c in channels if c.app_secret}
    for secret in secrets_to_try:
        expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
        if hmac.compare_digest(expected, provided):
            return True
    return False


@csrf_exempt
@require_http_methods(["GET", "POST"])
def webhook(request):
    if request.method == "GET":
        return _verify(request)

    raw = request.body
    try:
        payload = json.loads(raw.decode() or "{}")
    except ValueError:
        return HttpResponse("ok")  # never tell a prober it got the format wrong

    channels = _channels_in_payload(payload)
    if not channels:
        logger.warning("webhook for unknown phone_number_id ignored")
        return HttpResponse("ok")

    if not _signature_ok(raw, request.headers.get("X-Hub-Signature-256", ""), channels):
        logger.warning("webhook signature rejected")
        return HttpResponseForbidden("")

    from apps.channels_wa.tasks import process_inbound_payload

    try:
        process_inbound_payload.delay(payload)
    except Exception:
        # Broker down: process inline rather than lose the customer's message.
        logger.warning("celery unavailable, processing webhook inline", exc_info=True)
        process_inbound_payload(payload)

    return HttpResponse("ok")


def _verify(request):
    """Meta's subscription handshake."""
    from apps.channels_wa.models import WhatsAppChannel

    mode = request.GET.get("hub.mode")
    token = request.GET.get("hub.verify_token", "")
    challenge = request.GET.get("hub.challenge", "")
    if mode == "subscribe" and token:
        if WhatsAppChannel.objects.filter(verify_token=token, is_active=True).exists():
            return HttpResponse(challenge)
    logger.warning("webhook verification failed")
    return HttpResponseForbidden("")
