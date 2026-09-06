# SPDX-FileCopyrightText: 2026 blender-cli Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Pushed perception and images: one budget render per action, deltas against what the agent last saw.

`register` attaches the two providers and the `agent.perceive` helper to a
session; everything else here is what they need. The perception provider
renders the budget views once per request and remembers the buffers; the image
provider crops the same buffers, so an action never renders twice.
"""

import base64
import hashlib
from pathlib import Path
import tempfile

import bpy
import numpy as np

import agent
from agent_compare import measure
from agent_observe import VIEWS, png, render_budget

PASSES = ("color", "silhouette")
PADDING = 8
# EEVEE is byte-deterministic for identical state, so any visible edit exceeds this;
# it exists so a driver's last-bit noise cannot manufacture a changed region.
TOLERANCE = 2
OVERLAY = {"before": (255, 0, 0), "after": (0, 255, 255),
           "both": (255, 255, 255), "neither": (32, 32, 32)}


UNCHANGED = {"region": None, "fraction": 0.0, "silhouette_delta": 0.0}


class State:
    """What the agent last saw, per view, plus this request's render."""

    def __init__(self):
        self.views = {}
        self.framing = None
        self.counts = None
        self.snapshot = None
        self.budget = None
        self.pending = None


STATE = State()


def register(session):
    """Attach the perception and image channels, and `agent.perceive`, to a session."""
    global STATE
    STATE = State()
    import agent_runtime
    agent_runtime.register_provider(Perception())
    agent_runtime.register_provider(Image())
    agent_runtime.register_helper("perceive", perceive)


def changed_objects(session):
    """Object names an agent would recognise, from this request's ID diff."""
    difference = session.last_diff if session.last_diff is not None else session.diff()
    entries = difference["added"] + difference["changed"] + difference["removed"]
    names = {entry["name"] for entry in entries if entry["type"] == "OBJECT"}
    data = {entry["name"] for entry in entries if entry["type"] != "OBJECT"}
    for obj in bpy.context.scene.objects:
        if obj.data is not None and obj.data.name in data:
            names.add(obj.name)
    return sorted(names)


def symmetry(tile):
    """Silhouette IoU under mirroring about each world axis, in this view.

    The axis along the view direction is invisible to an orthographic silhouette,
    so it is null rather than the trivial 1.0 that mirroring the buffer would report.
    """
    mask = tile["silhouette"]
    result = {"x": None, "y": None, "z": None}
    for name, axis in tile["axes"].items():
        flipped = np.flip(mask, axis=axis)
        union = np.count_nonzero(mask | flipped)
        result[name] = float(np.count_nonzero(mask & flipped) / union) if union else 1.0
    return result


def difference(previous, current):
    """Changed region, changed fraction and silhouette delta of one view."""
    distance = np.abs(current["color"].astype(np.int16) - previous["color"].astype(np.int16)).max(axis=2)
    changed = (distance > TOLERANCE) | (current["silhouette"] != previous["silhouette"])
    region = None
    if changed.any():
        rows, columns = np.nonzero(changed)
        region = [int(columns.min()), int(rows.min()), int(columns.max()) + 1, int(rows.max()) + 1]
    # Only the silhouettes decide IoU; the colors go with them to keep one metric implementation.
    iou = measure(previous["color"], previous["silhouette"],
                  current["color"], current["silhouette"], ("iou",))["iou"]
    return {"region": region, "fraction": float(np.count_nonzero(changed)) / changed.size,
            "silhouette_delta": float(1 - iou)}


def comparable(previous, tiles, view):
    """A view rendered at another budget size is a view the agent has not seen."""
    return view in previous and previous[view]["color"].shape == tiles[view]["color"].shape


def perception(session, view, tile, framing, counts, change):
    """The perception payload: scene facts plus the delta against what the agent last saw."""
    bounds = framing["bounds"]
    changed = None if change is None else {"objects": changed_objects(session),
                                           "view": view, **change}
    return {**counts, "bounds": bounds,
            "dims": [high - low for low, high in zip(bounds["low"], bounds["high"])],
            "framing": framing, "changed": changed, "symmetry": symmetry(tile)}


