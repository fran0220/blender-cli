# SPDX-FileCopyrightText: 2026 blender-cli Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Session controls alongside upstream bpy, not a replacement for it."""

_session = None


def _active():
    if _session is None:
        raise RuntimeError("This operation requires an open blender-cli session")
    return _session


def snapshot(label=None):
    """Return the content-addressed snapshot ID for the current session state."""
    return _active().snapshot(label, "snapshot")


def rollback(snapshot_id):
    """Restore a session snapshot ID or relative offset; return None. Reacquire RNA references afterwards."""
    _active().rollback(snapshot_id)


def diff():
    """Return added, changed and removed datablock records since the last exec boundary."""
    return _active().diff()


def history():
    """Return a list of snapshot event dicts containing snapshot, label, verb and at."""
    return [dict(event) for event in _active().history]


def observe(views=("front",), passes=("color",), size=512, ref=None):
    """Return a deterministic render dict with image path, views, passes, size and world-space framing."""
    from agent_observe import observe as render
    return render(views=views, passes=passes, size=size, ref=ref)


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
