"""The drag-and-drop builder: one page, a JSON feed for the canvas, and small edits.

The server stays the only place a workflow is changed. The canvas sends one
small operation at a time (connect these, move that, add a step) and gets
back the whole workflow to redraw from; the step settings form is the same
one the step page uses, shown in a side panel.
"""

import datetime as dt
import json

from django.contrib import messages as flash
from django.contrib.auth.decorators import login_required
from django.db.models import Count
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.contacts.models import normalise_msisdn
from apps.core.audit import audit
from apps.core.scoping import scoped_get_or_404

from . import editing
from . import graph as g
from . import layout, simulate
from .models import PhoneTest, RunEvent, Workflow, WorkflowRun
from .views import _manager, _shown_version, _step_context

COUNT_DAYS = 30
SIM_KEY = "workflow_sim"


def _node(step_id, step, names, position, counts):
    exits = g.exits_for(step)
    config = step.get("config") or {}
    is_jump = step.get("type") == "jump"
    return {
        "id": step_id,
        "type": step.get("type"),
        "label": g.STEP_TYPES.get(step.get("type"), {}).get("label", step.get("type")),
        "name": step.get("name") or "",
        "summary": editing.summary(step, names),
        "exits": [{"key": k, "label": g.exit_label(step, k)} for k in exits],
        "next": {k: (step.get("next") or {}).get(k) for k in exits},
        "jump_to": config.get("target") if is_jump else None,
        "x": position.get("x", 0),
        "y": position.get("y", 0),
        "reached": counts["reached"].get(step_id, 0),
        "ended": counts["ended"].get(step_id, 0),
    }


def _counts(workflow):
    since = timezone.now() - dt.timedelta(days=COUNT_DAYS)
    reached = dict(
        RunEvent.objects.filter(run__workflow=workflow, at__gte=since)
        .exclude(step_id="")
        .values_list("step_id")
        .annotate(n=Count("run", distinct=True))
    )
    ended = dict(
        WorkflowRun.objects.filter(workflow=workflow, created_at__gte=since, ended_at__isnull=False)
        .exclude(current_step="")
        .values_list("current_step")
        .annotate(n=Count("id"))
    )
    started = WorkflowRun.objects.filter(workflow=workflow, created_at__gte=since).count()
    return {"reached": reached, "ended": ended, "started": started}


def _payload(request, workflow, version):
    graph = version.graph if version else {"start": None, "steps": {}}
    names = editing.lookups(request.workspace, graph)
    positions = layout.positions_for(workflow, graph)
    counts = _counts(workflow)
    return {
        "version": version.number if version else None,
        "editable": bool(version and version.editable),
        "published": workflow.published.number if workflow.published_id else None,
        "is_active": workflow.is_active,
        "start": graph.get("start"),
        "start_pos": (workflow.layout or {}).get(editing.START),
        "nodes": [_node(sid, step, names, positions.get(sid, {}), counts)
                  for sid, step in (graph.get("steps") or {}).items()],
        "checks": g.checks(graph, workspace=request.workspace),
        "started": counts["started"],
        "count_days": COUNT_DAYS,
    }


@login_required
def builder(request, pk):
    if not _manager(request):
        return redirect("workspaces:list")
    workflow = scoped_get_or_404(Workflow, request, pk=pk)
    return render(request, "workflows/builder.html", {
        "workflow": workflow,
        "step_types": [(k, v["label"]) for k, v in g.STEP_TYPES.items()],
    })


@login_required
def builder_data(request, pk):
    if not _manager(request):
        return redirect("workspaces:list")
    workflow = scoped_get_or_404(Workflow, request, pk=pk)
    return JsonResponse(_payload(request, workflow, _shown_version(workflow)))


@login_required
@require_POST
def builder_op(request, pk):
    """One edit from the canvas. Returns the whole workflow again, so the canvas never drifts."""
    if not _manager(request):
        return redirect("workspaces:list")
    workflow = scoped_get_or_404(Workflow, request, pk=pk)
    try:
        op = json.loads(request.body or b"{}")
    except ValueError:
        return JsonResponse({"error": "That request could not be read."}, status=400)
    kind = op.get("op")

    # Positions are the workflow's, not a version's: moving a box needs no draft.
    if kind in ("move", "tidy"):
        version = _shown_version(workflow)
        if kind == "tidy":
            workflow.layout = layout.tidy(version.graph if version else {})
        else:
            for step_id, pos in (op.get("positions") or {}).items():
                try:
                    workflow.layout[str(step_id)] = {"x": round(float(pos["x"])), "y": round(float(pos["y"]))}
                except (KeyError, TypeError, ValueError):
                    continue
        workflow.save(update_fields=["layout"])
        return JsonResponse(_payload(request, workflow, version) if kind == "tidy" else {"ok": True})

    version = editing.editable_version(workflow, request.user)
    graph = version.graph
    error, added = None, None
    if kind == "connect":
        error = editing.connect(graph, op.get("from"), op.get("exit"), op.get("to"))
    elif kind == "disconnect":
        error = editing.disconnect(graph, op.get("from"), op.get("exit"))
    elif kind == "add":
        if op.get("kind") not in g.STEP_TYPES:
            error = "Choose what kind of step to add."
        else:
            added = editing.add_step(graph, op["kind"])
            try:
                workflow.layout[added] = {"x": round(float(op.get("x", 0))), "y": round(float(op.get("y", 0)))}
            except (TypeError, ValueError):
                workflow.layout[added] = {"x": 0, "y": 0}
            workflow.save(update_fields=["layout"])
    elif kind == "delete":
        error = editing.remove_step(graph, op.get("id"))
    else:
        error = "Unknown change."
    version.save(update_fields=["graph", "updated_at"])
    data = _payload(request, workflow, version)
    if error:
        data["error"] = error
    if added:
        data["added"] = added
    return JsonResponse(data, status=400 if error else 200)


