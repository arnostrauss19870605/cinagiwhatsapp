"""Audiences and the bulk template sender.

A bulk send is the highest-blast-radius thing this platform does - one typo
reaches six hundred brokers on the number the support line depends on - so
these tests lean on the guard rails: de-duplication, opt-outs, media headers,
and dry runs that touch nothing.
"""

import datetime as dt
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.channels_wa.messaging.base import SendResult
from apps.channels_wa.models import WhatsAppChannel
from apps.contacts.models import Audience, Contact
from apps.inbox.models import Conversation, Message
from apps.library.models import MessageTemplate
from apps.workspaces.models import Workspace

SENT = SendResult(ok=True, wamid="wamid.BULK")


class HeaderComponentTests(TestCase):
    """A media-header template refuses to send without its header parameter,
    so build_components must produce one."""

    def setUp(self):
        self.workspace = Workspace.objects.create(name="Alpha")
        self.channel = WhatsAppChannel.objects.create(
            workspace=self.workspace, display_name="A", phone_number="+27000000000",
            phone_number_id="1",
        )

    def _template(self, components):
        return MessageTemplate.objects.create(
            workspace=self.workspace, channel=self.channel, name="t", language="en",
            status=MessageTemplate.Status.APPROVED, components=components,
        )

    def test_a_document_header_is_built_with_id_and_filename(self):
        template = self._template([
            {"type": "HEADER", "format": "DOCUMENT"},
            {"type": "BODY", "text": "Hi {{1}}"},
        ])
        built = template.build_components(
            ["Thabo"], header_media={"id": "MEDIA9", "filename": "agenda.pdf"}
        )
        self.assertEqual(built[0], {
            "type": "header",
            "parameters": [{"type": "document",
                            "document": {"id": "MEDIA9", "filename": "agenda.pdf"}}],
        })
        self.assertEqual(built[1]["type"], "body")

    def test_an_image_header_takes_a_link_and_no_filename(self):
        template = self._template([
            {"type": "HEADER", "format": "IMAGE"},
            {"type": "BODY", "text": "Hi {{1}}"},
        ])
        built = template.build_components(
            ["Thabo"], header_media={"link": "https://x/agenda.png", "filename": "ignored"}
        )
        self.assertEqual(
            built[0]["parameters"][0]["image"], {"link": "https://x/agenda.png"}
        )

    def test_a_plain_template_still_builds_only_the_body(self):
        template = self._template([{"type": "BODY", "text": "Hi {{1}}"}])
        built = template.build_components(["Thabo"])
        self.assertEqual(len(built), 1)
        self.assertEqual(built[0]["type"], "body")

    def test_header_format_reads_the_synced_components(self):
        template = self._template([
            {"type": "HEADER", "format": "DOCUMENT"}, {"type": "BODY", "text": "x"},
        ])
        self.assertEqual(template.header_format, "DOCUMENT")


