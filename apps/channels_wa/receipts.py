"""Delivery and read receipts from Meta."""

import datetime as dt
import logging

from django.utils import timezone

from apps.inbox import events
from apps.inbox.models import Message

logger = logging.getLogger(__name__)


def process_statuses(channel, statuses):
    for status in statuses or []:
        wamid = status.get("id")
        state = status.get("status")
        if not wamid or not state:
            continue
        message = (
            Message.objects.for_workspace(channel.workspace)
            .filter(wamid=wamid)
            .select_related("conversation")
            .first()
        )
        if message is None:
            continue
        try:
            at = dt.datetime.fromtimestamp(int(status.get("timestamp", 0)), tz=dt.timezone.utc)
        except (TypeError, ValueError):
            at = timezone.now()
        if state == "failed":
            message.wa_error = (status.get("errors") or [{}])[0]
        if message.advance_status(state, at):
            message.save(
                update_fields=["wa_status", "sent_at", "delivered_at", "read_at", "wa_error"]
            )
            events.conversation_changed(message.conversation, "status")
