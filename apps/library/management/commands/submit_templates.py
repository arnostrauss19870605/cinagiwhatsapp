"""Submit the version-controlled template pack to Meta for review.

    python manage.py submit_templates --dry-run          # show what would be sent
    python manage.py submit_templates                    # submit everything missing
    python manage.py submit_templates --only event_teaser

Credentials come from the connected channel in the database, or from the
environment (WHATSAPP_WABA_ID, WHATSAPP_ACCESS_TOKEN, WHATSAPP_APP_ID) so this
can run before a number has been connected in the app. Nothing is printed that
would reveal a token.
"""

import copy
import json
import os

from django.core.management.base import BaseCommand, CommandError

from apps.library.event_templates import LANGUAGE, TEMPLATES

MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".pdf": "application/pdf"}


class _EnvChannel:
    """Stands in for a WhatsAppChannel when credentials come from the environment."""

    def __init__(self):
        self.waba_id = os.environ.get("WHATSAPP_WABA_ID", "")
        self.access_token = os.environ.get("WHATSAPP_ACCESS_TOKEN", "")
        self.graph_version = os.environ.get("WHATSAPP_GRAPH_VERSION", "")
        self.phone_number_id = os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "")
        self.pk = "env"


class Command(BaseCommand):
    help = "Submit the event message templates to Meta for review."

    def add_arguments(self, parser):
        parser.add_argument("--channel", type=int, help="WhatsAppChannel id to use.")
        parser.add_argument("--only", action="append", help="Submit only these template names.")
        parser.add_argument("--dry-run", action="store_true", help="Show the payloads, send nothing.")
        parser.add_argument(
            "--skip-media",
            action="store_true",
            help="Skip templates that need a sample image or document.",
        )

    def handle(self, *args, **options):
        from apps.channels_wa.messaging import get_channel_client
        from apps.channels_wa.models import WhatsAppChannel

        channel = None
        if options["channel"]:
            channel = WhatsAppChannel.objects.filter(pk=options["channel"]).first()
            if channel is None:
                raise CommandError(f"No channel with id {options['channel']}.")
        else:
            channel = WhatsAppChannel.objects.filter(is_active=True, waba_id__gt="").first()
        if channel is None or not channel.access_token:
            channel = _EnvChannel()
            if not (channel.waba_id and channel.access_token):
                raise CommandError(
                    "No connected channel found and WHATSAPP_WABA_ID / WHATSAPP_ACCESS_TOKEN "
                    "are not set. Connect a number in the app, or set those two environment "
                    "variables, and try again."
                )

        app_id = os.environ.get("WHATSAPP_APP_ID", "")
        client = get_channel_client(channel)

        wanted = set(options["only"] or [])
        existing = set()
        if not options["dry_run"]:
            try:
                existing = {t.get("name") for t in client.fetch_templates()}
            except Exception as exc:  # the account may simply have none yet
                self.stdout.write(self.style.WARNING(f"Could not list existing templates: {exc}"))

        submitted = skipped = failed = 0
        for definition in TEMPLATES:
            name = definition["name"]
            if wanted and name not in wanted:
                continue
            if name in existing:
                self.stdout.write(f"  = {name}: already exists, skipping")
                skipped += 1
                continue

            components = copy.deepcopy(definition["components"])
            needs_media = any("_sample" in c for c in components)
            if needs_media and options["skip_media"]:
                self.stdout.write(f"  - {name}: needs a sample file, skipped")
                skipped += 1
                continue

            try:
                components = self._resolve_media(client, components, app_id, options["dry_run"])
            except Exception as exc:
                self.stdout.write(self.style.ERROR(f"  x {name}: sample upload failed - {exc}"))
                failed += 1
                continue

            payload = {
                "name": name,
                "language": LANGUAGE,
                "category": definition["category"],
                "components": components,
            }

            if options["dry_run"]:
                self.stdout.write(self.style.HTTP_INFO(f"\n--- {name} ({definition['category']}) ---"))
                self.stdout.write(json.dumps(payload, indent=2))
                continue

            try:
                result = client.create_template(payload)
            except Exception as exc:
                self.stdout.write(self.style.ERROR(f"  x {name}: {getattr(exc, 'friendly', exc)}"))
                failed += 1
                continue
            self.stdout.write(
                self.style.SUCCESS(f"  + {name}: submitted ({result.get('status', 'PENDING')})")
            )
            submitted += 1

        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("\nDry run - nothing was sent to Meta."))
        else:
            self.stdout.write(
                f"\nSubmitted {submitted}, skipped {skipped}, failed {failed}. "
                "Approval usually takes minutes to a few hours; check WhatsApp Manager."
            )

    def _resolve_media(self, client, components, app_id, dry_run):
        """Swap a local sample file for the handle Meta wants on a media header."""
        from django.conf import settings

        resolved = []
        for component in components:
            sample = component.pop("_sample", None)
            if sample:
                if dry_run:
                    component["example"] = {"header_handle": [f"<handle for {sample}>"]}
                else:
                    if not app_id:
                        raise RuntimeError("WHATSAPP_APP_ID is needed to upload a sample file.")
                    path = settings.BASE_DIR / sample
                    if not path.exists():
                        raise RuntimeError(f"Sample file missing: {sample}")
                    mime = MIME.get(path.suffix.lower(), "application/octet-stream")
                    handle = client.upload_sample_media(str(path), app_id, mime)
                    if not handle:
                        raise RuntimeError("Meta did not return an upload handle.")
                    component["example"] = {"header_handle": [handle]}
            resolved.append(component)
        return resolved
