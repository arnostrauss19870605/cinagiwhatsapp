"""Questions asked with quick-reply buttons: answers land on the right person, once."""

from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.channels_wa.inbound import _store_message
from apps.channels_wa.messaging.base import SendResult
from apps.channels_wa.models import WhatsAppChannel
from apps.channels_wa.outbound import send_template
from apps.contacts.models import Audience, Contact
from apps.inbox.models import Conversation, Message
from apps.library.models import BulkSend, MessageTemplate
from apps.questions.models import Answer, AnswerOption, BonusEntry, PrizeDraw, Question
from apps.workspaces.models import Workspace, WorkspaceMembership

User = get_user_model()
SENT = SendResult(ok=True, wamid="wamid.Q1")
SEND_TEXT = "apps.channels_wa.messaging.meta_cloud.MetaCloudChannel.send_text"
SEND_TEMPLATE = "apps.channels_wa.messaging.meta_cloud.MetaCloudChannel.send_template"


@override_settings(OUTBOUND_COMMS_MODE="live")
class QuestionAnswerTests(TestCase):
    def setUp(self):
        self.workspace = Workspace.objects.create(name="Cinagi Broker Support", auto_assign_enabled=False)
        self.channel = WhatsAppChannel.objects.create(
            workspace=self.workspace, display_name="Broker Support", phone_number_id="123"
        )
        self.owner = User.objects.create_user("arno@cinagi.co.za", "arno@cinagi.co.za", first_name="Arno")
        WorkspaceMembership.objects.create(user=self.owner, workspace=self.workspace, role="owner")
        self.contact = Contact.objects.create(workspace=self.workspace, wa_id="27820000001", display_name="Thabo Mokoena")
        self.template = MessageTemplate.objects.create(
            workspace=self.workspace, channel=self.channel, name="quiz_exercise", language="en",
            status=MessageTemplate.Status.APPROVED,
            components=[
                {"type": "BODY", "text": "Hi {{1}}, how often do you exercise?"},
                {"type": "BUTTONS", "buttons": [
                    {"type": "QUICK_REPLY", "text": "Never"},
                    {"type": "QUICK_REPLY", "text": "1-2 times"},
                    {"type": "QUICK_REPLY", "text": "3+ times"},
                ]},
            ],
        )
        self.active = Audience.objects.create(workspace=self.workspace, name="Active brokers")
        self.question = Question.objects.create(
            workspace=self.workspace, template=self.template, title="Q1 exercise",
            thanks_text="Thanks {{first_name}}, that is one entry in the draw.",
        )
        AnswerOption.objects.create(question=self.question, label="Never", value="never", score=0, order=0)
        AnswerOption.objects.create(question=self.question, label="1-2 times", value="one_to_two", score=1, order=1)
        AnswerOption.objects.create(
            question=self.question, label="3+ times", value="three_plus", score=2, is_correct=True,
            audience=self.active, order=2,
        )
        self.conversation = Conversation.objects.create(
            workspace=self.workspace, channel=self.channel, contact=self.contact, status=Conversation.Status.BOT
        )
        with mock.patch(SEND_TEMPLATE, return_value=SENT):
            self.sent = send_template(self.conversation, self.template, ["Thabo"], actor=Message.Actor.BOT)

    def _tap(self, label, *, context=True, wamid="wamid.tap1"):
        payload = {
            "id": wamid, "from": self.contact.wa_id, "type": "button",
            "button": {"text": label, "payload": label},
            "timestamp": str(int(timezone.now().timestamp())),
        }
        if context:
            payload["context"] = {"from": "27110000000", "id": self.sent.wamid}
        with mock.patch(SEND_TEXT, return_value=SENT) as send:
            _store_message(self.channel, payload, {})
        self.conversation.refresh_from_db()
        return send

    def test_a_button_tap_becomes_a_scored_answer_tied_to_the_send(self):
        send = self._tap("3+ times")
        answer = Answer.objects.get()
        self.assertEqual(answer.contact, self.contact)
        self.assertEqual(answer.option.value, "three_plus")
        self.assertEqual((answer.score, answer.is_correct), (2, True))
        self.assertEqual(answer.sent_message, self.sent)
        self.assertEqual(answer.reply_message.kind, Message.Kind.BUTTON)
        self.assertIn(self.contact, self.active.contacts.all())
        # Thanked by name, and the chat is not thrown at a person.
        send.assert_called_once()
        self.assertIn("Thanks Thabo", send.call_args.args[1])
        self.assertEqual(self.conversation.status, Conversation.Status.BOT)

    def test_a_second_tap_changes_the_answer_without_a_second_entry_or_thanks(self):
        self._tap("3+ times")
        send = self._tap("Never", wamid="wamid.tap2")
        send.assert_not_called()
        answer = Answer.objects.get()
        self.assertEqual((answer.option.value, answer.score, answer.taps), ("never", 0, 2))
        self.assertNotIn(self.contact, self.active.contacts.all())

    def test_the_same_webhook_twice_is_one_answer(self):
        self._tap("Never")
        self._tap("Never")  # Meta redelivers; ProcessedInbound is per wamid, but be sure here too
        self.assertEqual(Answer.objects.count(), 1)
        self.assertEqual(Answer.objects.get().taps, 1)

    def test_a_tap_without_context_matches_a_recent_question_by_label(self):
        self._tap("1-2 times", context=False)
        self.assertEqual(Answer.objects.get().option.value, "one_to_two")

    def test_free_text_that_is_not_an_answer_is_left_for_a_person(self):
        payload = {
            "id": "wamid.text1", "from": self.contact.wa_id, "type": "text",
            "text": {"body": "Can you call me about my policy?"},
            "timestamp": str(int(timezone.now().timestamp())),
        }
        with mock.patch(SEND_TEXT, return_value=SENT):
            _store_message(self.channel, payload, {})
        self.assertEqual(Answer.objects.count(), 0)
        self.conversation.refresh_from_db()
        self.assertEqual(self.conversation.status, Conversation.Status.QUEUED)

    def test_a_switched_off_question_stops_counting(self):
        self.question.is_active = False
        self.question.save()
        self._tap("Never")
        self.assertEqual(Answer.objects.count(), 0)

    def test_an_answer_carries_its_bulk_send(self):
        batch = BulkSend.objects.create(workspace=self.workspace, channel=self.channel, template=self.template, template_name="quiz_exercise")
        Message.objects.filter(pk=self.sent.pk).update(bulk_send=batch)
        self._tap("Never")
        self.assertEqual(Answer.objects.get().bulk_send, batch)

    # -- pages ----------------------------------------------------------------

    def test_results_page_and_export(self):
        self._tap("3+ times")
        self.client.force_login(self.owner)
        page = self.client.get(reverse("questions:question_results", args=[self.question.pk]))
        self.assertEqual(page.status_code, 200)
        self.assertEqual((page.context["asked"], page.context["answered"], page.context["correct"]), (1, 1, 1))
        self.assertContains(page, "Thabo Mokoena")
        export = self.client.get(reverse("questions:question_export", args=[self.question.pk]))
        self.assertEqual(export.status_code, 200)
        self.assertIn("spreadsheetml", export["Content-Type"])

    def test_creating_a_question_from_a_template_with_buttons(self):
        other = MessageTemplate.objects.create(
            workspace=self.workspace, channel=self.channel, name="quiz_sleep", language="en",
            status=MessageTemplate.Status.APPROVED,
            components=[
                {"type": "BODY", "text": "How many hours do you sleep?"},
                {"type": "BUTTONS", "buttons": [
                    {"type": "QUICK_REPLY", "text": "Under 6"}, {"type": "QUICK_REPLY", "text": "6 to 8"},
                ]},
            ],
        )
        self.client.force_login(self.owner)
        picker = self.client.get(reverse("questions:question_create"))
        self.assertContains(picker, "quiz_sleep")
        self.assertNotContains(picker, "quiz_exercise")  # already a question
        response = self.client.post(
            reverse("questions:question_create"),
            {
                "save": "1", "template": other.pk, "title": "Q2 sleep", "thanks_text": "", "counts_as_entry": "on",
                "opt_0_value": "under_6", "opt_0_score": "0", "opt_0_audience": "",
                "opt_1_value": "six_to_eight", "opt_1_score": "2", "opt_1_correct": "on", "opt_1_audience": self.active.pk,
            },
        )
        question = Question.objects.get(template=other)
        self.assertRedirects(response, reverse("questions:question_results", args=[question.pk]))
        options = list(question.options.values_list("label", "value", "score", "is_correct"))
        self.assertEqual(options, [("Under 6", "under_6", 0, False), ("6 to 8", "six_to_eight", 2, True)])
        self.assertEqual(question.options.get(label="6 to 8").audience, self.active)

    def test_agents_cannot_see_questions(self):
        agent = User.objects.create_user("sam@cinagi.co.za", "sam@cinagi.co.za")
        WorkspaceMembership.objects.create(user=agent, workspace=self.workspace, role="agent")
        self.client.force_login(agent)
        self.assertEqual(self.client.get(reverse("questions:questions")).status_code, 403)


