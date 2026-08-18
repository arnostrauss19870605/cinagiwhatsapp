"""Database fields that encrypt their contents at rest.

WhatsApp access tokens and app secrets are per workspace and live in the
database (that is what makes this platform multi tenant), so they must not be
readable by anyone with a database dump. Key comes from FIELD_ENCRYPTION_KEY.
"""

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.db import models

_PREFIX = "enc:"


def _cipher():
    return Fernet(settings.FIELD_ENCRYPTION_KEY.encode())


def encrypt(value: str) -> str:
    if value in (None, ""):
        return value
    return _PREFIX + _cipher().encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    if value in (None, ""):
        return value
    if not value.startswith(_PREFIX):
        # Value written before encryption was enabled - return as-is so the
        # app keeps working; it will be re-encrypted on next save.
        return value
    try:
        return _cipher().decrypt(value[len(_PREFIX) :].encode()).decode()
    except InvalidToken:
        return ""


class EncryptedTextField(models.TextField):
    """TextField whose value is Fernet encrypted in the database."""

    def get_prep_value(self, value):
        return encrypt(super().get_prep_value(value))

    def from_db_value(self, value, expression, connection):
        return decrypt(value)

    def to_python(self, value):
        if value is None:
            return value
        if isinstance(value, str) and value.startswith(_PREFIX):
            return decrypt(value)
        return value
