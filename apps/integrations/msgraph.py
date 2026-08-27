"""Microsoft Graph, app-only.

Used to send the invitation email that carries each guest's personal wa.me
link. That link is what makes the guest message us first, which opens their
free 24 hour window - so the email is not decoration, it is the cheapest part
of the cost model.

Credentials are client-credentials (application permissions, Mail.Send), read
from the environment. One redacted log line per call, never the token.
"""

import base64
import logging
import time

import requests
from django.conf import settings

logger = logging.getLogger(__name__)


class GraphError(Exception):
    def __init__(self, message, *, status=None, body=None, friendly=None):
        super().__init__(message)
        self.status = status
        self.body = body
        self.friendly = friendly or "Microsoft Graph would not accept that."


class GraphClient:
    def __init__(self, tenant_id=None, client_id=None, client_secret=None, sender=None):
        self.tenant_id = tenant_id or settings.MS_GRAPH_TENANT_ID
        self.client_id = client_id or settings.MS_GRAPH_CLIENT_ID
        self.client_secret = client_secret or settings.MS_GRAPH_CLIENT_SECRET
        self.sender = sender or settings.MS_GRAPH_SENDER
        self.base_url = getattr(settings, "MS_GRAPH_BASE_URL", "https://graph.microsoft.com/v1.0")
        self._token = None
        self._token_expires = 0

    @property
    def configured(self):
        return all([self.tenant_id, self.client_id, self.client_secret, self.sender])

    # -- auth ---------------------------------------------------------------

    def _access_token(self):
        if self._token and time.time() < self._token_expires - 60:
            return self._token
        # Built from the tenant id rather than MS_GRAPH_AUTHORITY, which ships
        # with a {MS_GRAPH_TENANT_ID} placeholder that nothing substitutes.
        url = f"https://login.microsoftonline.com/{self.tenant_id}/oauth2/v2.0/token"
        response = requests.post(
            url,
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "scope": getattr(
                    settings, "MS_GRAPH_SCOPES", "https://graph.microsoft.com/.default"
                ),
                "grant_type": "client_credentials",
            },
            timeout=30,
        )
        if response.status_code >= 400:
            body = _safe_json(response)
            logger.warning("graph token request failed status=%s", response.status_code)
            raise GraphError(
                body.get("error_description", "token request failed"),
                status=response.status_code,
                body=body,
                friendly="Microsoft would not issue a token. Check the tenant, client id "
                "and secret, and that the secret has not expired.",
            )
        payload = response.json()
        self._token = payload["access_token"]
        self._token_expires = time.time() + int(payload.get("expires_in", 3600))
        return self._token

    # -- mail ---------------------------------------------------------------

    def send_mail(self, to, subject, html, *, reply_to=None, save_to_sent=True, attachments=None):
        """attachments: [(filename, mime_type, bytes), ...]

        Graph only takes inline base64 up to about 3 MB per message. A calendar
        file is under a kilobyte, so the simple path is the right one here; if
        anything larger is ever attached this will need an upload session.
        """
        started = time.monotonic()
        url = f"{self.base_url}/users/{self.sender}/sendMail"
        message = {
            "subject": subject,
            "body": {"contentType": "HTML", "content": html},
            "toRecipients": [{"emailAddress": {"address": to}}],
        }
        if reply_to:
            message["replyTo"] = [{"emailAddress": {"address": reply_to}}]
        if attachments:
            message["attachments"] = [
                {
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "name": name,
                    "contentType": mime,
                    "contentBytes": base64.b64encode(content).decode("ascii"),
                }
                for name, mime, content in attachments
            ]

        response = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {self._access_token()}",
                "Content-Type": "application/json",
            },
            json={"message": message, "saveToSentItems": save_to_sent},
            timeout=60,
        )
        logger.info(
            "graph sendMail to=%s status=%s ms=%s",
            _mask_email(to),
            response.status_code,
            int((time.monotonic() - started) * 1000),
        )
        if response.status_code >= 400:
            body = _safe_json(response)
            error = (body.get("error") or {}).get("message", "")
            raise GraphError(
                error or "sendMail failed",
                status=response.status_code,
                body=body,
                friendly=_friendly_mail_error(response.status_code, error),
            )
        return True

    def whoami(self):
        """Cheap check that credentials work and the mailbox is reachable."""
        response = requests.get(
            f"{self.base_url}/users/{self.sender}",
            headers={"Authorization": f"Bearer {self._access_token()}"},
            timeout=30,
        )
        if response.status_code >= 400:
            body = _safe_json(response)
            raise GraphError(
                "mailbox lookup failed",
                status=response.status_code,
                body=body,
                friendly=_friendly_mail_error(
                    response.status_code, (body.get("error") or {}).get("message", "")
                ),
            )
        return response.json()


def _friendly_mail_error(status, error):
    if status == 403:
        return (
            "Microsoft refused: the app registration probably lacks the Mail.Send "
            "application permission, or admin consent was never granted."
        )
    if status == 404:
        return "That sender mailbox does not exist in the tenant."
    if status == 401:
        return "The token was rejected. Check the client secret has not expired."
    return error or "Microsoft Graph would not accept that."


def _safe_json(response):
    try:
        return response.json()
    except ValueError:
        return {}


def _mask_email(address):
    name, _, domain = (address or "").partition("@")
    return f"{name[:2]}***@{domain}" if domain else "***"
