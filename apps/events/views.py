"""Public views. No login, no session - a guest taps a link on their phone,
or the website registration form posts their RSVP to us."""

import hmac
import json
import logging
import re

from django.db import transaction
from django.http import Http404, HttpResponse, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from apps.core.audit import audit

from . import calendar as ics
from .models import Event, Guest, JourneyEvent, normalise_msisdn

logger = logging.getLogger(__name__)


@require_GET
def guest_ics(request, token):
    """Serve one guest's calendar entry.

    The invite token is the only credential, which is the same trust level as
    the wa.me link it arrived beside. It exposes nothing a guest does not
    already know: the event, the venue, and their own guest number.
    """
    guest = Guest.objects.filter(invite_token=token).select_related("event").first()
    if guest is None or not guest.event.is_active:
        raise Http404

    body = ics.build(guest.event, guest)
    response = HttpResponse(body, content_type="text/calendar; charset=utf-8")
    response["Content-Disposition"] = (
        f'attachment; filename="{ics.filename_for(guest.event)}"'
    )
    response["Cache-Control"] = "private, max-age=300"
    logger.info("ics served guest=%s", guest.pk)
    return response


# -- website registration form ----------------------------------------------

# Gravity Forms posts whatever field keys the site admin configured, so every
# field is matched by a canonical form of its key: lower-case, letters and
# digits only. "First Name", "first_name" and "firstname" all land the same.
FIELD_ALIASES = {
    "first_name": {"firstname", "first"},
    "last_name": {"lastname", "surname", "last"},
    "email": {"email", "emailaddress"},
    "msisdn": {"mobile", "mobilenumber", "phone", "phonenumber", "cell",
               "cellphone", "cellnumber", "msisdn", "whatsapp", "whatsappnumber",
               "contactnumber"},
    "company": {"company", "companyname", "organisation", "organization",
                "brokerage", "firm"},
    "party_size": {"partysize", "party", "guests", "seats"},
    "dietary": {"dietary", "diet", "dietaryrequirements", "dietaryneeds"},
    "attendance": {"attending", "attendance", "rsvp", "response",
                   "willyouattend", "areyouattending", "willyoubeattending"},
    "full_name": {"name", "fullname"},
}


