# SPDX-FileCopyrightText: 2026 blender-cli Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Display-space classic CV and numeric fitting on Phase 3 render tiles."""

from pathlib import Path
import tempfile

import bpy
import numpy as np

from agent_observe import (VIEWS, aim, bytes_rgb, isolated_data, names, png,
                           render_passes, render_scene, resize, srgb)

METRICS = ("iou", "chamfer", "ssim", "hist")
BACKGROUND = float(bytes_rgb(srgb(np.array(0.035)))) / 255


def morphology(mask, dilate):
    padded = np.pad(mask, 1, mode="edge")
    result = mask.copy()
    for y in range(3):
        for x in range(3):
            other = padded[y:y + mask.shape[0], x:x + mask.shape[1]]
            if dilate:
                result |= other
            else:
                result &= other
    return result


def reference(ref, size, policy):
    """Load with Blender's codecs; caller owns the isolated-data lifetime."""
    image = bpy.data.images.load(str(Path(ref).resolve()), check_existing=False)
    w, h = image.size
    if not w or not h:
        raise ValueError("Reference image has no pixels")
    rgba = np.empty(w * h * 4, dtype=np.float32)
    image.pixels.foreach_get(rgba)
    rgba = rgba.reshape(h, w, 4)[::-1].copy()
    alpha = np.clip(rgba[:, :, 3:4], 0, 1)
    # ImBuf byte pixels are straight display RGB. Float pixels are premultiplied linear.
    if image.is_float:
        rgba[:, :, :3] = srgb(np.divide(rgba[:, :, :3], alpha,
                                       out=np.zeros_like(rgba[:, :, :3]), where=alpha > 0))
    rgba = np.clip(rgba, 0, 1)
    if not np.isfinite(rgba).all():
        raise ValueError("Reference contains non-finite pixels")
    # Recognize the documented single-tile observe frame, not arbitrary image borders.
    if w == h and w in (516, 772, 1028):
        border = np.concatenate((rgba[:2].reshape(-1, 4), rgba[-2:].reshape(-1, 4),
                                 rgba[:, :2].reshape(-1, 4), rgba[:, -2:].reshape(-1, 4)))
        if np.all(bytes_rgb(border[:, :3]) == 32) and np.all(border[:, 3] == 1):
            rgba = rgba[2:-2, 2:-2]
            h, w = rgba.shape[:2]
    scale = min(size / w, size / h)
    rw, rh = max(1, round(w * scale)), max(1, round(h * scale))
    # Resample premultiplied display RGB so transparent RGB does not bleed into edges.
    premul = rgba.copy()
    premul[:, :, :3] *= premul[:, :, 3:4]
    fitted = resize(premul, rw, rh)
    a = fitted[:, :, 3]
    rgb = np.divide(fitted[:, :, :3], a[:, :, None], out=np.zeros_like(fitted[:, :, :3]),
                    where=a[:, :, None] > 0)
    if policy == "auto":
        border = np.concatenate((rgb[0], rgb[-1], rgb[:, 0], rgb[:, -1]))
        background = np.median(border, axis=0)
        distances = np.linalg.norm(border - background, axis=1)
        median = np.median(distances)
        threshold = max(0.08, float(median + 6 * np.median(np.abs(distances - median))))
        mask = np.linalg.norm(rgb - background, axis=2) > threshold
        # Meaningful alpha is a stronger background cue than color estimation.
        if np.any(a < 1 - 1 / 255):
            mask = a >= 0.5
        mask = morphology(morphology(mask, False), True)  # 3x3 opening.
        mask = morphology(morphology(mask, True), False)  # 3x3 closing.
    else:
        # `channels` describes storage (usually RGBA even for RGB files); depth uses
        # ImBuf's source color mode and distinguishes an actual alpha channel.
        has_alpha = image.depth in (16, 32, 64, 128)
        mask = a >= 0.5 if has_alpha else (rgb @ np.array((0.2126, 0.7152, 0.0722))) >= 0.5
    tile = np.full((size, size, 3), BACKGROUND, dtype=np.float64)
    silhouette = np.zeros((size, size), dtype=bool)
    x, y = (size - rw) // 2, (size - rh) // 2
    tile[y:y + rh, x:x + rw] = rgb * a[:, :, None] + BACKGROUND * (1 - a[:, :, None])
    silhouette[y:y + rh, x:x + rw] = mask
    return tile, silhouette


