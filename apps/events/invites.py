"""The invitation email - the front door to the whole journey."""

from django.utils.html import escape

SUBJECT = "You are invited: {event_name}"

BODY = """\
<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
            max-width:560px;margin:0 auto;color:#0f172a;line-height:1.55">
  <div style="background:#0f766e;padding:28px 32px;border-radius:16px 16px 0 0">
    <p style="margin:0;color:#ffffff;font-size:13px;letter-spacing:.08em;
              text-transform:uppercase">Cinagi</p>
    <h1 style="margin:6px 0 0;color:#ffffff;font-size:24px">{event_name}</h1>
    <p style="margin:8px 0 0;color:#d1fae5;font-size:15px">{date_label} &middot; {venue}</p>
  </div>

  <div style="border:1px solid #e2e8f0;border-top:0;border-radius:0 0 16px 16px;padding:32px">
    <p style="margin:0 0 16px">Hi {first_name},</p>

    <p style="margin:0 0 16px">
      We are hosting our annual product update and launch, and we would like you there.
      Three announcements, live demos, and breakfast from 08h30.
    </p>

    <p style="margin:0 0 24px">
      RSVP takes about a minute and happens on WhatsApp, so you can ask us anything
      between now and the day.
    </p>

    <p style="margin:0 0 28px">
      <a href="{whatsapp_link}"
         style="background:#25D366;color:#ffffff;text-decoration:none;padding:14px 26px;
                border-radius:10px;font-weight:600;display:inline-block">
        RSVP on WhatsApp
      </a>
    </p>

    <p style="margin:0 0 8px;font-size:14px;color:#475569">
      That button opens a chat with us on {display_number}. If it does not work on this
      device, save the number and send us the word <strong>RSVP</strong>.
    </p>

    <p style="margin:24px 0 0;font-size:14px;color:#475569">
      See you there,<br>The Cinagi team
    </p>

    <p style="margin:28px 0 0;padding-top:16px;border-top:1px solid #e2e8f0;
              font-size:12px;color:#94a3b8">
      Cinagi (Pty) Ltd is an authorised financial services provider (FSP 50104).
      You are receiving this because you are a registered Cinagi broker. Reply to this
      email if you would rather not hear about our events.
    </p>
  </div>
</div>
"""


def render(guest, event, whatsapp_link, display_number):
    context = {
        "event_name": escape(event.name),
        "date_label": escape(event.date_label()),
        "venue": escape(event.venue or "Bryanston"),
        "first_name": escape(guest.first_name),
        "whatsapp_link": escape(whatsapp_link),
        "display_number": escape(display_number),
    }
    return SUBJECT.format(**context), BODY.format(**context)
