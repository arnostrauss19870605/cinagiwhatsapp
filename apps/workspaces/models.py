import datetime as dt
import zoneinfo

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.text import slugify

from apps.core.models import TimeStampedModel


class Workspace(TimeStampedModel):
    """One WhatsApp number's world: its agents, rules, templates and chats.

    Everything a customer sees or an agent touches hangs off a workspace, and
    nothing crosses between workspaces.
    """

    name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=140, unique=True, blank=True)
    description = models.CharField(max_length=255, blank=True)
    timezone = models.CharField(max_length=64, default="Africa/Johannesburg")
    accent_colour = models.CharField(
        max_length=7,
        default="#0f766e",
        help_text="Shown around the inbox so agents always know which number they are on.",
    )
    is_active = models.BooleanField(default=True)
    settings = models.JSONField(default=dict, blank=True)

    # Behaviour that phase 4/5 build on, with safe defaults from day one.
    auto_assign_enabled = models.BooleanField(
        default=True, verbose_name="Automatically give new chats to an available agent"
    )
    queue_timeout_minutes = models.PositiveIntegerField(
        default=15, help_text="Escalate to a supervisor if nobody picks up a chat in this time."
    )
    sticky_agent_days = models.PositiveIntegerField(
        default=7, help_text="Send a returning customer back to the agent who last helped them."
    )
    out_of_hours_message = models.TextField(
        blank=True,
        default="Thanks for your message. Our team is offline right now and will reply when we open.",
    )
    send_out_of_hours_message = models.BooleanField(default=True)

    class Meta:
        ordering = ("name",)

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            base = slugify(self.name)[:120] or "workspace"
            slug, counter = base, 1
            while Workspace.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                counter += 1
                slug = f"{base}-{counter}"
            self.slug = slug
        super().save(*args, **kwargs)

    @property
    def tz(self):
        try:
            return zoneinfo.ZoneInfo(self.timezone)
        except Exception:
            return zoneinfo.ZoneInfo("Africa/Johannesburg")

    @property
    def default_channel(self):
        return self.whatsappchannels.filter(is_active=True).order_by("-is_default", "id").first()

    def local_now(self):
        return timezone.now().astimezone(self.tz)

    def is_open(self, at=None):
        """Are we inside business hours right now (workspace local time)?"""
        moment = (at or timezone.now()).astimezone(self.tz)
        if self.holidays.filter(date=moment.date()).exists():
            return False
        if self.holidays.filter(
            recurring_annually=True, date__month=moment.month, date__day=moment.day
        ).exists():
            return False
        hours = self.business_hours.filter(weekday=moment.weekday()).first()
        if hours is None:
            return True  # no schedule configured yet = always open
        if hours.is_closed:
            return False
        return hours.opens_at <= moment.time() <= hours.closes_at

    def next_open_time(self, at=None):
        moment = (at or timezone.now()).astimezone(self.tz)
        for offset in range(0, 8):
            day = moment + dt.timedelta(days=offset)
            hours = self.business_hours.filter(weekday=day.weekday()).first()
            if hours is None or hours.is_closed:
                continue
            candidate = day.replace(
                hour=hours.opens_at.hour, minute=hours.opens_at.minute, second=0, microsecond=0
            )
            if candidate > moment:
                return candidate
        return None


class WorkspaceMembership(TimeStampedModel):
    class Role(models.TextChoices):
        OWNER = "owner", "Owner"
        ADMIN = "admin", "Administrator"
        SUPERVISOR = "supervisor", "Supervisor"
        AGENT = "agent", "Agent"
        VIEWER = "viewer", "Viewer"

    MANAGE_ROLES = {Role.OWNER, Role.ADMIN}
    SUPERVISE_ROLES = {Role.OWNER, Role.ADMIN, Role.SUPERVISOR}
    REPLY_ROLES = {Role.OWNER, Role.ADMIN, Role.SUPERVISOR, Role.AGENT}

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="workspace_memberships"
    )
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="memberships")
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.AGENT)
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "workspace"], name="uniq_membership")
        ]

    def __str__(self):
        return f"{self.user} in {self.workspace} ({self.role})"

    @property
    def can_manage(self):
        return self.role in self.MANAGE_ROLES

    @property
    def can_supervise(self):
        return self.role in self.SUPERVISE_ROLES

    @property
    def can_reply(self):
        return self.role in self.REPLY_ROLES


class BusinessHours(models.Model):
    WEEKDAYS = [
        (0, "Monday"),
        (1, "Tuesday"),
        (2, "Wednesday"),
        (3, "Thursday"),
        (4, "Friday"),
        (5, "Saturday"),
        (6, "Sunday"),
    ]

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="business_hours")
    weekday = models.PositiveSmallIntegerField(choices=WEEKDAYS)
    opens_at = models.TimeField(default=dt.time(8, 0))
    closes_at = models.TimeField(default=dt.time(17, 0))
    is_closed = models.BooleanField(default=False)

    class Meta:
        ordering = ("weekday",)
        verbose_name_plural = "business hours"
        constraints = [
            models.UniqueConstraint(fields=["workspace", "weekday"], name="uniq_hours_day")
        ]

    def __str__(self):
        return f"{self.get_weekday_display()} {self.opens_at}-{self.closes_at}"


class Holiday(models.Model):
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="holidays")
    date = models.DateField()
    name = models.CharField(max_length=120)
    recurring_annually = models.BooleanField(default=False)

    class Meta:
        ordering = ("date",)

    def __str__(self):
        return f"{self.name} ({self.date})"
