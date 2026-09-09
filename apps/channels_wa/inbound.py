"""Turn a Meta webhook payload into contacts, conversations and messages.

Runs in a Celery worker, never in the web request - the webhook itself always
answers 200 immediately, because Meta retries anything else.
"""

import datetime as dt
import logging

from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.contacts.models import Contact
from apps.inbox import events
from apps.inbox.models import Conversation, Message, ProcessedInbound

logger = logging.getLogger(__name__)

MEDIA_KINDS = {"image", "document", "video", "audio", "sticker"}


def _ts(value):
    try:
        return dt.datetime.fromtimestamp(int(value), tz=dt.timezone.utc)
    except (TypeError, ValueError):
        return timezone.now()


def extract_body(payload):
    """Human readable text for any message type, so the inbox never shows blanks."""
    kind = payload.get("type", "unsupported")
    if kind == "text":
        return payload.get("text", {}).get("body", "")
    if kind == "button":
        return payload.get("button", {}).get("text", "")
    if kind == "interactive":
        interactive = payload.get("interactive", {})
        node = interactive.get("button_reply") or interactive.get("list_reply") or {}
        return node.get("title", "")
    if kind == "location":
        loc = payload.get("location", {})
        return loc.get("name") or f"{loc.get('latitude')}, {loc.get('longitude')}"
    if kind in MEDIA_KINDS:
        return payload.get(kind, {}).get("caption", "")
    if kind == "reaction":
        return payload.get("reaction", {}).get("emoji", "")
    return ""


def selection_id(payload):
    """The id behind a button or list tap - what the automation engine matches on."""
    interactive = payload.get("interactive", {})
    node = interactive.get("button_reply") or interactive.get("list_reply") or {}
    return node.get("id", "") or payload.get("button", {}).get("payload", "")


def process_value(channel, value):
    """Handle one `changes[].value` block for a resolved channel."""
    workspace = channel.workspace
    profiles = {p.get("wa_id"): p for p in value.get("contacts", []) or []}

    for payload in value.get("messages", []) or []:
        wamid = payload.get("id", "")
        if not wamid:
            continue
        try:
            with transaction.atomic():
                ProcessedInbound.objects.create(workspace=workspace, wamid=wamid)
        except IntegrityError:
            logger.info("duplicate inbound ignored wamid=%s", wamid)
            continue
        _store_message(channel, payload, profiles)

    if value.get("statuses"):
        from apps.channels_wa.receipts import process_statuses

        process_statuses(channel, value["statuses"])


def _store_message(channel, payload, profiles):
    workspace = channel.workspace
    wa_id = payload.get("from", "")
    profile = profiles.get(wa_id, {})
    received_at = _ts(payload.get("timestamp"))

    contact, _ = Contact.objects.get_or_create(
        workspace=workspace,
        wa_id=wa_id,
        defaults={"profile_name": (profile.get("profile") or {}).get("name", "")},
    )
    changed = []
    profile_name = (profile.get("profile") or {}).get("name", "")
    if profile_name and contact.profile_name != profile_name:
        contact.profile_name = profile_name
        changed.append("profile_name")
    contact.last_seen_at = received_at
    changed.append("last_seen_at")
    contact.save(update_fields=changed)

    conversation = (
        Conversation.objects.for_workspace(workspace)
        .filter(contact=contact, status__in=Conversation.OPEN_STATUSES)
        .order_by("-last_activity_at")
        .first()
    )
    is_new = conversation is None
    if is_new:
        conversation = Conversation(
            workspace=workspace,
            channel=channel,
            contact=contact,
            status=Conversation.Status.QUEUED,
        )

    conversation.touch_inbound(received_at)
    conversation.unread_agent_count = (conversation.unread_agent_count or 0) + 1
    if conversation.status == Conversation.Status.WAITING:
        conversation.status = Conversation.Status.ASSIGNED
    conversation.save()

    kind = payload.get("type", "unsupported")
    message = Message.objects.create(
        workspace=workspace,
        conversation=conversation,
        direction=Message.Direction.IN,
        actor=Message.Actor.CONTACT,
        kind=kind if kind in Message.Kind.values else Message.Kind.UNSUPPORTED,
        body=extract_body(payload),
        payload=payload,
        wamid=payload.get("id", ""),
        wa_status=Message.Status.DELIVERED,
        created_at=received_at,
    )

    if kind in MEDIA_KINDS:
        _fetch_media(channel, message, payload, kind)

    channel.last_inbound_at = received_at
    channel.save(update_fields=["last_inbound_at"])

    if is_new:
        from apps.agents.allocation import auto_assign

        auto_assign(conversation)
        _maybe_send_out_of_hours(conversation)

    # STOP outranks everything, including a request for a person: someone who
    # has asked to be left alone is not helped by being put in a queue.
    if _wants_to_stop(message.body):
        _opt_out(conversation, message)
    elif _wants_to_resume(message.body) and conversation.contact.is_opted_out:
        _opt_in(conversation, message)
    # Always honoured, whatever the automation is doing and whatever step the
    # customer is on. This is the promise the footer on every template makes.
    elif _wants_a_human(message.body):
        _escalate_to_human(conversation, message)
    else:
        # The event journey gets first refusal on everything else. It claims
        # tokens and RSVP answers and leaves anything it does not understand
        # for a person, rather than guessing.
        try:
            from apps.events.journey import handle as handle_event

            handle_event(conversation, message)
        except Exception:
            logger.exception("event journey failed conversation=%s", conversation.pk)

    events.conversation_changed(conversation, "message")
    return message


