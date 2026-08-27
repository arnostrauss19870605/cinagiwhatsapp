"""Email the invitation, with each guest's personal WhatsApp link.

    python manage.py send_invites --event 1 --dry-run
    python manage.py send_invites --event 1 --limit 5 --only arno@cinagi.co.za
    python manage.py send_invites --event 1

Sent through Microsoft Graph from the configured mailbox. The email exists to
get the guest to message us first: their inbound message opens a free 24 hour
window, and the RSVP, the ticket and every question afterwards happen inside
it. Sending 600 template messages instead would cost real money and need
marketing opt-in.

Safe by default: --dry-run prints what would be sent, --limit caps a run, and
nobody is emailed twice unless you pass --resend.
"""

import time

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.events.invites import render
from apps.events.models import Event, Guest, JourneyEvent


class Command(BaseCommand):
    help = "Send the invitation email with each guest's WhatsApp deep link."

    def add_arguments(self, parser):
        parser.add_argument("--event", type=int, required=True)
        parser.add_argument("--limit", type=int, help="Send at most this many.")
        parser.add_argument("--only", action="append", help="Only these email addresses.")
        parser.add_argument("--resend", action="store_true", help="Include guests already emailed.")
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument(
            "--pause", type=float, default=0.4, help="Seconds between sends (default 0.4)."
        )

    def handle(self, *args, **options):
        from apps.channels_wa.models import WhatsAppChannel
        from apps.events import calendar as ics
        from apps.integrations.msgraph import GraphClient, GraphError

        event = Event.objects.filter(pk=options["event"]).first()
        if event is None:
            raise CommandError(f"No event with id {options['event']}.")

        channel = (
            WhatsAppChannel.objects.for_workspace(event.workspace)
            .filter(is_active=True)
            .order_by("-is_default", "pk")
            .first()
        )
        if channel is None:
            raise CommandError("No active WhatsApp number on this workspace.")
        if not channel.phone_number:
            raise CommandError(
                "The channel has no display phone number, so the wa.me link cannot be built."
            )

        client = GraphClient()
        if not client.configured:
            raise CommandError(
                "Microsoft Graph is not configured. Set MS_GRAPH_TENANT_ID, "
                "MS_GRAPH_CLIENT_ID, MS_GRAPH_CLIENT_SECRET and MS_GRAPH_SENDER."
            )

        guests = Guest.objects.filter(event=event).exclude(email="")
        if not options["resend"]:
            guests = guests.filter(invite_email_sent_at__isnull=True)
        if options["only"]:
            guests = guests.filter(email__in=options["only"])
        guests = guests.order_by("pk")
        if options["limit"]:
            guests = guests[: options["limit"]]

        total = guests.count()
        if not total:
            self.stdout.write("Nobody to email. Everyone has had it, or the list is empty.")
            return

        self.stdout.write(f"\n{total} guest(s), from {client.sender}, linking to {channel.phone_number}\n")

        # Checked on every run, dry or not. A dry run that skips the credential
        # check proves the guest list and nothing about the thing most likely to
        # break on the day.
        try:
            mailbox = client.whoami()
            self.stdout.write(
                self.style.SUCCESS(f"Graph ok - sending as {mailbox.get('displayName')}\n")
            )
        except GraphError as exc:
            if not options["dry_run"]:
                raise CommandError(f"Microsoft Graph check failed: {exc.friendly}")
            self.stdout.write(self.style.ERROR(f"Graph NOT ready: {exc.friendly}"))
            self.stdout.write(
                self.style.WARNING("Run 'python manage.py check_graph' for the detail.\n")
            )

        sent = failed = 0
        for guest in guests:
            link = guest.whatsapp_link(channel.phone_number)
            subject, html = render(guest, event, link, channel.phone_number)

            if options["dry_run"]:
                self.stdout.write(f"  - {guest.email:38s} {guest.full_name:24s} {link}")
                continue

            try:
                client.send_mail(
                    guest.email,
                    subject,
                    html,
                    attachments=[(
                        ics.filename_for(event),
                        "text/calendar; method=PUBLISH",
                        ics.build(event, guest),
                    )],
                )
            except GraphError as exc:
                self.stdout.write(self.style.ERROR(f"  x {guest.email}: {exc.friendly}"))
                failed += 1
                continue

            guest.invite_email_sent_at = timezone.now()
            guest.save(update_fields=["invite_email_sent_at"])
            JourneyEvent.objects.create(
                workspace=guest.workspace, guest=guest, step="invite_emailed", detail=guest.email
            )
            sent += 1
            self.stdout.write(f"  + {guest.email}")
            time.sleep(options["pause"])

        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("\nDry run - no email was sent."))
        else:
            self.stdout.write(self.style.SUCCESS(f"\nSent {sent}, failed {failed}."))
            self.stdout.write(
                "Guests now message us first, which opens their free 24 hour window. "
                "Watch the inbox."
            )
