"""Workflow pages: the list, one workflow's steps, step settings, webhooks and files."""

import json

from django.contrib import messages as flash
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from apps.core.audit import audit
from apps.core.scoping import require_role, scoped_get_or_404
from apps.workspaces.models import WorkspaceMembership

from . import editing
from . import graph as g
from .models import RunEvent, WebhookConnection, Workflow, WorkflowFile, WorkflowRun, WorkflowVersion

FILE_TYPES = {
    ".pdf": "application/pdf",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".mp4": "video/mp4",
}
MAX_FILE_MB = 16


def _manager(request):
    if request.workspace is None:
        return False
    require_role(request, *WorkspaceMembership.MANAGE_ROLES)
    return True


@login_required
def workflow_list(request):
    if not _manager(request):
        return redirect("workspaces:list")
    if request.method == "POST":
        name = (request.POST.get("name") or "").strip()[:120]
        if not name:
            flash.error(request, "Give the workflow a name.")
        elif Workflow.objects.for_request(request).filter(name__iexact=name).exists():
            flash.error(request, f"There is already a workflow called '{name}'.")
        else:
            workflow = Workflow.objects.create(workspace=request.workspace, name=name)
            WorkflowVersion.objects.create(workflow=workflow, number=1, graph={"start": None, "steps": {}},
                                           created_by=request.user)
            audit("workflow.created", request=request, target=workflow)
            return redirect("workflows:detail", pk=workflow.pk)
    workflows = Workflow.objects.for_request(request).annotate(
        live=Count("runs", filter=Q(runs__status__in=WorkflowRun.LIVE_STATUSES)),
        total=Count("runs"),
    ).select_related("published")
    return render(request, "workflows/list.html", {"workflows": workflows})


@login_required
@require_POST
def workflow_import(request):
    from .respondio import ImportError_, import_export

    if not _manager(request):
        return redirect("workspaces:list")
    upload = request.FILES.get("file")
    if upload is None:
        flash.error(request, "Choose the respond.io export file (.json) first.")
        return redirect("workflows:list")
    try:
        data = json.loads(upload.read().decode("utf-8-sig"))
        workflow, notes = import_export(request.workspace, data, user=request.user)
    except (UnicodeDecodeError, json.JSONDecodeError):
        flash.error(request, "That file is not a respond.io export. In respond.io, open the workflow and "
                             "choose Export to get a .json file.")
        return redirect("workflows:list")
    except ImportError_ as exc:
        flash.error(request, str(exc))
        return redirect("workflows:list")
    draft = workflow.draft
    draft.graph.setdefault("source", {})["notes"] = notes
    draft.save(update_fields=["graph"])
    audit("workflow.imported", request=request, target=workflow, source="respond.io", notes=len(notes))
    flash.success(request, f"'{workflow.name}' imported as a draft. Check the notes below, then publish it.")
    return redirect("workflows:detail", pk=workflow.pk)


def _back(request, workflow):
    """Where a form should land afterwards: the builder or list view it came from."""
    target = request.POST.get("next") or ""
    if target.startswith(f"/workflows/{workflow.pk}/"):
        return redirect(target)
    return redirect("workflows:detail", pk=workflow.pk)


def _shown_version(workflow):
    return workflow.draft or workflow.published or workflow.latest


@login_required
def workflow_detail(request, pk):
    if not _manager(request):
        return redirect("workspaces:list")
    workflow = scoped_get_or_404(Workflow, request, pk=pk)
    version = _shown_version(workflow)
    graph = version.graph if version else {"steps": {}}
    names = editing.lookups(request.workspace, graph)
    ordered = g.reachable(graph)
    loose = [sid for sid in (graph.get("steps") or {}) if sid not in ordered]
    steps = []
    for step_id in ordered + loose:
        step = graph["steps"][step_id]
        steps.append({
            "id": step_id,
            "step": step,
            "label": g.STEP_TYPES.get(step.get("type"), {}).get("label", step.get("type")),
            "summary": editing.summary(step, names),
            "exits": [
                {"key": key, "label": g.exit_label(step, key),
                 "target": (step.get("next") or {}).get(key),
                 "target_name": names["steps"].get((step.get("next") or {}).get(key))}
                for key in g.exits_for(step)
            ],
            "is_start": step_id == graph.get("start"),
            "unreached": step_id in loose,
        })
    return render(request, "workflows/detail.html", {
        "workflow": workflow,
        "version": version,
        "steps": steps,
        "problems": g.problems(graph, workspace=request.workspace) if version and version.editable else [],
        "notes": ((graph.get("source") or {}).get("notes") or []) if version and version.editable else [],
        "versions": workflow.versions.select_related("published_by")[:10],
        "step_types": [(k, v["label"]) for k, v in g.STEP_TYPES.items()],
        "recent_runs": workflow.runs.select_related("contact").order_by("-created_at")[:15],
    })


