import secrets

from django.db import models

from apps.core.fields import EncryptedTextField
from apps.core.models import TimeStampedModel
from apps.core.scoping import WorkspaceScopedModel


class WhatsAppChannel(WorkspaceScopedModel, TimeStampedModel):
    """One WhatsApp number, with its own Meta credentials.

    Credentials live here rather than in environment variables - that single
    choice is what lets one deployment serve many numbers.
    """

    class Status(models.TextChoices):
        DRAFT = "draft", "Not connected yet"
        CONNECTED = "connected", "Connected"
        ERROR = "error", "Needs attention"

    display_name = models.CharField(
        max_length=120, help_text="What your team calls this number, e.g. Claims Support"
    )
    phone_number = models.CharField(
        max_length=32, blank=True, help_text="The number customers message, e.g. +27 11 123 4567"
    )
    phone_number_id = models.CharField(
        max_length=64,
        unique=True,
        help_text="Phone number ID - copy it from WhatsApp Manager > API Setup.",
    )
    waba_id = models.CharField(
        max_length=64, blank=True, help_text="WhatsApp Business Account ID (for templates)."
    )
    access_token = EncryptedTextField(
        blank=True, help_text="Permanent system user token from Meta. Stored encrypted."
    )
    app_secret = EncryptedTextField(
        blank=True, help_text="Meta app secret - used to prove webhooks really came from Meta."
    )
    verify_token = models.CharField(max_length=64, blank=True)
    graph_version = models.CharField(max_length=10, blank=True)

    is_default = models.BooleanField(default=True)
    is_active = models.BooleanField(default=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    status_detail = models.CharField(max_length=255, blank=True)
    quality_rating = models.CharField(max_length=20, blank=True)
    messaging_limit = models.CharField(max_length=40, blank=True)
    last_verified_at = models.DateTimeField(null=True, blank=True)
    last_inbound_at = models.DateTimeField(null=True, blank=True)
    templates_synced_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("display_name",)

    def __str__(self):
        return f"{self.display_name} ({self.phone_number or self.phone_number_id})"

    def save(self, *args, **kwargs):
        if not self.verify_token:
            self.verify_token = secrets.token_urlsafe(24)
        super().save(*args, **kwargs)

    @property
    def is_connected(self):
        return self.status == self.Status.CONNECTED

    @property
    def webhook_url(self):
        from django.conf import settings

        return f"{settings.PUBLIC_BASE_URL}/wa/webhook/"

    @property
    def health(self):
        """Plain-language health for the dashboard card."""
        if not self.is_active:
            return ("paused", "This number is switched off.")
        if self.status == self.Status.DRAFT:
            return ("draft", "Not connected yet - finish the setup steps.")
        if self.status == self.Status.ERROR:
            return ("error", self.status_detail or "Something is wrong with this connection.")
        if self.quality_rating and self.quality_rating.upper() in {"RED", "LOW"}:
            return ("warning", "WhatsApp has flagged this number's quality. Send fewer, better messages.")
        return ("ok", "Connected and healthy.")

    def client(self):
        from .messaging import get_channel_client

        return get_channel_client(self)
