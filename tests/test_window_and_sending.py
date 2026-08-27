"""The 24 hour window, the outbound safety rail and template validation."""

import datetime as dt
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.channels_wa.comms_guard import outbound_blocked
from apps.channels_wa.messaging.base import SendResult
from apps.channels_wa.models import WhatsAppChannel
from apps.contacts.models import Contact
from apps.inbox.models import Conversation, Message
from apps.library.models import MessageTemplate, QuickSnippet
from apps.workspaces.models import Workspace, WorkspaceMembership

User = get_user_model()


class SendingTests(TestCase):
    def setUp(self):
        self.workspace = Workspace.objects.create(name="Alpha")
        self.user = User.objects.create_user("agent", "agent@example.com", "pw12345678")
        WorkspaceMembership.objects.create(user=self.user, workspace=self.workspace, role="agent")
        self.channel = WhatsAppChannel.objects.create(
            workspace=self.workspace, display_name="Support", phone_number_id="123"
        )
        self.contact = Contact.objects.create(workspace=self.workspace, wa_id="27820000001")
        self.conversation = Conversation.objects.create(
            workspace=self.workspace,
            channel=self.channel,
            contact=self.contact,
            last_inbound_at=timezone.now(),
            window_expires_at=timezone.now() + dt.timedelta(hours=24),
        )
        self.client.force_login(self.user)

    def test_window_open_when_recent(self):
        self.assertTrue(self.conversation.window_open)
        self.assertIn("reply freely", self.conversation.window_hint)

    def test_window_closed_after_24_hours(self):
        self.conversation.window_expires_at = timezone.now() - dt.timedelta(minutes=1)
        self.assertFalse(self.conversation.window_open)
        self.assertIn("approved template", self.conversation.window_hint)

    def test_free_text_is_refused_outside_the_window(self):
        self.conversation.window_expires_at = timezone.now() - dt.timedelta(minutes=1)
        self.conversation.save()
        with mock.patch(
            "apps.channels_wa.messaging.meta_cloud.MetaCloudChannel.send_text"
        ) as send:
            self.client.post(
                reverse("inbox:send", args=[self.conversation.pk]), {"body": "hello"}
            )
            send.assert_not_called()
        self.assertEqual(Message.objects.count(), 0)

    @override_settings(OUTBOUND_COMMS_MODE="live")
    def test_agent_reply_is_stored_and_sent(self):
        with mock.patch(
            "apps.channels_wa.messaging.meta_cloud.MetaCloudChannel.send_text",
            return_value=SendResult(ok=True, wamid="wamid.OUT"),
        ) as send:
            self.client.post(
                reverse("inbox:send", args=[self.conversation.pk]), {"body": "Good day"}
            )
            send.assert_called_once()
        message = Message.objects.get()
        self.assertEqual(message.direction, Message.Direction.OUT)
        self.assertEqual(message.wa_status, Message.Status.SENT)
        self.conversation.refresh_from_db()
        self.assertEqual(self.conversation.assigned_to, self.user)

    def test_suppress_mode_blocks_every_number(self):
        with override_settings(OUTBOUND_COMMS_MODE="suppress"):
            self.assertIsNotNone(outbound_blocked("27820000001"))

    def test_allowlist_mode_permits_only_listed_numbers(self):
        with override_settings(
            OUTBOUND_COMMS_MODE="allowlist", OUTBOUND_ALLOWLIST=["+27 82 000 0001"]
        ):
            self.assertIsNone(outbound_blocked("27820000001"))
            self.assertIsNotNone(outbound_blocked("27820000009"))

    def test_live_mode_permits_sending(self):
        with override_settings(OUTBOUND_COMMS_MODE="live"):
            self.assertIsNone(outbound_blocked("27820000001"))

    def test_allowlist_accepts_the_same_number_written_differently(self):
        """0726124698, 27726124698 and +27 72 612 4698 are one phone."""
        for stored in ["0726124698", "27726124698", "+27 72 612 4698"]:
            with override_settings(OUTBOUND_COMMS_MODE="allowlist", OUTBOUND_ALLOWLIST=[stored]):
                for dialled in ["0726124698", "27726124698", "+27726124698"]:
                    self.assertIsNone(
                        outbound_blocked(dialled),
                        f"stored {stored} should have matched {dialled}",
                    )

    def test_allowlist_still_blocks_a_different_number(self):
        with override_settings(OUTBOUND_COMMS_MODE="allowlist", OUTBOUND_ALLOWLIST=["27726124698"]):
            self.assertIsNotNone(outbound_blocked("27831234567"))
            self.assertIsNotNone(outbound_blocked(""))


