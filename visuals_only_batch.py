"""Подготовка изображений трёх готовых сценариев, независимо от озвучки и монтажа."""

import argparse
from datetime import datetime, timezone
from pathlib import Path
import time

from editing_inputs import snapshot_inputs
from file_io import atomic_write_json
from production_report import build_report
from production_state import ProductionError, read, sha
from script_workflow import approved_script
from visual_materials import run_visual_materials


def run_batch(script_batch, directory, previous_run=None, *, full_report=False):
    """Снимки входов и журнал остаются прежними при продолжении команды."""
    script_batch, directory = Path(script_batch).resolve(), Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    source = read(script_batch / "batch.json")
    path = directory / "batch_summary.json"
    summary = (
        read(path)
        if path.exists()
        else {
            "schema_version": 1,
            "mode": "real",
            "stage": "visuals_only",
            "created_at_utc": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
            "source_batch": str(script_batch),
            "topics": [],
            "service_error": None,
            "real_upstream_generations": 0,
            "cost_usd": None,
            "cost_reason": "Суммы подтверждаются отдельно через Costs API; usage не равен списаниям.",
        }
    )
    if summary["source_batch"] != str(script_batch):
        raise ProductionError(
            "В этой папке уже другой набор сценариев. Исходные результаты сохранены."
        )
    if previous_run is not None:
        previous_run = str(Path(previous_run).resolve())
        if summary.get("previous_run") not in (None, previous_run):
            raise ProductionError("Нельзя заменить ссылку на предыдущий запуск.")
        summary["previous_run"] = previous_run
    if (
        summary.get("service_error")
        and summary.get("temporary_service_error")
        and (summary.get("retry_at") or float("inf")) <= time.time()
    ):
        summary["service_error"] = None
        summary["resume_after_service_pause"] = time.time()
        atomic_write_json(path, summary)
    if summary.get("service_error"):
        print("Сохранённая ошибка сервиса: " + summary["service_error"])
        return path
    # Все три одобренных входа фиксируем до первого запроса. При общем сбое
    # отчёт всё равно покажет остальные темы как не запущенные, без заглушек.
    for index, item in enumerate(source["entries"], 1):
        origin = Path(item["directory"])
        if not origin.is_absolute():
            origin = script_batch / origin
        script = approved_script(origin)
        run = directory / f"topic_{index}"
        snapshot_inputs(origin, run, include_audio=False)
        if not any(topic["topic"] == item["topic"] for topic in summary["topics"]):
            summary["topics"].append(
                {
                    "topic": item["topic"],
                    "directory": str(run),
                    "source_directory": str(origin),
                    "script_version_id": script["script_version_id"],
                    "script_sha256": sha(run / "script.json"),
                    "visual_status": "not_started",
                    "render_status": "not_started",
                    "duration_seconds": 0,
                    "reason": None,
                }
            )
        if not (run / "visual_materials.json").exists():
            atomic_write_json(
                run / "visual_materials.json",
                {
                    "schema_version": 1,
                    "mode": "real",
                    "script_version_id": script["script_version_id"],
                    "status": "not_started",
                    "fragments": [
                        {
                            "fragment_index": i,
                            "status": "not_started",
                            "reason": "Запросы по этому фрагменту ещё не выполнялись.",
                        }
                        for i in range(1, len(script["fragments"]) + 1)
                    ],
                },
            )
    atomic_write_json(path, summary)
    for index, item in enumerate(source["entries"], 1):
        origin = Path(item["directory"])
        if not origin.is_absolute():
            origin = script_batch / origin
        script = approved_script(origin)
        run = directory / f"topic_{index}"
        snapshot_inputs(origin, run, include_audio=False)
        copied = approved_script(run)
        if copied["script_version_id"] != script["script_version_id"]:
            raise ProductionError("Версия сценария изменилась во время копирования.")
        print(f'{index}/{len(source["entries"])}: {item["topic"]}', flush=True)
        if previous_run:
            atomic_write_json(
                run / "visual_previous.json",
                {"directory": str(Path(previous_run) / f"topic_{index}")},
            )
        started = time.monotonic()
        materials = read(run_visual_materials(run))
        previous = next(
            (topic for topic in summary["topics"] if topic["topic"] == item["topic"]),
            None,
        )
        entry = {
            "topic": item["topic"],
            "directory": str(run),
            "source_directory": str(origin),
            "script_version_id": script["script_version_id"],
            "script_sha256": sha(run / "script.json"),
            "visual_status": materials["status"],
            "render_status": "not_started",
            "reason": materials.get("service_error"),
            "duration_seconds": (previous or {}).get("duration_seconds", 0)
            + time.monotonic()
            - started,
            "fragments": materials["fragments"],
        }
        summary["topics"] = [
            entry if topic["topic"] == item["topic"] else topic
            for topic in summary["topics"]
        ]
        summary["service_error"] = materials.get("service_error")
        summary["temporary_service_error"] = materials.get(
            "temporary_service_error", False
        )
        summary["retry_at"] = materials.get("retry_at")
        atomic_write_json(path, summary)
        accepted = sum(
            fragment["status"] == "approved" for fragment in materials["fragments"]
        )
        print(
            f'Принято {accepted}/{len(materials["fragments"])}; {materials["status"]}',
            flush=True,
        )
        if summary["service_error"]:
            print("Общий сбой сервиса: остальные запросы не отправляются.", flush=True)
            break
    if full_report:
        report = directory / (
            "Smartgram_visuals_only_" + summary["created_at_utc"] + ".html"
        )
        build_report(directory, report)
        return report
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--script-batch", type=Path, default=Path("outputs/script_batch_tvuyecw6")
    )
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument(
        "--previous-run",
        type=Path,
        help="Предыдущая итерация: история не меняется и её лимиты не сбрасываются",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--full-report",
        action="store_true",
        help="HTML один раз после всех тем, без автоматической сверки Costs API",
    )
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="Только HTML сохранённого запуска, без API",
    )
    args = parser.parse_args()
    if args.dry_run:
        print(
            "Пять шагов изображений из одобренных text/visual. Без исследования, сценариста, TTS, ASR и монтажа; API не вызываются."
        )
        return 0
    try:
        if args.report_only:
            report = args.directory / "report.html"
            build_report(args.directory, report)
        else:
            report = run_batch(
                args.script_batch,
                args.directory,
                args.previous_run,
                full_report=args.full_report,
            )
    except (ProductionError, OSError, ValueError, KeyError) as error:
        print("Подготовка изображений остановлена: " + str(error))
        return 1
    print("Результат:", report)
    copied = True
    if report.suffix == ".html":
        from html_report_sync import sync_generated_report

        copied = sync_generated_report(report, refresh_billing=False)
    summary = read(args.directory / "batch_summary.json")
    return 0 if copied and not summary.get("service_error") else 1


if __name__ == "__main__":
    raise SystemExit(main())
