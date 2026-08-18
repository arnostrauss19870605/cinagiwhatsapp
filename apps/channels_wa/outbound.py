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

    if result.ok:
        conversation.touch_outbound()
        if conversation.status in (Conversation.Status.QUEUED, Conversation.Status.BOT):
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


def send_template(conversation, template, values, *, author=None, actor=Message.Actor.AGENT):
    components = template.build_components(values)
    message = Message(
        workspace=conversation.workspace,
        conversation=conversation,
        direction=Message.Direction.OUT,
        actor=actor,
        author=author,
        kind=Message.Kind.TEMPLATE,
        body=template.preview(values),
        template=template,
        payload={"template": template.name, "language": template.language, "values": list(values)},
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
