# SPDX-FileCopyrightText: 2026 blender-cli Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Bounded in-process parameter search against registered targets.

`handle(request, session, emit)` answers `{"op": "fit"}`, emitting `progress`
events and returning the `done` fields; `agent_target.register` installs it.
Program parameters are read and written through `agent_program.parameters` and
`agent_program.set_parameters`, so an evaluation re-executes only the steps
that read a changed name; RNA paths assign directly.
"""

import hashlib
import math
from pathlib import Path
import re
import sys
import tempfile
import time

import bpy
import numpy as np

import agent_target
from agent_observe import png

# The kernel's contract table owns these defaults; `seconds` has none, so the
# evaluation count is the only bound unless the agent asks for a deadline.
DEFAULT_BUDGET = {"evals": 200, "seconds": None, "size": 128,
                  "patience": 16, "tolerance": 1e-3}
METHODS = ("coordinate", "nelder-mead", "random")
# `patience` is the convergence rule. This floor only stops a step small enough
# that no trial differs from the point it came from, which would spin forever.
STEP_FLOOR = 1e-12
PROGRESS_INTERVAL = 0.5
SEED = 0
INDEX = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\[(-?\d+)\]\Z")


class Stop(Exception):
    """The search ended: `reason` is the `stopped` field of its `done`."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def split(path):
    """Split an RNA path on its top-level dots, leaving subscripts intact."""
    parts, current, depth, quote = [], "", 0, None
    for character in path:
        if quote:
            quote = None if character == quote else quote
        elif character in "\"'":
            quote = character
        elif character == "[":
            depth += 1
        elif character == "]":
            depth -= 1
        elif character == "." and depth == 0:
            parts.append(current)
            current = ""
            continue
        current += character
    parts.append(current)
    if depth or quote or not all(parts):
        raise ValueError(f"Malformed RNA path: {path}")
    return parts


def rna_target(path):
    """Resolve an RNA path to its live owner, attribute and optional array index."""
    parts = split(path)
    attribute, index = parts[-1], None
    match = INDEX.match(attribute)
    if match:
        attribute, index = match.group(1), int(match.group(2))
    head = ".".join(parts[:-1])
    owner = bpy.data.path_resolve(head) if head else bpy.data
    return owner, attribute, index


class Parameter:
    """One searched scalar: a program parameter by name or a live RNA path."""

    def __init__(self, spec):
        name, path = spec.get("name"), spec.get("path")
        if (name is None) == (path is None):
            raise ValueError('A fit parameter needs exactly one of "name" (program parameter) '
                             'or "path" (RNA path)')
        if "min" not in spec or "max" not in spec:
            raise ValueError(f'Fit parameter {name or path} needs "min" and "max"')
        self.name, self.path = name, path
        self.key = name if name is not None else path
        self.low, self.high = float(spec["min"]), float(spec["max"])
        if not self.high > self.low:
            raise ValueError(f"Fit parameter {self.key} needs max greater than min")

    def normalise(self, value):
        return min(1.0, max(0.0, (float(value) - self.low) / (self.high - self.low)))

    def denormalise(self, unit):
        return self.low + unit * (self.high - self.low)

    def get(self, session):
        if self.path is None:
            params = program(session).params
            if self.name not in params:
                raise KeyError(f"No such program parameter: {self.name}")
            return float(params[self.name])
        owner, attribute, index = rna_target(self.path)
        value = getattr(owner, attribute)
        return float(value if index is None else value[index])

    def set(self, value):
        owner, attribute, index = rna_target(self.path)
        if index is None:
            setattr(owner, attribute, value)
        else:
            getattr(owner, attribute)[index] = value


def program(session):
    """The session's program: it owns `P` and the re-execution a write triggers."""
    if session is None:
        raise ValueError("Program parameters require an open session; fit RNA paths instead")
    import agent_program
    return agent_program.attach(session)


def step_error():
    import agent_program
    return agent_program.StepError


class Objective:
    """A single number to maximise or minimise, from targets or from agent code."""

    def __init__(self, spec, state, size, namespace):
        spec = dict(spec or {})
        self.size, self.namespace = size, namespace
        self.code = spec.pop("code", None)
        if self.code is not None:
            if spec:
                raise ValueError('A code objective takes no other fields')
            self.direction, self.metric, self.entries, self.weights = 1, None, [], []
            return
        self.metric = spec.pop("metric", "iou")
        if self.metric not in agent_target.DIRECTION:
            raise ValueError(f"Unknown objective metric: {self.metric}")
        one = spec.pop("target", None)
        many = spec.pop("targets", None)
        weights = spec.pop("weights", None)
        if spec:
            raise ValueError(f"Unknown objective fields: {', '.join(sorted(spec))}")
        if one is not None and many is not None:
            raise ValueError('An objective takes "target" or "targets", not both')
        wanted = [one] if one is not None else (many if many is not None else sorted(state))
        if not wanted:
            raise ValueError("fit needs a registered target or a code objective")
        for name in wanted:
            if name not in state:
                raise KeyError(f"No such target: {name}")
        self.entries = [state[name] for name in wanted]
        self.weights = [1.0] * len(wanted) if weights is None else [float(w) for w in weights]
        if len(self.weights) != len(self.entries) or sum(self.weights) <= 0:
            raise ValueError("Objective weights must be one positive-sum weight per target")
        self.direction = agent_target.DIRECTION[self.metric]

    def __call__(self):
        if self.code is not None:
            return float(eval(self.code, self.namespace))
        scored = agent_target.score(self.entries, self.size, metrics=(self.metric,))
        total = sum(weight * scored[entry.name]["metrics"][self.metric]
                    for entry, weight in zip(self.entries, self.weights))
        return total / sum(self.weights)

    def record(self):
        if self.code is not None:
            return {"code": self.code}
        return {"targets": [entry.name for entry in self.entries], "metric": self.metric,
                "weights": self.weights}


