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


@shared_task(name="apps.channels_wa.tasks.sync_templates", ignore_result=True)
def sync_templates(channel_id=None):
    """Pull Meta-approved templates. Nightly, and on demand from the UI."""
    from django.utils import timezone

    from apps.channels_wa.models import WhatsAppChannel
    from apps.library.models import MessageTemplate

    channels = WhatsAppChannel.objects.filter(is_active=True, status=WhatsAppChannel.Status.CONNECTED)
    if channel_id:
        channels = channels.filter(pk=channel_id)

    synced = 0
    for channel in channels.select_related("workspace"):
        if not channel.waba_id:
            continue
        try:
            templates = channel.client().fetch_templates()
        except Exception:
            logger.warning("template sync failed channel=%s", channel.pk, exc_info=True)
            continue
        for item in templates:
            MessageTemplate.objects.update_or_create(
                channel=channel,
                name=item.get("name", ""),
                language=item.get("language", ""),
                defaults={
                    "workspace": channel.workspace,
                    "meta_id": str(item.get("id", "")),
                    "category": item.get("category", "UTILITY"),
                    "status": item.get("status", "PENDING"),
                    "components": item.get("components", []),
                    "last_synced_at": timezone.now(),
                },
            )
            synced += 1
        channel.templates_synced_at = timezone.now()
        channel.save(update_fields=["templates_synced_at"])
    return synced


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
