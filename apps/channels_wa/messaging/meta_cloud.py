"""Meta WhatsApp Cloud API transport.

Every outbound call in the platform funnels through this class - it is the one
place that knows about graph.facebook.com. Patterns carried over from the
Wellniciti implementation: the comms guard runs inside _post, one redacted log
line per call, and friendly error text for the UI instead of raw Graph errors.
"""

import logging
import time

import requests
from django.conf import settings

from apps.channels_wa.comms_guard import outbound_blocked
from apps.core.logging_filters import mask

from .base import MessagingChannel, SendResult, TransportError

logger = logging.getLogger(__name__)

FRIENDLY_ERRORS = {
    131047: "This chat has been quiet for more than 24 hours, so WhatsApp only allows an approved template.",
    131026: "WhatsApp could not deliver to that number. Check it is a real WhatsApp user.",
    132000: "The template's placeholders do not match what was approved.",
    132001: "That template does not exist for this number, or the language does not match.",
    131056: "Too many messages to this number too quickly. Try again shortly.",
    100: "WhatsApp rejected the request. Check the number and template details.",
    190: "The access token has expired or been revoked. Reconnect the number.",
}


class MetaCloudChannel(MessagingChannel):
    @property
    def base_url(self):
        version = self.channel.graph_version or settings.WHATSAPP_GRAPH_VERSION
        return f"https://graph.facebook.com/{version}"

    @property
    def _headers(self):
        return {
            "Authorization": f"Bearer {self.channel.access_token}",
            "Content-Type": "application/json",
        }

    # -- plumbing ---------------------------------------------------------

    def _request(self, method, url, *, json=None, data=None, files=None, params=None, timeout=None):
        started = time.monotonic()
        try:
            response = requests.request(
                method,
                url,
                headers=(
                    {"Authorization": f"Bearer {self.channel.access_token}"}
                    if files or data
                    else self._headers
                ),
                json=json,
                data=data,
                files=files,
                params=params,
                timeout=timeout or settings.WHATSAPP_TIMEOUT,
            )
        except requests.RequestException as exc:
            logger.warning(
                "whatsapp upstream unreachable channel=%s url=%s err=%s",
                self.channel.pk,
                url.split("?")[0],
                exc.__class__.__name__,
            )
            raise TransportError(
                str(exc), friendly="We could not reach WhatsApp just now. Please try again."
            ) from exc

        duration = int((time.monotonic() - started) * 1000)
        logger.info(
            "whatsapp %s %s status=%s ms=%s channel=%s token=%s",
            method,
            url.split("graph.facebook.com")[-1].split("?")[0],
            response.status_code,
            duration,
            self.channel.pk,
            mask(self.channel.access_token),
        )
        if response.status_code >= 400:
            body = self._safe_json(response)
            error = (body or {}).get("error", {})
            code = error.get("code")
            raise TransportError(
                error.get("message", response.text[:300]),
                status=response.status_code,
                body=body,
                friendly=FRIENDLY_ERRORS.get(
                    code, error.get("error_user_msg") or error.get("message") or None
                ),
            )
        return response

    @staticmethod
    def _safe_json(response):
        try:
            return response.json()
        except ValueError:
            return {}

    def _post_message(self, to, payload):
        blocked = outbound_blocked(to)
        if blocked:
            logger.info("outbound blocked to=%s reason=%s", mask(str(to), 6), blocked)
            return SendResult(ok=False, blocked_reason=blocked)

        url = f"{self.base_url}/{self.channel.phone_number_id}/messages"
        body = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": to, **payload}
        try:
            response = self._request("POST", url, json=body)
        except TransportError as exc:
            return SendResult(ok=False, error=exc.friendly, raw=exc.body or {})
        data = self._safe_json(response)
        wamid = (data.get("messages") or [{}])[0].get("id", "")
        return SendResult(ok=True, wamid=wamid, raw=data)

    # -- sending ----------------------------------------------------------

    def send_text(self, to, body, *, preview_url=False):
        return self._post_message(
            to, {"type": "text", "text": {"body": body, "preview_url": preview_url}}
        )

    def send_template(self, to, template_name, language, components=None):
        template = {"name": template_name, "language": {"code": language}}
        if components:
            template["components"] = components
        return self._post_message(to, {"type": "template", "template": template})

    def send_media(self, to, media_id_or_url, *, kind="image", caption="", filename=""):
        key = "id" if not str(media_id_or_url).startswith("http") else "link"
        media = {key: media_id_or_url}
        if caption and kind in {"image", "video", "document"}:
            media["caption"] = caption
        if filename and kind == "document":
            media["filename"] = filename
        return self._post_message(to, {"type": kind, kind: media})

    def send_buttons(self, to, body, buttons, *, header="", footer=""):
        interactive = {
            "type": "button",
            "body": {"text": body},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": b["id"], "title": b["title"][:20]}}
                    for b in buttons[:3]
                ]
            },
        }
        if header:
            interactive["header"] = {"type": "text", "text": header[:60]}
        if footer:
            interactive["footer"] = {"text": footer[:60]}
        return self._post_message(to, {"type": "interactive", "interactive": interactive})

    def send_list(self, to, body, button_text, sections, *, header="", footer=""):
        interactive = {
            "type": "list",
            "body": {"text": body},
            "action": {"button": button_text[:20], "sections": sections},
        }
        if header:
            interactive["header"] = {"type": "text", "text": header[:60]}
        if footer:
            interactive["footer"] = {"text": footer[:60]}
        return self._post_message(to, {"type": "interactive", "interactive": interactive})

    def mark_read(self, wamid):
        url = f"{self.base_url}/{self.channel.phone_number_id}/messages"
        try:
            self._request(
                "POST",
                url,
                json={"messaging_product": "whatsapp", "status": "read", "message_id": wamid},
            )
            return True
        except TransportError:
            return False

    # -- media ------------------------------------------------------------

    def upload_media(self, file_obj, mime_type):
        url = f"{self.base_url}/{self.channel.phone_number_id}/media"
        response = self._request(
            "POST",
            url,
            data={"messaging_product": "whatsapp", "type": mime_type},
            files={"file": (getattr(file_obj, "name", "upload"), file_obj, mime_type)},
        )
        return self._safe_json(response).get("id", "")

    def download_media(self, media_id):
        """Two hops: look up the URL, then fetch it with the same bearer token."""
        meta = self._safe_json(self._request("GET", f"{self.base_url}/{media_id}"))
        url = meta.get("url")
        if not url:
            raise TransportError("Media URL missing", body=meta)
        response = self._request("GET", url, timeout=60)
        return response.content, meta.get("mime_type", ""), meta.get("sha256", "")

    # -- account ----------------------------------------------------------

    def fetch_number_profile(self):
        """Used by the connect wizard to prove the credentials actually work."""
        response = self._request(
            "GET",
            f"{self.base_url}/{self.channel.phone_number_id}",
            params={"fields": "display_phone_number,verified_name,quality_rating,messaging_limit_tier"},
        )
        return self._safe_json(response)

    def create_template(self, payload):
        """Submit a template for review. Templates are normally authored in
        WhatsApp Manager; this exists so a whole campaign's worth can be
        submitted from version-controlled definitions in one go."""
        if not self.channel.waba_id:
            raise TransportError(
                "No WhatsApp Business Account ID",
                friendly="Add the WhatsApp Business Account ID before submitting templates.",
            )
        response = self._request(
            "POST", f"{self.base_url}/{self.channel.waba_id}/message_templates", json=payload
        )
        return self._safe_json(response)

    def upload_sample_media(self, path, app_id, mime_type):
        """Resumable upload, returning the handle a media header needs for review.

        Two steps: open a session against the app, then send the bytes. The
        handle it returns goes in example.header_handle, not the media id.
        """
        import os

        size = os.path.getsize(path)
        session = self._safe_json(
            self._request(
                "POST",
                f"{self.base_url}/{app_id}/uploads",
                params={"file_length": size, "file_type": mime_type},
            )
        )
        session_id = session.get("id", "")
        if not session_id:
            raise TransportError("Upload session failed", body=session)

        with open(path, "rb") as handle:
            data = handle.read()
        import requests as _requests

        upload = _requests.post(
            f"{self.base_url}/{session_id}",
            headers={
                "Authorization": f"OAuth {self.channel.access_token}",
                "file_offset": "0",
                "Content-Type": "application/octet-stream",
            },
            data=data,
            timeout=120,
        )
        if upload.status_code >= 400:
            raise TransportError("Sample upload failed", status=upload.status_code, body=self._safe_json(upload))
        return self._safe_json(upload).get("h", "")

    def fetch_templates(self):
        if not self.channel.waba_id:
            raise TransportError(
                "No WhatsApp Business Account ID",
                friendly="Add the WhatsApp Business Account ID to sync templates.",
            )
        templates, url = [], f"{self.base_url}/{self.channel.waba_id}/message_templates"
        params = {"limit": 100}
        while url:
            data = self._safe_json(self._request("GET", url, params=params))
            templates.extend(data.get("data", []))
            url = (data.get("paging") or {}).get("next")
            params = None
        return templates
