"""Одна явная коррекция неверного JSON выбора по тем же сохранённым картинкам."""

import argparse
from pathlib import Path
from production_state import config, digest, read, run_lock
from shot_materials import ShotMaterials
from visual_materials import Selection, validate_selection
from production_state import ProductionError, inside


def repair_saved_selection(directory):
    """Не меняет сырой ответ, не ищет файлы и не восстанавливает бюджеты."""
    directory = Path(directory)
    with run_lock(directory):
        runner = ShotMaterials(directory)
        fixed = 0
        for operation_key, operation in list(
            runner.journal.state["operations"].items()
        ):
            if (
                operation["step"] != "visual_select"
                or operation["status"] != "completed"
            ):
                continue
            data = operation["identity"]["data"]
            if "contract_feedback" in data or not data.get("shot"):
                continue
            candidates = data["candidates"]
            index = data["fragment"]["fragment_index"]
            try:
                validate_selection(operation["result"], candidates, index)
                continue
            except ProductionError as error:
                feedback = str(error)
            shot = data["shot"]
            sid = shot["shot_id"]
            job = next(
                (
                    j
                    for j in runner.journal.state["jobs"].values()
                    if any(s["shot_id"] == sid for s in j.get("shots", []))
                ),
                None,
            )
            if not job or sid in job["approved"]:
                continue
            rd = next(
                (
                    r
                    for r in reversed(job["rounds"])
                    if all(
                        c["candidate_id"]
                        in {p["candidate_id"] for p in r["candidates"]}
                        for c in candidates
                    )
                ),
                None,
            )
            if not rd:
                raise ProductionError(
                    "Не найден исходный раунд для сохранённых кандидатов."
                )
            repairs = rd.setdefault("contract_repairs", {})
            if repairs.get(sid, {}).get("status") == "completed":
                continue
            if sid in repairs and repairs[sid]["original_operation"] != operation_key:
                raise ProductionError(
                    "Один дополнительный ответ на кадр уже использован."
                )
            repairs[sid] = {
                "original_operation": operation_key,
                "status": "started",
                "reason": feedback,
            }
            runner.journal.save()
            settings = dict(runner.settings["selection"])
            settings["prompt"] = (
                (Path(__file__).parent / "prompts/visual_select_contract_repair.txt")
                .read_text()
                .rstrip("\n")
            )
            result = runner.ask(
                "visual_select",
                settings,
                Selection,
                dict(
                    data,
                    contract_feedback=feedback,
                    previous_invalid_response=operation["result"],
                    allowed_candidate_ids=[c["candidate_id"] for c in candidates],
                ),
                [
                    (c["candidate_id"], inside(directory, c["original_path"]))
                    for c in candidates
                ],
            )
            validate_selection(result, candidates, index)
            repairs[sid].update(status="completed", result=result)
            rd.setdefault("selections", {})[sid] = result
            rd["finished"] = False
            job["status"] = "running"
            runner.journal.save()
            fixed += 1
        return fixed


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        print("Исправлено контрактов:", repair_saved_selection(args.run_dir))
    except (ProductionError, OSError, ValueError) as error:
        parser.exit(1, str(error) + "\n")
