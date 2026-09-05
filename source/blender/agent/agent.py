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
    return _active().snapshot(label, "snapshot")


def rollback(snapshot_id):
    _active().rollback(snapshot_id)


def diff():
    return _active().diff()


def history():
    return list(_active().history)


def observe(views=("front",), passes=("color",), size=512, ref=None):
    raise NotImplementedError("agent.observe is delivered in Phase 3")


def compare(ref, view, metrics=("iou",), mask="auto"):
    raise NotImplementedError("agent.compare is delivered in Phase 4")
