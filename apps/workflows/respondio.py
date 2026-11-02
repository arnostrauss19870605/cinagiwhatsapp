"""Turn a respond.io workflow export into one of ours.

respond.io stores a workflow as a flat list of steps, each pointing at its
parent, with "connector" pseudo-steps for every exit (a question's success and
failure, a branch's rules). We rebuild that as steps with named exits.

What does not carry over is reported rather than guessed at:

- A branch on which WhatsApp number the chat came in on is collapsed: here
  each number is its own workspace, so only the main number's path is kept.
- Starting another respond.io workflow becomes an end, unless that workflow
  has been imported too.
- "Assign to" pointed at one named respond.io user; here the chat goes to the
  next available agent.
- Files are copied onto our own storage, because respond.io's links are not
  ours to rely on.
"""

import logging
import re
from collections import Counter, defaultdict

from django.core.files.base import ContentFile
from django.db import transaction

from . import graph as g
from .models import Workflow, WorkflowFile, WorkflowVersion

logger = logging.getLogger(__name__)

CONNECTOR_TYPES = {"askQuestionConnector", "branchConnector", "dateTimeConnector", "assignToConnector"}
OPERATORS = {
    "isEqualTo": "equals",
    "isNotEqualTo": "not_equals",
    "contains": "contains",
    "exists": "is_set",
    "isNotEmpty": "is_set",
    "doesNotExist": "is_empty",
    "isEmpty": "is_empty",
}
REPLY_WITH_NUMBER = re.compile(r"\breply\b.{0,40}\b(\d|number)", re.IGNORECASE)


class ImportError_(ValueError):
    """The file is not a respond.io workflow export. The message is shown as is."""


