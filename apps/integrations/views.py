"""The API keys page: make a key, see it once, revoke it."""

from django.contrib import messages as flash
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.core.audit import audit
from apps.core.scoping import require_role, scoped_get_or_404
from apps.workspaces.models import WorkspaceMembership

from .models import ApiKey

NEW_KEY = "api_key_just_made"


@login_required
def api_keys(request):
    from apps.channels_wa.models import WhatsAppChannel

    if request.workspace is None:
        return redirect("workspaces:list")
    require_role(request, *WorkspaceMembership.MANAGE_ROLES)
    if request.method == "POST":
        name = (request.POST.get("name") or "").strip()[:120]
        if not name:
            flash.error(request, "Say who will use the key, e.g. 'BIMS policy system'.")
        else:
            key, raw = ApiKey.issue(request.workspace, name, user=request.user)
            audit("api_key.created", request=request, target=key, name=name)
            request.session[NEW_KEY] = {"id": key.pk, "raw": raw}
        return redirect("integrations:api_keys")
    just_made = request.session.pop(NEW_KEY, None)
    return render(request, "integrations/api_keys.html", {
        "keys": ApiKey.objects.for_request(request),
        "just_made": just_made,
        "channels": WhatsAppChannel.objects.for_request(request).filter(is_active=True),
        "base_url": request.build_absolute_uri("/api/v2/"),
    })


@login_required
@require_POST
def api_key_revoke(request, pk):
    require_role(request, *WorkspaceMembership.MANAGE_ROLES)
    key = scoped_get_or_404(ApiKey, request, pk=pk)
    if key.revoked_at is None:
        key.revoked_at = timezone.now()
        key.save(update_fields=["revoked_at", "updated_at"])
        audit("api_key.revoked", request=request, target=key)
        flash.success(request, f"'{key.name}' no longer works.")
    return redirect("integrations:api_keys")
