"""Local timeline rendering, two-pass loudness normalization and complete final decode."""

import json
import math
import subprocess
import tempfile
import threading
import time
from fractions import Fraction
from pathlib import Path
from PIL import Image
from media_tools import MediaError, run_command, limit_ffmpeg
from production_state import ProductionError, inside, sha
from subtitles import font_filter
from visual_sequence import validate_sequence
from visual_geometry import frame, oriented


def schedule(fragments, duration, fps, transition="cut", transition_ms=0):
    if type(fps) is not int or fps <= 0:
        raise ProductionError("Invalid fps.")
    if transition not in {"cut", "dissolve"}:
        raise ProductionError("Unknown transition.")
    if not math.isfinite(transition_ms) or transition_ms < 0:
        raise ProductionError("Invalid transition duration.")
    total = math.ceil(duration * fps - 1e-9)
    result = []
    previous = 0
    for i, fragment in enumerate(fragments):
        start = round(fragment["display_start"] * fps)
        end = total if i == len(fragments) - 1 else round(fragment["display_end"] * fps)
        if start != previous or end <= start:
            raise ProductionError("A scene has no representable frame or timeline gap.")
        overlap = 0
        if i and transition == "dissolve":
            overlap = min(
                round(transition_ms * fps / 1000),
                (end - start) // 2,
                (result[-1]["end_frame"] - result[-1]["start_frame"]) // 2,
            )
        result.append(
            {
                "fragment_index": fragment["fragment_index"],
                "shot_id": fragment.get("shot_id"),
                "start_frame": start,
                "end_frame": end,
                "display_start": fragment["display_start"],
                "display_end": fragment["display_end"],
                "dissolve_frames": overlap,
                **{
                    k: fragment[k]
                    for k in (
                        "source_identity",
                        "section_id",
                        "covered_shots",
                        "fragment_indices",
                        "covered_shot_ids",
                    )
                    if k in fragment
                },
            }
        )
        previous = end
    if previous != total:
        raise ProductionError("Scene schedule does not cover all audio.")
    return result, total


def render_video(
    directory, assets, scenes, total_frames, size, settings, media, journal
):
    directory = Path(directory)
    validate_sequence(assets, scenes)
    target = directory / "silent.mp4"
    fps = settings["fps"]
    timeout = settings["render_timeout_seconds"]
    images = [oriented(inside(directory, a["original_path"])) for a in assets]

    def at(index, n):
        s = scenes[index]
        count = s["end_frame"] - s["start_frame"]
        t = max(0, min(1, (n - s["start_frame"]) / max(1, count - 1)))
        return frame(images[index], assets[index]["motion"], t)

    command = [
        media.ffmpeg,
        "-hide_banner",
        "-y",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-s",
        f"{size[0]}x{size[1]}",
        "-framerate",
        str(fps),
        "-i",
        "pipe:0",
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "fast",
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        target,
    ]
    started = time.monotonic()
    timed_out = threading.Event()
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(
            limit_ffmpeg(command),
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=errors,
        )

        def terminate():
            timed_out.set()
            process.kill()

        timer = threading.Timer(timeout, terminate)
        timer.start()
        try:
            index = 0
            for n in range(total_frames):
                while index + 1 < len(scenes) and n >= scenes[index]["end_frame"]:
                    index += 1
                image = at(index, n)
                # Symmetric overlaps blend on the existing global clock: no shortened scenes.
                for boundary in (index, index + 1):
                    if not 0 < boundary < len(scenes):
                        continue
                    s = scenes[boundary]
                    overlap = s["dissolve_frames"]
                    b = s["start_frame"]
                    left = b - overlap // 2
                    if overlap and left <= n < left + overlap:
                        image = Image.blend(
                            at(boundary - 1, n),
                            at(boundary, n),
                            (n - left + 0.5) / overlap,
                        )
                        break
                process.stdin.write(image.tobytes())
            process.stdin.close()
            process.wait(timeout=max(0.1, timeout - (time.monotonic() - started)))
        except (BrokenPipeError, subprocess.TimeoutExpired, OSError):
            process.kill()
            process.wait()
            errors.seek(0)
            detail = errors.read().decode(errors="replace")[-1500:]
            raise ProductionError(
                "video_render failed"
                + (" (timeout)" if timed_out.is_set() else "")
                + ": "
                + detail
            ) from None
        finally:
            timer.cancel()
            if process.poll() is None:
                process.kill()
                process.wait()
            if process.stdin and not process.stdin.closed:
                try:
                    process.stdin.close()
                except BrokenPipeError:
                    pass
            for image in images:
                image.close()
        errors.seek(0)
        stderr = errors.read().decode(errors="replace")
    journal.append(
        {
            "step": "video_render",
            "arguments": [str(v) for v in command],
            "duration_seconds": time.monotonic() - started,
            "returncode": process.returncode,
            "stderr": stderr,
        }
    )
    if process.returncode or timed_out.is_set():
        raise ProductionError("video_render failed; no approval.")
    return target


