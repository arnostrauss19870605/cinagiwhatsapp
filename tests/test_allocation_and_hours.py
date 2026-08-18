"""Work hours and who gets the next chat."""

import datetime as dt

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from apps.agents.allocation import auto_assign
from apps.agents.models import AgentProfile
from apps.channels_wa.models import WhatsAppChannel
from apps.contacts.models import Contact
from apps.inbox.models import AssignmentLog, Conversation
from apps.workspaces.models import BusinessHours, Holiday, Workspace, WorkspaceMembership

User = get_user_model()


class BusinessHoursTests(TestCase):
    def setUp(self):
        self.workspace = Workspace.objects.create(name="Alpha", timezone="Africa/Johannesburg")

    def test_open_when_no_schedule_is_configured(self):
        self.assertTrue(self.workspace.is_open())

    def test_closed_on_a_day_marked_closed(self):
        for weekday in range(7):
            BusinessHours.objects.create(workspace=self.workspace, weekday=weekday, is_closed=True)
        self.assertFalse(self.workspace.is_open())

    def test_closed_on_a_holiday(self):
        for weekday in range(7):
            BusinessHours.objects.create(
                workspace=self.workspace,
                weekday=weekday,
                opens_at=dt.time(0, 1),
                closes_at=dt.time(23, 59),
            )
        today = timezone.now().astimezone(self.workspace.tz).date()
        Holiday.objects.create(workspace=self.workspace, date=today, name="Test holiday")
        self.assertFalse(self.workspace.is_open())


class AllocationTests(TestCase):
    def setUp(self):
        self.workspace = Workspace.objects.create(name="Alpha")
        self.channel = WhatsAppChannel.objects.create(
            workspace=self.workspace, display_name="S", phone_number_id="1"
        )
        self.contact = Contact.objects.create(workspace=self.workspace, wa_id="27820000001")

    def _agent(self, username, presence="online", max_concurrent=5, accepts=True):
        user = User.objects.create_user(username, f"{username}@example.com", "pw12345678")
        WorkspaceMembership.objects.create(user=user, workspace=self.workspace, role="agent")
        return AgentProfile.objects.create(
            workspace=self.workspace,
            user=user,
            presence=presence,
            max_concurrent=max_concurrent,
            accepts_auto_assignment=accepts,
        )

    def _conversation(self):
        return Conversation.objects.create(
            workspace=self.workspace, channel=self.channel, contact=self.contact
        )

    def test_an_available_agent_gets_the_chat(self):
        profile = self._agent("thandi")
        conversation = self._conversation()
        self.assertEqual(auto_assign(conversation), profile)
        conversation.refresh_from_db()
        self.assertEqual(conversation.assigned_to, profile.user)
        self.assertEqual(conversation.status, Conversation.Status.ASSIGNED)
        self.assertTrue(AssignmentLog.objects.filter(conversation=conversation).exists())

    def test_offline_agents_are_skipped(self):
        self._agent("offline_one", presence="offline")
        conversation = self._conversation()
        self.assertIsNone(auto_assign(conversation))
        conversation.refresh_from_db()
        self.assertIsNone(conversation.assigned_to)

    def test_agents_at_capacity_are_skipped(self):
        profile = self._agent("busy", max_concurrent=1)
        Conversation.objects.create(
            workspace=self.workspace,
            channel=self.channel,
            contact=self.contact,
            assigned_to=profile.user,
            status=Conversation.Status.ASSIGNED,
        )
        self.assertIsNone(auto_assign(self._conversation()))

    def test_agents_who_opted_out_are_skipped(self):
        self._agent("no_auto", accepts=False)
        self.assertIsNone(auto_assign(self._conversation()))

    def test_the_least_busy_agent_wins(self):
        busy = self._agent("busy_one")
        quiet = self._agent("quiet_one")
        someone_else = Contact.objects.create(workspace=self.workspace, wa_id="27820000099")
        Conversation.objects.create(
            workspace=self.workspace,
            channel=self.channel,
            contact=someone_else,
            assigned_to=busy.user,
            status=Conversation.Status.ASSIGNED,
        )
        self.assertEqual(auto_assign(self._conversation()), quiet)

    def test_returning_customers_go_back_to_the_same_agent(self):
        first = self._agent("first_agent")
        self._agent("second_agent")
        Conversation.objects.create(
            workspace=self.workspace,
            channel=self.channel,
            contact=self.contact,
            assigned_to=first.user,
            status=Conversation.Status.RESOLVED,
            last_activity_at=timezone.now(),
        )
        # second_agent has fewer open chats, but continuity wins
        self.assertEqual(auto_assign(self._conversation()), first)

    def test_nothing_is_assigned_when_auto_assign_is_off(self):
        self._agent("someone")
        self.workspace.auto_assign_enabled = False
        self.workspace.save()
        self.assertIsNone(auto_assign(self._conversation()))
