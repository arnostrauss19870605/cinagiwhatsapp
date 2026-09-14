"""Deleting a template removes it from Meta and from the library, unless something still needs it."""

from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.channels_wa.messaging.base import TransportError
from apps.channels_wa.models import WhatsAppChannel
from apps.library.models import MessageTemplate, TemplateDraft
from apps.workspaces.models import Workspace, WorkspaceMembership

User = get_user_model()
DELETE = "apps.channels_wa.messaging.meta_cloud.MetaCloudChannel.delete_template"


@override_settings(OUTBOUND_COMMS_MODE="live")
class TemplateDeleteTests(TestCase):
    def setUp(self):
        self.workspace = Workspace.objects.create(name="Alpha")
        self.channel = WhatsAppChannel.objects.create(
            workspace=self.workspace, display_name="A", phone_number_id="1", waba_id="w1", access_token="t"
        )
        self.owner = User.objects.create_user("arno@cinagi.co.za", "arno@cinagi.co.za")
        WorkspaceMembership.objects.create(user=self.owner, workspace=self.workspace, role="owner")
        self.client.force_login(self.owner)
        self.template = MessageTemplate.objects.create(
            workspace=self.workspace, channel=self.channel, name="old_invite", language="en",
            status=MessageTemplate.Status.APPROVED, meta_id="123",
        )
        TemplateDraft.objects.create(
            workspace=self.workspace, channel=self.channel, internal_title="Old", name="old_invite", body="x",
            template=self.template, status=TemplateDraft.Status.SUBMITTED,
        )

    def test_deleting_removes_it_from_meta_and_the_library_with_its_drafts(self):
        with mock.patch(DELETE, return_value={"success": True}) as delete:
            response = self.client.post(reverse("library:template_delete", args=[self.template.pk]))
        self.assertRedirects(response, reverse("library:templates"))
        delete.assert_called_once_with("old_invite", "123")
        self.assertFalse(MessageTemplate.objects.exists())
        self.assertFalse(TemplateDraft.objects.exists())

    def test_a_template_behind_a_question_is_kept(self):
        from apps.questions.models import Question

        Question.objects.create(workspace=self.workspace, template=self.template, title="Q1")
        with mock.patch(DELETE) as delete:
            self.client.post(reverse("library:template_delete", args=[self.template.pk]))
        delete.assert_not_called()
        self.assertTrue(MessageTemplate.objects.filter(pk=self.template.pk).exists())

    def test_metas_refusal_keeps_the_row(self):
        with mock.patch(DELETE, side_effect=TransportError("no", friendly="Permission denied.", body={"error": {"code": 10}})):
            self.client.post(reverse("library:template_delete", args=[self.template.pk]))
        self.assertTrue(MessageTemplate.objects.filter(pk=self.template.pk).exists())

    def test_already_gone_on_meta_is_still_tidied_here(self):
        with mock.patch(DELETE, side_effect=TransportError("no", friendly="Not found.", body={"error": {"code": 100}})):
            self.client.post(reverse("library:template_delete", args=[self.template.pk]))
        self.assertFalse(MessageTemplate.objects.exists())

    def test_the_delete_button_is_only_for_managers(self):
        page = self.client.get(reverse("library:templates"))
        self.assertContains(page, "Yes, delete old_invite")
        agent = User.objects.create_user("sam@cinagi.co.za", "sam@cinagi.co.za")
        WorkspaceMembership.objects.create(user=agent, workspace=self.workspace, role="agent")
        self.client.force_login(agent)
        self.assertNotContains(self.client.get(reverse("library:templates")), "Yes, delete")
        self.assertEqual(self.client.post(reverse("library:template_delete", args=[self.template.pk])).status_code, 403)


@override_settings(OUTBOUND_COMMS_MODE="live")
class WrittenHereDeleteTests(TestCase):
    """The 'Written here' list offers Delete too, and routes each draft the right way."""

    def setUp(self):
        self.workspace = Workspace.objects.create(name="Alpha")
        self.channel = WhatsAppChannel.objects.create(
            workspace=self.workspace, display_name="A", phone_number_id="1", waba_id="w1", access_token="t"
        )
        self.owner = User.objects.create_user("arno@cinagi.co.za", "arno@cinagi.co.za")
        WorkspaceMembership.objects.create(user=self.owner, workspace=self.workspace, role="owner")
        self.client.force_login(self.owner)

    def _draft(self, **kw):
        fields = dict(workspace=self.workspace, channel=self.channel, internal_title="Invite", name="invite", body="x")
        fields.update(kw)
        return TemplateDraft.objects.create(**fields)

    def test_an_unsent_draft_is_deleted_here_only(self):
        draft = self._draft()
        page = self.client.get(reverse("library:templates"))
        self.assertContains(page, reverse("library:draft_delete", args=[draft.pk]))
        with mock.patch(DELETE) as delete:
            self.client.post(reverse("library:draft_delete", args=[draft.pk]))
        delete.assert_not_called()
        self.assertFalse(TemplateDraft.objects.exists())

    def test_a_failed_submission_can_be_deleted(self):
        draft = self._draft(status=TemplateDraft.Status.ERROR)
        self.client.post(reverse("library:draft_delete", args=[draft.pk]))
        self.assertFalse(TemplateDraft.objects.exists())

    def test_a_draft_still_being_sent_waits(self):
        draft = self._draft(status=TemplateDraft.Status.SUBMITTING)
        page = self.client.get(reverse("library:templates"))
        self.assertNotContains(page, "Yes, delete Invite")
        self.client.post(reverse("library:draft_delete", args=[draft.pk]))
        self.assertTrue(TemplateDraft.objects.exists())

    def test_a_draft_on_whatsapp_goes_through_the_template(self):
        template = MessageTemplate.objects.create(
            workspace=self.workspace, channel=self.channel, name="invite", language="en",
            status=MessageTemplate.Status.APPROVED, meta_id="9",
        )
        draft = self._draft(status=TemplateDraft.Status.SUBMITTED, template=template)
        page = self.client.get(reverse("library:templates"))
        self.assertContains(page, reverse("library:template_delete", args=[template.pk]))
        # The draft route refuses and points at the template route.
        self.client.post(reverse("library:draft_delete", args=[draft.pk]))
        self.assertTrue(TemplateDraft.objects.exists())
        with mock.patch(DELETE, return_value={"success": True}):
            self.client.post(reverse("library:template_delete", args=[template.pk]))
        self.assertFalse(TemplateDraft.objects.exists())
        self.assertFalse(MessageTemplate.objects.exists())
