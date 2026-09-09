"""Analytics: how sending is going, and where it is not.

Everything here is read-only and derived from the message rows that receipts
keep updating, so a bulk send's delivered and read figures keep improving for
days after it ran. Nothing is cached; the queries are cheap at this scale and
a stale number on this page costs more than the query.
"""

import datetime as dt
import statistics
from collections import Counter

from django.contrib.auth.decorators import login_required
from django.db.models import Count, Exists, F, OuterRef, Q
from django.db.models.functions import TruncDate
from django.shortcuts import redirect, render
from django.utils import timezone

from apps.contacts.models import Contact
from apps.core.scoping import require_role, scoped_get_or_404
from apps.inbox.models import Conversation, Message
from apps.library.models import BulkSend
from apps.workspaces.models import WorkspaceMembership

PERIODS = [(7, "Last 7 days"), (30, "Last 30 days"), (90, "Last 90 days")]

# A message counts as sent once WhatsApp accepted it; delivered and read are
# later stages of the same message, so each bucket includes the ones after it.
SENT = [Message.Status.SENT, Message.Status.DELIVERED, Message.Status.READ]
DELIVERED = [Message.Status.DELIVERED, Message.Status.READ]
READ = [Message.Status.READ]

REPLIED = Exists(
    Message.objects.filter(
        conversation=OuterRef("conversation"),
        direction=Message.Direction.IN,
        created_at__gt=OuterRef("created_at"),
    )
)


def _percent(part, whole):
    return round(part * 100 / whole) if whole else 0


def _period(request):
    try:
        days = int(request.GET.get("days", 30))
    except (TypeError, ValueError):
        days = 30
    if days not in dict(PERIODS):
        days = 30
    return days, timezone.now() - dt.timedelta(days=days)


def _outcome_counts(outbound):
    """Sent / delivered / read / failed / blocked for a queryset of outbound messages."""
    row = outbound.aggregate(
        total=Count("id"),
        sent=Count("id", filter=Q(wa_status__in=SENT)),
        delivered=Count("id", filter=Q(wa_status__in=DELIVERED)),
        read=Count("id", filter=Q(wa_status__in=READ)),
        failed=Count("id", filter=Q(wa_status=Message.Status.FAILED)),
        blocked=Count("id", filter=Q(wa_status=Message.Status.BLOCKED)),
        queued=Count("id", filter=Q(wa_status=Message.Status.QUEUED)),
    )
    row["replied"] = outbound.annotate(replied=REPLIED).filter(replied=True).count()
    row["delivered_pct"] = _percent(row["delivered"], row["sent"])
    row["read_pct"] = _percent(row["read"], row["sent"])
    row["replied_pct"] = _percent(row["replied"], row["sent"])
    row["failed_pct"] = _percent(row["failed"], row["total"])
    row["funnel"] = [
        ("Sent", row["sent"], 100 if row["sent"] else 0),
        ("Delivered", row["delivered"], row["delivered_pct"]),
        ("Read", row["read"], row["read_pct"]),
        ("Replied", row["replied"], row["replied_pct"]),
    ]
    return row


def batch_stats(batch):
    return _outcome_counts(batch.messages.all())


def _daily(messages, days, now):
    """Outbound and inbound counts per day, every day present even when zero."""
    rows = (
        messages.annotate(day=TruncDate("created_at"))
        .values("day", "direction")
        .annotate(n=Count("id"))
    )
    counts = {}
    for row in rows:
        counts.setdefault(row["day"], {"out": 0, "in": 0})
        key = "out" if row["direction"] == Message.Direction.OUT else "in"
        counts[row["day"]][key] += row["n"]

    today = timezone.localdate(now)
    series = []
    for offset in range(days - 1, -1, -1):
        day = today - dt.timedelta(days=offset)
        entry = counts.get(day, {"out": 0, "in": 0})
        series.append({"day": day, "out": entry["out"], "in": entry["in"]})
    peak = max((max(e["out"], e["in"]) for e in series), default=0) or 1
    for entry in series:
        entry["out_pct"] = round(entry["out"] * 100 / peak)
        entry["in_pct"] = round(entry["in"] * 100 / peak)
    # Label roughly every week so the axis stays readable at 90 days.
    step = 1 if days <= 7 else 7
    for index, entry in enumerate(series):
        entry["label"] = entry["day"].strftime("%d %b") if (len(series) - 1 - index) % step == 0 else ""
    return series, peak


def _median_first_response(conversations, since):
    minutes = [
        (answered - opened).total_seconds() / 60
        for opened, answered in conversations.filter(first_response_at__gte=since).values_list(
            "created_at", "first_response_at"
        )
        if answered and opened and answered >= opened
    ]
    if not minutes:
        return None
    return round(statistics.median(minutes))


def _humanise_minutes(minutes):
    if minutes is None:
        return "No replies yet"
    if minutes < 1:
        return "Under a minute"
    if minutes < 60:
        return f"{minutes} min"
    hours, rest = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {rest:02d}m"
    return f"{hours // 24}d {hours % 24}h"


