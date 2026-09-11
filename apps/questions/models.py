"""One-question surveys asked with a template's quick-reply buttons, and the
prize draw their answers feed.

A Question wraps an approved template. The template's buttons are the
possible answers; here each button gets a stored value, a score, and an
optional audience the person joins when they tap it. Every answer is tied to
the exact message it replied to, so a forwarded message or a stray tap never
counts as someone else's entry.
"""

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.core.models import TimeStampedModel
from apps.core.scoping import WorkspaceScopedModel


class Question(WorkspaceScopedModel, TimeStampedModel):
    template = models.OneToOneField(
        "library.MessageTemplate", on_delete=models.PROTECT, related_name="question",
        help_text="The approved template whose quick-reply buttons are the answers.",
    )
    title = models.CharField(max_length=120, help_text="How the team refers to it, e.g. 'Q1 exercise'.")
    thanks_text = models.TextField(
        blank=True,
        help_text="Sent back the first time someone answers. Leave blank to send nothing.",
    )
    counts_as_entry = models.BooleanField(
        default=True, help_text="Each person who answers earns one prize-draw entry."
    )
    is_active = models.BooleanField(default=True, help_text="Switched-off questions stop counting answers.")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        ordering = ("-created_at",)

    def __str__(self):
        return self.title

    def option_for(self, label):
        """The option matching a button tap, by its label. Case and spacing forgiven."""
        wanted = " ".join((label or "").split()).casefold()
        for option in self.options.all():
            if " ".join(option.label.split()).casefold() == wanted:
                return option
        return None

    def sends(self):
        """Outbound messages of this template that WhatsApp accepted."""
        from apps.inbox.models import Message

        return Message.objects.filter(
            workspace=self.workspace,
            template=self.template,
            direction=Message.Direction.OUT,
            wa_status__in=[Message.Status.SENT, Message.Status.DELIVERED, Message.Status.READ],
        )

    @property
    def people_asked(self):
        return self.sends().values("conversation__contact").distinct().count()

    @property
    def people_answered(self):
        return self.answers.count()

    @property
    def response_rate(self):
        asked = self.people_asked
        return round(self.people_answered * 100 / asked) if asked else 0


class AnswerOption(TimeStampedModel):
    question = models.ForeignKey(Question, on_delete=models.CASCADE, related_name="options")
    label = models.CharField(max_length=60, help_text="Must match the button text exactly.")
    value = models.SlugField(max_length=60, help_text="Short code stored with the answer, e.g. three_plus.")
    score = models.IntegerField(default=0)
    is_correct = models.BooleanField(default=False)
    audience = models.ForeignKey(
        "contacts.Audience", null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
        help_text="People who choose this answer are added to this audience.",
    )
    order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ("order", "pk")
        constraints = [
            models.UniqueConstraint(fields=["question", "label"], name="uniq_option_label_per_question"),
            models.UniqueConstraint(fields=["question", "value"], name="uniq_option_value_per_question"),
        ]

    def __str__(self):
        return self.label

    @property
    def answer_count(self):
        return self.answers.count()


class Answer(WorkspaceScopedModel):
    """One person's answer to one question. A second tap updates it, never duplicates it."""

    question = models.ForeignKey(Question, on_delete=models.CASCADE, related_name="answers")
    contact = models.ForeignKey("contacts.Contact", on_delete=models.CASCADE, related_name="answers")
    option = models.ForeignKey(AnswerOption, null=True, blank=True, on_delete=models.SET_NULL, related_name="answers")
    raw_text = models.CharField(max_length=200, blank=True)
    sent_message = models.ForeignKey(
        "inbox.Message", null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
        help_text="The template message this answered.",
    )
    reply_message = models.ForeignKey(
        "inbox.Message", null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
        help_text="The latest tap or reply that set this answer.",
    )
    bulk_send = models.ForeignKey(
        "library.BulkSend", null=True, blank=True, on_delete=models.SET_NULL, related_name="answers"
    )
    score = models.IntegerField(default=0)
    is_correct = models.BooleanField(default=False)
    first_answered_at = models.DateTimeField(default=timezone.now)
    answered_at = models.DateTimeField(default=timezone.now)
    taps = models.PositiveSmallIntegerField(default=1)

    class Meta:
        ordering = ("-answered_at",)
        constraints = [
            models.UniqueConstraint(fields=["question", "contact"], name="uniq_answer_per_person")
        ]

    def __str__(self):
        return f"{self.contact} -> {self.option or self.raw_text}"


class BonusEntry(WorkspaceScopedModel, TimeStampedModel):
    """Extra prize-draw entries handed out by a person, e.g. for turning up on the day."""

    contact = models.ForeignKey("contacts.Contact", on_delete=models.CASCADE, related_name="bonus_entries")
    entries = models.PositiveSmallIntegerField(default=1)
    reason = models.CharField(max_length=120)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        ordering = ("-created_at",)


class PrizeDraw(WorkspaceScopedModel, TimeStampedModel):
    """A recorded draw: who won, out of how many entries, drawn by whom. Never edited."""

    name = models.CharField(max_length=120)
    winner = models.ForeignKey("contacts.Contact", on_delete=models.PROTECT, related_name="prizes_won")
    winner_entries = models.PositiveIntegerField()
    total_entries = models.PositiveIntegerField()
    people = models.PositiveIntegerField()
    drawn_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        ordering = ("-created_at",)
