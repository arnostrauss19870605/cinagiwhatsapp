"""The bulk send loop, shared by the management command and the UI's task.

One place decides how a batch goes out: reuse the contact's open chat or open
a bot-owned one, personalise {{1}} to the first name, and space the sends so a
campaign never looks like a burst. Opt-outs, the campaign window and the
comms-guard test mode are all enforced further down in send_template and the
transport - a blocked contact becomes a counted, visible message row, never a
silent skip and never an accidental send.
"""

import time

from apps.channels_wa.outbound import send_template
from apps.inbox.models import Conversation, Message
from apps.library.drafts import values_for


def first_name_of(contact):
    name = (contact.display_name or contact.profile_name or "").strip()
    first = name.split()[0] if name else ""
    return first if first and first[0].isalpha() else "there"


def resolve_channel(workspace):
    from apps.channels_wa.models import WhatsAppChannel

    return (
        WhatsAppChannel.objects.for_workspace(workspace)
        .filter(is_active=True)
        .order_by("-is_default", "pk")
        .first()
    )


def audience_contacts(workspace, audiences):
    from apps.contacts.models import Contact

    return (
        Contact.objects.for_workspace(workspace)
        .filter(audiences__in=audiences, is_blocked=False)
        .distinct()
        .order_by("pk")
    )


def start_bulk_send(workspace, channel, template, audiences, values, *,
                    header_media=None, created_by=None, created_by_id=None, recipient_count=0):
    """Record a batch before the first message leaves, so a crash mid-run still shows."""
    from apps.library.models import BulkSend

    return BulkSend.objects.create(
        workspace=workspace,
        channel=channel,
        template=template,
        template_name=template.name,
        audience_names=[a.name for a in audiences],
        values=values if isinstance(values, dict) else list(values),
        header_media=header_media or {},
        created_by_id=created_by.pk if created_by is not None else created_by_id,
        recipient_count=recipient_count,
    )


def send_to_contacts(workspace, channel, template, contacts, values, *,
                     header_media=None, pause=1.0, bulk_send=None):
    """Send one template to each contact. Returns counts and per-contact notes."""
    from django.utils import timezone

    from apps.library.models import BulkSend

    if bulk_send is not None:
        bulk_send.status = BulkSend.Status.RUNNING
        bulk_send.started_at = timezone.now()
        bulk_send.recipient_count = len(contacts)
        bulk_send.save(update_fields=["status", "started_at", "recipient_count"])

    sent = blocked = failed = 0
    notes = []
    for index, contact in enumerate(contacts):
        if index and pause:
            time.sleep(pause)
        conversation = (
            Conversation.objects.for_workspace(workspace)
            .filter(contact=contact, status__in=Conversation.OPEN_STATUSES)
            .order_by("-last_activity_at")
            .first()
        )
        if conversation is None:
            conversation = Conversation.objects.create(
                workspace=workspace,
                channel=channel,
                contact=contact,
                status=Conversation.Status.BOT,
            )
        try:
            resolved = values_for(template, contact, values)
        except ValueError as exc:
            message = Message.objects.create(
                workspace=workspace, conversation=conversation, direction=Message.Direction.OUT,
                actor=Message.Actor.BOT, kind=Message.Kind.TEMPLATE, template=template,
                body=template.preview([]), wa_status=Message.Status.BLOCKED,
                wa_error={"reason": f"Not sent: {exc} for {contact.name}."},
                bulk_send=bulk_send,
            )
            blocked += 1
            notes.append(f"{contact.name}: {exc}")
            continue
        message = send_template(
            conversation,
            template,
            resolved,
            header_media=header_media,
            actor=Message.Actor.BOT,
        )
        if bulk_send is not None:
            Message.objects.filter(pk=message.pk).update(bulk_send=bulk_send)
        if message.wa_status == Message.Status.SENT:
            sent += 1
        elif message.wa_status == Message.Status.BLOCKED:
            blocked += 1
            notes.append(f"{contact.name}: {message.wa_error.get('reason', 'blocked')}")
        else:
            failed += 1
            notes.append(f"{contact.name}: {message.wa_error.get('reason', 'failed')}")

    if bulk_send is not None:
        bulk_send.status = BulkSend.Status.DONE
        bulk_send.finished_at = timezone.now()
        bulk_send.sent_count, bulk_send.blocked_count, bulk_send.failed_count = sent, blocked, failed
        bulk_send.save(
            update_fields=["status", "finished_at", "sent_count", "blocked_count", "failed_count"]
        )
    return {"sent": sent, "blocked": blocked, "failed": failed, "notes": notes}