def settled(session, views, budget):
    """True when this request cannot have changed the picture the agent already has.

    The picture is a function of Main. The diff provider has already established
    whether any datablock changed and left the state it settled on in
    `session.current`, so an action that changed nothing, at the same budget, is
    answered from the buffers instead of rendering them again. The snapshot is
    part of the test because an in-code rollback moves Main without leaving a diff.
    """
    return (session.last_diff is not None and not any(session.last_diff.values()) and
            STATE.snapshot == session.current and STATE.budget == budget and
            all(view in STATE.views for view in views))


def sample(session):
    """Answer this request's budget views, rendering them only if the picture can have moved."""
    if STATE.pending is not None:
        return STATE.pending
    policy = session.request_feedback
    views = list(dict.fromkeys(policy["image"]["views"]))
    budget = (policy["image"]["size"], policy["image"]["samples"])
    previous = STATE.views
    if settled(session, views, budget):
        STATE.pending = {
            "policy": policy, "views": views, "tiles": previous, "previous": previous,
            "changes": {view: dict(UNCHANGED) for view in views},
            "perception": perception(session, views[0], previous[views[0]], STATE.framing,
                                     STATE.counts, dict(UNCHANGED)),
        }
        return STATE.pending
    try:
        tiles, framing, counts = render_budget(views, *budget)
    except BaseException as error:
        STATE.pending = {"error": f"{type(error).__name__}: {error}", "policy": policy,
                         "views": views}
        raise
    changes = {view: difference(previous[view], tiles[view]) if comparable(previous, tiles, view)
               else None for view in views}
    STATE.views, STATE.framing, STATE.counts = tiles, framing, counts
    STATE.snapshot, STATE.budget = session.current, budget
    STATE.pending = {
        "policy": policy, "views": views, "tiles": tiles, "previous": previous, "changes": changes,
        "perception": perception(session, views[0], tiles[views[0]], framing, counts,
                                 changes[views[0]]),
    }
    return STATE.pending


def perceive(session, view="front", size=256):
    """Return the perception payload for one view without advancing the remembered state."""
    if view not in VIEWS:
        raise ValueError(f"Unknown view: {view}")
    if not isinstance(size, int) or size < 1:
        raise ValueError("size must be a positive integer")
    tiles, framing, counts = render_budget([view], size, session.request_feedback["image"]["samples"])
    change = (difference(STATE.views[view], tiles[view])
              if comparable(STATE.views, tiles, view) else None)
    return perception(session, view, tiles[view], framing, counts, change)


def buffer(tile, name):
    if name not in PASSES:
        raise ValueError(f"The budget view renders {' and '.join(PASSES)}, not {name}")
    if name == "silhouette":
        return np.repeat(tile["silhouette"][:, :, None].astype(np.uint8) * 255, 3, axis=2)
    return tile["color"]


def overlay(previous, current, region):
    """Before red, after cyan, agreement white, neither the neutral background."""
    x0, y0, x1, y1 = region
    before, after = previous["silhouette"][y0:y1, x0:x1], current["silhouette"][y0:y1, x0:x1]
    rgb = np.full(before.shape + (3,), OVERLAY["neither"], dtype=np.uint8)
    rgb[before & ~after] = OVERLAY["before"]
    rgb[after & ~before] = OVERLAY["after"]
    rgb[before & after] = OVERLAY["both"]
    return rgb


