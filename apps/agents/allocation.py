"""Give a waiting chat to the right agent.

Deliberately explainable: every decision writes an AssignmentLog row, so
"why did I get this?" always has an answer. Order of preference:

1. the agent who last helped this customer (continuity beats fairness)
2. the least busy available agent
3. nobody - the chat stays in the queue and a supervisor is told
"""

import datetime as dt
import logging

from django.utils import timezone

logger = logging.getLogger(__name__)


def available_agents(workspace, *, required_skills=None):
    from apps.agents.models import AgentProfile

    profiles = (
        AgentProfile.objects.for_workspace(workspace)
        .filter(presence=AgentProfile.Presence.ONLINE, accepts_auto_assignment=True)
        .select_related("user")
    )
    if required_skills:
        profiles = profiles.filter(skills__in=required_skills).distinct()
    return [p for p in profiles if p.has_capacity]


def pick_agent(conversation):
    workspace = conversation.workspace
    if not workspace.is_open():
        return None, "outside business hours"

    candidates = available_agents(workspace)
    if not candidates:
        return None, "no agent available"

    # 1. stickiness - back to whoever last helped this person
    if workspace.sticky_agent_days:
        from apps.inbox.models import Conversation

        since = timezone.now() - dt.timedelta(days=workspace.sticky_agent_days)
        previous = (
            Conversation.objects.for_workspace(workspace)
            .filter(contact=conversation.contact, assigned_to__isnull=False, last_activity_at__gte=since)
            .exclude(pk=conversation.pk)
            .order_by("-last_activity_at")
            .first()
        )
        if previous:
            for candidate in candidates:
                if candidate.user_id == previous.assigned_to_id:
                    return candidate, "same agent as their last chat"

    # 2. least busy, oldest-assigned first as the tie break
    candidates.sort(key=lambda p: (p.open_count, p.last_assigned_at or timezone.datetime.min.replace(tzinfo=dt.timezone.utc)))
    return candidates[0], "least busy available agent"


def auto_assign(conversation):
    """Assign if we can. Returns the AgentProfile chosen, or None."""
    from apps.inbox.models import AssignmentLog, Conversation

    if not conversation.workspace.auto_assign_enabled:
        return None
    if conversation.assigned_to_id or conversation.status not in (
        Conversation.Status.QUEUED,
        Conversation.Status.BOT,
    ):
        return None

    profile, reason = pick_agent(conversation)
    if profile is None:
        logger.info("no agent for conversation=%s reason=%s", conversation.pk, reason)
        return None

    conversation.assigned_to = profile.user
    conversation.assigned_at = timezone.now()
    conversation.status = Conversation.Status.ASSIGNED
    conversation.save(update_fields=["assigned_to", "assigned_at", "status"])

    profile.last_assigned_at = timezone.now()
    profile.save(update_fields=["last_assigned_at"])

    AssignmentLog.objects.create(
        workspace=conversation.workspace,
        conversation=conversation,
        to_user=profile.user,
        reason=AssignmentLog.Reason.AUTO,
        detail=reason,
    )
    return profile
