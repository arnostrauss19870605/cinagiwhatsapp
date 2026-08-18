from django import forms


class ReplyForm(forms.Form):
    body = forms.CharField(
        widget=forms.Textarea(
            attrs={
                "rows": 2,
                "placeholder": "Type a message.  Press / to find a saved reply.",
                "x-ref": "composer",
            }
        ),
        required=False,
    )
    attachment = forms.FileField(required=False)
    snippet_id = forms.IntegerField(required=False, widget=forms.HiddenInput)

    def clean(self):
        cleaned = super().clean()
        if not (cleaned.get("body") or "").strip() and not cleaned.get("attachment"):
            raise forms.ValidationError("Type a message or attach a file first.")
        return cleaned


class NoteForm(forms.Form):
    body = forms.CharField(
        widget=forms.Textarea(
            attrs={"rows": 2, "placeholder": "Note for your team - the customer never sees this."}
        )
    )


class TemplateSendForm(forms.Form):
    template_id = forms.IntegerField(widget=forms.HiddenInput)

    def __init__(self, *args, template=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.template = template
        if template is not None:
            for index in range(1, template.variable_count + 1):
                self.fields[f"var_{index}"] = forms.CharField(
                    label=f"Value for {{{{{index}}}}}", required=True
                )

    def values(self):
        return [
            self.cleaned_data[f"var_{i}"]
            for i in range(1, (self.template.variable_count if self.template else 0) + 1)
        ]
