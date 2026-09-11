"""Authoring a template in the app and handing it to Meta.

People write `{{first_name}}`, not `{{1}}`. Meta only understands numbers, so
the translation happens once, at submission, and the order is frozen on the
draft so sending can fill the right blank for the rest of the template's life.
"""

import mimetypes
import re

from django.conf import settings
from django.utils import timezone

from apps.channels_wa.messaging.base import TransportError
from apps.core.audit import audit

NAME_RE = re.compile(r"^[a-z0-9_]{1,512}$")
PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*|\d+)\s*\}\}")

BODY_MAX = 1024
HEADER_MAX = 60
FOOTER_MAX = 60
BUTTON_TEXT_MAX = 25
BUTTONS_MAX = 10

SOURCES = [
    ("first_name", "The person's first name"),
    ("full_name", "The person's full name"),
    ("number", "The person's WhatsApp number"),
    ("attribute", "A saved detail on the contact"),
    ("manual", "Typed in when sending"),
]

DEFAULT_SAMPLES = {"first_name": "Thabo", "full_name": "Thabo Mokoena", "number": "+27 72 612 4698"}


def placeholders(text):
    """Placeholder keys in order of first appearance."""
    seen = []
    for key in PLACEHOLDER_RE.findall(text or ""):
        if key not in seen:
            seen.append(key)
    return seen


def default_variable(key):
    source = "first_name" if key in ("first_name", "1") else "manual"
    if key in ("full_name", "name"):
        source = "full_name"
    if key in ("number", "phone", "mobile"):
        source = "number"
    return {
        "key": key,
        "label": key.replace("_", " ").capitalize() if not key.isdigit() else f"Value {key}",
        "source": source,
        "attribute": "",
        "sample": DEFAULT_SAMPLES.get(source, ""),
        "fallback": "",
        "required": True,
    }


def merge_variables(body, existing):
    """Keep what the person configured for keys still in the body; add rows for new keys."""
    by_key = {v.get("key"): v for v in existing or []}
    return [by_key.get(key) or default_variable(key) for key in placeholders(body)]


def positional_text(text, variables):
    """`Hi {{first_name}}` -> `Hi {{1}}`, following the draft's frozen order."""
    order = {v["key"]: index + 1 for index, v in enumerate(variables)}

    def swap(match):
        key = match.group(1)
        return "{{%d}}" % order[key] if key in order else match.group(0)

    return PLACEHOLDER_RE.sub(swap, text or "")


def validate(draft):
    """Plain-language problems that would make Meta reject the template. Empty means fine."""
    problems = []
    if not NAME_RE.match(draft.name or ""):
        problems.append("The template name may only use lowercase letters, numbers and underscores.")
    body = draft.body or ""
    if not body.strip():
        problems.append("The message needs some text.")
    if len(body) > BODY_MAX:
        problems.append(f"The message is {len(body)} characters; WhatsApp allows {BODY_MAX}.")
    if draft.header_type == "text" and not (draft.header_text or "").strip():
        problems.append("A text header needs some words, or choose 'No header'.")
    if len(draft.header_text or "") > HEADER_MAX:
        problems.append(f"The header is over {HEADER_MAX} characters.")
    if placeholders(draft.header_text or ""):
        problems.append("Keep placeholders out of the header; put them in the message instead.")
    if len(draft.footer or "") > FOOTER_MAX:
        problems.append(f"The footer is over {FOOTER_MAX} characters.")
    if draft.header_type in ("image", "video", "document") and not draft.header_sample:
        problems.append("Upload a sample file for the header. Meta reviews the template against it.")
    stripped = body.strip()
    if PLACEHOLDER_RE.match(stripped) or PLACEHOLDER_RE.search(stripped) and stripped.endswith("}}"):
        problems.append("Do not start or end the message with a placeholder; WhatsApp rejects that.")
    for variable in draft.variables or []:
        if not (variable.get("sample") or "").strip():
            problems.append(f"Give '{variable.get('key')}' a realistic sample value for Meta's review.")
        if variable.get("source") == "attribute" and not (variable.get("attribute") or "").strip():
            problems.append(f"Say which saved detail fills '{variable.get('key')}'.")
    buttons = draft.buttons or []
    if len(buttons) > BUTTONS_MAX:
        problems.append(f"At most {BUTTONS_MAX} buttons.")
    if sum(1 for b in buttons if b.get("type") == "url") > 2:
        problems.append("At most two link buttons.")
    if sum(1 for b in buttons if b.get("type") == "phone") > 1:
        problems.append("At most one call button.")
    for button in buttons:
        text = (button.get("text") or "").strip()
        if not text:
            problems.append("Every button needs a label.")
        elif len(text) > BUTTON_TEXT_MAX:
            problems.append(f"Button '{text}' is over {BUTTON_TEXT_MAX} characters.")
        if button.get("type") == "url" and not (button.get("url") or "").startswith("https://"):
            problems.append(f"Button '{text}' needs an https link.")
        if button.get("type") == "phone" and not (button.get("phone") or "").strip():
            problems.append(f"Button '{text}' needs a phone number.")
    # Deduplicate while keeping order.
    return list(dict.fromkeys(problems))


