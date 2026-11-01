"""Compare sampled candidate pixels against the exact expected scene and burned ASS."""

from pathlib import Path
from PIL import Image, ImageChops, ImageStat
from media_tools import run_command
from subtitles import font_filter
from visual_geometry import frame, oriented
from production_state import inside


def check_schedule(root, assets, scenes, samples, size, media, journal, *, fps=None):
    from visual_sequence import validate_sequence

    validate_sequence(assets, scenes)
    root = Path(root)
    if fps is None:
        fps = next(
            (round(s["frame_number"] / s["time"]) for s in samples if s["time"] > 0), 30
        )
    if (
        type(fps) is not int
        or fps <= 0
        or any(abs(s["time"] - s["frame_number"] / fps) > 1e-6 for s in samples)
    ):
        raise ValueError("Reference and candidate must use the same frame clock.")
    directory = root / "schedule_checks"
    directory.mkdir(exist_ok=True)
    images = [oriented(inside(root, a["original_path"])) for a in assets]
    measurements = []

    def expected(index, n):
        s = scenes[index]
        t = max(
            0,
            min(
                1,
                (n - s["start_frame"]) / max(1, s["end_frame"] - s["start_frame"] - 1),
            ),
        )
        return frame(images[index], assets[index]["motion"], t)

    for sample in samples:
        n = sample["frame_number"]
        index = next(
            i for i, s in enumerate(scenes) if s["start_frame"] <= n < s["end_frame"]
        )
        image = expected(index, n)
        for boundary in (index, index + 1):
            if not 0 < boundary < len(scenes):
                continue
            s = scenes[boundary]
            overlap = s["dissolve_frames"]
            left = s["start_frame"] - overlap // 2
            if overlap and left <= n < left + overlap:
                image = Image.blend(
                    expected(boundary - 1, n),
                    expected(boundary, n),
                    (n - left + 0.5) / overlap,
                )
                break
        raw = directory / f"{n}_raw.png"
        reference = directory / f"{n}_expected.png"
        image.save(raw)
        # A single PNG defaults to 25 fps. Use the candidate's integer frame
        # clock, including its native time base; fractional seconds can floor
        # an exact ASS boundary (3.600000 → 3.599999) on a different time base.
        run_command(
            [
                media.ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-framerate",
                str(fps),
                "-i",
                raw,
                "-vf",
                f'setpts={sample["frame_number"]},'
                + font_filter(root / "subtitles.ass", root / "font"),
                "-frames:v",
                "1",
                "-fps_mode",
                "vfr",
                "-threads",
                "1",
                reference,
            ],
            timeout=60,
            journal=journal,
        )
        with Image.open(reference) as ref, Image.open(sample["path"]) as actual:
            delta = ImageChops.difference(ref.convert("RGB"), actual.convert("RGB"))
            means = ImageStat.Stat(delta).mean
            # Whole-frame MAE alone can hide a missing subtitle. Measure the subtitle band too.
            band = delta.crop(
                (
                    round(size[0] * 0.06),
                    round(size[1] * 0.65),
                    round(size[0] * 0.94),
                    round(size[1] * 0.92),
                )
            )
            band_error = sum(ImageStat.Stat(band).mean) / 3
        measurements.append(
            {
                "frame_id": sample["frame_id"],
                "mae": sum(means) / 3,
                "subtitle_band_mae": band_error,
                "passed": sum(means) / 3 < 8 and band_error < 8,
            }
        )
    for image in images:
        image.close()
    return {
        "passed": bool(measurements) and all(m["passed"] for m in measurements),
        "samples": measurements,
        "method": "Decoded candidate compared to original-transform reference with ASS at exact frame PTS; MAE thresholds 8 RGB levels.",
    }
