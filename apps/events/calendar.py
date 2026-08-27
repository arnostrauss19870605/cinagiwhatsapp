"""The calendar entry, built by hand as an .ics file.

Two deliberate choices worth knowing about.

METHOD is PUBLISH, not REQUEST. A REQUEST turns the entry into a meeting
invitation: the guest gets accept/decline buttons and every click sends a reply
email back to the sender's mailbox. We already take the RSVP on WhatsApp, so
that would give us a second, competing answer and bury Shane in responses.
PUBLISH just puts it in their diary.

Times are written in UTC. South Africa has no daylight saving, but emitting a
VTIMEZONE block correctly is fiddly and getting it wrong moves the event by an
hour in someone's calendar. UTC is unambiguous everywhere and costs nothing.
"""

import datetime as dt

from django.conf import settings
from django.utils import timezone

PRODID = "-//Cinagi//WhatsApp Event Journey//EN"
DEFAULT_DURATION = dt.timedelta(hours=4)


def _escape(value):
    """RFC 5545 TEXT escaping. Order matters: backslash first."""
    return (
        str(value or "")
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def _stamp(moment):
    return moment.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _fold(line):
    """Lines are limited to 75 octets. Fold on bytes, not characters, or a
    multi-byte character can be split down the middle."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    out, chunk = [], raw[:75]
    # do not split a UTF-8 sequence: back off until the remainder decodes
    while True:
        try:
            chunk.decode("utf-8")
            break
        except UnicodeDecodeError:
            chunk = chunk[:-1]
    out.append(chunk.decode("utf-8"))
    rest = raw[len(chunk):]
    while rest:
        piece = rest[:74]
        while True:
            try:
                piece.decode("utf-8")
                break
            except UnicodeDecodeError:
                piece = piece[:-1]
        out.append(" " + piece.decode("utf-8"))
        rest = rest[len(piece):]
    return "\r\n".join(out)


def uid_for(event, guest=None):
    """Stable per guest, so a re-send updates the existing entry rather than
    leaving two of them in the diary."""
    tail = guest.invite_token if guest is not None else f"event-{event.pk}"
    return f"cinagi-{tail}@cinagi.co.za"


def starts_and_ends(event):
    starts = event.doors_open_at or event.starts_at
    ends = getattr(event, "ends_at", None) or (event.starts_at + DEFAULT_DURATION)
    if ends <= starts:
        ends = starts + DEFAULT_DURATION
    return starts, ends


def description_for(event, guest=None):
    lines = []
    if guest is not None and guest.guest_number:
        lines.append(f"You are guest number {guest.guest_number}.")
    if event.doors_open_at and event.doors_open_at != event.starts_at:
        local = timezone.localtime(event.doors_open_at)
        lines.append(f"Doors from {local:%H}h{local:%M}.")
    lines.append("Questions? Message Cinagi Broker Support on WhatsApp and we will help.")
    return "\n".join(lines)


def build(event, guest=None, *, sequence=0, organiser=None):
    """Return the .ics as bytes, ready to attach or serve."""
    starts, ends = starts_and_ends(event)
    organiser = organiser or getattr(settings, "MS_GRAPH_SENDER", "") or ""

    fields = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:{uid_for(event, guest)}",
        f"DTSTAMP:{_stamp(timezone.now())}",
        f"DTSTART:{_stamp(starts)}",
        f"DTEND:{_stamp(ends)}",
        f"SEQUENCE:{sequence}",
        "STATUS:CONFIRMED",
        "TRANSP:OPAQUE",
        f"SUMMARY:{_escape(event.name)}",
        f"DESCRIPTION:{_escape(description_for(event, guest))}",
    ]
    if event.venue:
        fields.append(f"LOCATION:{_escape(event.venue)}")
    if event.venue_lat is not None and event.venue_lng is not None:
        fields.append(f"GEO:{event.venue_lat};{event.venue_lng}")
    if organiser:
        fields.append(f"ORGANIZER;CN=Cinagi:mailto:{organiser}")
    fields += [
        "BEGIN:VALARM",
        "TRIGGER:-PT1H",
        "ACTION:DISPLAY",
        f"DESCRIPTION:{_escape(event.name)} starts in an hour",
        "END:VALARM",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    return ("\r\n".join(_fold(f) for f in fields) + "\r\n").encode("utf-8")


def filename_for(event):
    slug = "".join(c if c.isalnum() else "-" for c in event.name.lower())
    while "--" in slug:
        slug = slug.replace("--", "-")
    return f"{slug.strip('-') or 'event'}.ics"


def link_for(guest):
    """The short link we send on WhatsApp.

    WhatsApp will not carry a text/calendar document - Meta's allowed document
    types do not include it - so the button sends a link to this file on our own
    domain instead. Phones open it straight into the calendar app.
    """
    base = getattr(settings, "PUBLIC_BASE_URL", "").rstrip("/")
    return f"{base}/e/{guest.invite_token}.ics"
