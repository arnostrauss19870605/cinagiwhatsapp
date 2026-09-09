from django.contrib import messages as flash
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q
from django.shortcuts import redirect, render

from apps.core.audit import audit
from apps.core.scoping import require_right, require_role, scoped_get_or_404
from apps.workspaces.models import WorkspaceMembership

from .forms import QuickSnippetForm
from .models import MessageTemplate, QuickSnippet


@login_required
def templates(request):
    if request.workspace is None:
        return redirect("workspaces:list")
    channels = request.workspace.whatsappchannels.all()
    return render(
        request,
        "library/templates.html",
        {
            "templates": MessageTemplate.objects.for_request(request).select_related("channel"),
            "channels": channels,
        },
    )


MEDIA_MIME = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".mp4": "video/mp4",
}


@login_required
def bulk_send(request):
    """Send an approved template to whole audiences, in the background.

    Two steps on one page: pick the template, then fill in its values and
    choose the audiences. The actual sending runs on the outbound queue -
    a 600-person send must never hang a browser tab.
    """
    from apps.contacts.models import Audience
    from apps.library.event_templates import sendable_on

    if request.workspace is None:
        return redirect("workspaces:list")
    require_right(request, "send_bulk")

    templates = (
        MessageTemplate.objects.for_request(request)
        .filter(status=MessageTemplate.Status.APPROVED)
        .select_related("channel")
    )
    audiences = Audience.objects.for_request(request).annotate(member_count=Count("contacts"))

    chosen = None
    template_id = request.POST.get("template") or request.GET.get("template")
    if template_id:
        chosen = templates.filter(pk=template_id).first()

    if request.method == "POST" and chosen:
        picked = list(audiences.filter(pk__in=request.POST.getlist("audiences")))
        values = [
            (request.POST.get(f"value_{index}") or "").strip()
            for index in range(2, chosen.variable_count + 1)
        ]
        error = None
        allowed, reason = sendable_on(chosen.name)
        if not allowed:
            error = reason
        elif not picked:
            error = "Choose at least one audience."
        elif any(not v for v in values):
            error = "Fill in every message value."

        header_media = None
        if not error and chosen.header_format in {"IMAGE", "DOCUMENT", "VIDEO"}:
            header_media, error = _resolve_header_media(request, chosen)

        if error:
            flash.error(request, error)
        else:
            from apps.library.bulk import audience_contacts, resolve_channel, start_bulk_send
            from apps.library.tasks import run_bulk_send

            recipients = audience_contacts(request.workspace, picked).count()
            if recipients == 0:
                flash.error(request, "Those audiences have nobody in them yet.")
            else:
                batch = start_bulk_send(
                    request.workspace, resolve_channel(request.workspace), chosen, picked, values,
                    header_media=header_media, created_by=request.user, recipient_count=recipients,
                )
                run_bulk_send.delay(
                    request.workspace.pk, chosen.pk, [a.pk for a in picked],
                    values, header_media, request.user.pk, batch.pk,
                )
                audit(
                    "library.bulk_send_queued", request=request, target=chosen,
                    audiences=[a.name for a in picked], recipients=recipients,
                )
                flash.success(
                    request,
                    f"Sending '{chosen.name}' to {recipients} people has started. "
                    "This page shows how it is going.",
                )
                return redirect("reporting:bulk_send", pk=batch.pk)

    value_fields = range(2, chosen.variable_count + 1) if chosen else []
    return render(
        request,
        "library/bulk_send.html",
        {
            "templates": templates,
            "audiences": audiences,
            "chosen": chosen,
            "value_fields": value_fields,
        },
    )


def _resolve_header_media(request, template):
    """The header file or link for an image/document/video template."""
    from pathlib import Path

    kind = template.header_format.lower()
    upload = request.FILES.get("media_file")
    link = (request.POST.get("media_url") or "").strip()
    if upload:
        mime = MEDIA_MIME.get(Path(upload.name).suffix.lower())
        if mime is None:
            return None, "That file type cannot be sent. Use a PDF, JPG, PNG or MP4."
        channel = template.channel
        try:
            media_id = channel.client().upload_media(upload, mime)
        except Exception:
            return None, "WhatsApp did not accept that file. Try again, or use a link instead."
        if not media_id:
            return None, "WhatsApp did not accept that file. Try again, or use a link instead."
        media = {"id": media_id}
        if kind == "document":
            media["filename"] = upload.name
        return media, None
    if link.startswith("https://"):
        media = {"link": link}
        if kind == "document":
            media["filename"] = Path(link).name or "document.pdf"
        return media, None
    return None, f"This template carries a {kind}, so attach a file or paste an https link."


@login_required
def snippets(request):
    if request.workspace is None:
        return redirect("workspaces:list")
    return render(
        request,
        "library/snippets.html",
        {
            "snippets": QuickSnippet.objects.for_request(request)
            .filter(Q(owner__isnull=True) | Q(owner=request.user))
            .order_by("category", "title"),
        },
    )


@login_required
def snippet_edit(request, pk=None):
    instance = scoped_get_or_404(QuickSnippet, request, pk=pk) if pk else None
    if request.method == "POST":
        form = QuickSnippetForm(request.POST, instance=instance)
        if form.is_valid():
            snippet = form.save(commit=False)
            snippet.workspace = request.workspace
            snippet.owner = request.user if form.cleaned_data["share"] == "me" else None
            snippet.created_by = snippet.created_by or request.user
            snippet.save()
            audit("snippet.saved", request=request, target=snippet, title=snippet.title)
            flash.success(request, "Saved.")
            return redirect("library:snippets")
    else:
        initial = {"share": "me" if instance and instance.owner_id else "team"}
        form = QuickSnippetForm(instance=instance, initial=initial)
    return render(request, "library/snippet_form.html", {"form": form, "snippet": instance})


@login_required
def snippet_delete(request, pk):
    snippet = scoped_get_or_404(QuickSnippet, request, pk=pk)
    snippet.delete()
    flash.success(request, "Deleted.")
    return redirect("library:snippets")
