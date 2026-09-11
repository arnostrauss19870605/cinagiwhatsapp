from django.contrib import messages as flash
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q
from django.shortcuts import redirect, render

from apps.core.audit import audit
from apps.core.scoping import require_right, require_role, scoped_get_or_404
from apps.workspaces.models import WorkspaceMembership

from .forms import QuickSnippetForm
from .models import MessageTemplate, QuickSnippet, TemplateDraft


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
            "drafts": TemplateDraft.objects.for_request(request).select_related("channel", "template", "created_by"),
        },
    )


HEADER_TYPES = TemplateDraft.HeaderType.choices


def _read_draft_form(request, draft):
    """Copy the editor's fields onto the draft. Buttons and placeholders come from the body."""
    from apps.library import drafts as drafting

    draft.internal_title = (request.POST.get("internal_title") or "").strip()[:120]
    draft.name = (request.POST.get("name") or "").strip().lower()[:512]
    draft.language = (request.POST.get("language") or "en").strip()[:12]
    draft.category = request.POST.get("category") or MessageTemplate.Category.MARKETING
    draft.header_type = request.POST.get("header_type") or TemplateDraft.HeaderType.NONE
    draft.header_text = (request.POST.get("header_text") or "").strip()[:60] if draft.header_type == "text" else ""
    if draft.header_type in ("image", "video", "document") and request.FILES.get("header_sample"):
        draft.header_sample = request.FILES["header_sample"]
    elif draft.header_type in ("none", "text"):
        draft.header_sample = None
    draft.body = (request.POST.get("body") or "").replace("\r\n", "\n").strip()
    draft.footer = (request.POST.get("footer") or "").strip()[:60]
    draft.description = (request.POST.get("description") or "").strip()[:255]

    types = request.POST.getlist("button_type")
    texts = request.POST.getlist("button_text")
    urls = request.POST.getlist("button_url")
    phones = request.POST.getlist("button_phone")
    buttons = []
    for index, kind in enumerate(types):
        text = (texts[index] if index < len(texts) else "").strip()
        if not text and not kind:
            continue
        buttons.append({
            "type": kind or "quick_reply",
            "text": text,
            "url": (urls[index] if index < len(urls) else "").strip(),
            "phone": (phones[index] if index < len(phones) else "").strip(),
        })
    draft.buttons = buttons

    variables = drafting.merge_variables(draft.body, draft.variables)
    for variable in variables:
        key = variable["key"]
        if request.POST.get(f"var_{key}_present"):
            variable["label"] = (request.POST.get(f"var_{key}_label") or variable["label"]).strip()[:60]
            variable["source"] = request.POST.get(f"var_{key}_source") or variable["source"]
            variable["attribute"] = (request.POST.get(f"var_{key}_attribute") or "").strip()[:60]
            variable["sample"] = (request.POST.get(f"var_{key}_sample") or "").strip()[:120]
            variable["fallback"] = (request.POST.get(f"var_{key}_fallback") or "").strip()[:120]
            variable["required"] = request.POST.get(f"var_{key}_required") == "on"
        if variable["source"] in ("first_name", "full_name", "number") and not variable["sample"]:
            variable["sample"] = drafting.DEFAULT_SAMPLES[variable["source"]]
    draft.variables = variables


