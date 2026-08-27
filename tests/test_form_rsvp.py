"""The website registration form posting RSVPs to us.

Gravity Forms on the public site sends each submission to our webhook. The
per-event secret is the only credential, field keys arrive however the site
admin named them, and people press submit twice - so the tests cover exactly
those three realities.
"""

import datetime as dt
import json
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.channels_wa.messaging.base import SendResult
from apps.channels_wa.models import WhatsAppChannel
from apps.events.models import Event, Guest, JourneyEvent
from apps.inbox.models import Conversation, Message
from apps.library.models import MessageTemplate
from apps.workspaces.models import Workspace


class FormRsvpTests(TestCase):
    def setUp(self):
        self.workspace = Workspace.objects.create(name="Cinagi Broker Support")
        self.channel = WhatsAppChannel.objects.create(
            workspace=self.workspace,
            display_name="Broker Support",
            phone_number="+27842358896",
            phone_number_id="123",
            is_active=True,
        )
        self.event = Event.objects.create(
            workspace=self.workspace,
            name="Annual Product Update and Launch",
            starts_at=timezone.now() + dt.timedelta(days=30),
            venue="Bryanston",
            registration_secret="form-secret-123",
        )
        self.url = f"/e/register/{self.event.pk}/"

    def _post(self, payload, secret="form-secret-123", **kwargs):
        headers = {"HTTP_X_REGISTRATION_SECRET": secret} if secret is not None else {}
        return self.client.post(
            self.url, data=json.dumps(payload), content_type="application/json",
            **headers, **kwargs,
        )

    # -- the secret is the credential ----------------------------------------

    def test_no_secret_is_a_404(self):
        self.assertEqual(self._post({"first_name": "Thabo"}, secret=None).status_code, 404)

    def test_a_wrong_secret_is_a_404(self):
        self.assertEqual(self._post({"first_name": "Thabo"}, secret="wrong").status_code, 404)

    def test_a_blank_stored_secret_means_the_endpoint_is_off(self):
        self.event.registration_secret = ""
        self.event.save(update_fields=["registration_secret"])
        self.assertEqual(self._post({"first_name": "Thabo"}, secret="").status_code, 404)

    def test_the_secret_may_arrive_as_a_query_parameter(self):
        response = self.client.post(
            f"{self.url}?secret=form-secret-123",
            data=json.dumps({"first_name": "Thabo", "mobile": "0726124698"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)

    # -- a submission becomes a roster row -----------------------------------

    def test_a_submission_creates_an_attending_guest_with_a_number(self):
        response = self._post({
            "First Name": "Thabo",
            "Last Name": "Mokoena",
            "Email": "thabo@example.com",
            "Mobile Number": "072 612 4698",
            "Company": "Mokoena Brokers",
            "Are you attending?": "Yes, I will be there",
            "Dietary requirements": "Halaal",
        })
        self.assertEqual(response.status_code, 200)
        body = response.json()

        guest = Guest.objects.get(event=self.event, msisdn="27726124698")
        self.assertEqual(guest.first_name, "Thabo")
        self.assertEqual(guest.company, "Mokoena Brokers")
        self.assertEqual(guest.dietary, "Halaal")
        self.assertEqual(guest.rsvp_status, Guest.Rsvp.ATTENDING)
        self.assertEqual(guest.stage, Guest.Stage.CONFIRMED)
        self.assertEqual(guest.guest_number, 1)
        self.assertIsNotNone(guest.rsvp_at)

        self.assertTrue(body["ok"])
        self.assertTrue(body["created"])
        self.assertEqual(body["guest_number"], 1)
        self.assertIn(guest.invite_token, body["whatsapp_link"])
        self.assertTrue(body["whatsapp_link"].startswith("https://wa.me/27842358896"))

    def test_a_form_encoded_post_works_too(self):
        response = self.client.post(
            self.url,
            data={"first_name": "Thabo", "mobile": "0726124698"},
            HTTP_X_REGISTRATION_SECRET="form-secret-123",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(Guest.objects.filter(event=self.event, msisdn="27726124698").exists())

    def test_a_decline_is_recorded_without_a_guest_number(self):
        self._post({
            "first_name": "Thabo", "mobile": "0726124698",
            "rsvp": "No, I cannot make it this time",
        })
        guest = Guest.objects.get(event=self.event, msisdn="27726124698")
        self.assertEqual(guest.rsvp_status, Guest.Rsvp.DECLINED)
        self.assertIsNone(guest.guest_number)

    def test_outside_gauteng_maps_to_the_launch_pack(self):
        self._post({
            "first_name": "Thabo", "mobile": "0726124698",
            "attendance": "I am outside Gauteng - send me the launch pack",
        })
        guest = Guest.objects.get(event=self.event, msisdn="27726124698")
        self.assertEqual(guest.rsvp_status, Guest.Rsvp.PACK)

    def test_party_size_is_read_from_the_form(self):
        self._post({
            "first_name": "Thabo", "mobile": "0726124698",
            "party_size": "+2",
        })
        guest = Guest.objects.get(event=self.event, msisdn="27726124698")
        self.assertEqual(guest.party_size, 3)

    def test_the_journey_records_where_the_rsvp_came_from(self):
        self._post({"first_name": "Thabo", "mobile": "0726124698"})
        guest = Guest.objects.get(event=self.event, msisdn="27726124698")
        steps = list(guest.journey.values_list("step", flat=True))
        self.assertIn("form_registered", steps)
        self.assertIn(Guest.Stage.CONFIRMED, steps)

    # -- submitting twice never duplicates -----------------------------------

    def test_a_repeat_submission_updates_the_same_guest(self):
        self._post({"first_name": "Thabo", "mobile": "0726124698"})
        self._post({"first_name": "Thabo", "mobile": "0726124698", "company": "Mokoena Brokers"})
        guests = Guest.objects.filter(event=self.event, msisdn="27726124698")
        self.assertEqual(guests.count(), 1)
        self.assertEqual(guests.get().company, "Mokoena Brokers")
        self.assertEqual(guests.get().guest_number, 1)

    def test_an_imported_guest_is_matched_not_duplicated(self):
        existing = Guest.objects.create(
            workspace=self.workspace, event=self.event,
            first_name="Thabo", last_name="Mokoena", msisdn="27726124698",
        )
        token = existing.invite_token
        response = self._post({
            "first_name": "Thabo", "mobile": "0726124698", "email": "thabo@example.com",
        })
        self.assertEqual(Guest.objects.filter(event=self.event).count(), 1)
        existing.refresh_from_db()
        self.assertEqual(existing.email, "thabo@example.com")
        self.assertEqual(existing.invite_token, token)
        self.assertIn(token, response.json()["whatsapp_link"])

    def test_matching_falls_back_to_email_when_there_is_no_mobile(self):
        Guest.objects.create(
            workspace=self.workspace, event=self.event,
            first_name="Thabo", email="thabo@example.com",
        )
        self._post({"first_name": "Thabo", "email": "Thabo@Example.com"})
        self.assertEqual(Guest.objects.filter(event=self.event).count(), 1)

    # -- event rules still apply ---------------------------------------------

    def test_a_full_event_puts_the_guest_on_the_waitlist(self):
        self.event.capacity = 1
        self.event.save(update_fields=["capacity"])
        Guest.objects.create(
            workspace=self.workspace, event=self.event, first_name="First",
            msisdn="27720000001", rsvp_status=Guest.Rsvp.ATTENDING, guest_number=1,
        )
        self._post({"first_name": "Thabo", "mobile": "0726124698"})
        guest = Guest.objects.get(event=self.event, msisdn="27726124698")
        self.assertEqual(guest.rsvp_status, Guest.Rsvp.WAITLIST)
        self.assertIsNone(guest.guest_number)

    def test_closed_rsvps_are_saved_as_details_only(self):
        self.event.rsvp_closes_at = timezone.now() - dt.timedelta(days=1)
        self.event.save(update_fields=["rsvp_closes_at"])
        response = self._post({"first_name": "Thabo", "mobile": "0726124698"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["ok"])
        guest = Guest.objects.get(event=self.event, msisdn="27726124698")
        self.assertEqual(guest.rsvp_status, Guest.Rsvp.NONE)

    def test_a_submission_without_contact_details_is_rejected_plainly(self):
        response = self._post({"first_name": "Thabo"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("mobile number or email", response.json()["error"])

    def test_a_full_name_field_is_split(self):
        self._post({"name": "Thabo Mokoena", "mobile": "0726124698"})
        guest = Guest.objects.get(event=self.event, msisdn="27726124698")
        self.assertEqual(guest.first_name, "Thabo")
        self.assertEqual(guest.last_name, "Mokoena")


@override_settings(OUTBOUND_COMMS_MODE="live")
class RsvpConfirmationTests(TestCase):
    """The WhatsApp confirmation that follows a website-form RSVP.

    The guest has never messaged us, so no free window exists and the send
    must go out as the approved `event_rsvp_received` template. Approval is
    Meta's to give, so the roster must survive the template not existing yet.
    """

    def setUp(self):
        self.workspace = Workspace.objects.create(name="Cinagi Broker Support")
        self.channel = WhatsAppChannel.objects.create(
            workspace=self.workspace,
            display_name="Broker Support",
            phone_number="+27842358896",
            phone_number_id="123",
            is_active=True,
        )
        self.event = Event.objects.create(
            workspace=self.workspace,
            name="Annual Product Update and Launch",
            starts_at=timezone.now() + dt.timedelta(days=30),
            venue="Bryanston",
            registration_secret="form-secret-123",
        )
        self.template = MessageTemplate.objects.create(
            workspace=self.workspace,
            channel=self.channel,
            name="event_rsvp_received",
            language="en",
            status=MessageTemplate.Status.APPROVED,
            components=[
                {"type": "BODY", "text": "Thanks {{1}}, you are guest number {{2}}. {{3}}."}
            ],
        )
        self.url = f"/e/register/{self.event.pk}/"

    def _post(self, payload):
        with mock.patch(
            "apps.channels_wa.messaging.meta_cloud.MetaCloudChannel.send_template",
            return_value=SendResult(ok=True, wamid="wamid.CONFIRM"),
        ) as send:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(
                    self.url, data=json.dumps(payload), content_type="application/json",
                    HTTP_X_REGISTRATION_SECRET="form-secret-123",
                )
        return response, send

    def test_an_attending_rsvp_triggers_the_confirmation_template(self):
        response, send = self._post({
            "first_name": "Thabo", "last_name": "Mokoena", "mobile": "0726124698",
        })
        self.assertEqual(response.status_code, 200)
        send.assert_called_once()

        guest = Guest.objects.get(event=self.event, msisdn="27726124698")
        self.assertIsNotNone(guest.contact)
        self.assertEqual(guest.contact.wa_id, "27726124698")
        self.assertEqual(guest.contact.display_name, "Thabo Mokoena")

        message = Message.objects.get(kind=Message.Kind.TEMPLATE)
        self.assertEqual(message.wa_status, Message.Status.SENT)
        self.assertEqual(message.actor, Message.Actor.BOT)
        self.assertEqual(message.conversation.contact, guest.contact)
        self.assertEqual(
            message.payload["values"],
            ["Thabo", "1", self.event.date_label()],
        )
        self.assertIn("rsvp_confirmation_sent", guest.journey.values_list("step", flat=True))

    def test_an_unapproved_template_skips_the_send_but_keeps_the_rsvp(self):
        self.template.status = MessageTemplate.Status.PENDING
        self.template.save(update_fields=["status"])
        response, send = self._post({"first_name": "Thabo", "mobile": "0726124698"})
        self.assertEqual(response.status_code, 200)
        send.assert_not_called()

        guest = Guest.objects.get(event=self.event, msisdn="27726124698")
        self.assertEqual(guest.rsvp_status, Guest.Rsvp.ATTENDING)
        skipped = guest.journey.filter(step="confirmation_skipped").first()
        self.assertIsNotNone(skipped)
        self.assertIn("not approved", skipped.detail)

    def test_a_decline_sends_nothing(self):
        response, send = self._post({
            "first_name": "Thabo", "mobile": "0726124698", "rsvp": "No, I cannot make it",
        })
        send.assert_not_called()

    def test_a_repeat_submission_reuses_the_open_conversation(self):
        self._post({"first_name": "Thabo", "mobile": "0726124698"})
        self._post({"first_name": "Thabo", "mobile": "0726124698"})
        self.assertEqual(Conversation.objects.count(), 1)

    def test_the_definition_in_the_pack_matches_what_the_task_sends(self):
        from apps.library.event_templates import BY_NAME

        definition = BY_NAME["event_rsvp_received"]
        self.assertEqual(definition["category"], "UTILITY")
        body = next(c for c in definition["components"] if c["type"] == "BODY")
        # Three variables: first name, guest number, date label - what the task passes.
        self.assertIn("{{3}}", body["text"])
        self.assertNotIn("{{4}}", body["text"])
        self.assertNotIn(
            "HEADER", [c["type"] for c in definition["components"]],
            "a media header would make the auto-send depend on minting a ticket image",
        )
