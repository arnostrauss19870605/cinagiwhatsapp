"""Background work for the event journey."""

import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(name="apps.events.tasks.send_rsvp_confirmation", ignore_result=True)
def send_rsvp_confirmation(guest_id):
    """Confirm a website-form RSVP on WhatsApp.

    A form guest has not messaged us, so no free window is open and the only
    way to reach them is the approved `event_registration_received` template.
    If Meta has not approved it yet the send is skipped and noted on the
    journey - the RSVP itself is already safely on the roster.
    """
    from apps.channels_wa.models import WhatsAppChannel
    from apps.channels_wa.outbound import send_template
    from apps.contacts.models import Contact
    from apps.core.audit import audit
    from apps.inbox.models import Conversation, Message
    from apps.library.models import MessageTemplate

    from .models import Guest, JourneyEvent

    guest = (
        Guest.objects.select_related("event", "workspace", "contact")
        .filter(pk=guest_id)
        .first()
    )
    if guest is None or guest.rsvp_status != Guest.Rsvp.ATTENDING or not guest.msisdn:
        return

    def skip(reason):
        logger.info("rsvp confirmation skipped guest=%s: %s", guest_id, reason)
        JourneyEvent.objects.create(
            workspace=guest.workspace, guest=guest, step="confirmation_skipped", detail=reason
        )

    channel = (
        WhatsAppChannel.objects.for_workspace(guest.workspace)
        .filter(is_active=True)
        .order_by("-is_default", "pk")
        .first()
    )
    if channel is None:
        return skip("no active WhatsApp number on this workspace")

    template = (
        MessageTemplate.objects.for_workspace(guest.workspace)
        .filter(
            channel=channel,
            name="event_registration_received",
            status=MessageTemplate.Status.APPROVED,
        )
        .first()
    )
    if template is None:
        return skip("the 'event_registration_received' template is not approved yet")

    contact, _ = Contact.objects.get_or_create(
        workspace=guest.workspace,
        wa_id=guest.msisdn,
        defaults={"display_name": guest.full_name},
    )
    if not contact.display_name:
        contact.display_name = guest.full_name
        contact.save(update_fields=["display_name"])
    if guest.contact_id is None:
        guest.contact = contact
        guest.save(update_fields=["contact"])

    conversation = (
        Conversation.objects.for_workspace(guest.workspace)
        .filter(contact=contact, status__in=Conversation.OPEN_STATUSES)
        .order_by("-last_activity_at")
        .first()
    )
    if conversation is None:
        conversation = Conversation.objects.create(
            workspace=guest.workspace,
            channel=channel,
            contact=contact,
            status=Conversation.Status.BOT,
        )

    message = send_template(
        conversation,
        template,
        [guest.first_name, str(guest.guest_number or ""), guest.event.date_label()],
        actor=Message.Actor.BOT,
    )
    JourneyEvent.objects.create(
        workspace=guest.workspace,
        guest=guest,
        step="rsvp_confirmation_sent" if message.wa_status == Message.Status.SENT
        else "rsvp_confirmation_failed",
        detail=message.wa_status,
    )
    audit(
        "event.rsvp_confirmation",
        workspace=guest.workspace,
        target=guest,
        status=message.wa_status,
    )