class Converter:
    def __init__(self, data, *, linked=None):
        if not isinstance(data, dict) or not isinstance(data.get("workflow"), list):
            raise ImportError_("That file is not a respond.io workflow export.")
        self.data = data
        self.linked = linked or {}  # respond.io workflow id -> our Workflow pk
        self.nodes = {str(n.get("id")): n for n in data["workflow"]}
        self.children = defaultdict(list)
        for node in data["workflow"]:
            self.children[str(node.get("parentId"))].append(node)
        self.steps = {}
        self.files = []  # (step id, url, filename, mime)
        self.notes = []
        self.main_channel = self._main_channel()

    # -- reading respond.io's tree --------------------------------------

    def _main_channel(self):
        counts = Counter()
        for node in self.data["workflow"]:
            if node.get("type") != "sendMessage":
                continue
            data = node.get("data") or {}
            for channel in [data.get("channelId")] + [p.get("channelId") for p in data.get("payload") or []]:
                if channel:
                    counts[channel] += 1
        return counts.most_common(1)[0][0] if counts else None

    def _step_children(self, rid):
        return [n for n in self.children.get(str(rid), []) if n.get("type") not in CONNECTOR_TYPES]

    def _connectors(self, rid):
        node = self.nodes.get(str(rid)) or {}
        order = [str(c) for c in (node.get("data") or {}).get("connectors") or []]
        found = [n for n in self.children.get(str(rid), []) if n.get("type") in CONNECTOR_TYPES]
        found.sort(key=lambda n: order.index(str(n["id"])) if str(n["id"]) in order else len(order))
        return found

    def _after(self, rid):
        """The step that follows a step or a connector, converted. None = the end."""
        following = self._step_children(rid)
        if not following:
            return None
        if len(following) > 1:
            self.notes.append(f"'{self._name(rid)}' had more than one next step; only the first was kept.")
        return self.convert(str(following[0]["id"]))

    def _name(self, rid):
        node = self.nodes.get(str(rid)) or {}
        return node.get("name") or node.get("type") or str(rid)

    # -- conversion ------------------------------------------------------

    def run(self):
        triggers = [n for n in self.data["workflow"] if n.get("type") == "trigger"]
        if not triggers:
            raise ImportError_("That respond.io export has no trigger step.")
        trigger = triggers[0]
        start = self._after(trigger["id"])
        kind = ((trigger.get("data") or {}).get("type") or "")
        if kind != "conversationOpened":
            self.notes.append(
                f"respond.io started this workflow on '{kind}'. Here it only runs when another workflow starts it."
            )
        self.notes = list(dict.fromkeys(self.notes))
        return {
            "start": start,
            "steps": self.steps,
            "source": {"respondio_id": self.data.get("id"), "name": self.data.get("name", "")},
        }, ("chat_opened" if kind == "conversationOpened" else "manual")

    def convert(self, rid):
        """Our step id for respond.io step `rid`, converting it on first sight."""
        node = self.nodes.get(str(rid))
        if node is None:
            return None
        kind = node.get("type")
        if kind == "branch" and self._is_channel_branch(rid):
            return self._collapse_channel_branch(rid)
        sid = f"r{rid}"
        if sid in self.steps:
            return sid
        self.steps[sid] = {"type": "end", "name": node.get("name") or "", "config": {}, "next": {}}
        handler = getattr(self, f"_from_{kind}", None)
        if handler is None:
            self.notes.append(f"'{self._name(rid)}' is a respond.io '{kind}' step, which is not supported; "
                              "the workflow ends there.")
            return sid
        handler(rid, node, node.get("data") or {}, self.steps[sid])
        return sid

    def _from_sendMessage(self, rid, node, data, step):
        messages = [p.get("message") or {} for p in data.get("payload") or []]
        if not messages:
            step.update(type="send_message", config={"text": ""})
            step["next"] = {"next": self._after(rid)}
            return
        # One of our steps per message, chained in order.
        current, sid = step, f"r{rid}"
        for index, message in enumerate(messages):
            if index:
                sid = f"r{rid}-{index}"
                current = self.steps[sid] = {"type": "send_message", "name": f"{node.get('name')} ({index + 1})",
                                             "config": {}, "next": {}}
                previous["next"] = {"next": sid}
            current["type"] = "send_message"
            if message.get("type") == "attachment":
                attachment = message.get("attachment") or {}
                current["config"] = {"text": (attachment.get("description") or "").strip()}
                self.files.append((sid, attachment.get("url"), attachment.get("fileName") or "file",
                                   attachment.get("mimeType") or "application/octet-stream"))
            else:
                current["config"] = {"text": message.get("text") or ""}
            previous = current
        previous["next"] = {"next": self._after(rid)}

    def _from_askQuestion(self, rid, node, data, step):
        question = data.get("question") or {}
        kind = {"multiple": "choice", "number": "number"}.get(question.get("type"), "text")
        config = {"text": question.get("text") or "", "answer": kind}
        if kind == "choice":
            raw = [str(o) for o in question.get("options") or []]
            config["options"] = [" ".join(o.split()) for o in raw]
            if raw != config["options"]:
                self.notes.append(f"'{node.get('name')}': extra spaces were trimmed from its options.")
            if REPLY_WITH_NUMBER.search(config["text"]):
                self.notes.append(
                    f"'{node.get('name')}' asks people to reply with a number. Its options now appear as "
                    f"{'tap buttons' if g.presentation(config['options']) == 'buttons' else 'a pick-list'}, "
                    "so consider rewording it (typed numbers still work)."
                )
        if kind == "number":
            for bound in ("min", "max"):
                if question.get(bound) not in (None, ""):
                    config[bound] = question[bound]
        save = data.get("saveResponse") or {}
        if save.get("field"):
            config["save_to"] = {"kind": "contact", "name": save["field"]}
        elif save.get("variable"):
            config["save_to"] = {"kind": "variable", "name": save["variable"]}
        timeout = data.get("timeout") or {}
        unit = {"minutes": 1, "hours": 60, "days": 1440}.get(timeout.get("unit"), 0)
        if unit and timeout.get("value"):
            config["timeout_minutes"] = int(timeout["value"]) * unit
        step.update(type="ask_question", config=config)
        nxt = {}
        for connector in self._connectors(rid):
            key = "answered" if (connector.get("data") or {}).get("connectorType") == "success" else "invalid"
            nxt[key] = self._after(connector["id"])
        step["next"] = nxt

    def _from_branch(self, rid, node, data, step):
        rules, nxt, else_target = [], {}, None
        for index, connector in enumerate(self._connectors(rid), start=1):
            conditions = (connector.get("data") or {}).get("conditions")
            if not conditions:
                else_target = self._after(connector["id"])
                continue
            rule_id = f"rule{index}"
            rules.append({
                "id": rule_id,
                "name": connector.get("name") or f"Rule {index}",
                "match": "any" if "$or" in conditions else "all",
                "conditions": [self._condition(node, c) for c in conditions.get("$or") or conditions.get("$and") or []],
            })
            nxt[rule_id] = self._after(connector["id"])
        nxt["else"] = else_target
        step.update(type="branch", config={"rules": rules}, next=nxt)

    def _condition(self, node, condition):
        category = condition.get("category")
        source = {"variable": "variable", "contactField": "contact"}.get(category)
        if source is None:
            self.notes.append(f"'{node.get('name')}' checks '{category}', which is not supported here; "
                              "that check never matches.")
        operator = OPERATORS.get(condition.get("operator"))
        if operator is None:
            self.notes.append(f"'{node.get('name')}' uses the comparison '{condition.get('operator')}'; "
                              "it was read as 'is'.")
            operator = "equals"
        value = condition.get("value")
        if isinstance(value, str) and (" " in value or value != value.strip()):
            self.notes.append(
                f"'{node.get('name')}' compared against '{' '.join(value.split())}' with hidden or extra "
                "spaces, so in respond.io it never matched. It now matches what people actually pick."
            )
        return {"source": source or "variable", "field": condition.get("field") or "",
                "operator": operator, "value": value if isinstance(value, str) else str(value or "")}

    def _is_channel_branch(self, rid):
        connectors = [c for c in self._connectors(rid) if (c.get("data") or {}).get("conditions")]
        return bool(connectors) and all(
            any(cond.get("category") == "lastInteractedChannel"
                for cond in sum(((c["data"]["conditions"].get(k) or []) for k in ("$and", "$or")), []))
            for c in connectors
        )

    def _collapse_channel_branch(self, rid):
        for connector in self._connectors(rid):
            conditions = (connector.get("data") or {}).get("conditions") or {}
            values = [cond.get("value") for cond in (conditions.get("$and") or []) + (conditions.get("$or") or [])]
            if self.main_channel in values:
                dropped = [c.get("name") for c in self._connectors(rid) if c is not connector]
                self.notes.append(
                    f"'{self._name(rid)}' chose a path by WhatsApp number. Only the '{connector.get('name')}' path "
                    f"was kept; each number is its own workspace here (left out: {', '.join(dropped)})."
                )
                return self._after(connector["id"])
        self.notes.append(f"'{self._name(rid)}' chose a path by WhatsApp number and none matched; "
                          "the workflow ends there.")
        return None

    def _from_dateTime(self, rid, node, data, step):
        if data.get("action") != "businessHours":
            self.notes.append(f"'{node.get('name')}' was a '{data.get('action')}' check; it now checks office "
                              "hours instead.")
        days = [f"{t['day'].title()} {t['startTime']}–{t['endTime']}"
                for t in data.get("times") or [] if not t.get("disabled")]
        if days:
            self.notes.append(
                f"'{node.get('name')}' used its own hours in respond.io ({', '.join(days)}). Here it uses this "
                "workspace's working hours and holidays, so check those match."
            )
        nxt = {}
        for connector in self._connectors(rid):
            key = "open" if (connector.get("data") or {}).get("connectorType") == "success" else "closed"
            nxt[key] = self._after(connector["id"])
        step.update(type="office_hours", config={}, next=nxt)

    def _from_assignTo(self, rid, node, data, step):
        if data.get("userId") or data.get("teamId"):
            self.notes.append(f"'{node.get('name')}' assigned to one specific respond.io user. Here the chat goes "
                              "to the next available agent.")
        step.update(type="assign", config={"to": "auto"}, next={})

    def _from_closeConversation(self, rid, node, data, step):
        step.update(type="close", config={"note": data.get("summary") or ""}, next={})

    def _from_updateContactField(self, rid, node, data, step):
        value = str(data.get("contactFieldValue") or "").replace("{{$system.current_datetime}}", "{{now}}")
        step.update(type="update_contact", config={"field": data.get("contactField") or "", "value": value},
                    next={"next": self._after(rid)})

    def _from_jumpTo(self, rid, node, data, step):
        step.update(type="jump", config={"target": None, "max_times": int(data.get("maxJumps") or 3)},
                    next={})
        step["config"]["target"] = self.convert(str(data.get("stepId")))

    def _from_triggerWorkflow(self, rid, node, data, step):
        target = self.linked.get(data.get("workflowId")) or self.linked.get(str(data.get("workflowId")))
        if target:
            step.update(type="start_workflow", config={"workflow_id": target}, next={})
            return
        self.notes.append(
            f"'{node.get('name')}' started another respond.io workflow (id {data.get('workflowId')}) that has not "
            "been imported, so the workflow ends there."
        )
        step.update(type="end", config={}, next={})