def loudness(path, media, journal=None):
    _, stderr = run_command(
        [
            media.ffmpeg,
            "-hide_banner",
            "-nostats",
            "-i",
            path,
            "-map",
            "0:a:0",
            "-af",
            "loudnorm=I=-16:TP=-2:LRA=11:print_format=json",
            "-f",
            "null",
            "-",
        ],
        timeout=300,
        journal=journal,
    )
    try:
        start = stderr.rfind("{")
        raw = json.loads(stderr[start : stderr.rfind("}") + 1])
        values = {
            key: float(raw[key])
            for key in [
                "input_i",
                "input_tp",
                "input_lra",
                "input_thresh",
                "target_offset",
            ]
        }
        if not all(math.isfinite(v) for v in values.values()):
            raise ValueError
        return values
    except (ValueError, KeyError):
        raise ProductionError(
            "Loudness cannot be measured (silence/nonfinite values)."
        ) from None


def compose(directory, audio, settings, media, journal, music=None):
    directory = Path(directory)
    candidate = directory / "candidate.mp4"
    measured = loudness(audio, media, journal)
    norm = (
        f'loudnorm=I={settings["loudness_lufs"]}:TP=-2:LRA=11:measured_I={measured["input_i"]}:'
        f'measured_TP={measured["input_tp"]}:measured_LRA={measured["input_lra"]}:'
        f'measured_thresh={measured["input_thresh"]}:offset={measured["target_offset"]}:linear=true:print_format=json'
    )
    # Anchor output timestamps to the continuous sample clock. Dynamic loudnorm
    # can emit correct samples with shifted tail PTS (observed +60 ms at 24 kHz).
    # This changes neither samples, narration speed nor duration; no trimming/padding.
    norm += ",asetpts=N/SR/TB"
    arguments = [
        media.ffmpeg,
        "-hide_banner",
        "-y",
        "-i",
        directory / "silent.mp4",
        "-i",
        audio,
    ]
    if music is not None:
        raise ProductionError(
            "Music must be premixed using the prepared-rights adapter before compose."
        )
    arguments += [
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-vf",
        font_filter(directory / "subtitles.ass", directory / "font"),
        "-af",
        norm,
        "-c:v",
        "libx264",
        "-preset",
        "fast",
        "-crf",
        str(settings["crf"]),
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-ar",
        "48000",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        candidate,
    ]
    run_command(arguments, timeout=300, journal=journal)
    return candidate, measured


def technical_check(
    candidate,
    audio,
    duration,
    size,
    fps,
    total_frames,
    media,
    input_hashes,
    root,
    journal,
):
    result = {
        "passed": False,
        "checks": {},
        "errors": [],
        "duration_tolerance_seconds": 1 / fps + 1024 / 48000,
        "loudness_tolerance_lu": 0.5,
        "true_peak_limit_dbtp": -1,
        "candidate_sha256": sha(candidate),
    }
    try:
        raw, _ = run_command(
            [
                media.ffprobe,
                "-v",
                "error",
                "-count_frames",
                "-show_streams",
                "-show_format",
                "-of",
                "json",
                candidate,
            ],
            timeout=300,
            journal=journal,
        )
        probe = json.loads(raw)
        result["probe"] = probe
        videos = [s for s in probe["streams"] if s["codec_type"] == "video"]
        audios = [s for s in probe["streams"] if s["codec_type"] == "audio"]
        if len(videos) != 1 or len(audios) != 1:
            raise ProductionError("Expected exactly one video/audio stream.")
        v, a = videos[0], audios[0]
        checks = result["checks"]
        checks["codecs_size_fps"] = (
            v["codec_name"] == "h264"
            and a["codec_name"] == "aac"
            and v["pix_fmt"] == "yuv420p"
            and [v["width"], v["height"]] == list(size)
            and Fraction(v["avg_frame_rate"]) == fps
        )
        checks["all_video_frames"] = int(v["nb_read_frames"]) == total_frames
        tolerance = result["duration_tolerance_seconds"]
        checks["duration_preserves_source_audio"] = (
            abs(float(a["duration"]) - duration) <= tolerance
            and abs(float(v["duration"]) - duration) <= tolerance
            and abs(float(probe["format"]["duration"]) - duration) <= tolerance
        )
        # Decode all streams to completion. Any error, including corruption after first frames, blocks.
        run_command(
            [
                media.ffmpeg,
                "-v",
                "error",
                "-xerror",
                "-err_detect",
                "explode",
                "-i",
                candidate,
                "-map",
                "0:v:0",
                "-map",
                "0:a:0",
                "-f",
                "null",
                "-",
            ],
            timeout=300,
            journal=journal,
        )
        checks["full_decode"] = True
        volume = loudness(candidate, media, journal)
        result["loudness_after_aac"] = volume
        checks["loudness"] = (
            abs(volume["input_i"] + 16) <= result["loudness_tolerance_lu"]
            and volume["input_tp"] <= -1
        )
        checks["input_hashes"] = all(
            sha(inside(root, name)) == expected
            for name, expected in input_hashes.items()
        )
        checks["source_wav"] = (
            sha(audio) == input_hashes[str(Path(audio).relative_to(root))]
        )
        # MP4 top-level atom order, not a substring search inside media payload.
        atoms = []
        with Path(candidate).open("rb") as file:
            while header := file.read(8):
                if len(header) != 8:
                    raise ProductionError("Truncated MP4 atom.")
                length = int.from_bytes(header[:4], "big")
                kind = header[4:].decode("ascii", errors="replace")
                header_size = 8
                if length == 1:
                    length = int.from_bytes(file.read(8), "big")
                    header_size = 16
                if length == 0:
                    break
                if length < header_size:
                    raise ProductionError("Invalid MP4 atom.")
                atoms.append(kind)
                file.seek(length - header_size, 1)
        checks["faststart"] = (
            "moov" in atoms
            and "mdat" in atoms
            and atoms.index("moov") < atoms.index("mdat")
        )
        result["passed"] = all(checks.values())
        result["errors"] = [name for name, value in checks.items() if not value]
    except (ProductionError, MediaError, ValueError, KeyError, OSError) as error:
        result["errors"].append(str(error))
    return result


