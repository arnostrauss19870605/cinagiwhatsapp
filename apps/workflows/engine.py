"""Run a chat through a workflow.

The engine is driven from two places: a new chat arriving (start), and the
customer answering a question (handle_reply). Between those it runs steps
back to back until it has to wait for the customer or the workflow ends.

Who is in charge of a chat is recorded in `Conversation.automation_state`
under "workflow_run". Every path that hands a chat to a person (STOP, asking
for a human, an agent replying or claiming it) clears that, so a run whose id
is no longer there has been overruled and must not say another word.
"""

import logging

from django.db import transaction
from django.utils import timezone

from apps.inbox.models import Conversation, Message

from . import graph as g
from .models import RunEvent, Workflow, WorkflowRun

logger = logging.getLogger(__name__)

STATE_KEY = "workflow_run"
STEPS_PER_PASS = 60

DEFAULT_INVALID = "Sorry, I did not understand that. Please choose one of the options."
DEFAULT_LIST_BUTTON = "Choose an option"


# -- starting and stopping ---------------------------------------------------


def starting_workflow(workspace):
    return Workflow.starting_for(workspace)


def start(workflow, conversation, *, version=None, reason="a new chat started"):
    """Begin `workflow` for this chat. Returns the run, or None if it has nothing published."""
    version = version or workflow.published
    if version is None:
        return None
    stop(conversation, "another workflow took over")
    run = WorkflowRun.objects.create(
        workspace=conversation.workspace,
        workflow=workflow,
        version=version,
        conversation=conversation,
        contact=conversation.contact,
    )
    run.conversation = conversation
    _log(run, "", {}, f"Started: {reason}")
    conversation.automation_state = {**(conversation.automation_state or {}), STATE_KEY: run.pk}
    update = ["automation_state"]
    if conversation.status in (Conversation.Status.QUEUED, Conversation.Status.BOT) and not conversation.assigned_to_id:
        conversation.status = Conversation.Status.BOT
        update.append("status")
    conversation.save(update_fields=update)
    _advance(run, version.graph.get("start"))
    return run


def live_run(conversation):
    run_id = (conversation.automation_state or {}).get(STATE_KEY)
    if not run_id:
        return None
    return (
        WorkflowRun.objects.for_workspace(conversation.workspace)
        .filter(pk=run_id, conversation=conversation, status__in=WorkflowRun.LIVE_STATUSES)
        .select_related("version", "workflow")
        .first()
    )


def stop(conversation, reason, *, status=WorkflowRun.Status.STOPPED):
    """Take the automation off this chat. Safe to call when nothing is running."""
    runs = WorkflowRun.objects.for_workspace(conversation.workspace).filter(
        conversation=conversation, status__in=WorkflowRun.LIVE_STATUSES
    )
    for run in runs:
        run.end(status, reason)
        run.save(update_fields=["status", "end_reason", "ended_at", "waiting_until"])
        _log(run, run.current_step, run.step() or {}, f"Stopped: {reason}")
    state = conversation.automation_state or {}
    if STATE_KEY in state:
        conversation.automation_state = {k: v for k, v in state.items() if k != STATE_KEY}
        Conversation.objects.filter(pk=conversation.pk).update(automation_state=conversation.automation_state)


# -- answers -----------------------------------------------------------------


