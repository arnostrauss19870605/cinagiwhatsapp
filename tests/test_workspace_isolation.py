"""The highest risk bug class in a shared inbox: seeing another workspace's chats.

If any of these ever fail, stop and fix it before anything else ships.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.channels_wa.models import WhatsAppChannel
from apps.contacts.models import Contact
from apps.inbox.models import Conversation
from apps.library.models import QuickSnippet
from apps.workspaces.models import Workspace, WorkspaceMembership

User = get_user_model()


class WorkspaceIsolationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.ws_a = Workspace.objects.create(name="Alpha")
        cls.ws_b = Workspace.objects.create(name="Bravo")

        cls.user_a = User.objects.create_user("agent_a", "a@example.com", "pw12345678")
        cls.user_b = User.objects.create_user("agent_b", "b@example.com", "pw12345678")
        WorkspaceMembership.objects.create(user=cls.user_a, workspace=cls.ws_a, role="admin")
        WorkspaceMembership.objects.create(user=cls.user_b, workspace=cls.ws_b, role="admin")

        cls.channel_b = WhatsAppChannel.objects.create(
            workspace=cls.ws_b, display_name="B number", phone_number_id="222"
        )
        contact_b = Contact.objects.create(workspace=cls.ws_b, wa_id="27820000002")
        cls.conversation_b = Conversation.objects.create(
            workspace=cls.ws_b, channel=cls.channel_b, contact=contact_b
        )
        cls.snippet_b = QuickSnippet.objects.create(
            workspace=cls.ws_b, title="B only", body="secret"
        )

    def setUp(self):
        self.client.force_login(self.user_a)

    def test_cannot_open_another_workspaces_conversation(self):
        response = self.client.get(reverse("inbox:conversation", args=[self.conversation_b.pk]))
        self.assertEqual(response.status_code, 404)

    def test_cannot_send_into_another_workspaces_conversation(self):
        response = self.client.post(
            reverse("inbox:send", args=[self.conversation_b.pk]), {"body": "hello"}
        )
        self.assertEqual(response.status_code, 404)

    def test_conversation_list_only_shows_own_workspace(self):
        response = self.client.get(reverse("inbox:inbox") + "?view=open")
        self.assertNotContains(response, "27820000002")

    def test_cannot_edit_another_workspaces_channel(self):
        response = self.client.get(reverse("channels_wa:edit", args=[self.channel_b.pk]))
        self.assertEqual(response.status_code, 404)

    def test_cannot_edit_another_workspaces_snippet(self):
        response = self.client.get(reverse("library:snippet_edit", args=[self.snippet_b.pk]))
        self.assertEqual(response.status_code, 404)

    def test_switching_to_a_workspace_you_are_not_in_is_refused(self):
        response = self.client.get(reverse("workspaces:switch", args=[self.ws_b.slug]))
        self.assertRedirects(response, reverse("workspaces:list"))

    def test_denied_access_is_audited(self):
        from apps.core.models import AuditLog

        self.client.get(reverse("inbox:conversation", args=[self.conversation_b.pk]))
        self.assertTrue(AuditLog.objects.filter(action="access.denied").exists())
