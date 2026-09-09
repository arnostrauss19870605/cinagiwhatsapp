"""Sign-in by emailed code, and the Users page where administrators create logins.

Only people an administrator has added can sign in, and only with an address
on the organisation's domain. The sign-in page never says whether an address
exists: it is on the public internet, and "no such user" is a gift to anyone
guessing staff addresses.
"""

import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import Http404
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from apps.core.audit import audit
from apps.integrations.msgraph import GraphError
from apps.workspaces.models import Workspace, WorkspaceMembership

from . import emails
from .forms import CodeForm, EmailForm, UserForm
from .models import LoginCode, email_domain_allowed

logger = logging.getLogger(__name__)
User = get_user_model()

SESSION_EMAIL = "login_email"
SESSION_NEXT = "login_next"
NEUTRAL = "If that address has a login, a sign-in code is on its way. Check your inbox."


def _safe_next(request):
    candidate = request.GET.get("next") or request.POST.get("next") or ""
    if url_has_allowed_host_and_scheme(candidate, allowed_hosts={request.get_host()}):
        return candidate
    return ""


def login_start(request):
    if request.user.is_authenticated:
        return redirect(settings.LOGIN_REDIRECT_URL)

    form = EmailForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        email = form.cleaned_data["email"]
        request.session[SESSION_EMAIL] = email
        request.session[SESSION_NEXT] = _safe_next(request)

        user = User.objects.filter(email__iexact=email, is_active=True).first()
        if user is None or not email_domain_allowed(email):
            audit("login.unknown_email", email=email)
        elif LoginCode.recent_count(user) >= 3:
            messages.error(request, "That address has asked for several codes already. Wait ten minutes and try again.")
            return redirect("accounts:login")
        else:
            _, code = LoginCode.issue(user)
            try:
                delivered = emails.send_login_code(user, code)
            except GraphError:
                messages.error(request, "We could not send the email just now. Please try again in a minute.")
                return redirect("accounts:login")
            if not delivered and settings.DEBUG:
                messages.warning(request, f"Email is not set up on this machine. Your code is {code}.")
        messages.info(request, NEUTRAL)
        return redirect("accounts:login_code")

    return render(request, "accounts/login.html", {"form": form, "next": _safe_next(request)})


def login_code(request):
    if request.user.is_authenticated:
        return redirect(settings.LOGIN_REDIRECT_URL)
    email = request.session.get(SESSION_EMAIL)
    if not email:
        return redirect("accounts:login")

    form = CodeForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = User.objects.filter(email__iexact=email, is_active=True).first()
        live = LoginCode.live_for(user) if user else None
        if live is not None and live.verify(form.cleaned_data["code"]):
            login(request, user, backend="django.contrib.auth.backends.ModelBackend")
            audit("login.success", actor=user)
            destination = request.session.pop(SESSION_NEXT, "") or settings.LOGIN_REDIRECT_URL
            request.session.pop(SESSION_EMAIL, None)
            return redirect(destination)
        audit("login.bad_code", email=email)
        if live is not None and live.used_at is not None:
            messages.error(request, "Too many wrong guesses. Ask for a new code.")
            return redirect("accounts:login")
        form.add_error("code", "That code is not right, or it has expired. Check the email or ask for a new one.")

    return render(request, "accounts/login_code.html", {"form": form, "email": email})


@login_required
@require_POST
def preferences(request):
    """Personal settings from the chat screen. Saved per person, not per browser."""
    request.user.send_on_enter = request.POST.get("send_on_enter") == "on"
    request.user.save(update_fields=["send_on_enter"])
    if request.headers.get("HX-Request"):
        from django.http import HttpResponse

        return HttpResponse(status=204)
    messages.success(
        request,
        "Enter now sends your message." if request.user.send_on_enter else "Enter now starts a new line.",
    )
    return redirect(_safe_next(request) or "inbox:inbox")


# -- users ----------------------------------------------------------------


def _manageable_workspaces(request):
    """The numbers this person may give others access to."""
    if request.user.is_platform_admin:
        return list(Workspace.objects.filter(is_active=True).order_by("name"))
    return [
        m.workspace
        for m in WorkspaceMembership.objects.select_related("workspace")
        .filter(user=request.user, is_active=True, workspace__is_active=True, role__in=WorkspaceMembership.MANAGE_ROLES)
        .order_by("workspace__name")
    ]


def _require_user_admin(request):
    if request.user.is_platform_admin:
        return
    membership = getattr(request, "membership", None)
    if membership is None or not membership.can_manage:
        raise PermissionDenied("Only an administrator can manage users.")


ROLE_CHOICES = [
    (WorkspaceMembership.Role.ADMIN, "Administrator - runs the number: settings, users, templates"),
    (WorkspaceMembership.Role.SUPERVISOR, "Supervisor - sees analytics and everyone's chats"),
    (WorkspaceMembership.Role.AGENT, "Agent - answers chats"),
    (WorkspaceMembership.Role.VIEWER, "Viewer - read only"),
]


def _access_rows(request, user=None):
    """One row per manageable number: current membership (if any) and the form values."""
    existing = {}
    if user is not None:
        existing = {m.workspace_id: m for m in user.workspace_memberships.select_related("workspace")}
    rows = []
    for workspace in _manageable_workspaces(request):
        membership = existing.get(workspace.pk)
        prefix = f"ws_{workspace.pk}"
        if request.method == "POST":
            has_access = request.POST.get(f"{prefix}_access") == "on"
            role = request.POST.get(f"{prefix}_role") or WorkspaceMembership.Role.AGENT
            rights = {field: request.POST.get(f"{prefix}_{field}") == "on" for field, _ in WorkspaceMembership.RIGHTS}
        else:
            has_access = membership is not None
            role = membership.role if membership else WorkspaceMembership.Role.AGENT
            rights = {field: getattr(membership, field) if membership else False for field, _ in WorkspaceMembership.RIGHTS}
        rows.append(
            {
                "workspace": workspace,
                "prefix": prefix,
                "membership": membership,
                "locked": membership is not None and membership.is_owner,
                "has_access": has_access,
                "role": role,
                "rights": rights,
                "rights_rows": [
                    {"field": field, "label": label, "checked": rights[field]}
                    for field, label in WorkspaceMembership.RIGHTS
                ],
            }
        )
    return rows


