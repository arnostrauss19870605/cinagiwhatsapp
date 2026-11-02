"""The respond.io-shaped API, and keeping the greeting menu away from people answering us.

Another system calling us is a door into customers' phones, so these tests
pin down the key, the workspace it binds to, that nothing is created or sent
for a request that is refused, and that every send keeps the platform's
rules (opt-out, the 24-hour window, test mode).
"""

import datetime as dt
import json
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.channels_wa.inbound import process_value
from apps.channels_wa.messaging.base import SendResult
from apps.channels_wa.models import WhatsAppChannel
from apps.contacts.models import Contact
from apps.inbox.models import Conversation, Message
from apps.integrations.models import ApiKey
from apps.library.models import MessageTemplate
from apps.workflows.models import Workflow, WorkflowRun, WorkflowVersion
from apps.workspaces.models import Workspace, WorkspaceMembership

NUMBER = "27726124698"
POST_MESSAGE = "apps.channels_wa.messaging.meta_cloud.MetaCloudChannel._post_message"


class ApiBase(TestCase):
    def setUp(self):
        self.workspace = Workspace.objects.create(name="Cinagi Broker Support")
        self.channel = WhatsAppChannel.objects.create(
            workspace=self.workspace, display_name="Support", phone_number_id="123",
            status=WhatsAppChannel.Status.CONNECTED, is_active=True,
        )
        self.key, self.raw = ApiKey.issue(self.workspace, "BIMS")
        self.template = MessageTemplate.objects.create(
            workspace=self.workspace, channel=self.channel, name="policy_reminder", language="en",
            status=MessageTemplate.Status.APPROVED,
            components=[{"type": "BODY", "text": "Hi {{1}}, policy {{2}} renews soon."}],
        )

    def call(self, path, body=None, *, key=None, method="post"):
        headers = {"HTTP_AUTHORIZATION": f"Bearer {key if key is not None else self.raw}"}
        if method == "get":
            return self.client.get(f"/api/v2/{path}", **headers)
        return self.client.post(f"/api/v2/{path}", json.dumps(body or {}), content_type="application/json",
                                **headers)

    def template_body(self, **template):
        return {"message": {"type": "whatsapp_template", "template": {
            "name": "policy_reminder", "languageCode": "en",
            "components": [{"type": "body", "parameters": [{"type": "text", "text": "Thabo"},
                                                          {"type": "text", "text": "GAP1"}]}],
            **template}}}


class AuthTests(ApiBase):
    def test_a_missing_wrong_or_revoked_key_is_refused(self):
        for key in ("", "cwa_wrong", "not-even-a-key"):
            with self.subTest(key=key):
                self.assertEqual(self.call(f"contact/phone:{NUMBER}", key=key, method="get").status_code, 401)
        self.key.revoked_at = timezone.now()
        self.key.save()
        self.assertEqual(self.call(f"contact/phone:{NUMBER}", method="get").status_code, 401)

    def test_only_a_hash_of_the_key_is_stored(self):
        self.assertNotIn(self.raw, {self.key.key_hash, self.key.prefix})
        self.assertTrue(self.raw.startswith(self.key.prefix))

    def test_a_key_only_sees_its_own_workspace(self):
        other = Workspace.objects.create(name="Elsewhere")
        theirs = Contact.objects.create(workspace=other, wa_id=NUMBER)
        self.assertEqual(self.call(f"contact/id:{theirs.pk}", method="get").status_code, 404)
        other_channel = WhatsAppChannel.objects.create(workspace=other, display_name="X", phone_number_id="9",
                                                       is_active=True)
        response = self.call(f"contact/phone:{NUMBER}/message", {**self.template_body(), "channelId": other_channel.pk})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Contact.objects.filter(workspace=self.workspace).exists())


