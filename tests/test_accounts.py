"""Sign-in by emailed code, the Users page, and per-person rights."""

import datetime as dt
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import LoginCode
from apps.workspaces.models import Workspace, WorkspaceMembership

User = get_user_model()
SEND_CODE = "apps.accounts.views.emails.send_login_code"
SEND_WELCOME = "apps.accounts.views.emails.send_welcome"


@override_settings(LOGIN_EMAIL_DOMAINS=["cinagi.co.za"], DEBUG=False)
class LoginTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            "arno@cinagi.co.za", "arno@cinagi.co.za", first_name="Arno", last_name="Strauss"
        )
        self.workspace = Workspace.objects.create(name="Cinagi Broker Support")
        WorkspaceMembership.objects.create(user=self.user, workspace=self.workspace, role="owner")

    def _request_code(self, email="arno@cinagi.co.za"):
        with mock.patch(SEND_CODE, return_value=True) as send:
            response = self.client.post(reverse("accounts:login"), {"email": email})
        return response, send

    def test_the_login_page_asks_only_for_an_email(self):
        response = self.client.get(reverse("accounts:login"))
        self.assertContains(response, "Email me a code")
        self.assertNotContains(response, 'type="password"')

    def test_a_known_address_gets_a_code_and_can_sign_in(self):
        response, send = self._request_code()
        self.assertRedirects(response, reverse("accounts:login_code"))
        send.assert_called_once()
        user, code = send.call_args.args
        self.assertEqual(user, self.user)
        self.assertRegex(code, r"^\d{6}$")

        response = self.client.post(reverse("accounts:login_code"), {"code": code})
        self.assertRedirects(response, reverse("core:dashboard"))
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.user.pk)
        self.assertIsNotNone(LoginCode.objects.get().used_at)

    def test_an_unknown_address_gets_the_same_answer_and_no_email(self):
        response, send = self._request_code("nobody@cinagi.co.za")
        self.assertRedirects(response, reverse("accounts:login_code"))
        send.assert_not_called()
        self.assertEqual(LoginCode.objects.count(), 0)

    def test_an_address_outside_the_organisation_never_gets_a_code(self):
        outsider = User.objects.create_user("x@gmail.com", "x@gmail.com")
        WorkspaceMembership.objects.create(user=outsider, workspace=self.workspace, role="agent")
        _, send = self._request_code("x@gmail.com")
        send.assert_not_called()

    def test_a_switched_off_user_cannot_get_a_code(self):
        self.user.is_active = False
        self.user.save()
        _, send = self._request_code()
        send.assert_not_called()

    def test_a_wrong_code_is_refused_and_five_misses_retire_it(self):
        _, send = self._request_code()
        code = send.call_args.args[1]
        wrong = "000000" if code != "000000" else "111111"
        for _ in range(4):
            response = self.client.post(reverse("accounts:login_code"), {"code": wrong})
            self.assertContains(response, "not right")
        response = self.client.post(reverse("accounts:login_code"), {"code": wrong})
        self.assertRedirects(response, reverse("accounts:login"))
        # Even the right code is dead now.
        self.client.post(reverse("accounts:login"), {"email": "arno@cinagi.co.za"})
        response = self.client.post(reverse("accounts:login_code"), {"code": code})
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_an_expired_code_is_refused(self):
        _, send = self._request_code()
        code = send.call_args.args[1]
        LoginCode.objects.update(expires_at=timezone.now() - dt.timedelta(minutes=1))
        response = self.client.post(reverse("accounts:login_code"), {"code": code})
        self.assertContains(response, "not right")

    def test_a_new_code_retires_the_previous_one(self):
        _, first = self._request_code()
        _, second = self._request_code()
        old, new = first.call_args.args[1], second.call_args.args[1]
        self.assertEqual(LoginCode.objects.filter(used_at__isnull=True).count(), 1)
        if old != new:
            self.client.post(reverse("accounts:login_code"), {"code": old})
            self.assertNotIn("_auth_user_id", self.client.session)

    def test_too_many_requests_are_throttled(self):
        for _ in range(3):
            self._request_code()
        response, send = self._request_code()
        self.assertRedirects(response, reverse("accounts:login"))
        send.assert_not_called()

    def test_next_is_honoured_but_only_on_this_site(self):
        with mock.patch(SEND_CODE, return_value=True) as send:
            self.client.post(reverse("accounts:login") + "?next=/inbox/", {"email": "arno@cinagi.co.za"})
        response = self.client.post(reverse("accounts:login_code"), {"code": send.call_args.args[1]})
        self.assertRedirects(response, "/inbox/", fetch_redirect_response=False)

        self.client.logout()
        with mock.patch(SEND_CODE, return_value=True) as send:
            self.client.post(reverse("accounts:login") + "?next=https://evil.example/", {"email": "arno@cinagi.co.za"})
        response = self.client.post(reverse("accounts:login_code"), {"code": send.call_args.args[1]})
        self.assertRedirects(response, reverse("core:dashboard"))

    def test_the_code_page_needs_an_email_first(self):
        self.assertRedirects(self.client.get(reverse("accounts:login_code")), reverse("accounts:login"))

    def test_protected_pages_send_you_to_the_email_login(self):
        response = self.client.get(reverse("inbox:inbox"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("accounts:login"), response.url)


@override_settings(LOGIN_EMAIL_DOMAINS=["cinagi.co.za"])
class UserAdminTests(TestCase):
    def setUp(self):
        self.workspace = Workspace.objects.create(name="Cinagi Broker Support")
        self.other = Workspace.objects.create(name="Claims")
        self.owner = User.objects.create_user("arno@cinagi.co.za", "arno@cinagi.co.za", first_name="Arno")
        WorkspaceMembership.objects.create(user=self.owner, workspace=self.workspace, role="owner")
        self.agent = User.objects.create_user("sam@cinagi.co.za", "sam@cinagi.co.za", first_name="Sam")
        WorkspaceMembership.objects.create(user=self.agent, workspace=self.workspace, role="agent")
        self.client.force_login(self.owner)

    def _create(self, **overrides):
        data = {
            "first_name": "Thandi",
            "last_name": "Nkosi",
            "email": "thandi@cinagi.co.za",
            "phone": "",
            f"ws_{self.workspace.pk}_access": "on",
            f"ws_{self.workspace.pk}_role": "supervisor",
            f"ws_{self.workspace.pk}_can_send_bulk": "on",
        }
        data.update(overrides)
        with mock.patch(SEND_WELCOME, return_value=True) as welcome:
            response = self.client.post(reverse("accounts:user_create"), data)
        return response, welcome

    def test_an_administrator_creates_a_user_who_is_emailed_and_linked_to_a_number(self):
        response, welcome = self._create()
        self.assertRedirects(response, reverse("accounts:users"))
        person = User.objects.get(email="thandi@cinagi.co.za")
        self.assertEqual(person.username, "thandi@cinagi.co.za")
        self.assertFalse(person.has_usable_password())
        membership = person.workspace_memberships.get()
        self.assertEqual((membership.workspace, membership.role), (self.workspace, "supervisor"))
        self.assertTrue(membership.can_send_bulk)
        self.assertFalse(membership.can_edit_hours)
        welcome.assert_called_once()
        user, inviter, memberships = welcome.call_args.args
        self.assertEqual((user, inviter, list(memberships)), (person, self.owner, [membership]))

    def test_only_organisation_addresses_can_be_given_a_login(self):
        response, welcome = self._create(email="thandi@gmail.com")
        self.assertContains(response, "Only @cinagi.co.za addresses")
        self.assertFalse(User.objects.filter(email="thandi@gmail.com").exists())
        welcome.assert_not_called()

    def test_a_duplicate_address_is_refused(self):
        response, _ = self._create(email="sam@cinagi.co.za")
        self.assertContains(response, "already has a login")

    def test_an_owner_only_hands_out_access_to_numbers_they_administer(self):
        response = self.client.get(reverse("accounts:user_create"))
        self.assertContains(response, "Cinagi Broker Support")
        self.assertNotContains(response, "Claims")
        # Posting a foreign workspace id is ignored, not honoured.
        self._create(**{f"ws_{self.other.pk}_access": "on", f"ws_{self.other.pk}_role": "admin"})
        person = User.objects.get(email="thandi@cinagi.co.za")
        self.assertEqual(list(person.workspace_memberships.values_list("workspace", flat=True)), [self.workspace.pk])

    def test_editing_changes_role_and_rights_and_can_remove_access(self):
        membership = self.agent.workspace_memberships.get()
        response = self.client.post(
            reverse("accounts:user_edit", args=[self.agent.pk]),
            {
                "first_name": "Sam", "last_name": "Dlamini", "email": "sam@cinagi.co.za", "phone": "",
                f"ws_{self.workspace.pk}_access": "on",
                f"ws_{self.workspace.pk}_role": "admin",
                f"ws_{self.workspace.pk}_can_edit_hours": "on",
            },
        )
        self.assertRedirects(response, reverse("accounts:users"))
        membership.refresh_from_db()
        self.assertEqual((membership.role, membership.can_send_bulk, membership.can_edit_hours), ("admin", False, True))

        self.client.post(
            reverse("accounts:user_edit", args=[self.agent.pk]),
            {"first_name": "Sam", "last_name": "Dlamini", "email": "sam@cinagi.co.za", "phone": ""},
        )
        self.assertFalse(self.agent.workspace_memberships.exists())

    def test_you_cannot_lock_yourself_out(self):
        response = self.client.post(
            reverse("accounts:user_toggle_active", args=[self.owner.pk])
        )
        self.owner.refresh_from_db()
        self.assertTrue(self.owner.is_active)
        self.assertRedirects(response, reverse("accounts:users"))

    def test_switching_someone_off_stops_them_signing_in(self):
        self.client.post(reverse("accounts:user_toggle_active", args=[self.agent.pk]))
        self.agent.refresh_from_db()
        self.assertFalse(self.agent.is_active)
        self.client.logout()
        with mock.patch(SEND_CODE, return_value=True) as send:
            self.client.post(reverse("accounts:login"), {"email": "sam@cinagi.co.za"})
        send.assert_not_called()

    def test_agents_cannot_reach_the_users_pages(self):
        self.client.force_login(self.agent)
        self.assertEqual(self.client.get(reverse("accounts:users")).status_code, 403)
        self.assertEqual(self.client.get(reverse("accounts:user_create")).status_code, 403)

    def test_resend_sends_the_welcome_again(self):
        with mock.patch(SEND_WELCOME, return_value=True) as welcome:
            self.client.post(reverse("accounts:user_resend_welcome", args=[self.agent.pk]))
        welcome.assert_called_once()


class ComposerPreferenceTests(TestCase):
    def setUp(self):
        self.workspace = Workspace.objects.create(name="Cinagi Broker Support")
        self.user = User.objects.create_user("sam@cinagi.co.za", "sam@cinagi.co.za")
        WorkspaceMembership.objects.create(user=self.user, workspace=self.workspace, role="agent")
        self.client.force_login(self.user)

    def test_send_on_enter_is_on_by_default_and_can_be_switched_off(self):
        self.assertTrue(self.user.send_on_enter)
        response = self.client.post(reverse("accounts:preferences"), {"next": "/inbox/"})
        self.assertRedirects(response, "/inbox/", fetch_redirect_response=False)
        self.user.refresh_from_db()
        self.assertFalse(self.user.send_on_enter)

        response = self.client.post(
            reverse("accounts:preferences"), {"send_on_enter": "on"}, HTTP_HX_REQUEST="true"
        )
        self.assertEqual(response.status_code, 204)
        self.user.refresh_from_db()
        self.assertTrue(self.user.send_on_enter)

    def test_the_composer_reflects_the_preference(self):
        from apps.channels_wa.models import WhatsAppChannel
        from apps.contacts.models import Contact
        from apps.inbox.models import Conversation

        channel = WhatsAppChannel.objects.create(workspace=self.workspace, display_name="A", phone_number_id="1")
        contact = Contact.objects.create(workspace=self.workspace, wa_id="27820000001")
        conversation = Conversation.objects.create(
            workspace=self.workspace, channel=channel, contact=contact,
            last_inbound_at=timezone.now(), window_expires_at=timezone.now() + dt.timedelta(hours=24),
        )
        page = self.client.get(reverse("inbox:conversation", args=[conversation.pk]))
        self.assertContains(page, "sendOnEnter: true")
        self.user.send_on_enter = False
        self.user.save()
        page = self.client.get(reverse("inbox:conversation", args=[conversation.pk]))
        self.assertContains(page, "sendOnEnter: false")


class RightsTests(TestCase):
    def setUp(self):
        self.workspace = Workspace.objects.create(name="Cinagi Broker Support")
        self.admin = User.objects.create_user("admin@cinagi.co.za", "admin@cinagi.co.za")
        self.membership = WorkspaceMembership.objects.create(
            user=self.admin, workspace=self.workspace, role="admin"
        )
        self.client.force_login(self.admin)

    def test_an_administrator_without_the_right_cannot_send_bulk_or_change_hours(self):
        self.assertEqual(self.client.get(reverse("library:bulk_send")).status_code, 403)
        self.assertEqual(self.client.get(reverse("workspaces:hours")).status_code, 403)
        self.assertEqual(self.client.post(reverse("workspaces:holiday_add"), {}).status_code, 403)
        nav = self.client.get(reverse("core:dashboard"))
        self.assertNotContains(nav, reverse("library:bulk_send"))
        self.assertNotContains(nav, reverse("workspaces:hours"))

    def test_the_rights_open_those_pages(self):
        self.membership.can_send_bulk = True
        self.membership.can_edit_hours = True
        self.membership.save()
        self.assertEqual(self.client.get(reverse("library:bulk_send")).status_code, 200)
        self.assertEqual(self.client.get(reverse("workspaces:hours")).status_code, 200)
        nav = self.client.get(reverse("core:dashboard"))
        self.assertContains(nav, reverse("library:bulk_send"))
        self.assertContains(nav, reverse("workspaces:hours"))

    def test_the_owner_always_has_every_right(self):
        self.membership.role = "owner"
        self.membership.save()
        self.assertTrue(self.membership.may_send_bulk)
        self.assertTrue(self.membership.may_edit_hours)
        self.assertEqual(self.client.get(reverse("workspaces:hours")).status_code, 200)

    def test_an_agent_given_the_bulk_right_can_send_and_see_the_result(self):
        self.membership.role = "agent"
        self.membership.can_send_bulk = True
        self.membership.save()
        self.assertEqual(self.client.get(reverse("library:bulk_send")).status_code, 200)
        self.assertEqual(self.client.get(reverse("reporting:overview")).status_code, 403)
