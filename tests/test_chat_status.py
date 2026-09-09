"""What a chat's status says after a campaign message, a reply, and an agent's answer.

A bulk send must not make hundreds of chats look like customers waiting for
help. Nobody is waiting until the customer writes back; then somebody is.
"""

import datetime as dt
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.channels_wa.inbound import _store_message
from apps.channels_wa.messaging.base import SendResult
from apps.channels_wa.models import WhatsAppChannel
from apps.channels_wa.outbound import send_template, send_text
from apps.contacts.models import Contact
from apps.inbox.models import Conversation, Message
from apps.library.models import MessageTemplate
from apps.workspaces.models import Workspace, WorkspaceMembership

User = get_user_model()
SENT = SendResult(ok=True, wamid="wamid.X")
SEND_TEXT = "apps.channels_wa.messaging.meta_cloud.MetaCloudChannel.send_text"
SEND_TEMPLATE = "apps.channels_wa.messaging.meta_cloud.MetaCloudChannel.send_template"


@override_settings(OUTBOUND_COMMS_MODE="live")
class ChatStatusTests(TestCase):
    def setUp(self):
        self.workspace = Workspace.objects.create(name="Cinagi Broker Support", auto_assign_enabled=False)
        self.channel = WhatsAppChannel.objects.create(
            workspace=self.workspace, display_name="Broker Support", phone_number_id="123"
        )
        self.agent = User.objects.create_user("sam@cinagi.co.za", "sam@cinagi.co.za", first_name="Sam")
        WorkspaceMembership.objects.create(user=self.agent, workspace=self.workspace, role="agent")
        self.contact = Contact.objects.create(workspace=self.workspace, wa_id="27820000001", display_name="Thabo")
        self.template = MessageTemplate.objects.create(
            workspace=self.workspace, channel=self.channel, name="event_invite", language="en",
            status=MessageTemplate.Status.APPROVED,
            components=[{"type": "BODY", "text": "Hi {{1}}"}],
        )

    def _campaign_chat(self):
        conversation = Conversation.objects.create(
            workspace=self.workspace, channel=self.channel, contact=self.contact,
            status=Conversation.Status.BOT,
        )
        with mock.patch(SEND_TEMPLATE, return_value=SENT):
            send_template(conversation, self.template, ["Thabo"], actor=Message.Actor.BOT)
        conversation.refresh_from_db()
        return conversation

    def _reply(self, conversation, body="Yes please", wamid="wamid.in1"):
        _store_message(
            self.channel,
            {"id": wamid, "from": self.contact.wa_id, "type": "text", "text": {"body": body},
             "timestamp": str(int(timezone.now().timestamp()))},
            {},
        )
        conversation.refresh_from_db()
        return conversation

    def test_a_campaign_message_does_not_put_the_chat_in_the_waiting_queue(self):
        conversation = self._campaign_chat()
        self.assertEqual(conversation.status, Conversation.Status.BOT)
        self.assertIsNone(conversation.assigned_to)

        self.client.force_login(self.agent)
        waiting = self.client.get(reverse("inbox:inbox") + "?view=unassigned")
        self.assertNotIn(conversation, list(waiting.context["conversations"]))
        self.assertEqual(waiting.context["counts"]["unassigned"], 0)
        everything = self.client.get(reverse("inbox:inbox") + "?view=open")
        self.assertContains(everything, "Sent, no reply yet")
        self.assertNotContains(everything, "Waiting for someone")

    def test_a_reply_the_automation_does_not_understand_needs_a_person(self):
        conversation = self._campaign_chat()
        with mock.patch(SEND_TEXT, return_value=SENT):
            self._reply(conversation, "Can you call me about my policy?")
        self.assertEqual(conversation.status, Conversation.Status.QUEUED)

        self.client.force_login(self.agent)
        waiting = self.client.get(reverse("inbox:inbox") + "?view=unassigned")
        self.assertIn(conversation, list(waiting.context["conversations"]))
        self.assertContains(waiting, "Waiting for someone")

    def test_an_agents_reply_takes_the_chat(self):
        conversation = self._campaign_chat()
        with mock.patch(SEND_TEXT, return_value=SENT):
            send_text(conversation, "Hello Thabo", author=self.agent)
        conversation.refresh_from_db()
        self.assertEqual(conversation.status, Conversation.Status.ASSIGNED)

    def test_a_customer_reply_to_a_waiting_chat_with_nobody_on_it_goes_to_the_queue(self):
        conversation = Conversation.objects.create(
            workspace=self.workspace, channel=self.channel, contact=self.contact,
            status=Conversation.Status.WAITING,
            last_outbound_at=timezone.now() - dt.timedelta(hours=1),
        )
        with mock.patch(SEND_TEXT, return_value=SENT):
            self._reply(conversation)
        self.assertEqual(conversation.status, Conversation.Status.QUEUED)

    def test_a_customer_reply_to_an_agents_chat_stays_with_that_agent(self):
        conversation = Conversation.objects.create(
            workspace=self.workspace, channel=self.channel, contact=self.contact,
            status=Conversation.Status.WAITING, assigned_to=self.agent,
            last_outbound_at=timezone.now() - dt.timedelta(hours=1),
        )
        with mock.patch(SEND_TEXT, return_value=SENT):
            self._reply(conversation)
        self.assertEqual((conversation.status, conversation.assigned_to), (Conversation.Status.ASSIGNED, self.agent))

    def test_the_thread_fragment_carries_the_ids_the_live_refresh_swaps(self):
        conversation = self._campaign_chat()
        self.client.force_login(self.agent)
        fragment = self.client.get(reverse("inbox:thread", args=[conversation.pk]))
        self.assertContains(fragment, 'id="thread-header"')
        self.assertContains(fragment, 'id="messages"')
        page = self.client.get(reverse("inbox:conversation", args=[conversation.pk]))
        self.assertContains(page, 'hx-select="#messages"')
        self.assertContains(page, "every 10s")
