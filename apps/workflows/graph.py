"""The shape of a workflow and the rules every step follows.

A graph is plain JSON so the builder can edit it and a version can be copied
whole:

    {"start": "welcome",
     "steps": {
        "welcome": {"type": "send_message", "name": "Welcome",
                    "config": {"text": "Hi!"}, "next": {"next": "menu"}},
        "menu":    {"type": "ask_question", ...,
                    "next": {"answered": "route", "invalid": "menu"}}}}

`next` maps each of a step's exits to the step it leads to. An exit that
leads nowhere ends the workflow there.
"""

import re

STEP_TYPES = {
    "send_message": {"label": "Send a message", "exits": ["next"]},
    "ask_question": {"label": "Ask a question", "exits": ["answered", "invalid", "timeout"]},
    "branch": {"label": "Branch", "exits": []},  # one exit per rule, plus "else"
    "office_hours": {"label": "Office hours", "exits": ["open", "closed"]},
    "update_contact": {"label": "Update a contact detail", "exits": ["next"]},
    "audience": {"label": "Add to or remove from an audience", "exits": ["next"]},
    "assign": {"label": "Hand to an agent", "exits": []},
    "close": {"label": "Close the chat", "exits": []},
    "jump": {"label": "Jump to another step", "exits": ["limit"]},
    "webhook": {"label": "Call a webhook", "exits": ["success", "failure"]},
    "start_workflow": {"label": "Start another workflow", "exits": []},
    "end": {"label": "End", "exits": []},
}

EXIT_LABELS = {
    "next": "Then",
    "answered": "Answered",
    "invalid": "Not a valid answer",
    "timeout": "No answer in time",
    "open": "Within office hours",
    "closed": "Outside office hours",
    "limit": "Jumped too many times",
    "success": "It worked",
    "failure": "It failed",
    "else": "Anything else",
}

ANSWER_KINDS = {
    "choice": "Pick from a list",
    "number": "A number",
    "text": "Any text",
}

OPERATORS = {
    "equals": "is",
    "not_equals": "is not",
    "contains": "contains",
    "is_set": "has a value",
    "is_empty": "is empty",
}

# WhatsApp's own limits on tap buttons and list rows.
MAX_BUTTONS, BUTTON_LABEL = 3, 20
MAX_LIST_ROWS, LIST_LABEL = 10, 24

DEFAULT_TIMEOUT_MINUTES = 7 * 24 * 60
MAX_STEPS_PER_RUN = 400


def normalise(text):
    """How two answers are compared: case, spacing and invisible spaces ignored.

    Exports from other tools carry non-breaking spaces and stray leading
    spaces in option labels; a strict compare silently sends people down the
    wrong branch, so they never count here.
    """
    text = str(text or "").replace(" ", " ").replace("​", "")
    return " ".join(text.casefold().split())


def exits_for(step):
    kind = step.get("type")
    if kind == "branch":
        rules = (step.get("config") or {}).get("rules") or []
        return [rule["id"] for rule in rules if rule.get("id")] + ["else"]
    return list(STEP_TYPES.get(kind, {}).get("exits", []))


def exit_label(step, exit_key):
    if step.get("type") == "branch":
        for rule in (step.get("config") or {}).get("rules") or []:
            if rule.get("id") == exit_key:
                return rule.get("name") or "Rule"
    return EXIT_LABELS.get(exit_key, exit_key)


def presentation(options):
    """How a choice question appears in WhatsApp: buttons, a list, or numbered text."""
    labels = [str(o) for o in options]
    if len(labels) <= MAX_BUTTONS and all(len(label.strip()) <= BUTTON_LABEL for label in labels):
        return "buttons"
    if len(labels) <= MAX_LIST_ROWS and all(len(label.strip()) <= LIST_LABEL for label in labels):
        return "list"
    return "numbered"


_NUMBER_REPLY = re.compile(r"^\s*(\d{1,2})\s*[.)]?\s*$")


def match_option(options, *, reply_id="", text="", step_id=""):
    """Which option (by index) a reply picked, or None.

    A tap carries the id we sent; typed replies match the option's number
    ("2") or its words ("claims").
    """
    prefix = f"wf:{step_id}:"
    if reply_id and reply_id.startswith(prefix):
        tail = reply_id[len(prefix):]
        if tail.isdigit() and int(tail) < len(options):
            return int(tail)
    wanted = normalise(text)
    if not wanted:
        return None
    number = _NUMBER_REPLY.match(wanted)
    if number:
        index = int(number.group(1)) - 1
        return index if 0 <= index < len(options) else None
    for index, option in enumerate(options):
        if normalise(option) == wanted:
            return index
    return None


