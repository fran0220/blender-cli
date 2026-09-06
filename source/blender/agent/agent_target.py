# SPDX-FileCopyrightText: 2026 blender-cli Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Reference targets and the objective the process scores after every action.

`register(session)` is the kernel's `PROVIDER_MODULES` entry point for
workstream T: it installs the objective provider (order 300), the `target` and
`fit` request handlers and the `agent.objective`/`agent.fit` helpers, and
replaces `session.targets` with the target set loaded from disk. Targets live
on the session as `session.targets` and under `.blender-cli/targets/<name>/`.
"""

import json
from pathlib import Path
import re
import shutil
import time

import bpy
import numpy as np

from agent_compare import METRICS, measure, reference
from agent_observe import VIEWS, isolated_data, names, png, render_passes, render_scene, aim

FEEDBACK_SIZE = 256
# The kernel's contract table owns this default; it is repeated, never redefined.
DEFAULT_METRICS = ("iou",)
# +1: larger is better. -1: smaller is better.
DIRECTION = {"iou": 1, "ssim": 1, "chamfer": -1, "hist": -1}
GRID = 4
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")


def improves(metric, value, best):
    return value > best if DIRECTION[metric] > 0 else value < best


class Target:
    """One reference image bound to one preset view, with its preprocessing policy."""

    def __init__(self, name, ref, view="front", mask="auto", fit="bbox", metrics=DEFAULT_METRICS):
        if not NAME.match(name):
            raise ValueError(f"Target name must match [A-Za-z0-9][A-Za-z0-9._-]*: {name}")
        if view not in VIEWS:
            raise ValueError(f"Unknown view: {view}")
        if mask not in ("auto", "none"):
            raise ValueError("mask must be auto or none")
        if fit not in ("bbox", "none"):
            raise ValueError("fit must be bbox or none")
        self.name, self.view, self.mask, self.fit = name, view, mask, fit
        self.metrics = tuple(names(metrics, METRICS))
        self.ref = str(Path(ref).resolve())
        if not Path(self.ref).is_file():
            raise FileNotFoundError(self.ref)
        self.tiles = {}

    def tile(self, size):
        """Reference RGB, silhouette and bbox record at one tile size; cached per size."""
        if size not in self.tiles:
            self.tiles[size] = reference(self.ref, size, self.mask, self.fit)
        return self.tiles[size]

    def record(self):
        return {"name": self.name, "ref": self.ref, "view": self.view, "mask": self.mask,
                "fit": self.fit, "metrics": list(self.metrics),
                "reference": self.tile(FEEDBACK_SIZE)[2]}


class Targets(dict):
    """Name to Target, plus the previous scoring and the best seen this session.

    A dict, so `session.targets` reads exactly as the kernel declares it: the
    target names `session status` reports.
    """

    def __init__(self, directory):
        super().__init__()
        self.directory = Path(directory)
        self.previous = {}
        self.best = {}
        if self.directory.is_dir():
            for path in sorted(self.directory.glob("*/target.json")):
                stored = json.loads(path.read_text(encoding="utf-8"))
                entry = Target(stored["name"], stored["ref"], stored["view"], stored["mask"],
                               stored["fit"], stored["metrics"])
                self[entry.name] = entry

    def store(self, entry):
        directory = self.directory / entry.name
        directory.mkdir(parents=True, exist_ok=True)
        copy = directory / ("reference" + Path(entry.ref).suffix.lower())
        for stale in directory.glob("reference.*"):
            if stale != copy:
                stale.unlink()
        if Path(entry.ref) != copy:
            shutil.copyfile(entry.ref, copy)
        entry.ref = str(copy.resolve())
        with isolated_data():
            silhouette = entry.tile(FEEDBACK_SIZE)[1]
        (directory / "silhouette.png").write_bytes(
            png(np.repeat(silhouette[:, :, None].astype(np.uint8) * 255, 3, axis=2)))
        (directory / "target.json").write_text(
            json.dumps({**entry.record(), "at": time.time()}, indent=2), encoding="utf-8")
        self[entry.name] = entry
        self.previous.pop(entry.name, None)
        self.best.pop(entry.name, None)
        return directory

    def remove(self, name):
        self.pop(name)
        self.previous.pop(name, None)
        self.best.pop(name, None)
        shutil.rmtree(self.directory / name, ignore_errors=True)


def store(session=None):
    """Targets for a session, or the working directory's targets for a one-shot request."""
    if session is None:
        return Targets(Path.cwd() / ".blender-cli" / "targets")
    if not isinstance(session.targets, Targets):
        # The kernel opens a session with a plain dict; T fills it in at registration.
        session.targets = Targets(Path(session.snapshot_directory).parent / "targets")
    return session.targets


