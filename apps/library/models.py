import re

from django.conf import settings
from django.db import models

from apps.core.models import TimeStampedModel
from apps.core.scoping import WorkspaceScopedModel

PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z0-9_.]+)\s*\}\}")


class MessageTemplate(WorkspaceScopedModel, TimeStampedModel):
    """A Meta-approved template. Read only here - authored in WhatsApp Manager.

    We never create or edit templates through this app; we sync what Meta has
    approved and validate before sending. That avoids the whole class of
    "why was my template rejected" support tickets.
    """

    class Category(models.TextChoices):
        UTILITY = "UTILITY", "Utility"
        MARKETING = "MARKETING", "Marketing"
        AUTHENTICATION = "AUTHENTICATION", "Authentication"

    class Status(models.TextChoices):
        APPROVED = "APPROVED", "Approved"
        PENDING = "PENDING", "Waiting for Meta"
        REJECTED = "REJECTED", "Rejected"
        PAUSED = "PAUSED", "Paused"
        DISABLED = "DISABLED", "Disabled"

    channel = models.ForeignKey(
        "channels_wa.WhatsAppChannel", on_delete=models.CASCADE, related_name="templates"
    )
    meta_id = models.CharField(max_length=64, blank=True)
    name = models.CharField(max_length=200)
    language = models.CharField(
        max_length=12, help_text="Must match Business Manager exactly - en_US is not en."
    )
    category = models.CharField(max_length=20, choices=Category.choices, default=Category.UTILITY)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    components = models.JSONField(default=list, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("name",)
        constraints = [
            models.UniqueConstraint(
                fields=["channel", "name", "language"], name="uniq_template_per_channel"
            )
        ]

    def __str__(self):
        return f"{self.name} ({self.language})"

    @property
    def is_usable(self):
        return self.status == self.Status.APPROVED

    @property
    def body_text(self):
        for component in self.components or []:
            if component.get("type", "").upper() == "BODY":
                return component.get("text", "")
        return ""

    @property
    def header_text(self):
        for component in self.components or []:
            if component.get("type", "").upper() == "HEADER":
                return component.get("text", "")
        return ""

    @property
    def variable_count(self):
        return len(set(re.findall(r"\{\{(\d+)\}\}", self.body_text)))

    def preview(self, values=None):
        """Render the template body with the supplied values, for the UI."""
        values = values or []
        text = self.body_text
        for index, value in enumerate(values, start=1):
            text = text.replace("{{%d}}" % index, str(value))
        return text

    def build_components(self, values):
        """Turn a flat list of values into the Graph API components payload."""
        values = [v for v in values]
        if not values:
            return []
        return [
            {
                "type": "body",
                "parameters": [{"type": "text", "text": str(v)} for v in values],
            }
        ]


class QuickSnippet(WorkspaceScopedModel, TimeStampedModel):
    """A saved reply an agent can drop into a chat. Ours, not Meta's.

    Usable any time the 24 hour window is open. Type / in the composer to find
    one. Personal snippets belong to one agent; shared ones to the workspace.
    """

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="snippets",
        help_text="Leave blank to share with the whole team.",
    )
    title = models.CharField(max_length=120)
    shortcut = models.CharField(
        max_length=40, blank=True, help_text="Short word to find it fast, e.g. hours"
    )
    body = models.TextField(help_text="Use {{contact.first_name}} to greet the customer by name.")
    category = models.CharField(max_length=60, blank=True)
    usage_count = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        ordering = ("category", "title")

    def __str__(self):
        return self.title

    def render(self, contact=None, agent=None, workspace=None):
        context = {
            "contact.name": getattr(contact, "name", ""),
            "contact.first_name": (getattr(contact, "name", "") or "").split(" ")[0],
            "contact.number": getattr(contact, "pretty_number", ""),
            "agent.name": getattr(agent, "display_name", ""),
            "workspace.name": getattr(workspace, "name", ""),
        }
        return PLACEHOLDER.sub(lambda m: str(context.get(m.group(1), m.group(0))), self.body)


class KnowledgeArticle(WorkspaceScopedModel, TimeStampedModel):
    """Answers the automation will draw on in phase 6. Written by the team."""

    title = models.CharField(max_length=200)
    body = models.TextField()
    keywords = models.CharField(max_length=255, blank=True)
    is_published = models.BooleanField(default=True)

    class Meta:
        ordering = ("title",)

    def __str__(self):
        return self.title