def _apply_access(user, rows, request):
    """Create, update or remove memberships to match the form. Owners are never touched."""
    valid_roles = {value for value, _ in ROLE_CHOICES}
    changed = []
    for row in rows:
        if row["locked"]:
            continue
        membership = row["membership"]
        if not row["has_access"]:
            if membership is not None:
                membership.delete()
                changed.append(f"removed from {row['workspace'].name}")
            continue
        role = row["role"] if row["role"] in valid_roles else WorkspaceMembership.Role.AGENT
        if membership is None:
            membership = WorkspaceMembership(user=user, workspace=row["workspace"])
        membership.role = role
        membership.is_active = True
        for field, _ in WorkspaceMembership.RIGHTS:
            setattr(membership, field, row["rights"][field])
        membership.save()
        changed.append(f"{row['workspace'].name}: {membership.get_role_display().lower()}")
    return changed


@login_required
def users(request):
    _require_user_admin(request)
    workspaces = _manageable_workspaces(request)
    people = (
        User.objects.filter(workspace_memberships__workspace__in=workspaces)
        .distinct()
        .prefetch_related("workspace_memberships__workspace")
        .order_by("first_name", "last_name", "email")
    )
    if request.user.is_platform_admin:
        people = User.objects.all().prefetch_related("workspace_memberships__workspace").order_by(
            "first_name", "last_name", "email"
        )
    return render(request, "accounts/users.html", {"people": people, "workspaces": workspaces})


@login_required
def user_create(request):
    _require_user_admin(request)
    form = UserForm(request.POST or None)
    rows = _access_rows(request)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            user = form.save()
            _apply_access(user, rows, request)
        memberships = list(user.workspace_memberships.select_related("workspace"))
        audit("user.created", request=request, target=user, email=user.email,
              access=[f"{m.workspace.name}:{m.role}" for m in memberships])
        try:
            sent = emails.send_welcome(user, request.user, memberships)
        except GraphError as exc:
            sent = False
            messages.warning(request, f"{user.display_name} was created but the welcome email did not go: {exc.friendly}")
        if sent:
            messages.success(request, f"{user.display_name} has a login. We have emailed them how to sign in.")
        elif not messages.get_messages(request):
            messages.success(request, f"{user.display_name} has a login. Email is not set up, so tell them to sign in with their email address.")
        return redirect("accounts:users")
    return render(
        request,
        "accounts/user_form.html",
        {"form": form, "rows": rows, "roles": ROLE_CHOICES, "rights": WorkspaceMembership.RIGHTS, "person": None},
    )


@login_required
def user_edit(request, pk):
    _require_user_admin(request)
    person = User.objects.filter(pk=pk).first()
    if person is None:
        raise Http404
    if not request.user.is_platform_admin:
        manageable = {w.pk for w in _manageable_workspaces(request)}
        if not person.workspace_memberships.filter(workspace_id__in=manageable).exists():
            raise Http404
    form = UserForm(request.POST or None, instance=person)
    rows = _access_rows(request, person)
    if request.method == "POST" and form.is_valid():
        if person == request.user and not any(r["has_access"] or r["locked"] for r in rows):
            messages.error(request, "You cannot remove your own access to every number.")
        else:
            with transaction.atomic():
                form.save()
                changed = _apply_access(person, rows, request)
            audit("user.updated", request=request, target=person, changes=changed)
            messages.success(request, f"Saved {person.display_name}.")
            return redirect("accounts:users")
    return render(
        request,
        "accounts/user_form.html",
        {"form": form, "rows": rows, "roles": ROLE_CHOICES, "rights": WorkspaceMembership.RIGHTS, "person": person},
    )


@login_required
@require_POST
def user_resend_welcome(request, pk):
    _require_user_admin(request)
    person = User.objects.filter(pk=pk, is_active=True).first()
    if person is None:
        raise Http404
    try:
        sent = emails.send_welcome(person, request.user, list(person.workspace_memberships.select_related("workspace")))
    except GraphError as exc:
        messages.error(request, f"The email did not go: {exc.friendly}")
        return redirect("accounts:users")
    if sent:
        audit("user.welcome_resent", request=request, target=person)
        messages.success(request, f"Sign-in instructions sent to {person.email}.")
    else:
        messages.warning(request, "Email is not set up on this server, so nothing was sent.")
    return redirect("accounts:users")


@login_required
@require_POST
def user_toggle_active(request, pk):
    _require_user_admin(request)
    person = User.objects.filter(pk=pk).first()
    if person is None:
        raise Http404
    if person == request.user:
        messages.error(request, "You cannot switch off your own login.")
        return redirect("accounts:users")
    person.is_active = not person.is_active
    person.save(update_fields=["is_active"])
    audit("user.deactivated" if not person.is_active else "user.reactivated", request=request, target=person)
    if person.is_active:
        messages.success(request, f"{person.display_name} can sign in again.")
    else:
        messages.success(request, f"{person.display_name} can no longer sign in. Their chats and history are kept.")
    return redirect("accounts:users")
