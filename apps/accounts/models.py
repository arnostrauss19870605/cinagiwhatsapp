import hashlib
import hmac
import secrets

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils import timezone


class User(AbstractUser):
    """Custom user from day one so adding fields later is never a migration crisis."""

    email = models.EmailField("email address", unique=True)
    phone = models.CharField(max_length=30, blank=True)
    is_platform_admin = models.BooleanField(
        default=False, help_text="Can see and manage every workspace."
    )
    send_on_enter = models.BooleanField(
        default=True, help_text="In a chat, Enter sends the message and Shift+Enter starts a new line."
    )

    REQUIRED_FIELDS = ["email"]

    def __str__(self):
        return self.get_full_name() or self.username

    @property
    def display_name(self):
        return self.get_full_name() or self.username

    @property
    def initials(self):
        parts = (self.get_full_name() or self.username).split()
        return "".join(p[0].upper() for p in parts[:2]) or "?"


def email_domain_allowed(email):
    """Is this address inside the organisation? Empty allowlist means any domain."""
    allowed = [d.lower().lstrip("@") for d in settings.LOGIN_EMAIL_DOMAINS]
    if not allowed:
        return True
    domain = (email or "").rsplit("@", 1)[-1].lower()
    return domain in allowed


class LoginCode(models.Model):
    """A one-time sign-in code, emailed to the person. Stored hashed, like a password.

    Passwords are the thing people reuse, share and forget; a code that dies in
    ten minutes is not. Every request for a new code retires the previous one,
    and five wrong guesses retire it early.
    """

    MAX_ATTEMPTS = 5

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="login_codes")
    code_hash = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ("-created_at",)

    @staticmethod
    def _hash(code):
        return hmac.new(
            settings.SECRET_KEY.encode(), str(code).encode(), hashlib.sha256
        ).hexdigest()

    @classmethod
    def issue(cls, user):
        """Retire any live code and mint a fresh six-digit one. Returns (record, code)."""
        cls.objects.filter(user=user, used_at__isnull=True).update(used_at=timezone.now())
        code = f"{secrets.randbelow(1_000_000):06d}"
        record = cls.objects.create(
            user=user,
            code_hash=cls._hash(code),
            expires_at=timezone.now() + timezone.timedelta(minutes=settings.LOGIN_CODE_MINUTES),
        )
        return record, code

    @classmethod
    def recent_count(cls, user, minutes=10):
        return cls.objects.filter(
            user=user, created_at__gte=timezone.now() - timezone.timedelta(minutes=minutes)
        ).count()

    @classmethod
    def live_for(cls, user):
        return cls.objects.filter(
            user=user, used_at__isnull=True, expires_at__gt=timezone.now()
        ).first()

    def verify(self, code):
        """One attempt. True on a match; the record is retired on success or on too many misses."""
        self.attempts += 1
        matched = hmac.compare_digest(self.code_hash, self._hash((code or "").strip()))
        if matched or self.attempts >= self.MAX_ATTEMPTS:
            self.used_at = timezone.now()
        self.save(update_fields=["attempts", "used_at"])
        return matched
