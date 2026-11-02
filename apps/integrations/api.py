"""A small API in respond.io's shape, so systems already calling respond.io need only a new address and key.

    POST /api/v2/contact/create_or_update/{identifier}
    GET  /api/v2/contact/{identifier}
    POST /api/v2/contact/{identifier}/message

Authorization is "Bearer <key>". The key belongs to one workspace, which is
how a call knows whose contacts and which WhatsApp number it means; a
message may name a number with "channelId". Identifiers are respond.io's:
"phone:+27821234567", "id:123" or "email:someone@example.com".

Every send goes through the same functions as the inbox, so opt-outs, the
24-hour rule and test mode apply exactly as they do everywhere else.
"""

import json
import logging

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from apps.contacts.models import Contact, normalise_msisdn
from apps.core.audit import audit

from .models import ApiKey

logger = logging.getLogger(__name__)

MEDIA_KINDS = {"image": "image", "video": "video", "audio": "audio", "file": "document", "document": "document"}


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def _error(status, message):
    return JsonResponse({"code": status, "message": message}, status=status)


def endpoint(methods):
    """Authenticate, parse JSON, and turn ApiError into a plain JSON answer."""
    def wrap(view):
        @csrf_exempt
        @require_http_methods(methods)
        def inner(request, *args, **kwargs):
            header = request.headers.get("Authorization", "")
            raw = header[7:].strip() if header.lower().startswith("bearer ") else ""
            key = ApiKey.authenticate(raw)
            if key is None:
                return _error(401, "Missing or wrong API key. Send it as 'Authorization: Bearer <key>'.")
            body = {}
            if request.body:
                try:
                    body = json.loads(request.body)
                except ValueError:
                    return _error(400, "The request body is not valid JSON.")
                if not isinstance(body, dict):
                    return _error(400, "The request body must be a JSON object.")
            try:
                return view(request, key, body, *args, **kwargs)
            except ApiError as exc:
                return _error(exc.status, exc.message)
        return inner
    return wrap


# -- contacts -----------------------------------------------------------------


def _parse(identifier):
    kind, _, value = (identifier or "").partition(":")
    kind, value = kind.strip().lower(), value.strip()
    if kind not in ("phone", "id", "email") or not value:
        raise ApiError(400, "The identifier must look like 'phone:+27821234567', 'id:123' or 'email:a@b.co'.")
    return kind, value


def _find(workspace, identifier):
    kind, value = _parse(identifier)
    contacts = Contact.objects.for_workspace(workspace)
    if kind == "id":
        return contacts.filter(pk=value).first() if value.isdigit() else None
    if kind == "email":
        return contacts.filter(attributes__email__iexact=value).first()
    msisdn = normalise_msisdn(value)
    if len(msisdn) < 10:
        raise ApiError(400, f"'{value}' is not a usable mobile number.")
    return contacts.filter(wa_id=msisdn).first()


def _phone_for(identifier, body):
    kind, value = _parse(identifier)
    msisdn = normalise_msisdn(value if kind == "phone" else body.get("phone") or "")
    if len(msisdn) < 10:
        raise ApiError(400, "A WhatsApp contact needs a mobile number: use a 'phone:' identifier or send 'phone'.")
    return msisdn


def _describe(contact):
    attributes = contact.attributes or {}
    return {
        "id": contact.pk,
        "firstName": attributes.get("first_name", ""),
        "lastName": attributes.get("last_name", ""),
        "phone": contact.pretty_number,
        "email": attributes.get("email", ""),
        "language": contact.locale,
        "custom_fields": [{"name": k, "value": v} for k, v in attributes.items()
                          if k not in ("first_name", "last_name", "email")],
        "optedOut": contact.is_opted_out,
    }