def _fetch_media(channel, message, payload, kind):
    """Pull the file into private storage - customers' documents never sit on Meta."""
    media_id = (payload.get(kind) or {}).get("id")
    if not media_id:
        return
    try:
        content, mime, _ = channel.client().download_media(media_id)
    except Exception:
        logger.warning("media download failed message=%s", message.pk, exc_info=True)
        return
    filename = (payload.get(kind) or {}).get("filename") or f"{media_id}.{(mime or 'bin').split('/')[-1]}"
    message.media.save(filename, ContentFile(content), save=False)
    message.media_mime = mime or ""
    message.media_filename = filename
    message.save(update_fields=["media", "media_mime", "media_filename"])


def _wants_a_human(text):
    """Does this message ask for a person?

    Deliberately generous. Letting through someone who did not strictly need a
    human costs a few seconds of an agent's time; failing to let through
    someone who did costs a customer. Two ways to match: a short message that
    is essentially just the word, or any message expressing the intent.
    """
    import re

    from apps.library.event_templates import HUMAN_KEYWORDS

    cleaned = " ".join((text or "").lower().split()).strip(" .!?")
    if not cleaned:
        return False
    if cleaned in HUMAN_KEYWORDS:
        return True

    # "can I speak to someone", "talk to a person please", "chat to an agent"
    intent = re.compile(
        r"\b(speak|talk|chat|call)\b.{0,20}?\b(agent|human|person|someone|operator|consultant)\b"
    )
    if intent.search(cleaned):
        return True

    # A short message that is basically the keyword: "agent please", "need help"
    if len(cleaned.split()) <= 4 and re.search(
        r"\b(agent|human|operator|consultant|help)\b", cleaned
    ):
        return True
    return False


def _wants_to_stop(text):
    """Has this person asked to be left alone?

    Deliberately narrower than the request-a-human match. Opting someone out
    by accident is a worse failure than missing it once, because the next
    message they get is one they explicitly asked not to receive - and they
    told a regulator's favourite word to a financial services provider.
    """
    import re

    cleaned = " ".join((text or "").lower().split()).strip(" .!?")
    if not cleaned:
        return False
    if cleaned in {"stop", "unsubscribe", "opt out", "optout", "remove me", "cancel messages"}:
        return True
    return bool(
        re.search(r"\b(stop|unsubscribe|opt.?out)\b.{0,25}\b(messages?|sending|contacting|list)\b", cleaned)
        or re.search(r"\b(remove|take) me off\b", cleaned)
        or re.search(r"\bdo ?n[o']?t (message|contact|whatsapp) me\b", cleaned)
    )


def _wants_to_resume(text):
    cleaned = " ".join((text or "").lower().split()).strip(" .!?")
    return cleaned in {"start", "resume", "subscribe", "opt in", "optin"}