def clamp(value):
    return min(1.0, max(0.0, value))


def coordinate(point, loss, step=0.25):
    """Cyclic coordinate descent: keep walking an improving direction, halve a barren cycle."""
    best = loss(point)
    while step > STEP_FLOOR:
        improved = False
        for axis in range(len(point)):
            for offset in (step, -step):
                walked = False
                while True:
                    trial = list(point)
                    trial[axis] = clamp(point[axis] + offset)
                    if trial[axis] == point[axis]:
                        break
                    value = loss(trial)
                    if value >= best:
                        break
                    best, point, improved, walked = value, trial, True, True
                if walked:
                    break
        if not improved:
            step /= 2


def nelder_mead(point, loss, spread=0.15):
    size = len(point)
    simplex = [list(point)]
    for axis in range(size):
        vertex = list(point)
        vertex[axis] = clamp(vertex[axis] + (spread if vertex[axis] + spread <= 1 else -spread))
        simplex.append(vertex)
    values = [loss(vertex) for vertex in simplex]
    while True:
        order = sorted(range(size + 1), key=lambda index: values[index])
        simplex, values = [simplex[i] for i in order], [values[i] for i in order]
        if max(abs(vertex[axis] - simplex[0][axis])
               for vertex in simplex for axis in range(size)) < 1e-6:
            return
        centroid = [sum(vertex[axis] for vertex in simplex[:-1]) / size for axis in range(size)]

        def toward(factor):
            return [clamp(centroid[axis] + factor * (simplex[-1][axis] - centroid[axis]))
                    for axis in range(size)]

        reflected = toward(-1.0)
        value = loss(reflected)
        if value < values[0]:
            expanded = toward(-2.0)
            expansion = loss(expanded)
            simplex[-1], values[-1] = (expanded, expansion) if expansion < value else (reflected, value)
        elif value < values[-2]:
            simplex[-1], values[-1] = reflected, value
        else:
            contracted = toward(0.5)
            contraction = loss(contracted)
            if contraction < values[-1]:
                simplex[-1], values[-1] = contracted, contraction
            else:
                for index in range(1, size + 1):
                    simplex[index] = [clamp(simplex[0][axis] + 0.5 * (simplex[index][axis] - simplex[0][axis]))
                                      for axis in range(size)]
                    values[index] = loss(simplex[index])


