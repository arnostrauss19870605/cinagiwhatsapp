"""The contacts directory, per-contact history, and the chat exports.

Exporting a customer's chat history is data leaving the platform, so the
tests pin the permission line (supervisors and up), the audit trail, and
that every format actually carries the conversation.
"""

import datetime as dt
import json

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.channels_wa.models import WhatsAppChannel
from apps.contacts.models import Audience, Contact
from apps.core.models import AuditLog
from apps.inbox.models import Conversation, Message
from apps.workspaces.models import Workspace, WorkspaceMembership

User = get_user_model()


class ContactPagesBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.workspace = Workspace.objects.create(name="Cinagi Broker Support")
        cls.supervisor = User.objects.create_user("super", "s@example.com", "pw12345678")
        cls.agent = User.objects.create_user("agent", "a@example.com", "pw12345678")
        WorkspaceMembership.objects.create(
            user=cls.supervisor, workspace=cls.workspace, role="supervisor"
        )
        WorkspaceMembership.objects.create(user=cls.agent, workspace=cls.workspace, role="agent")
        cls.channel = WhatsAppChannel.objects.create(
            workspace=cls.workspace, display_name="Broker Support",
            phone_number="+27842358896", phone_number_id="123", is_active=True,
        )
        cls.contact = Contact.objects.create(
            workspace=cls.workspace, wa_id="27726124698", display_name="Thabo Mokoena"
        )
        cls.conversation = Conversation.objects.create(
            workspace=cls.workspace, channel=cls.channel, contact=cls.contact,
            last_inbound_at=timezone.now(),
        )
        Message.objects.create(
            workspace=cls.workspace, conversation=cls.conversation,
            direction=Message.Direction.IN, kind=Message.Kind.TEXT,
            body="Is there parking?",
        )
        Message.objects.create(
            workspace=cls.workspace, conversation=cls.conversation,
            direction=Message.Direction.OUT, actor=Message.Actor.AGENT,
            author=cls.agent, kind=Message.Kind.TEXT,
            body="Plenty - follow the Cinagi banners.", wa_status=Message.Status.READ,
        )

    def setUp(self):
        self.client.force_login(self.agent)


class ContactListTests(ContactPagesBase):
    def test_search_finds_by_name_and_by_local_number_form(self):
        for query in ("thabo", "072 612 4698", "27726124698"):
            response = self.client.get(reverse("contacts:contacts"), {"q": query})
            self.assertContains(response, "Thabo Mokoena", msg_prefix=query)

    def test_another_workspaces_contacts_never_appear(self):
        other = Workspace.objects.create(name="Bravo")
        Contact.objects.create(workspace=other, wa_id="27820000009", display_name="Stranger")
        response = self.client.get(reverse("contacts:contacts"))
        self.assertNotContains(response, "Stranger")

    def test_the_detail_page_shows_the_whole_history(self):
        response = self.client.get(reverse("contacts:contact_detail", args=[self.contact.pk]))
        self.assertContains(response, "Is there parking?")
        self.assertContains(response, "follow the Cinagi banners")

    def test_agents_can_set_audience_membership(self):
        vips = Audience.objects.create(workspace=self.workspace, name="VIP brokers")
        launch = Audience.objects.create(workspace=self.workspace, name="Launch attendees")
        self.client.post(
            reverse("contacts:contact_audiences", args=[self.contact.pk]),
            {"audiences": [vips.pk, launch.pk]},
        )
        self.assertEqual(self.contact.audiences.count(), 2)
        self.client.post(
            reverse("contacts:contact_audiences", args=[self.contact.pk]),
            {"audiences": [vips.pk]},
        )
        self.assertEqual(list(self.contact.audiences.all()), [vips])

    def test_an_audience_from_another_workspace_cannot_be_attached(self):
        other = Workspace.objects.create(name="Bravo")
        foreign = Audience.objects.create(workspace=other, name="Foreign")
        self.client.post(
            reverse("contacts:contact_audiences", args=[self.contact.pk]),
            {"audiences": [foreign.pk]},
        )
        self.assertEqual(self.contact.audiences.count(), 0)

    def test_the_next_redirect_only_accepts_local_paths(self):
        response = self.client.post(
            reverse("contacts:contact_audiences", args=[self.contact.pk]),
            {"audiences": [], "next": "https://evil.example/phish"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith("/contacts/"))


class ChatExportTests(ContactPagesBase):
    def setUp(self):
        self.client.force_login(self.supervisor)

    def _export(self, fmt):
        return self.client.get(
            reverse("contacts:contact_export", args=[self.contact.pk, fmt])
        )

    def test_agents_may_not_export(self):
        self.client.force_login(self.agent)
        self.assertEqual(self._export("json").status_code, 403)

    def test_json_carries_the_conversation(self):
        response = self._export("json")
        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.content)
        self.assertEqual(payload["contact"]["number"], "+27726124698")
        bodies = [m["body"] for m in payload["messages"]]
        self.assertIn("Is there parking?", bodies)
        directions = [m["direction"] for m in payload["messages"]]
        self.assertEqual(directions, ["in", "out"])

    def test_csv_has_a_header_and_both_rows(self):
        response = self._export("csv")
        text = response.content.decode("utf-8")
        self.assertIn("at,direction,from,kind,status,body", text)
        self.assertIn("Is there parking?", text)
        self.assertIn("read", text)
        self.assertIn('attachment; filename="thabo-mokoena-chat.csv"',
                      response["Content-Disposition"])

    def test_pdf_is_a_real_pdf(self):
        response = self._export("pdf")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.content.startswith(b"%PDF"))
        self.assertGreater(len(response.content), 1500)

    def test_every_export_is_audited(self):
        self._export("csv")
        entry = AuditLog.objects.filter(action="contact.exported").latest("pk")
        self.assertEqual(entry.detail["fmt"], "csv")
        self.assertEqual(entry.detail["messages"], 2)

    def test_a_contact_in_another_workspace_is_a_404(self):
        other = Workspace.objects.create(name="Bravo")
        stranger = Contact.objects.create(workspace=other, wa_id="27820000009")
        response = self.client.get(
            reverse("contacts:contact_export", args=[stranger.pk, "json"])
        )
        self.assertEqual(response.status_code, 404)

    def test_internal_notes_are_labelled_in_the_export(self):
        Message.objects.create(
            workspace=self.workspace, conversation=self.conversation,
            direction=Message.Direction.OUT, actor=Message.Actor.AGENT,
            author=self.agent, kind=Message.Kind.TEXT,
            body="Client is a VIP", payload={"note": True},
        )
        payload = json.loads(self._export("json").content)
        kinds = [m["kind"] for m in payload["messages"]]
        self.assertIn("internal note", kinds)
