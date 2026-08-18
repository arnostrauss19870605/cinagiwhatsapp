from django import forms

from .models import KnowledgeArticle, QuickSnippet


class QuickSnippetForm(forms.ModelForm):
    share = forms.ChoiceField(
        choices=[("team", "Everyone on this workspace"), ("me", "Only me")],
        initial="team",
        label="Who can use this?",
        widget=forms.RadioSelect,
    )

    class Meta:
        model = QuickSnippet
        fields = ["title", "shortcut", "category", "body"]
        labels = {
            "title": "Name it",
            "shortcut": "Short word to find it",
            "category": "Group (optional)",
            "body": "The message",
        }
        help_texts = {
            "title": "What your team will see in the list, e.g. Office hours.",
            "shortcut": "Type / then this word in the chat box to insert it.",
            "body": "Tip: {{contact.first_name}} inserts the customer's first name.",
        }
        widgets = {
            "body": forms.Textarea(attrs={"rows": 5}),
            "title": forms.TextInput(attrs={"placeholder": "Office hours"}),
            "shortcut": forms.TextInput(attrs={"placeholder": "hours"}),
        }


class KnowledgeArticleForm(forms.ModelForm):
    class Meta:
        model = KnowledgeArticle
        fields = ["title", "body", "keywords", "is_published"]
        widgets = {"body": forms.Textarea(attrs={"rows": 8})}
