"""The conversation the guest actually has.

Ordering matters and is deliberate:

    1. a request for a human always wins (handled upstream in inbound.py)
    2. a token the guest tapped - RSVP-xxxx, CHECKIN-xxxx
    3. a button they tapped, answered against the step they are on
    4. free text, read for an obvious RSVP, otherwise left to a human

Every reply here is a plain message, not a template, because the guest opened
the window by messaging first. That is the difference between a few cents and
a few rand per guest across a 600 person list.
"""

import logging
import re

from django.utils import timezone

from apps.channels_wa.outbound import send_location, send_text
from apps.inbox.models import Message

from .models import Event, Guest, JourneyEvent

logger = logging.getLogger(__name__)

# The trailing lookahead, not \b. Tokens are base64url, so about one in sixty
# ends in a hyphen - and \b cannot hold after a hyphen, so the match silently
# stopped short and the guest was never found. Roughly ten brokers in a list of
# six hundred would have tapped their link and got nothing.
TOKEN = re.compile(
    r"\b(RSVP|CHECKIN|STATION)-([A-Za-z0-9_\-]{6,})(?![A-Za-z0-9_\-])", re.I
)

INTERESTS = ["AI onboarding", "API integrations", "Advisor tools", "Pricing", "Client portal"]


def handle(conversation, message):
    """Claim this message for the event journey, or return False."""
    text = (message.body or "").strip()
    guest = _guest_for(conversation)

    match = TOKEN.search(text)
    if match:
        return _handle_token(conversation, message, match.group(1).upper(), match.group(2))

    if guest is None:
        return False

    if message.kind == Message.Kind.INTERACTIVE or _looks_like_button(text):
        if _handle_choice(conversation, guest, text):
            return True

    return _handle_free_text(conversation, guest, text)


# -- entry ------------------------------------------------------------------

def _handle_token(conversation, message, kind, token):
    field = "invite_token" if kind == "RSVP" else "ticket_token"
    guest = Guest.objects.for_workspace(conversation.workspace).filter(**{field: token}).first()
    if guest is None:
        logger.info("unknown %s token on conversation=%s", kind, conversation.pk)
        return False

    # Link the WhatsApp contact to the guest record the first time they message.
    if guest.contact_id is None:
        guest.contact = conversation.contact
        guest.msisdn = guest.msisdn or conversation.contact.wa_id
        guest.save(update_fields=["contact", "msisdn"])
    if not conversation.contact.display_name:
        conversation.contact.display_name = guest.full_name
        conversation.contact.save(update_fields=["display_name"])

    if kind == "CHECKIN":
        return _check_in(conversation, guest)

    if guest.first_message_at is None:
        guest.advance(Guest.Stage.ENGAGED, first_message_at=timezone.now())

    return _ask_rsvp(conversation, guest)


def _ask_rsvp(conversation, guest):
    event = guest.event
    if guest.rsvp_status == Guest.Rsvp.ATTENDING:
        send_text(
            conversation,
            f"You are already confirmed, {guest.first_name} - guest number "
            f"{guest.guest_number}. See you on {event.date_label()}.\n\n"
            "Reply CHANGE if something has come up, or ask me anything about the day.",
            actor=Message.Actor.BOT,
        )
        return True

    if not event.rsvp_open:
        send_text(
            conversation,
            f"Thanks {guest.first_name}, but RSVPs for {event.name} have closed. "
            "Reply AGENT and someone will see whether we can still fit you in.",
            actor=Message.Actor.BOT,
        )
        return True

    send_text(
        conversation,
        f"Hi {guest.first_name}, good to hear from you.\n\n"
        f"{event.name}\n{event.date_label()}, doors from {event.doors_label()}\n{event.venue}\n\n"
        "Can you make it? Reply with a number:\n"
        "1  Yes, I will be there\n"
        "2  I am based outside Gauteng and would like the launch pack sent to me\n"
        "3  I cannot make it this time",
        actor=Message.Actor.BOT,
    )
    JourneyEvent.objects.create(workspace=guest.workspace, guest=guest, step="rsvp_asked")
    return True


# -- answers ----------------------------------------------------------------

def _handle_choice(conversation, guest, text):
    choice = text.strip().lower()

    if choice in {"1", "yes", "count me in", "rsvp now", "i will be there"}:
        return _confirm(conversation, guest, Guest.Rsvp.ATTENDING)
    if choice in {"2", "pack", "launch pack", "send launch pack", "outside gauteng"}:
        return _confirm(conversation, guest, Guest.Rsvp.PACK)
    if choice in {"3", "no", "not this time", "cannot make it", "plans changed"}:
        return _confirm(conversation, guest, Guest.Rsvp.DECLINED)
    if choice in {"add to calendar", "calendar", "add to diary"}:
        return _send_calendar(conversation, guest)
    if choice in {"send venue pin", "venue pin", "send pin", "directions", "venue"}:
        return _send_venue_pin(conversation, guest)
    if choice in {"change", "cancel"}:
        guest.advance(Guest.Stage.ENGAGED, rsvp_status=Guest.Rsvp.NONE)
        return _ask_rsvp(conversation, guest)
    return False