def _form_payload(request):
    if request.content_type and "json" in request.content_type:
        try:
            data = json.loads(request.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        return data if isinstance(data, dict) else None
    return {key: value for key, value in request.POST.items()}


def _extract(payload):
    by_canonical = {}
    for key, value in payload.items():
        canonical = re.sub(r"[^a-z0-9]", "", str(key).lower())
        if canonical and str(value or "").strip():
            by_canonical.setdefault(canonical, str(value).strip())

    fields = {}
    for field, aliases in FIELD_ALIASES.items():
        for alias in aliases:
            if alias in by_canonical:
                fields[field] = by_canonical[alias]
                break

    if "first_name" not in fields and "full_name" in fields:
        first, _, last = fields["full_name"].partition(" ")
        fields["first_name"] = first
        fields.setdefault("last_name", last.strip())
    fields.pop("full_name", None)
    return fields


def _rsvp_from(answer):
    lowered = (answer or "").lower()
    if "pack" in lowered or "outside" in lowered:
        return Guest.Rsvp.PACK
    if re.search(r"\b(no|not|can'?t|cannot|decline[ds]?|unable)\b", lowered):
        return Guest.Rsvp.DECLINED
    return Guest.Rsvp.ATTENDING


def _party_size(raw):
    match = re.search(r"\d+", raw or "")
    if not match:
        return 1
    count = int(match.group())
    if (raw or "").strip().startswith("+"):
        count += 1
    return max(1, min(count, 10))


def _whatsapp_link(guest):
    from apps.channels_wa.models import WhatsAppChannel

    channel = (
        WhatsAppChannel.objects.for_workspace(guest.workspace)
        .filter(is_active=True)
        .order_by("-is_default", "pk")
        .first()
    )
    if channel is None or not channel.phone_number:
        return ""
    return guest.whatsapp_link(channel.phone_number)


@csrf_exempt
@require_POST
def form_rsvp(request, event_id):
    """Take an RSVP posted by the Gravity Forms webhook on the public website.

    The per-event shared secret is the credential; without a matching one the
    response is a plain 404, the same as a wrong event id. A repeat submission
    updates the same guest - matched on mobile, then email - so the roster
    never grows a duplicate row from someone pressing submit twice.
    """
    event = Event.objects.filter(pk=event_id, is_active=True).first()
    provided = request.headers.get("X-Registration-Secret", "") or request.GET.get("secret", "")
    if (
        event is None
        or not event.registration_secret
        or not hmac.compare_digest(event.registration_secret, provided)
    ):
        logger.warning("form rsvp rejected event=%s", event_id)
        raise Http404

    payload = _form_payload(request)
    if payload is None:
        return JsonResponse({"ok": False, "error": "The submission could not be read."}, status=400)

    fields = _extract(payload)
    fields["msisdn"] = normalise_msisdn(fields.get("msisdn", ""))
    if not fields.get("first_name") or not (fields["msisdn"] or fields.get("email")):
        return JsonResponse(
            {"ok": False, "error": "We need at least a first name and a mobile number or email."},
            status=400,
        )

    guest = None
    if fields["msisdn"]:
        guest = Guest.objects.filter(event=event, msisdn=fields["msisdn"]).first()
    if guest is None and fields.get("email"):
        guest = Guest.objects.filter(event=event, email__iexact=fields["email"]).first()

    details = {
        key: fields[key]
        for key in ("first_name", "last_name", "company", "email", "msisdn", "dietary")
        if fields.get(key)
    }
    created = guest is None
    if created:
        guest = Guest.objects.create(workspace=event.workspace, event=event, **details)
    else:
        for key, value in details.items():
            setattr(guest, key, value)
        guest.save(update_fields=list(details))

    JourneyEvent.objects.create(
        workspace=event.workspace, guest=guest, step="form_registered", payload=fields
    )

    if not event.rsvp_open:
        audit("event.form_rsvp_closed", workspace=event.workspace, target=guest, request=request)
        return JsonResponse(
            {"ok": False, "error": "RSVPs for this event have closed.",
             "whatsapp_link": _whatsapp_link(guest)},
            status=200,
        )

    status = _rsvp_from(fields.get("attendance"))
    extra = {"rsvp_at": timezone.now()}
    if "party_size" in fields:
        extra["party_size"] = _party_size(fields["party_size"])

    if status == Guest.Rsvp.ATTENDING and event.is_full and not guest.is_coming:
        guest.advance(Guest.Stage.ENGAGED, rsvp_status=Guest.Rsvp.WAITLIST, **extra)
    else:
        if status == Guest.Rsvp.ATTENDING and guest.guest_number is None:
            extra["guest_number"] = event.next_guest_number()
        guest.advance(Guest.Stage.CONFIRMED, rsvp_status=status, **extra)

    if guest.rsvp_status == Guest.Rsvp.ATTENDING and guest.msisdn:
        from .tasks import send_rsvp_confirmation

        transaction.on_commit(lambda: send_rsvp_confirmation.delay(guest.pk))

    audit(
        "event.form_rsvp",
        workspace=event.workspace,
        target=guest,
        request=request,
        rsvp_status=guest.rsvp_status,
        created=created,
    )
    logger.info("form rsvp saved guest=%s status=%s", guest.pk, guest.rsvp_status)
    return JsonResponse(
        {
            "ok": True,
            "created": created,
            "rsvp_status": guest.rsvp_status,
            "guest_number": guest.guest_number,
            "whatsapp_link": _whatsapp_link(guest),
        }
    )
