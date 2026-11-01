"""Монтаж из явно указанных принятых входов. Поиск, TTS и исследование не вызываются."""

import argparse
from pathlib import Path
import shutil
import time

from editing import run_editing
from media_tools import MediaError
from research import ResearchError
from editing_inputs import (
    snapshot_inputs,
    import_manifest,
    checked_audio,
    checked_visuals,
)
from file_io import atomic_write_json
from production_state import ProductionError, UpstreamBlocked, read, sha, digest, config
from script_workflow import approved_script


def existing_ready_release(run, script, audio, visuals):
    """Проверенная готовая тема не пересобирается из-за локальной правки другой темы."""
    from visual_sequence import first_appearance_sequence, validate_sequence, POLICY
    from visual_shots import shot_timeline
    from subtitle_binding import binding

    path = run / "render_manifest.json"
    if not path.exists():
        return None
    value = read(path)
    if value.get("status") != "ready":
        return None
    settings = config()["editing"]
    saved = dict(value["settings_snapshot"])
    current = dict(settings)
    saved.pop("implementation_sha256", None)
    current.pop("implementation_sha256", None)
    if saved != current:
        return None
    if any(
        not (run / name).is_file() or sha(run / name) != h
        for name, h in value.get("output_hashes", {}).items()
    ):
        return None
    if not value.get("output_hashes") or any(
        sha(run / name) != h for name, h in value["input_hashes"].items()
    ):
        return None
    quality = read(run / "quality_report.json")
    plan = read(run / "montage_plan.json")
    if (
        not quality.get("ready")
        or not quality["technical"]["passed"]
        or plan["policy"] != POLICY
    ):
        return None
    if (
        plan["source_visuals_sha256"] != digest(visuals)
        or plan["audio_sha256"] != audio["audio_sha256"]
    ):
        return None
    _, _, words, fragments = checked_audio(script, audio, run)
    assets, _ = checked_visuals(script, visuals, run)
    aliases = settings["subtitle_segment"].get("equivalences", {})
    proof = binding(script, words, fragments, aliases)
    timeline = shot_timeline(
        script, visuals, fragments, words, aliases, binding_proof=proof
    )
    selected, merged = first_appearance_sequence(assets, timeline)
    validate_sequence(selected, plan["sections"])
    return value


def run_batch(inputs_file, output):
    inputs_file = Path(inputs_file).resolve()
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    specification = read(inputs_file)
    summary_path = output / "assembly_status.json"
    summary = (
        read(summary_path)
        if summary_path.exists()
        else {"schema_version": 1, "topics": [], "existing_media_only": True}
    )
    for index, entry in enumerate(specification["entries"], 1):
        started = time.monotonic()
        run = output / f"topic_{index}"
        result = {
            "name": entry["name"],
            "topic": entry["topic"],
            "directory": str(run),
            "status": "blocked",
        }
        try:
            source_paths = {
                key: Path(entry[key]).resolve()
                for key in ("script_file", "audio_manifest", "materials_file")
            }
            source_hashes = {key: sha(path) for key, path in source_paths.items()}
            marker = {
                "existing_media_only": True,
                "paths": {k: str(p) for k, p in source_paths.items()},
                "sha256": source_hashes,
            }
            if (run / "assembly_inputs.json").exists() and read(
                run / "assembly_inputs.json"
            ) != marker:
                raise ProductionError(
                    "Набор исходных входов изменился; старая сборка сохранена."
                )
            snapshot_inputs(
                source_paths["script_file"].parent, run, include_audio=False
            )
            atomic_write_json(run / "assembly_inputs.json", marker)
            if not (run / "audio_manifest.json").exists():
                import_manifest(source_paths["audio_manifest"], run, "audio")
            if not (run / "visual_materials.json").exists():
                import_manifest(source_paths["materials_file"], run, "visuals")
            script = approved_script(run)
            audio = read(run / "audio_manifest.json")
            visuals = read(run / "visual_materials.json")
            checked_audio(script, audio, run)
            checked_visuals(script, visuals, run)
            print(
                f'{index}/{len(specification["entries"])}: {entry["topic"]}: монтаж сохранённых материалов',
                flush=True,
            )
            render = existing_ready_release(run, script, audio, visuals)
            result["resumed_without_rendering"] = render is not None
            if render is None:
                render = read(run_editing(run))
            if render["status"] != "ready":
                raise ProductionError(
                    render.get("reason") or "Финальная проверка не завершена."
                )
            plan = read(run / "montage_plan.json")
            quality = read(run / "quality_report.json")
            target = output / (entry["name"] + ".mp4")
            source = run / "video.mp4"
            if not target.exists() or sha(target) != sha(source):
                shutil.copyfile(source, target)
            if sha(target) != sha(source):
                raise ProductionError("SHA готовой копии MP4 не совпадает.")
            result.update(
                status="ready",
                video=str(target),
                size_bytes=target.stat().st_size,
                sha256=sha(target),
                duration_seconds=render["duration_seconds"],
                unique_sources=plan["unique_source_count"],
                no_repeated_sections=quality["technical"]["checks"][
                    "unique_visual_sources"
                ],
                technical_checks=quality["technical"]["checks"],
                final_visual_check=quality["ready"],
                audio_sha256=audio["audio_sha256"],
                script_version_id=script["script_version_id"],
                sources=[
                    {
                        k: a.get(k)
                        for k in (
                            "title",
                            "author",
                            "source_page",
                            "license",
                            "license_url",
                            "origin",
                            "model",
                            "raw_sha256",
                        )
                    }
                    for a in read(run / "attribution.json")["images"]
                ],
            )
        except (
            ProductionError,
            MediaError,
            ResearchError,
            OSError,
            ValueError,
            KeyError,
        ) as error:
            result["reason"] = str(error)
            print(f'{entry["name"]}: остановка: {error}', flush=True)
        previous = next(
            (t for t in summary["topics"] if t["name"] == entry["name"]), {}
        )
        result["assembly_seconds"] = (
            previous.get("assembly_seconds", 0) + time.monotonic() - started
        )
        summary["topics"] = [
            t for t in summary["topics"] if t["name"] != entry["name"]
        ] + [result]
        summary["status"] = (
            "ready"
            if len(summary["topics"]) == len(specification["entries"])
            and all(t["status"] == "ready" for t in summary["topics"])
            else "incomplete"
        )
        atomic_write_json(summary_path, summary)
        print(f'{entry["name"]}: {result["status"]}', flush=True)
    return summary_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--inputs-file",
        type=Path,
        required=True,
        help="JSON с явными script_file/audio_manifest/materials_file каждой темы",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    path = run_batch(args.inputs_file, args.output)
    print(path)
    return 0 if read(path)["status"] == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