def build_components(draft, header_handle=None):
    """The Graph API payload's components, with positional variables and samples."""
    variables = draft.variables or []
    components = []
    if draft.header_type == "text":
        components.append({"type": "HEADER", "format": "TEXT", "text": draft.header_text})
    elif draft.header_type in ("image", "video", "document"):
        component = {"type": "HEADER", "format": draft.header_type.upper()}
        if header_handle:
            component["example"] = {"header_handle": [header_handle]}
        components.append(component)
    body = {"type": "BODY", "text": positional_text(draft.body, variables)}
    if variables:
        body["example"] = {"body_text": [[v.get("sample", "") for v in variables]]}
    components.append(body)
    if (draft.footer or "").strip():
        components.append({"type": "FOOTER", "text": draft.footer.strip()})
    buttons = []
    for button in draft.buttons or []:
        kind = button.get("type")
        text = (button.get("text") or "").strip()
        if kind == "quick_reply":
            buttons.append({"type": "QUICK_REPLY", "text": text})
        elif kind == "url":
            buttons.append({"type": "URL", "text": text, "url": button.get("url", "")})
        elif kind == "phone":
            buttons.append({"type": "PHONE_NUMBER", "text": text, "phone_number": button.get("phone", "")})
    if buttons:
        components.append({"type": "BUTTONS", "buttons": buttons})
    return components


def render_preview(draft, values=None):
    """The body with placeholders filled from samples, or from the given values."""
    text = draft.body or ""
    for variable in draft.variables or []:
        key = variable.get("key")
        value = (values or {}).get(key, variable.get("sample") or variable.get("fallback") or "")
        text = re.sub(r"\{\{\s*%s\s*\}\}" % re.escape(key), str(value), text)
    return text


