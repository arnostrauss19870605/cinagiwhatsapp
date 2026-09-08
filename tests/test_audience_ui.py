"""The audience pages and the bulk send screen.

Bulk sending from a browser is the platform's biggest red button, so the
tests pin down who may press it (managers), that pressing it only queues
background work, and that a spreadsheet upload can never duplicate a person.
"""

from io import BytesIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.channels_wa.messaging.base import SendResult
from apps.channels_wa.models import WhatsAppChannel
from apps.contacts.models import Audience, Contact
from apps.inbox.models import Message
from apps.library.models import MessageTemplate
from apps.workspaces.models import Workspace, WorkspaceMembership

User = get_user_model()


class AudienceUiBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.workspace = Workspace.objects.create(name="Cinagi Broker Support")
        cls.manager = User.objects.create_user("manager", "m@example.com", "pw12345678")
        cls.agent = User.objects.create_user("agent", "a@example.com", "pw12345678")
        WorkspaceMembership.objects.create(user=cls.manager, workspace=cls.workspace, role="admin")
        WorkspaceMembership.objects.create(user=cls.agent, workspace=cls.workspace, role="agent")
        cls.channel = WhatsAppChannel.objects.create(
            workspace=cls.workspace, display_name="Broker Support",
            phone_number="+27842358896", phone_number_id="123", is_active=True,
        )

    def setUp(self):
        self.client.force_login(self.manager)


class AudiencePageTests(AudienceUiBase):
    def test_a_manager_can_create_an_audience(self):
        response = self.client.post(
            reverse("contacts:audiences"), {"name": "Launch attendees", "description": "RSVP yes"}
        )
        audience = Audience.objects.get(name="Launch attendees")
        self.assertRedirects(response, reverse("contacts:audience_detail", args=[audience.pk]))

    def test_an_agent_may_not_manage_audiences(self):
        self.client.force_login(self.agent)
        self.assertEqual(self.client.get(reverse("contacts:audiences")).status_code, 403)

    def test_duplicate_names_are_refused_in_plain_language(self):
        Audience.objects.create(workspace=self.workspace, name="Launch attendees")
        response = self.client.post(reverse("contacts:audiences"), {"name": "launch ATTENDEES"})
        self.assertEqual(Audience.objects.count(), 1)
        self.assertContains(response, "already an audience")

    def test_members_can_be_added_and_removed(self):
        audience = Audience.objects.create(workspace=self.workspace, name="VIPs")
        contact = Contact.objects.create(
            workspace=self.workspace, wa_id="27726124698", display_name="Thabo Mokoena"
        )
        self.client.post(
            reverse("contacts:audience_add", args=[audience.pk]), {"contact": contact.pk}
        )
        self.assertEqual(audience.contacts.count(), 1)
        self.client.post(
            reverse("contacts:audience_remove", args=[audience.pk]), {"contact": contact.pk}
        )
        self.assertEqual(audience.contacts.count(), 0)

    def test_a_contact_from_another_workspace_cannot_be_added(self):
        other = Workspace.objects.create(name="Bravo")
        stranger = Contact.objects.create(workspace=other, wa_id="27820000009")
        audience = Audience.objects.create(workspace=self.workspace, name="VIPs")
        response = self.client.post(
            reverse("contacts:audience_add", args=[audience.pk]), {"contact": stranger.pk}
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(audience.contacts.count(), 0)


class AudienceImportTests(AudienceUiBase):
    def _upload(self, content):
        audience = Audience.objects.create(workspace=self.workspace, name="Launch attendees")
        upload = BytesIO(content.encode("utf-8"))
        upload.name = "guests.csv"
        self.client.post(
            reverse("contacts:audience_import", args=[audience.pk]), {"file": upload}
        )
        return audience

    def test_a_csv_creates_contacts_and_membership(self):
        audience = self._upload(
            "name,mobile\nThabo Mokoena,072 612 4698\nAnn Botha,+27 83 123 4567\n"
        )
        self.assertEqual(audience.contacts.count(), 2)
        thabo = Contact.objects.get(wa_id="27726124698")
        self.assertEqual(thabo.display_name, "Thabo Mokoena")

    def test_reimporting_never_duplicates(self):
        content = "first_name,last_name,phone\nThabo,Mokoena,0726124698\n"
        audience = self._upload(content)
        upload = BytesIO(content.encode("utf-8"))
        upload.name = "guests.csv"
        self.client.post(
            reverse("contacts:audience_import", args=[audience.pk]), {"file": upload}
        )
        self.assertEqual(Contact.objects.filter(wa_id="27726124698").count(), 1)
        self.assertEqual(audience.contacts.count(), 1)

    def test_rows_without_a_number_are_left_out(self):
        audience = self._upload("name,mobile\nNo Number,\n")
        self.assertEqual(audience.contacts.count(), 0)


@override_settings(OUTBOUND_COMMS_MODE="live", CELERY_TASK_ALWAYS_EAGER=True)
class BulkSendUiTests(AudienceUiBase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.template = MessageTemplate.objects.create(
            workspace=cls.workspace, channel=cls.channel, name="logistics_update",
            language="en", status=MessageTemplate.Status.APPROVED,
            components=[{"type": "BODY", "text": "Hi {{1}}, we are on for tomorrow."}],
        )
        cls.audience = Audience.objects.create(workspace=cls.workspace, name="Launch attendees")
        cls.audience.contacts.add(
            Contact.objects.create(workspace=cls.workspace, wa_id="27726124698",
                                   display_name="Thabo Mokoena")
        )

    def test_the_page_lists_templates_and_audiences(self):
        response = self.client.get(reverse("library:bulk_send"))
        self.assertContains(response, "logistics_update")

    def test_an_agent_may_not_open_the_bulk_send_page(self):
        self.client.force_login(self.agent)
        self.assertEqual(self.client.get(reverse("library:bulk_send")).status_code, 403)

    def test_posting_queues_the_background_send(self):
        with mock.patch(
            "apps.channels_wa.messaging.meta_cloud.MetaCloudChannel.send_template",
            return_value=SendResult(ok=True, wamid="wamid.UI"),
        ) as send:
            response = self.client.post(reverse("library:bulk_send"), {
                "template": self.template.pk,
                "audiences": [self.audience.pk],
            })
        self.assertRedirects(response, reverse("library:bulk_send"))
        send.assert_called_once()
        message = Message.objects.get(kind=Message.Kind.TEMPLATE)
        self.assertEqual(message.payload["values"], ["Thabo"])

    def test_choosing_no_audience_is_a_plain_error_and_sends_nothing(self):
        with mock.patch(
            "apps.channels_wa.messaging.meta_cloud.MetaCloudChannel.send_template"
        ) as send:
            response = self.client.post(reverse("library:bulk_send"), {
                "template": self.template.pk,
            }, follow=True)
        send.assert_not_called()
        self.assertContains(response, "at least one audience")

    def test_a_media_template_demands_a_file_or_link(self):
        self.template.components = [
            {"type": "HEADER", "format": "DOCUMENT"},
            {"type": "BODY", "text": "Hi {{1}}"},
        ]
        self.template.save(update_fields=["components"])
        with mock.patch(
            "apps.channels_wa.messaging.meta_cloud.MetaCloudChannel.send_template"
        ) as send:
            response = self.client.post(reverse("library:bulk_send"), {
                "template": self.template.pk,
                "audiences": [self.audience.pk],
            }, follow=True)
        send.assert_not_called()
        self.assertContains(response, "attach a file or paste an https link")
