import datetime as dt
import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.core.models import TimeStampedModel
from apps.core.scoping import WorkspaceScopedModel

WINDOW = dt.timedelta(hours=24)


def private_media_path(instance, filename):
    return f"conversations/{instance.workspace_id}/{instance.conversation_id}/{filename}"


class Tag(WorkspaceScopedModel):
    name = models.CharField(max_length=60)
    colour = models.CharField(max_length=7, default="#64748b")
    is_system = models.BooleanField(default=False)

    class Meta:
        ordering = ("name",)
        constraints = [
            models.UniqueConstraint(fields=["workspace", "name"], name="uniq_tag_per_workspace")
        ]

    def __str__(self):
        return self.name


class Conversation(WorkspaceScopedModel, TimeStampedModel):
    class Status(models.TextChoices):
        BOT = "bot", "Handled by automation"
        QUEUED = "queued", "Waiting for an agent"
        ASSIGNED = "assigned", "With an agent"
        WAITING = "waiting", "Waiting on the customer"
        RESOLVED = "resolved", "Resolved"
        CLOSED = "closed", "Closed"

    OPEN_STATUSES = [Status.BOT, Status.QUEUED, Status.ASSIGNED, Status.WAITING]

    class Priority(models.IntegerChoices):
        LOW = 0, "Low"
        NORMAL = 1, "Normal"
        HIGH = 2, "High"
        URGENT = 3, "Urgent"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    channel = models.ForeignKey(
        "channels_wa.WhatsAppChannel", on_delete=models.PROTECT, related_name="conversations"
    )
    contact = models.ForeignKey(
        "contacts.Contact", on_delete=models.CASCADE, related_name="conversations"
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.QUEUED)
    priority = models.IntegerField(choices=Priority.choices, default=Priority.NORMAL)
    subject = models.CharField(max_length=200, blank=True)
    tags = models.ManyToManyField(Tag, blank=True, related_name="conversations")

    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="assigned_conversations",
    )
    assigned_at = models.DateTimeField(null=True, blank=True)

    last_inbound_at = models.DateTimeField(null=True, blank=True)
    last_outbound_at = models.DateTimeField(null=True, blank=True)
    last_activity_at = models.DateTimeField(default=timezone.now, db_index=True)
    window_expires_at = models.DateTimeField(
        null=True, blank=True, help_text="When WhatsApp's 24 hour free-reply window closes."
    )
    unread_agent_count = models.PositiveIntegerField(default=0)

    first_response_at = models.DateTimeField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="resolved_conversations",
    )
    auto_resolved = models.BooleanField(default=False)
    handoff_reason = models.CharField(max_length=200, blank=True)
    automation_state = models.JSONField(default=dict, blank=True)
    redacted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-last_activity_at",)
        indexes = [
            models.Index(fields=["workspace", "status", "-last_activity_at"]),
            models.Index(fields=["workspace", "assigned_to", "status"]),
        ]

    def __str__(self):
        return f"{self.contact.name} - {self.get_status_display()}"

    # -- the 24 hour window ------------------------------------------------

    @property
    def window_open(self):
        return bool(self.window_expires_at and self.window_expires_at > timezone.now())

    @property
    def window_remaining(self):
        if not self.window_open:
            return None
        return self.window_expires_at - timezone.now()

    @property
    def window_hint(self):
        """Plain language for the composer - no jargon, no maths for the agent."""
        if self.window_open:
            remaining = self.window_remaining
            hours = int(remaining.total_seconds() // 3600)
            if hours >= 1:
                return f"You can reply freely for another {hours} hour{'s' if hours != 1 else ''}."
            minutes = max(1, int(remaining.total_seconds() // 60))
            return f"You can reply freely for another {minutes} minutes."
        return (
            "This chat has been quiet for more than 24 hours. WhatsApp only allows an "
            "approved template until the customer messages again."
        )

    @property
    def is_open(self):
        return self.status in self.OPEN_STATUSES

    def touch_inbound(self, at=None):
        at = at or timezone.now()
        self.last_inbound_at = at
        self.last_activity_at = at
        self.window_expires_at = at + WINDOW

    def touch_outbound(self, at=None):
        at = at or timezone.now()
        self.last_outbound_at = at
        self.last_activity_at = at
        if self.first_response_at is None:
            self.first_response_at = at


class Message(WorkspaceScopedModel):
    class Direction(models.TextChoices):
        IN = "in", "From customer"
        OUT = "out", "To customer"
        SYSTEM = "system", "System"

    class Actor(models.TextChoices):
        CONTACT = "contact", "Customer"
        AGENT = "agent", "Agent"
        BOT = "bot", "Automation"
        SYSTEM = "system", "System"

    class Kind(models.TextChoices):
        TEXT = "text", "Text"
        IMAGE = "image", "Image"
        DOCUMENT = "document", "Document"
        VIDEO = "video", "Video"
        AUDIO = "audio", "Voice note"
        STICKER = "sticker", "Sticker"
        LOCATION = "location", "Location"
        CONTACTS = "contacts", "Contact card"
        INTERACTIVE = "interactive", "Menu reply"
        TEMPLATE = "template", "Template"
        UNSUPPORTED = "unsupported", "Unsupported"

    class Status(models.TextChoices):
        QUEUED = "queued", "Sending"
        SENT = "sent", "Sent"
        DELIVERED = "delivered", "Delivered"
        READ = "read", "Read"
        FAILED = "failed", "Failed"
        BLOCKED = "blocked", "Not sent (test mode)"

    STATUS_ORDER = {"queued": 0, "blocked": 0, "sent": 1, "delivered": 2, "read": 3, "failed": 4}

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    conversation = models.ForeignKey(
        Conversation, on_delete=models.CASCADE, related_name="messages"
    )
    direction = models.CharField(max_length=10, choices=Direction.choices)
    actor = models.CharField(max_length=10, choices=Actor.choices, default=Actor.CONTACT)
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="sent_messages",
    )
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.TEXT)
    body = models.TextField(blank=True)
    payload = models.JSONField(default=dict, blank=True)

    media = models.FileField(upload_to=private_media_path, blank=True, null=True)
    media_mime = models.CharField(max_length=100, blank=True)
    media_filename = models.CharField(max_length=255, blank=True)

    template = models.ForeignKey(
        "library.MessageTemplate", null=True, blank=True, on_delete=models.SET_NULL
    )
    snippet = models.ForeignKey(
        "library.QuickSnippet", null=True, blank=True, on_delete=models.SET_NULL
    )

    wamid = models.CharField(max_length=128, blank=True, db_index=True)
    wa_status = models.CharField(max_length=20, choices=Status.choices, default=Status.QUEUED)
    wa_error = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("created_at",)
        indexes = [models.Index(fields=["conversation", "created_at"])]

    def __str__(self):
        return f"{self.direction}:{self.kind}:{self.body[:30]}"

    @property
    def is_inbound(self):
        return self.direction == self.Direction.IN

    @property
    def status_icon(self):
        return {
            "queued": "clock",
            "blocked": "ban",
            "sent": "check",
            "delivered": "check-double",
            "read": "check-double-blue",
            "failed": "alert",
        }.get(self.wa_status, "clock")

    def advance_status(self, new_status, at=None):
        """Status only ever moves forward - a late 'delivered' never undoes 'read'."""
        current = self.STATUS_ORDER.get(self.wa_status, 0)
        incoming = self.STATUS_ORDER.get(new_status, 0)
        if new_status != "failed" and incoming <= current:
            return False
        at = at or timezone.now()
        self.wa_status = new_status
        if new_status == "sent":
            self.sent_at = at
        elif new_status == "delivered":
            self.delivered_at = at
        elif new_status == "read":
            self.read_at = at
        return True


class ProcessedInbound(models.Model):
    """Meta redelivers webhooks. This is how we only act once."""

    workspace = models.ForeignKey(
        "workspaces.Workspace", on_delete=models.CASCADE, related_name="processed_inbound"
    )
    wamid = models.CharField(max_length=128, unique=True)
    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["received_at"])]


class InternalNote(WorkspaceScopedModel):
    """Never sent to the customer - the team's own margin notes."""

    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name="notes")
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="notes"
    )
    body = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("created_at",)


class AssignmentLog(WorkspaceScopedModel):
    """Why did I get this chat? Always answerable."""

    class Reason(models.TextChoices):
        AUTO = "auto", "Given out automatically"
        MANUAL = "manual", "Picked up by an agent"
        REASSIGN = "reassign", "Moved by a supervisor"
        ESCALATION = "escalation", "Escalated"
        TIMEOUT = "timeout", "Returned to the queue (no reply)"
        OFFLINE = "offline", "Returned to the queue (agent went offline)"
        RELEASE = "release", "Released back to the queue"

    conversation = models.ForeignKey(
        Conversation, on_delete=models.CASCADE, related_name="assignment_logs"
    )
    from_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    to_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    reason = models.CharField(max_length=20, choices=Reason.choices)
    detail = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)
