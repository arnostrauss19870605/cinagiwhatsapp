import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(name="apps.channels_wa.tasks.process_inbound_payload", ignore_result=True)
def process_inbound_payload(payload):
    """Fan a Meta webhook payload out to the right workspace(s)."""
    from apps.channels_wa.inbound import process_value
    from apps.channels_wa.models import WhatsAppChannel

    for entry in payload.get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            value = change.get("value") or {}
            field = change.get("field") or "messages"
            if field in TEMPLATE_FIELDS:
                _process_template_event(str(entry.get("id") or ""), field, value)
                continue
            phone_number_id = (value.get("metadata") or {}).get("phone_number_id")
            channel = WhatsAppChannel.objects.filter(
                phone_number_id=phone_number_id, is_active=True
            ).select_related("workspace").first()
            if channel is None:
                logger.warning("no channel for phone_number_id=%s", phone_number_id)
                continue
            try:
                process_value(channel, value)
            except Exception:
                logger.exception("inbound processing failed channel=%s", channel.pk)


TEMPLATE_FIELDS = {"message_template_status_update", "message_template_quality_update"}


def _process_template_event(waba_id, field, value):
    """Approval, rejection, pause and quality events for every number on that account."""
    from apps.channels_wa.models import WhatsAppChannel
    from apps.library.template_status import apply_quality_event, apply_status_event

    channels = WhatsAppChannel.objects.filter(waba_id=waba_id, is_active=True).select_related("workspace")
    if not channels:
        logger.warning("template event for unknown account waba=%s", waba_id)
        return
    for channel in channels:
        try:
            if field == "message_template_status_update":
                apply_status_event(channel, value)
            else:
                apply_quality_event(channel, value)
        except Exception:
            logger.exception("template event failed channel=%s field=%s", channel.pk, field)


@shared_task(name="apps.channels_wa.tasks.sync_templates", ignore_result=True)
def sync_templates(channel_id=None):
    """Pull Meta-approved templates. Nightly, and on demand from the UI."""
    from django.utils import timezone

    from apps.channels_wa.models import WhatsAppChannel
    from apps.library.models import MessageTemplate

    channels = WhatsAppChannel.objects.filter(is_active=True, status=WhatsAppChannel.Status.CONNECTED)
    if channel_id:
        channels = channels.filter(pk=channel_id)

    from apps.library.template_status import apply_api_item

    synced = 0
    for channel in channels.select_related("workspace"):
        if not channel.waba_id:
            continue
        try:
            templates = channel.client().fetch_templates()
        except Exception:
            logger.warning("template sync failed channel=%s", channel.pk, exc_info=True)
            continue
        now = timezone.now()
        for item in templates:
            apply_api_item(channel, item, now=now)
            synced += 1
        channel.templates_synced_at = now
        channel.save(update_fields=["templates_synced_at"])
    return synced


@shared_task(name="apps.channels_wa.tasks.poll_template_statuses", ignore_result=True)
def poll_template_statuses():
    """Every ten minutes, re-check any account still waiting on a Meta verdict.

    The webhook is the primary route; this is the safety net for a missed
    event. Only channels with a template pending in the last week are polled,
    so a template Meta never answers does not keep the poll busy forever.
    """
    import datetime as dt

    from django.utils import timezone

    from apps.channels_wa.models import WhatsAppChannel
    from apps.library.models import MessageTemplate

    recent = timezone.now() - dt.timedelta(days=7)
    waiting = (
        MessageTemplate.objects.filter(status=MessageTemplate.Status.PENDING, updated_at__gte=recent)
        .values_list("channel_id", flat=True)
        .distinct()
    )
    polled = 0
    for channel_id in waiting:
        if WhatsAppChannel.objects.filter(pk=channel_id, is_active=True, status=WhatsAppChannel.Status.CONNECTED).exists():
            sync_templates(channel_id)
            polled += 1
    return polled


@shared_task(name="apps.channels_wa.tasks.alert_pending_chats", ignore_result=True)
def alert_pending_chats():
    """Nudge the people on duty about chats still waiting for a reply, every minute."""
    from apps.inbox.alerts import notify_pending

    return notify_pending()


@shared_task(name="apps.channels_wa.tasks.sweep_unassigned", ignore_result=True)
def sweep_unassigned():
    """Anything still waiting gets another shot at an agent, every minute."""
    from apps.agents.allocation import auto_assign
    from apps.inbox.models import Conversation

    assigned = 0
    for conversation in Conversation.objects.filter(
        status=Conversation.Status.QUEUED, assigned_to__isnull=True
    ).select_related("workspace", "contact")[:200]:
        if auto_assign(conversation):
            assigned += 1
    return assigned