def handle_reply(conversation, message):
    """Treat this customer message as an answer to the question a run is waiting on.

    Returns True when a workflow took the message, False when it is someone
    else's to deal with.
    """
    with transaction.atomic():
        run_id = (conversation.automation_state or {}).get(STATE_KEY)
        if not run_id:
            return False
        run = (
            WorkflowRun.objects.select_for_update()
            .filter(pk=run_id, conversation=conversation, status=WorkflowRun.Status.WAITING)
            .select_related("version", "workflow")
            .first()
        )
        if run is None:
            return False
        run.conversation = conversation
        step_id, step = run.current_step, run.step() or {}
        if step.get("type") != "ask_question":
            return False
        config = step.get("config") or {}
        from apps.channels_wa.inbound import selection_id

        value, valid = read_answer(
            step_id, config, message.body, reply_id=selection_id(message.payload or {})
        )
        if valid:
            _save_answer(run, config, value)
            run.status = WorkflowRun.Status.RUNNING
            run.waiting_until = None
            run.attempts = 0
            run.save(update_fields=["status", "waiting_until", "attempts", "variables"])
            _log(run, step_id, step, f"Answered: {value}")
            _go(run, step, "answered")
            return True

        run.attempts += 1
        run.save(update_fields=["attempts"])
        _log(run, step_id, step, "Did not understand the answer", detail={"reply": message.body[:200]})
        target = (step.get("next") or {}).get("invalid")
        if target:
            run.status = WorkflowRun.Status.RUNNING
            run.waiting_until = None
            run.save(update_fields=["status", "waiting_until"])
            _advance(run, target)
        elif run.attempts <= int(config.get("retries", 2)):
            _send_text(run, step_id, config.get("invalid_text") or DEFAULT_INVALID)
            _ask(run, step_id, step, again=True)
        else:
            _hand_over(run, step_id, step, "the customer's answer could not be understood")
        return True


def read_answer(step_id, config, body, *, reply_id=""):
    """(value, valid) for a reply to an ask_question step. Shared with the test chat."""
    kind = config.get("answer", "text")
    text = (body or "").strip()
    if kind == "choice":
        options = config.get("options") or []
        index = g.match_option(options, reply_id=reply_id, text=text, step_id=step_id)
        if index is None:
            return None, False
        return str(options[index]).strip(), True
    if kind == "number":
        digits = text.replace(" ", "").replace(",", ".")
        try:
            number = float(digits)
        except ValueError:
            return None, False
        for bound, ok in (("min", lambda b: number >= b), ("max", lambda b: number <= b)):
            raw = config.get(bound)
            if raw not in (None, ""):
                try:
                    if not ok(float(raw)):
                        return None, False
                except (TypeError, ValueError):
                    pass
        return digits if not digits.endswith(".0") else digits[:-2], True
    return (text, True) if text else (None, False)


def _save_answer(run, config, value):
    save_to = config.get("save_to") or {}
    name = (save_to.get("name") or "").strip()
    run.variables = {**(run.variables or {}), "last_answer": value}
    if not name:
        return
    if save_to.get("kind") == "contact":
        _set_contact_detail(run.contact, name, value)
    else:
        run.variables[name] = value


# -- running steps -----------------------------------------------------------


def _advance(run, step_id):
    """Run steps from `step_id` until the workflow waits or ends."""
    steps = run.version.graph.get("steps") or {}
    for _ in range(STEPS_PER_PASS):
        if not step_id:
            _finish(run, "", {}, "Reached the end of the workflow")
            return
        if not _still_in_charge(run):
            return
        step = steps.get(step_id)
        if step is None:
            _fail(run, step_id, {}, "A step this workflow points to no longer exists")
            return
        run.current_step = step_id
        run.status = WorkflowRun.Status.RUNNING
        run.steps_taken += 1
        run.save(update_fields=["current_step", "status", "steps_taken"])
        if run.steps_taken > g.MAX_STEPS_PER_RUN:
            _hand_over(run, step_id, step, "the workflow went round too many steps")
            return
        outcome = _run_step(run, step_id, step)
        if outcome is None:
            return  # waiting, or the run ended inside the step
        kind, value = outcome
        if kind == "goto":
            step_id = value
        else:
            step_id = (step.get("next") or {}).get(value)
            if not step_id:
                _finish(run, run.current_step, step, "Reached the end of the workflow")
                return
    _hand_over(run, step_id, steps.get(step_id) or {}, "the workflow went round too many steps")


def _go(run, step, exit_key):
    _advance(run, (step.get("next") or {}).get(exit_key) or None)


