# SPDX-FileCopyrightText: 2026 blender-cli Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Deterministic observation of evaluated geometry, without editing the user's scene."""

import base64
import contextlib
import hashlib
import math
from pathlib import Path
import struct
import tempfile
import zlib

import bpy
import numpy as np
from mathutils import Vector

import agent


VIEWS = ("front", "back", "left", "right", "top", "bottom", "persp", "camera", "side")
PASSES = ("color", "wire", "silhouette", "normal", "depth")
BORDER = 2
OCCUPANCY = 1 / 1.1


def names(value, allowed):
    result = value.split(",") if isinstance(value, str) else list(value)
    if not result or any(item not in allowed for item in result):
        raise ValueError(f"Expected a nonempty list from {', '.join(allowed)}")
    return result


def srgb(linear):
    linear = np.maximum(linear, 0)
    return np.where(linear <= 0.0031308, linear * 12.92,
                    1.055 * np.power(linear, 1 / 2.4) - 0.055)


def bytes_rgb(rgb):
    return np.floor(np.clip(rgb, 0, 1) * 255 + 0.5).astype(np.uint8)


def png(rgb):
    """RGB8 PNG: fixed filter/compression, only IHDR/IDAT/IEND, no varying metadata."""
    height, width, _ = rgb.shape

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    rows = b"".join(b"\0" + row.tobytes() for row in rgb)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(rows, 9)) + chunk(b"IEND", b""))


def resize(rgb, width, height):
    """Deterministic pixel-center bilinear resampling, clamped at the image edge."""
    y = np.clip((np.arange(height) + 0.5) * rgb.shape[0] / height - 0.5, 0, rgb.shape[0] - 1)
    x = np.clip((np.arange(width) + 0.5) * rgb.shape[1] / width - 0.5, 0, rgb.shape[1] - 1)
    y0, x0 = y.astype(int), x.astype(int)
    y1, x1 = np.minimum(y0 + 1, rgb.shape[0] - 1), np.minimum(x0 + 1, rgb.shape[1] - 1)
    fy, fx = (y - y0)[:, None, None], (x - x0)[None, :, None]
    return ((rgb[y0[:, None], x0] * (1 - fx) + rgb[y0[:, None], x1] * fx) * (1 - fy) +
            (rgb[y1[:, None], x0] * (1 - fx) + rgb[y1[:, None], x1] * fx) * fy)


def transform(points, matrix, rows=3):
    """Apply a matrix to whole arrays of points, in upstream's own term order.

    Blender's `mul_v3_m4v3` sums `m[0]*x + m[1]*y + m[2]*z + m[3]` in that order and
    in single precision; keeping both makes a million points one vectorized pass
    without moving the result off the value the per-vertex path produced.
    """
    matrix = np.array(matrix, dtype=np.float32)
    columns = [points[:, 0] * matrix[row][0] + points[:, 1] * matrix[row][1] +
               points[:, 2] * matrix[row][2] + (matrix[row][3] if matrix.shape[1] == 4 else 0)
               for row in range(rows)]
    return np.stack(columns, axis=1)


@contextlib.contextmanager
def isolated_data():
    # Delete only IDs created by this operation, even if setup/render/composition fails.
    recalc = agent._native["preserve_recalc"]()
    groups = [getattr(bpy.data, prop.identifier) for prop in bpy.data.bl_rna.properties
              if prop.type == "COLLECTION"]
    before = {item.as_pointer() for group in groups for item in group}
    # Observation does not execute user render/frame/depsgraph handlers. They may mutate Main.
    handler_names = ("render_init", "render_pre", "render_post", "render_write", "render_complete",
                     "render_cancel", "render_stats", "frame_change_pre", "frame_change_post",
                     "depsgraph_update_pre", "depsgraph_update_post")
    handlers = [(getattr(bpy.app.handlers, name), list(getattr(bpy.app.handlers, name)))
                for name in handler_names]
    for callbacks, _ in handlers:
        callbacks.clear()
    try:
        yield
    finally:
        added = [item for group in groups for item in group if item.as_pointer() not in before]
        if added:
            bpy.data.batch_remove(added)
        bpy.context.view_layer.update()
        for callbacks, original in handlers:
            callbacks[:] = original
        del recalc


