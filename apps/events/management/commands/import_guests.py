"""Load the guest list from a CSV.

    python manage.py import_guests --event 1 --file guests.csv --dry-run
    python manage.py import_guests --event 1 --file guests.csv

Expected columns (header row, any order, case-insensitive):
    first_name, last_name, email, mobile, company, segment

Numbers are normalised to international form, duplicates are matched on mobile
so re-running the import updates rather than duplicates, and rows that cannot
be used are reported rather than silently dropped.
"""

import csv

from django.core.management.base import BaseCommand, CommandError

from apps.events.models import Event, Guest, normalise_msisdn


class Command(BaseCommand):
    help = "Import a guest list from CSV."

    def add_arguments(self, parser):
        parser.add_argument("--event", type=int, required=True)
        parser.add_argument("--file", required=True)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        event = Event.objects.filter(pk=options["event"]).first()
        if event is None:
            raise CommandError(f"No event with id {options['event']}.")

        created = updated = skipped = 0
        problems = []

        with open(options["file"], newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            fields = {name.lower().strip(): name for name in (reader.fieldnames or [])}
            if "first_name" not in fields:
                raise CommandError(
                    f"No first_name column. Found: {', '.join(reader.fieldnames or []) or 'nothing'}"
                )

            for row_number, row in enumerate(reader, start=2):
                def value(key):
                    return (row.get(fields.get(key, ""), "") or "").strip()

                first = value("first_name")
                msisdn = normalise_msisdn(value("mobile") or value("msisdn") or value("phone"))
                email = value("email")
                if not first or not (msisdn or email):
                    problems.append(f"row {row_number}: needs a first name and a mobile or email")
                    skipped += 1
                    continue

                guest = Guest.objects.filter(event=event, msisdn=msisdn).first() if msisdn else None
                guest = guest or (
                    Guest.objects.filter(event=event, email__iexact=email).first() if email else None
                )

                data = {
                    "first_name": first,
                    "last_name": value("last_name"),
                    "company": value("company"),
                    "email": email,
                    "msisdn": msisdn,
                    "segment": (value("segment") or Guest.Segment.BROKER).lower(),
                }
                if guest:
                    updated += 1
                    if not options["dry_run"]:
                        for field, val in data.items():
                            setattr(guest, field, val)
                        guest.save()
                else:
                    created += 1
                    if not options["dry_run"]:
                        Guest.objects.create(workspace=event.workspace, event=event, **data)

        for problem in problems[:20]:
            self.stdout.write(self.style.WARNING(f"  ! {problem}"))
        if len(problems) > 20:
            self.stdout.write(self.style.WARNING(f"  ! and {len(problems) - 20} more"))

        verb = "Would create" if options["dry_run"] else "Created"
        self.stdout.write(
            self.style.SUCCESS(f"{verb} {created}, updated {updated}, skipped {skipped}.")
        )
        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("Dry run - nothing was written."))
