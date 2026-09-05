# SPDX-FileCopyrightText: 2026 blender-cli Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Pushed perception and images: one budget render per action, deltas against what the agent last saw.

`PROVIDERS` is what the runtime registers; everything else here is what they need.
The perception provider renders the budget views once per request and remembers the
buffers; the image provider crops the same buffers, so an action never renders twice.
"""

import base64
import copy
import hashlib
from pathlib import Path
import tempfile

import bpy
import numpy as np

import agent
from agent_compare import measure
from agent_observe import VIEWS, png, render_budget

PASSES = ("color", "silhouette")
MODES = ("delta", "full", "off")
SIZES = (128, 256, 512)
PADDING = 8
# EEVEE is byte-deterministic for identical state, so any visible edit exceeds this;
# it exists so a driver's last-bit noise cannot manufacture a changed region.
TOLERANCE = 2
OVERLAY = {"before": (255, 0, 0), "after": (0, 255, 255),
           "both": (255, 255, 255), "neither": (32, 32, 32)}

DEFAULT_POLICY = {
    "perception": True,
    "objective": True,
    "image": {"mode": "delta", "threshold": 0.002, "views": ["front"],
              "pass": "color", "size": 256, "overlay": True},
}


class State:
    """What the agent last saw, per view, plus this request's render."""

    def __init__(self):
        self.policy = copy.deepcopy(DEFAULT_POLICY)
        self.views = {}
        self.pending = None
        self.last_perception = None


STATE = State()


def validate(policy):
    image = policy["image"]
    if image["mode"] not in MODES:
        raise ValueError(f"image.mode must be one of {', '.join(MODES)}")
    if not isinstance(image["threshold"], (int, float)) or not 0 <= image["threshold"] <= 1:
        raise ValueError("image.threshold must be a fraction between 0 and 1")
    views = list(image["views"])
    if not views or any(view not in VIEWS for view in views):
        raise ValueError(f"image.views must be a nonempty list from {', '.join(VIEWS)}")
    if image["pass"] not in PASSES:
        raise ValueError(f"image.pass must be one of {', '.join(PASSES)}")
    if image["size"] not in SIZES:
        raise ValueError(f"image.size must be one of {', '.join(map(str, SIZES))}")
    policy["perception"] = bool(policy["perception"])
    policy["image"] = {**image, "views": views, "threshold": float(image["threshold"]),
                       "overlay": bool(image["overlay"])}
    return policy


def configure(values):
    """Merge a partial feedback policy into the session policy and return the whole policy."""
    policy = copy.deepcopy(STATE.policy)
    for key, value in values.items():
        if key == "image":
            if not isinstance(value, dict) or any(item not in policy["image"] for item in value):
                raise ValueError(f"image accepts {', '.join(sorted(policy['image']))}")
            policy["image"].update(value)
        elif key in policy:
            policy[key] = value
        else:
            raise ValueError(f"Unknown feedback key: {key}")
    STATE.policy = validate(policy)
    return copy.deepcopy(STATE.policy)


def policy(request):
    """The session policy, with this request's `feedback` image overrides applied."""
    override = (request or {}).get("feedback") or {}
    if not override:
        return STATE.policy
    merged = copy.deepcopy(STATE.policy)
    if any(key != "image" for key in override):
        raise ValueError("A request may override only the image policy")
    merged["image"].update(override.get("image", {}))
    return validate(merged)


def changed_objects():
    """Object names an agent would recognise, from this request's ID diff."""
    if agent._session is None:
        return []
    difference = agent.diff()
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
    return (view in previous and
            previous[view]["color"].shape == tiles[view]["color"].shape)


def perception(view, tile, framing, counts, change):
    """The perception payload: scene facts plus the delta against what the agent last saw."""
    bounds = framing["bounds"]
    changed = None if change is None else {"objects": changed_objects(), "view": view, **change}
    return {**counts, "bounds": bounds,
            "dims": [high - low for low, high in zip(bounds["low"], bounds["high"])],
            "framing": framing, "changed": changed, "symmetry": symmetry(tile)}


