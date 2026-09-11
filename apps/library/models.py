import re

from django.conf import settings
from django.db import models

from apps.core.models import TimeStampedModel
from apps.core.scoping import WorkspaceScopedModel

PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z0-9_.]+)\s*\}\}")


class MessageTemplate(WorkspaceScopedModel, TimeStampedModel):
    """A template as Meta knows it: the source of truth for what may be sent.

    Rows arrive two ways - synced from WhatsApp Manager, or created when a
    TemplateDraft written in the app is submitted. Either way the components
    here are what Meta approved, in Meta's positional form, and status follows
    Meta's verdicts through the webhook and the poll.
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
    status_changed_at = models.DateTimeField(null=True, blank=True)
    rejection_reason = models.TextField(
        blank=True, help_text="Meta's explanation when a template is rejected, paused or disabled."
    )
    quality = models.CharField(
        max_length=12, blank=True,
        help_text="Meta's quality rating from customer feedback: GREEN, YELLOW or RED.",
    )

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
    def quality_label(self):
        return {"GREEN": "Good", "YELLOW": "Watch", "RED": "Poor"}.get(self.quality, "")

    @property
    def variable_map(self):
        """Named variables from the draft this was authored from, or None if synced from Manager."""
        draft = self.drafts.exclude(variables=[]).order_by("-version", "-pk").first()
        return draft.variables if draft else None

    @property
    def manual_variables(self):
        return [v for v in (self.variable_map or []) if v.get("source") == "manual"]

    @property
    def quick_reply_labels(self):
        """The quick-reply button texts, in order, or [] for a template without any."""
        for component in self.components or []:
            if component.get("type", "").upper() == "BUTTONS":
                return [
                    b.get("text", "") for b in component.get("buttons", [])
                    if b.get("type", "").upper() == "QUICK_REPLY"
                ]
        return []

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

    @property
    def header_format(self):
        """'IMAGE', 'DOCUMENT', 'VIDEO', 'TEXT' or '' - what the header needs."""
        for component in self.components or []:
            if component.get("type", "").upper() == "HEADER":
                return component.get("format", "TEXT").upper()
        return ""

    def preview(self, values=None):
        """Render the template body with the supplied values, for the UI."""
        values = values or []
        text = self.body_text
        for index, value in enumerate(values, start=1):
            text = text.replace("{{%d}}" % index, str(value))
        return text

    def build_components(self, values, header_media=None):
        """Turn a flat list of values into the Graph API components payload.

        A template with a media header refuses to send unless the header
        parameter is supplied, so `header_media` is required for those:
        {"link": url} or {"id": media_id}, plus "filename" for a document.
        """
        components = []
        if header_media:
            kind = self.header_format.lower() or "document"
            media = {}
            if header_media.get("id"):
                media["id"] = header_media["id"]
            elif header_media.get("link"):
                media["link"] = header_media["link"]
            if kind == "document" and header_media.get("filename"):
                media["filename"] = header_media["filename"]
            components.append(
                {"type": "header", "parameters": [{"type": kind, kind: media}]}
            )
        values = [v for v in values]
        if values:
            components.append(
                {
                    "type": "body",
                    "parameters": [{"type": "text", "text": str(v)} for v in values],
                }
            )
        return components


def template_sample_path(instance, filename):
    return f"template_samples/{instance.workspace_id}/{filename}"


class TemplateDraft(WorkspaceScopedModel, TimeStampedModel):
    """A template written in the app, on its way to Meta.

    Editable until it is submitted. After that it is a record of what was
    sent for review, and any change means a new version with a new name,
    because Meta's edit rules (one per day, re-review) make in-place edits
    a trap.
    """

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        SUBMITTING = "submitting", "Submitting"
        SUBMITTED = "submitted", "Submitted to Meta"
        ERROR = "error", "Submission failed"

    class HeaderType(models.TextChoices):
        NONE = "none", "No header"
        TEXT = "text", "Text"
        IMAGE = "image", "Image"
        VIDEO = "video", "Video"
        DOCUMENT = "document", "Document (PDF)"

    channel = models.ForeignKey(
        "channels_wa.WhatsAppChannel", on_delete=models.PROTECT, related_name="template_drafts"
    )
    internal_title = models.CharField(max_length=120, help_text="How the team refers to it.")
    name = models.CharField(max_length=512, help_text="Meta's name: lowercase letters, numbers and underscores.")
    language = models.CharField(max_length=12, default="en")
    category = models.CharField(max_length=20, choices=MessageTemplate.Category.choices, default=MessageTemplate.Category.MARKETING)
    header_type = models.CharField(max_length=10, choices=HeaderType.choices, default=HeaderType.NONE)
    header_text = models.CharField(max_length=60, blank=True)
    header_sample = models.FileField(upload_to=template_sample_path, blank=True, null=True)
    body = models.TextField(help_text="Use {{first_name}} and similar placeholders.")
    footer = models.CharField(max_length=60, blank=True)
    buttons = models.JSONField(default=list, blank=True)
    variables = models.JSONField(default=list, blank=True)
    description = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT)
    template = models.ForeignKey(
        MessageTemplate, null=True, blank=True, on_delete=models.SET_NULL, related_name="drafts"
    )
    meta_template_id = models.CharField(max_length=64, blank=True)
    rejection_reason = models.TextField(blank=True)
    last_request = models.JSONField(default=dict, blank=True)
    last_response = models.JSONField(default=dict, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    version = models.PositiveSmallIntegerField(default=1)
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="versions")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        ordering = ("-updated_at",)
        constraints = [
            models.UniqueConstraint(fields=["channel", "name", "language"], name="uniq_draft_per_channel")
        ]

    def __str__(self):
        return f"{self.internal_title} ({self.name} v{self.version})"

    @property
    def editable(self):
        return self.status in (self.Status.DRAFT, self.Status.ERROR)

    @property
    def meta_status(self):
        """What Meta thinks, once submitted; before that, our own status."""
        if self.template_id:
            return self.template.status
        return ""

    @property
    def status_label(self):
        if self.template_id:
            label = self.template.get_status_display()
            return label
        return self.get_status_display()

    @property
    def problem(self):
        if self.template_id and self.template.rejection_reason:
            return self.template.rejection_reason
        return self.rejection_reason


class BulkSend(WorkspaceScopedModel, TimeStampedModel):
    """One press of the bulk-send button, and what became of it.

    The messages themselves carry the delivery truth (receipts update them for
    days afterwards), so the counts here are what the sender saw at the end of
    the run. Analytics re-derive delivered/read/replied from the messages that
    point back here.
    """

    class Status(models.TextChoices):
        QUEUED = "queued", "Waiting to start"
        RUNNING = "running", "Sending"
        DONE = "done", "Finished"
        DROPPED = "dropped", "Could not start"

    channel = models.ForeignKey(
        "channels_wa.WhatsAppChannel", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="bulk_sends",
    )
    template = models.ForeignKey(
        MessageTemplate, null=True, blank=True, on_delete=models.SET_NULL, related_name="bulk_sends"
    )
    template_name = models.CharField(max_length=200)
    audience_names = models.JSONField(default=list, blank=True)
    values = models.JSONField(default=list, blank=True)
    header_media = models.JSONField(default=dict, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="bulk_sends"
    )
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.QUEUED)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    recipient_count = models.PositiveIntegerField(default=0)
    sent_count = models.PositiveIntegerField(default=0)
    blocked_count = models.PositiveIntegerField(default=0)
    failed_count = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self):
        return f"{self.template_name} to {', '.join(self.audience_names) or 'nobody'}"

    @property
    def audience_label(self):
        return ", ".join(self.audience_names) or "No audience"

    @property
    def values_label(self):
        if isinstance(self.values, dict):
            return " · ".join(f"{k}: {v}" for k, v in self.values.items())
        return " · ".join(str(v) for v in (self.values or []))


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
