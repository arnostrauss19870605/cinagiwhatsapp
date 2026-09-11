"""Meta's template verdicts: the webhook that carries them and the poll that backs it up."""

import hashlib
import hmac
import json
from unittest import mock

from django.test import TestCase, override_settings
from django.urls import reverse

from apps.channels_wa.models import WhatsAppChannel
from apps.channels_wa.tasks import poll_template_statuses, process_inbound_payload
from apps.library.models import MessageTemplate
from apps.workspaces.models import Workspace

APP_SECRET = "test-app-secret"


def status_payload(event, *, name="event_invite", language="en", reason=None, other_info=None, waba="waba-1"):
    value = {
        "event": event,
        "message_template_id": 987654,
        "message_template_name": name,
        "message_template_language": language,
        "reason": reason,
    }
    if other_info:
        value["other_info"] = other_info
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": waba, "changes": [{"field": "message_template_status_update", "value": value}]}],
    }


def quality_payload(new, previous="GREEN", *, name="event_invite", language="en", waba="waba-1"):
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": waba, "changes": [{
            "field": "message_template_quality_update",
            "value": {
                "previous_quality_score": previous, "new_quality_score": new,
                "message_template_id": 987654, "message_template_name": name,
                "message_template_language": language,
            },
        }]}],
    }


