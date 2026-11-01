"""Адресный ремонт: фактический участок видео, устойчивый ID и отдельный ресурс."""

import copy
import re
from pathlib import Path
from production_state import ProductionError, ProductionServiceError, digest, read, sha
from visual_sequence import same_source

DEFAULT_REPAIR = {
    "max_repairs_per_fragment": 2,
    "search_rounds_limit": 2,
    "cards_per_round": 50,
    "previews_per_round": 10,
    "max_search_api_calls_per_fragment_per_round": 6,
    "finalist_limit": 3,
}


def search_limits(settings):
    """Ресурс ремонта не изменяет первоначальный бюджет и фиксируется в журнале."""
    repair = dict(DEFAULT_REPAIR, **settings.get("repair", {}))
    for name, value in repair.items():
        if type(value) is not int or not 0 < value <= 100:
            raise ProductionError(
                f"Настройка repair.{name} должна быть целым числом 1–100."
            )
    return dict(
        settings["search"],
        **repair,
        search_results_limit=repair["cards_per_round"] * repair["search_rounds_limit"],
        preview_limit=repair["previews_per_round"] * repair["search_rounds_limit"],
    )


def normalized_issues(issues):
    return sorted(
        [
            {
                k: re.sub(r"\s+", " ", str(i.get(k, ""))).strip()
                for k in (
                    "frame_id",
                    "cue_id",
                    "fragment_index",
                    "problem",
                    "correction",
                    "target_step",
                )
            }
            for i in issues
        ],
        key=digest,
    )


def resolve_targets(directory, visuals, issues):
    """frame/cue → реальный montage_plan, а не автоматически весь narration-фрагмент."""
    directory = Path(directory)
    plan = (
        read(directory / "montage_plan.json")
        if (directory / "montage_plan.json").exists()
        else None
    )
    quality = (
        read(directory / "quality_report.json")
        if (directory / "quality_report.json").exists()
        else {}
    )
    targets = {}
    for issue in issues:
        if issue.get("target_step") not in {"video_render", "visual_materials"}:
            continue
        frame = issue.get("frame_id")
        cue = issue.get("cue_id")
        sections = []
        if frame or cue is not None:
            if not plan:
                raise ProductionError(
                    "Для адресного ремонта отсутствует montage_plan.json."
                )
            samples = [
                s
                for s in quality.get("samples", [])
                if (s["frame_id"] == frame if frame else s.get("cue_id") == cue)
            ]
            if frame:
                if (
                    samples
                    and cue is not None
                    and any(s.get("cue_id") != cue for s in samples)
                ):
                    raise ProductionError(
                        "frame_id и cue_id относятся к разным сохранённым кадрам."
                    )
                match = re.fullmatch(r"frame_(\d+)", frame)
                if not match:
                    raise ProductionError("Неизвестный frame_id для ремонта: " + frame)
                number = int(match.group(1))
                sections = [
                    s
                    for s in plan["sections"]
                    if s["start_frame"] <= number < s["end_frame"]
                ]
            else:
                ids = {s.get("visual_section_id") for s in samples}
                sections = [s for s in plan["sections"] if s.get("section_id") in ids]
            if len(sections) != 1:
                raise ProductionError(
                    "Неоднозначная привязка замечания к визуальному участку; ремонт остановлен."
                )
            section = sections[0]
            owner = next(
                (
                    s
                    for f in visuals["fragments"]
                    for s in f.get("shots", [])
                    if s["shot_id"] == section["shot_id"]
                ),
                None,
            )
            if owner is None:
                raise ProductionError("Исходный shot визуального участка отсутствует.")
            for fragment in visuals["fragments"]:
                for shot in fragment.get("shots", []):
                    if shot["shot_id"] == owner["shot_id"] or (
                        shot["shot_id"] in section.get("covered_shot_ids", [])
                        and shot.get("asset")
                        and same_source(shot, owner)
                    ):
                        targets.setdefault(fragment["fragment_index"], set()).add(
                            shot["shot_id"]
                        )
        else:
            index = issue.get("fragment_index")
            found = next(
                (f for f in visuals["fragments"] if f["fragment_index"] == index), None
            )
            if not found:
                raise ProductionError("Не найден фрагмент замечания к видеоряду.")
            targets.setdefault(index, set()).update(
                s["shot_id"] for s in found.get("shots", [])
            )
    if not targets:
        raise ProductionError("Нет однозначно определённых кадров для ремонта.")
    return {index: sorted(ids) for index, ids in sorted(targets.items())}


