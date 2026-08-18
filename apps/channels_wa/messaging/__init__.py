from .base import MessagingChannel, SendResult, TransportError
from .meta_cloud import MetaCloudChannel


def get_channel_client(channel):
    """Return the transport for a WhatsAppChannel.

    Only Meta Cloud API exists today. A BSP (360dialog, Twilio, Infobip) is
    added here as a second class implementing MessagingChannel - no business
    logic anywhere else needs to change.
    """
    return MetaCloudChannel(channel)


__all__ = [
    "MessagingChannel",
    "MetaCloudChannel",
    "SendResult",
    "TransportError",
    "get_channel_client",
]
