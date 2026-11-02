import hashlib
import secrets

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.core.models import TimeStampedModel
from apps.core.scoping import WorkspaceScopedModel

KEY_PREFIX = "cwa_"


def _digest(raw):
    return hashlib.sha256(raw.encode()).hexdigest()


class ApiKey(WorkspaceScopedModel, TimeStampedModel):
    """A key another system uses to call our API for one workspace.

    Only a hash is stored: the key is shown once when it is made and can never
    be read back, so a database copy cannot be used to send messages. The key
    alone says which workspace a call is for.
    """

    name = models.CharField(max_length=120, help_text="Who uses it, e.g. 'BIMS policy system'.")
    prefix = models.CharField(max_length=16, help_text="The start of the key, to tell keys apart.")
    key_hash = models.CharField(max_length=64, unique=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self):
        return self.name

    @property
    def is_active(self):
        return self.revoked_at is None

    @classmethod
    def issue(cls, workspace, name, *, user=None):
        """Make a key. Returns (ApiKey, the raw key to show once)."""
        raw = KEY_PREFIX + secrets.token_urlsafe(32)
        key = cls.objects.create(
            workspace=workspace, name=name, prefix=raw[:10], key_hash=_digest(raw), created_by=user
        )
        return key, raw

    @classmethod
    def authenticate(cls, raw):
        if not raw or not raw.startswith(KEY_PREFIX):
            return None
        key = cls.objects.filter(key_hash=_digest(raw), revoked_at__isnull=True).select_related("workspace").first()
        if key is not None:
            cls.objects.filter(pk=key.pk).update(last_used_at=timezone.now())
        return key