def sample_schedule(cues, scenes, total_frames, fps):
    # ASS actually changes on centisecond boundaries; choose representable frames inside each cue.
    frames = {}

    def add(n, reason):
        n = max(0, min(total_frames - 1, n))
        entry = frames.setdefault(
            n, {"frame_number": n, "time": n / fps, "reasons": []}
        )
        if reason not in entry["reasons"]:
            entry["reasons"].append(reason)

    for cue in cues:
        first = math.ceil(round(cue["start"] * 100) / 100 * fps - 1e-9)
        last = math.ceil(round(cue["end"] * 100) / 100 * fps - 1e-9) - 1
        if last < first:
            raise ProductionError("Cue has no actual rendered video frame.")
        for n in {first, (first + last) // 2, last}:
            add(n, "cue_" + str(cue["cue_id"]))
    for scene in scenes[1:]:
        b = scene["start_frame"]
        add(b - 1, "transition_before")
        add(b, "transition_after")
        overlap = scene["dissolve_frames"]
        if overlap:
            add(b - overlap // 2, "dissolve_start")
            add(b + (overlap - 1) // 2, "dissolve_end")
    for n, entry in frames.items():
        entry["frame_id"] = f"frame_{n:06d}"
        scene = next(s for s in scenes if s["start_frame"] <= n < s["end_frame"])
        cue = next(
            (
                c
                for c in cues
                if round(c["start"] * 100) / 100
                <= n / fps
                < round(c["end"] * 100) / 100
            ),
            None,
        )
        covered = next(
            (
                c
                for c in scene.get("covered_shots", [])
                if c["display_start"] <= n / fps < c["display_end"]
            ),
            None,
        )
        entry["fragment_index"] = (cue or {}).get(
            "fragment_index", (covered or scene).get("fragment_index")
        )
        entry["shot_id"] = scene.get("shot_id")
        entry["visual_section_id"] = scene.get("section_id")
        entry["visual_fragment_indices"] = scene.get(
            "fragment_indices", [scene["fragment_index"]]
        )
        entry["cue_id"] = cue["cue_id"] if cue else None
    return [frames[n] for n in sorted(frames)]


def extract_frames(candidate, directory, samples, media, journal):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    # Decode by absolute frame number. Extraction can never pick a source-image preview by mistake.
    expression = "+".join(f'eq(n\\,{entry["frame_number"]})' for entry in samples)
    run_command(
        [
            media.ffmpeg,
            "-hide_banner",
            "-y",
            "-i",
            candidate,
            "-vf",
            f"select={expression}",
            "-fps_mode",
            "vfr",
            "-start_number",
            "0",
            directory / "%06d.png",
        ],
        timeout=300,
        journal=journal,
    )
    paths = sorted(directory.glob("[0-9]*.png"))
    if len(paths) != len(samples):
        raise ProductionError(
            "Extracted frame coverage mismatch; no omitted samples permitted."
        )
    for entry, path in zip(samples, paths):
        entry.update(path=str(path), sha256=sha(path), candidate_sha256=sha(candidate))
    return samples
