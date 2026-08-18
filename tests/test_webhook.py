"""Meta's webhook: signatures, verification and never acting twice."""

import hashlib
import hmac
import json

from django.utils import timezone

from django.test import TestCase
from django.urls import reverse

from apps.channels_wa.models import WhatsAppChannel
from apps.inbox.models import Conversation, Message, ProcessedInbound
from apps.workspaces.models import Workspace

APP_SECRET = "test-app-secret"


def payload_for(phone_number_id="123", wamid="wamid.TEST1", text="Hello there"):
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "waba",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "27110000000",
                                "phone_number_id": phone_number_id,
                            },
                            "contacts": [
                                {"profile": {"name": "Thandi"}, "wa_id": "27820000001"}
                            ],
                            "messages": [
                                {
                                    "from": "27820000001",
                                    "id": wamid,
                                    "timestamp": str(int(timezone.now().timestamp())),
                                    "type": "text",
                                    "text": {"body": text},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


class WebhookTests(TestCase):
    def setUp(self):
        self.workspace = Workspace.objects.create(name="Alpha")
        self.channel = WhatsAppChannel.objects.create(
            workspace=self.workspace,
            display_name="Support",
            phone_number_id="123",
            app_secret=APP_SECRET,
            status=WhatsAppChannel.Status.CONNECTED,
        )
        self.url = reverse("channels_wa:webhook")

    def post(self, payload, *, secret=APP_SECRET, sign=True):
        raw = json.dumps(payload).encode()
        headers = {}
        if sign:
            digest = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
            headers["HTTP_X_HUB_SIGNATURE_256"] = f"sha256={digest}"
        return self.client.post(self.url, raw, content_type="application/json", **headers)

    def test_verification_handshake_accepts_the_right_token(self):
        response = self.client.get(
            self.url,
            {
                "hub.mode": "subscribe",
                "hub.verify_token": self.channel.verify_token,
                "hub.challenge": "42",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"42")

    def test_verification_rejects_a_wrong_token(self):
        response = self.client.get(
            self.url,
            {"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "42"},
        )
        self.assertEqual(response.status_code, 403)

    def test_unsigned_payload_is_rejected(self):
        response = self.post(payload_for(), sign=False)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(Message.objects.count(), 0)

    def test_wrongly_signed_payload_is_rejected(self):
        response = self.post(payload_for(), secret="wrong-secret")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(Message.objects.count(), 0)

    def test_signed_payload_creates_a_conversation(self):
        response = self.post(payload_for())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Message.objects.count(), 1)
        conversation = Conversation.objects.get()
        self.assertEqual(conversation.workspace, self.workspace)
        self.assertEqual(conversation.contact.wa_id, "27820000001")
        self.assertEqual(conversation.messages.first().body, "Hello there")
        self.assertTrue(conversation.window_open)

    def test_the_same_message_twice_is_only_stored_once(self):
        self.post(payload_for())
        self.post(payload_for())
        self.assertEqual(Message.objects.count(), 1)
        self.assertEqual(ProcessedInbound.objects.count(), 1)

    def test_unknown_number_is_ignored_quietly(self):
        response = self.post(payload_for(phone_number_id="999"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Message.objects.count(), 0)

    def test_second_message_joins_the_open_conversation(self):
        self.post(payload_for(wamid="wamid.A", text="one"))
        self.post(payload_for(wamid="wamid.B", text="two"))
        self.assertEqual(Conversation.objects.count(), 1)
        self.assertEqual(Message.objects.count(), 2)
