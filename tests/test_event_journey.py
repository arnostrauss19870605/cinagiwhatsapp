"""The guest's side of the event journey.

Every reply in this flow is a plain message inside the window the guest opened
by messaging us, so these tests also protect the cost model: if a change here
starts requiring templates, the event gets materially more expensive.
"""

import datetime as dt
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.channels_wa.messaging.base import SendResult
from apps.channels_wa.models import WhatsAppChannel
from apps.contacts.models import Contact
from apps.events.models import Event, Guest
from apps.inbox.models import Conversation, Message
from apps.workspaces.models import Workspace

SENT = SendResult(ok=True, wamid="wamid.TEST")


@override_settings(OUTBOUND_COMMS_MODE="live")
class EventJourneyTests(TestCase):
    def setUp(self):
        self.workspace = Workspace.objects.create(name="Cinagi Broker Support")
        self.channel = WhatsAppChannel.objects.create(
            workspace=self.workspace,
            display_name="Broker Support",
            phone_number="+27842358896",
            phone_number_id="123",
        )
        self.event = Event.objects.create(
            workspace=self.workspace,
            name="Annual Product Update and Launch",
            starts_at=timezone.now() + dt.timedelta(days=30),
            venue="Bryanston",
            capacity=2,
        )
        self.guest = Guest.objects.create(
            workspace=self.workspace,
            event=self.event,
            first_name="Thabo",
            last_name="Mokoena",
            email="thabo@example.com",
            msisdn="27726124698",
        )
        self.contact = Contact.objects.create(workspace=self.workspace, wa_id="27726124698")
        self.conversation = Conversation.objects.create(
            workspace=self.workspace,
            channel=self.channel,
            contact=self.contact,
            last_inbound_at=timezone.now(),
            window_expires_at=timezone.now() + dt.timedelta(hours=24),
        )

    def _inbound(self, body, kind=Message.Kind.TEXT):
        return Message.objects.create(
            workspace=self.workspace,
            conversation=self.conversation,
            direction=Message.Direction.IN,
            kind=kind,
            body=body,
        )

    def _handle(self, body):
        from apps.events.journey import handle

        with mock.patch(
            "apps.channels_wa.messaging.meta_cloud.MetaCloudChannel.send_text", return_value=SENT
        ) as send:
            claimed = handle(self.conversation, self._inbound(body))
        return claimed, send

    # -- entry --------------------------------------------------------------

    def test_the_deep_link_token_links_the_guest_to_the_contact(self):
        claimed, send = self._handle(f"RSVP-{self.guest.invite_token}")
        self.assertTrue(claimed)
        self.guest.refresh_from_db()
        self.assertEqual(self.guest.contact, self.contact)
        self.assertEqual(self.guest.stage, Guest.Stage.ENGAGED)
        self.assertIn("Can you make it?", send.call_args[0][1])

    def test_an_unknown_token_is_left_for_a_human(self):
        claimed, send = self._handle("RSVP-notarealtoken")
        self.assertFalse(claimed)
        send.assert_not_called()

    def test_the_contact_name_is_filled_in_from_the_guest_list(self):
        self._handle(f"RSVP-{self.guest.invite_token}")
        self.contact.refresh_from_db()
        self.assertEqual(self.contact.display_name, "Thabo Mokoena")

    # -- answering ----------------------------------------------------------

    def test_choosing_one_confirms_attendance_and_assigns_a_guest_number(self):
        self._handle(f"RSVP-{self.guest.invite_token}")
        claimed, send = self._handle("1")
        self.assertTrue(claimed)
        self.guest.refresh_from_db()
        self.assertEqual(self.guest.rsvp_status, Guest.Rsvp.ATTENDING)
        self.assertEqual(self.guest.guest_number, 1)
        self.assertIn("guest number 1", send.call_args[0][1])

    def test_natural_language_yes_is_understood(self):
        self._handle(f"RSVP-{self.guest.invite_token}")
        self._handle("Yes I will be there")
        self.guest.refresh_from_db()
        self.assertEqual(self.guest.rsvp_status, Guest.Rsvp.ATTENDING)

    def test_declining_releases_the_seat_and_offers_the_recap(self):
        self._handle(f"RSVP-{self.guest.invite_token}")
        claimed, send = self._handle("3")
        self.guest.refresh_from_db()
        self.assertEqual(self.guest.rsvp_status, Guest.Rsvp.DECLINED)
        self.assertIn("recap", send.call_args[0][1].lower())

    def test_a_plus_one_is_recorded(self):
        self._handle(f"RSVP-{self.guest.invite_token}")
        self._handle("1")
        self._handle("+1")
        self.guest.refresh_from_db()
        self.assertEqual(self.guest.party_size, 2)

    def test_a_full_event_puts_the_guest_on_the_waitlist(self):
        for index in range(2):
            Guest.objects.create(
                workspace=self.workspace,
                event=self.event,
                first_name=f"Filler{index}",
                msisdn=f"2772000000{index}",
                rsvp_status=Guest.Rsvp.ATTENDING,
                guest_number=index + 1,
            )
        self._handle(f"RSVP-{self.guest.invite_token}")
        claimed, send = self._handle("1")
        self.guest.refresh_from_db()
        self.assertEqual(self.guest.rsvp_status, Guest.Rsvp.WAITLIST)
        self.assertIn("first in line", send.call_args[0][1])

    def test_a_question_is_left_for_a_person(self):
        self._handle(f"RSVP-{self.guest.invite_token}")
        claimed, send = self._handle("Is there parking for a bakkie and a trailer?")
        self.assertFalse(claimed)

    # -- the venue pin ------------------------------------------------------

    def _tap_venue_pin(self):
        from apps.events.journey import handle

        message = self._inbound("Send venue pin", kind=Message.Kind.INTERACTIVE)
        with mock.patch(
            "apps.channels_wa.messaging.meta_cloud.MetaCloudChannel.send_location",
            return_value=SENT,
        ) as location, mock.patch(
            "apps.channels_wa.messaging.meta_cloud.MetaCloudChannel.send_text", return_value=SENT
        ) as text:
            claimed = handle(self.conversation, message)
        return claimed, location, text

    def test_the_pin_is_a_location_message_when_coordinates_are_trusted(self):
        self.event.venue = "63 Bryanston Dr, Bryanston, Sandton, 2191"
        self.event.venue_lat = -26.05
        self.event.venue_lng = 28.03
        self.event.save()
        self._handle(f"RSVP-{self.guest.invite_token}")

        claimed, location, text = self._tap_venue_pin()
        self.assertTrue(claimed)
        location.assert_called_once()
        args, kwargs = location.call_args
        self.assertEqual(args[1:], (-26.05, 28.03))
        self.assertEqual(kwargs["address"], "63 Bryanston Dr, Bryanston, Sandton, 2191")
        recorded = Message.objects.filter(
            direction=Message.Direction.OUT, kind=Message.Kind.LOCATION
        )
        self.assertTrue(recorded.exists())

    def test_without_coordinates_the_pin_is_a_maps_link_for_the_address(self):
        self.event.venue = "63 Bryanston Dr, Bryanston, Sandton, 2191"
        self.event.save()
        self._handle(f"RSVP-{self.guest.invite_token}")

        claimed, location, text = self._tap_venue_pin()
        self.assertTrue(claimed)
        location.assert_not_called()
        body = text.call_args[0][1]
        self.assertIn("https://www.google.com/maps/search/?api=1&query=", body)
        self.assertIn("63+Bryanston+Dr", body)

    # -- day of -------------------------------------------------------------

    def test_the_venue_qr_checks_the_guest_in(self):
        self._handle(f"RSVP-{self.guest.invite_token}")
        self._handle("1")
        claimed, send = self._handle(f"CHECKIN-{self.guest.ticket_token}")
        self.assertTrue(claimed)
        self.guest.refresh_from_db()
        self.assertEqual(self.guest.stage, Guest.Stage.CHECKED_IN)
        self.assertIsNotNone(self.guest.checked_in_at)
        self.assertIn("checked in", send.call_args[0][1].lower())

    def test_checking_in_twice_is_harmless(self):
        self._handle(f"RSVP-{self.guest.invite_token}")
        self._handle("1")
        self._handle(f"CHECKIN-{self.guest.ticket_token}")
        first = Guest.objects.get(pk=self.guest.pk).checked_in_at
        claimed, send = self._handle(f"CHECKIN-{self.guest.ticket_token}")
        self.assertEqual(Guest.objects.get(pk=self.guest.pk).checked_in_at, first)
        self.assertIn("already checked in", send.call_args[0][1].lower())

    # -- the cost model -----------------------------------------------------

    def test_every_journey_reply_is_a_session_message_not_a_template(self):
        self._handle(f"RSVP-{self.guest.invite_token}")
        self._handle("1")
        outbound = Message.objects.filter(direction=Message.Direction.OUT)
        self.assertTrue(outbound.exists())
        self.assertFalse(
            outbound.filter(kind=Message.Kind.TEMPLATE).exists(),
            "the journey must answer inside the guest's free window",
        )


