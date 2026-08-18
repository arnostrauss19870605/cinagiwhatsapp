from django.contrib import messages as flash
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from apps.core.audit import audit
from apps.core.scoping import scoped_get_or_404
from apps.library.models import MessageTemplate, QuickSnippet

from .forms import NoteForm, ReplyForm, TemplateSendForm
from .models import AssignmentLog, Conversation, InternalNote, Message

FILTERS = {
    "mine": "Mine",
    "unassigned": "Waiting",
    "open": "All open",
    "resolved": "Resolved",
}


def _conversations(request, view="open", query=""):
    qs = (
        Conversation.objects.for_request(request)
        .select_related("contact", "assigned_to", "channel")
        .order_by("-last_activity_at")
    )
    if view == "mine":
        qs = qs.filter(assigned_to=request.user, status__in=Conversation.OPEN_STATUSES)
    elif view == "unassigned":
        qs = qs.filter(assigned_to__isnull=True, status__in=Conversation.OPEN_STATUSES)
    elif view == "resolved":
        qs = qs.filter(status__in=[Conversation.Status.RESOLVED, Conversation.Status.CLOSED])
    else:
        qs = qs.filter(status__in=Conversation.OPEN_STATUSES)
    if query:
        digits = "".join(ch for ch in query if ch.isdigit())
        qs = qs.filter(
            Q(contact__display_name__icontains=query)
            | Q(contact__profile_name__icontains=query)
            | Q(contact__wa_id__contains=digits or query)
            | Q(messages__body__icontains=query)
        ).distinct()
    return qs


@login_required
def inbox(request, pk=None):
    if request.workspace is None:
        return redirect("workspaces:list")

    view = request.GET.get("view", "mine" if pk is None else request.GET.get("view", "open"))
    query = request.GET.get("q", "").strip()
    conversations = _conversations(request, view, query)[:100]

    conversation = None
    if pk:
        conversation = scoped_get_or_404(Conversation, request, pk=pk)
        if conversation.unread_agent_count:
            conversation.unread_agent_count = 0
            conversation.save(update_fields=["unread_agent_count"])

    context = {
        "conversations": conversations,
        "conversation": conversation,
        "view": view,
        "query": query,
        "filters": FILTERS,
        "reply_form": ReplyForm(),
        "note_form": NoteForm(),
        "snippets": _snippets(request)[:8],
        "templates": MessageTemplate.objects.for_request(request).filter(
            status=MessageTemplate.Status.APPROVED
        )[:50],
        "counts": {
            "mine": _conversations(request, "mine").count(),
            "unassigned": _conversations(request, "unassigned").count(),
            "open": _conversations(request, "open").count(),
        },
    }
    if request.headers.get("HX-Request") and request.GET.get("fragment") == "list":
        return render(request, "inbox/partials/conversation_list.html", context)
    return render(request, "inbox/inbox.html", context)


def _snippets(request, query=""):
    qs = QuickSnippet.objects.for_request(request).filter(is_active=True).filter(
        Q(owner__isnull=True) | Q(owner=request.user)
    )
    if query:
        qs = qs.filter(
            Q(title__icontains=query) | Q(shortcut__icontains=query) | Q(body__icontains=query)
        )
    return qs


@login_required
def thread(request, pk):
    """The conversation pane on its own - what the websocket ping makes us refetch."""
    conversation = scoped_get_or_404(Conversation, request, pk=pk)
    return render(
        request,
        "inbox/partials/thread.html",
        {"conversation": conversation, "reply_form": ReplyForm(), "note_form": NoteForm()},
    )


@login_required
def send(request, pk):
    conversation = scoped_get_or_404(Conversation, request, pk=pk)
    if request.membership and not request.membership.can_reply:
        flash.error(request, "You have view-only access to this workspace.")
        return redirect("inbox:conversation", pk=pk)

    form = ReplyForm(request.POST, request.FILES)
    if not form.is_valid():
        flash.error(request, next(iter(form.errors.values()))[0])
        return redirect("inbox:conversation", pk=pk)

    if not conversation.window_open:
        flash.error(
            request,
            "This chat has been quiet for more than 24 hours. Choose an approved template instead.",
        )
        return redirect("inbox:conversation", pk=pk)

    from apps.channels_wa.outbound import send_media, send_text

    snippet = None
    snippet_id = form.cleaned_data.get("snippet_id")
    if snippet_id:
        snippet = _snippets(request).filter(pk=snippet_id).first()
        if snippet:
            snippet.usage_count += 1
            snippet.save(update_fields=["usage_count"])

    attachment = form.cleaned_data.get("attachment")
    body = (form.cleaned_data.get("body") or "").strip()
    if attachment:
        message = send_media(conversation, attachment, caption=body, author=request.user)
    else:
        message = send_text(conversation, body, author=request.user, snippet=snippet)

    if conversation.assigned_to_id is None:
        _claim(request, conversation, AssignmentLog.Reason.MANUAL, "replied without claiming")

    if message.wa_status == Message.Status.FAILED:
        flash.error(request, message.wa_error.get("reason", "WhatsApp did not accept that message."))
    elif message.wa_status == Message.Status.BLOCKED:
        flash.warning(request, message.wa_error.get("reason", ""))

    audit("message.sent", request=request, target=conversation, status=message.wa_status)
    return redirect("inbox:conversation", pk=pk)