@override_settings(OUTBOUND_COMMS_MODE="live")
class PrizeDrawTests(TestCase):
    def setUp(self):
        self.workspace = Workspace.objects.create(name="Cinagi Broker Support")
        self.channel = WhatsAppChannel.objects.create(workspace=self.workspace, display_name="A", phone_number_id="1")
        self.owner = User.objects.create_user("arno@cinagi.co.za", "arno@cinagi.co.za")
        WorkspaceMembership.objects.create(user=self.owner, workspace=self.workspace, role="owner")
        self.client.force_login(self.owner)
        self.thabo = Contact.objects.create(workspace=self.workspace, wa_id="27820000001", display_name="Thabo")
        self.ann = Contact.objects.create(workspace=self.workspace, wa_id="27820000002", display_name="Ann")
        self.left = Contact.objects.create(workspace=self.workspace, wa_id="27820000003", display_name="Left", opted_out_at=timezone.now())
        for index in range(2):
            template = MessageTemplate.objects.create(
                workspace=self.workspace, channel=self.channel, name=f"q{index}", language="en",
                status=MessageTemplate.Status.APPROVED,
                components=[{"type": "BUTTONS", "buttons": [{"type": "QUICK_REPLY", "text": "Yes"}]}],
            )
            question = Question.objects.create(workspace=self.workspace, template=template, title=f"Q{index}")
            option = AnswerOption.objects.create(question=question, label="Yes", value="yes")
            Answer.objects.create(workspace=self.workspace, question=question, contact=self.thabo, option=option)
            if index == 0:
                Answer.objects.create(workspace=self.workspace, question=question, contact=self.ann, option=option)
                Answer.objects.create(workspace=self.workspace, question=question, contact=self.left, option=option)

    def test_entries_are_one_per_question_plus_bonuses_and_skip_opt_outs(self):
        BonusEntry.objects.create(workspace=self.workspace, contact=self.ann, entries=2, reason="attended")
        page = self.client.get(reverse("questions:prize_draw"))
        rows = {r["contact"].name: r["entries"] for r in page.context["rows"]}
        self.assertEqual(rows, {"Thabo": 2, "Ann": 3})
        self.assertEqual(page.context["total_entries"], 5)

    def test_drawing_records_a_winner_from_the_pool(self):
        response = self.client.post(reverse("questions:draw_winner"), {"name": "Launch prize"})
        self.assertRedirects(response, reverse("questions:prize_draw"))
        draw = PrizeDraw.objects.get()
        self.assertIn(draw.winner, [self.thabo, self.ann])
        self.assertEqual((draw.total_entries, draw.people, draw.drawn_by), (3, 2, self.owner))

    def test_bonus_entries_come_from_the_page(self):
        self.client.post(reverse("questions:bonus_entry"), {"contact": self.thabo.pk, "entries": 3, "reason": "on the day"})
        self.assertEqual(BonusEntry.objects.get().entries, 3)
        export = self.client.get(reverse("questions:prize_draw_export"))
        self.assertEqual(export.status_code, 200)