class SeedEventTests(TestCase):
    """The seed command carries the venue pin, so it must be safe to re-run."""

    def test_it_creates_then_updates_without_duplicating(self):
        from django.core.management import call_command

        from apps.events.models import Event

        Workspace.objects.create(name="Cinagi Broker Support")
        call_command("seed_event")
        call_command("seed_event")

        events = Event.objects.all()
        self.assertEqual(events.count(), 1)
        event = events.get()
        self.assertEqual(event.venue, "63 Bryanston Dr, Bryanston, Sandton, 2191")
        self.assertAlmostEqual(event.venue_lat, -26.0616, places=3)
        self.assertAlmostEqual(event.venue_lng, 28.0116, places=3)
        self.assertIsNotNone(event.doors_open_at)
        self.assertLess(event.doors_open_at, event.starts_at)


class WhatsAppLinkTests(TestCase):
    def test_the_link_carries_the_token_and_no_punctuation(self):
        workspace = Workspace.objects.create(name="Alpha")
        event = Event.objects.create(
            workspace=workspace, name="Launch", starts_at=timezone.now() + dt.timedelta(days=5)
        )
        guest = Guest.objects.create(workspace=workspace, event=event, first_name="Thabo")
        link = guest.whatsapp_link("+27 84 235 8896")
        self.assertTrue(link.startswith("https://wa.me/27842358896?text=RSVP-"))
        self.assertIn(guest.invite_token, link)


