"""Strict adapters for existing script/audio contracts; immutable file snapshots."""

import math
import shutil
from pathlib import Path
from audio_files import inspect_wav
from file_io import atomic_write_json
from production_state import ProductionError, UpstreamBlocked, digest, inside, read, sha
from script_workflow import approved_script
from visual_materials import verify_asset
from visual_shots import (
    entry_shots,
    validate_shot_ranges,
    asset_dependencies,
    approved_assets,
)


def snapshot_inputs(source, destination, include_audio=True):
    source = Path(source).resolve()
    destination = Path(destination).resolve()
    if source == destination:
        raise ProductionError("Snapshot must be separate from source.")
    # Сохраняем также доказательство одобрения и предыдущие завершённые версии.
    names = [
        "script.json",
        "script_state.json",
        "verification.json",
        "research.json",
        "research.md",
        "research_origin.json",
        "run.json",
    ]
    names += sorted(
        p.name
        for pattern in ("script_[0-9]*.json", "verification_[0-9]*.json")
        for p in source.glob(pattern)
    )
    optional = (
        ["audio_manifest.json", "visual_materials.json", "attribution.json"]
        if include_audio
        else []
    )
    names = [name for name in names + optional if (source / name).is_file()]
    dependencies = set(names)
    if "audio_manifest.json" in names:
        audio = read(source / "audio_manifest.json")
        if audio.get("audio_path"):
            dependencies.add(audio["audio_path"])
    if "visual_materials.json" in names:
        visual_input = read(source / "visual_materials.json")
        if visual_input.get("status") == "approved":
            checked_visuals(approved_script(source), visual_input, source)
            # Carry the revocation authority too: a lagging public manifest alone
            # must not become approved again merely by entering a snapshot.
            if (source / "visual_materials_state.json").exists():
                dependencies.add("visual_materials_state.json")
        for asset in approved_assets(visual_input):
            dependencies.update(asset_dependencies(asset))
    # Capture all dependencies before copying and verify both the source and copies after.
    hashes = {name: sha(inside(source, name)) for name in dependencies}
    if (destination / "input_snapshot.json").exists():
        saved = read(destination / "input_snapshot.json")
        if saved["source"] != str(source) or saved["files_sha256"] != hashes:
            raise ProductionError(
                "Snapshot already exists with different source/hashes; choose a new run directory."
            )
        if any(sha(inside(destination, n)) != v for n, v in hashes.items()):
            raise ProductionError("Immutable snapshot changed.")
        return destination
    destination.mkdir(parents=True, exist_ok=True)
    for name in dependencies:
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(inside(source, name), target)
    if any(
        sha(inside(source, n)) != v or sha(inside(destination, n)) != v
        for n, v in hashes.items()
    ):
        raise ProductionError(
            "Source changed while snapshotting; snapshot is not accepted."
        )
    atomic_write_json(
        destination / "input_snapshot.json",
        {
            "schema_version": 1,
            "source": str(source),
            "files_sha256": hashes,
            "stable_during_copy": True,
        },
    )
    return destination