class MessageStatusTests(TestCase):
    def setUp(self):
        self.workspace = Workspace.objects.create(name="Alpha")
        self.channel = WhatsAppChannel.objects.create(
            workspace=self.workspace, display_name="S", phone_number_id="1"
        )
        contact = Contact.objects.create(workspace=self.workspace, wa_id="27820000001")
        conversation = Conversation.objects.create(
            workspace=self.workspace, channel=self.channel, contact=contact
        )
        self.message = Message.objects.create(
            workspace=self.workspace,
            conversation=conversation,
            direction=Message.Direction.OUT,
            body="hi",
            wa_status=Message.Status.SENT,
        )

    def test_status_moves_forward(self):
        self.assertTrue(self.message.advance_status("delivered"))
        self.assertEqual(self.message.wa_status, "delivered")

    def test_status_never_goes_backwards(self):
        self.message.advance_status("read")
        self.assertFalse(self.message.advance_status("delivered"))
        self.assertEqual(self.message.wa_status, "read")


class TemplateTests(TestCase):
    def test_placeholders_are_counted_and_rendered(self):
        workspace = Workspace.objects.create(name="Alpha")
        channel = WhatsAppChannel.objects.create(
            workspace=workspace, display_name="S", phone_number_id="1"
        )
        template = MessageTemplate.objects.create(
            workspace=workspace,
            channel=channel,
            name="claim_update",
            language="en_US",
            components=[{"type": "BODY", "text": "Hi {{1}}, your claim {{2}} was received."}],
        )
        self.assertEqual(template.variable_count, 2)
        self.assertEqual(
            template.preview(["Thandi", "CL-1"]), "Hi Thandi, your claim CL-1 was received."
        )
        components = template.build_components(["Thandi", "CL-1"])
        self.assertEqual(len(components[0]["parameters"]), 2)


class SnippetTests(TestCase):
    def test_placeholders_are_filled_from_the_contact(self):
        workspace = Workspace.objects.create(name="Alpha")
        contact = Contact.objects.create(
            workspace=workspace, wa_id="27820000001", display_name="Thandi Mokoena"
        )
        snippet = QuickSnippet.objects.create(
            workspace=workspace, title="Greeting", body="Hi {{contact.first_name}}, how can we help?"
        )
        self.assertEqual(snippet.render(contact=contact), "Hi Thandi, how can we help?")


class FailureExplanationTests(TestCase):
    """A failed send must say why in words the operator can act on."""

    def _message(self, wa_error):
        workspace = Workspace.objects.create(name="Alpha")
        channel = WhatsAppChannel.objects.create(
            workspace=workspace, display_name="S", phone_number_id="1"
        )
        contact = Contact.objects.create(workspace=workspace, wa_id="27726124698")
        conversation = Conversation.objects.create(
            workspace=workspace, channel=channel, contact=contact
        )
        return Message.objects.create(
            workspace=workspace,
            conversation=conversation,
            direction=Message.Direction.OUT,
            wa_status=Message.Status.FAILED,
            wa_error=wa_error,
        )

    def test_undeliverable_number_is_explained(self):
        message = self._message({"errors": [{"code": 131026, "title": "Message undeliverable"}]})
        self.assertIn("cannot receive WhatsApp messages", message.failure_explanation)
        self.assertIn("Message undeliverable", message.failure_explanation)

    def test_closed_window_is_explained(self):
        message = self._message({"errors": [{"code": 131047}]})
        self.assertIn("24 hours", message.failure_explanation)

    def test_unknown_error_still_says_something(self):
        self.assertEqual(self._message({}).failure_explanation, "WhatsApp did not say why.")