@endpoint(["POST"])
def contact_create_or_update(request, key, body, identifier):
    workspace = key.workspace
    contact = _find(workspace, identifier)
    created = contact is None
    if created:
        contact = Contact.objects.create(workspace=workspace, wa_id=_phone_for(identifier, body))

    attributes = dict(contact.attributes or {})
    first = str(body.get("firstName") or "").strip()
    last = str(body.get("lastName") or "").strip()
    if first:
        attributes["first_name"] = first[:60]
    if last:
        attributes["last_name"] = last[:60]
    kind, value = _parse(identifier)
    email = body.get("email") or (value if kind == "email" else "")
    if email:
        attributes["email"] = str(email).strip()[:254]
    for field in body.get("custom_fields") or []:
        if isinstance(field, dict) and str(field.get("name") or "").strip():
            attributes[str(field["name"]).strip()[:60]] = field.get("value")
    contact.attributes = attributes
    if first or last:
        contact.display_name = " ".join(p for p in (first or attributes.get("first_name", ""),
                                                    last or attributes.get("last_name", "")) if p)[:120]
    if body.get("language"):
        contact.locale = str(body["language"])[:12]
    contact.save()
    audit("api.contact_saved", workspace=workspace, target=contact, api_key=key.name, created=created)
    return JsonResponse({"contactId": contact.pk})


@endpoint(["GET"])
def contact_get(request, key, body, identifier):
    contact = _find(key.workspace, identifier)
    if contact is None:
        raise ApiError(404, "No contact with that identifier in this workspace.")
    return JsonResponse(_describe(contact))


# -- messages -----------------------------------------------------------------


def _channel(workspace, channel_id):
    from apps.channels_wa.models import WhatsAppChannel
    from apps.library.bulk import resolve_channel

    if channel_id in (None, ""):
        channel = resolve_channel(workspace)
    else:
        channel = (
            WhatsAppChannel.objects.for_workspace(workspace).filter(pk=channel_id, is_active=True).first()
            if str(channel_id).isdigit() else None
        )
        if channel is None:
            raise ApiError(400, f"channelId {channel_id} is not an active WhatsApp number in this workspace.")
    if channel is None:
        raise ApiError(400, "This workspace has no active WhatsApp number.")
    return channel


def _open_conversation(workspace, contact):
    from apps.inbox.models import Conversation

    return (
        Conversation.objects.for_workspace(workspace)
        .filter(contact=contact, status__in=Conversation.OPEN_STATUSES)
        .order_by("-last_activity_at")
        .first()
    )


def _conversation(workspace, channel, contact):
    from apps.inbox.models import Conversation

    conversation = _open_conversation(workspace, contact)
    if conversation is None:
        conversation = Conversation.objects.create(
            workspace=workspace, channel=channel, contact=contact, status=Conversation.Status.BOT
        )
    return conversation


def _template_parts(template_body):
    """Split respond.io-style components into (body values, header media, other components)."""
    values, header_media, extra = [], None, []
    for component in template_body.get("components") or []:
        if not isinstance(component, dict):
            continue
        kind = str(component.get("type") or "").lower()
        parameters = component.get("parameters") or []
        if kind == "body":
            values = [str(p.get("text", "")) for p in parameters if isinstance(p, dict)]
        elif kind == "header":
            for p in parameters:
                media_kind = str((p or {}).get("type") or "").lower()
                media = (p or {}).get(media_kind) if media_kind in ("image", "video", "document") else None
                if isinstance(media, dict) and (media.get("link") or media.get("id")):
                    header_media = {k: media[k] for k in ("link", "id", "filename") if media.get(k)}
        elif kind == "button":
            extra.append({k: component[k] for k in ("type", "sub_type", "index", "parameters") if k in component})
    return values, header_media, extra


def _prepare_template(channel, template_body):
    """Check a template request fully before anything is created or sent."""
    from apps.library.models import MessageTemplate

    name = str(template_body.get("name") or "").strip()
    language = str(template_body.get("languageCode") or "").strip()
    if not name or not language:
        raise ApiError(400, "A template needs 'name' and 'languageCode'.")
    approved = MessageTemplate.objects.for_workspace(channel.workspace).filter(
        channel=channel, name=name, status=MessageTemplate.Status.APPROVED
    )
    template = approved.filter(language=language).first()
    if template is None:
        others = sorted(approved.values_list("language", flat=True))
        if others:
            raise ApiError(400, f"'{name}' is approved in {', '.join(others)}, not '{language}'. The language must "
                                "match WhatsApp exactly (en_US is not en).")
        raise ApiError(400, f"There is no approved template called '{name}' on this number.")
    values, header_media, extra = _template_parts(template_body)
    expected = template.variable_count
    if expected and len(values) != expected:
        raise ApiError(400, f"'{name}' takes {expected} body value(s); {len(values)} were sent.")
    if template.header_format in ("IMAGE", "VIDEO", "DOCUMENT") and not header_media:
        raise ApiError(400, f"'{name}' has a {template.header_format.lower()} header: send it as a header "
                            "component with a link.")
    return template, values, header_media, extra


