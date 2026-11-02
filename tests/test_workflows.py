"""Workflows: a chat walked through menus, questions and hand-overs.

The engine is driven exactly as production drives it - through inbound
payloads in Meta's shape - so these tests also pin down the order of the
rules: STOP first, a tap on a workflow button next, then asking for a person,
then whatever the workflow is waiting for.
"""

import datetime as dt
import hashlib
import hmac
import json
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.channels_wa.inbound import process_value
from apps.channels_wa.messaging.base import SendResult
from apps.channels_wa.models import WhatsAppChannel
from apps.contacts.models import Contact
from apps.inbox.models import Conversation, Message
from apps.workflows import engine
from apps.workflows.models import RunEvent, WebhookConnection, Workflow, WorkflowRun, WorkflowVersion
from apps.workspaces.models import BusinessHours, Workspace

NUMBER = "27726124698"


def graph(connection_id=None):
    return {
        "start": "hours",
        "steps": {
            "hours": {"type": "office_hours", "name": "Office hours", "config": {},
                      "next": {"open": "welcome", "closed": "closed"}},
            "closed": {"type": "send_message", "name": "Closed", "config": {"text": "We are closed."}, "next": {}},
            "welcome": {"type": "send_message", "name": "Welcome", "config": {"text": "Welcome to Cinagi."},
                        "next": {"next": "menu"}},
            "menu": {"type": "ask_question", "name": "Main menu",
                     "config": {"text": "How can we help?", "answer": "choice",
                                "options": ["Policy", "Claims", "Chat to a Consultant"],
                                "save_to": {"kind": "variable", "name": "Main_Menu"}, "retries": 1},
                     "next": {"answered": "route"}},
            "route": {"type": "branch", "name": "Route", "config": {"rules": [
                {"id": "policy", "name": "Policy", "match": "all", "conditions": [
                    {"source": "variable", "field": "Main_Menu", "operator": "equals", "value": "Policy"}]},
                {"id": "claims", "name": "Claims", "match": "all", "conditions": [
                    {"source": "variable", "field": "Main_Menu", "operator": "equals", "value": "claims "}]},
                {"id": "human", "name": "Consultant", "match": "all", "conditions": [
                    {"source": "variable", "field": "Main_Menu", "operator": "equals",
                     "value": "Chat to a Consultant"}]},
            ]}, "next": {"policy": "id", "claims": "claims_info", "human": "handover", "else": "back"}},
            "id": {"type": "ask_question", "name": "ID number",
                   "config": {"text": "Your ID number?", "answer": "number", "min": 0, "max": 9999999999999,
                              "save_to": {"kind": "contact", "name": "id_number"}},
                   "next": {"answered": "hook", "invalid": "id"}},
            "hook": {"type": "webhook", "name": "Request documents", "config": {"connection_id": connection_id},
                     "next": {"success": "thanks", "failure": "sorry"}},
            "thanks": {"type": "send_message", "name": "Thanks", "config": {"text": "Documents are on their way."},
                       "next": {"next": "done"}},
            "sorry": {"type": "send_message", "name": "Sorry", "config": {"text": "Please try again later."},
                      "next": {}},
            "done": {"type": "close", "name": "Close", "config": {}, "next": {}},
            "claims_info": {"type": "send_message", "name": "Claims info", "config": {"text": "Claim online."},
                            "next": {}},
            "handover": {"type": "assign", "name": "To an agent", "config": {"to": "auto"}, "next": {}},
            "back": {"type": "jump", "name": "Back to menu", "config": {"target": "menu", "max_times": 1},
                     "next": {}},
        },
    }


class WorkflowTestBase(TestCase):
    def setUp(self):
        self.workspace = Workspace.objects.create(name="Cinagi Broker Support")
        self.channel = WhatsAppChannel.objects.create(
            workspace=self.workspace, display_name="Support", phone_number_id="123",
            status=WhatsAppChannel.Status.CONNECTED, is_active=True,
        )
        self.connection = WebhookConnection.objects.create(
            workspace=self.workspace, name="Policy documents", url="https://hooks.example.com/docs",
            token="secret-token",
        )
        self.workflow = Workflow.objects.create(workspace=self.workspace, name="Main", is_active=True)
        self.version = WorkflowVersion.objects.create(
            workflow=self.workflow, number=1, graph=graph(self.connection.pk), published_at=timezone.now()
        )
        self.workflow.published = self.version
        self.workflow.save()
        self.counter = 0

    def inbound(self, text=None, *, reply_id=None, title=None):
        self.counter += 1
        payload = {"from": NUMBER, "id": f"wamid.in{self.counter}", "timestamp": str(int(timezone.now().timestamp()))}
        if reply_id:
            payload.update(type="interactive",
                           interactive={"type": "list_reply", "list_reply": {"id": reply_id, "title": title or ""}})
        else:
            payload.update(type="text", text={"body": text})
        process_value(self.channel, {
            "metadata": {"phone_number_id": "123"},
            "contacts": [{"profile": {"name": "Thabo"}, "wa_id": NUMBER}],
            "messages": [payload],
        })
        return Conversation.objects.get(contact__wa_id=NUMBER, status__in=Conversation.OPEN_STATUSES + ["resolved"])

    def bodies(self):
        return list(
            Message.objects.filter(direction=Message.Direction.OUT).order_by("created_at").values_list("body", flat=True)
        )

    def run_(self):
        return WorkflowRun.objects.latest("created_at")


