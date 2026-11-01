"""Five-step automatic Commons workflow; no audio dependency, bounded durable retries."""

import copy
import hashlib
import json
from pathlib import Path
from typing import Literal

import httpx
from pydantic import Field, StrictInt
from model_api import StrictModel
from commons_api import (
    API_URL,
    MAX_API_BYTES,
    VisualError,
    VisualServiceError,
    accepted_license,
    bounded_get,
    create_commons_client,
    download_image,
    parse_candidate,
)
from image_files import inspect_image
from file_io import atomic_write, atomic_write_json
from script_workflow import approved_script
from production_state import (
    Journal,
    ModelClient,
    ProductionError,
    ProductionServiceError,
    config,
    digest,
    inside,
    read,
    run_lock,
    sha,
)
from visual_geometry import prepare


class QueryFragment(StrictModel):
    fragment_index: StrictInt
    queries: list[str] = Field(min_length=1, max_length=6)
    must_show: list[str]
    must_not_show: list[str]


class Queries(StrictModel):
    fragments: list[QueryFragment] = Field(min_length=1)


class Keep(StrictModel):
    label: str
    box: list[float] = Field(min_length=4, max_length=4)


class Composition(StrictModel):
    mode: Literal["crop", "background"]
    initial_crop: list[float] = Field(min_length=4, max_length=4)
    focus_point: list[float] = Field(min_length=2, max_length=2)
    zoom_percent: float = Field(ge=0, le=10)
    must_keep: list[Keep]


class Rejection(StrictModel):
    candidate_id: str
    reason: str


class Selection(StrictModel):
    fragment_index: StrictInt
    status: Literal["selected", "needs_search"]
    primary_candidate_id: str | None
    backup_candidate_id: str | None
    reason: str
    rejected_candidates: list[Rejection]
    composition: Composition | None


class Review(StrictModel):
    fragment_index: StrictInt
    decision: Literal["approved", "fix_framing", "replace_candidate", "needs_search"]
    issues: list[str]
    corrections: Composition | None


def search_page(query, continuation, client):
    params = {
        "action": "query",
        "format": "json",
        "formatversion": 2,
        "generator": "search",
        "gsrnamespace": 6,
        "gsrsearch": query,
        "gsrlimit": 5,
        "prop": "imageinfo",
        "iiprop": "url|size|mime|extmetadata",
        "iiextmetadatalanguage": "en",
        "maxlag": 5,
        **continuation,
    }
    try:
        raw, _ = bounded_get(client, API_URL, MAX_API_BYTES, params)
        response = json.loads(raw)
        if "error" in response:
            raise ProductionServiceError(
                "Commons API: " + json.dumps(response["error"], ensure_ascii=False)
            )
        candidates = []
        for page in response.get("query", {}).get("pages", []):
            try:
                candidates.append(parse_candidate(page))
            except (KeyError, TypeError, IndexError):
                continue
        return {
            "candidates": candidates,
            "continue": response.get("continue", {}),
            "request_params": params,
        }
    except httpx.HTTPError as error:
        raise ProductionServiceError(
            "Commons: "
            + type(error).__name__
            + f' HTTP {getattr(getattr(error,"response",None),"status_code",None)}'
        ) from None
    except VisualServiceError as error:
        raise ProductionServiceError(str(error)) from None
    except (ValueError, KeyError, TypeError):
        raise ProductionServiceError("Commons: invalid response.") from None


