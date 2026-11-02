"""Changing a workflow: drafts, publishing, and editing one step at a time.

Pure functions over a graph wherever possible, so the step forms here and the
drag-and-drop builder share one set of rules.
"""

import copy

from django.db import transaction
from django.utils import timezone

from apps.core.audit import audit

from . import graph as g
from .models import WorkflowVersion


def editable_version(workflow, user=None):
    """The draft to edit: the existing one, or a fresh copy of the newest version."""
    draft = workflow.draft
    if draft is not None:
        return draft
    latest = workflow.latest
    return WorkflowVersion.objects.create(
        workflow=workflow,
        number=(latest.number + 1) if latest else 1,
        graph=copy.deepcopy(latest.graph) if latest else {"start": None, "steps": {}},
        created_by=user,
        note=f"Edited from version {latest.number}" if latest else "",
    )


def publish(workflow, version, *, user=None, request=None):
    """Make `version` the one new chats start on. Returns plain-language problems; empty = published."""
    found = g.problems(version.graph, workspace=workflow.workspace)
    if found:
        return found
    with transaction.atomic():
        version.published_at = timezone.now()
        version.published_by = user
        version.save(update_fields=["published_at", "published_by"])
        workflow.published = version
        workflow.save(update_fields=["published", "updated_at"])
    audit("workflow.published", request=request, workspace=workflow.workspace, actor=user,
          target=workflow, version=version.number)
    return []


def switch_on(workflow, *, user=None, request=None):
    """Switch a workflow on. Only one workflow takes new chats, so any other is switched off."""
    from .models import Workflow

    others = []
    if workflow.trigger == Workflow.Trigger.CHAT_OPENED:
        others = list(
            Workflow.objects.for_workspace(workflow.workspace)
            .filter(is_active=True, trigger=Workflow.Trigger.CHAT_OPENED)
            .exclude(pk=workflow.pk)
        )
        for other in others:
            other.is_active = False
            other.save(update_fields=["is_active", "updated_at"])
    workflow.is_active = True
    workflow.save(update_fields=["is_active", "updated_at"])
    audit("workflow.switched_on", request=request, workspace=workflow.workspace, actor=user,
          target=workflow, switched_off=[o.name for o in others])
    return others


# -- reading a step for people -----------------------------------------------


def summary(step, lookups):
    """One line saying what a step does, in plain words."""
    config = step.get("config") or {}
    kind = step.get("type")
    if kind == "send_message":
        parts = []
        if config.get("file_id"):
            parts.append(f"sends {lookups['files'].get(config['file_id'], 'a deleted file')}")
        if (config.get("text") or "").strip():
            parts.append(f"“{_clip(config['text'])}”")
        return " with ".join(parts) if parts else "nothing to send yet"
    if kind == "ask_question":
        question = f"“{_clip(config.get('text') or '')}”"
        if config.get("answer") == "choice":
            options = config.get("options") or []
            how = {"buttons": "tap buttons", "list": "a pick-list", "numbered": "a numbered list"}[
                g.presentation(options)
            ]
            return f"{question} — {len(options)} options as {how}"
        return f"{question} — {g.ANSWER_KINDS.get(config.get('answer'), 'any text').lower()}"
    if kind == "branch":
        return f"{len(config.get('rules') or [])} rule(s), then anything else"
    if kind == "update_contact":
        return f"sets {config.get('field') or '?'} to “{config.get('value') or ''}”"
    if kind == "audience":
        verb = "removes from" if config.get("action") == "remove" else "adds to"
        return f"{verb} {lookups['audiences'].get(config.get('audience_id'), 'a deleted audience')}"
    if kind == "assign":
        if config.get("to") == "agent" and config.get("user_id"):
            return f"gives the chat to {lookups['users'].get(config['user_id'], 'someone no longer here')}"
        return "gives the chat to the next available agent"
    if kind == "jump":
        return f"back to “{lookups['steps'].get(config.get('target'), '?')}”, at most {config.get('max_times', 3)} times"
    if kind == "webhook":
        return f"calls {lookups['connections'].get(config.get('connection_id'), 'a deleted webhook')}"
    if kind == "start_workflow":
        return f"hands over to {lookups['workflows'].get(config.get('workflow_id'), 'a deleted workflow')}"
    if kind == "office_hours":
        return "uses this workspace's working hours and holidays"
    if kind == "close":
        return "marks the chat resolved"
    return ""


def _clip(text, length=90):
    text = " ".join(str(text).split())
    return text if len(text) <= length else text[: length - 1] + "…"


