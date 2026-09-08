"""Background work for the template library."""

import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(name="apps.library.tasks.run_bulk_send", ignore_result=True)
def run_bulk_send(workspace_id, template_id, audience_ids, values, header_media=None, actor_id=None):
    """Send a template to every contact in the chosen audiences.

    Runs on the outbound queue so a 600-person send never ties up a web
    request. The audit row at the end is how the person who pressed the
    button finds out what actually happened.
    """
    from apps.contacts.models import Audience
    from apps.core.audit import audit
    from apps.library.bulk import audience_contacts, resolve_channel, send_to_contacts
    from apps.library.models import MessageTemplate
    from apps.workspaces.models import Workspace

    workspace = Workspace.objects.filter(pk=workspace_id).first()
    template = MessageTemplate.objects.filter(pk=template_id, workspace=workspace).first()
    audiences = list(Audience.objects.filter(pk__in=audience_ids, workspace=workspace))
    if workspace is None or template is None or not audiences:
        logger.warning("bulk send dropped: workspace=%s template=%s", workspace_id, template_id)
        return

    channel = resolve_channel(workspace)
    if channel is None:
        logger.warning("bulk send dropped: no active channel workspace=%s", workspace_id)
        return

    contacts = list(audience_contacts(workspace, audiences))
    result = send_to_contacts(
        workspace, channel, template, contacts, values, header_media=header_media
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
