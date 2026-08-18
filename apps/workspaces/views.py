import datetime as dt

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render

from apps.core.audit import audit
from apps.core.scoping import require_role

from .forms import (
    BusinessHoursFormSet,
    HolidayForm,
    MembershipForm,
    WorkspaceForm,
    WorkspaceSettingsForm,
)
from .middleware import SESSION_KEY
from .models import BusinessHours, Holiday, Workspace, WorkspaceMembership


@login_required
def workspace_list(request):
    return render(
        request,
        "workspaces/list.html",
        {"memberships": request.user.workspace_memberships.select_related("workspace")},
    )


@login_required
def switch(request, slug):
    workspace = get_object_or_404(Workspace, slug=slug, is_active=True)
    membership = WorkspaceMembership.objects.filter(
        user=request.user, workspace=workspace, is_active=True
    ).first()
    if membership is None:
        audit("workspace.switch_denied", request=request, workspace=workspace)
        messages.error(request, "You do not have access to that workspace.")
        return redirect("workspaces:list")
    request.session[SESSION_KEY] = workspace.pk
    messages.success(request, f"You are now working in {workspace.name}.")
    return redirect(request.GET.get("next") or "core:dashboard")


@login_required
def create(request):
    if request.method == "POST":
        form = WorkspaceForm(request.POST)
        if form.is_valid():
            workspace = form.save()
            WorkspaceMembership.objects.create(
                user=request.user, workspace=workspace, role=WorkspaceMembership.Role.OWNER
            )
            for weekday in range(7):
                BusinessHours.objects.create(
                    workspace=workspace,
                    weekday=weekday,
                    is_closed=weekday >= 5,
                )
            request.session[SESSION_KEY] = workspace.pk
            audit("workspace.created", request=request, workspace=workspace, name=workspace.name)
            messages.success(request, "Workspace created. Next: connect your WhatsApp number.")
            return redirect("channels_wa:connect")
    else:
        form = WorkspaceForm()
    return render(request, "workspaces/create.html", {"form": form})


@login_required
def settings_view(request):
    workspace = request.workspace
    if workspace is None:
        return redirect("workspaces:list")
    require_role(request, *WorkspaceMembership.MANAGE_ROLES)

    if request.method == "POST":
        form = WorkspaceSettingsForm(request.POST, instance=workspace)
        if form.is_valid():
            form.save()
            audit("workspace.updated", request=request, fields=list(form.changed_data))
            messages.success(request, "Saved.")
            return redirect("workspaces:settings")
    else:
        form = WorkspaceSettingsForm(instance=workspace)
    return render(request, "workspaces/settings.html", {"form": form})


@login_required
def hours(request):
    workspace = request.workspace
    if workspace is None:
        return redirect("workspaces:list")
    require_role(request, *WorkspaceMembership.MANAGE_ROLES)

    for weekday in range(7):
        BusinessHours.objects.get_or_create(
            workspace=workspace, weekday=weekday, defaults={"is_closed": weekday >= 5}
        )
    queryset = workspace.business_hours.all()

    if request.method == "POST":
        formset = BusinessHoursFormSet(request.POST, queryset=queryset)
        holiday_form = HolidayForm()
        if formset.is_valid():
            formset.save()
            audit("workspace.hours_updated", request=request)
            messages.success(request, "Working hours saved.")
            return redirect("workspaces:hours")
    else:
        formset = BusinessHoursFormSet(queryset=queryset)
        holiday_form = HolidayForm()

    return render(
        request,
        "workspaces/hours.html",
        {
            "formset": formset,
            "holiday_form": holiday_form,
            "holidays": workspace.holidays.filter(
                date__gte=dt.date.today() - dt.timedelta(days=365)
            ),
            "is_open_now": workspace.is_open(),
        },
    )


@login_required
def holiday_add(request):
    workspace = request.workspace
    require_role(request, *WorkspaceMembership.MANAGE_ROLES)
    form = HolidayForm(request.POST)
    if form.is_valid():
        holiday = form.save(commit=False)
        holiday.workspace = workspace
        holiday.save()
        messages.success(request, f"Added {holiday.name}.")
    return redirect("workspaces:hours")


@login_required
def holiday_delete(request, pk):
    require_role(request, *WorkspaceMembership.MANAGE_ROLES)
    Holiday.objects.filter(pk=pk, workspace=request.workspace).delete()
    return redirect("workspaces:hours")


@login_required
def team(request):
    workspace = request.workspace
    if workspace is None:
        return redirect("workspaces:list")
    require_role(request, *WorkspaceMembership.MANAGE_ROLES)

    if request.method == "POST":
        form = MembershipForm(request.POST, workspace=workspace)
        if form.is_valid():
            membership = form.save(commit=False)
            membership.workspace = workspace
            membership.user = form.user
            membership.save()
            audit("workspace.member_added", request=request, member=str(form.user))
            messages.success(request, f"{form.user.display_name} can now use this workspace.")
            return redirect("workspaces:team")
    else:
        form = MembershipForm(workspace=workspace)

    return render(
        request,
        "workspaces/team.html",
        {
            "form": form,
            "memberships": workspace.memberships.select_related("user").order_by("user__username"),
        },
    )


@login_required
def member_remove(request, pk):
    require_role(request, *WorkspaceMembership.MANAGE_ROLES)
    membership = get_object_or_404(WorkspaceMembership, pk=pk, workspace=request.workspace)
    if membership.role == WorkspaceMembership.Role.OWNER:
        messages.error(request, "You cannot remove the workspace owner.")
    else:
        audit("workspace.member_removed", request=request, member=str(membership.user))
        membership.delete()
        messages.success(request, "Removed.")
    return redirect("workspaces:team")
