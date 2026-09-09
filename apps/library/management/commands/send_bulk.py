"""Send an approved template to every contact in one or more audiences.

    python manage.py send_bulk --audience "Launch attendees" --template event_agenda \
        --value "Wednesday 30 September" --media docs/agenda.pdf --dry-run

    python manage.py send_bulk --audience "Launch attendees" --audience "VIP brokers" \
        --template event_logistics --limit 50

{{1}} is always the contact's first name; --value fills {{2}} onwards, in order.
A contact in several of the named audiences is sent to once. Opt-outs, the
campaign send window and the comms-guard test mode are all enforced downstream
in the transport, so a blocked guest shows up in the counts rather than being
messaged. Safe by default: --dry-run lists recipients, --limit caps a run, and
--pause spaces the sends so a batch never looks like a burst.
"""

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

MEDIA_MIME = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".mp4": "video/mp4",
}


class Command(BaseCommand):
    help = "Send an approved template to every contact in the named audience(s)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--audience", action="append", required=True,
            help="Audience name (or id). Repeat for several; overlap is de-duplicated.",
        )
        parser.add_argument("--template", required=True, help="Approved template name.")
        parser.add_argument(
            "--value", action="append", default=[],
            help="Body variable {{2}} onwards, in order. {{1}} is the first name.",
        )
        parser.add_argument(
            "--media",
            help="File path or https URL for the template's image/document/video header.",
        )
        parser.add_argument("--limit", type=int, help="Send to at most this many contacts.")
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument(
            "--pause", type=float, default=1.0, help="Seconds between sends (default 1)."
        )

    def handle(self, *args, **options):
        from apps.contacts.models import Audience
        from apps.core.audit import audit
        from apps.library.bulk import audience_contacts, resolve_channel, send_to_contacts
        from apps.library.event_templates import sendable_on
        from apps.library.models import MessageTemplate

        audiences = []
        for label in options["audience"]:
            found = (
                Audience.objects.filter(pk=label).first()
                if str(label).isdigit()
                else None
            ) or Audience.objects.filter(name__iexact=label).first()
            if found is None:
                raise CommandError(f"No audience called '{label}'.")
            audiences.append(found)

        workspaces = {a.workspace_id for a in audiences}
        if len(workspaces) > 1:
            raise CommandError("Those audiences belong to different workspaces.")
        workspace = audiences[0].workspace

        channel = resolve_channel(workspace)
        if channel is None:
            raise CommandError("No active WhatsApp number on this workspace.")

        template = (
            MessageTemplate.objects.for_workspace(workspace)
            .filter(channel=channel, name=options["template"],
                    status=MessageTemplate.Status.APPROVED)
            .first()
        )
        if template is None:
            raise CommandError(
                f"'{options['template']}' is not an approved template on this number. "
                "Check WhatsApp Manager, then run a template sync."
            )

        allowed, reason = sendable_on(template.name)
        if not allowed:
            raise CommandError(reason)

        expected = template.variable_count
        supplied = 1 + len(options["value"])
        if expected and supplied != expected:
            raise CommandError(
                f"'{template.name}' takes {expected} value(s) - the first name plus "
                f"{expected - 1} --value option(s); you supplied {len(options['value'])}."
            )

        header_media = self._resolve_media(template, channel, options)

        contacts = audience_contacts(workspace, audiences)
        if options["limit"]:
            contacts = contacts[: options["limit"]]
        contacts = list(contacts)
        if not contacts:
            raise CommandError("Those audiences have no sendable contacts.")

        if options["dry_run"]:
            for contact in contacts[:20]:
                self.stdout.write(f"  would send to {contact.name} (+{contact.wa_id})")
            if len(contacts) > 20:
                self.stdout.write(f"  ... and {len(contacts) - 20} more")
            self.stdout.write(self.style.WARNING(
                f"Dry run - '{template.name}' was not sent to {len(contacts)} contact(s)."
            ))
            return

        from apps.library.bulk import start_bulk_send

        batch = start_bulk_send(
            workspace, channel, template, audiences, options["value"],
            header_media=header_media, recipient_count=len(contacts),
        )
        result = send_to_contacts(
            workspace, channel, template, contacts, options["value"],
            header_media=header_media, pause=options["pause"], bulk_send=batch,
        )
        for note in result["notes"]:
            self.stdout.write(f"  - {note}")
        audit(
            "library.bulk_send",
            workspace=workspace,
            target=template,
            audiences=[a.name for a in audiences],
            sent=result["sent"],
            blocked=result["blocked"],
            failed=result["failed"],
        )
        self.stdout.write(self.style.SUCCESS(
            f"Sent {result['sent']}, blocked {result['blocked']}, "
            f"failed {result['failed']} of {len(contacts)}."
        ))

    def _resolve_media(self, template, channel, options):
        """Turn --media into the header parameter, uploading a local file once."""
        header = template.header_format
        needs_media = header in {"IMAGE", "DOCUMENT", "VIDEO"}
        if not needs_media:
            if options["media"]:
                raise CommandError(f"'{template.name}' has no media header; drop --media.")
            return None
        if not options["media"]:
            raise CommandError(
                f"'{template.name}' has a {header.lower()} header, so --media is needed - "
                "a file path or an https URL."
            )
        source = options["media"]
        if source.startswith("http"):
            media = {"link": source}
        else:
            path = Path(source)
            if not path.exists():
                raise CommandError(f"No such file: {source}")
            mime = MEDIA_MIME.get(path.suffix.lower())
            if mime is None:
                raise CommandError(f"Unsupported file type: {path.suffix}")
            if options["dry_run"]:
                media = {"id": "<uploaded on the real run>"}
            else:
                with path.open("rb") as handle:
                    media_id = channel.client().upload_media(handle, mime)
                if not media_id:
                    raise CommandError("WhatsApp did not accept the media upload.")
                media = {"id": media_id}
        if header == "DOCUMENT":
            media["filename"] = Path(source).name
        return media