def problems(graph, *, workspace=None):
    """Plain-language reasons this graph cannot be published. Empty = fine."""
    found = []
    steps = graph.get("steps") or {}
    start = graph.get("start")
    if not steps:
        return ["The workflow has no steps yet."]
    if start not in steps:
        found.append("The workflow does not say which step comes first.")

    def name(step_id):
        return (steps.get(step_id) or {}).get("name") or step_id

    for step_id, step in steps.items():
        kind = step.get("type")
        config = step.get("config") or {}
        if kind not in STEP_TYPES:
            found.append(f"'{name(step_id)}' is a kind of step this platform does not know.")
            continue
        for exit_key, target in (step.get("next") or {}).items():
            if target and target not in steps:
                found.append(f"'{name(step_id)}' leads to a step that no longer exists.")
        if kind == "send_message" and not (config.get("text") or "").strip() and not config.get("file_id"):
            found.append(f"'{name(step_id)}' has nothing to send.")
        if kind == "ask_question":
            if not (config.get("text") or "").strip():
                found.append(f"'{name(step_id)}' has no question.")
            if config.get("answer") == "choice" and not config.get("options"):
                found.append(f"'{name(step_id)}' has no options to pick from.")
        if kind == "branch":
            for rule in config.get("rules") or []:
                if not rule.get("conditions"):
                    found.append(f"A rule in '{name(step_id)}' has no conditions.")
        if kind == "jump" and config.get("target") not in steps:
            found.append(f"'{name(step_id)}' jumps to a step that does not exist.")
        if kind == "webhook" and not config.get("connection_id"):
            found.append(f"'{name(step_id)}' does not say which webhook to call.")
        if kind == "update_contact" and not (config.get("field") or "").strip():
            found.append(f"'{name(step_id)}' does not say which detail to update.")
        if kind == "audience" and not config.get("audience_id"):
            found.append(f"'{name(step_id)}' does not say which audience.")
        if kind == "start_workflow" and not config.get("workflow_id"):
            found.append(f"'{name(step_id)}' does not say which workflow to start.")

    if workspace is not None:
        found.extend(_missing_references(steps, workspace, name))
    return found


def _missing_references(steps, workspace, name):
    from apps.contacts.models import Audience

    from .models import WebhookConnection, Workflow, WorkflowFile

    found = []
    wanted = {"file_id": set(), "connection_id": set(), "audience_id": set(), "workflow_id": set()}
    for step in steps.values():
        config = step.get("config") or {}
        for key in wanted:
            if config.get(key):
                wanted[key].add(config[key])
    checks = (
        ("file_id", WorkflowFile, "a file that has been deleted"),
        ("connection_id", WebhookConnection, "a webhook that has been deleted"),
        ("audience_id", Audience, "an audience that has been deleted"),
        ("workflow_id", Workflow, "a workflow that has been deleted"),
    )
    for key, model, words in checks:
        if not wanted[key]:
            continue
        present = set(
            model.objects.for_workspace(workspace).filter(pk__in=wanted[key]).values_list("pk", flat=True)
        )
        for step_id, step in steps.items():
            value = (step.get("config") or {}).get(key)
            if value and value not in present:
                found.append(f"'{name(step_id)}' uses {words}.")
    return found


def new_step_id(graph, kind):
    steps = graph.get("steps") or {}
    base = kind.split("_")[0]
    index = 1
    while f"{base}{index}" in steps:
        index += 1
    return f"{base}{index}"


def reachable(graph):
    """Step ids reachable from the start, in the order a reader meets them."""
    steps = graph.get("steps") or {}
    order, seen = [], set()
    pending = [graph.get("start")]
    while pending:
        step_id = pending.pop(0)
        if not step_id or step_id in seen or step_id not in steps:
            continue
        seen.add(step_id)
        order.append(step_id)
        step = steps[step_id]
        for exit_key in exits_for(step):
            target = (step.get("next") or {}).get(exit_key)
            if target:
                pending.append(target)
    return order