@override_settings(OUTBOUND_COMMS_MODE="live")
class StopTests(TestCase):
    """STOP is a legal instruction, not a menu option."""

    def setUp(self):
        self.workspace = Workspace.objects.create(name="Cinagi Broker Support")
        self.channel = WhatsAppChannel.objects.create(
            workspace=self.workspace,
            display_name="Broker Support",
            phone_number="+27842358896",
            phone_number_id="123",
        )
        self.event = Event.objects.create(
            workspace=self.workspace,
            name="Annual Product Update and Launch",
            starts_at=timezone.now() + dt.timedelta(days=30),
            venue="Bryanston",
        )
        self.contact = Contact.objects.create(
            workspace=self.workspace, wa_id="27726124698", display_name="Thabo Mokoena"
        )
        self.guest = Guest.objects.create(
            workspace=self.workspace,
            event=self.event,
            first_name="Thabo",
            last_name="Mokoena",
            msisdn="27726124698",
            contact=self.contact,
        )
        self.conversation = Conversation.objects.create(
            workspace=self.workspace,
            channel=self.channel,
            contact=self.contact,
            last_inbound_at=timezone.now(),
            window_expires_at=timezone.now() + dt.timedelta(hours=24),
        )

    def test_stop_matching_is_narrower_than_the_human_match(self):
        from apps.channels_wa.inbound import _wants_to_stop

        for text in ["STOP", "stop.", "unsubscribe", "please remove me off your list",
                     "stop sending me messages", "do not message me again"]:
            self.assertTrue(_wants_to_stop(text), text)
        for text in ["", "stop the keynote is at 9?", "can you stop by our office",
                     "the demo stopped working"]:
            self.assertFalse(_wants_to_stop(text), text)

    def test_stop_records_the_opt_out_confirms_it_and_ends_the_journey(self):
        from apps.channels_wa.inbound import _opt_out

        with mock.patch(
            "apps.channels_wa.messaging.meta_cloud.MetaCloudChannel.send_text", return_value=SENT
        ) as send:
            _opt_out(self.conversation, None)

        self.contact.refresh_from_db()
        self.guest.refresh_from_db()
        self.assertTrue(self.contact.is_opted_out)
        self.assertEqual(self.guest.stage, Guest.Stage.STOPPED)

        body = send.call_args[0][1]
        self.assertIn("off the list", body)
        self.assertIn("START", body)
        self.assertIn("AGENT", body)

    def test_a_template_is_never_sent_to_someone_who_stopped(self):
        from apps.channels_wa.outbound import send_template
        from apps.library.models import MessageTemplate

        self.contact.opted_out_at = timezone.now()
        self.contact.save(update_fields=["opted_out_at"])

        template = MessageTemplate.objects.create(
            workspace=self.workspace,
            channel=self.channel,
            name="event_rsvp_reminder",
            language="en",
            status=MessageTemplate.Status.APPROVED,
            components=[{"type": "BODY", "text": "Hi {{1}}"}],
        )
        with mock.patch(
            "apps.channels_wa.messaging.meta_cloud.MetaCloudChannel.send_template"
        ) as send:
            message = send_template(self.conversation, template, ["Thabo"])

        send.assert_not_called()
        self.assertEqual(message.wa_status, Message.Status.BLOCKED)
        self.assertIn("opted out", message.wa_error["reason"])

    def test_start_puts_them_back_on(self):
        from apps.channels_wa.inbound import _opt_in

        self.contact.opted_out_at = timezone.now()
        self.contact.save(update_fields=["opted_out_at"])

        with mock.patch(
            "apps.channels_wa.messaging.meta_cloud.MetaCloudChannel.send_text", return_value=SENT
        ) as send:
            _opt_in(self.conversation, None)

        self.contact.refresh_from_db()
        self.assertFalse(self.contact.is_opted_out)
        self.assertIn("Welcome back", send.call_args[0][1])