class HumanEscapeHatchTests(TestCase):
    """Every template promises 'Reply AGENT any time'. That promise is kept here."""

    def test_asking_for_a_person_is_recognised(self):
        from apps.channels_wa.inbound import _wants_a_human

        for text in ["agent", "AGENT", "human", "  Help ", "can I speak to someone",
                     "talk to a person please"]:
            self.assertTrue(_wants_a_human(text), f"{text!r} should reach a human")

    def test_ordinary_messages_are_not_escalated(self):
        from apps.channels_wa.inbound import _wants_a_human

        for text in ["", "What time does the keynote start on Wednesday morning?",
                     "Yes please, I would like to come along to the launch event"]:
            self.assertFalse(_wants_a_human(text), f"{text!r} should not escalate")


class CampaignWindowTests(TestCase):
    """An invitation to an event that has happened is worse than no invitation."""

    # Dates are derived from EVENT_DATE, never written down, so these keep
    # passing when the event moves - which is the whole point.

    def test_template_is_sendable_inside_its_window(self):
        import datetime as dt
        from apps.library.event_templates import BY_NAME, sendable_on

        inside = BY_NAME["event_invite"]["send_from"] + dt.timedelta(days=1)
        self.assertTrue(sendable_on("event_invite", inside)[0])

    def test_template_is_refused_after_the_event(self):
        import datetime as dt
        from apps.library.event_templates import EVENT_DATE, sendable_on

        allowed, reason = sendable_on("event_invite", EVENT_DATE + dt.timedelta(days=4))
        self.assertFalse(allowed)
        self.assertIn(f"{EVENT_DATE.day} {EVENT_DATE:%B}", reason)

    def test_template_is_refused_before_its_time(self):
        import datetime as dt
        from apps.library.event_templates import BY_NAME, sendable_on

        early = BY_NAME["event_teaser"]["send_from"] - dt.timedelta(days=5)
        allowed, reason = sendable_on("event_teaser", early)
        self.assertFalse(allowed)
        self.assertIn("not due to go out", reason)

    def test_templates_outside_the_campaign_are_unaffected(self):
        from apps.library.event_templates import sendable_on

        self.assertEqual(sendable_on("claim_status_update"), (True, ""))


class DateIsNotBakedIntoCopyTests(TestCase):
    """Moving the event must not mean ten template edits and ten re-reviews."""

    def test_no_template_body_contains_a_month_name(self):
        import re
        from apps.library.event_templates import TEMPLATES

        months = re.compile(
            r"January|February|March|April|May|June|July|August|September|October|"
            r"November|December"
        )
        for definition in TEMPLATES:
            for component in definition["components"]:
                if component["type"] == "BODY":
                    self.assertIsNone(
                        months.search(component["text"]),
                        f"{definition['name']} has a date written into approved copy; "
                        "pass it as a variable instead",
                    )

    def test_every_body_variable_has_a_sample(self):
        import re
        from apps.library.event_templates import TEMPLATES

        for definition in TEMPLATES:
            for component in definition["components"]:
                if component["type"] != "BODY":
                    continue
                count = len(set(re.findall(r"\{\{(\d+)\}\}", component["text"])))
                samples = component.get("example", {}).get("body_text", [[]])[0]
                self.assertEqual(
                    count, len(samples), f"{definition['name']}: {count} variables, "
                    f"{len(samples)} samples - Meta rejects that"
                )

    def test_every_template_offers_a_route_to_a_person(self):
        from apps.library.event_templates import HUMAN_FOOTER, TEMPLATES

        for definition in TEMPLATES:
            footers = [c for c in definition["components"] if c["type"] == "FOOTER"]
            self.assertTrue(footers, f"{definition['name']} has no footer")
            self.assertEqual(footers[0]["text"], HUMAN_FOOTER)

    def test_windows_move_with_the_event_date(self):
        from apps.library.event_templates import BY_NAME, EVENT_DATE

        self.assertEqual(BY_NAME["event_morning"]["send_from"], EVENT_DATE)
        self.assertLess(BY_NAME["event_invite"]["send_until"], EVENT_DATE)
