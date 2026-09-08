"""Fill an audience from an event's guest list, for bulk template sends.

    python manage.py audience_from_event --event 1 --audience "Launch attendees"
    python manage.py audience_from_event --event 1 --audience "Declined" --rsvp declined

Guests who have never messaged us have no contact yet, so one is created from
their mobile number - the same linking the confirmation task does. Idempotent:
re-running adds new members and never duplicates, and guests who changed their
RSVP out of the chosen status are removed so the audience tracks the roster.
"""

from django.core.management.base import BaseCommand, CommandError

from apps.contacts.models import Audience, Contact
from apps.events.models import Event, Guest


class Command(BaseCommand):
    help = "Create or refresh an audience from an event's guest list."

    def add_arguments(self, parser):
        parser.add_argument("--event", type=int, required=True)
        parser.add_argument("--audience", required=True, help="Audience name; created if new.")
        parser.add_argument(
            "--rsvp",
            default=Guest.Rsvp.ATTENDING,
            choices=[choice for choice, _ in Guest.Rsvp.choices],
            help="Which guests to include (default: attending).",
        )

    def handle(self, *args, **options):
        event = Event.objects.filter(pk=options["event"]).first()
        if event is None:
            raise CommandError(f"No event with id {options['event']}.")

        audience, created = Audience.objects.get_or_create(
            workspace=event.workspace,
            name=options["audience"],
            defaults={"description": f"{event.name}: {options['rsvp']}"},
        )

        guests = event.guests.filter(rsvp_status=options["rsvp"])
        members = []
        skipped = 0
        for guest in guests.select_related("contact"):
            contact = guest.contact
            if contact is None:
                if not guest.msisdn:
                    skipped += 1
                    continue
                contact, _ = Contact.objects.get_or_create(
                    workspace=event.workspace,
                    wa_id=guest.msisdn,
                    defaults={"display_name": guest.full_name},
                )
                guest.contact = contact
                guest.save(update_fields=["contact"])
            members.append(contact)

        audience.contacts.set(members)
        verb = "Created" if created else "Refreshed"
        self.stdout.write(self.style.SUCCESS(
            f"{verb} '{audience.name}' with {len(members)} contact(s) "
            f"({options['rsvp']} guests of '{event.name}')."
        ))
        if skipped:
            self.stdout.write(self.style.WARNING(
                f"{skipped} guest(s) have no mobile number and were left out."
            ))