def repair_identity(directory, script, targets, issues):
    request = Path(directory) / "correction_request.json"
    quality = Path(directory) / "quality_report.json"
    candidate = read(request).get("candidate_sha256") if request.exists() else None
    candidate = candidate or (
        read(quality).get("candidate_sha256") if quality.exists() else None
    )
    if not candidate:
        raise ProductionError(
            "Не сохранён хеш проверенного кандидата видео; ремонт не начат."
        )
    root = Path(directory)
    copies = [root / "candidate.mp4", root / "editing_attempts" / (candidate + ".mp4")]
    if not any(path.is_file() and sha(path) == candidate for path in copies):
        raise ProductionError(
            "Байты проверенного кандидата видео отсутствуют или хеш не совпадает; ремонт не начат."
        )
    return digest(
        {
            "script_version_id": script["script_version_id"],
            "candidate_sha256": candidate,
            "affected_shots": targets,
            "issues": normalized_issues(issues),
        }
    )


def context(job):
    return copy.deepcopy(job.get("repair_context"))


def candidate_allowed(runner, job, shot_id, candidate):
    if runner.source_in_use(candidate, shot_id):
        return False
    return not any(
        same_source(candidate, old)
        for old in job.get("repair_context", {}).get("rejected_materials", [])
    )


def assessment_key(job, shot_id, candidate_id):
    shot = next((s for s in job.get("shots", []) if s["shot_id"] == shot_id), {})
    prefix = (
        (job["repair_id"] + ":" + shot.get("task_version", "") + ":")
        if job.get("repair_id")
        else ""
    )
    return prefix + shot_id + ":" + candidate_id


def start_repair(runner, script, visuals, issues):
    """До вызовов API сохраняем отзыв только проблемных кадров и ресурс каждого фрагмента."""
    visuals = copy.deepcopy(visuals)
    for saved in list(runner.journal.state["jobs"].values()):
        pending = saved.get("pending_repair", {})
        if pending.get("status") != "completed" and pending.get("previous_result"):
            visuals["fragments"][saved["fragment_index"] - 1] = copy.deepcopy(
                pending["previous_result"]
            )
    targets = resolve_targets(runner.directory, visuals, issues)
    identifier = repair_identity(runner.directory, script, targets, issues)
    limits = search_limits(runner.settings)
    for index, ids in targets.items():
        fragment = script["fragments"][index - 1]
        key = digest({"fragment": fragment, "index": index})
        parent = runner.journal.state["jobs"].get(key)
        if not parent:
            raise ProductionError(
                "Нет сохранённого задания исходного фрагмента для ремонта."
            )
        old = parent.get("pending_repair", {})
        work_key = digest({"parent": key, "repair_id": identifier})
        if work_key in runner.journal.state["jobs"]:
            if old.get("repair_id") != identifier:
                raise ProductionError(
                    "Нельзя восстановить другой ремонт поверх текущего."
                )
            continue
        records = parent.setdefault("repair_jobs", [])
        if len(records) >= limits["max_repairs_per_fragment"]:
            raise ProductionError(
                f'Лимит ремонтов фрагмента {index}: {len(records)}/{limits["max_repairs_per_fragment"]}.'
            )
        previous = copy.deepcopy(
            old.get("previous_result")
            if old and old.get("status") != "completed"
            else visuals["fragments"][index - 1]
        )
        all_shots = previous.get("shots") or parent.get("shots", [])
        sources = [s for s in all_shots if s["shot_id"] in ids]
        if len(sources) != len(ids) or any(not s.get("asset") for s in sources):
            raise ProductionError(
                "Отсутствуют исходные принятые материалы проблемного участка."
            )
        for existing in records:
            runner.journal.state["jobs"][existing]["superseded"] = True
        rejected = parent.setdefault("repair_rejected_sources", {})
        for source in sources:
            saved = rejected.setdefault(source["shot_id"], [])
            if not any(same_source(source, old) for old in saved):
                saved.append(copy.deepcopy(source["asset"]))
        repair_context = {
            "repair_id": identifier,
            "script_version_id": script["script_version_id"],
            "affected_shot_ids": ids,
            "issues": copy.deepcopy(issues),
            "original_fragment": copy.deepcopy(fragment),
            "rejected_materials": [
                copy.deepcopy(asset) for sid in ids for asset in rejected[sid]
            ],
        }
        work = {
            "key": work_key,
            "repair_of": key,
            "repair_id": identifier,
            "fragment": copy.deepcopy(fragment),
            "fragment_index": index,
            "original_shots": copy.deepcopy(parent["shots"]),
            "shots": [copy.deepcopy(s) for s in parent["shots"] if s["shot_id"] in ids],
            "rounds": [],
            "approved": {},
            "corrections": {},
            "rejections": [],
            "status": "running",
            "workflow_version": 3,
            "budget": {},
            "cache_ids": [],
            "search_limits": limits,
            "generation_settings": copy.deepcopy(runner.settings["generation"]),
            "repair_context": repair_context,
        }
        runner.journal.state["jobs"][work_key] = work
        records.append(work_key)
        parent.setdefault("approval_history", []).append(copy.deepcopy(previous))
        parent["pending_repair"] = {
            "repair_id": identifier,
            "work_key": work_key,
            "status": "replacement_required",
            "script_version_id": script["script_version_id"],
            "issues": copy.deepcopy(issues),
            "previous_result": previous,
        }
        for retained in all_shots:
            if (
                retained["shot_id"] not in ids
                and retained.get("status") == "approved"
                and retained["shot_id"] not in parent["approved"]
            ):
                from visual_materials import verify_asset

                verify_asset(retained, runner.directory)
                parent["approved"][retained["shot_id"]] = copy.deepcopy(retained)
        for sid in ids:
            parent["approved"].pop(sid, None)
        parent["status"] = "repair_pending"
    runner.journal.save()  # Один write-ahead: все отзывы и бюджеты до сети/изменения публичного файла.
    result = copy.deepcopy(visuals)
    result["status"] = "needs_visual"
    for index, ids in targets.items():
        parent = runner.journal.state["jobs"][
            digest({"fragment": script["fragments"][index - 1], "index": index})
        ]
        for shot in result["fragments"][index - 1].get("shots", []):
            if shot["shot_id"] in ids:
                shot.update(
                    status="needs_visual",
                    reason="Ожидается адресная замена после финальной проверки.",
                )
        result["fragments"][index - 1]["status"] = "needs_visual"
    from file_io import atomic_write_json

    atomic_write_json(runner.directory / "visual_materials.json", result)
    return identifier


