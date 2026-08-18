from django.conf import settings
from django.db import models


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class AuditLog(models.Model):
    """One table, every significant thing that happened, per workspace.

    Written through apps.core.audit.audit() - never directly.
    """

    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="audit_entries",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_entries",
    )
    action = models.CharField(max_length=100, db_index=True)
    target_type = models.CharField(max_length=100, blank=True)
    target_id = models.CharField(max_length=100, blank=True)
    detail = models.JSONField(default=dict, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [models.Index(fields=["workspace", "-created_at"])]

    def __str__(self):
        return f"{self.action} @ {self.created_at:%Y-%m-%d %H:%M}"


class UiCopy(models.Model):
    """Wording shown to users, editable without a developer.

    Templates use {% copy "key" "fallback" %}. Changing a label is a settings
    change, not a deployment.
    """

    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="ui_copy",
        help_text="Leave blank to apply to every workspace.",
    )
    key = models.CharField(max_length=120)
    text = models.TextField()

    class Meta:
        verbose_name_plural = "UI copy"
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "key"], name="uniq_uicopy_workspace_key"
            )
        ]

    def __str__(self):
        return self.key


class FeatureToggle(models.Model):
    """Deploy-free kill switches. A missing row means the feature is ON."""

    key = models.CharField(max_length=100, unique=True)
    description = models.CharField(max_length=255, blank=True)
    is_enabled = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.key}={'on' if self.is_enabled else 'off'}"