def _still_in_charge(run):
    conversation = run.conversation
    if (conversation.automation_state or {}).get(STATE_KEY) != run.pk:
        if run.is_live:
            run.end(WorkflowRun.Status.STOPPED, "someone else took over the chat")
            run.save(update_fields=["status", "end_reason", "ended_at", "waiting_until"])
        return False
    return run.is_live


def _run_step(run, step_id, step):
    """Do one step. Returns ("exit", key) or ("goto", step id) to carry on, None to stop here."""
    handler = HANDLERS.get(step.get("type"))
    if handler is None:
        _fail(run, step_id, step, "This kind of step is not supported")
        return None
    try:
        return handler(run, step_id, step, step.get("config") or {})
    except Exception:
        logger.exception("workflow step failed run=%s step=%s", run.pk, step_id)
        _hand_over(run, step_id, step, "a step went wrong")
        return None


def _window_closed(run, step_id, step):
    if run.conversation.window_open:
        return False
    _finish(run, step_id, step, "Stopped: the chat has been quiet for more than 24 hours, so WhatsApp "
                                "only allows an approved template")
    return True


def _send_message(run, step_id, step, config):
    if _window_closed(run, step_id, step):
        return None
    sent = []
    text = (config.get("text") or "").strip()
    if config.get("file_id"):
        from apps.channels_wa.outbound import send_stored_file

        from .models import WorkflowFile

        stored = WorkflowFile.objects.for_workspace(run.workspace).filter(pk=config["file_id"]).first()
        if stored is None:
            _log(run, step_id, step, "The file to send has been deleted")
        else:
            message = send_stored_file(run.conversation, stored, caption=text, payload=_tag(run, step_id))
            sent.append(message)
    elif text:
        sent.append(_send_text(run, step_id, text))
    for message in sent:
        _log(run, step_id, step, _sent_words(message))
    return ("exit", "next")


def _sent_words(message):
    if message.wa_status == Message.Status.BLOCKED:
        return "Not sent (test mode)"
    if message.wa_status == Message.Status.FAILED:
        return f"WhatsApp did not accept the message: {message.wa_error.get('reason', '')}"[:255]
    return "Sent"


def _send_text(run, step_id, text):
    from apps.channels_wa.outbound import send_text

    return send_text(run.conversation, text, actor=Message.Actor.BOT, payload=_tag(run, step_id))


def _tag(run, step_id):
    return {"auto": "workflow", "workflow_run": run.pk, "step": step_id}


def _ask_question(run, step_id, step, config):
    if _window_closed(run, step_id, step):
        return None
    run.attempts = 0
    _ask(run, step_id, step)
    return None


def _ask(run, step_id, step, *, again=False):
    import datetime as dt

    from apps.channels_wa import outbound

    config = step.get("config") or {}
    text = (config.get("text") or "").strip()
    conversation = run.conversation
    if config.get("answer") == "choice":
        options = [str(o).strip() for o in config.get("options") or []]
        style = g.presentation(options)
        rows = [{"id": f"wf:{step_id}:{i}", "title": label} for i, label in enumerate(options)]
        if style == "buttons":
            message = outbound.send_buttons(conversation, text, rows, payload=_tag(run, step_id))
        elif style == "list":
            message = outbound.send_list(
                conversation, text, config.get("list_button") or DEFAULT_LIST_BUTTON, rows,
                payload=_tag(run, step_id),
            )
        else:
            numbered = "\n".join(f"{i + 1}. {label}" for i, label in enumerate(options))
            message = _send_text(run, step_id, f"{text}\n\n{numbered}")
    else:
        message = _send_text(run, step_id, text)

    minutes = int(config.get("timeout_minutes") or g.DEFAULT_TIMEOUT_MINUTES)
    run.status = WorkflowRun.Status.WAITING
    run.waiting_until = timezone.now() + dt.timedelta(minutes=minutes)
    run.save(update_fields=["status", "waiting_until", "attempts"])
    _log(run, step_id, step, ("Asked again: " if again else "Asked: ") + _sent_words(message).lower())


