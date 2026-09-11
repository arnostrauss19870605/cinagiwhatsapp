"""Turn a button tap into an answer, tied to the message it answered.

WhatsApp sends a quick-reply tap as a `button` message carrying the id of the
template message it replies to. That id is the whole basis of attribution:
it finds the exact send, and through it the campaign, the question and the
person. A tap with no such context is only accepted when it plainly matches
a button on a question that person was asked in the last week.
"""

import datetime as dt
import logging

from django.utils import timezone

from apps.core.audit import audit
from apps.inbox.models import Message
from apps.questions.models import Answer

logger = logging.getLogger(__name__)

RECENT = dt.timedelta(days=7)


def _sent_message_for(conversation, message):
    """The outbound template message this reply answers, or None."""
    payload = message.payload or {}
    context_id = (payload.get("context") or {}).get("id") or ""
    sent = Message.objects.filter(
        workspace=conversation.workspace,
        conversation__contact=conversation.contact,
        direction=Message.Direction.OUT,
        template__isnull=False,
    ).select_related("template")
    if context_id:
        return sent.filter(wamid=context_id).first()
    # No context: the most recent question sent to this person recently,
    # provided the reply text matches one of its buttons exactly.
    candidates = (
        sent.filter(created_at__gte=timezone.now() - RECENT, template__question__isnull=False)
        .order_by("-created_at")[:5]
    )
    for candidate in candidates:
        if candidate.template.question.option_for(message.body) is not None:
            return candidate
    return None


def record_answer(conversation, message):
    """Record the answer this inbound message represents. Returns the Answer, or None.

    None means "not an answer to any question": the caller should treat the
    message as an ordinary reply. Never raises.
    """
    try:
        return _record(conversation, message)
    except Exception:
        logger.exception("answer recording failed message=%s", message.pk)
        return None


def _record(conversation, message):
    if message.kind not in (Message.Kind.TEXT, Message.Kind.INTERACTIVE, Message.Kind.BUTTON):
        return None
    sent = _sent_message_for(conversation, message)
    if sent is None:
        return None
    question = getattr(sent.template, "question", None)
    if question is None or not question.is_active:
        return None

    option = question.option_for(message.body)
    if option is None:
        payload = (message.payload or {}).get("button") or {}
        option = question.option_for(payload.get("payload", ""))
    if option is None and message.kind == Message.Kind.TEXT:
        return None  # free text that is not a button label is a conversation, not an answer

    now = timezone.now()
    answer, created = Answer.objects.get_or_create(
        question=question,
        contact=conversation.contact,
        defaults={
            "workspace": conversation.workspace,
            "option": option,
            "raw_text": (message.body or "")[:200],
            "sent_message": sent,
            "reply_message": message,
            "bulk_send": sent.bulk_send,
            "score": option.score if option else 0,
            "is_correct": bool(option and option.is_correct),
            "first_answered_at": now,
            "answered_at": now,
        },
    )
    previous_option = None
    if not created:
        # Meta redelivers webhooks; the same tap must not count as a change.
        if message.wamid and answer.reply_message_id and answer.reply_message.wamid == message.wamid:
            return answer
        previous_option = answer.option
        answer.option = option
        answer.raw_text = (message.body or "")[:200]
        answer.reply_message = message
        answer.score = option.score if option else 0
        answer.is_correct = bool(option and option.is_correct)
        answer.answered_at = now
        answer.taps += 1
        answer.save()

    _apply_audience(conversation.contact, option, previous_option)
    audit(
        "question.answered", workspace=conversation.workspace, target=question,
        contact=conversation.contact.pk, option=option.value if option else "",
        changed=not created,
    )
    if created and question.thanks_text.strip():
        _thank(conversation, question)
    return answer


def _apply_audience(contact, option, previous_option):
    """Join the chosen answer's audience; leave the one the earlier tap put them in."""
    if previous_option is not None and previous_option.audience_id and (
        option is None or previous_option.audience_id != option.audience_id
    ):
        previous_option.audience.contacts.remove(contact)
    if option is not None and option.audience_id:
        option.audience.contacts.add(contact)


def _thank(conversation, question):
    from apps.channels_wa.outbound import send_text
    from apps.library.bulk import first_name_of

    text = question.thanks_text.replace("{{first_name}}", first_name_of(conversation.contact))
    try:
        send_text(conversation, text, actor=Message.Actor.BOT, payload={"auto": "question_thanks"})
    except Exception:
        logger.warning("thank-you after answer failed conversation=%s", conversation.pk, exc_info=True)
