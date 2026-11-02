"""The builder's test chat: walk a workflow on screen without touching anything real.

Nothing is sent, no webhook is called, no contact or audience changes. What
would have happened appears as a note in the transcript instead. Answers are
read and branches chosen by the engine's own functions, so the test chat
cannot disagree with what a customer would see.
"""

from django.utils import timezone

from . import graph as g
from .engine import DEFAULT_INVALID, STEPS_PER_PASS, choose_rule, read_answer


def new_state(workflow_id, *, after_hours=False):
    return {
        "workflow": workflow_id,
        "step": None,
        "vars": {},
        "contact": {"name": "Test customer"},
        "jumps": {},
        "attempts": 0,
        "waiting": False,
        "ended": "",
        "after_hours": after_hours,
    }


def start(state, graphs, workspace):
    out = []
    _advance(state, graphs, workspace, _graph(state, graphs).get("start"), out)
    return out


def reply(state, graphs, workspace, *, text="", reply_id=""):
    out = [{"from": "you", "text": text}]
    if not state.get("waiting"):
        out.append(_note("The workflow is not waiting for an answer. Start again to test from the top."))
        return out
    graph = _graph(state, graphs)
    step_id = state["step"]
    step = graph["steps"][step_id]
    config = step.get("config") or {}
    value, valid = read_answer(step_id, config, text, reply_id=reply_id)
    state["waiting"] = False
    if valid:
        state["attempts"] = 0
        state["vars"]["last_answer"] = value
        save = config.get("save_to") or {}
        if save.get("name"):
            if save.get("kind") == "contact":
                state["contact"][save["name"]] = value
                out.append(_note(f"Saved “{value}” on the contact as {save['name']}."))
            else:
                state["vars"][save["name"]] = value
        _advance(state, graphs, workspace, (step.get("next") or {}).get("answered"), out)
        return out
    state["attempts"] += 1
    target = (step.get("next") or {}).get("invalid")
    if target:
        out.append(_note("Not a valid answer, so 'Not a valid answer' is followed."))
        _advance(state, graphs, workspace, target, out)
    elif state["attempts"] <= int(config.get("retries", 2)):
        out.append(_bot(config.get("invalid_text") or DEFAULT_INVALID))
        _ask(state, step_id, step, out)
    else:
        _end(state, out, "The answer was not understood, so the chat would go to an agent.")
    return out


def timeout(state, graphs, workspace):
    if not state.get("waiting"):
        return [_note("The workflow is not waiting for an answer.")]
    step = _graph(state, graphs)["steps"][state["step"]]
    state["waiting"] = False
    out = [_note("Nobody answered in time.")]
    target = (step.get("next") or {}).get("timeout")
    if target:
        _advance(state, graphs, workspace, target, out)
    else:
        _end(state, out, "The workflow ends quietly: nobody answered.")
    return out


def _graph(state, graphs):
    return graphs(state["workflow"])


def _bot(text, **extra):
    return {"from": "bot", "text": text, **extra}


def _note(text):
    return {"from": "note", "text": text}


def _end(state, out, text):
    state["ended"] = text
    state["waiting"] = False
    out.append(_note(text))


def _advance(state, graphs, workspace, step_id, out):
    for _ in range(STEPS_PER_PASS):
        if not step_id:
            _end(state, out, "The workflow reached its end.")
            return
        graph = _graph(state, graphs)
        step = (graph.get("steps") or {}).get(step_id)
        if step is None:
            _end(state, out, "This leads to a step that no longer exists.")
            return
        state["step"] = step_id
        config = step.get("config") or {}
        kind = step.get("type")
        exit_key = None

        if kind == "send_message":
            if config.get("file_id"):
                out.append(_bot(config.get("text") or "", file=_file_name(workspace, config["file_id"])))
            elif (config.get("text") or "").strip():
                out.append(_bot(config["text"]))
            exit_key = "next"
        elif kind == "ask_question":
            state["attempts"] = 0
            _ask(state, step_id, step, out)
            return
        elif kind == "branch":
            rule = choose_rule(config, state["vars"], lambda field: _detail(state, field))
            exit_key = rule["id"] if rule else "else"
            out.append(_note(f"Branch ‘{step.get('name')}’ took "
                             f"‘{rule.get('name') if rule else 'Anything else'}’."))
        elif kind == "office_hours":
            is_open = not state.get("after_hours") and workspace.is_open()
            exit_key = "open" if is_open else "closed"
            out.append(_note("Within office hours." if is_open else "Outside office hours."))
        elif kind == "update_contact":
            value = str(config.get("value") or "").replace("{{now}}", timezone.localtime().strftime("%Y-%m-%d %H:%M"))
            for name, answer in state["vars"].items():
                value = value.replace("{{" + name + "}}", str(answer))
            state["contact"][config.get("field") or "?"] = value
            out.append(_note(f"Would set {config.get('field')} to “{value}”."))
            exit_key = "next"
        elif kind == "audience":
            verb = "remove the customer from" if config.get("action") == "remove" else "add the customer to"
            out.append(_note(f"Would {verb} an audience."))
            exit_key = "next"
        elif kind == "webhook":
            out.append(_note("Would call the webhook. The test chat does not call it and carries on as if it worked."))
            exit_key = "success"
        elif kind == "jump":
            used = int(state["jumps"].get(step_id, 0))
            if used < int(config.get("max_times") or 3):
                state["jumps"][step_id] = used + 1
                step_id = config.get("target")
                continue
            if (step.get("next") or {}).get("limit"):
                exit_key = "limit"
            else:
                _end(state, out, "Jumped back too many times, so the chat would go to an agent.")
                return
        elif kind == "assign":
            _end(state, out, "The chat would go to an agent here, and the workflow ends.")
            return
        elif kind == "close":
            _end(state, out, "The chat would be marked resolved here.")
            return
        elif kind == "start_workflow":
            target = config.get("workflow_id")
            if not target or graphs(target) is None:
                _end(state, out, "The workflow it should start is not published, so the chat would go to an agent.")
                return
            out.append(_note("Hands over to another workflow."))
            state["workflow"] = target
            step_id = graphs(target).get("start")
            continue
        else:
            _end(state, out, "The workflow ends here.")
            return

        step_id = (step.get("next") or {}).get(exit_key)
    _end(state, out, "The workflow went round too many steps without waiting, so the chat would go to an agent.")


def _ask(state, step_id, step, out):
    config = step.get("config") or {}
    text = config.get("text") or ""
    if config.get("answer") == "choice":
        options = [str(o).strip() for o in config.get("options") or []]
        style = g.presentation(options)
        if style == "numbered":
            text = text + "\n\n" + "\n".join(f"{i + 1}. {o}" for i, o in enumerate(options))
            out.append(_bot(text))
        else:
            out.append(_bot(text, style=style, step=step_id,
                            options=[{"id": f"wf:{step_id}:{i}", "title": o} for i, o in enumerate(options)]))
    else:
        out.append(_bot(text))
    state["waiting"] = True


def _detail(state, field):
    if field in ("name", "display_name"):
        return state["contact"].get("name", "")
    return state["contact"].get(field, "")


def _file_name(workspace, file_id):
    from .models import WorkflowFile

    stored = WorkflowFile.objects.for_workspace(workspace).filter(pk=file_id).first()
    return stored.filename if stored else "a deleted file"