def metadata_problem(candidate):
    if candidate.get("origin")=="pexels":
        from pexels_materials import metadata_problem as pexels_problem
        return pexels_problem(candidate)
    for key in (
        "title",
        "file_title",
        "source_page",
        "original_url",
        "author",
        "license",
    ):
        if not isinstance(candidate.get(key), str) or not candidate[key].strip():
            return "Missing " + key
    if candidate.get("original_mime") not in {"image/jpeg", "image/png", "image/svg+xml"}:
        return "Unsupported original MIME"
    from commons_materials import public_domain_proof
    if not accepted_license(candidate["license"], candidate.get("license_url", "")) and not public_domain_proof(candidate):
        return "Unknown/mismatched license"
    from urllib.parse import urlsplit, unquote

    page = urlsplit(candidate["source_page"])
    if (
        page.scheme != "https"
        or page.netloc != "commons.wikimedia.org"
        or not unquote(page.path).startswith("/wiki/File:")
    ):
        return "Invalid Commons source page"
    if (
        type(candidate.get("width")) is not int
        or type(candidate.get("height")) is not int
        or min(candidate["width"], candidate["height"]) <= 0
    ):
        return "Invalid dimensions in source metadata"
    if candidate.get("original_mime") != "image/svg+xml" and max(candidate["width"], candidate["height"]) < 1080:
        return "Insufficient dimensions even before orientation check"
    if candidate.get("restrictions"):
        return "Unresolved usage restrictions: " + candidate["restrictions"]
    return None


def validate_selection(result, candidates, index):
    ids = {c["candidate_id"] for c in candidates}
    if result["fragment_index"] != index:
        raise ProductionError("Selection fragment mismatch.")
    rejected = [r["candidate_id"] for r in result["rejected_candidates"]]
    if len(set(rejected)) != len(rejected) or not set(rejected) <= ids:
        raise ProductionError("Unknown/repeated rejected candidate ID.")
    if result["status"] == "selected":
        primary = result["primary_candidate_id"]
        backup = result["backup_candidate_id"]
        if (
            primary not in ids
            or primary in rejected
            or not result["composition"]
            or (
                backup is not None
                and (backup not in ids or backup == primary or backup in rejected)
            )
        ):
            raise ProductionError("Invalid primary/backup/composition.")
    elif (
        result["primary_candidate_id"] is not None
        or result["backup_candidate_id"] is not None
    ):
        raise ProductionError("needs_search cannot select a candidate.")
    return result


def verify_asset(entry, root):
    asset = entry["asset"]
    original = inside(root, asset["original_path"])
    if sha(original) != asset["original_sha256"]:
        raise ProductionError("Approved original hash changed.")
    if asset["source"].get("origin") in {"generated", "deterministic"}:
        from visual_generation import verify_created
        verify_created(asset["source"], root)
    elif metadata_problem(asset["source"]):
        raise ProductionError("Approved license metadata no longer valid.")
    if asset["source"].get("origin")=="pexels":
        from pexels_materials import verify_source
        verify_source(asset["source"],root)
    evidence = asset['source'].get('rights_evidence')
    if evidence and sha(inside(root,evidence['snapshot_path'])) != evidence['snapshot_sha256']:
        raise ProductionError('Снимок подтверждения прав изменён.')
    if asset.get('source_original_path') and sha(inside(root,asset['source_original_path'])) != asset['source_original_sha256']:
        raise ProductionError('Исходный SVG изменён.')
    motion = asset["motion"]
    if motion["original_sha256"] != asset["original_sha256"]:
        raise ProductionError("Motion/original hash mismatch.")
    from visual_geometry import geometry

    expected = geometry(original, motion["composition"], tuple(motion["output"]))
    if any(motion.get(k) != v for k, v in expected.items()):
        raise ProductionError("Approved geometry has changed.")
    for preview in motion["previews"]:
        if sha(inside(root, preview["path"])) != preview["sha256"]:
            raise ProductionError("Approved preview hash changed.")
    if entry.get("review", {}).get("decision") != "approved":
        raise ProductionError("Asset not approved by visual_review.")
    return asset