def render_scene(source, size, frame):
    graph = bpy.context.evaluated_depsgraph_get()
    scene = bpy.data.scenes.new("Agent observation")
    scene.frame_current = source.frame_current
    scene.frame_subframe = source.frame_subframe
    scene.render.engine = "BLENDER_EEVEE"
    scene.render.resolution_x = scene.render.resolution_y = size
    scene.render.resolution_percentage = 100
    scene.render.film_transparent = True
    scene.render.use_compositing = False
    scene.render.use_sequencer = False
    scene.render.use_stamp = False
    scene.render.dither_intensity = 0
    scene.eevee.taa_render_samples = 32
    scene.view_settings.view_transform = "Standard"
    scene.view_settings.look = "None"
    scene.view_settings.exposure = 0
    scene.view_settings.gamma = 1
    scene.display_settings.display_device = "sRGB"
    scene.view_layers[0].use_pass_z = True
    scene.view_layers[0].use_pass_normal = True
    scene.world = bpy.data.worlds.new("Agent neutral world")
    background = scene.world.node_tree.nodes.get("Background")
    background.inputs["Color"].default_value = (0.05, 0.05, 0.05, 1)
    background.inputs["Strength"].default_value = 1

    batches = []
    objects = set()
    if frame and frame not in source.objects:
        raise KeyError(f"No framing object: {frame}")
    for instance in graph.object_instances:
        obj = instance.object
        if not instance.show_self or obj.hide_render or obj.type in {"CAMERA", "LIGHT", "EMPTY", "ARMATURE", "LATTICE"}:
            continue
        if instance.is_instance and obj.type == "MESH":
            # Temporary GN objects retain the instancer's data_orig. Re-evaluating
            # for all layers would rebuild that object, not this evaluated instance.
            data = obj.data.copy()
        elif obj.type in {"MESH", "CURVE", "SURFACE", "FONT", "META"}:
            data = bpy.data.meshes.new_from_object(obj, preserve_all_data_layers=True, depsgraph=graph)
        else:
            data = obj.data.copy()
        copy = bpy.data.objects.new("Agent geometry", data)
        copy.matrix_world = instance.matrix_world.copy()
        scene.collection.objects.link(copy)
        if not frame or obj.original.name == frame:
            vertices = getattr(data, "vertices", ())
            if len(vertices):
                local = np.empty(len(vertices) * 3, dtype=np.float32)
                vertices.foreach_get("co", local)
                local = local.reshape(-1, 3)
            else:
                local = np.array(obj.bound_box, dtype=np.float32)
            batches.append(transform(local, instance.matrix_world))
            objects.add(obj.original.name)
    points = (np.concatenate(batches) if batches else
              np.array(((-1, -1, -1), (1, 1, 1)), dtype=np.float32))
    low = Vector(points.min(axis=0).tolist())
    high = Vector(points.max(axis=0).tolist())
    center = (low + high) / 2
    radius = max((high - low).length / 2, 0.01)
    # World-space, preference-independent key/fill/rim; SUN lights are scale independent.
    for direction, energy in (((-3, -4, 6), 3.0), ((4, -1, 2), 1.0), ((1, 4, 5), 2.0)):
        light = bpy.data.lights.new("Agent studio", "SUN")
        light.energy = energy
        light.angle = math.radians(10)
        obj = bpy.data.objects.new("Agent studio", light)
        obj.rotation_euler = (-Vector(direction)).to_track_quat('-Z', 'Y').to_euler()
        scene.collection.objects.link(obj)
    camera = bpy.data.objects.new("Agent camera", bpy.data.cameras.new("Agent camera"))
    scene.collection.objects.link(camera)
    scene.camera = camera
    framing = {"bounds": {"low": list(low), "high": list(high)}, "center": list(center),
               "radius": radius, "objects": sorted(objects), "occupancy": OCCUPANCY}
    return scene, points, center, radius, framing


def aim(scene, source, view, points, center, radius):
    camera = scene.camera
    if view == "camera":
        original = source.camera.evaluated_get(bpy.context.evaluated_depsgraph_get())
        camera.data = original.data.copy()
        camera.data.dof.use_dof = False
        camera.matrix_world = original.matrix_world.copy()
    else:
        # A preceding `camera` tile must not leak sensor fit, shift or panorama settings.
        camera.data = bpy.data.cameras.new("Agent preset")
        direction = Vector({"front": (0, -1, 0), "back": (0, 1, 0), "left": (-1, 0, 0),
                            "right": (1, 0, 0), "side": (1, 0, 0), "top": (0, 0, 1),
                            "bottom": (0, 0, -1), "persp": (1, -1, 0.8)}[view]).normalized()
        camera.rotation_euler = (-direction).to_track_quat('-Z', 'Y').to_euler()
        camera.data.type = "PERSP" if view == "persp" else "ORTHO"
        camera.data.lens = 50
        camera.data.sensor_width = 36
        distance = radius / OCCUPANCY / math.sin(math.atan(18 / 50)) if view == "persp" else radius * 3
        matrix = camera.rotation_euler.to_matrix().to_4x4()
        matrix.translation = center + direction * distance
        camera.matrix_world = matrix
        basis = camera.rotation_euler.to_matrix().transposed()
        projected = transform(points - np.array(center, dtype=np.float32), basis, rows=2)
        camera.data.ortho_scale = float(np.abs(projected).max()) * (2 / OCCUPANCY)
        camera.data.ortho_scale = max(camera.data.ortho_scale, 0.02)
        camera.data.clip_start = max(radius * 0.001, 0.0001)
        camera.data.clip_end = distance + radius * 4
    # EEVEE Z is axial depth except for panoramic cameras, which use radial depth.
    view = transform(points, camera.matrix_world.inverted())
    if camera.data.type == "PANO":
        depths = np.sqrt(view[:, 0] * view[:, 0] + view[:, 1] * view[:, 1] + view[:, 2] * view[:, 2])
    else:
        depths = -view[:, 2]
    near, far = float(depths.min()), float(depths.max())
    return near, max(far, near + 0.001)