def lookups(workspace, graph):
    from django.contrib.auth import get_user_model

    from apps.contacts.models import Audience

    from .models import WebhookConnection, Workflow, WorkflowFile

    users = get_user_model().objects.filter(workspace_memberships__workspace=workspace)
    return {
        "files": dict(WorkflowFile.objects.for_workspace(workspace).values_list("pk", "filename")),
        "connections": dict(WebhookConnection.objects.for_workspace(workspace).values_list("pk", "name")),
        "audiences": dict(Audience.objects.for_workspace(workspace).values_list("pk", "name")),
        "workflows": dict(Workflow.objects.for_workspace(workspace).values_list("pk", "name")),
        "users": {u.pk: str(u) for u in users},
        "steps": {sid: (s.get("name") or g.STEP_TYPES.get(s.get("type"), {}).get("label", sid))
                  for sid, s in (graph.get("steps") or {}).items()},
    }


# -- changing the graph ------------------------------------------------------


def add_step(graph, kind, *, after=None, exit_key=None):
    """Add a step of `kind`. Placed after `after`'s `exit_key`, it takes over what that exit led to."""
    if kind not in g.STEP_TYPES:
        raise ValueError("unknown step type")
    steps = graph.setdefault("steps", {})
    step_id = g.new_step_id(graph, kind)
    step = {"type": kind, "name": g.STEP_TYPES[kind]["label"], "config": _defaults(kind), "next": {}}
    if after and after in steps:
        exits = g.exits_for(steps[after])
        exit_key = exit_key if exit_key in exits else (exits[0] if exits else None)
        if exit_key:
            previous = (steps[after].get("next") or {}).get(exit_key)
            own = g.exits_for(step)
            if previous and own:
                step["next"][own[0]] = previous
            steps[after].setdefault("next", {})[exit_key] = step_id
    steps[step_id] = step
    if not graph.get("start"):
        graph["start"] = step_id
    return step_id


def _defaults(kind):
    return {
        "ask_question": {"text": "", "answer": "choice", "options": [], "retries": 2,
                         "timeout_minutes": g.DEFAULT_TIMEOUT_MINUTES},
        "branch": {"rules": []},
        "jump": {"target": None, "max_times": 3},
        "assign": {"to": "auto"},
        "audience": {"action": "add"},
        "send_message": {"text": ""},
    }.get(kind, {})


def remove_step(graph, step_id):
    """Take a step out. Whatever led to it now leads where it led (its first exit). Returns an error or None."""
    steps = graph.get("steps") or {}
    step = steps.get(step_id)
    if step is None:
        return "That step no longer exists."
    exits = g.exits_for(step)
    onward = (step.get("next") or {}).get(exits[0]) if exits else None
    if onward == step_id:
        onward = None
    for other_id, other in steps.items():
        if other_id == step_id:
            continue
        for key, target in list((other.get("next") or {}).items()):
            if target == step_id:
                other["next"][key] = onward
        config = other.get("config") or {}
        if other.get("type") == "jump" and config.get("target") == step_id:
            config["target"] = onward
    if graph.get("start") == step_id:
        graph["start"] = onward
    del steps[step_id]
    return None


