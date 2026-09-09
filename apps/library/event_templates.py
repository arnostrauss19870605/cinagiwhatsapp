"""Message templates for the 16 September 2026 event, as data.

Kept in version control so the whole set can be submitted, reviewed and
re-submitted for the next event without anyone retyping copy into a web form.

    python manage.py submit_templates --dry-run
    python manage.py submit_templates

Rules encoded here, learned the hard way:
  * language is "en" everywhere - there is no en_ZA, and the code must match
    exactly at send time or the send fails with "template not found";
  * a message about an event the guest already registered for is UTILITY;
    anything promoting the event to someone who has not said yes is MARKETING;
  * samples must look like real values - placeholder-looking samples get the
    template rejected;
  * quick-reply button text stays under 20 characters so it does not truncate;
  * every template carries the same footer, because a person must always be
    able to reach a person. Buttons are capped at three and are often full;
    a footer costs no button slot and cannot be crowded out;
  * every template declares the window in which sending it makes sense. An
    invite to an event that has happened is worse than no invite at all, so
    the app refuses to send outside that window rather than trusting a human
    to remember.
"""

import datetime as dt

LANGUAGE = "en"

# The one place the date lives. Change it here and every send window re-bases
# automatically. Crucially, the date is NOT written into the approved copy: it
# is passed as a variable at send time, so moving the event costs nothing at
# Meta. Baking a date into template text means ten edits, ten re-reviews and a
# 24-hour cooling-off period on each - and Meta does not care that your venue
# moved.
EVENT_DATE = dt.date(2026, 9, 30)          # Wednesday 30 September 2026


def event_label(date=None):
    """How the date reads inside a message: 'Wednesday 30 September'."""
    date = date or EVENT_DATE
    return f"{date:%A} {date.day} {date:%B}"


def short_label(date=None):
    """'30 September' - for mid-sentence use."""
    date = date or EVENT_DATE
    return f"{date.day} {date:%B}"

# The escape hatch, on every single template. 41 characters; Meta allows 60.
HUMAN_FOOTER = "Reply AGENT any time to talk to a person."

# Words that must always reach a human, whatever else is going on.
HUMAN_KEYWORDS = ["agent", "human", "person", "help", "speak to someone", "operator"]

# Header media samples. Meta only needs a representative file for review; the
# real image or document is supplied per send.
SAMPLE_IMAGE = "docs/samples/header_sample.png"
SAMPLE_DOCUMENT = "docs/samples/recap_sample.pdf"
# The real agenda: the invite pack is reviewed against the file it will send.
AGENDA_DOCUMENT = "docs/samples/launch_agenda.pdf"


def body(text, *examples):
    component = {"type": "BODY", "text": text}
    if examples:
        component["example"] = {"body_text": [list(examples)]}
    return component


def footer(text=HUMAN_FOOTER):
    return {"type": "FOOTER", "text": text}


def buttons(*labels):
    return {
        "type": "BUTTONS",
        "buttons": [{"type": "QUICK_REPLY", "text": label} for label in labels[:3]],
    }


def image_header():
    return {"type": "HEADER", "format": "IMAGE", "_sample": SAMPLE_IMAGE}


def document_header(sample=SAMPLE_DOCUMENT):
    return {"type": "HEADER", "format": "DOCUMENT", "_sample": sample}


