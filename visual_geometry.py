"""The same oriented-original transformation is used for previews and every movie frame."""

import math
from pathlib import Path
from PIL import Image, ImageOps
from file_io import atomic_write_json
from image_files import inspect_image
from production_state import ProductionError, sha


def box(value):
    if (
        not isinstance(value, list)
        or len(value) != 4
        or any(type(v) not in (int, float) or not math.isfinite(v) for v in value)
    ):
        raise ProductionError("Invalid normalized bounding box.")
    x, y, r, b = value
    if not 0 <= x < r <= 1 or not 0 <= y < b <= 1:
        raise ProductionError("Bounding box outside original.")
    return [float(v) for v in value]


def geometry(original, composition, output=(1080, 1920), max_zoom=10):
    measured = inspect_image(
        Path(original).read_bytes(), minimum_width=1, minimum_height=1
    )
    iw, ih = measured["width"], measured["height"]
    ow, oh = output
    z = composition["zoom_percent"]
    if type(z) not in (int, float) or not math.isfinite(z) or not 0 <= z <= max_zoom:
        raise ProductionError("Zoom outside configured bounds.")
    mode = composition["mode"]
    focus = composition["focus_point"]
    if len(focus) != 2 or any(
        type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1
        for v in focus
    ):
        raise ProductionError("Focus outside original.")
    keep = composition["must_keep"]
    for item in keep:
        box(item["box"])
    if mode == "crop":
        x, y, r, b = box(composition["initial_crop"])
        left, top, right, bottom = x * iw, y * ih, r * iw, b * ih
        # Fit the exact output aspect inside the requested region; never stretch.
        cw = min(right - left, (bottom - top) * ow / oh)
        ch = cw * oh / ow
        cx = min(max(focus[0] * iw, left + cw / 2), right - cw / 2)
        cy = min(max(focus[1] * ih, top + ch / 2), bottom - ch / 2)
        start = [cx - cw / 2, cy - ch / 2, cx + cw / 2, cy + ch / 2]
        ew, eh = cw / (1 + z / 100), ch / (1 + z / 100)
        ex = min(max(focus[0] * iw, start[0] + ew / 2), start[2] - ew / 2)
        ey = min(max(focus[1] * ih, start[1] + eh / 2), start[3] - eh / 2)
        end = [ex - ew / 2, ey - eh / 2, ex + ew / 2, ey + eh / 2]
        if ew + 1e-6 < ow or eh + 1e-6 < oh:
            raise ProductionError(
                f"Insufficient pixels at maximum zoom: {ew:.1f}x{eh:.1f}; need {ow}x{oh}."
            )
        # Each edge interpolates linearly. All constraints are linear inequalities;
        # checking both endpoints proves the entire path, not only three samples.
        for bounds in (start, end):
            if (
                bounds[0] < -1e-6
                or bounds[1] < -1e-6
                or bounds[2] > iw + 1e-6
                or bounds[3] > ih + 1e-6
            ):
                raise ProductionError("Motion exits original.")
            for item in keep:
                k = box(item["box"])
                if not (
                    bounds[0] <= k[0] * iw + 1e-6
                    and bounds[1] <= k[1] * ih + 1e-6
                    and bounds[2] >= k[2] * iw - 1e-6
                    and bounds[3] >= k[3] * ih - 1e-6
                ):
                    raise ProductionError("Motion crops must_keep: " + item["label"])
        result = {"start_crop": start, "end_crop": end}
    elif mode == "background":
        # Entire foreground fits even at maximum zoom. No synthesized detail/upscale.
        final_scale = min(ow / iw, oh / ih)
        if final_scale > 1 + 1e-9:
            raise ProductionError(
                "Insufficient original resolution for whole-image foreground."
            )
        result = {
            "start_scale": final_scale / (1 + z / 100),
            "end_scale": final_scale,
            "background_rgb": [24, 24, 24],
        }
    else:
        raise ProductionError("Unknown fit mode.")
    return {
        **result,
        "schema_version": 1,
        "mode": mode,
        "output": [ow, oh],
        "original_size": [iw, ih],
        "original_sha256": measured["file_sha256"],
        "composition": composition,
        "technical_checks": {
            "passed": True,
            "entire_linear_nested_path": True,
            "maximum_zoom_resolution": True,
            "exif_orientation": measured["orientation"],
            "no_upscale": True,
        },
    }


def oriented(path):
    with Image.open(path) as source:
        image = ImageOps.exif_transpose(source).convert("RGBA")
        background = Image.new("RGB", image.size, "white")
        background.paste(image, mask=image.getchannel("A"))
        return background


def frame(image, motion, position):
    if not 0 <= position <= 1:
        raise ProductionError("Invalid motion position.")
    ow, oh = motion["output"]
    if motion["mode"] == "crop":
        bounds = [
            a + (b - a) * position
            for a, b in zip(motion["start_crop"], motion["end_crop"])
        ]
        return image.transform(
            (ow, oh), Image.Transform.EXTENT, bounds, Image.Resampling.BICUBIC
        )
    scale = (
        motion["start_scale"] + (motion["end_scale"] - motion["start_scale"]) * position
    )
    size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
    resized = image.resize(size, Image.Resampling.BICUBIC)
    canvas = Image.new("RGB", (ow, oh), tuple(motion["background_rgb"]))
    canvas.paste(resized, ((ow - size[0]) // 2, (oh - size[1]) // 2))
    return canvas


def prepare(original, composition, directory, output=(1080, 1920), max_zoom=10):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    motion = geometry(original, composition, output, max_zoom)
    image = oriented(original)
    previews = []
    for name, t in [("start", 0), ("middle", 0.5), ("end", 1)]:
        target = directory / f"{name}.png"
        frame(image, motion, t).save(target)
        previews.append({"position": t, "path": str(target), "sha256": sha(target)})
    motion["previews"] = previews
    atomic_write_json(directory / "motion.json", motion)
    return motion
