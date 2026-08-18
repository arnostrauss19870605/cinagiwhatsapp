def workspace_context(request):
    return {
        "workspace": getattr(request, "workspace", None),
        "membership": getattr(request, "membership", None),
        "available_workspaces": getattr(request, "available_workspaces", []),
    }
