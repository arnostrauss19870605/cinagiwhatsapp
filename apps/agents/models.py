from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.core.models import TimeStampedModel
from apps.core.scoping import WorkspaceScopedModel


class Skill(WorkspaceScopedModel):
    name = models.CharField(max_length=60)

    class Meta:
        ordering = ("name",)
        constraints = [
            models.UniqueConstraint(fields=["workspace", "name"], name="uniq_skill_per_workspace")
        ]

    def __str__(self):
        return self.name


class Team(WorkspaceScopedModel):
    name = models.CharField(max_length=80)
    fallback_team = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        ordering = ("name",)

    def __str__(self):
        return self.name


class AgentProfile(WorkspaceScopedModel, TimeStampedModel):
    """How available an agent is, per workspace."""

    class Presence(models.TextChoices):
        ONLINE = "online", "Available"
        AWAY = "away", "Away"
        BUSY = "busy", "Busy"
        OFFLINE = "offline", "Offline"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="agent_profiles"
    )
    team = models.ForeignKey(Team, null=True, blank=True, on_delete=models.SET_NULL, related_name="agents")
    skills = models.ManyToManyField(Skill, blank=True, related_name="agents")
    presence = models.CharField(max_length=10, choices=Presence.choices, default=Presence.OFFLINE)
    presence_changed_at = models.DateTimeField(default=timezone.now)
    max_concurrent = models.PositiveIntegerField(
        default=5, verbose_name="How many chats at once?"
    )
    accepts_auto_assignment = models.BooleanField(
        default=True, verbose_name="Send me new chats automatically"
    )
    last_assigned_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["workspace", "user"], name="uniq_agent_per_workspace")
        ]

    def __str__(self):
        return f"{self.user} ({self.get_presence_display()})"

    @property
    def open_count(self):
        from apps.inbox.models import Conversation

        return Conversation.objects.filter(
            workspace=self.workspace,
            assigned_to=self.user,
            status__in=[Conversation.Status.ASSIGNED, Conversation.Status.WAITING],
        ).count()

    @property
    def has_capacity(self):
        return self.open_count < self.max_concurrent

    @property
    def is_available(self):
        return (
            self.presence == self.Presence.ONLINE
            and self.accepts_auto_assignment
            and self.has_capacity
        )

    def set_presence(self, presence):
        self.presence = presence
        self.presence_changed_at = timezone.now()
        self.save(update_fields=["presence", "presence_changed_at"])