def _checked_audio(script, audio, root, allow_test=False):
    expected_mode = "test" if allow_test else "real"
    if (
        audio.get("mode") != expected_mode
        or audio.get("status") != "completed"
        or audio.get("text_verified") is not True
    ):
        raise UpstreamBlocked(
            "Audio is not accepted: "
            + str(audio.get("status"))
            + "; "
            + str(audio.get("reason"))
        )
    if not allow_test and audio.get("audio_kind") != "ai_generated_speech":
        raise UpstreamBlocked("Test/unknown audio cannot enter a real release.")
    if audio.get("script_version_id") != script["script_version_id"]:
        raise UpstreamBlocked("Audio belongs to another script version.")
    if (
        audio.get("timing_status") != "aligned_to_deepgram_words"
        or audio.get("review", {}).get("decision") != "Можно использовать"
    ):
        raise UpstreamBlocked("Audio text/word/fragment approval missing.")
    path = inside(root, audio["audio_path"])
    measured = inspect_wav(path.read_bytes())
    if audio.get("audio_sha256") != measured["audio_sha256"]:
        raise UpstreamBlocked("Audio SHA-256 mismatch.")
    if not allow_test:
        from voice_alignment import text_authority

        authority = text_authority(
            script,
            (
                audio.get("transcript", ""),
                audio.get("openai_transcript", {}).get("text", ""),
            ),
            audio.get("review"),
        )
        if not authority["approved"]:
            raise UpstreamBlocked(
                "OpenAI has not confirmed the approved script; Deepgram cannot authorize text."
            )
        if (
            not authority["normalized_full_match"]
            and audio.get("text_confirmation", {}).get("approval_input_sha256")
            != authority["approval_input_sha256"]
        ):
            raise UpstreamBlocked(
                "Semantic OpenAI confirmation is not bound to this transcript/script snapshot; reassess saved evidence only."
            )
        from voice_audio_checks import current_audio_checks

        if not current_audio_checks(path, audio.get("technical_checks"))[1]["passed"]:
            raise UpstreamBlocked("Current WAV failed independent technical checks.")
    duration = measured["duration_seconds"]
    if abs(duration - audio.get("total_duration_seconds", -1)) > 1e-6:
        raise UpstreamBlocked("Audio duration differs from measured WAV.")
    words = audio.get("words")
    fragments = audio.get("fragments")
    if (
        not isinstance(words, list)
        or not words
        or not isinstance(fragments, list)
        or len(fragments) != len(script["fragments"])
    ):
        raise UpstreamBlocked("Word/fragment bindings missing.")
    previous = 0
    for number, word in enumerate(words, 1):
        start, end = word["start"], word["end"]
        if (
            word["word_id"] != number
            or not word.get("word")
            or any(
                type(v) not in (int, float) or not math.isfinite(v)
                for v in (start, end)
            )
            or not previous <= start < end <= duration
        ):
            raise UpstreamBlocked("Invalid source word timing/order.")
        previous = end
    previous_word = 0
    previous_display = 0
    for index, (fragment, text) in enumerate(zip(fragments, script["fragments"]), 1):
        first, last = fragment["first_word_id"], fragment["last_word_id"]
        if (
            fragment["fragment_index"] != index
            or fragment.get("text") != text["text"]
            or fragment.get("visual") != text["visual"]
        ):
            raise UpstreamBlocked("Audio fragment/script binding mismatch.")
        if (
            type(first) is not int
            or type(last) is not int
            or first != previous_word + 1
            or not first <= last <= len(words)
        ):
            raise UpstreamBlocked("Incomplete/out-of-order fragment word coverage.")
        ds, de = fragment["display_start"], fragment["display_end"]
        if (
            any(type(v) not in (int, float) or not math.isfinite(v) for v in (ds, de))
            or abs(ds - previous_display) > 1e-6
            or not ds < de <= duration
        ):
            raise UpstreamBlocked(
                "Display times must cover all audio and pauses without gaps."
            )
        if (
            fragment["speech_start"] != words[first - 1]["start"]
            or fragment["speech_end"] != words[last - 1]["end"]
            or not ds <= fragment["speech_start"] < fragment["speech_end"] <= de
        ):
            raise UpstreamBlocked(
                "Fragment seconds do not come from bound audio words."
            )
        previous_word = last
        previous_display = de
    if previous_word != len(words) or abs(previous_display - duration) > 1e-6:
        raise UpstreamBlocked("Incomplete final audio/word coverage.")
    return path, duration, words, fragments


def _checked_visuals(script, visuals, root):
    if (
        visuals.get("mode") != "real"
        or visuals.get("status") != "approved"
        or visuals.get("script_version_id") != script["script_version_id"]
    ):
        raise UpstreamBlocked("Visual materials not approved for this script version.")
    journal_path = Path(root) / "visual_materials_state.json"
    repair_jobs = read(journal_path).get("jobs", {}) if journal_path.exists() else {}
    entries = visuals.get("fragments", [])
    if len(entries) != len(script["fragments"]):
        raise UpstreamBlocked("Visual fragment count mismatch.")
    output = None
    assets = []
    for index, (entry, fragment) in enumerate(zip(entries, script["fragments"]), 1):
        if (
            entry.get("status") != "approved"
            or entry["fragment_index"] != index
            or entry["fragment_sha256"] != digest(fragment)
        ):
            raise UpstreamBlocked("Visual fragment text/visual changed.")
        repair_job = repair_jobs.get(digest({"fragment": fragment, "index": index}), {})
        repair = repair_job.get("pending_repair")
        if repair:
            if repair.get("status") != "completed":
                raise UpstreamBlocked(
                    "Visual approval revoked: unfinished repair in durable journal."
                )
            if repair.get("result_sha256") != digest(entry):
                raise UpstreamBlocked(
                    "Public visual manifest predates the successful repair."
                )
        shots = entry_shots(entry)
        if "shots" in entry:
            validate_shot_ranges(fragment, shots)
        for shot in shots:
            if shot.get("status") != "approved":
                raise UpstreamBlocked("Не все обязательные кадры фрагмента приняты.")
            asset = verify_asset(shot, root)
            if output is not None and output != asset["motion"]["output"]:
                raise UpstreamBlocked("Inconsistent visual output size.")
            output = asset["motion"]["output"]
            assets.append(
                dict(asset, fragment_index=index, shot_id=shot.get("shot_id"))
            )
    expected = [
        visuals["settings_snapshot"]["preparation"]["output"][name]
        for name in ("width", "height")
    ]
    if output != expected:
        raise UpstreamBlocked("Output dimensions differ from approved visual settings.")
    return assets, output