class VisualMaterials:
    def __init__(
        self, directory, settings=None, model=None, search=None, download=None
    ):
        self.directory = Path(directory)
        self.settings = settings or config()["visuals"]
        self.journal = Journal(self.directory, "visual_materials", self.settings)
        self.model = model or ModelClient(directory)
        self.search = search or search_page
        self.download = download or download_image
        # Reconcile pre-reservation journals as well: failed/in-flight search requests
        # consume the same six-call limit. Keep the failed call pending to block replay.
        for job in self.journal.state["jobs"].values():
            for rd in job["rounds"]:
                attempts = [
                    op
                    for op in self.journal.state["operations"].values()
                    if op["step"] == "visual_search"
                    and op["identity"].get("job") == job["key"]
                    and op["identity"].get("round") == rd["number"]
                ]
                rd["calls"] = max(rd["calls"], len(attempts))
                failed = [op for op in attempts if op["status"] != "completed"]
                if failed and not rd.get("search_complete"):
                    last = failed[-1]["identity"]
                    rd.setdefault(
                        "pending_search",
                        {k: last[k] for k in ("call", "query", "continue")},
                    )
        self.journal.save()

    def ask(self, step, settings, schema, data, images=()):
        identity = {
            "settings": settings,
            "schema": schema.model_json_schema(),
            "data": data,
            "images": [{"label": label, "sha256": sha(path)} for label, path in images],
        }
        return self.journal.operation(
            step, identity, lambda: self.model(step, settings, schema, data, images)
        )

    def source_in_use(self,candidate,owner):
        from visual_sequence import journal_source_in_use
        return journal_source_in_use(self.directory,self.journal.state,candidate,owner)

    def candidates(self, job, round_state, fragment, index, client):
        settings = self.settings["search"]
        target = settings["candidates_per_fragment_per_round"]
        while (
            round_state.get("pending_search")
            or round_state["calls"]
            < settings["max_search_api_calls_per_fragment_per_round"]
        ) and len(round_state["candidates"]) < target:
            qi = round_state["query_index"]
            queries = round_state["queries"]["queries"]
            if qi >= len(queries):
                break
            if not round_state.get("pending_search"):
                round_state["pending_search"] = {
                    "call": round_state["calls"],
                    "query": queries[qi],
                    "continue": round_state["continue"],
                }
                round_state["calls"] += 1
                self.journal.save()
            pending = round_state["pending_search"]
            call = pending["call"]
            continuation = pending["continue"]
            query = pending["query"]
            # The counter is reserved before even a failing request. A pending completed
            # operation resumes from its stored result; interrupted/failed attempts remain blocked.
            page = self.journal.operation(
                "visual_search",
                {
                    "job": job["key"],
                    "round": round_state["number"],
                    "call": call,
                    "query": query,
                    "continue": continuation,
                },
                lambda: self.search(query, continuation, client),
            )
            round_state.pop("pending_search", None)
            round_state["continue"] = page["continue"]
            if not page["continue"]:
                round_state["query_index"] += 1
            self.journal.save()
            for candidate in page["candidates"]:
                if len(round_state["candidates"]) >= target:
                    break
                cid = hashlib.sha256(candidate["file_title"].encode()).hexdigest()[:24]
                if cid in job["seen_ids"] or self.source_in_use(candidate,"fragment:"+str(index)):
                    continue
                job["seen_ids"].append(cid)
                self.journal.save()
                candidate = {**candidate, "candidate_id": cid}
                problem = metadata_problem(candidate)
                if problem:
                    job["rejections"].append(
                        {"candidate_id": cid, "reason": problem, "source": candidate}
                    )
                    self.journal.save()
                    continue
                try:

                    def fetch():
                        data = self.download(candidate["original_url"], client)
                        measured = inspect_image(
                            data, minimum_width=1, minimum_height=1
                        )
                        expected = (
                            "JPEG"
                            if candidate["original_mime"] == "image/jpeg"
                            else "PNG"
                        )
                        if measured["format"] != expected:
                            raise ProductionError("MIME and decoded original disagree.")
                        ow, oh = [
                            self.settings["preparation"]["output"][name]
                            for name in ("width", "height")
                        ]
                        if min(ow / measured["width"], oh / measured["height"]) > 1:
                            raise ProductionError(
                                "Insufficient resolution even for static whole image."
                            )
                        relative = f"visual_materials/originals/{cid}." + (
                            "jpg" if expected == "JPEG" else "png"
                        )
                        target_path = self.directory / relative
                        target_path.parent.mkdir(parents=True, exist_ok=True)
                        atomic_write(target_path, data)
                        return {**measured, "original_path": relative}

                    measured = self.journal.operation(
                        "visual_download", {"candidate": candidate}, fetch
                    )
                    if measured["file_sha256"] in job["seen_hashes"]:
                        raise ProductionError("Duplicate original hash across rounds.")
                    if (
                        sha(inside(self.directory, measured["original_path"]))
                        != measured["file_sha256"]
                    ):
                        raise ProductionError("Downloaded original changed.")
                    job["seen_hashes"].append(measured["file_sha256"])
                    round_state["candidates"].append({**candidate, **measured})
                except VisualServiceError as error:
                    raise ProductionServiceError(str(error)) from None
                except (ProductionError, VisualError) as error:
                    job["rejections"].append(
                        {"candidate_id": cid, "reason": str(error)}
                    )
                self.journal.save()
        round_state["search_complete"] = True
        self.journal.save()
        return round_state["candidates"]

    def fragment(self, fragment, index):
        key = digest({"fragment": fragment, "index": index})
        job = self.journal.state["jobs"].setdefault(
            key,
            {
                "key": key,
                "fragment": fragment,
                "fragment_index": index,
                "rounds": [],
                "seen_ids": [],
                "seen_hashes": [],
                "rejections": [],
                "corrections": {},
                "status": "running",
            },
        )
        if job.get('pending_repair',{}).get('status') in {'pending','in_progress'}:
            raise ProductionError('Unfinished visual repair; old approval is revoked. Resume the repair, not the cached crop.')
        if job["status"] == "approved":
            if job.get("approved_settings_id") != digest(self.settings):
                raise ProductionError(
                    "Approved visual settings changed; existing budgets retained. Explicitly return the affected fragment for reapproval before release."
                )
            verify_asset(job["result"], self.directory)
            return job["result"]
        with create_commons_client() as client:
            while len(job["rounds"]) < self.settings["search"][
                "max_rounds_per_fragment"
            ] or (job["rounds"] and not job["rounds"][-1].get("finished")):
                if not job["rounds"] or job["rounds"][-1].get("finished"):
                    job["rounds"].append(
                        {
                            "number": len(job["rounds"]) + 1,
                            "calls": 0,
                            "candidates": [],
                            "query_index": 0,
                            "continue": {},
                            "search_complete": False,
                            "attempted": [],
                            "finished": False,
                        }
                    )
                    self.journal.save()
                rd = job["rounds"][-1]
                if "queries" not in rd:
                    result = self.ask(
                        "visual_queries",
                        self.settings["queries"],
                        Queries,
                        {
                            "fragments": [dict(fragment, fragment_index=index)],
                            "format": self.settings["preparation"]["output"],
                            "search_call_budget": self.settings["search"][
                                "max_search_api_calls_per_fragment_per_round"
                            ],
                            "rejections": job["rejections"],
                            "search_round": rd["number"],
                        },
                    )
                    if (
                        len(result["fragments"]) != 1
                        or result["fragments"][0]["fragment_index"] != index
                    ):
                        raise ProductionError(
                            "Queries do not cover requested fragment exactly."
                        )
                    rd["queries"] = result["fragments"][0]
                    self.journal.save()
                candidates = (
                    rd["candidates"]
                    if rd["search_complete"]
                    else self.candidates(job, rd, fragment, index, client)
                )
                candidates=[c for c in candidates if not self.source_in_use(c,"fragment:"+str(index))]
                if not candidates:
                    rd["finished"] = True
                    self.journal.save()
                    continue
                if "selection" not in rd:
                    images = [
                        (c["candidate_id"], inside(self.directory, c["original_path"]))
                        for c in candidates
                    ]
                    rd["selection"] = validate_selection(
                        self.ask(
                            "visual_select",
                            self.settings["selection"],
                            Selection,
                            {
                                "fragment": dict(fragment, fragment_index=index),
                                "candidates": candidates,
                                "requirements": rd["queries"],
                                "output": self.settings["preparation"],
                                "previous_rejections": job["rejections"],
                            },
                            images,
                        ),
                        candidates,
                        index,
                    )
                    self.journal.save()
                selection = rd["selection"]
                if selection["status"] == "needs_search":
                    job["rejections"].extend(selection["rejected_candidates"])
                    rd["finished"] = True
                    self.journal.save()
                    continue
                for cid in (
                    selection["primary_candidate_id"],
                    selection["backup_candidate_id"],
                ):
                    if cid is None or cid in rd["attempted"]:
                        continue
                    candidate = next(c for c in candidates if c["candidate_id"] == cid)
                    if self.source_in_use(candidate,"fragment:"+str(index)):continue
                    original = inside(self.directory, candidate["original_path"])
                    if sha(original) != candidate["file_sha256"]:
                        raise ProductionError("Original changed before selection.")
                    composition = selection["composition"]
                    if cid != selection["primary_candidate_id"]:
                        backup = self.ask(
                            "visual_select",
                            self.settings["selection"],
                            Selection,
                            {
                                "fragment": dict(fragment, fragment_index=index),
                                "candidates": [candidate],
                                "previous_rejections": job["rejections"],
                                "output": self.settings["preparation"],
                            },
                            [(cid, original)],
                        )
                        validate_selection(backup, [candidate], index)
                        if backup["status"] != "selected":
                            rd["attempted"].append(cid)
                            self.journal.save()
                            continue
                        composition = backup["composition"]
                    # Restore latest proposed framing, including reserved correction count.
                    candidate_state = rd.setdefault("framing", {}).setdefault(
                        cid, {"composition": composition, "attempt": 0}
                    )
                    while True:
                        n = candidate_state["attempt"]
                        composition = candidate_state["composition"]
                        attempt_dir = (
                            self.directory
                            / "visual_materials"
                            / key[:16]
                            / cid
                            / str(n)
                        )
                        technical_error = None
                        motion = None
                        try:
                            motion = prepare(
                                original,
                                composition,
                                attempt_dir,
                                tuple(
                                    self.settings["preparation"]["output"][name]
                                    for name in ("width", "height")
                                ),
                                self.settings["preparation"]["max_zoom_percent"],
                            )
                            for preview in motion["previews"]:
                                preview["path"] = str(
                                    Path(preview["path"]).relative_to(self.directory)
                                )
                            atomic_write_json(attempt_dir / "motion.json", motion)
                        except (ProductionError, VisualError) as error:
                            technical_error = str(error)
                        if motion:
                            images = [("original " + cid, original)] + [
                                (
                                    f'preview {p["position"]}',
                                    inside(self.directory, p["path"]),
                                )
                                for p in motion["previews"]
                            ]
                            review = self.ask(
                                "visual_review",
                                self.settings["review"],
                                Review,
                                {
                                    "fragment": dict(fragment, fragment_index=index),
                                    "source_metadata": candidate,
                                    "motion": motion,
                                },
                                images,
                            )
                            if review["fragment_index"] != index:
                                raise ProductionError("Review fragment mismatch.")
                            if review["decision"] == "approved":
                                result = {
                                    "fragment_index": index,
                                    "fragment_sha256": digest(fragment),
                                    "status": "approved",
                                    "review": review,
                                    "asset": {
                                        "candidate_id": cid,
                                        "original_path": candidate["original_path"],
                                        "original_sha256": candidate["file_sha256"],
                                        "source": candidate,
                                        "motion": motion,
                                        "changes": "EXIF orientation; "
                                        + composition["mode"]
                                        + "; zoom "
                                        + str(composition["zoom_percent"])
                                        + "%",
                                    },
                                }
                                job.update(
                                    status="approved",
                                    result=result,
                                    approved_settings_id=digest(self.settings),
                                )
                                if job.get('pending_repair'):
                                    job['pending_repair'].update(status='completed',result_sha256=digest(result))
                                self.journal.save()
                                return result
                            reason = "; ".join(review["issues"]) or review["decision"]
                            next_composition = review["corrections"]
                            fix = review["decision"] == "fix_framing"
                        else:
                            reason = technical_error
                            fix = True
                            next_composition = None
                        job["rejections"].append(
                            {
                                "candidate_id": cid,
                                "reason": reason,
                                "framing_attempt": n,
                            }
                        )
                        self.journal.save()
                        if (
                            not fix
                            or n
                            >= self.settings["review"][
                                "max_crop_corrections_per_candidate"
                            ]
                        ):
                            break
                        # The correction attempt is represented by a deterministic key; paid work cannot repeat after crash.
                        job["corrections"][cid] = max(
                            job["corrections"].get(cid, 0), n + 1
                        )
                        self.journal.save()
                        if next_composition is None:
                            correction = self.ask(
                                "visual_select",
                                self.settings["selection"],
                                Selection,
                                {
                                    "fragment": dict(fragment, fragment_index=index),
                                    "candidates": [candidate],
                                    "output": self.settings["preparation"],
                                    "previous_composition": composition,
                                    "technical_error": reason,
                                    "correction_number": n + 1,
                                },
                                [(cid, original)],
                            )
                            validate_selection(correction, [candidate], index)
                            if correction["status"] != "selected":
                                break
                            next_composition = correction["composition"]
                        candidate_state.update(
                            attempt=n + 1, composition=next_composition
                        )
                        job["corrections"][cid] = n + 1
                        self.journal.save()
                    rd["attempted"].append(cid)
                    self.journal.save()
                rd["finished"] = True
                self.journal.save()
        job.update(
            status="exhausted",
            reason="Search/framing/backup budgets exhausted; approved fragments retained.",
        )
        self.journal.save()
        return {
            "fragment_index": index,
            "fragment_sha256": digest(fragment),
            "status": "needs_visual",
            "reason": job["reason"],
        }

    def begin_repairs(self, script, visuals, indices, issues):
        """Write-ahead revocation of ALL affected jobs before the public manifest or API.

        checked_visuals consults this journal, so a crash between these two writes
        cannot release the old manifest. Previous sources/results and counters stay.
        """
        for index in sorted(indices):
            fragment=script['fragments'][index-1]
            key=digest({'fragment':fragment,'index':index})
            job=self.journal.state['jobs'].get(key)
            if not job:raise ProductionError('No persisted visual correction budget for imported asset.')
            pending=job.get('pending_repair',{})
            if pending.get('status') not in {'pending','in_progress','replacement_required'}:
                if job.get('status')!='approved':raise ProductionError('Only approved visuals can start a repair.')
                previous=copy.deepcopy(job.get('result') or visuals['fragments'][index-1])
                pending={'status':'pending','script_version_id':script['script_version_id'],
                    'issues':copy.deepcopy(issues),'previous_result':previous,'attempt':None}
                job.setdefault('approval_history',[]).append(copy.deepcopy(pending))
                job['pending_repair']=pending
            job['status']='repair_pending'
        self.journal.save()  # Single authoritative revocation before any other write.
        visuals['status']='needs_visual'
        for index in sorted(indices):
            visuals['fragments'][index-1]={'fragment_index':index,'fragment_sha256':digest(script['fragments'][index-1]),
                'status':'needs_visual','reason':'Unfinished source/composition repair; previous approval revoked.'}
        atomic_write_json(self.directory/'visual_materials.json',visuals)

    def repair_approved(self, script, visuals, indices, issues):
        """Return final-review source issues to the same remaining framing/search budgets."""
        self.begin_repairs(script,visuals,indices,issues)
        for index in sorted(indices):
            fragment = script["fragments"][index - 1]
            key = digest({"fragment": fragment, "index": index})
            job = self.journal.state["jobs"][key]
            pending=job['pending_repair']
            issues=pending['issues']
            current = pending['previous_result']
            asset = current["asset"]
            cid = asset["candidate_id"]
            original = inside(self.directory, asset["original_path"])
            count = job["corrections"].get(cid, 0)
            maximum = self.settings["review"]["max_crop_corrections_per_candidate"]
            while count < maximum or pending.get('attempt') is not None:
                if pending.get('attempt') is None:
                    count += 1
                    job['corrections'][cid]=count
                    pending.update(status='in_progress',attempt=count)
                    self.journal.save()
                else:
                    count=pending['attempt']
                selection = self.ask(
                    "visual_select",
                    self.settings["selection"],
                    Selection,
                    {
                        "fragment": dict(fragment, fragment_index=index),
                        "candidates": [asset["source"]],
                        "output": self.settings["preparation"],
                        "final_issues": issues,
                        "previous_composition": asset["motion"]["composition"],
                        "correction_number": count,
                    },
                    [(cid, original)],
                )
                validate_selection(selection, [asset["source"]], index)
                if selection["status"] != "selected":
                    break
                attempt_dir = (
                    self.directory
                    / "visual_materials"
                    / key[:16]
                    / cid
                    / f"final_{count}"
                )
                try:
                    motion = prepare(
                        original,
                        selection["composition"],
                        attempt_dir,
                        tuple(
                            self.settings["preparation"]["output"][name]
                            for name in ("width", "height")
                        ),
                        self.settings["preparation"]["max_zoom_percent"],
                    )
                except VisualServiceError as error:
                    raise ProductionServiceError(str(error)) from None
                except (ProductionError, VisualError) as error:
                    job["rejections"].append(
                        {
                            "candidate_id": cid,
                            "reason": str(error),
                            "framing_attempt": count,
                        }
                    )
                    pending['attempt']=None
                    self.journal.save()
                    continue
                for preview in motion["previews"]:
                    preview["path"] = str(
                        Path(preview["path"]).relative_to(self.directory)
                    )
                review = self.ask(
                    "visual_review",
                    self.settings["review"],
                    Review,
                    {
                        "fragment": dict(fragment, fragment_index=index),
                        "source_metadata": asset["source"],
                        "motion": motion,
                        "final_issues": issues,
                    },
                    [("original " + cid, original)]
                    + [
                        (f'preview {p["position"]}', inside(self.directory, p["path"]))
                        for p in motion["previews"]
                    ],
                )
                if review["fragment_index"] != index:
                    raise ProductionError("Repair review fragment mismatch.")
                if review["decision"] == "approved":
                    repaired = {
                        **current,
                        "review": review,
                        "asset": {
                            **asset,
                            "motion": motion,
                            "changes": "EXIF orientation; "
                            + selection["composition"]["mode"]
                            + "; final approved correction "
                            + str(count),
                        },
                    }
                    job.update(
                        result=repaired,
                        status="approved",
                        approved_settings_id=digest(self.settings),
                    )
                    pending.update(status='completed',result_sha256=digest(repaired),attempt=None)
                    self.journal.save()
                    visuals["fragments"][index - 1] = repaired
                    break
                job["rejections"].append(
                    {
                        "candidate_id": cid,
                        "reason": "; ".join(review["issues"]),
                        "framing_attempt": count,
                    }
                )
                pending['attempt']=None
                self.journal.save()
                if review["decision"] != "fix_framing":
                    break
            if visuals["fragments"][index - 1]["status"] != "approved":
                job["status"] = "running"
                pending.update(status='replacement_required',attempt=None)
                for rd in job["rounds"]:
                    if "selection" in rd and cid in (
                        rd["selection"].get("primary_candidate_id"),
                        rd["selection"].get("backup_candidate_id"),
                    ):
                        if cid not in rd["attempted"]:
                            rd["attempted"].append(cid)
                self.journal.save()
                replacement = self.fragment(fragment, index)
                visuals["fragments"][index - 1] = replacement
            atomic_write_json(self.directory / "visual_materials.json", visuals)
        visuals["status"] = (
            "approved"
            if all(e["status"] == "approved" for e in visuals["fragments"])
            else "needs_visual"
        )
        atomic_write_json(self.directory / "visual_materials.json", visuals)
        if visuals["status"] != "approved":
            raise ProductionError(
                "Final source correction exhausted remaining visuals budgets."
            )
        return visuals

    def run(self, script):
        result = {
            "schema_version": 1,
            "mode": "real",
            "script_version_id": script["script_version_id"],
            "settings_snapshot": self.settings,
            "status": "running",
            "fragments": [],
        }
        path = self.directory / "visual_materials.json"
        for index, fragment in enumerate(script["fragments"], 1):
            try:
                entry = self.fragment(fragment, index)
            except ProductionServiceError as error:
                # Сохраняем принятые фрагменты, но не расходуем запросы после общего сбоя.
                result["service_error"] = str(error)
                for remaining in range(index, len(script["fragments"]) + 1):
                    result["fragments"].append(
                        {
                            "fragment_index": remaining,
                            "fragment_sha256": digest(
                                script["fragments"][remaining - 1]
                            ),
                            "status": "error" if remaining == index else "not_started",
                            "reason": (
                                str(error)
                                if remaining == index
                                else "Остановка после ошибки сервиса: " + str(error)
                            ),
                        }
                    )
                atomic_write_json(path, result)
                break
            except (ProductionError, VisualError) as error:
                entry = {
                    "fragment_index": index,
                    "fragment_sha256": digest(fragment),
                    "status": "error",
                    "reason": str(error),
                }
            result["fragments"].append(entry)
            atomic_write_json(path, result)
        result["status"] = (
            "approved"
            if all(e["status"] == "approved" for e in result["fragments"])
            else "needs_visual"
        )
        atomic_write_json(path, result)
        atomic_write_json(
            self.directory / "attribution.json",
            {
                "schema_version": 1,
                "script_version_id": script["script_version_id"],
                "images": [
                    {
                        "fragment_index": e["fragment_index"],
                        **e["asset"]["source"],
                        "changes": e["asset"]["changes"],
                    }
                    for e in result["fragments"]
                    if e["status"] == "approved"
                ],
            },
        )
        return path


