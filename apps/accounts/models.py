from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    """Custom user from day one so adding fields later is never a migration crisis."""

    email = models.EmailField("email address", unique=True)
    phone = models.CharField(max_length=30, blank=True)
    is_platform_admin = models.BooleanField(
        default=False, help_text="Can see and manage every workspace."
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
