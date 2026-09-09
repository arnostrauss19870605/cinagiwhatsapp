"""WhatsApp alerts to the people on duty: consultant requests and waiting chats."""

import datetime as dt
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.channels_wa.inbound import _escalate_to_human
from apps.channels_wa.messaging.base import SendResult
from apps.channels_wa.models import WhatsAppChannel
from apps.contacts.models import Contact
from apps.inbox import alerts
from apps.inbox.models import Conversation, Message
from apps.library.models import MessageTemplate
from apps.workspaces.models import Workspace

SENT = SendResult(ok=True, wamid="wamid.ALERT")
SEND_TEXT = "apps.channels_wa.messaging.meta_cloud.MetaCloudChannel.send_text"
SEND_TEMPLATE = "apps.channels_wa.messaging.meta_cloud.MetaCloudChannel.send_template"


@override_settings(
    OUTBOUND_COMMS_MODE="live",
    NOTIFICATION_NUMBERS=["0726124698", "+27 73 377 3247"],
    NOTIFICATION_PENDING_MINUTES=10,
    NOTIFICATION_REPEAT_MINUTES=60,
    PUBLIC_BASE_URL="https://brokers.example.test",
)
class AlertTests(TestCase):
    def setUp(self):
        self.workspace = Workspace.objects.create(name="Cinagi Broker Support")
        self.channel = WhatsAppChannel.objects.create(
            workspace=self.workspace, display_name="Broker Support", phone_number_id="123"
        )
        self.contact = Contact.objects.create(
            workspace=self.workspace, wa_id="27820000001", display_name="Thabo Mokoena"
        )
        self.conversation = self._conversation(self.contact, minutes_ago=15)

    def _conversation(self, contact, minutes_ago, **extra):
        moment = timezone.now() - dt.timedelta(minutes=minutes_ago)
        return Conversation.objects.create(
            workspace=self.workspace,
            channel=self.channel,
            contact=contact,
            last_inbound_at=moment,
            window_expires_at=moment + dt.timedelta(hours=24),
            **extra,
        )

    def test_numbers_are_normalised_to_international_form(self):
        self.assertEqual(alerts.recipients(), ["27726124698", "27733773247"])

    def test_asking_for_a_consultant_alerts_everyone_on_duty(self):
        message = Message.objects.create(
            workspace=self.workspace,
            conversation=self.conversation,
            direction=Message.Direction.IN,
            body="Cinagi Consultant",
        )
        with mock.patch(SEND_TEXT, return_value=SENT) as send:
            _escalate_to_human(self.conversation, message)

        self.assertEqual(send.call_count, 2)
        numbers = sorted(call.args[0] for call in send.call_args_list)
        self.assertEqual(numbers, ["27726124698", "27733773247"])
        text = send.call_args_list[0].args[1]
        self.assertIn("Thabo Mokoena (+27820000001)", text)
        self.assertIn("asked to speak to a consultant", text)
        self.assertIn("Cinagi Broker Support", text)
        self.assertIn("https://brokers.example.test/inbox/", text)
        # The alert is a nudge to a colleague, not part of the customer's chat.
        self.assertEqual(self.conversation.messages.filter(direction=Message.Direction.OUT).count(), 0)

    def test_an_approved_alert_template_is_preferred_over_free_text(self):
        MessageTemplate.objects.create(
            workspace=self.workspace,
            channel=self.channel,
            name="consultant_alert",
            language="en",
            status=MessageTemplate.Status.APPROVED,
        )
        with mock.patch(SEND_TEMPLATE, return_value=SENT) as template, mock.patch(
            SEND_TEXT, return_value=SENT
        ) as text:
            alerts.notify_consultant_requested(self.conversation)

        text.assert_not_called()
        self.assertEqual(template.call_count, 2)
        args = template.call_args_list[0].args
        self.assertEqual(args[1:3], ("consultant_alert", "en"))
        parameter = args[3][0]["parameters"][0]["text"]
        self.assertNotIn("\n", parameter)
        self.assertIn("asked to speak to a consultant", parameter)

    @override_settings(NOTIFICATION_NUMBERS=[])
    def test_nothing_is_sent_when_no_numbers_are_configured(self):
        with mock.patch(SEND_TEXT, return_value=SENT) as send:
            alerts.notify_consultant_requested(self.conversation)
            self.assertEqual(alerts.notify_pending(), 0)
        send.assert_not_called()

    def test_a_transport_failure_never_breaks_the_inbound_flow(self):
        message = Message.objects.create(
            workspace=self.workspace, conversation=self.conversation, body="agent"
        )
        with mock.patch(SEND_TEXT, side_effect=RuntimeError("boom")):
            _escalate_to_human(self.conversation, message)
        self.conversation.refresh_from_db()
        self.assertEqual(self.conversation.handoff_reason, "asked for a person")

    # -- chats waiting for a reply -------------------------------------------

    def test_a_chat_waiting_too_long_is_reported_once_per_wave(self):
        with mock.patch(SEND_TEXT, return_value=SENT) as send:
            self.assertEqual(alerts.notify_pending(), 1)
        self.assertEqual(send.call_count, 2)
        text = send.call_args_list[0].args[1]
        self.assertIn("1 chat is waiting for a reply on Cinagi Broker Support", text)
        self.assertIn("Thabo Mokoena (+27820000001) waiting 15 min", text)
        self.assertIn("/inbox/", text)

        self.conversation.refresh_from_db()
        self.assertIsNotNone(self.conversation.pending_alert_at)

        with mock.patch(SEND_TEXT, return_value=SENT) as send:
            self.assertEqual(alerts.notify_pending(), 0)
        send.assert_not_called()

    def test_a_fresh_customer_message_triggers_a_new_alert(self):
        self.conversation.pending_alert_at = timezone.now() - dt.timedelta(minutes=5)
        self.conversation.save()
        with mock.patch(SEND_TEXT, return_value=SENT) as send:
            self.assertEqual(alerts.notify_pending(), 0)
        send.assert_not_called()

        # The customer writes again; the earlier alert no longer covers it.
        self.conversation.touch_inbound(timezone.now() - dt.timedelta(minutes=4))
        self.conversation.save()
        with mock.patch(SEND_TEXT, return_value=SENT) as send:
            alerts.notify_pending(now=timezone.now() + dt.timedelta(minutes=10))
        self.assertEqual(send.call_count, 2)

    def test_a_still_waiting_chat_is_repeated_after_the_repeat_interval(self):
        self.conversation.pending_alert_at = timezone.now() - dt.timedelta(minutes=61)
        self.conversation.last_inbound_at = timezone.now() - dt.timedelta(minutes=90)
        self.conversation.save()
        with mock.patch(SEND_TEXT, return_value=SENT) as send:
            self.assertEqual(alerts.notify_pending(), 1)
        self.assertEqual(send.call_count, 2)

    def test_recent_or_answered_or_closed_chats_are_not_reported(self):
        self.conversation.last_inbound_at = timezone.now() - dt.timedelta(minutes=3)
        self.conversation.save()
        answered = self._conversation(
            Contact.objects.create(workspace=self.workspace, wa_id="27820000002"),
            minutes_ago=30,
            last_outbound_at=timezone.now() - dt.timedelta(minutes=20),
        )
        closed = self._conversation(
            Contact.objects.create(workspace=self.workspace, wa_id="27820000003"),
            minutes_ago=30,
            status=Conversation.Status.RESOLVED,
        )
        with mock.patch(SEND_TEXT, return_value=SENT) as send:
            self.assertEqual(alerts.notify_pending(), 0)
        send.assert_not_called()
        self.assertFalse(answered.pending_alert_at or closed.pending_alert_at)

    def test_several_waiting_chats_go_out_as_one_digest(self):
        for n in range(2, 5):
            self._conversation(
                Contact.objects.create(workspace=self.workspace, wa_id=f"2782000000{n}"),
                minutes_ago=20,
            )
        with mock.patch(SEND_TEXT, return_value=SENT) as send:
            self.assertEqual(alerts.notify_pending(), 4)
        self.assertEqual(send.call_count, 2)
        self.assertIn("4 chats are waiting", send.call_args_list[0].args[1])

    def test_the_on_duty_phones_own_chats_are_ignored(self):
        self.conversation.last_inbound_at = timezone.now() - dt.timedelta(minutes=40)
        self.conversation.save()
        staff = self._conversation(
            Contact.objects.create(workspace=self.workspace, wa_id="27726124698"), minutes_ago=40
        )
        self.assertNotIn(staff, list(alerts.pending_conversations()))