@login_required
@require_POST
def workflow_settings(request, pk):
    if not _manager(request):
        return redirect("workspaces:list")
    workflow = scoped_get_or_404(Workflow, request, pk=pk)
    name = (request.POST.get("name") or "").strip()[:120]
    if not name:
        flash.error(request, "Give the workflow a name.")
    elif Workflow.objects.for_request(request).filter(name__iexact=name).exclude(pk=workflow.pk).exists():
        flash.error(request, f"There is already a workflow called '{name}'.")
    else:
        workflow.name = name
        workflow.description = (request.POST.get("description") or "").strip()[:255]
        quiet = (request.POST.get("quiet_hours") or "").strip()
        if quiet.isdigit():
            workflow.quiet_after_our_message_hours = min(int(quiet), 720)
        if request.POST.get("trigger") in Workflow.Trigger.values:
            workflow.trigger = request.POST["trigger"]
            if workflow.trigger != Workflow.Trigger.CHAT_OPENED and workflow.is_active:
                workflow.is_active = False
        workflow.save()
        audit("workflow.settings_changed", request=request, target=workflow)
        flash.success(request, "Saved.")
    return redirect("workflows:detail", pk=workflow.pk)


@login_required
@require_POST
def workflow_publish(request, pk):
    if not _manager(request):
        return redirect("workspaces:list")
    workflow = scoped_get_or_404(Workflow, request, pk=pk)
    draft = workflow.draft
    if draft is None:
        flash.info(request, "There are no unpublished changes.")
        return _back(request, workflow)
    found = editing.publish(workflow, draft, user=request.user, request=request)
    if found:
        flash.error(request, "Not published yet - fix the problems listed first.")
    else:
        flash.success(request, f"Version {draft.number} is published. New chats use it from now on; chats already "
                               "part-way through finish on the version they started.")
    return _back(request, workflow)


@login_required
@require_POST
def workflow_discard(request, pk):
    if not _manager(request):
        return redirect("workspaces:list")
    workflow = scoped_get_or_404(Workflow, request, pk=pk)
    draft = workflow.draft
    if draft is not None and workflow.versions.count() > 1 and not draft.runs.exists():
        draft.delete()
        flash.success(request, "Unpublished changes thrown away.")
    return redirect("workflows:detail", pk=workflow.pk)


@login_required
@require_POST
def workflow_toggle(request, pk):
    if not _manager(request):
        return redirect("workspaces:list")
    workflow = scoped_get_or_404(Workflow, request, pk=pk)
    if request.POST.get("on") == "1":
        if workflow.published is None:
            flash.error(request, "Publish the workflow before switching it on.")
        elif workflow.trigger != Workflow.Trigger.CHAT_OPENED:
            flash.error(request, "This workflow only runs when another workflow starts it, so there is "
                                 "nothing to switch on.")
        else:
            others = editing.switch_on(workflow, user=request.user, request=request)
            note = f" '{others[0].name}' was switched off - only one workflow can greet new chats." if others else ""
            flash.success(request, f"'{workflow.name}' now greets every new chat.{note}")
    else:
        workflow.is_active = False
        workflow.save(update_fields=["is_active", "updated_at"])
        audit("workflow.switched_off", request=request, target=workflow)
        flash.success(request, f"'{workflow.name}' is off. New chats go straight to the team again.")
    return _back(request, workflow)


@login_required
@require_POST
def workflow_delete(request, pk):
    if not _manager(request):
        return redirect("workspaces:list")
    workflow = scoped_get_or_404(Workflow, request, pk=pk)
    if workflow.runs.filter(status__in=WorkflowRun.LIVE_STATUSES).exists():
        flash.error(request, "People are part-way through this workflow. Switch it off and try again later.")
        return redirect("workflows:detail", pk=workflow.pk)
    name = workflow.name
    workflow.delete()
    audit("workflow.deleted", request=request, name=name)
    flash.success(request, f"'{name}' deleted.")
    return redirect("workflows:list")


# -- steps -------------------------------------------------------------------


