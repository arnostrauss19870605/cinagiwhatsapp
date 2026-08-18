import zoneinfo

from django import forms

from .models import BusinessHours, Holiday, Workspace, WorkspaceMembership

TIMEZONES = sorted(tz for tz in zoneinfo.available_timezones() if "/" in tz)


class WorkspaceForm(forms.ModelForm):
    timezone = forms.ChoiceField(
        choices=[(tz, tz) for tz in TIMEZONES],
        initial="Africa/Johannesburg",
        help_text="Working hours and reports use this time zone.",
    )

    class Meta:
        model = Workspace
        fields = ["name", "description", "timezone", "accent_colour"]
        labels = {
            "name": "What should we call this workspace?",
            "description": "A short note so your team knows what it is for",
            "accent_colour": "Colour (helps agents see which number they are on)",
        }
        widgets = {
            "name": forms.TextInput(attrs={"placeholder": "e.g. Cinagi Claims Support"}),
            "description": forms.TextInput(attrs={"placeholder": "e.g. Main support line for claims"}),
            "accent_colour": forms.TextInput(attrs={"type": "color"}),
        }


class WorkspaceSettingsForm(forms.ModelForm):
    class Meta:
        model = Workspace
        fields = [
            "name",
            "description",
            "timezone",
            "accent_colour",
            "auto_assign_enabled",
            "queue_timeout_minutes",
            "sticky_agent_days",
            "send_out_of_hours_message",
            "out_of_hours_message",
        ]
        labels = {
            "auto_assign_enabled": "Automatically give new chats to an available agent",
            "queue_timeout_minutes": "Tell a supervisor if nobody picks up a chat within (minutes)",
            "sticky_agent_days": "Send returning customers back to the same agent for (days)",
            "send_out_of_hours_message": "Send an after-hours reply",
            "out_of_hours_message": "After-hours reply",
        }
        widgets = {"out_of_hours_message": forms.Textarea(attrs={"rows": 3})}


class BusinessHoursForm(forms.ModelForm):
    class Meta:
        model = BusinessHours
        fields = ["weekday", "opens_at", "closes_at", "is_closed"]
        widgets = {
            "opens_at": forms.TimeInput(attrs={"type": "time"}, format="%H:%M"),
            "closes_at": forms.TimeInput(attrs={"type": "time"}, format="%H:%M"),
        }


BusinessHoursFormSet = forms.modelformset_factory(
    BusinessHours, form=BusinessHoursForm, extra=0, can_delete=False
)


class HolidayForm(forms.ModelForm):
    class Meta:
        model = Holiday
        fields = ["date", "name", "recurring_annually"]
        widgets = {"date": forms.DateInput(attrs={"type": "date"})}
        labels = {"recurring_annually": "Happens every year on this date"}


class MembershipForm(forms.ModelForm):
    email = forms.EmailField(
        label="Team member's email address",
        help_text="They must already have a login. Ask an administrator to create one first.",
    )

    class Meta:
        model = WorkspaceMembership
        fields = ["role"]
        labels = {"role": "What may they do?"}

    def __init__(self, *args, workspace=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.workspace = workspace

    def clean_email(self):
        from django.contrib.auth import get_user_model

        email = self.cleaned_data["email"].strip().lower()
        user = get_user_model().objects.filter(email__iexact=email).first()
        if user is None:
            raise forms.ValidationError("No login found with that email address.")
        if WorkspaceMembership.objects.filter(user=user, workspace=self.workspace).exists():
            raise forms.ValidationError("They are already on this workspace.")
        self.user = user
        return email