@override_settings(OUTBOUND_COMMS_MODE="live", PUBLIC_BASE_URL="https://brokers.cinagi.co.za")
class CalendarTests(TestCase):
    """The .ics is the one artefact that leaves our system and is parsed by
    someone else's software, so it is worth testing the format itself."""

    def setUp(self):
        self.workspace = Workspace.objects.create(name="Cinagi Broker Support")
        self.channel = WhatsAppChannel.objects.create(
            workspace=self.workspace,
            display_name="Broker Support",
            phone_number="+27842358896",
            phone_number_id="123",
        )
        self.event = Event.objects.create(
            workspace=self.workspace,
            name="Annual Product Update; Launch, 2026",   # deliberately nasty
            starts_at=timezone.now() + dt.timedelta(days=30),
            doors_open_at=timezone.now() + dt.timedelta(days=30) - dt.timedelta(minutes=45),
            venue="Bryanston, Gauteng",
        )
        self.contact = Contact.objects.create(
            workspace=self.workspace, wa_id="27726124698", display_name="Thabo Mokoena"
        )
        self.guest = Guest.objects.create(
            workspace=self.workspace,
            event=self.event,
            first_name="Thabo",
            last_name="Mokoena",
            msisdn="27726124698",
            contact=self.contact,
            guest_number=84,
        )
        self.conversation = Conversation.objects.create(
            workspace=self.workspace,
            channel=self.channel,
            contact=self.contact,
            last_inbound_at=timezone.now(),
            window_expires_at=timezone.now() + dt.timedelta(hours=24),
        )

    def _ics(self):
        from apps.events import calendar as ics

        return ics.build(self.event, self.guest).decode("utf-8")

    def test_the_file_is_well_formed_and_crlf_terminated(self):
        body = self._ics()
        self.assertTrue(body.startswith("BEGIN:VCALENDAR\r\n"))
        self.assertTrue(body.endswith("END:VCALENDAR\r\n"))
        self.assertNotIn("\n\n", body)
        for line in body.split("\r\n"):
            self.assertLessEqual(len(line.encode("utf-8")), 75, line)

    def test_semicolons_and_commas_in_the_name_are_escaped(self):
        body = self._ics()
        summary = [l for l in body.split("\r\n") if l.startswith("SUMMARY")][0]
        self.assertIn("\;", summary)
        self.assertIn("\\,", summary)

    def test_it_publishes_rather_than_requesting_an_rsvp(self):
        # A REQUEST would give the guest accept/decline buttons and send a reply
        # email per click - a second RSVP channel competing with WhatsApp.
        body = self._ics()
        self.assertIn("METHOD:PUBLISH", body)
        self.assertNotIn("METHOD:REQUEST", body)
        self.assertNotIn("ATTENDEE", body)

    def test_it_starts_at_doors_not_at_the_keynote(self):
        from apps.events import calendar as ics

        starts, ends = ics.starts_and_ends(self.event)
        self.assertEqual(starts, self.event.doors_open_at)
        self.assertGreater(ends, self.event.starts_at)

    def test_the_uid_is_stable_per_guest_so_a_resend_updates_the_entry(self):
        from apps.events import calendar as ics

        self.assertEqual(ics.uid_for(self.event, self.guest), ics.uid_for(self.event, self.guest))
        other = Guest.objects.create(
            workspace=self.workspace, event=self.event, first_name="Ann", last_name="B"
        )
        self.assertNotEqual(ics.uid_for(self.event, self.guest), ics.uid_for(self.event, other))

    def test_the_public_url_serves_it_with_the_calendar_content_type(self):
        response = self.client.get(f"/e/{self.guest.invite_token}.ics")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response["Content-Type"].startswith("text/calendar"))
        self.assertIn("BEGIN:VEVENT", response.content.decode())

    def test_an_unknown_token_is_a_404_not_a_stack_trace(self):
        self.assertEqual(self.client.get("/e/not-a-real-token.ics").status_code, 404)

    def test_the_button_replies_with_a_link_because_whatsapp_refuses_ics_files(self):
        from apps.events.journey import handle

        message = Message.objects.create(
            workspace=self.workspace,
            conversation=self.conversation,
            direction=Message.Direction.IN,
            kind=Message.Kind.INTERACTIVE,
            body="Add to calendar",
        )
        with mock.patch(
            "apps.channels_wa.messaging.meta_cloud.MetaCloudChannel.send_text", return_value=SENT
        ) as send:
            claimed = handle(self.conversation, message)

        self.assertTrue(claimed)
        body = send.call_args[0][1]
        self.assertIn(f"https://brokers.cinagi.co.za/e/{self.guest.invite_token}.ics", body)


