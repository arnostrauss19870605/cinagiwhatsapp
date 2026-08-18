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
