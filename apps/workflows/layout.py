"""Place steps on the builder's canvas as a left-to-right tree.

Used for workflows that arrive without positions (an import) and for the
builder's tidy-up button. Each step sits one column right of the step that
first leads to it; a step with several exits sits level with the middle of
the steps it leads to. Jumps do not count as leading somewhere, so a loop
back to the menu never drags the menu across the page.
"""

from . import graph as g

COLUMN = 340
ROW = 40
START_X, START_Y = 260, 40


def _height(step):
    """Roughly how tall the builder draws a step: header plus one row per exit."""
    return 96 + 26 * max(len(g.exits_for(step)), 1)


def tidy(graph):
    """{step id: {"x", "y"}} for every step in `graph`."""
    steps = graph.get("steps") or {}
    placed = {}
    cursor = [START_Y]

    def place(step_id, depth, seen):
        if step_id in placed or step_id in seen or step_id not in steps:
            return None
        seen = seen | {step_id}
        step = steps[step_id]
        children = []
        for exit_key in g.exits_for(step):
            target = (step.get("next") or {}).get(exit_key)
            if target and target not in placed and target not in children and target not in seen:
                children.append(target)
        centres = [c for c in (place(child, depth + 1, seen) for child in children) if c is not None]
        height = _height(step)
        if centres:
            y = (centres[0] + centres[-1]) / 2 - height / 2
        else:
            y = cursor[0]
            cursor[0] += height + ROW
        placed[step_id] = {"x": START_X + depth * COLUMN, "y": round(y)}
        return y + height / 2

    place(graph.get("start"), 0, frozenset())
    for step_id in steps:
        if step_id not in placed:
            place(step_id, 0, frozenset())
    return placed


def positions_for(workflow, graph):
    """The workflow's saved positions, with any step never placed laid out afresh."""
    layout = dict(workflow.layout or {})
    steps = graph.get("steps") or {}
    if any(step_id not in layout for step_id in steps):
        fresh = tidy(graph)
        if not layout:
            layout = fresh
        else:
            # Only the newcomers move; what the editor arranged stays put.
            lowest = max((p["y"] for p in layout.values()), default=0) + 200
            for step_id in steps:
                if step_id not in layout:
                    layout[step_id] = {"x": fresh[step_id]["x"], "y": lowest}
                    lowest += 160
        workflow.layout = layout
        workflow.save(update_fields=["layout"])
    return layout
