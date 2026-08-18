from django import forms

from .models import AgentProfile


class AgentProfileForm(forms.ModelForm):
    class Meta:
        model = AgentProfile
        fields = ["presence", "max_concurrent", "accepts_auto_assignment", "team"]
        labels = {
            "presence": "Right now I am",
            "max_concurrent": "Most chats I want at once",
            "accepts_auto_assignment": "Send me new chats automatically",
            "team": "Team",
        }