def _opt_out(conversation, message):
    """Honour STOP: record it, confirm it, and stop the automation."""
    from apps.channels_wa.outbound import send_text

    contact = conversation.contact
    if not contact.is_opted_out:
        contact.opted_out_at = timezone.now()
        contact.save(update_fields=["opted_out_at"])

    conversation.automation_state = {}
    conversation.handoff_reason = "opted out"
    conversation.save(update_fields=["automation_state", "handoff_reason"])

    _stop_event_journey(conversation)

    Message.objects.create(
        workspace=conversation.workspace,
        conversation=conversation,
        direction=Message.Direction.SYSTEM,
        actor=Message.Actor.SYSTEM,
        kind=Message.Kind.TEXT,
        body="Customer replied STOP. No further templates will be sent to this contact.",
        payload={"note": True, "escalation": "opted_out"},
        wa_status=Message.Status.SENT,
    )

    # A reply is allowed and is the right thing to do: they messaged us, so the
    # window is open, it costs nothing, and silence after STOP reads as a fault.
    name = _first_name(conversation)
    send_text(
        conversation,
        (f"{name}, you are off the list." if name else "You are off the list.")
        + " You will not get any further messages from us on this number unless you "
        "message us first.\n\n"
        "If that was a mistake, reply START. If you would rather talk to someone, "
        "reply AGENT.",
        actor=Message.Actor.BOT,
    )
    logger.info("opted out conversation=%s", conversation.pk)


def _opt_in(conversation, message):
    from apps.channels_wa.outbound import send_text

    contact = conversation.contact
    contact.opted_out_at = None
    contact.save(update_fields=["opted_out_at"])

    Message.objects.create(
        workspace=conversation.workspace,
        conversation=conversation,
        direction=Message.Direction.SYSTEM,
        actor=Message.Actor.SYSTEM,
        kind=Message.Kind.TEXT,
        body="Customer replied START. Messaging resumed.",
        payload={"note": True, "escalation": "opted_in"},
        wa_status=Message.Status.SENT,
    )
    name = _first_name(conversation)
    send_text(
        conversation,
        (f"Welcome back, {name}." if name else "Welcome back.")
        + " You are back on the list and I will pick up where we left off.",
        actor=Message.Actor.BOT,
    )
    logger.info("opted back in conversation=%s", conversation.pk)


def _stop_event_journey(conversation):
    """Mark the guest opted out so no campaign picks them up again."""
    try:
        from apps.events.models import Guest

        guest = (
            Guest.objects.for_workspace(conversation.workspace)
            .filter(contact=conversation.contact)
            .first()
        )
        if guest is not None:
            guest.advance(Guest.Stage.STOPPED, stopped_at=timezone.now())
    except Exception:
        logger.exception("could not stop event journey conversation=%s", conversation.pk)


def _first_name(conversation):
    name = (conversation.contact.display_name or "").split()
    return name[0] if name else ""


def _escalate_to_human(conversation, message):
    """Take a conversation away from automation and put it in the queue."""
    from apps.agents.allocation import auto_assign

    conversation.status = Conversation.Status.QUEUED if not conversation.assigned_to_id else conversation.status
    conversation.priority = max(conversation.priority, Conversation.Priority.HIGH)
    conversation.handoff_reason = "asked for a person"
    conversation.automation_state = {}
    conversation.save(update_fields=["status", "priority", "handoff_reason", "automation_state"])

    Message.objects.create(
        workspace=conversation.workspace,
        conversation=conversation,
        direction=Message.Direction.SYSTEM,
        actor=Message.Actor.SYSTEM,
        kind=Message.Kind.TEXT,
        body="Customer asked to speak to a person. Automation has been switched off "
        "for this chat.",
        payload={"note": True, "escalation": "human_requested"},
        wa_status=Message.Status.SENT,
    )
    if conversation.assigned_to_id is None:
        auto_assign(conversation)
    logger.info("human requested conversation=%s", conversation.pk)

    from apps.inbox.alerts import notify_consultant_requested

    notify_consultant_requested(conversation)


def _maybe_send_out_of_hours(conversation):
    workspace = conversation.workspace
    if not workspace.send_out_of_hours_message or workspace.is_open():
        return
    if not workspace.out_of_hours_message.strip():
        return
    # Once per window only - never a repeated auto-reply.
    already = conversation.messages.filter(
        actor=Message.Actor.BOT, payload__auto="out_of_hours"
    ).exists()
    if already:
        return
    from apps.channels_wa.outbound import send_text

    send_text(
        conversation,
        workspace.out_of_hours_message,
        actor=Message.Actor.BOT,
        payload={"auto": "out_of_hours"},
    )