@login_required
def step_edit(request, pk, step_id):
    if not _manager(request):
        return redirect("workspaces:list")
    workflow = scoped_get_or_404(Workflow, request, pk=pk)
    version = _shown_version(workflow)
    if request.method == "POST":
        version = editing.editable_version(workflow, request.user)
        errors = editing.apply_form(version.graph, step_id, request.POST)
        version.save(update_fields=["graph", "updated_at"])
        for error in errors:
            flash.error(request, error)
        if not errors:
            flash.success(request, "Step saved to the draft.")
            return redirect(f"{_detail_url(workflow)}#step-{step_id}")
    graph = version.graph if version else {}
    step = (graph.get("steps") or {}).get(step_id)
    if step is None:
        flash.error(request, "That step no longer exists.")
        return redirect("workflows:detail", pk=workflow.pk)
    return render(request, "workflows/step_edit.html", _step_context(request, workflow, graph, step_id, step))


def _detail_url(workflow):
    from django.urls import reverse

    return reverse("workflows:detail", args=[workflow.pk])


def _step_context(request, workflow, graph, step_id, step):
    from apps.contacts.models import Audience

    names = editing.lookups(request.workspace, graph)
    config = step.get("config") or {}
    rules = []
    for rule in config.get("rules") or []:
        rules.append({**rule, "conditions": list(rule.get("conditions") or []) + [{}]})
    rules.append({"id": "", "name": "", "match": "all", "conditions": [{}, {}]})
    return {
        "workflow": workflow,
        "step_id": step_id,
        "step": step,
        "config": config,
        "label": g.STEP_TYPES.get(step["type"], {}).get("label"),
        "exits": [{"key": k, "label": g.exit_label(step, k), "target": (step.get("next") or {}).get(k)}
                  for k in g.exits_for(step)],
        "other_steps": [(sid, names["steps"][sid]) for sid in g.reachable(graph) + [
            s for s in graph["steps"] if s not in g.reachable(graph)]],
        "is_start": graph.get("start") == step_id,
        "options_text": "\n".join(config.get("options") or []),
        "timeout_hours": (config.get("timeout_minutes") or g.DEFAULT_TIMEOUT_MINUTES) // 60,
        "rules": rules,
        "operators": list(g.OPERATORS.items()),
        "answer_kinds": list(g.ANSWER_KINDS.items()),
        "files": WorkflowFile.objects.for_request(request),
        "connections": WebhookConnection.objects.for_request(request),
        "audiences": Audience.objects.for_request(request),
        "workflows": Workflow.objects.for_request(request).exclude(pk=workflow.pk),
        "members": WorkspaceMembership.objects.filter(workspace=request.workspace).select_related("user"),
        "presentation": g.presentation(config.get("options") or []) if config.get("options") else "",
    }


@login_required
@require_POST
def step_add(request, pk):
    if not _manager(request):
        return redirect("workspaces:list")
    workflow = scoped_get_or_404(Workflow, request, pk=pk)
    kind = request.POST.get("kind")
    if kind not in g.STEP_TYPES:
        flash.error(request, "Choose what kind of step to add.")
        return redirect("workflows:detail", pk=workflow.pk)
    version = editing.editable_version(workflow, request.user)
    step_id = editing.add_step(version.graph, kind, after=request.POST.get("after") or None,
                               exit_key=request.POST.get("exit") or None)
    version.save(update_fields=["graph", "updated_at"])
    return redirect("workflows:step_edit", pk=workflow.pk, step_id=step_id)


@login_required
@require_POST
def step_delete(request, pk, step_id):
    if not _manager(request):
        return redirect("workspaces:list")
    workflow = scoped_get_or_404(Workflow, request, pk=pk)
    version = editing.editable_version(workflow, request.user)
    error = editing.remove_step(version.graph, step_id)
    version.save(update_fields=["graph", "updated_at"])
    if error:
        flash.error(request, error)
    else:
        flash.success(request, "Step removed from the draft. Whatever led to it now leads to the step after it.")
    return redirect("workflows:detail", pk=workflow.pk)


# -- runs --------------------------------------------------------------------


@login_required
def run_detail(request, pk):
    if not _manager(request):
        return redirect("workspaces:list")
    run = scoped_get_or_404(WorkflowRun, request, pk=pk)
    return render(request, "workflows/run_detail.html", {
        "run": run,
        "events": RunEvent.objects.for_request(request).filter(run=run),
    })