def random_search(point, loss, evals):
    """Seeded Latin hypercube, then coordinate refinement from its best sample."""
    generator = np.random.default_rng(SEED)
    count = max(2, evals // 2)
    grid = np.stack([generator.permutation(count) for _ in point], axis=1)
    samples = (grid + generator.random((count, len(point)))) / count
    best, best_point = loss(point), list(point)
    for row in samples:
        trial = [float(value) for value in row]
        value = loss(trial)
        if value < best:
            best, best_point = value, trial
    coordinate(best_point, loss, step=0.125)


def write(image, session):
    directory = (Path.cwd() / ".blender-cli" / "fit" if session is not None
                 else Path(tempfile.mkdtemp(prefix="blender-cli-fit-")))
    directory.mkdir(parents=True, exist_ok=True)
    encoded = png(image)
    path = directory / (hashlib.sha256(encoded).hexdigest() + ".png")
    path.write_bytes(encoded)
    return path


def cancelled(session):
    check = session.native.get("cancelled") if session is not None else None
    return bool(check and check())


def fit(params, objective=None, budget=None, method="coordinate", session=None, emit=None):
    """Search bounded parameters against the objective; leave Main at the best state."""
    if session is None:
        import agent
        session = agent._session
    if method not in METHODS:
        raise ValueError(f"method must be one of {', '.join(METHODS)}")
    if not params:
        raise ValueError("fit needs at least one parameter")
    settings = {**DEFAULT_BUDGET, **(budget or {})}
    unknown = set(settings) - set(DEFAULT_BUDGET)
    if unknown:
        raise ValueError(f"Unknown budget fields: {', '.join(sorted(unknown))}")
    if settings["evals"] < 1 or settings["size"] < 16:
        raise ValueError("budget needs evals >= 1 and size >= 16")
    if settings["seconds"] is not None and settings["seconds"] <= 0:
        raise ValueError("budget seconds must be greater than zero")
    if settings["patience"] < 1 or settings["tolerance"] < 0:
        raise ValueError("budget needs patience >= 1 and tolerance >= 0")
    parameters = [Parameter(spec) for spec in params]
    state = agent_target.store(session)
    namespace = session.namespace if session is not None else {}
    goal = Objective(objective, state, settings["size"], namespace)

    def assign(point):
        values = {p.key: p.denormalise(unit) for p, unit in zip(parameters, point)}
        named = {p.name: values[p.key] for p in parameters if p.path is None}
        if named:
            # Re-executes from the first step that reads a changed name.
            program(session).set_params(named)
        for parameter in parameters:
            if parameter.path is not None:
                parameter.set(values[parameter.key])
        bpy.context.view_layer.update()
        return values

    policy = session.request_feedback["progress"] if session is not None else "improvements"
    start = time.perf_counter()
    deadline = start + settings["seconds"] if settings["seconds"] else math.inf
    progress = {"evals": 0, "failed": 0, "best": None, "point": None, "curve": [],
                "at": start, "cache": {}, "stale": 0}

    def announce(improved):
        """`all` is a heartbeat and is rate limited; `improvements` is news and is not."""
        if emit is None or policy == "off":
            return
        now = time.perf_counter()
        if policy == "all":
            if now - progress["at"] < PROGRESS_INTERVAL:
                return
        elif not improved:
            return
        progress["at"] = now
        emit({"event": "progress", "eval": progress["evals"], "of": settings["evals"],
              "best": progress["best"],
              "params": {p.key: p.denormalise(unit)
                         for p, unit in zip(parameters, progress["point"])}})

    def loss(point):
        if cancelled(session):
            raise Stop("cancel")
        if progress["evals"] >= settings["evals"]:
            raise Stop("budget")
        if time.perf_counter() >= deadline:
            raise Stop("seconds")
        if progress["stale"] >= settings["patience"]:
            raise Stop("patience")
        key = tuple(round(unit, 9) for unit in point)
        if key in progress["cache"]:
            return progress["cache"][key]
        try:
            assign(point)
            score = goal()
        except step_error() as error:
            # A parameter the program cannot run is a dead region of the space,
            # not a failed request: cost it out and keep searching.
            progress["evals"] += 1
            progress["failed"] += 1
            progress["stale"] += 1
            progress["cache"][key] = math.inf
            print(f"fit: {error}", file=sys.stderr, flush=True)
            return math.inf
        progress["evals"] += 1
        cost = -goal.direction * score
        progress["cache"][key] = cost
        previous = progress["best"]
        improved = previous is None or cost < -goal.direction * previous
        if improved:
            progress["best"], progress["point"] = score, list(point)
            progress["curve"].append([progress["evals"], score])
        # Only a gain worth waiting for resets the patience counter; a smaller
        # one is still the best seen and still enters the curve.
        if previous is None or (improved and abs(score - previous) > settings["tolerance"]):
            progress["stale"] = 0
        else:
            progress["stale"] += 1
        announce(improved)
        return cost

    point = [parameter.normalise(parameter.get(session)) for parameter in parameters]
    # A method that exhausts its own step schedule ended for the reason
    # `patience` names: it has no improvement left to find.
    stopped = "patience"
    try:
        if method == "coordinate":
            coordinate(point, loss)
        elif method == "nelder-mead":
            nelder_mead(point, loss)
        else:
            random_search(point, loss, settings["evals"])
    except Stop as end:
        stopped = end.reason
    if progress["point"] is None:
        raise ValueError(
            f"fit scored nothing: {progress['failed']} of {progress['evals']} evaluations "
            "failed to run" if progress["failed"] else
            "fit ran no evaluation: the budget was exhausted before the first one")
    best = assign(progress["point"])
    result = {"method": method, "objective": goal.record(),
              "best": {"params": best, "score": progress["best"]},
              "evals": progress["evals"], "failed": progress["failed"],
              "curve": progress["curve"], "applied": True, "stopped": stopped,
              # The contract's generic done field for an op whose `cancels` is
              # `done`; `stopped` says which of the four reasons it was.
              "cancelled": stopped == "cancel"}
    if session is not None:
        result["best"]["snapshot"] = session.snapshot(None, "fit")
    if goal.entries:
        image, worst = agent_target.error_map(goal.entries[0], settings["size"])
        result["error_map"] = {"view": goal.entries[0].view, "target": goal.entries[0].name,
                               "image": str(write(image, session)),
                               "size": [settings["size"], settings["size"]],
                               "region": worst["region"]}
    return result


def handle(request, session=None, emit=None):
    """The `fit` request: returns the `done` payload after the search."""
    return fit(request["params"], request.get("objective"), request.get("budget"),
               request.get("method", "coordinate"), session=session, emit=emit)


def helper(session, params, objective=None, budget=None, method="coordinate"):
    """`agent.fit()`: the registry hands the session in, the agent supplies the rest."""
    return fit(params, objective=objective, budget=budget, method=method, session=session)