def _template_rows(outbound):
    rows = list(
        outbound.filter(kind=Message.Kind.TEMPLATE)
        .values("template_id", "template__name")
        .annotate(
            total=Count("id"),
            sent=Count("id", filter=Q(wa_status__in=SENT)),
            delivered=Count("id", filter=Q(wa_status__in=DELIVERED)),
            read=Count("id", filter=Q(wa_status__in=READ)),
            failed=Count("id", filter=Q(wa_status=Message.Status.FAILED)),
            blocked=Count("id", filter=Q(wa_status=Message.Status.BLOCKED)),
        )
        .order_by("-total")
    )
    for row in rows:
        row["name"] = row["template__name"] or "Template no longer in the library"
        row["replied"] = (
            outbound.filter(template_id=row["template_id"], wa_status__in=SENT)
            .annotate(replied=REPLIED)
            .filter(replied=True)
            .count()
        )
        row["delivered_pct"] = _percent(row["delivered"], row["sent"])
        row["read_pct"] = _percent(row["read"], row["sent"])
        row["replied_pct"] = _percent(row["replied"], row["sent"])
    return rows


def _failure_rows(outbound):
    reasons = Counter()
    for message in outbound.filter(wa_status=Message.Status.FAILED).only("wa_error")[:1000]:
        reasons[message.failure_explanation] += 1
    total = sum(reasons.values())
    return [
        {"reason": reason, "count": count, "pct": _percent(count, total)}
        for reason, count in reasons.most_common(8)
    ]


def _agent_rows(outbound, conversations, since):
    replies = Counter(
        dict(
            outbound.filter(actor=Message.Actor.AGENT, author__isnull=False)
            .values_list("author_id")
            .annotate(n=Count("id"))
        )
    )
    resolved = Counter(
        dict(
            conversations.filter(resolved_at__gte=since, resolved_by__isnull=False)
            .values_list("resolved_by_id")
            .annotate(n=Count("id"))
        )
    )
    from django.contrib.auth import get_user_model

    users = get_user_model().objects.filter(pk__in=set(replies) | set(resolved))
    rows = [
        {"name": user.display_name, "replies": replies.get(user.pk, 0), "resolved": resolved.get(user.pk, 0)}
        for user in users
    ]
    return sorted(rows, key=lambda r: (-r["replies"], -r["resolved"], r["name"]))


@login_required
def overview(request):
    if request.workspace is None:
        return redirect("workspaces:list")
    require_role(request, *WorkspaceMembership.SUPERVISE_ROLES)

    days, since = _period(request)
    now = timezone.now()

    messages = Message.objects.for_request(request).filter(created_at__gte=since)
    outbound = messages.filter(direction=Message.Direction.OUT)
    conversations = Conversation.objects.for_request(request)

    totals = _outcome_counts(outbound)
    totals["inbound"] = messages.filter(direction=Message.Direction.IN).count()
    totals["new_chats"] = conversations.filter(created_at__gte=since).count()
    totals["resolved"] = conversations.filter(resolved_at__gte=since).count()
    totals["consultant_requests"] = messages.filter(
        payload__escalation="human_requested"
    ).count()
    totals["opt_outs"] = Contact.objects.for_request(request).filter(opted_out_at__gte=since).count()
    # Open chats where the customer spoke last: the queue a person needs to clear.
    totals["waiting_now"] = (
        conversations.filter(status__in=Conversation.OPEN_STATUSES, last_inbound_at__isnull=False)
        .filter(Q(last_outbound_at__isnull=True) | Q(last_outbound_at__lt=F("last_inbound_at")))
        .count()
    )
    first_response = _median_first_response(conversations, since)

    daily, peak = _daily(messages, days, now)

    batches = list(
        BulkSend.objects.for_request(request).select_related("template", "created_by")[:25]
    )
    for batch in batches:
        batch.stats = batch_stats(batch)

    return render(
        request,
        "reporting/overview.html",
        {
            "days": days,
            "periods": PERIODS,
            "since": since,
            "totals": totals,
            "first_response": _humanise_minutes(first_response),
            "daily": daily,
            "peak": peak,
            "batches": batches,
            "template_rows": _template_rows(outbound),
            "failure_rows": _failure_rows(outbound),
            "agent_rows": _agent_rows(outbound, conversations, since),
        },
    )


@login_required
def bulk_send(request, pk):
    if request.workspace is None:
        return redirect("workspaces:list")
    # Whoever may press send may watch the result, whatever their role.
    if not (request.membership and request.membership.may_send_bulk):
        require_role(request, *WorkspaceMembership.SUPERVISE_ROLES)
    batch = scoped_get_or_404(BulkSend, request, pk=pk)

    show = request.GET.get("show") or "all"
    recipients = (
        batch.messages.select_related("conversation__contact")
        .annotate(replied=REPLIED)
        .order_by("created_at")
    )
    if show == "failed":
        recipients = recipients.filter(
            wa_status__in=[Message.Status.FAILED, Message.Status.BLOCKED]
        )
    elif show == "replied":
        recipients = recipients.filter(replied=True)
    elif show == "unread":
        recipients = recipients.filter(wa_status__in=[Message.Status.SENT, Message.Status.DELIVERED])
    elif show == "read":
        recipients = recipients.filter(wa_status=Message.Status.READ)

    return render(
        request,
        "reporting/bulk_send.html",
        {
            "batch": batch,
            "stats": batch_stats(batch),
            "recipients": recipients[:1000],
            "show": show,
            "filters": [
                ("all", "Everyone"),
                ("read", "Read"),
                ("unread", "Not read yet"),
                ("replied", "Replied"),
                ("failed", "Not delivered"),
            ],
        },
    )