@login_required
def builder_panel(request, pk, step_id):
    """The side panel's step form: shown on GET, saved on POST (HTMX swaps it in place)."""
    if not _manager(request):
        return redirect("workspaces:list")
    workflow = scoped_get_or_404(Workflow, request, pk=pk)
    version = _shown_version(workflow)
    errors, saved = [], False
    if request.method == "POST":
        version = editing.editable_version(workflow, request.user)
        errors = editing.apply_form(version.graph, step_id, request.POST)
        version.save(update_fields=["graph", "updated_at"])
        saved = not errors
    graph = version.graph if version else {"steps": {}}
    step = (graph.get("steps") or {}).get(step_id)
    if step is None:
        return HttpResponse('<p class="p-6 text-sm text-slate-500">That step no longer exists.</p>')
    context = _step_context(request, workflow, graph, step_id, step)
    context.update(errors=errors, saved=saved)
    response = render(request, "workflows/partials/builder_panel.html", context)
    if request.method == "POST":
        response["HX-Trigger"] = json.dumps({"workflowChanged": {"step": step_id}})
    return response


def _sim_graphs(request, workflow):
    """Graph lookup for the test chat: this workflow's draft, other workflows' published version."""
    def lookup(workflow_id):
        if workflow_id == workflow.pk:
            version = _shown_version(workflow)
            return version.graph if version else None
        other = Workflow.objects.for_request(request).filter(pk=workflow_id).select_related("published").first()
        return other.published.graph if other and other.published else None
    return lookup


@login_required
@require_POST
def builder_simulate(request, pk):
    if not _manager(request):
        return redirect("workspaces:list")
    workflow = scoped_get_or_404(Workflow, request, pk=pk)
    try:
        body = json.loads(request.body or b"{}")
    except ValueError:
        body = {}
    graphs = _sim_graphs(request, workflow)
    store = request.session.get(SIM_KEY) or {}
    key = str(workflow.pk)
    action = body.get("action")
    if action == "start" or key not in store:
        state = simulate.new_state(workflow.pk, after_hours=bool(body.get("after_hours")))
        out = simulate.start(state, graphs, request.workspace)
    else:
        state = store[key]
        if action == "timeout":
            out = simulate.timeout(state, graphs, request.workspace)
        else:
            out = simulate.reply(state, graphs, request.workspace, text=(body.get("text") or "")[:1000],
                                 reply_id=body.get("reply_id") or "")
    store[key] = state
    request.session[SIM_KEY] = store
    return JsonResponse({"messages": out, "waiting": state["waiting"], "ended": state["ended"],
                         "step": state["step"]})


@login_required
@require_POST
def builder_phone_test(request, pk):
    if not _manager(request):
        return redirect("workspaces:list")
    workflow = scoped_get_or_404(Workflow, request, pk=pk)
    version = _shown_version(workflow)
    wa_id = normalise_msisdn(request.POST.get("number") or "")
    if version is None or not version.graph.get("start"):
        flash.error(request, "Add some steps before testing.")
    elif len(wa_id) < 10:
        flash.error(request, "Enter your own mobile number, e.g. 082 123 4567.")
    else:
        PhoneTest.objects.for_request(request).filter(wa_id=wa_id, used_at__isnull=True).update(
            used_at=timezone.now()
        )
        PhoneTest.objects.create(
            workspace=request.workspace, workflow=workflow, version=version, wa_id=wa_id,
            expires_at=timezone.now() + dt.timedelta(minutes=30), created_by=request.user,
        )
        audit("workflow.phone_test", request=request, target=workflow, version=version.number, number=wa_id)
        flash.success(
            request,
            f"Ready. Within 30 minutes, send any message from +{wa_id} to this workspace's WhatsApp number: "
            f"that chat runs version {version.number} instead of the live one.",
        )
    return redirect("workflows:builder", pk=workflow.pk)