@login_required
@require_POST
def stop_conversation(request, conversation_pk):
    """The Stop workflow button in a chat: any team member may take a chat off the bot."""
    from apps.inbox.models import Conversation

    from .engine import stop

    if request.workspace is None:
        return redirect("workspaces:list")
    conversation = scoped_get_or_404(Conversation, request, pk=conversation_pk)
    stop(conversation, f"{request.user} stopped it")
    if conversation.status == Conversation.Status.BOT and not conversation.assigned_to_id:
        conversation.status = Conversation.Status.QUEUED
        conversation.save(update_fields=["status"])
    audit("workflow.stopped_in_chat", request=request, target=conversation)
    flash.success(request, "The workflow has stopped for this chat. It is with the team now.")
    return redirect("inbox:conversation", pk=conversation.pk)


# -- webhooks and files ------------------------------------------------------


@login_required
def connections(request):
    if not _manager(request):
        return redirect("workspaces:list")
    if request.method == "POST":
        name = (request.POST.get("name") or "").strip()[:120]
        url = (request.POST.get("url") or "").strip()
        if not name or not url.startswith("https://"):
            flash.error(request, "Give the webhook a name and an https:// web address.")
        elif WebhookConnection.objects.for_request(request).filter(name__iexact=name).exists():
            flash.error(request, f"There is already a webhook called '{name}'.")
        else:
            connection = WebhookConnection.objects.create(
                workspace=request.workspace, name=name, url=url[:500],
                token=(request.POST.get("token") or "").strip() or WebhookConnection.new_token(),
            )
            audit("webhook.created", request=request, target=connection, url=connection.url)
            flash.success(request, f"'{name}' added. Give its token to whoever runs the receiving end.")
        return redirect("workflows:connections")
    return render(request, "workflows/connections.html", {
        "connections": WebhookConnection.objects.for_request(request),
    })


@login_required
@require_POST
def connection_update(request, pk):
    if not _manager(request):
        return redirect("workspaces:list")
    connection = scoped_get_or_404(WebhookConnection, request, pk=pk)
    action = request.POST.get("action")
    if action == "delete":
        connection.delete()
        audit("webhook.deleted", request=request, name=connection.name)
        flash.success(request, f"'{connection.name}' deleted. Workflows that used it will say so before publishing.")
    elif action == "new_token":
        connection.token = WebhookConnection.new_token()
        connection.save(update_fields=["token", "updated_at"])
        audit("webhook.token_changed", request=request, target=connection)
        flash.success(request, f"'{connection.name}' has a new token. The old one stops working now.")
    elif action == "toggle":
        connection.is_active = not connection.is_active
        connection.save(update_fields=["is_active", "updated_at"])
        audit("webhook.switched", request=request, target=connection, on=connection.is_active)
    return redirect("workflows:connections")


@login_required
def files(request):
    from pathlib import Path

    if not _manager(request):
        return redirect("workspaces:list")
    if request.method == "POST":
        upload = request.FILES.get("file")
        mime = FILE_TYPES.get(Path(getattr(upload, "name", "")).suffix.lower()) if upload else None
        if upload is None:
            flash.error(request, "Choose a file first.")
        elif mime is None:
            flash.error(request, "WhatsApp can send PDFs, JPG and PNG pictures, and MP4 videos.")
        elif upload.size > MAX_FILE_MB * 1024 * 1024:
            flash.error(request, f"That file is too big. Keep it under {MAX_FILE_MB} MB.")
        else:
            stored = WorkflowFile(workspace=request.workspace, filename=upload.name[:255], mime=mime)
            stored.file.save(upload.name, upload, save=False)
            stored.save()
            audit("workflow_file.uploaded", request=request, target=stored)
            flash.success(request, f"'{stored.filename}' uploaded. Choose it in a 'Send a message' step.")
        return redirect("workflows:files")
    return render(request, "workflows/files.html", {"files": WorkflowFile.objects.for_request(request)})


@login_required
@require_POST
def file_delete(request, pk):
    if not _manager(request):
        return redirect("workspaces:list")
    stored = scoped_get_or_404(WorkflowFile, request, pk=pk)
    name = stored.filename
    stored.file.delete(save=False)
    stored.delete()
    audit("workflow_file.deleted", request=request, name=name)
    flash.success(request, f"'{name}' deleted. Workflows that sent it will say so before publishing.")
    return redirect("workflows:files")


@login_required
def file_download(request, pk):
    from django.http import FileResponse

    if not _manager(request):
        return redirect("workspaces:list")
    stored = scoped_get_or_404(WorkflowFile, request, pk=pk)
    return FileResponse(stored.file.open("rb"), filename=stored.filename)
