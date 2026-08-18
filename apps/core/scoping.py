"""Workspace scoping.

Every tenant-owned model inherits WorkspaceScopedModel and every query that
serves a request goes through ``.for_workspace(...)``. Cross workspace access
is the highest risk bug class in a shared inbox, so it is a single, obvious,
testable chokepoint rather than a habit.
"""

from django.core.exceptions import PermissionDenied
from django.db import models
from django.http import Http404


class WorkspaceQuerySet(models.QuerySet):
    def for_workspace(self, workspace):
        if workspace is None:
            return self.none()
        return self.filter(workspace=workspace)

    def for_request(self, request):
        return self.for_workspace(getattr(request, "workspace", None))


class WorkspaceManager(models.Manager.from_queryset(WorkspaceQuerySet)):
    pass


class WorkspaceScopedModel(models.Model):
    workspace = models.ForeignKey(
        "workspaces.Workspace", on_delete=models.CASCADE, related_name="%(class)ss"
    )

    objects = WorkspaceManager()

    class Meta:
        abstract = True


def scoped_get_or_404(model, request, **kwargs):
    """Fetch an object inside the active workspace or 404 - never leak existence."""
    workspace = getattr(request, "workspace", None)
    if workspace is None:
        raise Http404
    try:
        return model.objects.for_workspace(workspace).get(**kwargs)
    except model.DoesNotExist:
        from apps.core.audit import audit

        audit(
            "access.denied",
            request=request,
            model=model.__name__,
            lookup={k: str(v) for k, v in kwargs.items()},
        )
        raise Http404


def require_role(request, *roles):
    membership = getattr(request, "membership", None)
    if membership is None or membership.role not in roles:
        raise PermissionDenied("You do not have permission to do that.")
    return membership
