"""Message templates for the 30 September 2026 event, as data.

Kept in version control so the whole set can be submitted, reviewed and
re-submitted for the next event without anyone retyping copy into a web form.
Submit with:  python manage.py submit_templates

Rules encoded here, learned the hard way:
  * language is "en" everywhere - there is no en_ZA, and the code must match
    exactly at send time or the send fails with "template not found";
  * a message about an event the guest already registered for is UTILITY;
    anything promoting the event to someone who has not said yes is MARKETING;
  * samples must look like real values - placeholder-looking samples get the
    template rejected;
  * quick-reply button text stays under 20 characters so it does not truncate
    on a narrow phone.
"""

LANGUAGE = "en"

# Header media samples. Meta only needs a representative file for review; the
# real image or document is supplied per send.
SAMPLE_IMAGE = "docs/samples/header_sample.png"
SAMPLE_DOCUMENT = "docs/samples/recap_sample.pdf"


def body(text, *examples):
    component = {"type": "BODY", "text": text}
    if examples:
        component["example"] = {"body_text": [list(examples)]}
    return component


def buttons(*labels):
    return {
        "type": "BUTTONS",
        "buttons": [{"type": "QUICK_REPLY", "text": label} for label in labels],
    }


def image_header():
    return {"type": "HEADER", "format": "IMAGE", "_sample": SAMPLE_IMAGE}


def document_header():
    return {"type": "HEADER", "format": "DOCUMENT", "_sample": SAMPLE_DOCUMENT}


TEMPLATES = [
    {
        "name": "event_rsvp_reminder",
        "category": "MARKETING",
        "components": [
            body(
                "Hi {{1}}, seats for the Cinagi Product Launch on 30 September are filling up "
                "and I have not heard back from you yet.\n\n"
                "Tap below if you would like one, or let me know if you would rather join the "
                "online stream.",
                "Thabo",
            ),
            buttons("Count me in", "Send stream link", "Not this time"),
        ],
    },
    {
        "name": "event_agenda_reveal",
        "category": "MARKETING",
        "components": [
            image_header(),
            body(
                "Hi {{1}}, the agenda for 30 September is out.\n\n"
                "Based on what you told me, the session on {{2}} is the one to watch. It runs "
                "mid-morning, right after the keynote.\n\n"
                "The full agenda is in the image above. Ask me anything about it.",
                "Thabo",
                "API integrations",
            ),
            buttons("Ask about the agenda"),
        ],
    },
    {
        "name": "event_teaser",
        "category": "MARKETING",
        "components": [
            image_header(),
            body(
                "Hi {{1}}, one week to go.\n\n"
                "We are announcing three things on 30 September. One of them has been the "
                "single most requested item from brokers for two years running.\n\n"
                "Want to guess which?",
                "Thabo",
            ),
            buttons("Guess the announcement", "See the agenda"),
        ],
    },
    {
        "name": "event_rsvp_confirmed",
        "category": "UTILITY",
        "components": [
            image_header(),
            body(
                "You are confirmed, {{1}}. You are guest number {{2}}.\n\n"
                "Wednesday 30 September, registration from 08h30, Bryanston.\n\n"
                "Show the code above at the door. It is also your entry into the lucky draw.",
                "Thabo",
                "84",
            ),
            buttons("Add to calendar", "Send venue pin", "Ask a question"),
        ],
    },
    {
        "name": "event_logistics",
        "category": "UTILITY",
        "components": [
            body(
                "Hi {{1}}, we are on for tomorrow.\n\n"
                "Doors and breakfast from 08h30, keynote at 09h15. Parking is free in the "
                "basement. Take the P2 level and the lifts to reception.\n\n"
                "Still joining us?",
                "Thabo",
            ),
            buttons("I am still in", "Plans changed"),
        ],
    },
    {
        "name": "event_morning",
        "category": "UTILITY",
        "components": [
            image_header(),
            body(
                "Morning {{1}}, today is the day.\n\n"
                "Doors are open from 08h30 and here is your code again. See you shortly.",
                "Thabo",
            ),
            buttons("Send venue pin", "Running late"),
        ],
    },
    {
        "name": "event_feedback",
        "category": "UTILITY",
        "components": [
            body(
                "Hi {{1}}, thank you for joining us today.\n\n"
                "Two quick questions and then I will send you everything from the day. "
                "How was it?",
                "Thabo",
            ),
            buttons("Excellent", "Good", "Could be better"),
        ],
    },
    {
        "name": "event_recap",
        "category": "UTILITY",
        "components": [
            document_header(),
            body(
                "Here is everything from Wednesday, {{1}}. The slides, the demo links and "
                "the photos.\n\n"
                "This chat stays open. Ask me anything about what we launched, any time.",
                "Thabo",
            ),
            buttons("Book a demo", "Talk to my AM"),
        ],
    },
    {
        "name": "event_demo_booking",
        "category": "MARKETING",
        "components": [
            body(
                "Hi {{1}}, you asked good questions about the Co-pilot last week.\n\n"
                "Would you like 30 minutes with the team to see it against your own book? "
                "Pick a slot below.",
                "Thabo",
            ),
            buttons("See available times", "Not right now"),
        ],
    },
]
