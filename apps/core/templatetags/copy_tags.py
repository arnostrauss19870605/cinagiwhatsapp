from django import template
from django.utils.safestring import mark_safe

register = template.Library()


@register.simple_tag(takes_context=True)
def copy(context, key, fallback=""):
    """Look up editable UI wording, falling back to the text in the template."""
    from apps.core.models import UiCopy

    workspace = getattr(context.get("request"), "workspace", None)
    entry = (
        UiCopy.objects.filter(key=key, workspace=workspace).first()
        or UiCopy.objects.filter(key=key, workspace__isnull=True).first()
    )
    return mark_safe(entry.text if entry else fallback)
