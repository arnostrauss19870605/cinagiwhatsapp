from django import forms

from .models import WhatsAppChannel


class ChannelConnectForm(forms.ModelForm):
    """Deliberately worded for someone looking at WhatsApp Manager, not a developer."""

    class Meta:
        model = WhatsAppChannel
        fields = ["display_name", "phone_number", "phone_number_id", "waba_id", "access_token", "app_secret"]
        labels = {
            "display_name": "What do you call this number?",
            "phone_number": "The number customers will message",
            "phone_number_id": "Phone number ID",
            "waba_id": "WhatsApp Business Account ID",
            "access_token": "Access token",
            "app_secret": "App secret",
        }
        help_texts = {
            "display_name": "Only your team sees this, e.g. Claims Support.",
            "phone_number": "As customers would dial it, e.g. +27 11 123 4567.",
            "phone_number_id": "In Meta: WhatsApp Manager > API Setup > Phone number ID.",
            "waba_id": "Same page, listed as WhatsApp Business Account ID. Needed to load your approved templates.",
            "access_token": "Meta: Business Settings > System users > Generate token. Choose a permanent token with whatsapp_business_messaging and whatsapp_business_management.",
            "app_secret": "Meta: App Dashboard > Settings > Basic > App secret. We use it to check that incoming messages really came from Meta.",
        }
        widgets = {
            "display_name": forms.TextInput(attrs={"placeholder": "Claims Support"}),
            "phone_number": forms.TextInput(attrs={"placeholder": "+27 11 123 4567"}),
            "phone_number_id": forms.TextInput(attrs={"placeholder": "123456789012345"}),
            "waba_id": forms.TextInput(attrs={"placeholder": "098765432109876"}),
            "access_token": forms.PasswordInput(render_value=True, attrs={"autocomplete": "off"}),
            "app_secret": forms.PasswordInput(render_value=True, attrs={"autocomplete": "off"}),
        }

    def clean_phone_number_id(self):
        value = "".join(ch for ch in self.cleaned_data["phone_number_id"] if ch.isdigit())
        if not value:
            raise forms.ValidationError("Copy the numeric Phone number ID from WhatsApp Manager.")
        qs = WhatsAppChannel.objects.filter(phone_number_id=value)
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError("That number is already connected to a workspace.")
        return value


class TestMessageForm(forms.Form):
    to_number = forms.CharField(
        label="Send a test message to",
        help_text="Use your own WhatsApp number, with the country code, e.g. +27 82 123 4567.",
        widget=forms.TextInput(attrs={"placeholder": "+27 82 123 4567"}),
    )

    def clean_to_number(self):
        digits = "".join(ch for ch in self.cleaned_data["to_number"] if ch.isdigit())
        if len(digits) < 8:
            raise forms.ValidationError("That does not look like a full phone number.")
        return digits