def position(session):
    """The snapshot and step an objective value was reached at."""
    if session is None:
        return {"snapshot": None, "step": None}
    return {"snapshot": session.current, "step": session.step}


def worst_cell(reference_mask, render_mask, size):
    """The 4x4 cell with the most disagreeing pixels, split into missing and extra."""
    edges = [round(index * size / GRID) for index in range(GRID + 1)]
    worst = None
    for row in range(GRID):
        for column in range(GRID):
            y0, y1, x0, x1 = edges[row], edges[row + 1], edges[column], edges[column + 1]
            a, b = reference_mask[y0:y1, x0:x1], render_mask[y0:y1, x0:x1]
            missing = int(np.count_nonzero(a & ~b))
            extra = int(np.count_nonzero(b & ~a))
            error = missing + extra
            if worst is not None and error <= worst[0]:
                continue
            union = int(np.count_nonzero(a | b))
            worst = (error, {"region": [x0, y0, x1, y1],
                             "iou": float(np.count_nonzero(a & b) / union) if union else 1.0,
                             "missing": missing / error if error else 0.0,
                             "extra": extra / error if error else 0.0})
    return worst[1]


def render_views(source, views, size):
    """One converted scene, one render per distinct view; caller owns isolated data."""
    scene, points, center, radius, framing = render_scene(source, size, None)
    rendered = {}
    for view in views:
        if view == "camera" and source.camera is None:
            raise ValueError("The camera view requires scene.camera")
        near, far = aim(scene, source, view, points, center, radius)
        images = render_passes(scene, size, near, far)
        rendered[view] = (images["color"] / 255, images["silhouette"][:, :, 0] != 0)
    return rendered, framing


def budget_tiles(size):
    """Views the perception provider already rendered for the request being answered.

    Its provider runs at order 200, this one at 300, and both render through
    `render_budget`, so a target on a budget view costs no second render.
    """
    try:
        import agent_feedback
    except ModuleNotFoundError:
        return {}
    pending = agent_feedback.STATE.pending
    if not pending or "tiles" not in pending or pending["policy"]["image"]["size"] != size:
        return {}
    return {view: (tile["color"] / 255, tile["silhouette"])
            for view, tile in pending["tiles"].items()}


def score(entries, size, metrics=None, shared=False):
    """Per-target metrics and worst cell at one tile size; no session bookkeeping.

    `metrics` overrides each target's registered metrics, which is how `fit`
    scores a metric a target was not registered with. `shared` is set only
    while answering the request whose budget render is still current.
    """
    source = bpy.context.scene
    result = {}
    rendered = budget_tiles(size) if shared else {}
    missing = [view for view in sorted({entry.view for entry in entries}) if view not in rendered]
    with isolated_data():
        tiles = {entry.name: entry.tile(size) for entry in entries}
        if missing:
            rendered.update(render_views(source, missing, size)[0])
    for entry in entries:
        reference_rgb, reference_mask, _ = tiles[entry.name]
        render_rgb, render_mask = rendered[entry.view]
        wanted = entry.metrics if metrics is None else tuple(names(metrics, METRICS))
        result[entry.name] = {
            "metrics": measure(reference_rgb, reference_mask, render_rgb, render_mask, wanted),
            "worst": worst_cell(reference_mask, render_mask, size),
            "model": render_mask, "reference": reference_mask,
            "view": entry.view, "size": [size, size]}
    return result


def error_image(reference, model):
    """Silhouette error: missing red, extra blue, agreement white, background black.

    The one composer for this picture. `fit` returns it as `error_map` and the
    image provider crops it to the worst region for the `error` kind, so the
    two never drift.
    """
    image = np.zeros(reference.shape + (3,), dtype=np.uint8)
    image[reference & model] = (255, 255, 255)
    image[reference & ~model] = (220, 40, 40)
    image[model & ~reference] = (40, 90, 220)
    return image