class StartingTests(WorkflowTestBase):
    def test_a_new_chat_gets_the_welcome_and_the_menu_and_stays_with_the_bot(self):
        conversation = self.inbound("Hi")
        self.assertEqual(self.bodies(), ["Welcome to Cinagi.", "How can we help?"])
        menu = Message.objects.get(body="How can we help?")
        self.assertEqual(menu.kind, Message.Kind.INTERACTIVE)
        self.assertEqual([b["title"] for b in menu.payload["buttons"]], ["Policy", "Claims", "Chat to a Consultant"])
        self.assertEqual(conversation.status, Conversation.Status.BOT)
        self.assertIsNone(conversation.assigned_to_id)
        self.assertEqual(self.run_().status, WorkflowRun.Status.WAITING)

    def test_outside_office_hours_takes_the_closed_path(self):
        BusinessHours.objects.create(
            workspace=self.workspace, weekday=timezone.localtime().weekday(), is_closed=True,
            opens_at=dt.time(8), closes_at=dt.time(17),
        )
        self.inbound("Hi")
        self.assertEqual(self.bodies(), ["We are closed."])
        self.assertEqual(self.run_().status, WorkflowRun.Status.FINISHED)

    def test_without_a_switched_on_workflow_nothing_changes(self):
        self.workflow.is_active = False
        self.workflow.save()
        conversation = self.inbound("Hi")
        self.assertEqual(conversation.status, Conversation.Status.QUEUED)
        self.assertFalse(WorkflowRun.objects.exists())

    def test_another_workspaces_workflow_never_runs(self):
        other = Workspace.objects.create(name="Elsewhere")
        self.workflow.workspace = other
        self.workflow.save()
        self.inbound("Hi")
        self.assertFalse(WorkflowRun.objects.exists())

    def test_a_first_message_asking_for_a_person_skips_the_workflow(self):
        conversation = self.inbound("can I speak to a consultant please")
        self.assertFalse(WorkflowRun.objects.exists())
        self.assertEqual(conversation.handoff_reason, "asked for a person")


class AnsweringTests(WorkflowTestBase):
    def test_a_tap_routes_through_the_branch(self):
        self.inbound("Hi")
        self.inbound(reply_id="wf:menu:1", title="Claims")
        self.assertEqual(self.bodies()[-1], "Claim online.")
        run = self.run_()
        self.assertEqual(run.variables["Main_Menu"], "Claims")
        self.assertEqual(run.status, WorkflowRun.Status.FINISHED)

    def test_a_typed_number_counts(self):
        self.inbound("Hi")
        self.inbound("2")
        self.assertEqual(self.bodies()[-1], "Claim online.")

    def test_typed_words_count_whatever_the_case_and_spacing(self):
        self.inbound("Hi")
        self.inbound("  CLAIMS ")
        self.assertEqual(self.bodies()[-1], "Claim online.")

    def test_tapping_the_consultant_button_is_an_answer_not_an_escalation(self):
        self.inbound("Hi")
        conversation = self.inbound(reply_id="wf:menu:2", title="Chat to a Consultant")
        run = self.run_()
        self.assertEqual(run.status, WorkflowRun.Status.HANDED_OVER)
        self.assertEqual(conversation.status, Conversation.Status.QUEUED)
        self.assertIn("handed this chat to the team", Message.objects.filter(payload__note=True).last().body)
        self.assertNotIn(engine.STATE_KEY, conversation.automation_state)

    def test_a_wrong_answer_is_asked_again_then_handed_over(self):
        self.inbound("Hi")
        self.inbound("banana")
        self.assertEqual(self.bodies()[-2:], [engine.DEFAULT_INVALID, "How can we help?"])
        self.assertEqual(self.run_().status, WorkflowRun.Status.WAITING)
        conversation = self.inbound("still banana")
        self.assertEqual(self.run_().status, WorkflowRun.Status.HANDED_OVER)
        self.assertEqual(conversation.status, Conversation.Status.QUEUED)

    def test_an_invalid_exit_is_followed_when_connected(self):
        self.inbound("Hi")
        self.inbound("Policy")
        self.inbound("not a number")
        self.assertEqual(self.bodies()[-2:], ["Your ID number?", "Your ID number?"])

    def test_a_number_answer_is_saved_on_the_contact(self):
        self.inbound("Hi")
        self.inbound("Policy")
        self.inbound("8001015009087")
        self.assertEqual(Contact.objects.get(wa_id=NUMBER).attributes["id_number"], "8001015009087")