class ContactTests(ApiBase):
    def test_create_then_update_by_phone(self):
        response = self.call("contact/create_or_update/phone:+27726124698", {
            "firstName": "Thabo", "lastName": "Mokoena", "email": "thabo@example.com",
            "custom_fields": [{"name": "policy_number", "value": "GAP1"}]})
        contact = Contact.objects.get(wa_id=NUMBER)
        self.assertEqual(response.json(), {"contactId": contact.pk})
        self.assertEqual(contact.display_name, "Thabo Mokoena")
        self.assertEqual(contact.attributes["policy_number"], "GAP1")
        self.call(f"contact/create_or_update/id:{contact.pk}", {"lastName": "Dube"})
        contact.refresh_from_db()
        self.assertEqual(contact.display_name, "Thabo Dube")
        self.assertEqual(Contact.objects.count(), 1)

    def test_local_number_formats_find_the_same_person(self):
        self.call("contact/create_or_update/phone:0726124698", {"firstName": "Thabo"})
        data = self.call("contact/phone:+27 72 612 4698", method="get").json()
        self.assertEqual(data["firstName"], "Thabo")

    def test_email_alone_cannot_make_a_whatsapp_contact(self):
        response = self.call("contact/create_or_update/email:thabo@example.com", {"firstName": "Thabo"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("mobile number", response.json()["message"])
        self.call("contact/create_or_update/email:thabo@example.com", {"phone": "0726124698"})
        self.assertEqual(self.call("contact/email:thabo@example.com", method="get").json()["phone"], "+" + NUMBER)


class MessageTests(ApiBase):
    @override_settings(OUTBOUND_COMMS_MODE="live")
    def test_a_template_goes_out_and_is_recorded(self):
        with mock.patch(POST_MESSAGE, return_value=SendResult(ok=True, wamid="wamid.api")) as post:
            response = self.call(f"contact/phone:{NUMBER}/message", self.template_body())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "sent")
        sent = post.call_args.args[1]["template"]
        self.assertEqual(sent["name"], "policy_reminder")
        self.assertEqual([p["text"] for p in sent["components"][0]["parameters"]], ["Thabo", "GAP1"])
        message = Message.objects.get()
        self.assertEqual((message.actor, message.payload["auto"], message.payload["api_key"]), ("bot", "api", "BIMS"))

    @override_settings(OUTBOUND_COMMS_MODE="live")
    def test_url_buttons_are_passed_through(self):
        body = self.template_body()
        body["message"]["template"]["components"].append(
            {"type": "button", "sub_type": "url", "index": "0", "parameters": [{"type": "text", "text": "abc"}]})
        with mock.patch(POST_MESSAGE, return_value=SendResult(ok=True, wamid="w")) as post:
            self.call(f"contact/phone:{NUMBER}/message", body)
        components = post.call_args.args[1]["template"]["components"]
        self.assertEqual(components[-1]["sub_type"], "url")

    def test_a_wrong_language_is_explained_and_nothing_is_created(self):
        response = self.call(f"contact/phone:{NUMBER}/message", self.template_body(languageCode="en_US"))
        self.assertEqual(response.status_code, 400)
        self.assertIn("approved in en, not 'en_US'", response.json()["message"])
        self.assertFalse(Contact.objects.exists())
        self.assertFalse(Conversation.objects.exists())

    def test_the_wrong_number_of_values_is_refused(self):
        body = self.template_body()
        body["message"]["template"]["components"][0]["parameters"].pop()
        response = self.call(f"contact/phone:{NUMBER}/message", body)
        self.assertIn("takes 2 body value(s); 1 were sent", response.json()["message"])

    def test_text_needs_an_open_24_hour_window(self):
        response = self.call(f"contact/phone:{NUMBER}/message", {"message": {"type": "text", "text": "Hi"}})
        self.assertEqual(response.status_code, 400)
        self.assertIn("approved template", response.json()["message"])
        self.assertFalse(Conversation.objects.exists())

    @override_settings(OUTBOUND_COMMS_MODE="live")
    def test_text_inside_the_window_is_sent(self):
        contact = Contact.objects.create(workspace=self.workspace, wa_id=NUMBER)
        conversation = Conversation.objects.create(workspace=self.workspace, channel=self.channel, contact=contact,
                                                   status=Conversation.Status.QUEUED)
        conversation.touch_inbound()
        conversation.save()
        with mock.patch(POST_MESSAGE, return_value=SendResult(ok=True, wamid="w")):
            response = self.call(f"contact/id:{contact.pk}/message", {"message": {"type": "text", "text": "Paid."}})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(conversation.messages.get().body, "Paid.")

    @override_settings(OUTBOUND_COMMS_MODE="live")
    def test_someone_who_replied_stop_is_not_messaged(self):
        Contact.objects.create(workspace=self.workspace, wa_id=NUMBER, opted_out_at=timezone.now())
        with mock.patch(POST_MESSAGE) as post:
            response = self.call(f"contact/phone:{NUMBER}/message", self.template_body())
        post.assert_not_called()
        self.assertEqual(response.status_code, 422)
        self.assertIn("opted out", response.json()["message"])

    def test_test_mode_answers_422_and_sends_nothing(self):
        with mock.patch(POST_MESSAGE.replace("_post_message", "_request")) as request:
            response = self.call(f"contact/phone:{NUMBER}/message", self.template_body())
        request.assert_not_called()
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["status"], "blocked")