def _branch(run, step_id, step, config):
    rule = choose_rule(config, run.variables or {}, lambda field: _contact_detail(run.contact, field))
    if rule is not None:
        _log(run, step_id, step, f"Took '{rule.get('name') or 'rule'}'")
        return ("exit", rule["id"])
    _log(run, step_id, step, "Took 'anything else'")
    return ("exit", "else")


def choose_rule(config, variables, contact_detail):
    """The first branch rule that matches, or None. Shared with the test chat."""
    for rule in config.get("rules") or []:
        results = [_condition(c, variables, contact_detail) for c in rule.get("conditions") or []]
        if results and (any(results) if rule.get("match") == "any" else all(results)):
            return rule
    return None


def _condition(condition, variables, contact_detail):
    field = (condition.get("field") or "").strip()
    if condition.get("source") == "contact":
        actual = contact_detail(field)
    else:
        actual = variables.get(field, "")
    actual, wanted = g.normalise(actual), g.normalise(condition.get("value"))
    operator = condition.get("operator", "equals")
    if operator == "equals":
        return actual == wanted
    if operator == "not_equals":
        return actual != wanted
    if operator == "contains":
        return bool(wanted) and wanted in actual
    if operator == "is_set":
        return bool(actual)
    if operator == "is_empty":
        return not actual
    return False


def _office_hours(run, step_id, step, config):
    is_open = run.workspace.is_open()
    _log(run, step_id, step, "Within office hours" if is_open else "Outside office hours")
    return ("exit", "open" if is_open else "closed")


def _update_contact(run, step_id, step, config):
    field = (config.get("field") or "").strip()
    value = _render(config.get("value") or "", run)
    _set_contact_detail(run.contact, field, value)
    _log(run, step_id, step, f"Set {field} to {value}"[:255])
    return ("exit", "next")


def _audience(run, step_id, step, config):
    from apps.contacts.models import Audience

    audience = Audience.objects.for_workspace(run.workspace).filter(pk=config.get("audience_id")).first()
    if audience is None:
        _log(run, step_id, step, "The audience has been deleted")
    elif config.get("action") == "remove":
        audience.contacts.remove(run.contact)
        _log(run, step_id, step, f"Removed from {audience.name}")
    else:
        audience.contacts.add(run.contact)
        _log(run, step_id, step, f"Added to {audience.name}")
    return ("exit", "next")


def _assign(run, step_id, step, config):
    _hand_over(run, step_id, step, config.get("note") or "", config=config, requested=True)
    return None


def _close(run, step_id, step, config):
    conversation = run.conversation
    conversation.status = Conversation.Status.RESOLVED
    conversation.resolved_at = timezone.now()
    conversation.auto_resolved = True
    conversation.automation_state = {
        k: v for k, v in (conversation.automation_state or {}).items() if k != STATE_KEY
    }
    conversation.save(update_fields=["status", "resolved_at", "auto_resolved", "automation_state"])
    _end(run, step_id, step, WorkflowRun.Status.FINISHED, "Closed the chat")
    from apps.inbox import events

    events.conversation_changed(conversation, "update")
    return None


def _jump(run, step_id, step, config):
    target = config.get("target")
    limit = int(config.get("max_times") or 3)
    used = int((run.jumps or {}).get(step_id, 0))
    if used < limit:
        run.jumps = {**(run.jumps or {}), step_id: used + 1}
        run.save(update_fields=["jumps"])
        return ("goto", target)
    _log(run, step_id, step, "Jumped too many times")
    if (step.get("next") or {}).get("limit"):
        return ("exit", "limit")
    _hand_over(run, step_id, step, "the customer went round the same part of the workflow too often")
    return None