class OverrulingTests(WorkflowTestBase):
    def test_stop_ends_the_workflow(self):
        self.inbound("Hi")
        self.inbound("STOP")
        run = self.run_()
        self.assertEqual(run.status, WorkflowRun.Status.STOPPED)
        self.assertIn("STOP", run.end_reason)
        self.assertIsNotNone(Contact.objects.get(wa_id=NUMBER).opted_out_at)

    def test_asking_for_a_person_mid_menu_ends_it(self):
        self.inbound("Hi")
        conversation = self.inbound("agent")
        self.assertEqual(self.run_().status, WorkflowRun.Status.STOPPED)
        self.assertEqual(conversation.status, Conversation.Status.QUEUED)

    def test_an_agent_replying_ends_it_and_the_next_answer_goes_to_the_agent(self):
        from apps.channels_wa.outbound import send_text

        conversation = self.inbound("Hi")
        send_text(conversation, "Hi Thabo, this is Ann.")
        self.assertEqual(self.run_().status, WorkflowRun.Status.STOPPED)
        before = Message.objects.filter(direction=Message.Direction.OUT).count()
        self.inbound("Claims")
        self.assertEqual(Message.objects.filter(direction=Message.Direction.OUT).count(), before)


class StepTests(WorkflowTestBase):
    def test_going_round_the_loop_too_often_hands_over(self):
        self.version.graph["steps"]["menu"]["config"]["options"].append("Something else")
        self.version.save()
        self.inbound("Hi")
        self.inbound("Something else")  # else -> jump back (1 allowed)
        self.assertEqual(self.bodies()[-1], "How can we help?")
        conversation = self.inbound("Something else")  # jump limit reached
        self.assertEqual(self.run_().status, WorkflowRun.Status.HANDED_OVER)
        self.assertIn("same part of the workflow", conversation.handoff_reason)

    @override_settings(OUTBOUND_COMMS_MODE="live")
    def test_the_webhook_posts_a_signed_body_and_follows_success(self):
        response = mock.Mock(status_code=200)
        response.json.return_value = {"ok": True}
        with mock.patch(
            "apps.channels_wa.messaging.meta_cloud.MetaCloudChannel._post_message",
            return_value=SendResult(ok=True, wamid="wamid.out"),
        ), mock.patch("apps.workflows.webhooks.requests.post", return_value=response) as post:
            self.inbound("Hi")
            self.inbound("Policy")
            conversation = self.inbound("8001015009087")
        post.assert_called_once()
        body = post.call_args.kwargs["data"]
        headers = post.call_args.kwargs["headers"]
        self.assertEqual(headers["Authorization"], "Bearer secret-token")
        expected = "sha256=" + hmac.new(b"secret-token", body, hashlib.sha256).hexdigest()
        self.assertEqual(headers["X-Cinagi-Signature"], expected)
        sent = json.loads(body)
        self.assertEqual(sent["contact"]["details"]["id_number"], "8001015009087")
        self.assertEqual(sent["answers"]["Main_Menu"], "Policy")
        self.assertEqual(self.bodies()[-1], "Documents are on their way.")
        self.assertEqual(conversation.status, Conversation.Status.RESOLVED)

    def test_in_test_mode_the_webhook_is_not_called_and_the_failure_path_runs(self):
        with mock.patch("apps.workflows.webhooks.requests.post") as post:
            self.inbound("Hi")
            self.inbound("Policy")
            self.inbound("8001015009087")
        post.assert_not_called()
        self.assertEqual(self.bodies()[-1], "Please try again later.")

    def test_an_unanswered_question_times_out(self):
        self.inbound("Hi")
        run = self.run_()
        self.assertEqual(engine.expire_waiting(now=run.waiting_until + dt.timedelta(seconds=1)), 1)
        run.refresh_from_db()
        self.assertEqual(run.status, WorkflowRun.Status.FINISHED)
        self.assertIn("no answer", run.end_reason)

    def test_nothing_is_sent_once_the_24_hour_window_has_closed(self):
        conversation = self.inbound("Hi")
        Conversation.objects.filter(pk=conversation.pk).update(window_expires_at=timezone.now() - dt.timedelta(hours=1))
        run = self.run_()
        run.version.graph["steps"]["menu"]["next"]["timeout"] = "claims_info"
        run.version.save()
        before = Message.objects.filter(direction=Message.Direction.OUT).count()
        engine.expire_waiting(now=run.waiting_until + dt.timedelta(seconds=1))
        self.assertEqual(Message.objects.filter(direction=Message.Direction.OUT).count(), before)
        self.assertIn("24 hours", self.run_().end_reason)

    def test_every_step_is_logged(self):
        self.inbound("Hi")
        self.inbound("Claims")
        outcomes = list(RunEvent.objects.values_list("outcome", flat=True))
        self.assertIn("Within office hours", outcomes)
        self.assertIn("Answered: Claims", outcomes)
        self.assertIn("Took 'Claims'", outcomes)


