from django.contrib import messages as flash
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.shortcuts import redirect, render

from apps.core.audit import audit
from apps.core.scoping import scoped_get_or_404

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
