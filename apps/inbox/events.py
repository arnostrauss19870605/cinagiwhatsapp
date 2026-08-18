"""Tiny real-time signals.

We never push HTML over the websocket - only a "something changed" ping. The
browser then refetches the HTMX fragment it cares about. That keeps the socket
cheap, and a Redis blip degrades to the 8 second poll instead of breaking the
inbox. Broadcasting never raises.
"""

import logging

logger = logging.getLogger(__name__)


def workspace_group(workspace_id):
    return f"inbox_{workspace_id}"


def conversation_group(conversation_id):
    return f"conv_{str(conversation_id).replace('-', '')}"


def _send(group, payload):
    try:
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer

        layer = get_channel_layer()
        if layer is None:
            return
        async_to_sync(layer.group_send)(group, {"type": "inbox.event", "payload": payload})
    except Exception:  # pragma: no cover - realtime is a nicety, never a blocker
        logger.debug("realtime broadcast failed for group=%s", group, exc_info=True)


def conversation_changed(conversation, kind="update"):
    _send(
        workspace_group(conversation.workspace_id),
        {"event": kind, "conversation_id": str(conversation.pk)},
    )
    _send(
        conversation_group(conversation.pk),
        {"event": kind, "conversation_id": str(conversation.pk)},
    )