class GraphRuleTests(TestCase):
    def test_choice_presentation_follows_whatsapp_limits(self):
        from apps.workflows.graph import presentation

        self.assertEqual(presentation(["Yes", "No"]), "buttons")
        self.assertEqual(presentation(["More about Cinagi", "I want to join", "Policy & Benefits", "Claims"]), "list")
        self.assertEqual(presentation(["An option label far too long for a list row"]), "numbered")

    def test_problems_are_in_plain_words(self):
        from apps.workflows.graph import problems

        broken = {"start": "a", "steps": {"a": {"type": "send_message", "name": "Hello", "config": {},
                                               "next": {"next": "gone"}}}}
        found = problems(broken)
        self.assertIn("'Hello' has nothing to send.", found)
        self.assertIn("'Hello' leads to a step that no longer exists.", found)


class RespondIoImportTests(TestCase):
    """The real Cinagi export, converted and then run end to end."""

    def setUp(self):
        from pathlib import Path

        self.workspace = Workspace.objects.create(name="Cinagi Broker Support")
        self.channel = WhatsAppChannel.objects.create(
            workspace=self.workspace, display_name="Support", phone_number_id="123",
            status=WhatsAppChannel.Status.CONNECTED, is_active=True,
        )
        path = Path(__file__).parent / "fixtures" / "respondio_main_workflow.json"
        self.export = json.loads(path.read_text(encoding="utf-8"))

    def _import(self):
        from apps.workflows.respondio import import_export

        with self.settings(MEDIA_ROOT=self._media()):
            return import_export(self.workspace, self.export, fetch=lambda url: b"%PDF-1.4 test")

    def _media(self):
        import tempfile

        folder = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, folder, ignore_errors=True)
        return folder

    def test_the_export_becomes_a_draft_with_its_files_and_a_report(self):
        from apps.workflows.graph import problems
        from apps.workflows.models import WorkflowFile

        workflow, notes = self._import()
        draft = workflow.draft
        self.assertEqual(workflow.trigger, Workflow.Trigger.CHAT_OPENED)
        self.assertEqual(problems(draft.graph, workspace=self.workspace), [])
        self.assertEqual(WorkflowFile.objects.count(), 2, "the brochure is used twice but stored once")
        self.assertEqual(draft.graph["steps"][draft.graph["start"]]["type"], "office_hours")
        joined = "\n".join(notes)
        self.assertIn("Only the 'Cinagi' path was kept", joined)
        self.assertIn("never matched", joined)
        self.assertIn("1763630459846122", joined)
        self.assertEqual(len(notes), len(set(notes)))

    def test_the_imported_workflow_runs(self):
        workflow, _ = self._import()
        version = workflow.draft
        version.published_at = timezone.now()
        version.save()
        workflow.published, workflow.is_active = version, True
        workflow.save()

        counter = iter(range(1, 100))

        def say(text):
            process_value(self.channel, {
                "metadata": {"phone_number_id": "123"},
                "contacts": [{"profile": {"name": "Thabo"}, "wa_id": NUMBER}],
                "messages": [{"from": NUMBER, "id": f"wamid.{next(counter)}", "type": "text",
                              "timestamp": str(int(timezone.now().timestamp())), "text": {"body": text}}],
            })

        say("Hello")
        say("Prospective Client")  # the branch respond.io never matched
        say("Claims")
        say("How to Claim")
        bodies = list(Message.objects.filter(direction="out").values_list("body", flat=True))
        self.assertTrue(bodies[0].startswith("Welcome to Cinagi"))
        self.assertIn("Please let us know how we can assist you today", bodies[2])
        self.assertTrue(any(b.startswith("You can submit your claim online") for b in bodies))
        self.assertEqual(
            Message.objects.filter(direction="out", media_filename="Cinagi - How to Claim.pdf").count(), 1
        )
        self.assertEqual(bodies[-1], "Is there something else we can assist with?")