def boundary(mask):
    padded = np.pad(mask, 1, constant_values=False)
    return mask & ~(padded[:-2, 1:-1] & padded[2:, 1:-1] & padded[1:-1, :-2] & padded[1:-1, 2:])


def distance_transform(edge):
    """Exact separable city-block distance, four vectorized minimum-prefix scans."""
    distance = np.where(edge, 0, sum(edge.shape)).astype(np.int32)
    for axis in (0, 1):
        coordinates = np.arange(edge.shape[axis], dtype=np.int32)
        coordinates = coordinates[:, None] if axis == 0 else coordinates[None, :]
        distance = np.minimum.accumulate(distance - coordinates, axis=axis) + coordinates
        flipped = np.flip(distance, axis=axis)
        distance = np.flip(np.minimum.accumulate(flipped - coordinates, axis=axis) + coordinates, axis=axis)
    return distance


def box_mean(values):
    padded = np.pad(values, 3, mode="reflect")
    integral = np.pad(padded, ((1, 0), (1, 0))).cumsum(0).cumsum(1)
    return (integral[7:, 7:] - integral[:-7, 7:] - integral[7:, :-7] + integral[:-7, :-7]) / 49


def measure(reference_rgb, reference_mask, render_rgb, render_mask, metrics):
    result = {}
    if "iou" in metrics:
        union = np.count_nonzero(reference_mask | render_mask)
        result["iou"] = float(np.count_nonzero(reference_mask & render_mask) / union) if union else 1.0
    if "chamfer" in metrics:
        a, b = boundary(reference_mask), boundary(render_mask)
        if not a.any() or not b.any():
            result["chamfer"] = float(sum(a.shape) - 2) if a.any() != b.any() else 0.0
        else:
            result["chamfer"] = float((distance_transform(a)[b].mean() + distance_transform(b)[a].mean()) / 2)
    if "ssim" in metrics:
        weights = np.array((0.2126, 0.7152, 0.0722))
        x = np.where(reference_mask[:, :, None], reference_rgb, BACKGROUND) @ weights
        y = np.where(render_mask[:, :, None], render_rgb, BACKGROUND) @ weights
        mx, my = box_mean(x), box_mean(y)
        vx, vy = np.maximum(box_mean(x * x) - mx * mx, 0), np.maximum(box_mean(y * y) - my * my, 0)
        covariance = box_mean(x * y) - mx * my
        result["ssim"] = float(np.clip(((2 * mx * my + 0.01 ** 2) * (2 * covariance + 0.03 ** 2) /
                                      ((mx * mx + my * my + 0.01 ** 2) * (vx + vy + 0.03 ** 2))).mean(), -1, 1))
    if "hist" in metrics:
        def histogram(rgb, mask):
            bins = np.minimum((np.clip(rgb[mask], 0, 1) * 16).astype(np.int32), 15)
            counts = np.bincount(bins @ np.array((256, 16, 1)), minlength=4096)
            return counts / max(1, counts.sum())
        if not reference_mask.any() and not render_mask.any():
            result["hist"] = 0.0
        else:
            result["hist"] = float(np.clip(1 - np.minimum(histogram(reference_rgb, reference_mask),
                                                         histogram(render_rgb, render_mask)).sum(), 0, 1))
    return result


def compare(ref, view, metrics=("iou",), mask="auto", size=512, frame=None, debug=False):
    metrics = names(metrics, METRICS)
    if view not in VIEWS:
        raise ValueError(f"Unknown view: {view}")
    if size not in (512, 768, 1024):
        raise ValueError("size must be 512, 768 or 1024")
    if mask not in ("auto", "none"):
        raise ValueError("mask must be auto or none")
    source = bpy.context.scene
    if view == "camera" and source.camera is None:
        raise ValueError("The camera view requires scene.camera")
    with isolated_data():
        rgb, silhouette = reference(ref, size, mask)
        scene, points, center, radius = render_scene(source, size, frame)
        near, far = aim(scene, source, view, points, center, radius)
        images = render_passes(scene, size, near, far)
        result = {"view": view, **measure(rgb, silhouette, images["color"] / 255,
                                         images["silhouette"][:, :, 0] != 0, metrics)}
    if debug:
        directory = Path(tempfile.mkdtemp(prefix="blender-cli-compare-")) if debug is True else Path(debug).resolve()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "reference-silhouette.png"
        path.write_bytes(png(np.repeat(silhouette[:, :, None].astype(np.uint8) * 255, 3, axis=2)))
        result["debug"] = {"reference_silhouette": str(path)}
    return result
