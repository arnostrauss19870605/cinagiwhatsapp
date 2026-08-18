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