def _confirm(conversation, guest, status):
    event = guest.event

    if status == Guest.Rsvp.ATTENDING and event.is_full:
        guest.advance(Guest.Stage.ENGAGED, rsvp_status=Guest.Rsvp.WAITLIST, rsvp_at=timezone.now())
        send_text(
            conversation,
            f"We are full, {guest.first_name} - but you are first in line if a seat "
            "opens, and I will message you the moment one does.\n\n"
            "We will share the launch pack with you if no seat becomes available.",
            actor=Message.Actor.BOT,
        )
        return True

    number = None
    if status == Guest.Rsvp.ATTENDING and guest.guest_number is None:
        number = event.next_guest_number()

    guest.advance(
        Guest.Stage.CONFIRMED,
        rsvp_status=status,
        rsvp_at=timezone.now(),
        **({"guest_number": number} if number else {}),
    )

    if status == Guest.Rsvp.ATTENDING:
        send_text(
            conversation,
            f"You are in, {guest.first_name} - guest number {guest.guest_number}.\n\n"
            f"{event.date_label()}, registration from {event.doors_label()}\n{event.venue}\n\n"
            "I will send your entry code closer to the time. Anything you need before "
            "then, just ask me here.\n\n"
            "Is anyone coming with you? Reply +1 or +2, or just say no.",
            actor=Message.Actor.BOT,
        )
    elif status == Guest.Rsvp.PACK:
        send_text(
            conversation,
            f"Noted, {guest.first_name} - which region are you in? We will send you a "
            "link to our launch pack after the event.",
            actor=Message.Actor.BOT,
        )
    else:
        send_text(
            conversation,
            f"That is a pity, {guest.first_name}. I have released your seat.\n\n"
            "Would you like the recap afterwards - slides, demos and everything we "
            "announce? Reply YES and I will send it.",
            actor=Message.Actor.BOT,
        )
    return True


def _send_calendar(conversation, guest):
    """Answer the 'Add to calendar' button with a link, not a file.

    WhatsApp will not carry a .ics as a document - text/calendar is not one of
    Meta's allowed document types - so the file lives on our own domain and the
    guest gets a short link. Tapping it on a phone opens the calendar app
    directly, which is fewer steps than an attachment anyway.
    """
    from . import calendar as ics

    send_text(
        conversation,
        f"Here you go, {guest.first_name} - tap to add it to your diary:\n"
        f"{ics.link_for(guest)}",
        actor=Message.Actor.BOT,
    )
    JourneyEvent.objects.create(workspace=guest.workspace, guest=guest, step="calendar_sent")
    return True


def _send_venue_pin(conversation, guest):
    """Answer the 'Send venue pin' button.

    A live map pin only goes out when someone has set trusted coordinates on
    the event - a pin on the wrong gate is worse than no pin. Without them the
    guest gets a maps link for the street address, which their phone resolves.
    """
    from urllib.parse import quote_plus

    event = guest.event
    if event.venue_lat is not None and event.venue_lng is not None:
        send_location(
            conversation,
            event.venue_lat,
            event.venue_lng,
            name=event.name,
            address=event.venue,
            actor=Message.Actor.BOT,
        )
    else:
        send_text(
            conversation,
            f"Here you go, {guest.first_name} - tap for directions:\n"
            f"https://www.google.com/maps/search/?api=1&query={quote_plus(event.venue or 'Bryanston')}",
            actor=Message.Actor.BOT,
        )
    JourneyEvent.objects.create(workspace=guest.workspace, guest=guest, step="venue_pin_sent")
    return True


def _handle_free_text(conversation, guest, text):
    """Read an obvious RSVP out of ordinary language. Anything else is a human's job."""
    lowered = text.lower()

    party = re.search(r"\+\s*([12])\b|bring(?:ing)? (\w+)|with (\w+) colleagues?", lowered)
    if party and guest.rsvp_status == Guest.Rsvp.ATTENDING:
        extra = 1
        if party.group(1):
            extra = int(party.group(1))
        elif party.group(2) in {"two", "2"} or party.group(3) in {"two", "2"}:
            extra = 2
        guest.party_size = 1 + extra
        guest.save(update_fields=["party_size"])
        send_text(
            conversation,
            f"Got it - {guest.party_size} of you. Send me their names and numbers when "
            "you have them and I will look after them too.",
            actor=Message.Actor.BOT,
        )
        return True

    if guest.rsvp_status == Guest.Rsvp.NONE:
        if re.search(r"\b(yes|i will be there|count me in|see you there)\b", lowered):
            return _confirm(conversation, guest, Guest.Rsvp.ATTENDING)
        if re.search(r"\b(no|can'?t|cannot|unable|another time)\b", lowered):
            return _confirm(conversation, guest, Guest.Rsvp.DECLINED)

    # Not an RSVP. Leave it for a person rather than guess.
    return False


# -- day of -----------------------------------------------------------------

def _check_in(conversation, guest):
    event = guest.event
    if guest.checked_in_at:
        send_text(
            conversation,
            f"You are already checked in, {guest.first_name}. Enjoy the morning.",
            actor=Message.Actor.BOT,
        )
        return True
    if guest.rsvp_status not in {Guest.Rsvp.ATTENDING, Guest.Rsvp.WAITLIST}:
        send_text(
            conversation,
            f"Welcome, {guest.first_name} - I do not have you on the list, but the "
            "team at the door will sort you out. Reply AGENT if you need me.",
            actor=Message.Actor.BOT,
        )
        return True

    guest.advance(Guest.Stage.CHECKED_IN, checked_in_at=timezone.now())
    send_text(
        conversation,
        f"Welcome, {guest.first_name}. You are checked in.\n\n"
        "Coffee and refreshments are at the back. Please be seated by 09h00.\n\n"
        "I will send the slides and anything else you need during the morning. Ask me "
        "anything.",
        actor=Message.Actor.BOT,
    )
    return True


def _guest_for(conversation):
    return (
        Guest.objects.for_workspace(conversation.workspace)
        .filter(contact=conversation.contact, event__is_active=True)
        .order_by("-created_at")
        .first()
    )


def _looks_like_button(text):
    return len(text) <= 40
