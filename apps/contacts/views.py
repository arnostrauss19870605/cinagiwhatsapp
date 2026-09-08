"""Audience management: named groups of contacts for bulk sends."""

import csv
import io

from django.contrib import messages as flash
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from apps.core.audit import audit
from apps.core.scoping import require_role, scoped_get_or_404
from apps.workspaces.models import WorkspaceMembership

from .models import Audience, Contact, normalise_msisdn


def _manager(request):
    require_role(request, *WorkspaceMembership.MANAGE_ROLES)


@login_required
def audiences(request):
    if request.workspace is None:
        return redirect("workspaces:list")
    _manager(request)
    if request.method == "POST":
        name = (request.POST.get("name") or "").strip()
        if not name:
            flash.error(request, "Give the audience a name.")
        elif Audience.objects.for_request(request).filter(name__iexact=name).exists():
            flash.error(request, f"There is already an audience called '{name}'.")
        else:
            audience = Audience.objects.create(
                workspace=request.workspace,
                name=name,
                description=(request.POST.get("description") or "").strip(),
            )
            audit("audience.created", request=request, target=audience, name=name)
            flash.success(request, f"'{name}' created. Now add people to it.")
            return redirect("contacts:audience_detail", pk=audience.pk)
    return render(
        request,
        "contacts/audiences.html",
        {
            "audiences": Audience.objects.for_request(request).annotate(
                member_count=Count("contacts")
            ),
        },
    )


@login_required
def audience_detail(request, pk):
    if request.workspace is None:
        return redirect("workspaces:list")
    _manager(request)
    audience = scoped_get_or_404(Audience, request, pk=pk)

    query = (request.GET.get("q") or "").strip()
    candidates = []
    if query:
        candidates = (
            Contact.objects.for_request(request)
            .filter(Q(display_name__icontains=query) | Q(profile_name__icontains=query)
                    | Q(wa_id__icontains=normalise_msisdn(query) or query))
            .exclude(audiences=audience)[:20]
        )

    return render(
        request,
        "contacts/audience_detail.html",
        {
            "audience": audience,
            "members": audience.contacts.order_by("display_name", "wa_id"),
            "candidates": candidates,
            "query": query,
        },
    )


@login_required
@require_POST
def audience_add(request, pk):
    _manager(request)
    audience = scoped_get_or_404(Audience, request, pk=pk)
    contact = scoped_get_or_404(Contact, request, pk=request.POST.get("contact"))
    audience.contacts.add(contact)
    flash.success(request, f"{contact.name} added to '{audience.name}'.")
    return redirect("contacts:audience_detail", pk=audience.pk)


@login_required
@require_POST
def audience_remove(request, pk):
    _manager(request)
    audience = scoped_get_or_404(Audience, request, pk=pk)
    contact = scoped_get_or_404(Contact, request, pk=request.POST.get("contact"))
    audience.contacts.remove(contact)
    flash.success(request, f"{contact.name} removed from '{audience.name}'.")
    return redirect("contacts:audience_detail", pk=audience.pk)


@login_required
@require_POST
def audience_delete(request, pk):
    _manager(request)
    audience = scoped_get_or_404(Audience, request, pk=pk)
    name = audience.name
    audience.delete()
    audit("audience.deleted", request=request, name=name)
    flash.success(request, f"'{name}' deleted. The contacts themselves are untouched.")
    return redirect("contacts:audiences")


@login_required
@require_POST
def audience_import(request, pk):
    """Upload a CSV of people straight into this audience.

    Expected columns (header row, any order): name or first_name/last_name,
    and mobile (or phone / msisdn / whatsapp). Existing contacts are matched
    on their number, so re-importing updates membership without duplicating.
    """
    _manager(request)
    audience = scoped_get_or_404(Audience, request, pk=pk)

    upload = request.FILES.get("file")
    if upload is None:
        flash.error(request, "Choose a CSV file first.")
        return redirect("contacts:audience_detail", pk=audience.pk)

    try:
        text = upload.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        flash.error(request, "That file is not readable as a CSV. Save it as CSV and try again.")
        return redirect("contacts:audience_detail", pk=audience.pk)

    reader = csv.DictReader(io.StringIO(text))
    fields = {name.lower().strip(): name for name in (reader.fieldnames or [])}

    def value(row, *keys):
        for key in keys:
            if key in fields:
                return (row.get(fields[key], "") or "").strip()
        return ""

    added = created = skipped = 0
    for row in reader:
        msisdn = normalise_msisdn(
            value(row, "mobile", "phone", "msisdn", "whatsapp", "cell", "number")
        )
        if not msisdn:
            skipped += 1
            continue
        name = value(row, "name", "full_name") or " ".join(
            part for part in (value(row, "first_name"), value(row, "last_name")) if part
        )
        contact, was_created = Contact.objects.get_or_create(
            workspace=request.workspace,
            wa_id=msisdn,
            defaults={"display_name": name},
        )
        if not was_created and name and not contact.display_name:
            contact.display_name = name
            contact.save(update_fields=["display_name"])
        audience.contacts.add(contact)
        added += 1
        created += 1 if was_created else 0

    audit(
        "audience.imported", request=request, target=audience,
        added=added, created=created, skipped=skipped,
    )
    note = f"{added} added to '{audience.name}' ({created} new)."
    if skipped:
        note += f" {skipped} row(s) had no usable mobile number and were left out."
    flash.success(request, note)
    return redirect("contacts:audience_detail", pk=audience.pk)
