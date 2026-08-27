"""The event journey: who is invited, where they are in it, and what they did.

One rule shapes this whole app: a guest's own inbound message is free and opens
a 24 hour window, while anything we start costs a template send. So every entry
point is a wa.me deep link the guest taps - the invite email, the printed QR,
the venue QR at the door. We answer inside the window they opened.
"""

import secrets

from django.db import models
from django.utils import timezone

from apps.core.fields import EncryptedTextField
from apps.core.models import TimeStampedModel
from apps.core.scoping import WorkspaceScopedModel


def normalise_msisdn(raw):
    """International form, no plus. Local 0-prefixed numbers are read as South African."""
    digits = "".join(ch for ch in str(raw or "") if ch.isdigit())
    if not digits:
        return ""
    if digits.startswith("0"):
        digits = "27" + digits[1:]
    if digits.startswith("27") is False and len(digits) == 9:
        digits = "27" + digits
    return digits


def new_token():
    """URL-safe, and never starting or ending with punctuation.

    A trailing hyphen used to break the token matcher on the way back in. That
    is fixed, but a token that reads cleanly is also easier to say down a phone
    to a broker whose link will not open.
    """
    while True:
        token = secrets.token_urlsafe(9)
        if token[0].isalnum() and token[-1].isalnum():
            return token


class Event(WorkspaceScopedModel, TimeStampedModel):
    name = models.CharField(max_length=200)
    starts_at = models.DateTimeField()
    doors_open_at = models.DateTimeField(null=True, blank=True)
    ends_at = models.DateTimeField(
        null=True, blank=True, help_text="Blank means four hours after the start."
    )
    venue = models.CharField(max_length=200, blank=True)
    venue_lat = models.FloatField(null=True, blank=True)
    venue_lng = models.FloatField(null=True, blank=True)
    capacity = models.PositiveIntegerField(default=0, help_text="0 means no limit.")
    pack_url = models.URLField(
        blank=True, help_text="Launch pack, for brokers who cannot travel to Bryanston."
    )
    rsvp_closes_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    registration_secret = EncryptedTextField(
        blank=True,
        default="",
        help_text=(
            "Shared secret for the website registration form. The form's webhook must "
            "send it back to us; leave blank to keep the form endpoint switched off."
        ),
    )

    class Meta:
        ordering = ("-starts_at",)

    def __str__(self):
        return self.name

    @property
    def seats_taken(self):
        return self.guests.filter(rsvp_status=Guest.Rsvp.ATTENDING).aggregate(
            total=models.Sum("party_size")
        )["total"] or 0

    @property
    def seats_left(self):
        return max(0, self.capacity - self.seats_taken) if self.capacity else None

    @property
    def is_full(self):
        return self.capacity and self.seats_taken >= self.capacity

    @property
    def rsvp_open(self):
        if not self.is_active:
            return False
        if self.rsvp_closes_at and timezone.now() > self.rsvp_closes_at:
            return False
        return timezone.now() < self.starts_at

    def date_label(self):
        """'Wednesday 30 September' - passed to templates as a variable, never
        written into approved copy, so moving the event costs no Meta review."""
        local = timezone.localtime(self.starts_at)
        return f"{local:%A} {local.day} {local:%B}"

    def short_date_label(self):
        local = timezone.localtime(self.starts_at)
        return f"{local.day} {local:%B}"

    def doors_label(self):
        """'08h00' - how the doors time reads in a message. Passed to templates
        as a variable, like the date, so moving the time costs no Meta review."""
        local = timezone.localtime(self.doors_open_at or self.starts_at)
        return f"{local:%H}h{local:%M}"

    def next_guest_number(self):
        return (
            self.guests.filter(guest_number__isnull=False)
            .order_by("-guest_number")
            .values_list("guest_number", flat=True)
            .first()
            or 0
        ) + 1


