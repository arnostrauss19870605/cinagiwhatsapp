"""Create or update the launch event with its known facts.

    python manage.py seed_event
    python manage.py seed_event --workspace 2

Idempotent: matched by name within the workspace, so running it again after a
detail changes simply updates the row. The venue coordinates are the pin the
"Send venue pin" button drops, so they are version-controlled here rather than
living only in whichever database someone last typed them into.
"""

import datetime as dt
from zoneinfo import ZoneInfo

from django.core.management.base import BaseCommand, CommandError

from apps.events.models import Event
from apps.library.event_templates import EVENT_DATE
from apps.workspaces.models import Workspace

EVENT_NAME = "Annual Product Update and Launch"
VENUE = "63 Bryanston Dr, Bryanston, Sandton, 2191"
VENUE_LAT = -26.061599748687517
VENUE_LNG = 28.011626297142122

# Registration from 08h30, keynote at 09h15 - the times the approved template
# copy promises, so they are fixed here to match.
DOORS = dt.time(8, 30)
KEYNOTE = dt.time(9, 15)


class Command(BaseCommand):
    help = "Create or update the launch event with venue, times and coordinates."

    def add_arguments(self, parser):
        parser.add_argument("--workspace", type=int, help="Workspace id (default: the only one).")

    def handle(self, *args, **options):
        if options["workspace"]:
            workspace = Workspace.objects.filter(pk=options["workspace"]).first()
            if workspace is None:
                raise CommandError(f"No workspace with id {options['workspace']}.")
        else:
            workspaces = list(Workspace.objects.all()[:2])
            if not workspaces:
                raise CommandError("No workspace exists yet. Create one first.")
            if len(workspaces) > 1:
                raise CommandError("More than one workspace - say which with --workspace.")
            workspace = workspaces[0]

        tz = ZoneInfo("Africa/Johannesburg")
        event, created = Event.objects.update_or_create(
            workspace=workspace,
            name=EVENT_NAME,
            defaults={
                "starts_at": dt.datetime.combine(EVENT_DATE, KEYNOTE, tzinfo=tz),
                "doors_open_at": dt.datetime.combine(EVENT_DATE, DOORS, tzinfo=tz),
                "venue": VENUE,
                "venue_lat": VENUE_LAT,
                "venue_lng": VENUE_LNG,
            },
        )
        verb = "Created" if created else "Updated"
        self.stdout.write(
            self.style.SUCCESS(
                f"{verb} '{event.name}' (id {event.pk}) in workspace '{workspace.name}': "
                f"{event.date_label()}, {event.venue}."
            )
        )
        if not event.registration_secret:
            self.stdout.write(
                self.style.WARNING(
                    "No registration secret is set, so the website form endpoint is off. "
                    "Set one in the admin to accept form RSVPs."
                )
            )
