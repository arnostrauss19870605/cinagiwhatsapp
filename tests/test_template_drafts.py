"""Writing a template in the app, sending it to Meta, and filling its blanks per person."""

from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.channels_wa.messaging.base import SendResult, TransportError
from apps.channels_wa.models import WhatsAppChannel
from apps.contacts.models import Audience, Contact
from apps.inbox.models import Message
from apps.library import drafts as drafting
from apps.library.models import BulkSend, MessageTemplate, TemplateDraft
from apps.library.tasks import run_bulk_send
from apps.workspaces.models import Workspace, WorkspaceMembership

User = get_user_model()
CREATE = "apps.channels_wa.messaging.meta_cloud.MetaCloudChannel.create_template"
UPLOAD = "apps.channels_wa.messaging.meta_cloud.MetaCloudChannel.upload_sample_media"
SEND_TEMPLATE = "apps.channels_wa.messaging.meta_cloud.MetaCloudChannel.send_template"


class DraftLogicTests(TestCase):
    def setUp(self):
        self.workspace = Workspace.objects.create(name="Alpha")
        self.channel = WhatsAppChannel.objects.create(
            workspace=self.workspace, display_name="A", phone_number_id="1", waba_id="w1"
        )

    def _draft(self, **kw):
        fields = dict(
            workspace=self.workspace, channel=self.channel, internal_title="Renewal", name="policy_renewal",
            body="Hi {{first_name}}, policy {{policy_number}} renews on {{renewal_date}}. Questions? Reply here.",
            footer="Reply AGENT any time to talk to a person.",
            buttons=[{"type": "quick_reply", "text": "Renew now"}, {"type": "url", "text": "See details", "url": "https://x.example/p"}],
        )
        fields.update(kw)
        draft = TemplateDraft(**fields)
        draft.variables = drafting.merge_variables(draft.body, [])
        return draft

    def test_placeholders_are_found_in_order_and_given_sensible_defaults(self):
        draft = self._draft()
        keys = [v["key"] for v in draft.variables]
        self.assertEqual(keys, ["first_name", "policy_number", "renewal_date"])
        self.assertEqual(draft.variables[0]["source"], "first_name")
        self.assertEqual(draft.variables[0]["sample"], "Thabo")
        self.assertEqual(draft.variables[1]["source"], "manual")

    def test_named_placeholders_become_positional_for_meta(self):
        draft = self._draft()
        for v in draft.variables:
            v["sample"] = v["sample"] or "x"
        components = drafting.build_components(draft)
        body = next(c for c in components if c["type"] == "BODY")
        self.assertEqual(body["text"], "Hi {{1}}, policy {{2}} renews on {{3}}. Questions? Reply here.")
        self.assertEqual(body["example"]["body_text"][0], ["Thabo", "x", "x"])
        buttons = next(c for c in components if c["type"] == "BUTTONS")["buttons"]
        self.assertEqual(buttons[0], {"type": "QUICK_REPLY", "text": "Renew now"})
        self.assertEqual(buttons[1]["type"], "URL")
        self.assertEqual(components[-2]["type"], "FOOTER")

    def test_validation_speaks_plainly(self):
        draft = self._draft(name="Bad Name!", body="{{first_name}}")
        draft.variables = drafting.merge_variables(draft.body, [])
        problems = drafting.validate(draft)
        self.assertTrue(any("lowercase" in p for p in problems))
        self.assertTrue(any("start or end" in p for p in problems))
        draft = self._draft(buttons=[{"type": "quick_reply", "text": "This label is far too long for a button"}])
        self.assertTrue(any("over 25 characters" in p for p in drafting.validate(draft)))
        draft = self._draft(header_type="document")
        self.assertTrue(any("sample file" in p for p in drafting.validate(draft)))

    def test_values_are_filled_per_person_and_a_missing_required_one_stops_the_send(self):
        draft = self._draft()
        draft.variables[1]["source"] = "attribute"
        draft.variables[1]["attribute"] = "policy"
        draft.variables[2]["source"] = "manual"
        draft.save()
        template = MessageTemplate.objects.create(
            workspace=self.workspace, channel=self.channel, name="policy_renewal", language="en",
            status=MessageTemplate.Status.APPROVED, components=drafting.build_components(draft),
        )
        draft.template = template
        draft.save()
        thabo = Contact.objects.create(workspace=self.workspace, wa_id="27820000001", display_name="Thabo Mokoena", attributes={"policy": "POL-42"})
        self.assertEqual(drafting.values_for(template, thabo, {"renewal_date": "1 October"}), ["Thabo", "POL-42", "1 October"])
        blank = Contact.objects.create(workspace=self.workspace, wa_id="27820000002", display_name="Ann")
        with self.assertRaises(ValueError):
            drafting.values_for(template, blank, {"renewal_date": "1 October"})
        draft.variables[1]["fallback"] = "your policy"
        draft.save()
        self.assertEqual(drafting.values_for(template, blank, {"renewal_date": "1 October"})[1], "your policy")

    def test_a_synced_template_keeps_the_first_name_plus_positional_rule(self):
        template = MessageTemplate.objects.create(
            workspace=self.workspace, channel=self.channel, name="legacy", language="en",
            components=[{"type": "BODY", "text": "Hi {{1}}, see you on {{2}}."}],
        )
        thabo = Contact.objects.create(workspace=self.workspace, wa_id="27820000001", display_name="Thabo Mokoena")
        self.assertIsNone(template.variable_map)
        self.assertEqual(drafting.values_for(template, thabo, ["Wednesday"]), ["Thabo", "Wednesday"])

    def test_a_new_version_gets_a_new_name_and_keeps_the_original(self):
        draft = self._draft(status=TemplateDraft.Status.SUBMITTED)
        draft.save()
        copy = drafting.clone(draft)
        self.assertEqual((copy.name, copy.version, copy.parent, copy.status), ("policy_renewal_v2", 2, draft, TemplateDraft.Status.DRAFT))
        self.assertEqual(drafting.clone(copy).name, "policy_renewal_v3")
        draft.refresh_from_db()
        self.assertEqual(draft.status, TemplateDraft.Status.SUBMITTED)


