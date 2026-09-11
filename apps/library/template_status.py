"""Keep the template library in step with Meta's verdicts.

Two sources feed this: the webhook, which is immediate, and the poll, which
catches anything the webhook missed. Both end up here so the rules for what a
Meta event means live in one place.
"""

import logging

from django.utils import timezone

from apps.core.audit import audit
from apps.library.models import MessageTemplate

logger = logging.getLogger(__name__)

# Meta's webhook vocabulary, mapped to the statuses the app shows. Anything
# not listed leaves the status alone but is still recorded.
EVENT_TO_STATUS = {
    "APPROVED": MessageTemplate.Status.APPROVED,
    "REINSTATED": MessageTemplate.Status.APPROVED,
    "REJECTED": MessageTemplate.Status.REJECTED,
    "PENDING": MessageTemplate.Status.PENDING,
    "IN_APPEAL": MessageTemplate.Status.PENDING,
    "PENDING_DELETION": MessageTemplate.Status.DISABLED,
    "DELETED": MessageTemplate.Status.DISABLED,
    "DISABLED": MessageTemplate.Status.DISABLED,
    "PAUSED": MessageTemplate.Status.PAUSED,
}

# The API's list endpoint uses a slightly different vocabulary again.
API_STATUS = {
    "APPROVED": MessageTemplate.Status.APPROVED,
    "PENDING": MessageTemplate.Status.PENDING,
    "IN_APPEAL": MessageTemplate.Status.PENDING,
    "REJECTED": MessageTemplate.Status.REJECTED,
    "PAUSED": MessageTemplate.Status.PAUSED,
    "DISABLED": MessageTemplate.Status.DISABLED,
    "PENDING_DELETION": MessageTemplate.Status.DISABLED,
    "DELETED": MessageTemplate.Status.DISABLED,
}


def _reason(value):
    """Meta puts the explanation in different places depending on the event."""
    reason = value.get("reason") or ""
    if reason in ("", "NONE", None):
        other = value.get("other_info") or value.get("disable_info") or {}
        title = other.get("title") or other.get("disable_date") or ""
        description = other.get("description") or ""
        reason = " - ".join(part for part in (title, description) if part)
    return reason or ""


def apply_status_event(channel, value):
    """A `message_template_status_update` webhook for this channel's account."""
    name = value.get("message_template_name") or ""
    language = value.get("message_template_language") or ""
    event = (value.get("event") or "").upper()
    if not name or not language:
        logger.warning("template status event without a name; ignored channel=%s", channel.pk)
        return None

    template, created = MessageTemplate.objects.get_or_create(
        channel=channel,
        name=name,
        language=language,
        defaults={"workspace": channel.workspace, "status": MessageTemplate.Status.PENDING},
    )
    status = EVENT_TO_STATUS.get(event)
    changed = []
    if status and template.status != status:
        template.status = status
        template.status_changed_at = timezone.now()
        changed += ["status", "status_changed_at"]
    meta_id = str(value.get("message_template_id") or "")
    if meta_id and template.meta_id != meta_id:
        template.meta_id = meta_id
        changed.append("meta_id")
    reason = _reason(value) if status in (MessageTemplate.Status.REJECTED, MessageTemplate.Status.PAUSED, MessageTemplate.Status.DISABLED) else ""
    if template.rejection_reason != reason:
        template.rejection_reason = reason
        changed.append("rejection_reason")
    template.last_synced_at = timezone.now()
    changed.append("last_synced_at")
    template.save(update_fields=changed)

    audit(
        "template.status_event", workspace=channel.workspace, target=template,
        event=event, reason=reason, created=created,
    )
    if created or not template.components:
        # A template authored in WhatsApp Manager that we have never seen:
        # pull its text so it can be previewed and sent.
        from apps.channels_wa.tasks import sync_templates

        try:
            sync_templates.delay(channel.pk)
        except Exception:
            logger.warning("could not queue template sync channel=%s", channel.pk, exc_info=True)
    logger.info("template %s/%s -> %s (%s)", name, language, event, channel.pk)
    return template


def apply_quality_event(channel, value):
    """A `message_template_quality_update` webhook: Meta's read on how the template is landing."""
    name = value.get("message_template_name") or ""
    language = value.get("message_template_language") or ""
    score = (value.get("new_quality_score") or "").upper()
    if not name or not language:
        return None
    template = MessageTemplate.objects.filter(channel=channel, name=name, language=language).first()
    if template is None:
        return None
    template.quality = score
    template.save(update_fields=["quality"])
    audit(
        "template.quality_event", workspace=channel.workspace, target=template,
        previous=value.get("previous_quality_score"), new=score,
    )
    return template


def apply_api_item(channel, item, now=None):
    """One entry from GET /{waba}/message_templates, from the sync or the poll."""
    now = now or timezone.now()
    status = API_STATUS.get((item.get("status") or "").upper(), MessageTemplate.Status.PENDING)
    rejected = item.get("rejected_reason") or ""
    reason = "" if rejected in ("NONE", "") else rejected
    quality = ((item.get("quality_score") or {}).get("score") or "").upper()
    template, created = MessageTemplate.objects.get_or_create(
        channel=channel,
        name=item.get("name", ""),
        language=item.get("language", ""),
        defaults={"workspace": channel.workspace, "status": status},
    )
    fields = {
        "meta_id": str(item.get("id", "")),
        "category": item.get("category", "UTILITY"),
        "components": item.get("components", []),
        "last_synced_at": now,
    }
    if template.status != status:
        fields["status"] = status
        fields["status_changed_at"] = now
    if reason:
        fields["rejection_reason"] = reason
    if quality and quality != "UNKNOWN":
        fields["quality"] = quality
    for field, value in fields.items():
        setattr(template, field, value)
    template.save(update_fields=list(fields))
    return template