class WorkflowPageBase(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model

        from apps.workspaces.models import WorkspaceMembership

        User = get_user_model()
        self.workspace = Workspace.objects.create(name="Cinagi Broker Support")
        self.manager = User.objects.create_user("manager", "m@example.com", "pw12345678")
        self.agent = User.objects.create_user("agent", "a@example.com", "pw12345678")
        WorkspaceMembership.objects.create(user=self.manager, workspace=self.workspace, role="admin")
        WorkspaceMembership.objects.create(user=self.agent, workspace=self.workspace, role="agent")
        self.client.force_login(self.manager)
        self.workflow = Workflow.objects.create(workspace=self.workspace, name="Main")
        WorkflowVersion.objects.create(workflow=self.workflow, number=1, graph=graph())

    def url(self, name, *args):
        from django.urls import reverse

        return reverse(f"workflows:{name}", args=args)


class WorkflowPageTests(WorkflowPageBase):
    def test_the_pages_render(self):
        for name, args in [("list", []), ("detail", [self.workflow.pk]), ("step_edit", [self.workflow.pk, "menu"]),
                           ("step_edit", [self.workflow.pk, "route"]), ("step_edit", [self.workflow.pk, "hook"]),
                           ("connections", []), ("files", [])]:
            with self.subTest(name=name, args=args):
                self.assertEqual(self.client.get(self.url(name, *args)).status_code, 200)

    def test_agents_may_not_open_workflows(self):
        self.client.force_login(self.agent)
        self.assertEqual(self.client.get(self.url("list")).status_code, 403)
        self.assertEqual(self.client.get(self.url("detail", self.workflow.pk)).status_code, 403)

    def test_another_workspaces_workflow_is_a_404(self):
        other = Workflow.objects.create(workspace=Workspace.objects.create(name="Elsewhere"), name="Theirs")
        self.assertEqual(self.client.get(self.url("detail", other.pk)).status_code, 404)

    def test_publishing_is_refused_until_the_problems_are_fixed(self):
        self.client.post(self.url("publish", self.workflow.pk))
        self.workflow.refresh_from_db()
        self.assertIsNone(self.workflow.published, "the webhook step has no connection yet")

        connection = WebhookConnection.objects.create(workspace=self.workspace, name="Docs",
                                                      url="https://x.example.com", token="t")
        self.client.post(self.url("step_edit", self.workflow.pk, "hook"), {
            "name": "Request documents", "connection_id": connection.pk,
            "next_success": "thanks", "next_failure": "sorry",
        })
        self.client.post(self.url("publish", self.workflow.pk))
        self.workflow.refresh_from_db()
        self.assertEqual(self.workflow.published.number, 1)

    def test_editing_a_published_workflow_makes_a_new_draft(self):
        version = self.workflow.latest
        version.graph["steps"]["hook"]["config"]["connection_id"] = WebhookConnection.objects.create(
            workspace=self.workspace, name="Docs", url="https://x.example.com", token="t").pk
        version.save()
        self.client.post(self.url("publish", self.workflow.pk))
        self.client.post(self.url("step_edit", self.workflow.pk, "welcome"),
                         {"name": "Welcome", "text": "Hello there!", "next_next": "menu"})
        self.workflow.refresh_from_db()
        self.assertEqual(self.workflow.published.graph["steps"]["welcome"]["config"]["text"], "Welcome to Cinagi.")
        self.assertEqual(self.workflow.draft.number, 2)
        self.assertEqual(self.workflow.draft.graph["steps"]["welcome"]["config"]["text"], "Hello there!")

    def test_adding_a_step_slots_it_into_the_path_and_removing_closes_the_gap(self):
        self.client.post(self.url("step_add", self.workflow.pk), {"kind": "send_message", "after": "welcome",
                                                                  "exit": "next"})
        steps = self.workflow.draft.graph["steps"]
        new_id = steps["welcome"]["next"]["next"]
        self.assertEqual(steps[new_id]["next"]["next"], "menu")
        self.client.post(self.url("step_delete", self.workflow.pk, new_id))
        self.assertEqual(self.workflow.draft.graph["steps"]["welcome"]["next"]["next"], "menu")

    def test_branch_rules_are_edited_as_rows(self):
        self.client.post(self.url("step_edit", self.workflow.pk, "route"), {
            "name": "Route", "rule_count": "1",
            "rule-0-id": "", "rule-0-name": "VIP", "rule-0-match": "all", "rule-0-condition_count": "1",
            "rule-0-c0-source": "contact", "rule-0-c0-field": "tier", "rule-0-c0-operator": "equals",
            "rule-0-c0-value": "gold", "next_rule1": "claims_info", "next_else": "back",
        })
        rules = self.workflow.draft.graph["steps"]["route"]["config"]["rules"]
        self.assertEqual(rules, [{"id": "rule1", "name": "VIP", "match": "all", "conditions": [
            {"source": "contact", "field": "tier", "operator": "equals", "value": "gold"}]}])

    def test_switching_on_switches_the_other_greeting_workflow_off(self):
        from apps.workflows.editing import switch_on

        for workflow in (self.workflow, Workflow.objects.create(workspace=self.workspace, name="Other")):
            workflow.published = WorkflowVersion.objects.create(workflow=workflow, number=9, graph=graph(),
                                                                published_at=timezone.now())
            workflow.save()
        switch_on(Workflow.objects.get(name="Other"))
        self.client.post(self.url("toggle", self.workflow.pk), {"on": "1"})
        self.assertEqual(list(Workflow.objects.filter(is_active=True).values_list("name", flat=True)), ["Main"])

    def test_importing_a_respondio_file(self):
        from pathlib import Path

        from django.core.files.uploadedfile import SimpleUploadedFile

        raw = (Path(__file__).parent / "fixtures" / "respondio_main_workflow.json").read_bytes()
        with mock.patch("apps.workflows.respondio._download", side_effect=OSError("offline")),                 mock.patch("requests.get", side_effect=AssertionError("tests never reach the network")):
            response = self.client.post(self.url("import"), {
                "file": SimpleUploadedFile("main.json", raw, content_type="application/json")})
        workflow = Workflow.objects.exclude(pk=self.workflow.pk).get()
        self.assertRedirects(response, self.url("detail", workflow.pk))
        page = self.client.get(self.url("detail", workflow.pk))
        self.assertContains(page, "Notes from the respond.io import")
        self.assertContains(page, "could not be copied from respond.io")

    def test_a_file_that_is_not_an_export_is_refused_in_plain_words(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        response = self.client.post(self.url("import"), {"file": SimpleUploadedFile("x.json", b"{}")}, follow=True)
        self.assertContains(response, "not a respond.io workflow export")

    def test_webhook_tokens_are_stored_encrypted(self):
        from django.db import connection as db

        self.client.post(self.url("connections"), {"name": "Docs", "url": "https://hooks.example.com/x"})
        hook = WebhookConnection.objects.get(name="Docs")
        self.assertGreater(len(hook.token), 20)
        with db.cursor() as cursor:
            cursor.execute("SELECT token FROM workflows_webhookconnection WHERE id = %s", [hook.pk])
            self.assertNotEqual(cursor.fetchone()[0], hook.token)

    def test_the_stop_button_in_a_chat_hands_it_to_the_team(self):
        channel = WhatsAppChannel.objects.create(workspace=self.workspace, display_name="S", phone_number_id="9")
        contact = Contact.objects.create(workspace=self.workspace, wa_id=NUMBER)
        conversation = Conversation.objects.create(workspace=self.workspace, channel=channel, contact=contact,
                                                   status=Conversation.Status.BOT)
        run = WorkflowRun.objects.create(workspace=self.workspace, workflow=self.workflow,
                                         version=self.workflow.latest, conversation=conversation, contact=contact,
                                         status=WorkflowRun.Status.WAITING, current_step="menu")
        conversation.automation_state = {engine.STATE_KEY: run.pk}
        conversation.save()
        self.client.force_login(self.agent)
        self.assertContains(self.client.get(f"/inbox/{conversation.pk}/thread/"), "Workflow running")
        self.client.post(self.url("stop_conversation", conversation.pk))
        run.refresh_from_db()
        conversation.refresh_from_db()
        self.assertEqual(run.status, WorkflowRun.Status.STOPPED)
        self.assertEqual(conversation.status, Conversation.Status.QUEUED)


class BuilderTests(WorkflowPageBase):
    """The drag-and-drop builder's server side: everything the canvas asks of it."""

    def op(self, **body):
        response = self.client.post(self.url("builder_op", self.workflow.pk), json.dumps(body),
                                    content_type="application/json")
        return response.status_code, response.json()

    def test_the_page_and_its_data(self):
        self.assertEqual(self.client.get(self.url("builder", self.workflow.pk)).status_code, 200)
        data = self.client.get(self.url("builder_data", self.workflow.pk)).json()
        self.assertEqual(data["start"], "hours")
        menu = next(n for n in data["nodes"] if n["id"] == "menu")
        self.assertEqual([e["key"] for e in menu["exits"]], ["answered", "invalid", "timeout"])
        self.assertTrue(all("x" in n and "y" in n for n in data["nodes"]))
        self.workflow.refresh_from_db()
        self.assertEqual(set(self.workflow.layout), {n["id"] for n in data["nodes"]})

    def test_moving_boxes_needs_no_draft(self):
        version = self.workflow.latest
        version.published_at = timezone.now()
        version.save()
        status, _ = self.op(op="move", positions={"menu": {"x": 900, "y": 40}})
        self.assertEqual(status, 200)
        self.workflow.refresh_from_db()
        self.assertEqual(self.workflow.layout["menu"], {"x": 900, "y": 40})
        self.assertIsNone(self.workflow.draft)

    def test_connecting_and_disconnecting(self):
        self.op(op="connect", **{"from": "claims_info"}, exit="next", to="done")
        self.assertEqual(self.workflow.draft.graph["steps"]["claims_info"]["next"]["next"], "done")
        self.op(op="disconnect", **{"from": "claims_info"}, exit="next")
        self.assertIsNone(self.workflow.draft.graph["steps"]["claims_info"]["next"]["next"])
        self.op(op="connect", **{"from": "__start"}, exit="start", to="welcome")
        self.assertEqual(self.workflow.draft.graph["start"], "welcome")

    def test_a_connection_from_an_exit_a_step_does_not_have_is_refused(self):
        status, data = self.op(op="connect", **{"from": "welcome"}, exit="answered", to="menu")
        self.assertEqual(status, 400)
        self.assertIn("Reload", data["error"])

    def test_adding_and_removing_a_step(self):
        status, data = self.op(op="add", kind="webhook", x=120, y=300)
        self.assertEqual(status, 200)
        added = data["added"]
        self.assertEqual(self.workflow.draft.graph["steps"][added]["type"], "webhook")
        self.workflow.refresh_from_db()
        self.assertEqual(self.workflow.layout[added], {"x": 120, "y": 300})
        self.assertIn(added, {c["step"] for c in data["checks"]}, "a lone new step is flagged")
        self.op(op="delete", id=added)
        self.assertNotIn(added, self.workflow.draft.graph["steps"])

    def test_tidy_lays_the_steps_out_left_to_right(self):
        _, data = self.op(op="tidy")
        x = {n["id"]: n["x"] for n in data["nodes"]}
        self.assertLess(x["hours"], x["welcome"])
        self.assertLess(x["welcome"], x["menu"])

    def test_the_side_panel_saves_and_tells_the_canvas(self):
        url = self.url("builder_panel", self.workflow.pk, "welcome")
        self.assertContains(self.client.get(url), "Welcome to Cinagi.")
        response = self.client.post(url, {"name": "Hello", "text": "Hi!", "next_next": "menu"})
        self.assertContains(response, "Saved to the draft.")
        self.assertIn("workflowChanged", response["HX-Trigger"])
        self.assertEqual(self.workflow.draft.graph["steps"]["welcome"]["name"], "Hello")

    def test_only_steps_nothing_leads_to_are_flagged(self):
        from apps.workflows.graph import checks

        graph_ = self.workflow.latest.graph
        graph_["start"] = "welcome"
        flagged = [c["step"] for c in checks(graph_) if "Nothing leads" in c["text"]]
        self.assertEqual(flagged, ["hours"], "steps only reachable through 'hours' are not listed one by one")

    def test_the_builder_is_for_managers_and_this_workspace_only(self):
        other = Workflow.objects.create(workspace=Workspace.objects.create(name="Elsewhere"), name="Theirs")
        self.assertEqual(self.client.get(self.url("builder_data", other.pk)).status_code, 404)
        self.client.force_login(self.agent)
        self.assertEqual(self.client.get(self.url("builder", self.workflow.pk)).status_code, 403)
        response = self.client.post(self.url("builder_op", self.workflow.pk), "{}", content_type="application/json")
        self.assertEqual(response.status_code, 403)


class TestChatTests(WorkflowPageBase):
    def say(self, **body):
        return self.client.post(self.url("builder_simulate", self.workflow.pk), json.dumps(body),
                                content_type="application/json").json()

    def test_a_conversation_on_screen(self):
        data = self.say(action="start")
        texts = [m["text"] for m in data["messages"] if m["from"] == "bot"]
        self.assertEqual(texts, ["Welcome to Cinagi.", "How can we help?"])
        self.assertTrue(data["waiting"])
        self.assertEqual(data["step"], "menu")
        data = self.say(text="Policy", reply_id="wf:menu:0")
        self.assertEqual(data["messages"][-1]["text"], "Your ID number?")
        data = self.say(text="8001015009087")
        notes = [m["text"] for m in data["messages"] if m["from"] == "note"]
        self.assertTrue(any("Would call the webhook" in n for n in notes))
        self.assertTrue(any("resolved" in n for n in notes))

    def test_nothing_real_happens(self):
        with mock.patch("apps.workflows.webhooks.requests.post") as post, \
                mock.patch("apps.channels_wa.messaging.meta_cloud.MetaCloudChannel._post_message") as send:
            self.say(action="start")
            self.say(text="Policy")
            self.say(text="8001015009087")
        post.assert_not_called()
        send.assert_not_called()
        self.assertFalse(Message.objects.exists())
        self.assertFalse(Contact.objects.exists())

    def test_after_hours_and_no_answer(self):
        data = self.say(action="start", after_hours=True)
        self.assertEqual([m["text"] for m in data["messages"] if m["from"] == "bot"], ["We are closed."])
        self.say(action="start")
        data = self.say(action="timeout")
        self.assertIn("nobody answered", data["messages"][-1]["text"])

    def test_wrong_answers_are_asked_again_like_the_real_thing(self):
        self.say(action="start")
        data = self.say(text="banana")
        self.assertEqual([m["text"] for m in data["messages"] if m["from"] == "bot"],
                         [engine.DEFAULT_INVALID, "How can we help?"])


class PhoneTestTests(WorkflowTestBase):
    def test_an_armed_number_runs_the_draft_once(self):
        from apps.workflows.models import PhoneTest

        draft = WorkflowVersion.objects.create(workflow=self.workflow, number=2, graph=graph(self.connection.pk))
        draft.graph["steps"]["welcome"]["config"]["text"] = "Draft welcome."
        draft.save()
        PhoneTest.objects.create(workspace=self.workspace, workflow=self.workflow, version=draft, wa_id=NUMBER,
                                 expires_at=timezone.now() + dt.timedelta(minutes=30))
        self.inbound("Hi")
        self.assertEqual(self.bodies()[0], "Draft welcome.")
        self.assertEqual(self.run_().version, draft)
        self.assertIsNotNone(PhoneTest.objects.get().used_at)

    def test_an_expired_test_runs_the_live_version(self):
        from apps.workflows.models import PhoneTest

        PhoneTest.objects.create(workspace=self.workspace, workflow=self.workflow, version=self.version,
                                 wa_id=NUMBER, expires_at=timezone.now() - dt.timedelta(minutes=1))
        self.inbound("Hi")
        self.assertIsNone(PhoneTest.objects.get().used_at)
        self.assertEqual(self.bodies()[0], "Welcome to Cinagi.")

    def test_stop_still_wins_over_a_phone_test(self):
        from apps.workflows.models import PhoneTest

        PhoneTest.objects.create(workspace=self.workspace, workflow=self.workflow, version=self.version,
                                 wa_id=NUMBER, expires_at=timezone.now() + dt.timedelta(minutes=30))
        self.inbound("STOP")
        self.assertFalse(WorkflowRun.objects.exists())

    def test_arming_from_the_builder(self):
        from django.contrib.auth import get_user_model
        from django.urls import reverse

        from apps.workflows.models import PhoneTest
        from apps.workspaces.models import WorkspaceMembership

        manager = get_user_model().objects.create_user("m", "m@example.com", "pw12345678")
        WorkspaceMembership.objects.create(user=manager, workspace=self.workspace, role="admin")
        self.client.force_login(manager)
        self.client.post(reverse("workflows:builder_phone_test", args=[self.workflow.pk]), {"number": "072 612 4698"})
        self.assertEqual(PhoneTest.objects.get().wa_id, NUMBER)