@override_settings(OUTBOUND_COMMS_MODE="live", WHATSAPP_APP_ID="app-1", CELERY_TASK_ALWAYS_EAGER=True)
class DraftPageTests(TestCase):
    def setUp(self):
        self.workspace = Workspace.objects.create(name="Cinagi Broker Support")
        self.channel = WhatsAppChannel.objects.create(
            workspace=self.workspace, display_name="Broker Support", phone_number_id="123", waba_id="waba-1", is_active=True
        )
        self.owner = User.objects.create_user("arno@cinagi.co.za", "arno@cinagi.co.za", first_name="Arno")
        WorkspaceMembership.objects.create(user=self.owner, workspace=self.workspace, role="owner")
        self.client.force_login(self.owner)

    def _post(self, pk=None, action="save", **overrides):
        data = {
            "internal_title": "Exercise question", "name": "quiz_exercise", "language": "en", "category": "MARKETING",
            "channel": self.channel.pk, "header_type": "none", "header_text": "",
            "body": "Hi {{first_name}}, how often do you exercise each week?", "footer": "Reply AGENT any time.",
            "button_type": ["quick_reply", "quick_reply", "quick_reply"],
            "button_text": ["Never", "1-2 times", "3+ times"], "button_url": ["", "", ""], "button_phone": ["", "", ""],
            "action": action,
        }
        data.update(overrides)
        url = reverse("library:draft_edit", args=[pk]) if pk else reverse("library:draft_create")
        return self.client.post(url, data)

    def test_saving_a_draft_derives_its_placeholders_and_shows_a_preview(self):
        response = self._post()
        draft = TemplateDraft.objects.get()
        self.assertRedirects(response, reverse("library:draft_edit", args=[draft.pk]))
        self.assertEqual([v["key"] for v in draft.variables], ["first_name"])
        self.assertEqual(len(draft.buttons), 3)
        page = self.client.get(reverse("library:draft_edit", args=[draft.pk]))
        self.assertContains(page, "Send to Meta for approval")
        self.assertContains(page, "Never")
        self.assertEqual(page.context["sample_preview"], "Hi Thabo, how often do you exercise each week?")

    def test_submitting_sends_positional_components_and_creates_the_library_row(self):
        self._post()
        draft = TemplateDraft.objects.get()
        with mock.patch(CREATE, return_value={"id": "555", "status": "PENDING", "category": "MARKETING"}) as create:
            response = self._post(draft.pk, action="submit", **{"var_first_name_present": "1", "var_first_name_label": "First name", "var_first_name_source": "first_name", "var_first_name_sample": "Thabo", "var_first_name_required": "on"})
        self.assertRedirects(response, reverse("library:templates"))
        payload = create.call_args.args[0]
        self.assertEqual(payload["name"], "quiz_exercise")
        self.assertEqual(payload["components"][0]["text"], "Hi {{1}}, how often do you exercise each week?")
        draft.refresh_from_db()
        self.assertEqual((draft.status, draft.meta_template_id), (TemplateDraft.Status.SUBMITTED, "555"))
        template = MessageTemplate.objects.get(name="quiz_exercise")
        self.assertEqual((template.status, template.meta_id, draft.template), (MessageTemplate.Status.PENDING, "555", template))
        self.assertEqual(template.quick_reply_labels, ["Never", "1-2 times", "3+ times"])
        self.assertFalse(draft.editable)
        self.assertEqual(template.variable_map[0]["key"], "first_name")

    def test_a_media_header_uploads_its_sample_first(self):
        self._post(header_type="document", header_sample=SimpleUploadedFile("agenda.pdf", b"%PDF-1.4 test", content_type="application/pdf"))
        draft = TemplateDraft.objects.get()
        self.assertTrue(draft.header_sample)
        with mock.patch(UPLOAD, return_value="4:handle") as upload, mock.patch(CREATE, return_value={"id": "556", "status": "PENDING"}) as create:
            self._post(draft.pk, action="submit", header_type="document", **{"var_first_name_present": "1", "var_first_name_source": "first_name", "var_first_name_sample": "Thabo", "var_first_name_required": "on"})
        upload.assert_called_once()
        self.assertEqual(upload.call_args.args[1:], ("app-1", "application/pdf"))
        header = create.call_args.args[0]["components"][0]
        self.assertEqual(header, {"type": "HEADER", "format": "DOCUMENT", "example": {"header_handle": ["4:handle"]}})

    def test_metas_refusal_is_shown_and_the_draft_stays_editable(self):
        self._post()
        draft = TemplateDraft.objects.get()
        with mock.patch(CREATE, side_effect=TransportError("nope", friendly="That name is already taken.", body={"error": {"code": 100}})):
            self._post(draft.pk, action="submit", **{"var_first_name_present": "1", "var_first_name_source": "first_name", "var_first_name_sample": "Thabo", "var_first_name_required": "on"})
        draft.refresh_from_db()
        self.assertEqual(draft.status, TemplateDraft.Status.ERROR)
        self.assertEqual(draft.rejection_reason, "That name is already taken.")
        self.assertTrue(draft.editable)
        page = self.client.get(reverse("library:draft_edit", args=[draft.pk]))
        self.assertContains(page, "That name is already taken.")

    def test_a_submitted_draft_is_read_only_until_a_new_version(self):
        self._post()
        draft = TemplateDraft.objects.get()
        with mock.patch(CREATE, return_value={"id": "557", "status": "PENDING"}):
            self._post(draft.pk, action="submit", **{"var_first_name_present": "1", "var_first_name_source": "first_name", "var_first_name_sample": "Thabo", "var_first_name_required": "on"})
        self._post(draft.pk, body="Changed text {{first_name}}")
        draft.refresh_from_db()
        self.assertEqual(draft.body, "Hi {{first_name}}, how often do you exercise each week?")
        response = self.client.post(reverse("library:draft_clone", args=[draft.pk]))
        copy = TemplateDraft.objects.get(version=2)
        self.assertRedirects(response, reverse("library:draft_edit", args=[copy.pk]))
        self.assertEqual(copy.name, "quiz_exercise_v2")

    def test_the_templates_page_lists_drafts(self):
        self._post()
        page = self.client.get(reverse("library:templates"))
        self.assertContains(page, "Written here")
        self.assertContains(page, "Exercise question")
        self.assertContains(page, "New template")

    def test_bulk_send_fills_named_blanks_per_person(self):
        self._post(body="Hi {{first_name}}, your renewal is on {{renewal_date}}.", button_type=[], button_text=[], button_url=[], button_phone=[])
        draft = TemplateDraft.objects.get()
        with mock.patch(CREATE, return_value={"id": "558", "status": "APPROVED"}):
            self._post(
                draft.pk, action="submit", body="Hi {{first_name}}, your renewal is on {{renewal_date}}.",
                button_type=[], button_text=[], button_url=[], button_phone=[],
                **{
                    "var_first_name_present": "1", "var_first_name_source": "first_name", "var_first_name_sample": "Thabo", "var_first_name_required": "on",
                    "var_renewal_date_present": "1", "var_renewal_date_label": "Renewal date", "var_renewal_date_source": "manual", "var_renewal_date_sample": "1 October", "var_renewal_date_required": "on",
                },
            )
        template = MessageTemplate.objects.get(name="quiz_exercise")
        self.assertEqual(template.status, MessageTemplate.Status.APPROVED)
        thabo = Contact.objects.create(workspace=self.workspace, wa_id="27726124698", display_name="Thabo Mokoena")
        ann = Contact.objects.create(workspace=self.workspace, wa_id="27831234567", display_name="Ann Botha")
        audience = Audience.objects.create(workspace=self.workspace, name="Renewals")
        audience.contacts.set([thabo, ann])

        page = self.client.get(reverse("library:bulk_send") + f"?template={template.pk}")
        self.assertContains(page, "Renewal date")
        self.assertNotContains(page, "Value for")
        with mock.patch(SEND_TEMPLATE, return_value=SendResult(ok=True, wamid="wamid.X")) as send, mock.patch(
            "apps.library.tasks.run_bulk_send.delay", side_effect=lambda *a: run_bulk_send(*a)
        ):
            response = self.client.post(
                reverse("library:bulk_send"),
                {"template": template.pk, "audiences": [audience.pk], "value_renewal_date": "1 October"},
            )
        batch = BulkSend.objects.get()
        self.assertRedirects(response, reverse("reporting:bulk_send", args=[batch.pk]))
        self.assertEqual(batch.values, {"renewal_date": "1 October"})
        self.assertEqual(send.call_count, 2)
        bodies = sorted(m.body for m in Message.objects.filter(kind=Message.Kind.TEMPLATE))
        self.assertEqual(bodies, ["Hi Ann, your renewal is on 1 October.", "Hi Thabo, your renewal is on 1 October."])
        detail = self.client.get(reverse("reporting:bulk_send", args=[batch.pk]))
        self.assertContains(detail, "renewal_date: 1 October")