def _webhook(run, step_id, step, config):
    from . import webhooks
    from .models import WebhookConnection

    connection = (
        WebhookConnection.objects.for_workspace(run.workspace).filter(pk=config.get("connection_id")).first()
    )
    if connection is None:
        ok, detail = False, {"reason": "The webhook has been deleted."}
    else:
        try:
            body = webhooks.body_for(connection, run, step_id, step)
        except ValueError as exc:
            ok, detail = False, {"reason": f"The webhook '{connection.name}' has a broken body. {exc}"}
        else:
            ok, detail = webhooks.call(connection, body, wa_id=run.contact.wa_id)
    _log(run, step_id, step, "Webhook worked" if ok else f"Webhook failed: {detail.get('reason', '')}"[:255],
         detail=detail)
    save_as = (config.get("save_as") or "").strip()
    if ok and save_as and isinstance(detail.get("response"), dict):
        run.variables = {**(run.variables or {}), **{
            f"{save_as}.{k}": v for k, v in detail["response"].items() if isinstance(v, (str, int, float, bool))
        }}
        run.save(update_fields=["variables"])
    if ok:
        return ("exit", "success")
    if (step.get("next") or {}).get("failure"):
        return ("exit", "failure")
    _hand_over(run, step_id, step, "a webhook call failed")
    return None


def _start_workflow(run, step_id, step, config):
    target = (
        Workflow.objects.for_workspace(run.workspace)
        .filter(pk=config.get("workflow_id"), published__isnull=False)
        .select_related("published")
        .first()
    )
    if target is None:
        _hand_over(run, step_id, step, "the workflow it should start is not published")
        return None
    _end(run, step_id, step, WorkflowRun.Status.FINISHED, f"Started '{target.name}'")
    start(target, run.conversation, reason=f"started by '{run.workflow.name}'")
    return None


def _end_step(run, step_id, step, config):
    _finish(run, step_id, step, "Reached an end step")
    return None


HANDLERS = {
    "send_message": _send_message,
    "ask_question": _ask_question,
    "branch": _branch,
    "office_hours": _office_hours,
    "update_contact": _update_contact,
    "audience": _audience,
    "assign": _assign,
    "close": _close,
    "jump": _jump,
    "webhook": _webhook,
    "start_workflow": _start_workflow,
    "end": _end_step,
}


# -- handing over and ending -------------------------------------------------


def _hand_over(run, step_id, step, reason, *, config=None, requested=False):
    """Give the chat to the agents: from an assign step, or because the workflow could not go on."""
    from apps.agents.allocation import auto_assign

    conversation = run.conversation
    config = config or {}
    conversation.automation_state = {
        k: v for k, v in (conversation.automation_state or {}).items() if k != STATE_KEY
    }
    if not conversation.assigned_to_id:
        conversation.status = Conversation.Status.QUEUED
    conversation.handoff_reason = (
        f"workflow '{run.workflow.name}'" + (f": {reason}" if reason else "")
    )[:200]
    conversation.save(update_fields=["automation_state", "status", "handoff_reason"])

    note = (
        f"Workflow '{run.workflow.name}' handed this chat to the team."
        if requested
        else f"Workflow '{run.workflow.name}' stopped because {reason}. The chat is waiting for an agent."
    )
    if requested and reason:
        note += f" {reason}"
    Message.objects.create(
        workspace=conversation.workspace,
        conversation=conversation,
        direction=Message.Direction.SYSTEM,
        actor=Message.Actor.SYSTEM,
        kind=Message.Kind.TEXT,
        body=note,
        payload={"note": True, "workflow_run": run.pk},
        wa_status=Message.Status.SENT,
    )
    _end(run, step_id, step, WorkflowRun.Status.HANDED_OVER,
         "Handed to an agent" if requested else f"Handed to an agent: {reason}")

    if not conversation.assigned_to_id:
        if config.get("to") == "agent" and config.get("user_id"):
            _assign_to_user(conversation, config["user_id"], run)
        else:
            auto_assign(conversation)
    from apps.inbox import events

    events.conversation_changed(conversation, "assignment")