def error_map(entry, size):
    """The error image for one target, rendered fresh, with its worst cell."""
    source = bpy.context.scene
    with isolated_data():
        _, reference_mask, _ = entry.tile(size)
        rendered, _ = render_views(source, [entry.view], size)
    _, render_mask = rendered[entry.view]
    return (error_image(reference_mask, render_mask),
            worst_cell(reference_mask, render_mask, size))


def event(session=None, record=True, shared=False):
    """The objective event payload: metrics, delta against the last step, worst cell, best so far."""
    state = store(session)
    entries = [state[name] for name in sorted(state)]
    if not entries:
        return {"targets": {}, "best": {}}
    scored = score(entries, FEEDBACK_SIZE, shared=shared)
    reached = position(session)
    targets, best = {}, {}
    for entry in entries:
        values = scored[entry.name]["metrics"]
        previous = state.previous.get(entry.name)
        primary = entry.metrics[0]
        known = state.best.get(entry.name)
        if known is None or improves(primary, values[primary], known[primary]):
            known = {primary: values[primary], **reached}
        if record:
            state.previous[entry.name] = dict(values)
            state.best[entry.name] = known
        targets[entry.name] = {
            **values,
            "delta": {key: values[key] - previous[key] for key in values} if previous else None,
            "worst": scored[entry.name]["worst"]}
        best[entry.name] = dict(known)
    if record and session is not None:
        # What the image provider draws the error kind from, at the size it was
        # scored. Set before this provider returns, read at order 400.
        session.last_objective = {"size": FEEDBACK_SIZE, "targets": {
            name: {"view": scored[name]["view"],
                   "reference": scored[name]["reference"], "model": scored[name]["model"],
                   "worst": targets[name]["worst"],
                   "metric": state[name].metrics[0],
                   "delta": (targets[name]["delta"] or {}).get(state[name].metrics[0])}
            for name in targets}}
    return {"targets": targets, "best": best}


def objective(session=None):
    """`agent.objective()`: the objective event payload, without recording it.

    The helper registry passes the session in as the first argument.
    """
    if session is None:
        import agent
        session = agent._session
    return event(session, record=False)


def handle(request, session=None, emit=None):
    """The `target` request: set, list or clear; returns the `done` fields."""
    action = request.get("action")
    state = store(session)
    if action == "set":
        if "ref" not in request or "name" not in request:
            raise ValueError("target set requires name and ref")
        entry = Target(request["name"], request["ref"], request.get("view", "front"),
                       request.get("mask", "auto"), request.get("fit", "bbox"),
                       request.get("metrics", DEFAULT_METRICS))
        directory = state.store(entry)
        # `target` does not mutate, so no objective event follows it. Answering
        # the first score here is what saves the agent a round trip.
        return {**entry.record(), "silhouette": str(directory / "silhouette.png"),
                "objective": event(session)}
    if action == "list":
        return {"targets": [state[name].record() for name in sorted(state)]}
    if action == "clear":
        name = request.get("name")
        if name is not None and name not in state:
            raise KeyError(f"No such target: {name}")
        cleared = sorted(state) if name is None else [name]
        for target in cleared:
            state.remove(target)
        return {"cleared": cleared}
    raise ValueError("target requires an action: set|list|clear")


class Provider:
    """Order 300: scores every registered target after every state-changing request."""

    name = "objective"
    order = 300

    def before(self, request, session):
        # A scoring from the previous action must never be pictured as this
        # one's, so the handoff is cleared here rather than only replaced later.
        session.last_objective = None

    def after(self, request, session, emit):
        if not session.request_feedback["objective"]:
            return
        # Only here is the perception provider's budget render current.
        result = event(session, shared=True)
        if result["targets"]:
            emit({"event": "objective", **result})


def register(session):
    """Workstream T's kernel registration point, called once per session."""
    import agent_runtime
    import agent_fit
    store(session)
    agent_runtime.register_provider(Provider())
    agent_runtime.register_op("target", handle)
    agent_runtime.register_op("fit", agent_fit.handle)
    agent_runtime.register_helper("objective", objective)
    agent_runtime.register_helper("fit", agent_fit.helper)
