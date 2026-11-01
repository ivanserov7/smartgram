"""Real FFmpeg integration with clearly synthetic fixtures, no network/API/billing."""

import argparse
import math
import socket
import struct
import time
import wave
from pathlib import Path
from unittest.mock import patch
from PIL import Image, ImageDraw
from audio_files import inspect_wav
from file_io import atomic_write_json
from production_state import config, digest, read, sha
from scriptwriter import script_identity
from visual_geometry import prepare
from editing import EditingRun
from test_production import composition, metadata
from media_tools import load_media_settings
from subtitles import layout


def smoke(directory, transition="cut"):
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    fragments = [
        {"text": "Это 4 спутника.", "visual": "TEST landscape grid"},
        {"text": "Земля видна.", "visual": "TEST portrait diagram"},
    ]
    script = {
        "fragments": fragments,
        "script_version_id": script_identity({"fragments": fragments}),
        "mode": "test",
    }
    audio_path = root / "test_sound.wav"
    with wave.open(str(audio_path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(24000)
        sound = bytearray()
        for i in range(120000):
            t = i / 24000
            amplitude = 0.12 * (0.7 + 0.3 * math.sin(2 * math.pi * 3 * t))
            sound.extend(
                struct.pack(
                    "<h",
                    round(
                        32767
                        * amplitude
                        * (
                            math.sin(2 * math.pi * 220 * t)
                            + 0.3 * math.sin(2 * math.pi * 440 * t)
                        )
                    ),
                )
            )
        out.writeframes(sound)
    words = [
        {"word_id": i, "word": word, "start": start, "end": end}
        for i, (word, start, end) in enumerate(
            [
                ("это", 0.2, 0.6),
                ("четыре", 0.7, 1.15),
                ("спутника", 1.2, 1.9),
                ("земля", 2.7, 3.1),
                ("видна", 3.2, 3.9),
            ],
            1,
        )
    ]
    timing = [
        dict(
            fragments[0],
            fragment_index=1,
            first_word_id=1,
            last_word_id=3,
            speech_start=0.2,
            speech_end=1.9,
            display_start=0,
            display_end=2.7,
        ),
        dict(
            fragments[1],
            fragment_index=2,
            first_word_id=4,
            last_word_id=5,
            speech_start=2.7,
            speech_end=3.9,
            display_start=2.7,
            display_end=5,
        ),
    ]
    audio = {
        **inspect_wav(audio_path.read_bytes()),
        "audio_path": audio_path.name,
        "total_duration_seconds": 5,
        "mode": "test",
        "audio_kind": "synthetic_test_tone",
        "status": "completed",
        "text_verified": True,
        "script_version_id": script["script_version_id"],
        "timing_status": "aligned_to_deepgram_words",
        "review": {"decision": "Можно использовать"},
        "words": words,
        "fragments": timing,
    }
    cfg = config()
    cfg["editing"]["video_render"]["transition"] = transition
    cfg["editing"]["video_render"]["transition_ms"] = (
        400 if transition == "dissolve" else 0
    )
    visuals = {
        "mode": "real",
        "status": "approved",
        "script_version_id": script["script_version_id"],
        "settings_snapshot": cfg["visuals"],
        "fragments": [],
    }
    for i, size in enumerate([(2000, 1200), (1400, 2400)], 1):
        image = Image.new("RGB", size, ("#156a87" if i == 1 else "#9a4e29"))
        draw = ImageDraw.Draw(image)
        for x in range(0, size[0], 150):
            draw.line((x, 0, x, size[1]), fill="#eeeecc", width=4)
        draw.ellipse(
            (size[0] * 0.3, size[1] * 0.3, size[0] * 0.7, size[1] * 0.7), fill="#d9d16e"
        )
        source = root / f"test_original_{i}.png"
        image.save(source)
        motion = prepare(
            source,
            composition("background" if i == 1 else "crop"),
            root / f"prepared_{i}",
        )
        for preview in motion["previews"]:
            preview["path"] = str(Path(preview["path"]).relative_to(root))
        source_metadata = metadata()
        source_metadata.update(original_mime="image/png", mime="image/png")
        visuals["fragments"].append(
            {
                "fragment_index": i,
                "fragment_sha256": digest(fragments[i - 1]),
                "status": "approved",
                "review": {"decision": "approved"},
                "asset": {
                    "candidate_id": f"test_{i}",
                    "original_path": source.name,
                    "original_sha256": sha(source),
                    "source": source_metadata,
                    "motion": motion,
                    "changes": "TEST fixture; simulated approval, no real source claim",
                },
            }
        )
    for name, value in [
        ("script", script),
        ("audio_manifest", audio),
        ("visual_materials", visuals),
    ]:
        atomic_write_json(root / f"{name}.json", value)
    calls = []

    def model(step, settings, schema, data, images):
        calls.append(step)
        if step == "subtitle_segment":
            return {
                "status": "ok",
                "reason": None,
                "cues": [
                    {
                        "cue_id": 1,
                        "first_token_id": 1,
                        "last_token_id": 3,
                        "first_audio_word_id": 1,
                        "last_audio_word_id": 3,
                        "lines": [{"first_token_id": 1, "last_token_id": 3}],
                    },
                    {
                        "cue_id": 2,
                        "first_token_id": 4,
                        "last_token_id": 5,
                        "first_audio_word_id": 4,
                        "last_audio_word_id": 5,
                        "lines": [{"first_token_id": 4, "last_token_id": 5}],
                    },
                ],
            }
        if step == "final_check":
            return {
                "decision": "approved",
                "reason": None,
                "reviewed_frame_ids": [f["frame_id"] for f in data["frames"]],
                "issues": [],
                "subtitle_appearance": None,
            }
        raise AssertionError(step)

    started = time.monotonic()
    with patch(
        "socket.socket.connect", side_effect=AssertionError("Network forbidden")
    ), patch("socket.getaddrinfo", side_effect=AssertionError("DNS forbidden")):
        result = EditingRun(root, settings=cfg["editing"], model=model).run(
            script, audio, visuals, allow_test=True
        )
        initial_calls = len(calls)
        EditingRun(
            root,
            settings=cfg["editing"],
            model=lambda *args: (_ for _ in ()).throw(
                AssertionError("Successful API repeated")
            ),
        ).run(script, audio, visuals, allow_test=True)
    quality = read(root / "quality_report.json")
    assert (
        quality["technical"]["passed"]
        and quality["test_passed"]
        and not quality["ready"]
    )
    assert not (root / "video.mp4").exists()
    from video_render import technical_check

    corrupted = root / "intentionally_truncated.mp4"
    data = (root / "candidate.mp4").read_bytes()
    corrupted.write_bytes(data[: len(data) // 2])
    render = read(root / "render_manifest.json")
    negative = technical_check(
        corrupted,
        audio_path,
        5,
        [1080, 1920],
        30,
        150,
        load_media_settings(),
        render["input_hashes"],
        root,
        [],
    )
    assert not negative["passed"]
    atomic_write_json(root / "corruption_check.json", negative)
    # Actual oversized libass output, impossible duration and reading speed must fail.
    oversized = {
        "cue_id": 999,
        "text": "Ш" * 90,
        "line_text": ["Ш" * 90],
        "start": 0,
        "end": 0.2,
    }
    overflow = layout(
        root / "negative_layout",
        [oversized],
        cfg["editing"]["subtitle_layout"],
        load_media_settings(),
        "Arial",
        [1080, 1920],
    )
    assert not overflow["passed"]
    problems = set(overflow["issues"][0]["problems"])
    assert {"font_metric_width", "duration_ms", "characters_per_second"} <= problems
    atomic_write_json(root / "overflow_check.json", overflow)
    atomic_write_json(
        root / "smoke_result.json",
        {
            "status": "test_passed",
            "duration_seconds": time.monotonic() - started,
            "transition": transition,
            "corrupted_mp4_rejected": True,
            "actual_libass_overflow_rejected": True,
            "simulated_model_calls": initial_calls,
            "real_api_calls": 0,
            "source": "synthetic fixtures, not real speech",
            "video": "test_video.mp4",
            "checks": quality["technical"]["checks"],
            "resume_repeated_api_calls": 0,
        },
    )
    print(result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--transition", choices=["cut", "dissolve"], default="cut")
    args = parser.parse_args()
    smoke(args.output.resolve(), args.transition)
