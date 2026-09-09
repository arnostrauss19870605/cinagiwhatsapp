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


@shared_task(name="apps.library.tasks.run_bulk_resend", ignore_result=True)
def run_bulk_resend(bulk_send_id, contact_ids):
    """Send an existing batch's message again, to a chosen subset of its people.

    The new batch was recorded when the button was pressed; this just does
    the sending, with the same template, values and header file as before.
    """
    from apps.contacts.models import Contact
    from apps.core.audit import audit
    from apps.library.bulk import send_to_contacts
    from apps.library.models import BulkSend

    bulk_send = BulkSend.objects.select_related("workspace", "channel", "template").filter(pk=bulk_send_id).first()
    if bulk_send is None or bulk_send.template is None or bulk_send.channel is None:
        logger.warning("bulk resend dropped: batch=%s", bulk_send_id)
        _drop(bulk_send)
        return
    contacts = list(
        Contact.objects.for_workspace(bulk_send.workspace).filter(pk__in=contact_ids, is_blocked=False).order_by("pk")
    )
    result = send_to_contacts(
        bulk_send.workspace, bulk_send.channel, bulk_send.template, contacts, bulk_send.values,
        header_media=bulk_send.header_media or None, bulk_send=bulk_send,
    )
    audit(
        "library.bulk_resend", workspace=bulk_send.workspace, actor_id=bulk_send.created_by_id,
        target=bulk_send.template, sent=result["sent"], blocked=result["blocked"], failed=result["failed"],
    )


def _drop(bulk_send):
    if bulk_send is not None:
        from apps.library.models import BulkSend

        bulk_send.status = BulkSend.Status.DROPPED
        bulk_send.save(update_fields=["status"])