def sample(request):
    """Render the budget views once for this request and advance the remembered state."""
    if STATE.pending is not None:
        return STATE.pending
    current = policy(request)
    views = list(dict.fromkeys(current["image"]["views"]))
    previous = STATE.views
    try:
        tiles, framing, counts = render_budget(views, current["image"]["size"])
    except BaseException as error:
        STATE.pending = {"error": f"{type(error).__name__}: {error}", "policy": current, "views": views}
        raise
    STATE.views = tiles
    changes = {view: difference(previous[view], tiles[view]) if comparable(previous, tiles, view)
               else None for view in views}
    STATE.pending = {
        "policy": current, "views": views, "tiles": tiles, "previous": previous, "changes": changes,
        "perception": perception(views[0], tiles[views[0]], framing, counts, changes[views[0]]),
    }
    return STATE.pending


def perceive(view="front", size=None):
    """Return the perception payload for one view without advancing the remembered state."""
    if view not in VIEWS:
        raise ValueError(f"Unknown view: {view}")
    size = STATE.policy["image"]["size"] if size is None else size
    if size not in SIZES:
        raise ValueError(f"size must be one of {', '.join(map(str, SIZES))}")
    tiles, framing, counts = render_budget([view], size)
    change = difference(STATE.views[view], tiles[view]) if comparable(STATE.views, tiles, view) else None
    return perception(view, tiles[view], framing, counts, change)


def buffer(tile, name):
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


def emit_image(kind, view, rgb, region, current, request, **extra):
    data = png(np.ascontiguousarray(rgb))
    directory = (Path.cwd() / ".blender-cli" / "feedback" if agent._session
                 else Path(tempfile.mkdtemp(prefix="blender-cli-feedback-")))
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (hashlib.sha256(data).hexdigest() + ".png")
    path.write_bytes(data)
    event = {"event": "image", "kind": kind, "view": view, "pass": current["pass"],
             "path": str(path), "size": [rgb.shape[1], rgb.shape[0]], "region": region, **extra}
    if (request or {}).get("inline"):
        event["inline"] = base64.b64encode(data).decode("ascii")
    return event


def view_images(view, pending, request):
    current = pending["policy"]["image"]
    tile, change = pending["tiles"][view], pending["changes"][view]
    size = tile["silhouette"].shape[0]
    whole = [0, 0, size, size]
    if change is None:
        # Nothing to be a delta against: the agent has never seen this view.
        return [emit_image("full", view, buffer(tile, current["pass"]), whole, current, request)]
    if change["fraction"] < current["threshold"]:
        return []
    if current["mode"] == "full":
        return [emit_image("full", view, buffer(tile, current["pass"]), whole, current, request)]
    x0, y0, x1, y1 = change["region"]
    region = [max(0, x0 - PADDING), max(0, y0 - PADDING),
              min(size, x1 + PADDING), min(size, y1 + PADDING)]
    crop = buffer(tile, current["pass"])[region[1]:region[3], region[0]:region[2]]
    events = [emit_image("delta", view, crop, region, current, request)]
    if current["overlay"]:
        events.append(emit_image("overlay", view, overlay(pending["previous"][view], tile, region),
                                 region, current, request))
    return events


class Perception:
    name = "perception"
    order = 200

    def before(self, request, session):
        STATE.pending = None

    def after(self, request, session, emit):
        if not policy(request)["perception"]:
            return
        pending = sample(request)
        STATE.last_perception = pending["perception"]
        if session is not None:
            session.last_perception = pending["perception"]
        emit({"event": "perception", **pending["perception"]})


class Image:
    name = "image"
    order = 400

    def before(self, request, session):
        pass

    def after(self, request, session, emit):
        current = policy(request)["image"]
        if current["mode"] == "off":
            return
        try:
            pending = sample(request)
        except BaseException as error:
            pending = {"error": f"{type(error).__name__}: {error}"}
        if "error" in pending:
            # The picture channel reports its own failure; it never fails the request.
            emit({"event": "image", "kind": "error", "view": current["views"][0],
                  "message": pending["error"]})
            return
        for view in pending["views"]:
            for event in view_images(view, pending, request):
                emit(event)


PROVIDERS = (Perception(), Image())


def run(request=None):
    """Run the feedback providers for one request and return their events, in provider order.

    Interim driver with the failure isolation the registry specifies; the runtime's
    registry replaces it, and this function goes away, when workstream K lands.
    """
    request = request or {}
    events = []
    for provider in PROVIDERS:
        provider.before(request, agent._session)
    for provider in PROVIDERS:
        try:
            provider.after(request, agent._session, events.append)
        except BaseException as error:
            events.append({"event": "log", "stream": "stderr",
                           "text": f"feedback provider {provider.name} failed: "
                                   f"{type(error).__name__}: {error}\n"})
    return events