@override_settings(CELERY_TASK_ALWAYS_EAGER=True)
class TemplateStatusWebhookTests(TestCase):
    def setUp(self):
        self.workspace = Workspace.objects.create(name="Alpha")
        self.channel = WhatsAppChannel.objects.create(
            workspace=self.workspace, display_name="Support", phone_number_id="123",
            waba_id="waba-1", app_secret=APP_SECRET, status=WhatsAppChannel.Status.CONNECTED,
        )
        self.template = MessageTemplate.objects.create(
            workspace=self.workspace, channel=self.channel, name="event_invite", language="en",
            status=MessageTemplate.Status.PENDING,
            components=[{"type": "BODY", "text": "Hi {{1}}"}],
        )

    def post(self, payload):
        raw = json.dumps(payload).encode()
        digest = hmac.new(APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()
        with mock.patch("apps.channels_wa.tasks.sync_templates.delay"):
            return self.client.post(
                reverse("channels_wa:webhook"), raw, content_type="application/json",
                HTTP_X_HUB_SIGNATURE_256=f"sha256={digest}",
            )

    def test_an_approval_arrives_through_the_webhook(self):
        response = self.post(status_payload("APPROVED"))
        self.assertEqual(response.status_code, 200)
        self.template.refresh_from_db()
        self.assertEqual(self.template.status, MessageTemplate.Status.APPROVED)
        self.assertEqual(self.template.meta_id, "987654")
        self.assertIsNotNone(self.template.status_changed_at)

    def test_a_rejection_keeps_metas_reason(self):
        self.post(status_payload("REJECTED", reason="INVALID_FORMAT"))
        self.template.refresh_from_db()
        self.assertEqual(self.template.status, MessageTemplate.Status.REJECTED)
        self.assertEqual(self.template.rejection_reason, "INVALID_FORMAT")

    def test_a_pause_explains_itself_and_a_reinstatement_clears_it(self):
        self.template.status = MessageTemplate.Status.APPROVED
        self.template.save()
        self.post(status_payload(
            "PAUSED", other_info={"title": "SECOND_PAUSE", "description": "Paused for 6 hours because of low quality."},
        ))
        self.template.refresh_from_db()
        self.assertEqual(self.template.status, MessageTemplate.Status.PAUSED)
        self.assertIn("low quality", self.template.rejection_reason)

        self.post(status_payload("REINSTATED"))
        self.template.refresh_from_db()
        self.assertEqual(self.template.status, MessageTemplate.Status.APPROVED)
        self.assertEqual(self.template.rejection_reason, "")

    def test_a_quality_drop_is_recorded(self):
        self.post(quality_payload("RED"))
        self.template.refresh_from_db()
        self.assertEqual(self.template.quality, "RED")
        self.assertEqual(self.template.quality_label, "Poor")

    def test_a_template_we_have_never_seen_is_created_and_fetched(self):
        with mock.patch("apps.channels_wa.tasks.sync_templates.delay") as sync:
            raw = json.dumps(status_payload("APPROVED", name="brand_new")).encode()
            digest = hmac.new(APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()
            self.client.post(
                reverse("channels_wa:webhook"), raw, content_type="application/json",
                HTTP_X_HUB_SIGNATURE_256=f"sha256={digest}",
            )
        created = MessageTemplate.objects.get(name="brand_new")
        self.assertEqual(created.status, MessageTemplate.Status.APPROVED)
        self.assertEqual(created.workspace, self.workspace)
        sync.assert_called_once_with(self.channel.pk)

    def test_an_event_for_another_account_is_ignored(self):
        response = self.post(status_payload("APPROVED", waba="someone-else"))
        self.assertEqual(response.status_code, 200)
        self.template.refresh_from_db()
        self.assertEqual(self.template.status, MessageTemplate.Status.PENDING)

    def test_the_same_event_twice_changes_nothing_the_second_time(self):
        self.post(status_payload("APPROVED"))
        self.template.refresh_from_db()
        first = self.template.status_changed_at
        self.post(status_payload("APPROVED"))
        self.template.refresh_from_db()
        self.assertEqual(self.template.status_changed_at, first)


class TemplatePollTests(TestCase):
    def setUp(self):
        self.workspace = Workspace.objects.create(name="Alpha")
        self.channel = WhatsAppChannel.objects.create(
            workspace=self.workspace, display_name="Support", phone_number_id="123",
            waba_id="waba-1", access_token="tok", status=WhatsAppChannel.Status.CONNECTED,
        )
        self.pending = MessageTemplate.objects.create(
            workspace=self.workspace, channel=self.channel, name="event_invite", language="en",
            status=MessageTemplate.Status.PENDING,
        )

    def _api_item(self, status, **extra):
        return {
            "id": "987654", "name": "event_invite", "language": "en", "status": status,
            "category": "MARKETING", "components": [{"type": "BODY", "text": "Hi {{1}}"}],
            **extra,
        }

    def test_the_poll_picks_up_an_approval_the_webhook_missed(self):
        with mock.patch(
            "apps.channels_wa.messaging.meta_cloud.MetaCloudChannel.fetch_templates",
            return_value=[self._api_item("APPROVED", quality_score={"score": "GREEN"})],
        ) as fetch:
            self.assertEqual(poll_template_statuses(), 1)
        fetch.assert_called_once()
        self.pending.refresh_from_db()
        self.assertEqual(self.pending.status, MessageTemplate.Status.APPROVED)
        self.assertEqual(self.pending.quality, "GREEN")
        self.assertEqual(self.pending.components[0]["text"], "Hi {{1}}")

    def test_the_poll_records_a_rejection_reason(self):
        with mock.patch(
            "apps.channels_wa.messaging.meta_cloud.MetaCloudChannel.fetch_templates",
            return_value=[self._api_item("REJECTED", rejected_reason="ABUSIVE_CONTENT")],
        ):
            poll_template_statuses()
        self.pending.refresh_from_db()
        self.assertEqual(self.pending.status, MessageTemplate.Status.REJECTED)
        self.assertEqual(self.pending.rejection_reason, "ABUSIVE_CONTENT")

    def test_nothing_pending_means_no_call_to_meta(self):
        self.pending.status = MessageTemplate.Status.APPROVED
        self.pending.save()
        with mock.patch(
            "apps.channels_wa.messaging.meta_cloud.MetaCloudChannel.fetch_templates"
        ) as fetch:
            self.assertEqual(poll_template_statuses(), 0)
        fetch.assert_not_called()

    def test_the_task_dispatches_template_fields_without_a_phone_number(self):
        with mock.patch("apps.channels_wa.tasks.sync_templates.delay"):
            process_inbound_payload(status_payload("APPROVED"))
        self.pending.refresh_from_db()
        self.assertEqual(self.pending.status, MessageTemplate.Status.APPROVED)