def checked_audio(script, audio, root, allow_test=False):
    from voice_errors import VoiceoverError

    try:
        return _checked_audio(script, audio, root, allow_test)
    except UpstreamBlocked:
        raise
    except (
        KeyError,
        TypeError,
        ValueError,
        OSError,
        VoiceoverError,
        ProductionError,
    ) as error:
        raise UpstreamBlocked(
            "Invalid/unavailable audio input: " + str(error)
        ) from None


def checked_visuals(script, visuals, root):
    try:
        return _checked_visuals(script, visuals, root)
    except UpstreamBlocked:
        raise
    except (KeyError, TypeError, ValueError, OSError, ProductionError) as error:
        raise UpstreamBlocked(
            "Invalid/unavailable visual input: " + str(error)
        ) from None


def import_manifest(source_file, destination, kind):
    """Import a ready audio/visual manifest plus dependencies, keeping previous inputs and budgets."""
    if kind not in {"audio", "visuals"}:
        raise ProductionError("Unknown input kind.")
    source_file = Path(source_file).resolve()
    source = source_file.parent
    destination = Path(destination).resolve()
    saved = read(source_file)
    original_hash = sha(source_file)
    value = read(source_file)
    script = approved_script(destination)
    if value.get("script_version_id") != script["script_version_id"]:
        raise UpstreamBlocked("Imported manifest belongs to another script version.")
    source_visual_state = None
    if kind == "visuals" and value.get("status") == "approved":
        checked_visuals(script, value, source)
        state_file = source / "visual_materials_state.json"
        if state_file.exists():
            source_visual_state = sha(state_file)
    files = []
    if kind == "audio":
        files = [value["audio_path"]]
    else:
        for asset in approved_assets(value):
            files.extend(asset_dependencies(asset))
    files = list(dict.fromkeys(files))
    hashes = {name: sha(inside(source, name)) for name in files}
    prefix = Path("imported_inputs") / kind / original_hash[:20]
    mapping = {name: str(prefix / name) for name in files}
    for name in files:
        target = destination / mapping[name]
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and sha(target) != hashes[name]:
            raise ProductionError("An immutable imported dependency changed.")
        if not target.exists():
            shutil.copy2(inside(source, name), target)
    if sha(source_file) != original_hash or any(
        sha(inside(source, n)) != h or sha(inside(destination, mapping[n])) != h
        for n, h in hashes.items()
    ):
        raise ProductionError("Input changed during import; manifest not installed.")
    if kind == "visuals" and value.get("status") == "approved":
        if (
            source_visual_state is not None
            and sha(source / "visual_materials_state.json") != source_visual_state
        ):
            raise UpstreamBlocked(
                "Visual repair state changed during import; old approval not installed."
            )
        checked_visuals(script, saved, source)
    if kind == "audio":
        value["audio_path"] = mapping[value["audio_path"]]
    else:
        for asset in approved_assets(value):
            asset["original_path"] = mapping[asset["original_path"]]
            if asset.get("source_original_path"):
                asset["source_original_path"] = mapping[asset["source_original_path"]]
            for locator in ("provenance_path", "raw_path", "svg_path"):
                if asset["source"].get(locator):
                    asset["source"][locator] = mapping[asset["source"][locator]]
            proof = asset["source"].get("rights_evidence")
            if proof:
                proof["snapshot_path"] = mapping[proof["snapshot_path"]]
            for preview in asset["motion"]["previews"]:
                preview["path"] = mapping[preview["path"]]
    history = destination / "input_history" / kind / original_hash
    history.mkdir(parents=True, exist_ok=True)
    atomic_write_json(history / "original_manifest.json", saved)
    atomic_write_json(
        history / "origin.json",
        {
            "source": str(source_file),
            "manifest_sha256": original_hash,
            "dependencies_sha256": hashes,
            "local_path_mapping": mapping,
            "stable_during_copy": True,
        },
    )
    name = "audio_manifest.json" if kind == "audio" else "visual_materials.json"
    target = destination / name
    if target.exists():
        previous_hash = sha(target)
        previous = destination / "input_history" / kind / previous_hash
        previous.mkdir(parents=True, exist_ok=True)
        if not (previous / "installed_manifest.json").exists():
            shutil.copy2(target, previous / "installed_manifest.json")
    atomic_write_json(target, value)
    return target
