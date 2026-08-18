from dataclasses import dataclass, field


class TransportError(Exception):
    """A send or fetch failed at the provider."""

    def __init__(self, message, *, status=None, body=None, friendly=None):
        super().__init__(message)
        self.status = status
        self.body = body
        self.friendly = friendly or "WhatsApp could not accept that message."


@dataclass
class SendResult:
    ok: bool
    wamid: str = ""
    blocked_reason: str = ""
    error: str = ""
    raw: dict = field(default_factory=dict)


class MessagingChannel:
    """The interface every WhatsApp transport implements."""

    def __init__(self, channel):
        self.channel = channel

    def send_text(self, to, body, *, preview_url=False):
        raise NotImplementedError

    def send_template(self, to, template_name, language, components=None):
        raise NotImplementedError

    def send_media(self, to, media_id_or_url, *, kind="image", caption="", filename=""):
        raise NotImplementedError

    def send_buttons(self, to, body, buttons, *, header="", footer=""):
        raise NotImplementedError

    def send_list(self, to, body, button_text, sections, *, header="", footer=""):
        raise NotImplementedError

    def mark_read(self, wamid):
        raise NotImplementedError

    def upload_media(self, file_obj, mime_type):
        raise NotImplementedError

    def download_media(self, media_id):
        raise NotImplementedError

    def fetch_number_profile(self):
        raise NotImplementedError

    def fetch_templates(self):
        raise NotImplementedError