def _assign_to_user(conversation, user_id, run):
    from apps.agents.allocation import auto_assign
    from apps.inbox.models import AssignmentLog
    from apps.workspaces.models import WorkspaceMembership

    member = WorkspaceMembership.objects.filter(workspace=conversation.workspace, user_id=user_id).first()
    if member is None:
        auto_assign(conversation)
        return
    conversation.assigned_to_id = user_id
    conversation.assigned_at = timezone.now()
    conversation.status = Conversation.Status.ASSIGNED
    conversation.save(update_fields=["assigned_to", "assigned_at", "status"])
    AssignmentLog.objects.create(
        workspace=conversation.workspace,
        conversation=conversation,
        to_user_id=user_id,
        reason=AssignmentLog.Reason.AUTO,
        detail=f"workflow '{run.workflow.name}'",
    )


def _finish(run, step_id, step, outcome):
    _end(run, step_id, step, WorkflowRun.Status.FINISHED, outcome)
    state = run.conversation.automation_state or {}
    if state.get(STATE_KEY) == run.pk:
        run.conversation.automation_state = {k: v for k, v in state.items() if k != STATE_KEY}
        Conversation.objects.filter(pk=run.conversation.pk).update(
            automation_state=run.conversation.automation_state
        )


def _fail(run, step_id, step, outcome):
    _hand_over(run, step_id, step, outcome[0].lower() + outcome[1:])
    run.status = WorkflowRun.Status.FAILED
    run.save(update_fields=["status"])


def _end(run, step_id, step, status, outcome):
    run.end(status, outcome)
    run.save(update_fields=["status", "end_reason", "ended_at", "waiting_until"])
    _log(run, step_id, step, outcome)


def _log(run, step_id, step, outcome, *, detail=None):
    RunEvent.objects.create(
        workspace=run.workspace,
        run=run,
        step_id=step_id or "",
        step_name=(step or {}).get("name", "")[:120],
        step_type=(step or {}).get("type", ""),
        outcome=outcome[:255],
        detail=detail or {},
    )


# -- contact details ---------------------------------------------------------


def _contact_detail(contact, field):
    if field in ("name", "display_name"):
        return contact.name
    if field in ("number", "mobile", "phone"):
        return contact.wa_id
    return (contact.attributes or {}).get(field, "")


def _set_contact_detail(contact, field, value):
    if not field:
        return
    if field in ("name", "display_name"):
        contact.display_name = str(value)[:120]
        contact.save(update_fields=["display_name"])
        return
    contact.attributes = {**(contact.attributes or {}), field: value}
    contact.save(update_fields=["attributes"])


def _render(value, run):
    """Fill the few placeholders a contact-detail value may hold."""
    now = timezone.localtime().strftime("%Y-%m-%d %H:%M")
    text = str(value)
    for token in ("{{now}}", "{{$system.current_datetime}}"):
        text = text.replace(token, now)
    for name, answer in (run.variables or {}).items():
        text = text.replace("{{" + name + "}}", str(answer))
    return text


# -- timeouts ----------------------------------------------------------------


def expire_waiting(now=None):
    """Runs whose question has gone unanswered too long: take the timeout exit, or end."""
    now = now or timezone.now()
    expired = 0
    due = WorkflowRun.objects.filter(status=WorkflowRun.Status.WAITING, waiting_until__lte=now).values_list(
        "pk", flat=True
    )[:200]
    for run_id in list(due):
        with transaction.atomic():
            run = (
                WorkflowRun.objects.select_for_update()
                .filter(pk=run_id, status=WorkflowRun.Status.WAITING, waiting_until__lte=now)
                .select_related("version", "workflow", "conversation", "workspace")
                .first()
            )
            if run is None:
                continue
            step = run.step() or {}
            target = (step.get("next") or {}).get("timeout")
            run.status = WorkflowRun.Status.RUNNING
            run.waiting_until = None
            run.save(update_fields=["status", "waiting_until"])
            _log(run, run.current_step, step, "No answer in time")
            if target:
                _advance(run, target)
            else:
                _finish(run, run.current_step, step, "Ended: no answer in time")
            expired += 1
    return expired
