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


def send_to_contacts(workspace, channel, template, contacts, values, *,
                     header_media=None, pause=1.0):
    """Send one template to each contact. Returns counts and per-contact notes."""
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
        message = send_template(
            conversation,
            template,
            [first_name_of(contact), *values],
            header_media=header_media,
            actor=Message.Actor.BOT,
        )
        if message.wa_status == Message.Status.SENT:
            sent += 1
        elif message.wa_status == Message.Status.BLOCKED:
            blocked += 1
            notes.append(f"{contact.name}: {message.wa_error.get('reason', 'blocked')}")
        else:
            failed += 1
            notes.append(f"{contact.name}: {message.wa_error.get('reason', 'failed')}")
    return {"sent": sent, "blocked": blocked, "failed": failed, "notes": notes}
