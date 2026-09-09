from django import forms
from django.contrib.auth import get_user_model

from .models import email_domain_allowed

User = get_user_model()

INPUT = "mt-1 w-full rounded-lg border border-slate-300 px-3 py-2 focus:border-[#0065A9] focus:outline-none focus:ring-1 focus:ring-[#0065A9]"


class EmailForm(forms.Form):
    email = forms.EmailField(
        label="Your work email address",
        widget=forms.EmailInput(attrs={"class": INPUT, "autofocus": True, "autocomplete": "email"}),
    )

    def clean_email(self):
        return self.cleaned_data["email"].strip().lower()


class CodeForm(forms.Form):
    code = forms.CharField(
        label="The six-digit code we emailed you",
        min_length=6,
        max_length=6,
        widget=forms.TextInput(
            attrs={
                "class": INPUT + " text-center text-2xl tracking-[.4em]",
                "autofocus": True,
                "inputmode": "numeric",
                "autocomplete": "one-time-code",
                "pattern": "[0-9]*",
            }
        ),
    )

    def clean_code(self):
        return "".join(ch for ch in self.cleaned_data["code"] if ch.isdigit())


class UserForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ["first_name", "last_name", "email", "phone"]
        labels = {
            "first_name": "First name",
            "last_name": "Last name",
            "email": "Work email address",
            "phone": "Mobile number (optional)",
        }
        widgets = {
            "first_name": forms.TextInput(attrs={"class": INPUT}),
            "last_name": forms.TextInput(attrs={"class": INPUT}),
            "email": forms.EmailInput(attrs={"class": INPUT, "autocomplete": "off"}),
            "phone": forms.TextInput(attrs={"class": INPUT, "placeholder": "e.g. 082 123 4567"}),
        }

    def clean_first_name(self):
        value = self.cleaned_data["first_name"].strip()
        if not value:
            raise forms.ValidationError("A first name is needed so emails can greet them.")
        return value

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        if not email_domain_allowed(email):
            from django.conf import settings

            domains = ", ".join("@" + d for d in settings.LOGIN_EMAIL_DOMAINS)
            raise forms.ValidationError(f"Only {domains} addresses can be given a login.")
        clash = User.objects.filter(email__iexact=email).exclude(pk=self.instance.pk)
        if clash.exists():
            raise forms.ValidationError("Someone already has a login with that email address.")
        return email

    def save(self, commit=True):
        user = super().save(commit=False)
        # Sign-in is by email, so the username is the email. Passwords are never
        # set through this form; the admin site keeps its own for superusers.
        user.username = user.email
        if not user.pk:
            user.set_unusable_password()
        if commit:
            user.save()
        return user
