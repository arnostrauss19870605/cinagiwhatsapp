"""Credentials must not be readable in the database."""

from django.db import connection
from django.test import TestCase

from apps.channels_wa.models import WhatsAppChannel
from apps.workspaces.models import Workspace


class CredentialEncryptionTests(TestCase):
    def test_access_token_is_encrypted_at_rest_but_readable_in_python(self):
        workspace = Workspace.objects.create(name="Alpha")
        channel = WhatsAppChannel.objects.create(
            workspace=workspace,
            display_name="Support",
            phone_number_id="123",
            access_token="EAAsupersecrettoken",
            app_secret="topsecret",
        )
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT access_token, app_secret FROM channels_wa_whatsappchannel WHERE id = %s",
                [channel.pk],
            )
            stored_token, stored_secret = cursor.fetchone()
        self.assertNotIn("supersecret", stored_token)
        self.assertTrue(stored_token.startswith("enc:"))
        self.assertNotIn("topsecret", stored_secret)

        reloaded = WhatsAppChannel.objects.get(pk=channel.pk)
        self.assertEqual(reloaded.access_token, "EAAsupersecrettoken")
        self.assertEqual(reloaded.app_secret, "topsecret")

    def test_a_verify_token_is_generated_automatically(self):
        workspace = Workspace.objects.create(name="Alpha")
        channel = WhatsAppChannel.objects.create(
            workspace=workspace, display_name="S", phone_number_id="9"
        )
        self.assertTrue(len(channel.verify_token) > 20)
