"""Workflows: what the automation says and asks, step by step.

A workflow's steps live in a `graph` (see graph.py for its shape) on a
WorkflowVersion. Like an approved template, a published version is never
edited in place: editing makes a new draft version, and publishing it swaps
which version new chats start on. Chats already part-way through keep the
version they started on, so a change never strands someone mid-menu.
"""

import secrets

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.core.fields import EncryptedTextField
from apps.core.models import TimeStampedModel
from apps.core.scoping import WorkspaceScopedModel


class Workflow(WorkspaceScopedModel, TimeStampedModel):
    class Trigger(models.TextChoices):
        CHAT_OPENED = "chat_opened", "A new chat starts"
        MANUAL = "manual", "Only when another workflow starts it"

    name = models.CharField(max_length=120)
    description = models.CharField(max_length=255, blank=True)
    trigger = models.CharField(max_length=20, choices=Trigger.choices, default=Trigger.CHAT_OPENED)
    is_active = models.BooleanField(
        default=False, help_text="Switched on: new chats are handled by this workflow."
    )
    quiet_after_our_message_hours = models.PositiveSmallIntegerField(
        default=72,
        help_text="Do not greet someone who is replying to a message we sent them within this many hours "
                  "(a template, a bulk send, an API message). 0 greets everyone.",
    )
    published = models.ForeignKey(
        "WorkflowVersion", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    # Where each step sits on the builder's canvas. Kept apart from the
    # versions: moving a box changes nothing for customers, so it needs no
    # draft and no publishing.
    layout = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ("name",)
        constraints = [
            models.UniqueConstraint(fields=["workspace", "name"], name="uniq_workflow_per_workspace")
        ]

    def __str__(self):
        return self.name

    @property
    def draft(self):
        """The newest version, if it has not been published yet."""
        latest = self.versions.order_by("-number").first()
        if latest is not None and latest.published_at is None:
            return latest
        return None

    @property
    def latest(self):
        return self.versions.order_by("-number").first()

    def is_reply_to_us(self, contact, now=None):
        """Did we write to this person recently, other than through a workflow?

        Someone answering a message we sent them (a policy reminder, a
        campaign) is answering that, not starting a new enquiry, so the menu
        would only get in the way.
        """
        import datetime as dt

        from apps.inbox.models import Message

        if not self.quiet_after_our_message_hours:
            return False
        since = (now or timezone.now()) - dt.timedelta(hours=self.quiet_after_our_message_hours)
        return (
            Message.objects.for_workspace(self.workspace)
            .filter(conversation__contact=contact, direction=Message.Direction.OUT, created_at__gte=since)
            .exclude(payload__auto__in=["workflow", "out_of_hours"])
            .exclude(wa_status__in=[Message.Status.FAILED, Message.Status.BLOCKED])
            .exists()
        )

    @classmethod
    def starting_for(cls, workspace):
        """The switched-on workflow that takes new chats, if there is one."""
        return (
            cls.objects.for_workspace(workspace)
            .filter(is_active=True, trigger=cls.Trigger.CHAT_OPENED, published__isnull=False)
            .select_related("published")
            .order_by("pk")
            .first()
        )


class WorkflowVersion(TimeStampedModel):
    workflow = models.ForeignKey(Workflow, on_delete=models.CASCADE, related_name="versions")
    number = models.PositiveIntegerField()
    graph = models.JSONField(default=dict)
    note = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    published_at = models.DateTimeField(null=True, blank=True)
    published_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        ordering = ("-number",)
        constraints = [
            models.UniqueConstraint(fields=["workflow", "number"], name="uniq_workflow_version_number")
        ]

    def __str__(self):
        return f"{self.workflow.name} v{self.number}"

    @property
    def editable(self):
        return self.published_at is None


def workflow_file_path(instance, filename):
    return f"workflow_files/{instance.workspace_id}/{filename}"


class WorkflowFile(WorkspaceScopedModel, TimeStampedModel):
    """A file a workflow sends: a brochure, a how-to-claim PDF.

    Kept on our own storage so a workflow never depends on someone else's link
    staying alive. WhatsApp's copy (media_id) expires after 30 days, so it is
    refreshed before then rather than trusted forever.
    """

    file = models.FileField(upload_to=workflow_file_path)
    filename = models.CharField(max_length=255)
    mime = models.CharField(max_length=100)
    media_id = models.CharField(max_length=128, blank=True)
    media_uploaded_at = models.DateTimeField(null=True, blank=True)
    media_channel_id = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        ordering = ("filename",)

    def __str__(self):
        return self.filename

    @property
    def kind(self):
        if self.mime.startswith("image/"):
            return "image"
        if self.mime.startswith("video/"):
            return "video"
        return "document"


class WebhookConnection(WorkspaceScopedModel, TimeStampedModel):
    """Somewhere a workflow can send details to, with the token that proves it is us.

    The token is stored encrypted and never put in a workflow's steps, so a
    workflow can be exported or shown on screen without giving it away.
    """

    name = models.CharField(max_length=120)
    url = models.URLField(max_length=500, help_text="The https web address that receives the details.")
    token = EncryptedTextField(help_text="Sent with every call so the receiver knows it came from us.")
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ("name",)
        constraints = [
            models.UniqueConstraint(fields=["workspace", "name"], name="uniq_webhook_per_workspace")
        ]

    def __str__(self):
        return self.name

    @staticmethod
    def new_token():
        return secrets.token_urlsafe(32)


class WorkflowRun(WorkspaceScopedModel, TimeStampedModel):
    """One chat's way through one workflow."""

    class Status(models.TextChoices):
        RUNNING = "running", "Running"
        WAITING = "waiting", "Waiting for the customer"
        FINISHED = "finished", "Finished"
        HANDED_OVER = "handed_over", "Handed to an agent"
        STOPPED = "stopped", "Stopped"
        FAILED = "failed", "Stopped by a problem"

    LIVE_STATUSES = [Status.RUNNING, Status.WAITING]

    workflow = models.ForeignKey(Workflow, on_delete=models.CASCADE, related_name="runs")
    version = models.ForeignKey(WorkflowVersion, on_delete=models.PROTECT, related_name="runs")
    conversation = models.ForeignKey(
        "inbox.Conversation", on_delete=models.CASCADE, related_name="workflow_runs"
    )
    contact = models.ForeignKey("contacts.Contact", on_delete=models.CASCADE, related_name="workflow_runs")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.RUNNING)
    current_step = models.CharField(max_length=64, blank=True)
    variables = models.JSONField(default=dict, blank=True)
    jumps = models.JSONField(default=dict, blank=True)
    attempts = models.PositiveIntegerField(default=0)
    steps_taken = models.PositiveIntegerField(default=0)
    waiting_until = models.DateTimeField(null=True, blank=True, db_index=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    end_reason = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [models.Index(fields=["conversation", "status"])]

    def __str__(self):
        return f"{self.workflow.name} for {self.contact}"

    @property
    def is_live(self):
        return self.status in self.LIVE_STATUSES

    def step(self, step_id=None):
        return (self.version.graph.get("steps") or {}).get(step_id or self.current_step)

    def end(self, status, reason=""):
        self.status = status
        self.end_reason = reason[:200]
        self.ended_at = timezone.now()
        self.waiting_until = None


class RunEvent(WorkspaceScopedModel):
    """One line of a run's trail: which step, and what happened there."""

    run = models.ForeignKey(WorkflowRun, on_delete=models.CASCADE, related_name="events")
    at = models.DateTimeField(default=timezone.now)
    step_id = models.CharField(max_length=64, blank=True)
    step_name = models.CharField(max_length=120, blank=True)
    step_type = models.CharField(max_length=32, blank=True)
    outcome = models.CharField(max_length=255)
    detail = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ("at", "pk")

    def __str__(self):
        return f"{self.step_name}: {self.outcome}"


class PhoneTest(WorkspaceScopedModel, TimeStampedModel):
    """A team member's own number, armed to run a draft on their next message.

    This is how a draft is tried on a real phone before anyone else sees it:
    the person messages the WhatsApp number as usual, and that one chat runs
    the draft instead of the live version.
    """

    workflow = models.ForeignKey(Workflow, on_delete=models.CASCADE, related_name="phone_tests")
    version = models.ForeignKey(WorkflowVersion, on_delete=models.CASCADE, related_name="+")
    wa_id = models.CharField(max_length=32)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        ordering = ("-created_at",)

    @classmethod
    def armed_for(cls, workspace, wa_id):
        return (
            cls.objects.for_workspace(workspace)
            .filter(wa_id=wa_id, used_at__isnull=True, expires_at__gt=timezone.now())
            .select_related("workflow", "version")
            .first()
        )
