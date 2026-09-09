"""Background work for the template library."""

import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(name="apps.library.tasks.run_bulk_send", ignore_result=True)
def run_bulk_send(workspace_id, template_id, audience_ids, values, header_media=None, actor_id=None,
                  bulk_send_id=None):
    """Send a template to every contact in the chosen audiences.

    Runs on the outbound queue so a 600-person send never ties up a web
    request. The audit row at the end is how the person who pressed the
    button finds out what actually happened.
    """
    from apps.contacts.models import Audience
    from apps.core.audit import audit
    from apps.library.bulk import (
        audience_contacts, resolve_channel, send_to_contacts, start_bulk_send,
    )
    from apps.library.models import BulkSend, MessageTemplate
    from apps.workspaces.models import Workspace

    workspace = Workspace.objects.filter(pk=workspace_id).first()
    template = MessageTemplate.objects.filter(pk=template_id, workspace=workspace).first()
    audiences = list(Audience.objects.filter(pk__in=audience_ids, workspace=workspace))
    bulk_send = BulkSend.objects.filter(pk=bulk_send_id, workspace=workspace).first()
    if workspace is None or template is None or not audiences:
        logger.warning("bulk send dropped: workspace=%s template=%s", workspace_id, template_id)
        _drop(bulk_send)
        return

    channel = resolve_channel(workspace)
    if channel is None:
        logger.warning("bulk send dropped: no active channel workspace=%s", workspace_id)
        _drop(bulk_send)
        return

    contacts = list(audience_contacts(workspace, audiences))
    if bulk_send is None:
        bulk_send = start_bulk_send(
            workspace, channel, template, audiences, values,
            header_media=header_media, created_by_id=actor_id, recipient_count=len(contacts),
        )
    result = send_to_contacts(
        workspace, channel, template, contacts, values, header_media=header_media,
        bulk_send=bulk_send,
    )
    for note in result["notes"]:
        logger.info("bulk send note: %s", note)
    audit(
        "library.bulk_send",
        workspace=workspace,
        actor_id=actor_id,
        target=template,
        audiences=[a.name for a in audiences],
        sent=result["sent"],
        blocked=result["blocked"],
        failed=result["failed"],
    )
    logger.info(
        "bulk send done template=%s sent=%s blocked=%s failed=%s",
        template.name, result["sent"], result["blocked"], result["failed"],
    )


def _drop(bulk_send):
    if bulk_send is not None:
        from apps.library.models import BulkSend

        bulk_send.status = BulkSend.Status.DROPPED
        bulk_send.save(update_fields=["status"])