def submit(draft, *, actor=None):
    """Send the draft to Meta for review. Returns the library row it now tracks.

    Raises TransportError or ValueError with a message safe to show.
    """
    from apps.library.models import MessageTemplate, TemplateDraft

    problems = validate(draft)
    if problems:
        raise ValueError(problems[0])
    if not draft.channel.waba_id:
        raise ValueError("Add the WhatsApp Business Account ID to this number before submitting.")

    client = draft.channel.client()
    header_handle = None
    if draft.header_type in ("image", "video", "document"):
        app_id = settings.WHATSAPP_APP_ID
        if not app_id:
            raise ValueError("WHATSAPP_APP_ID is not set on the server, so the sample file cannot be uploaded.")
        mime = mimetypes.guess_type(draft.header_sample.name)[0] or "application/octet-stream"
        header_handle = client.upload_sample_media(draft.header_sample.path, app_id, mime)
        if not header_handle:
            raise ValueError("Meta did not accept the sample file.")

    components = build_components(draft, header_handle)
    payload = {
        "name": draft.name,
        "language": draft.language,
        "category": draft.category,
        "components": components,
    }
    draft.status = TemplateDraft.Status.SUBMITTING
    draft.last_request = payload
    draft.save(update_fields=["status", "last_request"])
    try:
        response = client.create_template(payload)
    except TransportError as exc:
        draft.status = TemplateDraft.Status.ERROR
        draft.last_response = exc.body or {"error": str(exc)}
        draft.rejection_reason = exc.friendly
        draft.save(update_fields=["status", "last_response", "rejection_reason"])
        audit("template.submit_failed", workspace=draft.workspace, actor=actor, target=draft, error=exc.friendly)
        raise

    now = timezone.now()
    meta_status = (response.get("status") or "PENDING").upper()
    template, _ = MessageTemplate.objects.update_or_create(
        channel=draft.channel,
        name=draft.name,
        language=draft.language,
        defaults={
            "workspace": draft.workspace,
            "meta_id": str(response.get("id", "")),
            "category": response.get("category") or draft.category,
            "status": MessageTemplate.Status.APPROVED if meta_status == "APPROVED" else MessageTemplate.Status.PENDING,
            "components": components,
            "last_synced_at": now,
            "status_changed_at": now,
        },
    )
    draft.template = template
    draft.meta_template_id = str(response.get("id", ""))
    draft.last_response = response
    draft.rejection_reason = ""
    draft.status = TemplateDraft.Status.SUBMITTED
    draft.submitted_at = now
    draft.save(update_fields=["template", "meta_template_id", "last_response", "rejection_reason", "status", "submitted_at"])
    audit("template.submitted", workspace=draft.workspace, actor=actor, target=template, draft=draft.pk, meta_id=draft.meta_template_id)
    return template


def next_version_name(name, version):
    base = re.sub(r"_v\d+$", "", name or "")
    return f"{base}_v{version}"


def clone(draft, *, actor=None):
    """A fresh, editable copy. The original keeps serving until this one is approved."""
    from apps.library.models import TemplateDraft

    version = draft.version + 1
    copy = TemplateDraft.objects.create(
        workspace=draft.workspace,
        channel=draft.channel,
        internal_title=draft.internal_title,
        name=next_version_name(draft.name, version),
        language=draft.language,
        category=draft.category,
        header_type=draft.header_type,
        header_text=draft.header_text,
        header_sample=draft.header_sample,
        body=draft.body,
        footer=draft.footer,
        buttons=draft.buttons,
        variables=draft.variables,
        description=draft.description,
        version=version,
        parent=draft,
        created_by=actor,
    )
    audit("template.new_version", workspace=draft.workspace, actor=actor, target=copy, parent=draft.pk)
    return copy


def values_for(template, contact, supplied):
    """The positional values to send `template` to `contact`.

    `supplied` is either a list (legacy positional templates: everything after
    the first name) or a dict of the manually typed values for a template
    authored here. Raises ValueError naming the blank that cannot be filled.
    """
    from apps.library.bulk import first_name_of

    variables = template.variable_map
    if not variables:
        return [first_name_of(contact), *(supplied or [])]
    manual = supplied if isinstance(supplied, dict) else {}
    values = []
    for variable in variables:
        key, source = variable.get("key"), variable.get("source")
        if source == "first_name":
            value = first_name_of(contact)
        elif source == "full_name":
            value = contact.name
        elif source == "number":
            value = contact.pretty_number
        elif source == "attribute":
            value = (contact.attributes or {}).get(variable.get("attribute") or "", "")
        else:
            value = manual.get(key, "")
        value = str(value or "").strip() or str(variable.get("fallback") or "").strip()
        if not value:
            if variable.get("required", True):
                raise ValueError(f"no value for '{key}'")
            value = ""
        values.append(value)
    return values