def emit_image(kind, view, rgb, region, policy, pass_name=None, **extra):
    data = png(np.ascontiguousarray(rgb))
    event = {"event": "image", "kind": kind, "view": view,
             "pass": pass_name or policy["pass"],
             "size": [rgb.shape[1], rgb.shape[0]], "region": region, **extra}
    if policy["inline"]:
        # An inline image crosses the boundary instead of a file, never beside one.
        return {**event, "inline": base64.b64encode(data).decode("ascii")}
    directory = (Path.cwd() / ".blender-cli" / "feedback" if agent._session
                 else Path(tempfile.mkdtemp(prefix="blender-cli-feedback-")))
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (hashlib.sha256(data).hexdigest() + ".png")
    path.write_bytes(data)
    return {**event, "path": str(path)}


def target_images(session, pending):
    """One error map per scored target: where the model disagrees with its reference.

    The objective provider scored at order 300 from this request's budget render and
    left the masks behind, so this costs no render. It answers the question a delta
    cannot: a delta shows what the last action moved, an error map shows what is still
    wrong. The picture itself is the objective's, cropped here to the worst region.
    """
    policy = pending["policy"]["image"]
    objective = session.last_objective
    if not objective:
        return []
    # The objective owns how the map looks, so `fit`'s picture and this one are one picture.
    from agent_target import error_image

    size = objective["size"]
    events = []
    for name in sorted(objective["targets"]):
        target = objective["targets"][name]
        # A target that barely moved is a number, not a picture; a first scoring is both.
        if target["delta"] is not None and abs(target["delta"]) <= policy["threshold"]:
            continue
        x0, y0, x1, y1 = target["worst"]["region"]
        region = [max(0, x0 - PADDING), max(0, y0 - PADDING),
                  min(size, x1 + PADDING), min(size, y1 + PADDING)]
        crop = error_image(target["reference"], target["model"])[region[1]:region[3],
                                                                region[0]:region[2]]
        events.append(emit_image("error", target["view"], crop, region, policy,
                                 target=name, pass_name="silhouette"))
    return events


def view_images(view, pending):
    policy = pending["policy"]["image"]
    tile, change = pending["tiles"][view], pending["changes"][view]
    size = tile["silhouette"].shape[0]
    if change is None:
        # Nothing to be a delta against: the agent has never seen this view.
        return [emit_image("full", view, buffer(tile, policy["pass"]), [0, 0, size, size], policy)]
    if change["fraction"] < policy["threshold"]:
        return []
    if policy["mode"] == "full":
        return [emit_image("full", view, buffer(tile, policy["pass"]), [0, 0, size, size], policy)]
    x0, y0, x1, y1 = change["region"]
    region = [max(0, x0 - PADDING), max(0, y0 - PADDING),
              min(size, x1 + PADDING), min(size, y1 + PADDING)]
    crop = buffer(tile, policy["pass"])[region[1]:region[3], region[0]:region[2]]
    events = [emit_image("delta", view, crop, region, policy)]
    if policy["overlay"]:
        events.append(emit_image("overlay", view, overlay(pending["previous"][view], tile, region),
                                 region, policy, pass_name="silhouette"))
    return events


class Perception:
    """The perceptual channel: what the picture now says, and how it moved."""

    name = "perception"
    order = 200

    def before(self, request, session):
        STATE.pending = None

    def after(self, request, session, emit):
        if not session.request_feedback["perception"]:
            return
        pending = sample(session)
        session.last_perception = pending["perception"]
        emit({"event": "perception", **pending["perception"]})


class Image:
    """The picture itself, as a delta against what the agent already saw."""

    name = "image"
    order = 400

    def before(self, request, session):
        pass

    def after(self, request, session, emit):
        policy = session.request_feedback["image"]
        if policy["mode"] == "off":
            return
        try:
            pending = sample(session)
        except BaseException as error:
            pending = {"error": f"{type(error).__name__}: {error}"}
        if "error" in pending:
            # The picture channel reports its own failure; it never fails the request.
            emit({"event": "image", "kind": "error", "view": policy["views"][0],
                  "message": pending["error"]})
            return
        for view in pending["views"]:
            for event in view_images(view, pending):
                emit(event)
        for event in target_images(session, pending):
            emit(event)