class QuietReplyTests(ApiBase):
    """A person answering our message gets the team, not the greeting menu."""

    def setUp(self):
        super().setUp()
        self.workflow = Workflow.objects.create(workspace=self.workspace, name="Main", is_active=True)
        version = WorkflowVersion.objects.create(workflow=self.workflow, number=1, published_at=timezone.now(), graph={
            "start": "hi", "steps": {"hi": {"type": "send_message", "name": "Hi",
                                            "config": {"text": "Welcome! Pick an option."}, "next": {}}}})
        self.workflow.published = version
        self.workflow.save()

    def reply(self, text="Thanks, got it"):
        process_value(self.channel, {
            "metadata": {"phone_number_id": "123"},
            "contacts": [{"profile": {"name": "Thabo"}, "wa_id": NUMBER}],
            "messages": [{"from": NUMBER, "id": f"wamid.{timezone.now().timestamp()}", "type": "text",
                          "timestamp": str(int(timezone.now().timestamp())), "text": {"body": text}}],
        })

    @override_settings(OUTBOUND_COMMS_MODE="live")
    def test_a_reply_to_an_api_template_skips_the_menu_even_after_the_chat_was_closed(self):
        with mock.patch(POST_MESSAGE, return_value=SendResult(ok=True, wamid="w")):
            self.call(f"contact/phone:{NUMBER}/message", self.template_body())
            Conversation.objects.update(status=Conversation.Status.RESOLVED)
            self.reply()
        self.assertFalse(WorkflowRun.objects.exists())
        chat = Conversation.objects.get(status=Conversation.Status.QUEUED)
        self.assertIn("replying to a message we sent", chat.messages.filter(payload__note=True).get().body)

    @override_settings(OUTBOUND_COMMS_MODE="live")
    def test_after_the_quiet_period_the_menu_is_back(self):
        with mock.patch(POST_MESSAGE, return_value=SendResult(ok=True, wamid="w")):
            self.call(f"contact/phone:{NUMBER}/message", self.template_body())
            Message.objects.update(created_at=timezone.now() - dt.timedelta(hours=73))
            Conversation.objects.update(status=Conversation.Status.RESOLVED)
            self.reply("Hello")
        self.assertTrue(WorkflowRun.objects.exists())

    def test_the_workflows_own_messages_do_not_count(self):
        self.reply("Hello")
        self.assertEqual(WorkflowRun.objects.count(), 1)
        Conversation.objects.update(status=Conversation.Status.RESOLVED)
        self.reply("Hello again")
        self.assertEqual(WorkflowRun.objects.count(), 2)

    @override_settings(OUTBOUND_COMMS_MODE="live")
    def test_an_api_message_stops_a_menu_that_is_waiting(self):
        version = self.workflow.published
        version.graph = {"start": "ask", "steps": {"ask": {
            "type": "ask_question", "name": "Menu",
            "config": {"text": "Pick", "answer": "choice", "options": ["A", "B"]}, "next": {}}}}
        version.save()
        with mock.patch(POST_MESSAGE, return_value=SendResult(ok=True, wamid="w")):
            self.reply("Hello")
            self.assertEqual(WorkflowRun.objects.get().status, WorkflowRun.Status.WAITING)
            self.call(f"contact/phone:{NUMBER}/message", {"message": {"type": "text", "text": "Update for you"}})
        self.assertEqual(WorkflowRun.objects.get().status, WorkflowRun.Status.STOPPED)


class ApiKeyPageTests(ApiBase):
    def setUp(self):
        super().setUp()
        User = get_user_model()
        self.manager = User.objects.create_user("manager", "m@example.com", "pw12345678")
        self.agent = User.objects.create_user("agent", "a@example.com", "pw12345678")
        WorkspaceMembership.objects.create(user=self.manager, workspace=self.workspace, role="admin")
        WorkspaceMembership.objects.create(user=self.agent, workspace=self.workspace, role="agent")

    def test_a_new_key_is_shown_once(self):
        self.client.force_login(self.manager)
        response = self.client.post("/integrations/api-keys/", {"name": "Zapier"}, follow=True)
        key = ApiKey.objects.get(name="Zapier")
        self.assertContains(response, "shown only once")
        self.assertContains(response, key.prefix)
        self.assertNotContains(self.client.get("/integrations/api-keys/"), "shown only once")

    def test_agents_cannot_manage_keys(self):
        self.client.force_login(self.agent)
        self.assertEqual(self.client.get("/integrations/api-keys/").status_code, 403)

    def test_revoking(self):
        self.client.force_login(self.manager)
        self.client.post(f"/integrations/api-keys/{self.key.pk}/revoke/")
        self.assertEqual(self.call(f"contact/phone:{NUMBER}", method="get").status_code, 401)
