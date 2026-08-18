"""Resolve the workspace the signed-in user is currently working in.

``request.workspace`` and ``request.membership`` are set on every request and
are the only sanctioned way to know which tenant we are serving.
"""

SESSION_KEY = "active_workspace_id"


class ActiveWorkspaceMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.workspace = None
        request.membership = None
        request.available_workspaces = []

        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            from apps.workspaces.models import WorkspaceMembership

            memberships = list(
                WorkspaceMembership.objects.select_related("workspace")
                .filter(user=user, is_active=True, workspace__is_active=True)
                .order_by("workspace__name")
            )
            request.available_workspaces = [m.workspace for m in memberships]

            chosen = request.session.get(SESSION_KEY)
            membership = next((m for m in memberships if m.workspace_id == chosen), None)
            if membership is None and memberships:
                membership = memberships[0]
                request.session[SESSION_KEY] = membership.workspace_id
            if membership is not None:
                request.workspace = membership.workspace
                request.membership = membership

        return self.get_response(request)
