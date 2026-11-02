"""Send a message and record it, in that order, from one place."""

import logging

from django.utils import timezone

from apps.inbox import events
from apps.inbox.models import Conversation, Message

logger = logging.getLogger(__name__)


def _finish(conversation, message, result):
    if result.blocked_reason:
        message.wa_status = Message.Status.BLOCKED
        message.wa_error = {"reason": result.blocked_reason}
    elif result.ok:
        message.wamid = result.wamid
        message.wa_status = Message.Status.SENT
        message.sent_at = timezone.now()
    else:
        message.wa_status = Message.Status.FAILED
        message.wa_error = {"reason": result.error, **(result.raw or {})}
    message.save()

    # An agent writing to the customer takes over from any workflow, whether or
    # not WhatsApp accepted that particular message.
    if message.actor == Message.Actor.AGENT:
        from apps.workflows.engine import stop

        stop(conversation, "an agent replied")

    if result.ok:
        conversation.touch_outbound()
        # A person replying takes the chat out of the queue. A campaign or the
        # automation sending does not: nobody is "with" the customer, and calling
        # it assigned would make every bulk send look like a chat needing help.
        if message.actor == Message.Actor.AGENT and conversation.status in (
            Conversation.Status.QUEUED, Conversation.Status.BOT
        ):
            conversation.status = Conversation.Status.ASSIGNED
        conversation.unread_agent_count = 0
        conversation.save()

    events.conversation_changed(conversation, "message")
    return message


def send_text(conversation, body, *, author=None, actor=Message.Actor.AGENT, snippet=None, payload=None):
    message = Message(
        workspace=conversation.workspace,
        conversation=conversation,
        direction=Message.Direction.OUT,
        actor=actor,
        author=author,
        kind=Message.Kind.TEXT,
        body=body,
        snippet=snippet,
        payload=payload or {},
    )
    result = conversation.channel.client().send_text(conversation.contact.wa_id, body)
    return _finish(conversation, message, result)


def send_location(conversation, latitude, longitude, *, name="", address="", author=None, actor=Message.Actor.AGENT):
    message = Message(
        workspace=conversation.workspace,
        conversation=conversation,
        direction=Message.Direction.OUT,
        actor=actor,
        author=author,
        kind=Message.Kind.LOCATION,
        body=address or name,
        payload={"latitude": latitude, "longitude": longitude, "name": name, "address": address},
    )
    result = conversation.channel.client().send_location(
        conversation.contact.wa_id, latitude, longitude, name=name, address=address
    )
    return _finish(conversation, message, result)


def send_template(conversation, template, values, *, header_media=None, author=None, actor=Message.Actor.AGENT):
    # An invitation to an event that has already happened is worse than no
    # invitation, so the campaign window is enforced here rather than trusted
    # to whoever is clicking send.
    from apps.library.event_templates import sendable_on

    # STOP is absolute. A template is always business-initiated, so this is the
    # one place it has to be checked - an agent replying inside an open window
    # is a conversation the customer started and is not covered by opt-out.
    allowed, reason = True, ""
    if conversation.contact.is_opted_out:
        allowed = False
        # %-d is glibc-only; day formatted portably so Windows dev boxes agree.
        opted = conversation.contact.opted_out_at
        reason = (
            f"{conversation.contact.name} opted out on "
            f"{opted.day} {opted:%B %Y}. Templates are not sent to "
            "contacts who have replied STOP."
        )
    else:
        allowed, reason = sendable_on(template.name)
    if not allowed:
        from apps.channels_wa.messaging.base import SendResult

        message = Message(
            workspace=conversation.workspace,
            conversation=conversation,
            direction=Message.Direction.OUT,
            actor=actor,
            author=author,
            kind=Message.Kind.TEMPLATE,
            body=template.preview(values),
            template=template,
        )
        return _finish(conversation, message, SendResult(ok=False, blocked_reason=reason))

    components = template.build_components(values, header_media=header_media)
    message = Message(
        workspace=conversation.workspace,
        conversation=conversation,
        direction=Message.Direction.OUT,
        actor=actor,
        author=author,
        kind=Message.Kind.TEMPLATE,
        body=template.preview(values),
        template=template,
        payload={
            "template": template.name,
            "language": template.language,
            "values": list(values),
            **({"header_media": header_media} if header_media else {}),
        },
    )
    result = conversation.channel.client().send_template(
        conversation.contact.wa_id, template.name, template.language, components
    )
    return _finish(conversation, message, result)