def _linked_workflows(workspace):
    """respond.io workflow ids already imported into this workspace -> our ids."""
    linked = {}
    for workflow in Workflow.objects.for_workspace(workspace):
        latest = workflow.latest
        source = ((latest.graph if latest else {}) or {}).get("source") or {}
        if source.get("respondio_id"):
            linked[source["respondio_id"]] = workflow.pk
    return linked


def _download(url):
    import requests

    response = requests.get(url, timeout=30)
    response.raise_for_status()
    return response.content


def import_export(workspace, data, *, user=None, name=None, fetch=None):
    """Create a draft workflow from a respond.io export. Returns (workflow, notes)."""
    fetch = fetch or _download
    converter = Converter(data, linked=_linked_workflows(workspace))
    graph, trigger = converter.run()

    with transaction.atomic():
        copied = {}  # url -> WorkflowFile pk, so a file used twice is stored once
        for step_id, url, filename, mime in converter.files:
            if url not in copied:
                try:
                    content = fetch(url)
                except Exception:
                    logger.warning("respond.io file download failed url=%s", url, exc_info=True)
                    converter.notes.append(
                        f"The file '{filename}' could not be copied from respond.io. Add it to "
                        f"'{graph['steps'][step_id]['name']}' by hand."
                    )
                    copied[url] = None
                    continue
                stored = WorkflowFile(workspace=workspace, filename=filename, mime=mime)
                stored.file.save(filename, ContentFile(content), save=False)
                stored.save()
                copied[url] = stored.pk
            if copied[url]:
                graph["steps"][step_id]["config"]["file_id"] = copied[url]

        base = (name or data.get("name") or "Imported workflow").strip()[:110]
        unique, n = base, 2
        while Workflow.objects.for_workspace(workspace).filter(name=unique).exists():
            unique, n = f"{base} ({n})", n + 1
        workflow = Workflow.objects.create(
            workspace=workspace,
            name=unique,
            description=(data.get("description") or "")[:255],
            trigger=trigger,
        )
        WorkflowVersion.objects.create(
            workflow=workflow, number=1, graph=graph, created_by=user, note="Imported from respond.io"
        )
    return workflow, converter.notes
