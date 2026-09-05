# SPDX-FileCopyrightText: 2026 blender-cli Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Session controls alongside upstream bpy, not a replacement for it.

Every helper returns the same dict its event or request carries. Helpers whose
computation belongs to another module resolve through the runtime's helper
registry: a module registers `agent_runtime.register_helper(name, function)`
in its `register(session)`, and every registered function takes the session as
its first argument. A build without that module answers `NotImplemented` by
name instead of failing obscurely, and no sibling workstream ever edits this
file to add one.
"""

_session = None


def _active():
    if _session is None:
        raise RuntimeError("This operation requires an open blender-cli session")
    return _session


def _helper(name):
    import agent_runtime
    function = agent_runtime.helper(name)
    return lambda *args, **kwargs: function(_active(), *args, **kwargs)


def snapshot(label=None):
    """Return the content-addressed snapshot ID for the current session state."""
    return _active().snapshot(label, "snapshot")


def rollback(snapshot_id):
    """Restore a session snapshot ID, label or offset; return None. Reacquire RNA references."""
    _active().rollback(snapshot_id)


def diff():
    """Return added, changed and removed datablock records since the last exec boundary."""
    return _active().diff()


def history():
    """Return a list of snapshot event dicts containing snapshot, label, op, step and at."""
    return [dict(event) for event in _active().history]


def observe(views=("front",), passes=("color",), size=512, ref=None, frame=None):
    """Return a deterministic render dict with image path, views, passes, size and framing."""
    from agent_observe import observe as render
    return render(views=views, passes=passes, size=size, ref=ref, frame=frame)


def compare(ref, view, metrics=("iou",), mask="auto", size=512, frame=None, debug=False, fit="bbox"):
    """Return requested numeric metrics, view and fitted reference bbox/occupancy; metrics is a tuple/list.

    fit='bbox' removes reference margins; fit='none' preserves framing. debug adds a mask image path.
    """
    from agent_compare import compare as compare_render
    return compare_render(ref, view, metrics=metrics, mask=mask, size=size, frame=frame, debug=debug, fit=fit)


def describe(path):
    """Return live bpy RNA metadata or agent helper signatures, docstrings and parameter defaults."""
    from agent_rna import describe as describe_rna
    return describe_rna(path)


def perceive(view="front", size=256):
    """Return the perception dict for the current state, in the event's shape."""
    return _helper("perceive")(view=view, size=size)


def objective():
    """Return the objective dict scoring every registered target, in the event's shape."""
    return _helper("objective")()


def fit(params, objective=None, budget=None, method="coordinate"):
    """Search parameters against the registered targets and apply the best result."""
    return _helper("fit")(params, objective=objective, budget=budget, method=method)


def program():
    """Return the current program text, parameters, step count and version."""
    return _helper("program")()


def register_provider(provider):
    """Register a feedback provider: name, order, before(request, session), after(request, session, emit)."""
    import agent_runtime
    agent_runtime.register_provider(provider)