TEMPLATES = [
    {
        "name": "event_invite",
        "category": "MARKETING",
        "send_from": dt.date(2026, 8, 25),
        "send_until": EVENT_DATE - dt.timedelta(days=1),
        "components": [
            body(
                "Hi {{1}}, Cinagi is hosting its Annual Product Update and Launch on "
                "{{2}} in Bryanston, and we would like you there.\n\n"
                "Three product announcements, live demos, and breakfast from 08h30.\n\n"
                "Tap below and I will take your RSVP right here in this chat. It takes "
                "about a minute.",
                "Thabo",
                "Wednesday 30 September",
            ),
            footer(),
            buttons("RSVP now", "Not this time", "Cinagi Consultant"),
        ],
    },
    {
        "name": "event_rsvp_reminder",
        "category": "MARKETING",
        "send_from": dt.date(2026, 9, 5),
        "send_until": EVENT_DATE - dt.timedelta(days=1),
        "components": [
            body(
                "Hi {{1}}, seats for the Cinagi Product Launch on {{2}} are filling up "
                "and I have not heard back from you yet.\n\n"
                "Tap below if you would like one, or let me know if you are "
                "unavailable to attend.",
                "Thabo",
                "30 September",
            ),
            footer(),
            buttons("Count me in", "Send launch pack", "Not this time"),
        ],
    },
    {
        "name": "event_agenda_reveal",
        "category": "MARKETING",
        "send_from": dt.date(2026, 9, 2),
        "send_until": EVENT_DATE - dt.timedelta(days=1),
        "components": [
            image_header(),
            body(
                "Hi {{1}}, the agenda for {{2}} is out.\n\n"
                "Based on what you told me, the session on {{3}} is the one to watch. "
                "It runs mid-morning, right after the keynote.\n\n"
                "The full agenda is in the image above. Ask me anything about it.",
                "Thabo",
                "30 September",
                "API integrations",
            ),
            footer(),
            buttons("Ask about the agenda", "Cinagi Consultant"),
        ],
    },
    {
        "name": "event_teaser",
        "category": "MARKETING",
        "send_from": dt.date(2026, 9, 9),
        "send_until": EVENT_DATE - dt.timedelta(days=1),
        "components": [
            image_header(),
            body(
                "Hi {{1}}, one week to go.\n\n"
                "We are announcing three things on {{2}}. One of them has been the "
                "single most requested item from brokers for two years running.\n\n"
                "Want to guess which?",
                "Thabo",
                "30 September",
            ),
            footer(),
            buttons("Guess the announcement", "See the agenda", "Cinagi Consultant"),
        ],
    },
    {
        # The agenda as a PDF the guest can keep. Business-initiated, so it
        # rides the same rules as the rest of the drip: MARKETING category,
        # frequency-capped, batched.
        "name": "event_agenda",
        "category": "MARKETING",
        "send_from": dt.date(2026, 9, 2),
        "send_until": EVENT_DATE - dt.timedelta(days=1),
        "components": [
            document_header(),
            body(
                "Hi {{1}}, here is the full agenda for {{2}} - every session, "
                "time and speaker in the document above.\n\n"
                "Ask me anything about the day.",
                "Thabo",
                "Wednesday 30 September",
            ),
            footer(),
            buttons("Ask about the agenda", "Cinagi Consultant"),
        ],
    },
    {
        # Thank-you to guests who have RSVP'd, with the agenda PDF attached.
        # MARKETING, not UTILITY: Meta files any template that mixes a
        # confirmation with promotional content (here, the prize draw) as
        # marketing, and a UTILITY submission would come back INCORRECT_CATEGORY.
        "name": "event_rsvp_thanks_agenda",
        "category": "MARKETING",
        "send_from": dt.date(2026, 9, 9),
        "send_until": EVENT_DATE - dt.timedelta(days=1),
        "components": [
            document_header(AGENDA_DOCUMENT),
            body(
                "Hi {{1}},\n\n"
                "Thank you for RSVP’ing to the Cinagi 2027 Product Update & Launch. "
                "We’re looking forward to welcoming you! We’ve attached the agenda "
                "and event details for easy reference.\n\n"
                "And to make the lead-up a little more interesting… 🎁\n\n"
                "Keep an eye on your WhatsApp — we’ll be sending a few quick "
                "questions, with each answer earning you an entry into our prize draw, "
                "plus a bonus entry up for grabs on the day.\n\n"
                "See you there!\n\n"
                "The Cinagi Team",
                "Arno",
            ),
            footer(),
            buttons("Cinagi Consultant"),
        ],
    },
    {
        # Internal: goes to the phones in NOTIFICATION_NUMBERS, never to a
        # customer, which is why it carries no footer or buttons and has no
        # send window. A template because free text only reaches a phone
        # that messaged the number in the last 24 hours; the people on duty
        # usually have not. See apps/inbox/alerts.py.
        "name": "consultant_alert",
        "category": "UTILITY",
        "internal": True,
        "components": [
            body(
                "Cinagi inbox alert: {{1}}\n\n"
                "Please open the inbox and reply to the customer.",
                "Thabo Mokoena (+27726124698) asked to speak to a consultant on "
                "Cinagi Broker Support. Open the inbox to reply: "
                "https://brokers.cinagi.co.za/inbox/",
            ),
        ],
    },
    {
        "name": "event_rsvp_confirmed",
        "category": "UTILITY",
        "send_from": dt.date(2026, 8, 25),
        "send_until": EVENT_DATE,
        "components": [
            image_header(),
            body(
                "You are confirmed, {{1}}. You are guest number {{2}}.\n\n"
                "{{3}}, registration from 08h30, Bryanston.\n\n"
                "Show the code above at the door. It is also your entry into the lucky draw.",
                "Thabo",
                "84",
                "Wednesday 30 September",
            ),
            footer(),
            buttons("Add to calendar", "Send venue pin", "Cinagi Consultant"),
        ],
    },
    {
        # Sent automatically when an RSVP arrives from the website form. The
        # guest has not messaged us, so no window is open and only a template
        # can reach them. Text-only on purpose: the QR ticket comes later, and
        # a media header would make the auto-send depend on minting one.
        # The copy reads like a booking receipt, not an invitation - Meta's
        # category checker rejected a first version naming the launch as
        # INCORRECT_CATEGORY, because promoting the event is MARKETING.
        # _v2 exists because Meta allows one edit per 24 hours on an active
        # template and the day's edit was spent; a fresh name reviews as a new
        # template. v1 (time baked as 08h30) can be deleted in WhatsApp Manager.
        "name": "event_rsvp_received_v2",
        "category": "UTILITY",
        "send_from": dt.date(2026, 8, 25),
        "send_until": EVENT_DATE,
        "components": [
            body(
                "Thanks {{1}}, your RSVP is confirmed. You are guest number {{2}}.\n\n"
                "Date: {{3}}\n"
                "Registration: from {{4}}\n"
                "Venue: Bryanston\n\n"
                "Your entry code will be sent to you closer to the day. Reply here "
                "if anything about your booking needs to change.",
                "Thabo",
                "84",
                "Wednesday 30 September",
                "08h00",
            ),
            footer(),
            buttons("Add to calendar", "Send venue pin", "Cinagi Consultant"),
        ],
    },
    {
        "name": "event_logistics",
        "category": "UTILITY",
        "send_from": EVENT_DATE - dt.timedelta(days=1),
        "send_until": EVENT_DATE - dt.timedelta(days=1),
        "components": [
            body(
                "Hi {{1}}, we are on for tomorrow.\n\n"
                "Doors and breakfast from 08h00, keynote at 09h15. Parking is free - "
                "please follow the Cinagi banners.\n\n"
                "Still joining us?",
                "Thabo",
            ),
            footer(),
            buttons("I am still in", "Plans changed", "Cinagi Consultant"),
        ],
    },
    {
        "name": "event_morning",
        "category": "UTILITY",
        "send_from": EVENT_DATE,
        "send_until": EVENT_DATE,
        "components": [
            image_header(),
            body(
                "Morning {{1}}, today is the day.\n\n"
                "Doors are open from 08h00 and here is your code again. See you shortly.",
                "Thabo",
            ),
            footer(),
            buttons("Send venue pin", "Running late", "Cinagi Consultant"),
        ],
    },
    {
        "name": "event_feedback",
        "category": "UTILITY",
        "send_from": EVENT_DATE,
        "send_until": EVENT_DATE + dt.timedelta(days=2),
        "components": [
            body(
                "Hi {{1}}, thank you for joining us today.\n\n"
                "Two quick questions and then I will send you everything from the day. "
                "How was it?",
                "Thabo",
            ),
            footer(),
            buttons("Excellent", "Good", "Could be better"),
        ],
    },
    {
        "name": "event_recap",
        "category": "UTILITY",
        "send_from": EVENT_DATE,
        "send_until": EVENT_DATE + dt.timedelta(days=14),
        "components": [
            document_header(),
            body(
                "Here is everything from {{2}}, {{1}}. The slides, the demo links "
                "and the photos.\n\n"
                "This chat stays open. Ask me anything about what we launched, any time.",
                "Thabo",
                "Wednesday",
            ),
            footer(),
            buttons("Book a demo", "Talk to my AM", "Cinagi Consultant"),
        ],
    },
    {
        "name": "event_demo_booking",
        "category": "MARKETING",
        "send_from": EVENT_DATE + dt.timedelta(days=1),
        "send_until": EVENT_DATE + dt.timedelta(days=30),
        "components": [
            body(
                "Hi {{1}}, you asked good questions about the Co-pilot last week.\n\n"
                "Would you like 30 minutes with the team to see it against your own book? "
                "Pick a slot below.",
                "Thabo",
            ),
            footer(),
            buttons("See available times", "Not right now", "Cinagi Consultant"),
        ],
    },
]

BY_NAME = {definition["name"]: definition for definition in TEMPLATES}


def sendable_on(name, on=None):
    """Should this template go out today? Returns (allowed, reason).

    Templates not defined here (anything outside the event campaign) are always
    allowed - this guard exists for the event, not for the support line.
    """
    definition = BY_NAME.get(name)
    if definition is None:
        return True, ""
    on = on or dt.date.today()
    starts, ends = definition.get("send_from"), definition.get("send_until")
    if starts and on < starts:
        return False, (
            f"'{name}' is not due to go out until {starts.day} {starts:%B %Y}. "
            "Sending it early would spoil the sequence."
        )
    if ends and on > ends:
        return False, (
            f"'{name}' was for the event on {EVENT_DATE.day} {EVENT_DATE:%B %Y} and "
            f"stopped being sendable after {ends.day} {ends:%B %Y}."
        )
    return True, ""