@login_required
def draft_edit(request, pk=None):
    """Write a template here instead of in WhatsApp Manager, then send it to Meta."""
    from apps.channels_wa.messaging.base import TransportError
    from apps.contacts.models import Contact
    from apps.library import drafts as drafting

    if request.workspace is None:
        return redirect("workspaces:list")
    require_role(request, *WorkspaceMembership.MANAGE_ROLES)
    draft = scoped_get_or_404(TemplateDraft, request, pk=pk) if pk else None
    channels = list(request.workspace.whatsappchannels.filter(is_active=True))
    if draft is None and not channels:
        flash.error(request, "Connect a WhatsApp number before writing a template.")
        return redirect("library:templates")

    if request.method == "POST" and (draft is None or draft.editable):
        if draft is None:
            draft = TemplateDraft(workspace=request.workspace, created_by=request.user)
        channel = next((c for c in channels if str(c.pk) == request.POST.get("channel")), channels[0])
        draft.channel = channel
        _read_draft_form(request, draft)
        clash = TemplateDraft.objects.filter(channel=channel, name=draft.name, language=draft.language).exclude(pk=draft.pk)
        if not draft.internal_title:
            flash.error(request, "Give the template a name for the team.")
        elif clash.exists():
            flash.error(request, f"There is already a draft called '{draft.name}' for this number.")
        else:
            if draft.status == TemplateDraft.Status.ERROR:
                draft.status = TemplateDraft.Status.DRAFT
            draft.save()
            audit("template.draft_saved", request=request, target=draft)
            if request.POST.get("action") == "submit":
                try:
                    template = drafting.submit(draft, actor=request.user)
                except ValueError as exc:
                    flash.error(request, str(exc))
                except TransportError as exc:
                    flash.error(request, f"Meta did not accept it: {exc.friendly}")
                else:
                    flash.success(
                        request,
                        f"'{template.name}' is with Meta for review. Approval usually takes minutes to a few hours; "
                        "this page updates on its own.",
                    )
                    return redirect("library:templates")
            else:
                flash.success(request, "Draft saved.")
            return redirect("library:draft_edit", pk=draft.pk)

    if draft is None:
        draft = TemplateDraft(workspace=request.workspace, channel=channels[0], language="en")
    problems = drafting.validate(draft) if draft.body else []

    preview_contact, preview_text = None, None
    query = (request.GET.get("preview_q") or "").strip()
    if query and draft.variables:
        preview_contact = (
            Contact.objects.for_request(request)
            .filter(Q(display_name__icontains=query) | Q(profile_name__icontains=query) | Q(wa_id__icontains=query))
            .first()
        )
        if preview_contact:
            class _Stub:
                variable_map = draft.variables
            try:
                resolved = drafting.values_for(_Stub, preview_contact, {})
                preview_text = drafting.render_preview(draft, {v["key"]: resolved[i] for i, v in enumerate(draft.variables)})
            except ValueError as exc:
                preview_text = f"Could not fill in this message for {preview_contact.name}: {exc}."

    return render(
        request,
        "library/draft_form.html",
        {
            "draft": draft,
            "channels": channels,
            "categories": MessageTemplate.Category.choices,
            "header_types": HEADER_TYPES,
            "sources": drafting.SOURCES,
            "problems": problems,
            "sample_preview": drafting.render_preview(draft),
            "preview_q": query,
            "preview_contact": preview_contact,
            "preview_text": preview_text,
        },
    )


@login_required
def draft_clone(request, pk):
    from apps.library import drafts as drafting

    require_role(request, *WorkspaceMembership.MANAGE_ROLES)
    draft = scoped_get_or_404(TemplateDraft, request, pk=pk)
    if request.method != "POST":
        return redirect("library:draft_edit", pk=draft.pk)
    copy = drafting.clone(draft, actor=request.user)
    flash.success(request, f"Version {copy.version} is ready to edit. '{draft.name}' keeps working until this one is approved.")
    return redirect("library:draft_edit", pk=copy.pk)


@login_required
def draft_delete(request, pk):
    require_role(request, *WorkspaceMembership.MANAGE_ROLES)
    draft = scoped_get_or_404(TemplateDraft, request, pk=pk)
    if request.method == "POST" and draft.editable:
        audit("template.draft_deleted", request=request, name=draft.name)
        draft.delete()
        flash.success(request, "Draft deleted.")
    return redirect("library:templates")


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

    manual_variables = chosen.manual_variables if chosen else []
    named = bool(chosen and chosen.variable_map)
    if request.method == "POST" and chosen:
        picked = list(audiences.filter(pk__in=request.POST.getlist("audiences")))
        if named:
            values = {
                v["key"]: (request.POST.get(f"value_{v['key']}") or "").strip() for v in manual_variables
            }
            missing = any(not v for v in values.values())
        else:
            values = [
                (request.POST.get(f"value_{index}") or "").strip()
                for index in range(2, chosen.variable_count + 1)
            ]
            missing = any(not v for v in values)
        error = None
        allowed, reason = sendable_on(chosen.name)
        if not allowed:
            error = reason
        elif not picked:
            error = "Choose at least one audience."
        elif missing:
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

    value_fields = range(2, chosen.variable_count + 1) if (chosen and not named) else []
    return render(
        request,
        "library/bulk_send.html",
        {
            "templates": templates,
            "audiences": audiences,
            "chosen": chosen,
            "value_fields": value_fields,
            "named": named,
            "manual_variables": manual_variables,
            "auto_variables": [v for v in (chosen.variable_map or []) if v.get("source") != "manual"] if chosen else [],
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