def wire_material():
    material = bpy.data.materials.new("Agent wire")
    nodes = material.node_tree.nodes
    nodes.clear()
    output = nodes.new("ShaderNodeOutputMaterial")
    emission = nodes.new("ShaderNodeEmission")
    wire = nodes.new("ShaderNodeWireframe")
    wire.use_pixel_size = True
    wire.inputs["Size"].default_value = 1.0
    material.node_tree.links.new(wire.outputs[0], emission.inputs["Color"])
    material.node_tree.links.new(emission.outputs[0], output.inputs["Surface"])
    return material


def render_passes(scene, size, near, far, wire=None):
    """Unbordered RGB8 tiles shared by observation and numeric comparison."""
    buffers = agent._native["render"](scene.name)

    def pixels(name, channels):
        return np.frombuffer(buffers[name], dtype=np.float32).reshape(size, size, channels)[::-1]

    combined = pixels("Combined", 4)
    color = srgb(combined[:, :, :3] + (1 - combined[:, :, 3:4]) * 0.035)
    depth = pixels("Depth", 1)
    mask = (depth < scene.camera.data.clip_end) & (combined[:, :, 3:4] >= 0.5)
    images = {"color": bytes_rgb(color), "silhouette": np.repeat(mask.astype(np.uint8) * 255, 3, axis=2),
              "normal": bytes_rgb(np.where(mask, pixels("Normal", 3) * 0.5 + 0.5, 0)),
              "depth": bytes_rgb(np.repeat(np.where(mask, 1 - (depth - near) / (far - near), 0), 3, axis=2))}
    if wire:
        scene.view_layers[0].material_override = wire
        wire_buffers = agent._native["render"](scene.name)
        edge = np.frombuffer(wire_buffers["Combined"], dtype=np.float32).reshape(size, size, 4)[::-1, :, :1]
        images["wire"] = bytes_rgb(color * (1 - np.clip(edge, 0, 1) * 0.9))
        scene.view_layers[0].material_override = None
    return images


def view_axes(view, camera):
    """Image axes whose mirror is a world-axis mirror about the framing center.

    Only the orthographic presets qualify: they project the framing center onto the
    image center, so flipping an axis of the buffer mirrors world space exactly.
    """
    if view == "camera" or camera.data.type != "ORTHO":
        return {}
    basis = camera.matrix_world.to_3x3().normalized()
    axes = {}
    for image_axis, vector in ((1, basis.col[0]), (0, basis.col[1])):
        for index, name in enumerate("xyz"):
            if abs(abs(vector[index]) - 1) < 1e-6:
                axes[name] = image_axis
    return axes


def render_budget(views, size, samples):
    """One render per view at feedback size, as raw buffers rather than encoded tiles.

    Feedback compares consecutive states pixel by pixel and crops regions out of them,
    so it needs the arrays; counts come from the same converted geometry as `framing`.
    The sample count is the budget's, not observation's: a delta is read for where it
    moved, not for its finish.
    """
    source = bpy.context.scene
    if "camera" in views and source.camera is None:
        raise ValueError("The camera view requires scene.camera")
    tiles = {}
    with isolated_data():
        scene, points, center, radius, framing = render_scene(source, size, None)
        scene.eevee.taa_render_samples = samples
        counts = {"objects": 0, "verts": 0, "faces": 0}
        for obj in scene.collection.objects:
            if obj.type in {"LIGHT", "CAMERA"}:
                continue
            counts["objects"] += 1
            counts["verts"] += len(getattr(obj.data, "vertices", ()))
            counts["faces"] += len(getattr(obj.data, "polygons", ()))
        for view in views:
            near, far = aim(scene, source, view, points, center, radius)
            images = render_passes(scene, size, near, far)
            tiles[view] = {"color": images["color"],
                           "silhouette": images["silhouette"][:, :, 0] != 0,
                           "axes": view_axes(view, scene.camera)}
    return tiles, framing, counts


