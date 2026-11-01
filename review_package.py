"""Небольшой отобранный результат для ревью; полные журналы остаются в outputs."""

from collections import Counter
from pathlib import Path
import shutil

from PIL import Image
from file_io import atomic_write_json
from production_state import read, sha, ROOT, digest


def prepare_package(directory, checks):
    directory = Path(directory).resolve()
    summary = read(directory / "batch_summary.json")
    target = ROOT / "review"
    target.mkdir(exist_ok=True)
    previews = target / "selected_previews"
    if previews.exists():
        shutil.rmtree(previews)
    previews.mkdir()
    global_ids = {"cards": set(), "previews": set(), "finalists": set()}
    cost_estimate = 0
    unknown_cost_calls = 0
    call_details = []
    runtime_snapshots = []
    shots = []
    metrics = Counter()
    usage = Counter()
    models = Counter()
    operations = Counter()
    sizes = Counter()
    for topic_index, topic in enumerate(summary["topics"], 1):
        run = Path(topic["directory"])
        manifest = read(run / "visual_materials.json")
        runtime_snapshots.append(
            {
                "topic": topic["topic"],
                "settings_sha256": digest(manifest.get("settings_snapshot", {})),
                "implementation_sha256": manifest.get("settings_snapshot", {}).get(
                    "implementation_sha256", {}
                ),
            }
        )
        for file in (run / "visual_materials").rglob("*"):
            if file.is_file():
                sizes["visual_assets_bytes"] += file.stat().st_size
        state_path = run / "visual_materials_state.json"
        if state_path.exists():
            state = read(state_path)
            metrics.update(state.get("metrics", {}))
            for job in state["jobs"].values():
                for name, values in job.get("budget", {}).items():
                    metrics["parent_" + name + "_slots"] += len(values)
                    global_ids[name].update(values)
            downloaded_originals = set()
            downloaded_previews = set()
            for op in state["operations"].values():
                if op["step"] == "visual_create" and op["status"] == "completed":
                    operations["created_" + op["result"]["origin"]] += 1
                if op["step"] == "commons_get":
                    attempts = len(op.get("attempts", []))
                    operations["commons_http_attempts"] += attempts
                    operations["commons_unfinished_attempt_records"] += sum(
                        a.get("status") == "started" for a in op.get("attempts", [])
                    )
                    operations["commons_" + op["identity"]["kind"] + "_logical"] += 1
                    operations[
                        "commons_" + op["identity"]["kind"] + "_http_attempts"
                    ] += attempts
                    if op.get("status") == "completed":
                        relative = (op.get("result") or {}).get("path")
                        if relative and op["identity"]["kind"] == "finalist":
                            downloaded_originals.add(Path(relative).stem)
                        if relative and op["identity"]["kind"] == "preview":
                            downloaded_previews.add(Path(relative).stem)
            operations["downloaded_originals"] += len(downloaded_originals)
            operations["downloaded_previews"] += len(downloaded_previews)
        ledger = run / "openai_usage.json"
        if ledger.exists():
            for entry in read(ledger).get("requests", []):
                telemetry = entry.get("telemetry", {})
                operations["openai_calls"] += 1
                models[telemetry.get("model") or "unknown"] += 1
                raw = telemetry.get("usage") or {}
                model = telemetry.get("model") or ""
                call_details.append(
                    {
                        "topic": topic["topic"],
                        "step": entry.get("step") or telemetry.get("step"),
                        "model": model,
                        "status": telemetry.get("status"),
                        "request_id": telemetry.get("provider_request_id"),
                        "instructions_sha256": digest(entry.get("instructions")),
                        "request_sha256": digest(entry.get("request")),
                        "usage": raw,
                        "duration_seconds": telemetry.get("duration_seconds"),
                        "image_presentations": len(entry.get("images", [])),
                    }
                )
                cached = (raw.get("input_tokens_details") or {}).get("cached_tokens", 0)
                if model.startswith("gpt-6-luna") and raw:
                    cost_estimate += (
                        (raw.get("input_tokens", 0) - cached) * 0.1
                        + cached * 0.01
                        + raw.get("output_tokens", 0) * 0.5
                    ) / 1_000_000
                elif entry.get("estimate_usd") is not None:
                    cost_estimate += entry["estimate_usd"]
                else:
                    unknown_cost_calls += 1
                for key in ("input_tokens", "output_tokens", "total_tokens"):
                    usage[key] += raw.get(key, 0)
                usage["cached_input_tokens"] += (
                    raw.get("input_tokens_details") or {}
                ).get("cached_tokens", 0)
        for fragment in manifest["fragments"]:
            for shot in fragment.get("shots", []):
                item = {
                    k: shot.get(k)
                    for k in (
                        "shot_id",
                        "text",
                        "visual_task",
                        "role",
                        "role_reason",
                        "must_show",
                        "status",
                        "selection_mode",
                        "selection_reason",
                        "reason",
                        "rejection_reasons",
                        "search_budget_used",
                    )
                }
                item.update(
                    topic=topic["topic"],
                    fragment_index=fragment["fragment_index"],
                    parent_status=fragment["status"],
                    parent_error=(
                        fragment.get("reason")
                        if fragment["status"] in {"error", "paused"}
                        else None
                    ),
                )
                if shot.get("asset"):
                    asset = shot["asset"]
                    source = asset["source"]
                    item["source"] = {
                        k: source.get(k)
                        for k in (
                            "title",
                            "source_page",
                            "original_url",
                            "author",
                            "credit",
                            "photographer_url",
                            "provider_url",
                            "terms_url",
                            "pexels_id",
                            "license",
                            "license_url",
                            "origin",
                            "model",
                            "parameters",
                            "preparation",
                            "prompt_sha256",
                            "created_at_utc",
                            "usage",
                            "estimate_usd",
                            "estimate_status",
                            "raw_sha256",
                        )
                    }
                    item["original_sha256"] = asset["original_sha256"]
                    item["review"] = shot["review"]
                    source_preview = run / asset["motion"]["previews"][1]["path"]
                    name = f't{topic_index}_{shot["shot_id"]}.jpg'
                    output = previews / name
                    with Image.open(source_preview) as image:
                        image = image.convert("RGB")
                        image.thumbnail((270, 480))
                        image.save(output, "JPEG", quality=82)
                    item["preview"] = "selected_previews/" + name
                    sizes["selected_previews_bytes"] += output.stat().st_size
                shots.append(item)
    counts = Counter(
        s.get("selection_mode") if s["status"] == "approved" else "unresolved"
        for s in shots
    )
    code = {p.name: sha(p) for p in ROOT.glob("*.py")}
    segments_path = directory / "execution_segments.json"
    segments = read(segments_path).get("segments", []) if segments_path.exists() else []
    result = {
        "schema_version": 1,
        "run_id": directory.name,
        "previous_run": summary.get("previous_run"),
        "mode": "real",
        "status": "approved" if shots and not counts["unresolved"] else "needs_visual",
        "shots": dict(counts),
        "checks": checks,
        "code_sha256": code,
        "inputs": [
            {k: t[k] for k in ("topic", "script_sha256", "script_version_id")}
            for t in summary["topics"]
        ],
        "api_calls": call_details,
        "runtime_snapshots": runtime_snapshots,
        "counters": dict(operations | metrics),
        "global_unique_candidates": {k: len(v) for k, v in global_ids.items()},
        "http_attempts_note": "Commons attempts включают резервирование до отправки. Незавершённые записи после прерывания не доказывают, что сервер получил запрос; они не исключены из лимитов.",
        "counter_scope": "parent_*_slots суммирует отдельные бюджеты фрагментов; global_unique_candidates удаляет повторы между ними; HTTP-попытки считаются отдельно.",
        "usage": dict(usage),
        "models": dict(models),
        "file_sizes": dict(sizes),
        "timings_seconds": {k: v for k, v in metrics.items() if k.endswith("_seconds")}
        | {
            "visual_pipeline": (
                sum(s["seconds"] for s in segments)
                if segments
                else sum(t["duration_seconds"] for t in summary["topics"])
            ),
            "packaging": None,
            "transfer": None,
        },
        "execution_segments": segments,
        "packaging_timing_note": "Измеряются финальным builder; квитанция outputs/review_build_receipt.json после единственной сборки.",
        "estimate_usd": cost_estimate,
        "unknown_cost_calls": unknown_cost_calls,
        "estimate_status": (
            "estimated_not_billed"
            if not unknown_cost_calls
            else "partial_estimate_not_billed"
        ),
        "pricing_date": "2026-11-01",
        "pricing_sources": [
            "https://developers.openai.com/api/docs/models/gpt-6-luna",
            "https://developers.openai.com/api/docs/models/gpt-image-2.5-flare",
        ],
        "service_error": summary.get("service_error"),
        "cloud_upload_confirmed": False,
    }
    atomic_write_json(target / "summary.json", result)
    atomic_write_json(
        target / "visuals.json",
        {"schema_version": 1, "run_id": directory.name, "shots": shots},
    )
    (target / "changes.txt").write_text(
        "Общий бюджет 100/20/3 на фрагмент, до двух раундов. Пакетная оценка настоящих превью <=768px; оригиналы только финалистов. Общий пул темы, отказ по смыслу только для пары shot/candidate. Роли изображения, создание только explanatory_required после not_found_within_budget. Компактный режим без HTML/Costs API. Все исходники в ZIP; полные журналы только локально. Качество голоса и монтаж не проверялись.\n",
        encoding="utf-8",
    )
    return result


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    prepare_package(args.directory, {"status": "see test result in summary"})
