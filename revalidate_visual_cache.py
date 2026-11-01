"""Явная техническая переоценка кеша после изменения кода, без вызовов модели."""

import argparse
from datetime import datetime, timezone
from pathlib import Path

from production_state import Journal, ProductionError, config, digest, run_lock
from visual_materials import verify_asset
from visual_shots import validate_shot_ranges


def revalidate_cache(directory):
    """Параметры/промпты обязаны совпасть. Хеш кода не заменяет проверку артефактов."""
    with run_lock(directory):
        current = config()["visuals"]
        journal = Journal(directory, "visual_materials", current)
        planned = []
        for job in journal.state["jobs"].values():
            old_id = job.get("approved_settings_id")
            if not old_id or old_id == digest(current):
                continue
            old = journal.state["snapshots"].get(old_id)
            if old is None:
                raise ProductionError("Нет исходного снимка настроек принятого показа.")
            comparable = lambda value: {
                k: v for k, v in value.items() if k != "implementation_sha256"
            }
            if comparable(old) != comparable(current):
                raise ProductionError(
                    "Промпт, модель или ограничения изменились: технической переоценки недостаточно."
                )
            if job.get("pending_repair", {}).get("status") in {
                "pending",
                "in_progress",
                "replacement_required",
            }:
                raise ProductionError(
                    "Незавершённое исправление запрещает восстановление старого одобрения."
                )
            validate_shot_ranges(job["fragment"], job["shots"])
            evidence = []
            for shot in job.get("approved", {}).values():
                if not any(
                    s["shot_id"] == shot["shot_id"] and s["text"] == shot["text"]
                    for s in job["shots"]
                ):
                    raise ProductionError(
                        "Принятый кадр больше не соответствует плану."
                    )
                asset = verify_asset(shot, Path(directory))
                evidence.append(
                    {
                        "shot_id": shot["shot_id"],
                        "original_sha256": asset["original_sha256"],
                        "preview_sha256": [
                            p["sha256"] for p in asset["motion"]["previews"]
                        ],
                        "visual_model_repeated": False,
                    }
                )
            planned.append((job, old_id, evidence))
        # Только после успешной проверки всех изменившихся записей.
        for job, old_id, evidence in planned:
            job.setdefault("technical_revalidations", []).append(
                {
                    "created_at_utc": datetime.now(timezone.utc).isoformat(),
                    "previous_settings_id": old_id,
                    "current_settings_id": digest(current),
                    "same_model_prompts_and_limits": True,
                    "checked_files": evidence,
                    "new_model_requests": 0,
                }
            )
            job["approved_settings_id"] = digest(current)
        journal.save()
        return len(planned)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        print("Технически перепроверено записей:", revalidate_cache(args.run_dir))
    except (ProductionError, OSError, ValueError) as error:
        parser.exit(1, str(error) + "\n")
