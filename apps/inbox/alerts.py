"""WhatsApp alerts to the people on duty.

Two triggers. A customer asks for a consultant: the alert goes at once. A chat
has waited longer than NOTIFICATION_PENDING_MINUTES for a reply: the scheduler
checks every minute and alerts once per wave of customer messages, repeating
every NOTIFICATION_REPEAT_MINUTES while nobody has answered.

Recipients come from NOTIFICATION_NUMBERS in the environment, not the
database. The people on duty are deployment staff, not workspace data, and a
workspace must never be able to point alerts at a stranger's phone.

Alerts leave through the workspace's own number and the normal transport, so
the outbound safety rail applies to them like everything else. They are not
recorded as inbox messages: a nudge to a colleague is not a conversation with
a customer. An approved `consultant_alert` template is used when the channel
has one, because free text only reaches a phone that has messaged the number
in the last 24 hours; without the template the alert is still attempted and a
warning is logged when WhatsApp refuses it.
"""

import datetime as dt
import logging

from django.conf import settings
from django.db.models import F, Q
from django.urls import reverse
from django.utils import timezone

from apps.contacts.models import normalise_msisdn

logger = logging.getLogger(__name__)

ALERT_TEMPLATE = "consultant_alert"
ALERT_LANGUAGE = "en"


def recipients():
    return [n for n in (normalise_msisdn(raw) for raw in settings.NOTIFICATION_NUMBERS) if n]


def inbox_url():
    return f"{settings.PUBLIC_BASE_URL}{reverse('inbox:inbox')}"


def _describe(contact):
    return f"{contact.name} (+{contact.wa_id})"


def _minutes_since(moment, now):
    return max(1, int((now - moment).total_seconds() // 60))


def _template_for(channel):
    from apps.library.models import MessageTemplate

    return MessageTemplate.objects.filter(
        channel=channel,
        name=ALERT_TEMPLATE,
        language=ALERT_LANGUAGE,
        status=MessageTemplate.Status.APPROVED,
    ).first()


def deliver(channel, text):
    """Send one alert text to every number on duty. Never raises."""
    numbers = recipients()
    if not numbers:
        return 0
    # A template parameter may not contain a line break or a run of spaces.
    flat = " ".join(text.split())
    template = _template_for(channel)
    client = channel.client()
    sent = 0
    for number in numbers:
        try:
            if template is not None:
                result = client.send_template(
                    number,
                    template.name,
                    template.language,
                    [{"type": "body", "parameters": [{"type": "text", "text": flat}]}],
                )
            else:
                result = client.send_text(number, text)
        except Exception:
            logger.exception("alert failed to=%s channel=%s", number[-4:], channel.pk)
            continue
        if result.ok:
            sent += 1
        elif result.blocked_reason:
            logger.info("alert blocked to=%s reason=%s", number[-4:], result.blocked_reason)
        else:
            logger.warning(
                "alert refused to=%s channel=%s error=%s template=%s",
                number[-4:], channel.pk, result.error, bool(template),
            )
    return sent


def notify_consultant_requested(conversation):
    """Tell the people on duty that a customer wants a person. Never raises."""
    try:
        text = (
            f"{_describe(conversation.contact)} asked to speak to a consultant on "
            f"{conversation.workspace.name}. Open the inbox to reply: {inbox_url()}"
        )
        return deliver(conversation.channel, text)
    except Exception:
        logger.exception("consultant alert failed conversation=%s", conversation.pk)
        return 0


def pending_conversations(now=None):
    """Open chats whose last message came from the customer and has waited too long."""
    from apps.inbox.models import Conversation

    now = now or timezone.now()
    waited = now - dt.timedelta(minutes=settings.NOTIFICATION_PENDING_MINUTES)
    repeat = now - dt.timedelta(minutes=settings.NOTIFICATION_REPEAT_MINUTES)
    return (
        Conversation.objects.filter(
            status__in=Conversation.OPEN_STATUSES,
            last_inbound_at__lte=waited,
        )
        .filter(Q(last_outbound_at__isnull=True) | Q(last_outbound_at__lt=F("last_inbound_at")))
        .filter(
            Q(pending_alert_at__isnull=True)
            | Q(pending_alert_at__lt=F("last_inbound_at"))
            | Q(pending_alert_at__lte=repeat)
        )
        .exclude(contact__wa_id__in=recipients())
        .select_related("workspace", "channel", "contact")
        .order_by("channel_id", "last_inbound_at")
    )


def notify_pending(now=None):
    """One digest per number, listing the chats still waiting. Never raises."""
    now = now or timezone.now()
    if not recipients():
        return 0
    try:
        by_channel = {}
        for conversation in pending_conversations(now):
            by_channel.setdefault(conversation.channel, []).append(conversation)

        alerted = 0
        for channel, conversations in by_channel.items():
            count = len(conversations)
            noun = "chat is" if count == 1 else "chats are"
            waiting = "; ".join(
                f"{_describe(c.contact)} waiting {_minutes_since(c.last_inbound_at, now)} min"
                for c in conversations[:10]
            )
            more = f"; and {count - 10} more" if count > 10 else ""
            text = (
                f"{count} {noun} waiting for a reply on {channel.workspace.name}: "
                f"{waiting}{more}. Open the inbox: {inbox_url()}"
            )
            if deliver(channel, text):
                from apps.inbox.models import Conversation

                Conversation.objects.filter(pk__in=[c.pk for c in conversations]).update(
                    pending_alert_at=now
                )
                alerted += count
        return alerted
    except Exception:
        logger.exception("pending-chat alert failed")
        return 0