@login_required
def send_template(request, pk):
    conversation = scoped_get_or_404(Conversation, request, pk=pk)
    template = scoped_get_or_404(
        MessageTemplate, request, pk=request.POST.get("template_id") or request.GET.get("template_id")
    )
    form = TemplateSendForm(request.POST or None, template=template)

    if request.method == "POST" and form.is_valid():
        from apps.channels_wa.outbound import send_template as do_send

        message = do_send(conversation, template, form.values(), author=request.user)
        if message.wa_status == Message.Status.FAILED:
            flash.error(request, message.wa_error.get("reason", "WhatsApp rejected the template."))
        elif message.wa_status == Message.Status.BLOCKED:
            flash.warning(request, message.wa_error.get("reason", ""))
        else:
            flash.success(request, "Template sent.")
        audit("template.sent", request=request, target=conversation, template=template.name)
        return redirect("inbox:conversation", pk=pk)

    return render(
        request,
        "inbox/template_send.html",
        {"conversation": conversation, "template": template, "form": form},
    )


def _claim(request, conversation, reason, detail=""):
    previous = conversation.assigned_to
    conversation.assigned_to = request.user
    conversation.assigned_at = timezone.now()
    conversation.status = Conversation.Status.ASSIGNED
    conversation.save(update_fields=["assigned_to", "assigned_at", "status"])
    AssignmentLog.objects.create(
        workspace=conversation.workspace,
        conversation=conversation,
        from_user=previous,
        to_user=request.user,
        reason=reason,
        detail=detail,
    )
    from apps.inbox import events

    events.conversation_changed(conversation, "assignment")


@login_required
def claim(request, pk):
    conversation = scoped_get_or_404(Conversation, request, pk=pk)
    _claim(request, conversation, AssignmentLog.Reason.MANUAL, "picked up from the queue")
    audit("conversation.claimed", request=request, target=conversation)
    return redirect("inbox:conversation", pk=pk)


@login_required
def release(request, pk):
    conversation = scoped_get_or_404(Conversation, request, pk=pk)
    previous = conversation.assigned_to
    conversation.assigned_to = None
    conversation.status = Conversation.Status.QUEUED
    conversation.save(update_fields=["assigned_to", "status"])
    AssignmentLog.objects.create(
        workspace=conversation.workspace,
        conversation=conversation,
        from_user=previous,
        reason=AssignmentLog.Reason.RELEASE,
    )
    flash.success(request, "Back in the queue for someone else.")
    return redirect("inbox:conversation", pk=pk)


@login_required
def resolve(request, pk):
    conversation = scoped_get_or_404(Conversation, request, pk=pk)
    conversation.status = Conversation.Status.RESOLVED
    conversation.resolved_at = timezone.now()
    conversation.resolved_by = request.user
    conversation.save(update_fields=["status", "resolved_at", "resolved_by"])
    audit("conversation.resolved", request=request, target=conversation)
    flash.success(request, "Marked as resolved.")
    return redirect(f"{request.META.get('HTTP_REFERER') or '/inbox/'}")


@login_required
def reopen(request, pk):
    conversation = scoped_get_or_404(Conversation, request, pk=pk)
    conversation.status = (
        Conversation.Status.ASSIGNED if conversation.assigned_to_id else Conversation.Status.QUEUED
    )
    conversation.resolved_at = None
    conversation.save(update_fields=["status", "resolved_at"])
    return redirect("inbox:conversation", pk=pk)


@login_required
def add_note(request, pk):
    conversation = scoped_get_or_404(Conversation, request, pk=pk)
    form = NoteForm(request.POST)
    if form.is_valid():
        InternalNote.objects.create(
            workspace=conversation.workspace,
            conversation=conversation,
            author=request.user,
            body=form.cleaned_data["body"],
        )
        Message.objects.create(
            workspace=conversation.workspace,
            conversation=conversation,
            direction=Message.Direction.SYSTEM,
            actor=Message.Actor.AGENT,
            author=request.user,
            kind=Message.Kind.TEXT,
            body=form.cleaned_data["body"],
            payload={"note": True},
            wa_status=Message.Status.SENT,
        )
    return redirect("inbox:conversation", pk=pk)


@login_required
def snippet_search(request):
    query = request.GET.get("q", "").strip()
    return render(
        request,
        "inbox/partials/snippet_list.html",
        {
            "snippets": _snippets(request, query)[:10],
            "conversation_id": request.GET.get("conversation"),
        },
    )


@login_required
def media(request, pk):
    """Customer documents are never served straight off disk by nginx."""
    message = scoped_get_or_404(Message, request, pk=pk)
    if not message.media:
        raise Http404
    return FileResponse(message.media.open("rb"), filename=message.media_filename or "attachment")


@login_required
def ping(request):
    """Cheap endpoint the poll fallback hits when websockets are unavailable."""
    return HttpResponse(status=204)
