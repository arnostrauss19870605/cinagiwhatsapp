from django.contrib import messages as flash
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render

from apps.core.audit import audit
from apps.core.scoping import require_role
from apps.workspaces.models import WorkspaceMembership

from .forms import AgentProfileForm
from .models import AgentProfile


def profile_for(request):
    if request.workspace is None:
        return None
    profile, _ = AgentProfile.objects.get_or_create(
        workspace=request.workspace, user=request.user
    )
    return profile


@login_required
def me(request):
    profile = profile_for(request)
    if profile is None:
        return redirect("workspaces:list")
    if request.method == "POST":
        form = AgentProfileForm(request.POST, instance=profile)
        if form.is_valid():
            form.save()
            audit("agent.availability_updated", request=request, presence=profile.presence)
            flash.success(request, "Saved.")
            return redirect("agents:me")
    else:
        form = AgentProfileForm(instance=profile)
    return render(request, "agents/me.html", {"form": form, "profile": profile})


@login_required
def set_presence(request, presence):
    profile = profile_for(request)
    if profile and presence in AgentProfile.Presence.values:
        profile.set_presence(presence)
    return redirect(request.META.get("HTTP_REFERER") or "inbox:inbox")


@login_required
def team_availability(request):
    require_role(request, *WorkspaceMembership.SUPERVISE_ROLES)
    return render(
        request,
        "agents/team.html",
        {
            "profiles": AgentProfile.objects.for_request(request).select_related("user", "team"),
        },
    )