def send_media(conversation, django_file, *, caption="", author=None):
    """Upload to Meta first, then send by media id, then keep our own copy."""
    channel = conversation.channel
    mime = getattr(django_file, "content_type", "") or "application/octet-stream"
    kind = (
        "image"
        if mime.startswith("image/")
        else "video"
        if mime.startswith("video/")
        else "audio"
        if mime.startswith("audio/")
        else "document"
    )
    message = Message(
        workspace=conversation.workspace,
        conversation=conversation,
        direction=Message.Direction.OUT,
        actor=Message.Actor.AGENT,
        author=author,
        kind=kind,
        body=caption,
        media_mime=mime,
        media_filename=getattr(django_file, "name", ""),
    )
    message.media = django_file

    from apps.channels_wa.comms_guard import outbound_blocked

    blocked = outbound_blocked(conversation.contact.wa_id)
    if blocked:
        from apps.channels_wa.messaging.base import SendResult

        return _finish(conversation, message, SendResult(ok=False, blocked_reason=blocked))

    try:
        django_file.seek(0)
        media_id = channel.client().upload_media(django_file, mime)
        result = channel.client().send_media(
            conversation.contact.wa_id,
            media_id,
            kind=kind,
            caption=caption,
            filename=getattr(django_file, "name", ""),
        )
    except Exception as exc:
        from apps.channels_wa.messaging.base import SendResult

        logger.warning("media send failed conversation=%s", conversation.pk, exc_info=True)
        result = SendResult(ok=False, error=getattr(exc, "friendly", "That file could not be sent."))
    return _finish(conversation, message, result)


def send_buttons(conversation, body, buttons, *, actor=Message.Actor.BOT, payload=None):
    """Up to three tap buttons. `buttons` = [{"id": ..., "title": ...}]."""
    message = Message(
        workspace=conversation.workspace,
        conversation=conversation,
        direction=Message.Direction.OUT,
        actor=actor,
        kind=Message.Kind.INTERACTIVE,
        body=body,
        payload={"buttons": buttons, **(payload or {})},
    )
    result = conversation.channel.client().send_buttons(conversation.contact.wa_id, body, buttons)
    return _finish(conversation, message, result)


def send_list(conversation, body, button_text, rows, *, actor=Message.Actor.BOT, payload=None):
    """A pick-list of up to ten rows. `rows` = [{"id": ..., "title": ...}]."""
    message = Message(
        workspace=conversation.workspace,
        conversation=conversation,
        direction=Message.Direction.OUT,
        actor=actor,
        kind=Message.Kind.INTERACTIVE,
        body=body,
        payload={"list": rows, **(payload or {})},
    )
    sections = [{"title": button_text[:24], "rows": [{"id": r["id"], "title": r["title"][:24]} for r in rows]}]
    result = conversation.channel.client().send_list(
        conversation.contact.wa_id, body, button_text, sections
    )
    return _finish(conversation, message, result)


def send_stored_file(conversation, stored, *, caption="", actor=Message.Actor.BOT, payload=None):
    """Send a file we keep (a workflow's brochure), reusing WhatsApp's copy while it is fresh.

    `stored` has file, filename, mime, media_id, media_uploaded_at and
    media_channel_id. WhatsApp deletes uploaded media after 30 days, so the
    copy is re-uploaded after 25, or when the chat is on a different number.
    """
    import datetime as dt

    from apps.channels_wa.comms_guard import outbound_blocked
    from apps.channels_wa.messaging.base import SendResult

    channel = conversation.channel
    kind = stored.kind
    message = Message(
        workspace=conversation.workspace,
        conversation=conversation,
        direction=Message.Direction.OUT,
        actor=actor,
        kind=kind,
        body=caption,
        media_mime=stored.mime,
        media_filename=stored.filename,
        payload=payload or {},
    )
    blocked = outbound_blocked(conversation.contact.wa_id)
    if blocked:
        return _finish(conversation, message, SendResult(ok=False, blocked_reason=blocked))

    fresh = (
        stored.media_id
        and stored.media_channel_id == channel.pk
        and stored.media_uploaded_at
        and timezone.now() - stored.media_uploaded_at < dt.timedelta(days=25)
    )
    try:
        if not fresh:
            with stored.file.open("rb") as handle:
                stored.media_id = channel.client().upload_media(handle, stored.mime)
            stored.media_uploaded_at = timezone.now()
            stored.media_channel_id = channel.pk
            stored.save(update_fields=["media_id", "media_uploaded_at", "media_channel_id"])
        if not stored.media_id:
            raise ValueError("WhatsApp returned no media id")
        result = channel.client().send_media(
            conversation.contact.wa_id, stored.media_id, kind=kind, caption=caption, filename=stored.filename
        )
    except Exception as exc:
        logger.warning("stored file send failed conversation=%s", conversation.pk, exc_info=True)
        result = SendResult(ok=False, error=getattr(exc, "friendly", "That file could not be sent."))
    return _finish(conversation, message, result)
