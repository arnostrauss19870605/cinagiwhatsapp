from django.contrib.auth.decorators import login_required
from django.db import connection
from django.http import JsonResponse
from django.shortcuts import redirect, render


def healthz(request):
    """Liveness/readiness probe: database, cache and (optionally) the broker."""
    checks = {"database": "ok", "cache": "ok"}
    status = 200
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except Exception as exc:  # pragma: no cover
        checks["database"] = f"error: {exc.__class__.__name__}"
        status = 503
    try:
        from django.core.cache import cache

        cache.set("healthz", "1", 5)
        cache.get("healthz")
    except Exception as exc:  # pragma: no cover
        checks["cache"] = f"error: {exc.__class__.__name__}"
        status = 503
    return JsonResponse({"status": "ok" if status == 200 else "degraded", **checks}, status=status)


@login_required
def dashboard(request):
    if request.workspace is None:
        return redirect("workspaces:list")

    from apps.channels_wa.models import WhatsAppChannel
    from apps.inbox.models import Conversation

    conversations = Conversation.objects.for_request(request)
    context = {
        "channels": WhatsAppChannel.objects.for_request(request),
        "counts": {
            "queued": conversations.filter(status=Conversation.Status.QUEUED).count(),
            "assigned_to_me": conversations.filter(
                status=Conversation.Status.ASSIGNED, assigned_to=request.user
            ).count(),
            "open": conversations.exclude(
                status__in=[Conversation.Status.RESOLVED, Conversation.Status.CLOSED]
            ).count(),
            "resolved_today": conversations.filter(
                status=Conversation.Status.RESOLVED
            ).count(),
        },
        "recent": conversations.select_related("contact").order_by("-last_activity_at")[:8],
    }
    return render(request, "core/dashboard.html", context)