def observe(views=("front", "persp"), passes=("color",), size=512, ref=None,
            layout="sheet", frame=None, overlay=False, out=None, inline=False):
    views, passes = names(views, VIEWS), names(passes, PASSES)
    if size not in (512, 768, 1024):
        raise ValueError("size must be 512, 768 or 1024")
    if layout not in ("sheet", "separate"):
        raise ValueError("layout must be sheet or separate")
    if overlay and not ref:
        raise ValueError("--overlay requires --ref")
    if inline and out:
        raise ValueError("--inline and --out are mutually exclusive")
    if inline and layout == "separate":
        raise ValueError("--inline requires sheet layout: only one image crosses the agent boundary")
    source = bpy.context.scene
    if "camera" in views and source.camera is None:
        raise ValueError("The camera view requires scene.camera")
    tiles = []
    with isolated_data():
        scene, points, center, radius, framing = render_scene(source, size, frame)
        wire = wire_material() if "wire" in passes else None
        for view in views:
            near, far = aim(scene, source, view, points, center, radius)
            images = render_passes(scene, size, near, far, wire)
            tiles.extend(images[pass_name] for pass_name in passes)
        reference = None
        if ref:
            image = bpy.data.images.load(str(Path(ref).resolve()), check_existing=False)
            w, h = image.size
            if not w or not h:
                raise ValueError("Reference image has no pixels")
            rgba = np.empty(w * h * 4, dtype=np.float32)
            image.pixels.foreach_get(rgba)
            rgba = rgba.reshape(h, w, 4)[::-1]
            reference = rgba[:, :, :3] * rgba[:, :, 3:4] + (1 - rgba[:, :, 3:4]) * (32 / 255)
            if overlay:
                scale = min(size / w, size / h)
                rw, rh = max(1, round(w * scale)), max(1, round(h * scale))
                resized = resize(reference, rw, rh)
                x, y = (size - rw) // 2, (size - rh) // 2
                tiles[0][y:y + rh, x:x + rw] = bytes_rgb(
                    tiles[0][y:y + rh, x:x + rw] / 510 + resized / 2)
                reference = None
            else:
                reference = bytes_rgb(resize(reference, max(1, round(w * size / h)), size))

    stride = size + BORDER * 2
    extra = reference.shape[1] + BORDER * 2 if reference is not None else 0
    if layout == "sheet":
        sheet = np.full((len(views) * stride, len(passes) * stride + extra, 3), 32, np.uint8)
        for i, tile in enumerate(tiles):
            y, x = (i // len(passes)) * stride + BORDER, (i % len(passes)) * stride + BORDER
            sheet[y:y + size, x:x + size] = tile
        if reference is not None:
            sheet[BORDER:BORDER + size, len(passes) * stride + BORDER:-BORDER] = reference
        outputs = [sheet]
    else:
        outputs = []
        for i, tile in enumerate(tiles):
            image = np.full((stride, stride + (extra if i == 0 else 0), 3), 32, np.uint8)
            image[BORDER:BORDER + size, BORDER:BORDER + size] = tile
            if i == 0 and reference is not None:
                image[BORDER:BORDER + size, stride + BORDER:-BORDER] = reference
            outputs.append(image)
    result = {"ok": True, "views": views, "passes": passes, "framing": framing,
              "size": [outputs[0].shape[1], outputs[0].shape[0]]}
    directory = None
    if not inline:
        directory = (Path.cwd() / ".blender-cli" / "observe" if agent._session else
                     Path(tempfile.mkdtemp(prefix="blender-cli-observe-"))) if not out else Path(out).resolve()
        if not out or layout == "separate":
            directory.mkdir(parents=True, exist_ok=True)
    records = []
    for i, image in enumerate(outputs):
        encoded = png(image)
        record = {"size": [image.shape[1], image.shape[0]]}
        if inline:
            record["base64"] = base64.b64encode(encoded).decode("ascii")
        else:
            path = directory if out and layout == "sheet" else directory / (hashlib.sha256(encoded).hexdigest() +
                    (f"-{i}" if layout == "separate" else "") + ".png")
            path.write_bytes(encoded)
            record["image"] = str(path)
        if layout == "separate":
            record.update(view=views[i // len(passes)], **{"pass": passes[i % len(passes)]})
        records.append(record)
    result.update(records[0])
    if layout == "separate":
        result["images"] = records
    return result