def apply_form(graph, step_id, data):
    """Save one step's settings from a submitted form. Returns a list of plain-language errors."""
    step = (graph.get("steps") or {}).get(step_id)
    if step is None:
        return ["That step no longer exists."]
    errors = []
    kind = step["type"]
    step["name"] = (data.get("name") or "").strip()[:120] or g.STEP_TYPES[kind]["label"]
    config = step.setdefault("config", {})

    def text(key, limit=4096):
        return (data.get(key) or "").strip()[:limit]

    def number(key, default=None, low=None):
        raw = (data.get(key) or "").strip()
        if not raw:
            return default
        try:
            value = int(raw)
        except ValueError:
            errors.append(f"'{raw}' is not a whole number.")
            return default
        return max(low, value) if low is not None else value

    def pk(key):
        raw = (data.get(key) or "").strip()
        return int(raw) if raw.isdigit() else None

    if kind == "send_message":
        config["text"] = text("text")
        config["file_id"] = pk("file_id")
        if not config["text"] and not config["file_id"]:
            errors.append("Write a message or choose a file to send.")
    elif kind == "ask_question":
        config["text"] = text("text", 1024)
        config["answer"] = data.get("answer") if data.get("answer") in g.ANSWER_KINDS else "text"
        options = [" ".join(line.split()) for line in (data.get("options") or "").splitlines() if line.strip()]
        config["options"] = options if config["answer"] == "choice" else []
        if config["answer"] == "choice" and not options:
            errors.append("Add at least one option, one per line.")
        for bound in ("min", "max"):
            config[bound] = number(bound) if config["answer"] == "number" else None
        save_name = text("save_name", 60)
        config["save_to"] = (
            {"kind": "contact" if data.get("save_kind") == "contact" else "variable", "name": save_name}
            if save_name else {}
        )
        config["retries"] = number("retries", 2, 0)
        config["invalid_text"] = text("invalid_text", 1024)
        hours = number("timeout_hours", None, 1)
        config["timeout_minutes"] = hours * 60 if hours else g.DEFAULT_TIMEOUT_MINUTES
        if not config["text"]:
            errors.append("Write the question.")
    elif kind == "branch":
        config["rules"] = _rules_from(data, config.get("rules") or [], errors)
    elif kind == "update_contact":
        config["field"] = text("field", 60)
        config["value"] = text("value", 500)
        if not config["field"]:
            errors.append("Say which contact detail to update.")
    elif kind == "audience":
        config["action"] = "remove" if data.get("action") == "remove" else "add"
        config["audience_id"] = pk("audience_id")
        if not config["audience_id"]:
            errors.append("Choose an audience.")
    elif kind == "assign":
        config["to"] = "agent" if data.get("to") == "agent" else "auto"
        config["user_id"] = pk("user_id") if config["to"] == "agent" else None
        config["note"] = text("note", 200)
        if config["to"] == "agent" and not config["user_id"]:
            errors.append("Choose the agent.")
    elif kind == "close":
        config["note"] = text("note", 200)
    elif kind == "jump":
        config["target"] = data.get("target") if data.get("target") in graph["steps"] else None
        config["max_times"] = number("max_times", 3, 1)
        if not config["target"]:
            errors.append("Choose the step to jump to.")
    elif kind == "webhook":
        config["connection_id"] = pk("connection_id")
        config["save_as"] = text("save_as", 60)
        if not config["connection_id"]:
            errors.append("Choose the webhook to call.")
    elif kind == "start_workflow":
        config["workflow_id"] = pk("workflow_id")
        if not config["workflow_id"]:
            errors.append("Choose the workflow to start.")

    nxt = {}
    for exit_key in g.exits_for(step):
        target = data.get(f"next_{exit_key}") or None
        nxt[exit_key] = target if target in graph["steps"] else None
    step["next"] = nxt
    if data.get("make_start") and step_id in graph["steps"]:
        graph["start"] = step_id
    return errors


def _rules_from(data, existing, errors):
    """Branch rules from rows named rule-<i>-* and rule-<i>-c<j>-*."""
    rules = []
    count = int(data.get("rule_count") or 0)
    for i in range(count):
        prefix = f"rule-{i}-"
        if data.get(prefix + "delete"):
            continue
        name = (data.get(prefix + "name") or "").strip()[:80]
        conditions = []
        for j in range(int(data.get(prefix + "condition_count") or 0)):
            c = f"{prefix}c{j}-"
            field = (data.get(c + "field") or "").strip()[:60]
            operator = data.get(c + "operator") if data.get(c + "operator") in g.OPERATORS else "equals"
            if not field:
                continue
            conditions.append({
                "source": "contact" if data.get(c + "source") == "contact" else "variable",
                "field": field,
                "operator": operator,
                "value": (data.get(c + "value") or "").strip()[:200],
            })
        if not name and not conditions:
            continue
        rule_id = (data.get(prefix + "id") or "").strip() or _new_rule_id(rules + existing)
        if not conditions:
            errors.append(f"The rule '{name or 'new rule'}' needs at least one condition.")
        rules.append({
            "id": rule_id,
            "name": name or f"Rule {len(rules) + 1}",
            "match": "any" if data.get(prefix + "match") == "any" else "all",
            "conditions": conditions,
        })
    return rules


def _new_rule_id(rules):
    taken = {r.get("id") for r in rules}
    index = 1
    while f"rule{index}" in taken:
        index += 1
    return f"rule{index}"


START = "__start"


def connect(graph, source, exit_key, target):
    """Point one exit (or the start) at a step. Returns an error or None."""
    steps = graph.get("steps") or {}
    if target not in steps:
        return "That step no longer exists."
    if source == START:
        graph["start"] = target
        return None
    step = steps.get(source)
    if step is None or exit_key not in g.exits_for(step):
        return "That connection does not exist any more. Reload the builder."
    step.setdefault("next", {})[exit_key] = target
    return None


def disconnect(graph, source, exit_key):
    if source == START:
        graph["start"] = None
        return None
    step = (graph.get("steps") or {}).get(source)
    if step is None:
        return "That step no longer exists."
    if exit_key in (step.get("next") or {}):
        step["next"][exit_key] = None
    return None