class Guest(WorkspaceScopedModel, TimeStampedModel):
    """One invited person, and everything the journey knows about them."""

    class Rsvp(models.TextChoices):
        NONE = "none", "Not answered"
        ATTENDING = "attending", "Coming in person"
        PACK = "pack", "Outside Gauteng - wants the launch pack"
        DECLINED = "declined", "Cannot make it"
        WAITLIST = "waitlist", "On the waitlist"

    class Stage(models.TextChoices):
        INVITED = "invited", "Invited"
        ENGAGED = "engaged", "Started a chat"
        CONFIRMED = "confirmed", "RSVP complete"
        RECONFIRMED = "reconfirmed", "Confirmed the day before"
        CHECKED_IN = "checked_in", "Checked in"
        FEEDBACK = "feedback", "Gave feedback"
        ALUMNI = "alumni", "Post-event"
        STOPPED = "stopped", "Opted out"

    class Segment(models.TextChoices):
        BROKER = "broker", "Broker"
        VIP = "vip", "VIP"
        PRESS = "press", "Press"
        STAFF = "staff", "Cinagi staff"

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="guests")
    contact = models.ForeignKey(
        "contacts.Contact",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="event_guests",
        help_text="Linked the moment they message us.",
    )

    first_name = models.CharField(max_length=80)
    last_name = models.CharField(max_length=80, blank=True)
    company = models.CharField(max_length=120, blank=True)
    email = models.EmailField(blank=True)
    msisdn = models.CharField(max_length=32, blank=True, help_text="International form, no plus.")
    # Encrypted at rest: an ID number is POPIA-sensitive personal information,
    # held only for venue access on the day.
    id_number = EncryptedTextField(
        blank=True, default="", help_text="ID or passport number, for venue access."
    )
    segment = models.CharField(max_length=20, choices=Segment.choices, default=Segment.BROKER)

    invite_token = models.CharField(max_length=32, default=new_token, unique=True)
    ticket_token = models.CharField(max_length=32, default=new_token, unique=True)
    guest_number = models.PositiveIntegerField(null=True, blank=True)

    stage = models.CharField(max_length=20, choices=Stage.choices, default=Stage.INVITED)
    rsvp_status = models.CharField(max_length=20, choices=Rsvp.choices, default=Rsvp.NONE)
    party_size = models.PositiveSmallIntegerField(default=1)
    dietary = models.CharField(max_length=120, blank=True)
    interests = models.JSONField(default=list, blank=True)

    invite_email_sent_at = models.DateTimeField(null=True, blank=True)
    first_message_at = models.DateTimeField(null=True, blank=True)
    rsvp_at = models.DateTimeField(null=True, blank=True)
    reconfirmed_at = models.DateTimeField(null=True, blank=True)
    checked_in_at = models.DateTimeField(null=True, blank=True)
    feedback_rating = models.CharField(max_length=40, blank=True)
    stopped_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("last_name", "first_name")
        constraints = [
            models.UniqueConstraint(
                fields=["event", "msisdn"],
                condition=~models.Q(msisdn=""),
                name="uniq_guest_number_per_event",
            )
        ]
        indexes = [models.Index(fields=["event", "stage"])]

    def __str__(self):
        return f"{self.full_name} ({self.get_rsvp_status_display()})"

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def is_coming(self):
        return self.rsvp_status == self.Rsvp.ATTENDING

    def whatsapp_link(self, phone_number, prefix="RSVP"):
        """The deep link that makes the guest start the conversation.

        This is the whole cost model: they message first, so the greeting, the
        RSVP and the ticket all happen inside their free 24 hour window.
        """
        number = "".join(ch for ch in phone_number if ch.isdigit())
        return f"https://wa.me/{number}?text={prefix}-{self.invite_token}"

    def advance(self, stage, **fields):
        self.stage = stage
        for field, value in fields.items():
            setattr(self, field, value)
        self.save(update_fields=["stage", *fields.keys()])
        JourneyEvent.objects.create(
            workspace=self.workspace, guest=self, step=stage, detail=fields and str(fields) or ""
        )


class JourneyEvent(WorkspaceScopedModel):
    """Why did this guest get that message? Always answerable."""

    guest = models.ForeignKey(Guest, on_delete=models.CASCADE, related_name="journey")
    step = models.CharField(max_length=40)
    detail = models.CharField(max_length=255, blank=True)
    payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self):
        return f"{self.guest_id}:{self.step}"
