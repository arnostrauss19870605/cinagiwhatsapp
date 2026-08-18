from django.db import models

from apps.core.models import TimeStampedModel
from apps.core.scoping import WorkspaceScopedModel


class Contact(WorkspaceScopedModel, TimeStampedModel):
    """A person messaging one workspace's number.

    Deliberately per workspace: the same phone number talking to two different
    Cinagi numbers is two contacts with two separate histories. Linking them is
    a later, explicit feature - never an accident.
    """

    wa_id = models.CharField(
        max_length=32, help_text="WhatsApp id: the phone number in full international form, no +."
    )
    display_name = models.CharField(max_length=120, blank=True)
    profile_name = models.CharField(
        max_length=120, blank=True, help_text="The name they set on their own WhatsApp profile."
    )
    locale = models.CharField(max_length=12, blank=True)
    attributes = models.JSONField(default=dict, blank=True)
    is_blocked = models.BooleanField(default=False)
    opted_out_at = models.DateTimeField(null=True, blank=True)
    first_seen_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-last_seen_at",)
        constraints = [
            models.UniqueConstraint(fields=["workspace", "wa_id"], name="uniq_contact_per_workspace")
        ]
        indexes = [models.Index(fields=["workspace", "wa_id"])]

    def __str__(self):
        return self.name

    @property
    def name(self):
        return self.display_name or self.profile_name or f"+{self.wa_id}"

    @property
    def initials(self):
        parts = self.name.replace("+", "").split()
        return "".join(p[0].upper() for p in parts[:2] if p[0].isalpha()) or "#"

    @property
    def pretty_number(self):
        return f"+{self.wa_id}"

    @property
    def is_opted_out(self):
        return self.opted_out_at is not None


class ContactExternalRef(TimeStampedModel):
    """Link a WhatsApp contact to a record in another system.

    This is the seam the Xealth/BIMS integration plugs into later: policy
    number, member key, claim reference. Nothing here calls anything yet.
    """

    class System(models.TextChoices):
        XEALTH = "xealth", "Xealth / BIMS"
        CRM = "crm", "CRM"
        OTHER = "other", "Other"

    contact = models.ForeignKey(Contact, on_delete=models.CASCADE, related_name="external_refs")
    system = models.CharField(max_length=32, choices=System.choices)
    external_id = models.CharField(max_length=120)
    payload = models.JSONField(default=dict, blank=True)
    verified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["contact", "system", "external_id"], name="uniq_contact_external_ref"
            )
        ]

    def __str__(self):
        return f"{self.system}:{self.external_id}"


class ContactConsent(TimeStampedModel):
    class Purpose(models.TextChoices):
        SERVICE = "service", "Service messages"
        MARKETING = "marketing", "Marketing messages"
        AI = "ai_processing", "AI assistance"

    contact = models.ForeignKey(Contact, on_delete=models.CASCADE, related_name="consents")
    purpose = models.CharField(max_length=32, choices=Purpose.choices)
    granted = models.BooleanField(default=True)
    source = models.CharField(max_length=120, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["contact", "purpose"], name="uniq_contact_consent")
        ]