@override_settings(OUTBOUND_COMMS_MODE="live")
class InviteTokenTests(TestCase):
    """Regression: a token ending in a hyphen used to be silently truncated.

    It surfaced as an intermittent test failure roughly one run in sixty, which
    is exactly how it would have surfaced in production - about ten brokers in a
    six hundred person list tapping their link and getting nothing back.
    """

    def setUp(self):
        self.workspace = Workspace.objects.create(name="Cinagi Broker Support")
        self.channel = WhatsAppChannel.objects.create(
            workspace=self.workspace,
            display_name="Broker Support",
            phone_number="+27842358896",
            phone_number_id="123",
        )
        self.event = Event.objects.create(
            workspace=self.workspace,
            name="Annual Product Update and Launch",
            starts_at=timezone.now() + dt.timedelta(days=30),
            venue="Bryanston",
        )
        self.contact = Contact.objects.create(workspace=self.workspace, wa_id="27726124698")
        self.conversation = Conversation.objects.create(
            workspace=self.workspace,
            channel=self.channel,
            contact=self.contact,
            last_inbound_at=timezone.now(),
            window_expires_at=timezone.now() + dt.timedelta(hours=24),
        )

    def _claims(self, token):
        from apps.events.journey import handle

        guest = Guest.objects.create(
            workspace=self.workspace,
            event=self.event,
            first_name="Thabo",
            last_name="Mokoena",
            invite_token=token,
        )
        message = Message.objects.create(
            workspace=self.workspace,
            conversation=self.conversation,
            direction=Message.Direction.IN,
            kind=Message.Kind.TEXT,
            body=f"RSVP-{token}",
        )
        with mock.patch(
            "apps.channels_wa.messaging.meta_cloud.MetaCloudChannel.send_text", return_value=SENT
        ):
            claimed = handle(self.conversation, message)
        guest.delete()
        return claimed

    def test_every_shape_of_token_is_matched(self):
        for token in ["Ab3xY7Qk9x", "Ab3xY7Qk9-", "-b3xY7Qk9x", "Ab3xY7Qk9_", "_b3xY7Qk9x",
                      "A-3x_Y7Qk9", "aaaaaa"]:
            self.assertTrue(self._claims(token), f"token {token!r} was not matched")

    def test_generated_tokens_never_start_or_end_with_punctuation(self):
        from apps.events.models import new_token

        for _ in range(500):
            token = new_token()
            self.assertTrue(token[0].isalnum() and token[-1].isalnum(), token)

    def test_a_token_inside_a_sentence_is_still_found(self):
        self.assertTrue(self._claims("Ab3xY7Qk9x"))
