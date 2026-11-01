"""Кадры внутри фрагмента: общий бюджет поиска, реальные картинки и проверка превью."""

from pathlib import Path
import hashlib
import time

from commons_api import create_commons_client, VisualError
from commons_requests import CommonsRequests, CommonsPaused, RequestExhausted
from commons_materials import (
    search_files,
    search_categories,
    category_files,
    resolve_rights,
    fetch_material,
    basic_problem,
)
from file_io import atomic_write_json
from image_files import inspect_image
from production_state import (
    ProductionError,
    ProductionServiceError,
    digest,
    inside,
    sha,
)
from visual_geometry import prepare
from visual_materials import (
    VisualMaterials,
    Selection,
    Review,
    validate_selection,
    verify_asset,
)
from visual_shots import VisualPlan, bind_shots, validate_shot_ranges


class ShotMaterials(VisualMaterials):
    def plan_round(self, fragment, index, job, rd):
        if "queries" in rd:
            return
        response = self.ask(
            "visual_queries",
            self.settings["queries"],
            VisualPlan,
            {
                "fragments": [dict(fragment, fragment_index=index)],
                "existing_shots": job.get("shots"),
                "search_round": rd["number"],
                "previous_queries_and_results": [
                    r.get("outcomes", []) for r in job["rounds"]
                ],
                "rejections": job["rejections"],
                "parent_budget": self.settings["search"],
                "format": self.settings["preparation"]["output"],
            },
        )
        if (
            len(response["fragments"]) != 1
            or response["fragments"][0]["fragment_index"] != index
        ):
            raise ProductionError("План не относится к запрошенному фрагменту.")
        shots = bind_shots(fragment, index, response["fragments"][0]["shots"])
        if job.get("shots") and [s["shot_id"] for s in shots] != [
            s["shot_id"] for s in job["shots"]
        ]:
            raise ProductionError(
                "Второй раунд изменил текстовые диапазоны уже сохранённого плана."
            )
        if not job.get("shots"):
            job["shots"] = shots
        else:
            for new, saved in zip(shots, job["shots"]):
                if saved["shot_id"] not in job["approved"]:
                    saved.update(new)
        rd["queries"] = {"shots": shots}
        self.journal.save()

    def record_rejection(self, job, shot, candidate, reason, terminal=True):
        job["rejections"].append(
            {
                "shot_id": shot["shot_id"],
                "candidate_id": candidate["candidate_id"],
                "reason": str(reason),
                "source": candidate,
                "terminal": terminal,
            }
        )
        self.journal.save()

    def acquire(self, job, rd, shot, gateway, call_limit, candidate_quota):
        sid = shot["shot_id"]
        route = rd.setdefault("cursors", {}).get(sid, 0)
        queries = shot["queries"]
        categories = job.setdefault("categories", {}).setdefault(sid, [])
        # Маршруты сохраняются: после паузы появившаяся категория не сдвинет курсор.
        if sid not in rd.setdefault("routes", {}):
            if categories:
                routes = [("category", 0), ("file", queries[0]), ("file", queries[-1])]
            elif rd["number"] == 2 and call_limit - rd["calls"] <= 2:
                routes = [("discover", queries[-1]), ("category", 0)]
            else:
                routes = [("file", queries[0])]
                if queries[-1] != queries[0]:
                    routes.append(("file", queries[-1]))
                routes += [("discover", queries[-1]), ("category", 0)]
                routes += [("file", q) for q in queries[1:-1]]
                routes.append(("category", 1))
            previous_queries = {
                o.get("query")
                for r in job["rounds"][:-1]
                for o in r.get("outcomes", [])
                if o.get("shot_id") == sid
            }
            routes = [
                (kind, value)
                for kind, value in routes
                if kind != "file" or value not in previous_queries
            ]
            rd["routes"][sid] = routes
            self.journal.save()
        routes = rd["routes"][sid]
        already = {c["candidate_id"] for c in rd["candidates"]}
        taken = 0
        while (
            (rd["calls"] < call_limit or rd.get("pending_shot") == sid)
            and len(rd["candidates"]) < 5
            and taken < candidate_quota
        ):
            categories = job.setdefault("categories", {}).setdefault(sid, [])
            rd["pending_shot"] = sid
            self.journal.save()
            try:
                if route >= len(routes):
                    rd.pop("pending_shot", None)
                    break
                kind, value = routes[route]
                if kind == "file":
                    label = value
                    page = search_files(gateway, value)
                elif kind == "discover":
                    found = search_categories(gateway, value)
                    categories[:] = list(dict.fromkeys(categories + found))
                    page = {"candidates": []}
                    label = "categories: " + value
                elif value < len(categories):
                    label = categories[value]
                    page = category_files(gateway, label)
                else:
                    rd.pop("pending_shot", None)
                    route += 1
                    rd["cursors"][sid] = route
                    self.journal.save()
                    continue
            except RequestExhausted as error:
                rd["outcomes"].append(
                    {
                        "shot_id": sid,
                        "route": route,
                        "status": "request_budget_exhausted",
                        "reason": str(error),
                    }
                )
                rd.pop("pending_shot", None)
                route += 1
                rd["cursors"][sid] = route
                self.journal.save()
                continue
            outcome = {
                "shot_id": sid,
                "route": route,
                "query": label,
                "found": len(page["candidates"]),
                "status": "found" if page["candidates"] else "empty",
                "filtered": 0,
                "downloaded": 0,
            }
            previous_outcome = next(
                (
                    item
                    for item in rd["outcomes"]
                    if item.get("shot_id") == sid
                    and item.get("route") == route
                    and "query" in item
                ),
                None,
            )
            if previous_outcome is None:
                rd["outcomes"].append(outcome)
            else:
                outcome = previous_outcome
            # Продолжение плохой выдачи не вытесняет более простой запрос.
            for candidate in page["candidates"]:
                if taken >= candidate_quota or len(rd["candidates"]) >= 5:
                    break
                cid = hashlib.sha256(
                    (
                        candidate["file_title"]
                        + "#"
                        + str(candidate.get("source_sha1") or "")
                    ).encode()
                ).hexdigest()[:24]
                candidate = dict(candidate, candidate_id=cid)
                if cid in already or any(
                    r.get("shot_id") == sid
                    and r["candidate_id"] == cid
                    and r.get("terminal", True)
                    for r in job["rejections"]
                ):
                    continue
                try:
                    problem = basic_problem(candidate)
                    if problem:
                        raise ProductionError(problem)
                    candidate = resolve_rights(candidate, gateway)
                    from visual_materials import metadata_problem

                    problem = metadata_problem(candidate)
                    if problem:
                        raise ProductionError(problem)
                    cached = job.setdefault("assets_cache", {}).get(cid)
                    if (
                        cached
                        and cached.get("original_url") == candidate["original_url"]
                        and cached.get("source_sha1") == candidate.get("source_sha1")
                        and sha(inside(self.directory, cached["original_path"]))
                        == cached["file_sha256"]
                    ):
                        inspect_image(
                            inside(
                                self.directory, cached["original_path"]
                            ).read_bytes(),
                            minimum_width=1,
                            minimum_height=1,
                        )
                        material = cached
                    else:
                        material = fetch_material(candidate, gateway)
                        outcome["downloaded"] += 1
                    duplicate = next(
                        (
                            c
                            for c in job["assets_cache"].values()
                            if c["file_sha256"] == material["file_sha256"]
                            and c["candidate_id"] != cid
                        ),
                        None,
                    )
                    if duplicate:
                        raise ProductionError(
                            "Одинаковый файл под другим именем уже рассмотрен."
                        )
                    job["assets_cache"][cid] = material
                    rd["candidates"].append(material)
                    already.add(cid)
                    taken += 1
                except CommonsPaused:
                    raise
                except (ProductionError, VisualError) as error:
                    outcome["filtered"] += 1
                    self.record_rejection(job, shot, candidate, error)
                self.journal.save()
            rd.pop("pending_shot", None)
            route += 1
            rd["cursors"][sid] = route
            self.journal.save()

    def choose(self, job, rd, shot, fragment):
        sid = shot["shot_id"]
        rejected = {
            r["candidate_id"]
            for r in job["rejections"]
            if r.get("shot_id") == sid and r.get("terminal", True)
        }
        candidates = [c for c in rd["candidates"] if c["candidate_id"] not in rejected and not self.source_in_use(c,sid)]
        if not candidates:
            return None
        data = {
            "fragment": {
                "fragment_index": shot["fragment_index"],
                "text": shot["text"],
                "visual": shot["visual_task"],
            },
            "shot": shot,
            "original_fragment_text": fragment["text"],
            "candidates": candidates,
            "requirements": {
                "must_show": shot["must_show"],
                "must_not_show": shot["must_not_show"],
                "preferences": shot["preferences"],
            },
            "output": self.settings["preparation"],
            "previous_rejections": job["rejections"],
        }
        result = rd.get("selections", {}).get(sid)
        if result is None:
            result = validate_selection(
                self.ask(
                    "visual_select",
                    self.settings["selection"],
                    Selection,
                    data,
                    [
                        (c["candidate_id"], inside(self.directory, c["original_path"]))
                        for c in candidates
                    ],
                ),
                candidates,
                shot["fragment_index"],
            )
        rd.setdefault("selections", {})[sid] = result
        self.journal.save()
        for rejection in result["rejected_candidates"]:
            self.record_rejection(
                job,
                shot,
                next(
                    c
                    for c in rd["candidates"]
                    if c["candidate_id"] == rejection["candidate_id"]
                ),
                rejection["reason"],
            )
        if result["status"] != "selected":
            listed = {r["candidate_id"] for r in result["rejected_candidates"]}
            for candidate in candidates:
                if candidate["candidate_id"] not in listed:
                    self.record_rejection(job, shot, candidate, result["reason"])
            return None
        for cid in (result["primary_candidate_id"], result["backup_candidate_id"]):
            if cid is None or cid not in {c["candidate_id"] for c in candidates}:
                continue
            candidate = next(c for c in candidates if c["candidate_id"] == cid)
            composition = result["composition"]
            candidate_selection = result
            if cid != result["primary_candidate_id"]:
                backup = validate_selection(
                    self.ask(
                        "visual_select",
                        self.settings["selection"],
                        Selection,
                        dict(
                            data,
                            candidates=[candidate],
                            previous_rejections=job["rejections"],
                        ),
                        [(cid, inside(self.directory, candidate["original_path"]))],
                    ),
                    [candidate],
                    shot["fragment_index"],
                )
                if backup["status"] != "selected":
                    continue
                composition = backup["composition"]
                candidate_selection = backup
            rd.setdefault("selection_reasons", {})[sid + ":" + cid] = (
                candidate_selection["reason"]
            )
            self.journal.save()
            approved = self.prepare_and_review(job, rd, shot, candidate, composition)
            if approved:
                return approved
        return None

    def prepare_and_review(self, job, rd, shot, candidate, composition):
        cid = candidate["candidate_id"]
        sid = shot["shot_id"]
        from visual_repair import candidate_allowed
        if not candidate_allowed(self,job,sid,candidate):
            self.record_rejection(job,shot,candidate,'Исходник уже занят или отклонён в текущем ремонте.');return None
        original = inside(self.directory, candidate["original_path"])
        state = rd.setdefault("framing", {}).setdefault(
            sid + ":" + cid,
            {"composition": composition, "attempt": job["corrections"].get(cid, 0)},
        )
        while True:
            if state.get("pending_correction"):
                # Тот же платный ключ операции после сбоя; счётчик не растёт повторно.
                correction = validate_selection(
                    self.ask(
                        "visual_select",
                        self.settings["selection"],
                        Selection,
                        state["pending_correction"],
                        [(cid, original)],
                    ),
                    [candidate],
                    shot["fragment_index"],
                )
                if correction["status"] != "selected":
                    return None
                state.update(
                    composition=correction["composition"],
                    attempt=state["pending_correction"]["correction_number"],
                )
                state.pop("pending_correction")
                self.journal.save()
            n = state["attempt"]
            composition = state["composition"]
            folder = self.directory / "visual_materials" / sid / cid
            if job.get('repair_id'):folder=folder/job['repair_id']
            folder=folder/str(n)
            motion = None
            try:
                motion = prepare(
                    original,
                    composition,
                    folder,
                    tuple(
                        self.settings["preparation"]["output"][k]
                        for k in ("width", "height")
                    ),
                    self.settings["preparation"]["max_zoom_percent"],
                )
                for preview in motion["previews"]:
                    preview["path"] = str(
                        Path(preview["path"]).relative_to(self.directory)
                    )
                atomic_write_json(folder / "motion.json", motion)
            except (ProductionError, VisualError) as error:
                reason = str(error)
            if motion:
                review = self.ask(
                    "visual_review",
                    self.settings["review"],
                    Review,
                    {
                        "fragment": {
                            "fragment_index": shot["fragment_index"],
                            "text": shot["text"],
                            "visual": shot["visual_task"],
                        },
                        "shot_id": sid,
                        "source_metadata": candidate,
                        "motion": motion,
                        **({'final_repair_context':{'script_version_id':job['pending_repair']['script_version_id'],
                            'issues':job['pending_repair']['issues'],'previous_result_sha256':digest(job['pending_repair']['previous_result'])}}
                            if job.get('pending_repair') else {}),
                    },
                    [("original", original)]
                    + [
                        (str(p["position"]), inside(self.directory, p["path"]))
                        for p in motion["previews"]
                    ],
                )
                if review["fragment_index"] != shot["fragment_index"]:
                    raise ProductionError("Проверка относится к другому фрагменту.")
                if review["decision"] == "approved":
                    if review["issues"] or review["corrections"] is not None:
                        raise ProductionError(
                            "Одобрение содержит нерешённые замечания."
                        )
                    result = dict(
                        shot,
                        status="approved",
                        review=review,
                        selection_reason=rd["selection_reasons"][sid + ":" + cid],
                        asset={
                            "candidate_id": cid,
                            "original_path": candidate["original_path"],
                            "original_sha256": candidate["file_sha256"],
                            "source": candidate,
                            "source_original_path": candidate.get(
                                "source_original_path"
                            ),
                            "source_original_sha256": candidate.get(
                                "source_original_sha256"
                            ),
                            "motion": motion,
                            "changes": ("Pexels original; EXIF; " if candidate.get("origin")=="pexels" else "Generated derivative; " if candidate.get("origin") in {"generated","deterministic"} else "Wikimedia derivative; EXIF; ")
                            + composition["mode"]
                            + "; zoom "
                            + str(composition["zoom_percent"])
                            + "%",
                        },
                    )
                    verify_asset(result, self.directory)
                    return result
                reason = "; ".join(review["issues"]) or review["decision"]
                if review["decision"] != "fix_framing":
                    self.record_rejection(job, shot, candidate, reason)
                    return None
                proposed = review["corrections"]
            else:
                proposed = None
            self.record_rejection(job, shot, candidate, reason, terminal=False)
            if (
                job["corrections"].get(cid, 0)
                >= self.settings["review"]["max_crop_corrections_per_candidate"]
            ):
                self.record_rejection(
                    job,
                    shot,
                    candidate,
                    "Исчерпаны два исправления кадрирования этого кандидата.",
                )
                return None
            job["corrections"][cid] = job["corrections"].get(cid, 0) + 1
            self.journal.save()
            if proposed is None:
                state["pending_correction"] = {
                    "fragment": {
                        "fragment_index": shot["fragment_index"],
                        "text": shot["text"],
                        "visual": shot["visual_task"],
                    },
                    "shot": shot,
                    "candidates": [candidate],
                    "previous_composition": composition,
                    "technical_error": reason,
                    "correction_number": job["corrections"][cid],
                    "output": self.settings["preparation"],
                }
                self.journal.save()
                continue
            state.update(composition=proposed, attempt=job["corrections"][cid])
            self.journal.save()

    def fragment(self, fragment, index):
        key = digest({"fragment": fragment, "index": index})
        job = self.journal.state["jobs"].setdefault(
            key,
            {
                "key": key,
                "fragment": fragment,
                "fragment_index": index,
                "rounds": [],
                "rejections": [],
                "corrections": {},
                "approved": {},
                "shots": [],
                "status": "running",
                "workflow_version": 2,
            },
        )
        if job.get('pending_repair',{}).get('status') in {'pending','in_progress'}:
            raise ProductionError('Unfinished shot repair; old approval is revoked.')
        if job.get("workflow_version") != 2:
            raise ProductionError(
                "Для новой стратегии требуется отдельный разрешённый run_id; старые лимиты сохранены."
            )
        if job.get("approved_settings_id") and job["approved_settings_id"] != digest(
            self.settings
        ):
            raise ProductionError(
                "Настройки принятого показа изменились; нужна явная переоценка, бюджет сохранён."
            )
        for result in job["approved"].values():
            verify_asset(result, self.directory)
        if job["shots"] and len(job["approved"]) == len(job["shots"]):
            return self.fragment_result(job)
        with create_commons_client() as client:
            while len(job["rounds"]) < 2 or (
                job["rounds"] and not job["rounds"][-1].get("finished")
            ):
                if not job["rounds"] or job["rounds"][-1].get("finished"):
                    job["rounds"].append(
                        {
                            "number": len(job["rounds"]) + 1,
                            "calls": 0,
                            "retry_calls": 0,
                            "candidates": [],
                            "outcomes": [],
                            "finished": False,
                            "search_complete": False,
                        }
                    )
                    self.journal.save()
                rd = job["rounds"][-1]
                self.plan_round(fragment, index, job, rd)
                gateway = CommonsRequests(
                    self.journal, rd, key, client, self.settings["search"]
                )
                pending = [
                    s for s in job["shots"] if s["shot_id"] not in job["approved"]
                ]
                for i, shot in enumerate(pending):
                    remaining = len(pending) - i
                    call_limit = min(
                        6, rd["calls"] + max(1, (6 - rd["calls"]) // remaining)
                    )
                    quota = max(1, (5 - len(rd["candidates"])) // remaining)
                    self.acquire(job, rd, shot, gateway, call_limit, quota)
                    chosen = self.choose(job, rd, shot, fragment)
                    if chosen:
                        job["approved"][shot["shot_id"]] = chosen
                        job["approved_settings_id"] = digest(self.settings)
                        self.journal.save()
                rd["finished"] = True
                rd["search_complete"] = True
                self.journal.save()
                if len(job["approved"]) == len(job["shots"]):
                    job["approved_settings_id"] = digest(self.settings)
                    job["status"] = "approved"
                    self.journal.save()
                    return self.fragment_result(job)
        job["status"] = "exhausted"
        self.journal.save()
        return self.fragment_result(job)

    def fragment_result(self, job):
        shots = []
        for shot in job["shots"]:
            shots.append(
                job["approved"].get(shot["shot_id"])
                or dict(
                    shot,
                    status="needs_visual",
                    reason=(
                        "Нет принятого изображения; поисковые раунды/кандидаты исчерпаны."
                        if job["status"] == "exhausted"
                        else "Подбор не завершён."
                    ),
                )
            )
        ready = bool(shots) and all(s["status"] == "approved" for s in shots)
        result = {
            "fragment_index": job["fragment_index"],
            "fragment_sha256": digest(job["fragment"]),
            "status": "approved" if ready else "needs_visual",
            "shots": shots,
            "timing_status": "awaiting_word_timings",
            "reason": None if ready else "Не все обязательные кадры приняты.",
        }
        if ready and job.get('pending_repair',{}).get('status')=='replacement_required':
            job['pending_repair'].update(status='completed',result_sha256=digest(result))
            self.journal.save()
        return result

    def run(self, script):
        result = {
            "schema_version": 2,
            "mode": "real",
            "script_version_id": script["script_version_id"],
            "settings_snapshot": self.settings,
            "status": "running",
            "fragments": [],
            "timing_status": "awaiting_word_timings",
        }
        path = self.directory / "visual_materials.json"
        for index, fragment in enumerate(script["fragments"], 1):
            try:
                entry = self.fragment(fragment, index)
            except (CommonsPaused, ProductionServiceError) as error:
                key = digest({"fragment": fragment, "index": index})
                job = self.journal.state["jobs"].get(key)
                entry = (
                    self.fragment_result(job)
                    if job
                    else {"fragment_index": index, "status": "paused", "shots": []}
                )
                entry.update(
                    status="paused" if getattr(error, "temporary", False) else "error",
                    reason=str(error),
                )
                for child in entry.get("shots", []):
                    if child["status"] != "approved":
                        child.update(status=entry["status"], reason=str(error))
                result.update(
                    service_error=str(error),
                    retry_at=getattr(error, "retry_at", None),
                    temporary_service_error=getattr(error, "temporary", False),
                )
                result["fragments"].append(entry)
                for n in range(index + 1, len(script["fragments"]) + 1):
                    result["fragments"].append(
                        {
                            "fragment_index": n,
                            "fragment_sha256": digest(script["fragments"][n - 1]),
                            "status": "not_started",
                            "shots": [],
                            "reason": "Остановка после общей ошибки сервиса.",
                        }
                    )
                break
            except (ProductionError, VisualError) as error:
                key = digest({"fragment": fragment, "index": index})
                job = self.journal.state["jobs"].get(key)
                entry = (
                    self.fragment_result(job)
                    if job
                    else {"fragment_index": index, "shots": []}
                )
                entry.update(status="error", reason=str(error))
            result["fragments"].append(entry)
            atomic_write_json(path, result)
        result["status"] = (
            "approved"
            if all(f["status"] == "approved" for f in result["fragments"])
            else ("paused" if result.get("temporary_service_error") else "needs_visual")
        )
        atomic_write_json(path, result)
        atomic_write_json(
            self.directory / "attribution.json",
            {
                "schema_version": 2,
                "script_version_id": script["script_version_id"],
                "images": [
                    {
                        "fragment_index": f["fragment_index"],
                        "shot_id": s["shot_id"],
                        **s["asset"]["source"],
                        "changes": s["asset"]["changes"],
                    }
                    for f in result["fragments"]
                    for s in f.get("shots", [])
                    if s["status"] == "approved"
                ],
            },
        )
        return path

    def repair_approved(self, script, visuals, indices, issues):
        """Снимаем одобрение затронутых кадров; прежние бюджеты не обнуляются."""
        self.begin_repairs(script,visuals,indices,issues)
        for index in indices:
            fragment = script["fragments"][index - 1]
            job = self.journal.state["jobs"].get(
                digest({"fragment": fragment, "index": index})
            )
            if not job:
                raise ProductionError(
                    "Нет сохранённого бюджета для импортированного показа."
                )
            for sid, entry in list(job["approved"].items()):
                self.record_rejection(
                    job,
                    entry,
                    dict(entry["asset"]["source"]),
                    "Финальная проверка запросила другой показ: " + str(issues),
                )
                del job["approved"][sid]
                for rd in job["rounds"]:
                    rd.get("selections", {}).pop(sid, None)
            if job["rounds"]:
                job["rounds"][-1]["finished"] = False
            job["status"] = "running"
            job['pending_repair']['status']='replacement_required'
            job.pop("approved_settings_id", None)
            self.journal.save()
        path = self.run(script)
        from production_state import read

        updated = read(path)
        if updated["status"] != "approved":
            raise ProductionError("Не удалось исправить показ в оставшемся бюджете.")
        return updated
