"""Send an approved template to a number, from the command line.

    python manage.py send_template --to 0726124698 --template event_invite --var Arno
    python manage.py send_template --to 0726124698 --template event_invite --var Arno --dry-run

A template is the only way to reach someone who has not messaged you in the
last 24 hours, so this is also the cleanest way to prove the outbound path
works. The message is stored like any other, so its delivery status appears in
the app and updates when WhatsApp reports back. Credentials come from the
connected channel - nothing secret is typed on the command line.
"""

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Send an approved WhatsApp template to a number."

    def add_arguments(self, parser):
        parser.add_argument("--to", required=True, help="Recipient, local or international form.")
        parser.add_argument("--template", required=True, help="Approved template name.")
        parser.add_argument(
            "--var", action="append", default=[], help="A body variable, in order. Repeatable."
        )
        parser.add_argument("--language", default="en")
        parser.add_argument("--channel", type=int, help="WhatsAppChannel id (defaults to the first).")
        parser.add_argument("--dry-run", action="store_true", help="Show what would be sent.")

    def handle(self, *args, **options):
        from apps.channels_wa.models import WhatsAppChannel
        from apps.contacts.models import Contact
        from apps.inbox.models import Conversation, Message
        from apps.library.models import MessageTemplate

        channel = (
            WhatsAppChannel.objects.filter(pk=options["channel"]).first()
            if options["channel"]
            else WhatsAppChannel.objects.filter(is_active=True).order_by("pk").first()
        )
        if channel is None:
            raise CommandError("No active WhatsApp channel. Connect a number in the app first.")

        template = MessageTemplate.objects.filter(
            channel=channel, name=options["template"], language=options["language"]
        ).first()
        if template is None:
            known = ", ".join(
                MessageTemplate.objects.filter(channel=channel).values_list("name", flat=True)
            ) or "none loaded - press Load templates in the app"
            raise CommandError(
                f"No template '{options['template']}' in {options['language']}. Available: {known}"
            )
        if not template.is_usable:
            raise CommandError(f"Template is {template.status}, not APPROVED, so it cannot be sent.")

        expected = template.variable_count
        given = len(options["var"])
        if expected != given:
            raise CommandError(
                f"'{template.name}' needs {expected} variable(s) and {given} were given. "
                f"Body: {template.body_text}"
            )

        number = "".join(ch for ch in options["to"] if ch.isdigit())
        if number.startswith("0"):
            # A local South African number written as 072... means 2772...
            number = "27" + number[1:]
            self.stdout.write(f"Read {options['to']} as international {number}.")

        self.stdout.write(f"\nTo:       +{number}")
        self.stdout.write(f"Template: {template.name} ({template.language}, {template.category})")
        self.stdout.write(f"Message:  {template.preview(options['var'])}\n")

        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("Dry run - nothing was sent."))
            return

        contact, _ = Contact.objects.get_or_create(
            workspace=channel.workspace, wa_id=number, defaults={"display_name": ""}
        )
        conversation = (
            Conversation.objects.filter(
                workspace=channel.workspace, contact=contact, status__in=Conversation.OPEN_STATUSES
            ).first()
            or Conversation.objects.create(
                workspace=channel.workspace,
                channel=channel,
                contact=contact,
                status=Conversation.Status.BOT,
            )
        )

        from apps.channels_wa.outbound import send_template as do_send

        message = do_send(conversation, template, options["var"], actor=Message.Actor.BOT)

        if message.wa_status == Message.Status.BLOCKED:
            self.stdout.write(self.style.WARNING(message.wa_error.get("reason", "Blocked.")))
        elif message.wa_status == Message.Status.FAILED:
            self.stdout.write(self.style.ERROR(f"Rejected: {message.failure_explanation}"))
        else:
            self.stdout.write(self.style.SUCCESS(f"WhatsApp accepted it (wamid {message.wamid})."))
            self.stdout.write(
                "That means queued, not delivered. Watch the chat in the app - the ticks "
                "update when WhatsApp reports back."
            )