@override_settings(OUTBOUND_COMMS_MODE="live")
class BulkSendTests(TestCase):
    def setUp(self):
        self.workspace = Workspace.objects.create(name="Cinagi Broker Support")
        self.channel = WhatsAppChannel.objects.create(
            workspace=self.workspace,
            display_name="Broker Support",
            phone_number="+27842358896",
            phone_number_id="123",
            is_active=True,
        )
        self.template = MessageTemplate.objects.create(
            workspace=self.workspace,
            channel=self.channel,
            name="event_agenda",
            language="en",
            status=MessageTemplate.Status.APPROVED,
            components=[
                {"type": "HEADER", "format": "DOCUMENT"},
                {"type": "BODY", "text": "Hi {{1}}, agenda for {{2}}."},
            ],
        )
        self.thabo = Contact.objects.create(
            workspace=self.workspace, wa_id="27726124698", display_name="Thabo Mokoena"
        )
        self.ann = Contact.objects.create(
            workspace=self.workspace, wa_id="27831234567", display_name="Ann Botha"
        )
        self.attendees = Audience.objects.create(workspace=self.workspace, name="Launch attendees")
        self.vips = Audience.objects.create(workspace=self.workspace, name="VIP brokers")
        self.attendees.contacts.set([self.thabo, self.ann])
        self.vips.contacts.set([self.thabo])

    def _send(self, *args):
        out = StringIO()
        with mock.patch(
            "apps.channels_wa.messaging.meta_cloud.MetaCloudChannel.send_template",
            return_value=SENT,
        ) as send:
            call_command(
                "send_bulk", "--pause", "0",
                "--template", "event_agenda",
                "--media", "https://brokers.cinagi.co.za/media/agenda.pdf",
                "--value", "Wednesday 30 September",
                *args, stdout=out,
            )
        return out.getvalue(), send

    def test_overlapping_audiences_send_once_per_contact(self):
        output, send = self._send("--audience", "Launch attendees", "--audience", "VIP brokers")
        self.assertEqual(send.call_count, 2)
        self.assertIn("Sent 2", output)
        self.assertEqual(
            Message.objects.filter(kind=Message.Kind.TEMPLATE).count(), 2
        )

    def test_the_first_variable_is_the_contacts_first_name(self):
        self._send("--audience", "VIP brokers")
        message = Message.objects.get(kind=Message.Kind.TEMPLATE)
        self.assertEqual(message.payload["values"], ["Thabo", "Wednesday 30 September"])

    def test_the_document_header_travels_with_every_send(self):
        output, send = self._send("--audience", "VIP brokers")
        components = send.call_args[0][3]
        header = components[0]
        self.assertEqual(header["type"], "header")
        self.assertEqual(
            header["parameters"][0]["document"]["link"],
            "https://brokers.cinagi.co.za/media/agenda.pdf",
        )
        self.assertEqual(header["parameters"][0]["document"]["filename"], "agenda.pdf")

    def test_an_opted_out_contact_is_counted_not_messaged(self):
        self.thabo.opted_out_at = timezone.now()
        self.thabo.save(update_fields=["opted_out_at"])
        output, send = self._send("--audience", "Launch attendees")
        self.assertEqual(send.call_count, 1)
        self.assertIn("Sent 1, blocked 1", output)

    def test_a_dry_run_sends_nothing_and_lists_recipients(self):
        output, send = self._send("--audience", "Launch attendees", "--dry-run")
        send.assert_not_called()
        self.assertIn("Thabo Mokoena", output)
        self.assertIn("Dry run", output)
        self.assertFalse(Message.objects.exists())

    def test_a_media_header_template_refuses_to_send_without_media(self):
        with self.assertRaises(CommandError) as caught:
            call_command(
                "send_bulk", "--audience", "Launch attendees",
                "--template", "event_agenda", "--value", "x", "--pause", "0",
            )
        self.assertIn("--media", str(caught.exception))

    def test_wrong_value_count_is_a_plain_error(self):
        with self.assertRaises(CommandError) as caught:
            call_command(
                "send_bulk", "--audience", "Launch attendees",
                "--template", "event_agenda", "--pause", "0",
                "--media", "https://x/a.pdf",
            )
        self.assertIn("takes 2 value", str(caught.exception))

    def test_an_unapproved_template_is_refused_upfront(self):
        self.template.status = MessageTemplate.Status.PENDING
        self.template.save(update_fields=["status"])
        with self.assertRaises(CommandError) as caught:
            self._send("--audience", "Launch attendees")
        self.assertIn("not an approved template", str(caught.exception))

    def test_a_contact_with_an_open_chat_reuses_it(self):
        existing = Conversation.objects.create(
            workspace=self.workspace, channel=self.channel, contact=self.thabo,
            status=Conversation.Status.ASSIGNED,
        )
        self._send("--audience", "VIP brokers")
        self.assertEqual(Conversation.objects.count(), 1)
        self.assertEqual(
            Message.objects.get(kind=Message.Kind.TEMPLATE).conversation, existing
        )


class AudienceFromEventTests(TestCase):
    def setUp(self):
        from apps.events.models import Event, Guest

        self.workspace = Workspace.objects.create(name="Cinagi Broker Support")
        self.event = Event.objects.create(
            workspace=self.workspace,
            name="Annual Product Update and Launch",
            starts_at=timezone.now() + dt.timedelta(days=22),
        )
        self.attending = Guest.objects.create(
            workspace=self.workspace, event=self.event, first_name="Thabo",
            last_name="Mokoena", msisdn="27726124698", rsvp_status=Guest.Rsvp.ATTENDING,
        )
        self.declined = Guest.objects.create(
            workspace=self.workspace, event=self.event, first_name="Ann",
            msisdn="27831234567", rsvp_status=Guest.Rsvp.DECLINED,
        )

    def _run(self):
        out = StringIO()
        call_command(
            "audience_from_event", "--event", str(self.event.pk),
            "--audience", "Launch attendees", stdout=out,
        )
        return out.getvalue()

    def test_attending_guests_become_contacts_in_the_audience(self):
        output = self._run()
        audience = Audience.objects.get(name="Launch attendees")
        self.assertEqual(audience.contacts.count(), 1)
        contact = audience.contacts.get()
        self.assertEqual(contact.wa_id, "27726124698")
        self.assertEqual(contact.display_name, "Thabo Mokoena")
        self.attending.refresh_from_db()
        self.assertEqual(self.attending.contact, contact)

    def test_rerunning_tracks_the_roster_without_duplicating(self):
        from apps.events.models import Guest

        self._run()
        self.attending.rsvp_status = Guest.Rsvp.DECLINED
        self.attending.save(update_fields=["rsvp_status"])
        self.declined.rsvp_status = Guest.Rsvp.ATTENDING
        self.declined.save(update_fields=["rsvp_status"])
        self._run()
        audience = Audience.objects.get(name="Launch attendees")
        self.assertEqual(
            list(audience.contacts.values_list("wa_id", flat=True)), ["27831234567"]
        )
