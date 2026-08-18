import logging

logger = logging.getLogger(__name__)


def audit(action, *, workspace=None, actor=None, target=None, request=None, **detail):
    """Record a significant event. Never raises - auditing must not break flow."""
    from apps.core.models import AuditLog

    try:
        ip = None
        if request is not None:
            ip = request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")[0].strip() or (
                request.META.get("REMOTE_ADDR")
            )
            if actor is None and getattr(request, "user", None) and request.user.is_authenticated:
                actor = request.user
            if workspace is None:
                workspace = getattr(request, "workspace", None)
        return AuditLog.objects.create(
            workspace=workspace,
            actor=actor if getattr(actor, "pk", None) else None,
            action=action,
            target_type=target.__class__.__name__ if target is not None else "",
            target_id=str(getattr(target, "pk", "")) if target is not None else "",
            detail=detail,
            ip_address=ip,
        )
    except Exception:  # pragma: no cover
        logger.exception("audit failed for action=%s", action)
        return None