def resume_fragment(runner, parent):
    """Повторный вход продолжает тот же ресурс, не возвращая отозванное approved."""
    pending = parent["pending_repair"]
    if not pending.get("work_key"):
        # Старый exhausted не означает расход нового ресурса; исходные счётчики сохраняются.
        script = read(runner.directory / "script.json")
        visuals = read(runner.directory / "visual_materials.json")
        for job in runner.journal.state["jobs"].values():
            if job.get("pending_repair", {}).get("previous_result"):
                visuals["fragments"][job["fragment_index"] - 1] = copy.deepcopy(
                    job["pending_repair"]["previous_result"]
                )
        start_repair(runner, script, visuals, pending["issues"])
        pending = parent["pending_repair"]
    work = runner.journal.state["jobs"][pending["work_key"]]
    runner.limits = work["search_limits"]
    try:
        runner.plan_repair(work)
        runner.search_repair(work)
    except ProductionError as error:
        work["stop"] = {
            "kind": "service" if isinstance(error, ProductionServiceError) else "error",
            "reason": str(error),
        }
        pending["reason"] = str(error)
        runner.journal.save()
        raise
    for sid, entry in work["approved"].items():
        from visual_materials import verify_asset

        verify_asset(entry, runner.directory)
        parent["approved"][sid] = copy.deepcopy(entry)
    for updated in work["shots"]:
        pos = next(
            i
            for i, s in enumerate(parent["shots"])
            if s["shot_id"] == updated["shot_id"]
        )
        parent["shots"][pos] = copy.deepcopy(updated)
    ready = len(parent["approved"]) == len(parent["shots"])
    parent["status"] = "approved" if ready else "repair_pending"
    if not ready:
        pending["status"] = "replacement_required"
    pending["reason"] = (
        None
        if ready
        else work.get("stop", {}).get("reason", "Кандидаты не прошли проверку.")
    )
    runner.journal.save()
    result = runner.fragment_result(parent)
    if not ready:
        result["reason"] = pending["reason"]
    return result