def _check_free_message(kind, message):
    if kind == "text":
        text = str(message.get("text") or "").strip()
        if not text:
            raise ApiError(400, "A text message needs 'text'.")
        if len(text) > 4096:
            raise ApiError(400, "WhatsApp allows at most 4096 characters in a message.")
        return {"text": text}
    attachment = message.get("attachment") or {}
    url = str(attachment.get("url") or "")
    media_kind = MEDIA_KINDS.get(str(attachment.get("type") or "file").lower())
    if not url.startswith("https://") or media_kind is None:
        raise ApiError(400, "An attachment needs a 'type' (image, video, audio or file) and an https 'url'.")
    return {"url": url, "kind": media_kind, "caption": str(attachment.get("description") or "")[:1024],
            "filename": str(attachment.get("fileName") or "")[:255]}


@endpoint(["POST"])
def contact_message(request, key, body, identifier):
    from apps.channels_wa import outbound
    from apps.inbox.models import Message
    from apps.workflows.engine import stop

    workspace = key.workspace
    message = body.get("message")
    if not isinstance(message, dict) or not message.get("type"):
        raise ApiError(400, "Send a 'message' object with a 'type': text, attachment or whatsapp_template.")
    kind = message["type"]
    if kind not in ("text", "attachment", "whatsapp_template"):
        raise ApiError(400, f"Message type '{kind}' is not supported. Use text, attachment or whatsapp_template.")

    channel = _channel(workspace, body.get("channelId"))
    if kind == "whatsapp_template":
        prepared = _prepare_template(channel, message.get("template") or {})
    else:
        prepared = _check_free_message(kind, message)
    contact = _find(workspace, identifier)
    if contact is None:
        contact = Contact(workspace=workspace, wa_id=_phone_for(identifier, body))
    if kind != "whatsapp_template":
        conversation = _open_conversation(workspace, contact) if contact.pk else None
        if conversation is None or not conversation.window_open:
            raise ApiError(400, "This person has not messaged in the last 24 hours, so WhatsApp only allows an "
                                "approved template (type 'whatsapp_template').")
    else:
        if contact.pk is None:
            contact.save()
        conversation = _conversation(workspace, channel, contact)

    # A message from another system answers for us: any menu the bot was
    # walking this person through would now be out of date.
    stop(conversation, f"a message was sent through the API ({key.name})")
    tag = {"auto": "api", "api_key": key.name}
    if kind == "text":
        sent = outbound.send_text(conversation, prepared["text"], actor=Message.Actor.BOT, payload=tag)
    elif kind == "attachment":
        sent = outbound.send_media_link(conversation, prepared["url"], kind=prepared["kind"],
                                        caption=prepared["caption"], filename=prepared["filename"], payload=tag)
    else:
        template, values, header_media, extra = prepared
        sent = outbound.send_template(conversation, template, values, header_media=header_media,
                                      actor=Message.Actor.BOT, extra_components=extra, payload=tag)

    audit("api.message_sent", workspace=workspace, target=contact, api_key=key.name, type=kind,
          status=sent.wa_status)
    answer = {"contactId": contact.pk, "messageId": str(sent.pk), "status": sent.wa_status}
    if sent.wa_status == Message.Status.BLOCKED:
        answer["message"] = sent.wa_error.get("reason", "Not sent.")
        return JsonResponse(answer, status=422)
    if sent.wa_status == Message.Status.FAILED:
        answer["message"] = sent.wa_error.get("reason", "WhatsApp did not accept the message.")
        return JsonResponse(answer, status=502)
    return JsonResponse(answer)