def run_visual_materials(script_file, settings=None, model=None, *, script_snapshot=None):
    directory = (
        Path(script_file).parent if Path(script_file).is_file() else Path(script_file)
    )
    if script_snapshot is not None:
        frozen=directory/'branch_snapshots'/digest(script_snapshot)
        script=approved_script(frozen)
        if script!=script_snapshot:raise ProductionError('Parallel visual snapshot does not match its saved approval.')
    else:
        script=approved_script(directory)
    with run_lock(directory):
        chosen = settings or config()["visuals"]
        existing = directory / "visual_materials.json"
        if existing.exists():
            current = read(existing)
            if (
                current.get("status") == "approved"
                and current.get("script_version_id") == script["script_version_id"]
                and current.get("settings_snapshot") == chosen
            ):
                from editing_inputs import checked_visuals

                checked_visuals(script, current, directory)
                from visual_sequence import assert_unique_sources
                from visual_shots import approved_assets
                assert_unique_sources(list(approved_assets(current)))
                return existing
        if chosen.get('workflow_version') == 3:
            from fast_materials import FastMaterials
            result=FastMaterials(directory, chosen, model).run(script)
            from visual_sequence import assert_unique_sources
            from visual_shots import approved_assets
            assert_unique_sources(list(approved_assets(read(result))))
            return result
        if chosen.get('workflow_version') == 2:
            from shot_materials import ShotMaterials
            result=ShotMaterials(directory, chosen, model).run(script)
            from visual_sequence import assert_unique_sources
            from visual_shots import approved_assets
            assert_unique_sources(list(approved_assets(read(result))))
            return result
        result=VisualMaterials(directory, chosen, model).run(script)
        from visual_sequence import assert_unique_sources
        from visual_shots import approved_assets
        assert_unique_sources(list(approved_assets(read(result))))
        return result
