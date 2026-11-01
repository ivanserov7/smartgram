"""Five editing steps and one fail-closed final gate, resumable on accepted inputs."""

import json
import shutil
from pathlib import Path
from typing import Literal
from pydantic import Field, StrictInt
from model_api import StrictModel
from file_io import atomic_write_json
from media_tools import MediaError, inspect_tools, load_media_settings
from script_workflow import approved_script
from production_state import (
    Journal,
    ModelClient,
    ProductionError,
    UpstreamBlocked,
    config,
    digest,
    inside,
    read,
    run_lock,
    sha,
)
from editing_inputs import checked_audio, checked_visuals
from visual_shots import shot_timeline, asset_dependencies
from visual_sequence import (
    first_appearance_sequence,
    validate_sequence,
    source_identity,
    POLICY,
)
from subtitles import Segmentation, layout, materialize, script_tokens
from video_render import (
    compose,
    extract_frames,
    render_video,
    sample_schedule,
    schedule,
    technical_check,
)


class FinalIssue(StrictModel):
    frame_id: str
    fragment_index: StrictInt
    cue_id: StrictInt | None
    problem: str
    correction: str
    target_step: Literal[
        "subtitle_segment", "subtitle_layout", "video_render", "visual_materials"
    ]


class Appearance(StrictModel):
    vertical_shift_pixels: float
    background_opacity: float = Field(ge=0.5, le=1)
    outline_pixels: float = Field(ge=2, le=6)


class FinalReview(StrictModel):
    decision: Literal["approved", "needs_fix", "uncertain"]
    reason: str | None
    reviewed_frame_ids: list[str]
    issues: list[FinalIssue]
    subtitle_appearance: Appearance | None


def validate_batch(response, batch):
    expected = {f["frame_id"]: f for f in batch}
    actual = response["reviewed_frame_ids"]
    if set(actual) != set(expected) or len(actual) != len(expected):
        raise ProductionError(
            "Final review skipped, duplicated or invented a frame ID."
        )
    for issue in response["issues"]:
        sample = expected.get(issue["frame_id"])
        if (
            sample is None
            or issue["fragment_index"] != sample["fragment_index"]
            or issue["cue_id"] != sample["cue_id"]
        ):
            raise ProductionError("Final issue IDs do not match actual frame metadata.")
    if response["decision"] == "approved" and response["issues"]:
        raise ProductionError("Approved final batch contains unresolved issues.")
    if response["decision"] == "needs_fix" and not response["issues"]:
        raise ProductionError("needs_fix without concrete issues.")
    return response


def ready(technical, batches, expected_ids):
    actual = [
        frame for batch in batches for frame in batch.get("reviewed_frame_ids", [])
    ]
    return (
        technical.get("passed") is True
        and bool(batches)
        and all(
            b.get("decision") == "approved" and not b.get("issues") for b in batches
        )
        and len(actual) == len(expected_ids)
        and set(actual) == set(expected_ids)
        and len(actual) == len(set(actual))
    )


class EditingRun:
    def __init__(self, directory, settings=None, model=None, media=None):
        self.directory = Path(directory)
        self.settings = settings or config()["editing"]
        self.journal = Journal(directory, "editing", self.settings)
        self.model = model or ModelClient(directory)
        self.media = media
        self.compute = []
        compute_path = self.directory / "render_compute.json"
        self.previous_compute = (
            read(compute_path).get("operations", []) if compute_path.exists() else []
        )

    def ask(self, step, settings, schema, data, images=()):
        return self.journal.operation(
            step,
            {
                "settings": settings,
                "schema": schema.model_json_schema(),
                "data": data,
                "images": [
                    {"label": label, "sha256": sha(path)} for label, path in images
                ],
            },
            lambda: self.model(step, settings, schema, data, images),
        )

    def segment(self, script, words, fragments, job, feedback=None):
        from subtitle_binding import binding

        proof = binding(
            script,
            words,
            fragments,
            self.settings["subtitle_segment"].get("equivalences", {}),
        )
        tokens = script_tokens(script)
        data = {
            "exact_script": script,
            "numbered_script_tokens": tokens,
            "audio_words_with_ids_and_times": words,
            "verified_tokenization_equivalences": self.settings["subtitle_segment"].get(
                "equivalences", {}
            ),
            "approved_fragment_word_ranges": fragments,
            "verified_script_audio_binding": proof,
            "text_authority": "OpenAI already confirmed the script; Deepgram text differences cannot veto it. Cues may only cut at paired block boundaries.",
            "layout_settings": self.settings["subtitle_layout"],
            "feedback": feedback,
            "status_contract": "Return status=ok and exact cues OR uncertain/blocked with reason and cues=[].",
            "previous_cues": job.get("segmentation") if feedback else None,
        }
        result = self.ask(
            "subtitle_segment", self.settings["subtitle_segment"], Segmentation, data
        )
        if (
            feedback
            and result["status"] == "ok"
            and job.get("segmentation", {}).get("status") == "ok"
        ):
            allowed = feedback.get("problem_cues_and_neighbours")
            if allowed:
                ranges = [(c["first_token_id"], c["last_token_id"]) for c in allowed]

                def stable(cue):
                    return {k: v for k, v in cue.items() if k != "cue_id"}

                for old in job["segmentation"]["cues"]:
                    if any(
                        a <= old["first_token_id"] <= b
                        or old["first_token_id"] <= a <= old["last_token_id"]
                        for a, b in ranges
                    ):
                        continue
                    matches = [
                        new
                        for new in result["cues"]
                        if new["first_token_id"] == old["first_token_id"]
                    ]
                    if len(matches) != 1 or stable(matches[0]) != stable(old):
                        raise ProductionError(
                            "Subtitle correction changed an unaffected group."
                        )
        job["segmentation"] = result
        self.journal.save()
        return result

    def restore_compatible_work(self, job, render_id, script, words, fragments, proof):
        """Локальная правка layout не оплачивает заново разметку и не рендерит прежний silent.mp4."""
        if not job.get("video_render") and (self.directory / "silent.mp4").exists():
            for saved in self.journal.state["jobs"].values():
                record = saved.get("video_render", {})
                if record.get("id") == render_id and record.get("sha256") == sha(
                    self.directory / "silent.mp4"
                ):
                    job["video_render"] = dict(record)
                    break
        if job.get("segmentation"):
            return
        for key, op in reversed(list(self.journal.state["operations"].items())):
            if (
                op["step"] != "subtitle_segment"
                or op["status"] != "completed"
                or op.get("result", {}).get("status") != "ok"
            ):
                continue
            data = op["identity"]["data"]
            if (
                data.get("exact_script") != script
                or data.get("audio_words_with_ids_and_times") != words
                or data.get("approved_fragment_word_ranges") != fragments
            ):
                continue
            from copy import deepcopy

            result = deepcopy(op["result"])
            try:
                materialize(
                    result,
                    script,
                    words,
                    fragments,
                    self.settings["subtitle_segment"].get("equivalences", {}),
                    binding_proof=proof,
                )
            except ProductionError:
                continue
            job["segmentation"] = result
            job["restored_segmentation_operation"] = key
            self.journal.save()
            return

    def execute(self, script, audio, visuals, allow_test=False):
        root = self.directory
        audio_path, duration, words, fragments = checked_audio(
            script, audio, root, allow_test
        )
        from subtitle_binding import binding

        binding_proof = binding(
            script,
            words,
            fragments,
            self.settings["subtitle_segment"].get("equivalences", {}),
        )
        atomic_write_json(root / "subtitle_binding.json", binding_proof)
        assets, size = checked_visuals(script, visuals, root)
        visual_timing = shot_timeline(
            script,
            visuals,
            fragments,
            words,
            self.settings["subtitle_segment"].get("equivalences", {}),
            binding_proof=binding_proof,
        )
        raw_shot_count = len(assets)
        assets, visual_timing = first_appearance_sequence(assets, visual_timing)
        scenes, total = schedule(
            visual_timing,
            duration,
            self.settings["video_render"]["fps"],
            self.settings["video_render"]["transition"],
            self.settings["video_render"]["transition_ms"],
        )
        validate_sequence(assets, scenes)
        plan = {
            "schema_version": 1,
            "policy": POLICY,
            "script_version_id": script["script_version_id"],
            "audio_sha256": audio["audio_sha256"],
            "source_visuals_sha256": digest(visuals),
            "raw_shot_count": raw_shot_count,
            "unique_source_count": len(assets),
            "duration_seconds": duration,
            "sections": scenes,
            "assets": [
                {
                    "source_identity": source_identity(a),
                    "original_path": a["original_path"],
                    "original_sha256": a["original_sha256"],
                    "motion": a["motion"],
                }
                for a in assets
            ],
            "approval_scope": "Existing framing retained; merged timeline is a new montage adaptation, awaiting final video check.",
        }
        atomic_write_json(root / "montage_plan.json", plan)
        media = self.media or load_media_settings()
        tools = inspect_tools(media, self.compute)
        if self.settings["subtitle_layout"]["font"] != tools["font_family"]:
            raise ProductionError(
                "Configured subtitle family differs from actual measured font; explicitly set font="
                + tools["font_family"]
            )
        identity = {
            "script": script,
            "audio": audio,
            "visuals": visuals,
            "settings": self.settings,
            "montage_plan": plan,
            "font_sha256": sha(media.font),
            "tools": tools["versions"],
            "test": allow_test,
        }
        key = digest(identity)
        job = self.journal.state["jobs"].setdefault(
            key, {"status": "started", "render_rounds": []}
        )
        # Correction budgets depend on the accepted version/input, never on an invocation or model configuration.
        family = digest(
            {
                "script": script["script_version_id"],
                "audio": audio["audio_sha256"],
                "words": words,
            }
        )
        budget = self.journal.state.setdefault("budgets", {}).setdefault(
            family, {"subtitle_groups": {}, "final_corrections": 0}
        )
        manifest = {
            "schema_version": 1,
            "mode": "test" if allow_test else "real",
            "status": "running",
            "input_id": key,
            "script_version_id": script["script_version_id"],
            "audio_sha256": audio["audio_sha256"],
            "settings_snapshot": self.settings,
            "tools": tools,
            "font": {
                "path": str(media.font),
                "family": tools["font_family"],
                "sha256": sha(media.font),
            },
            "fps": self.settings["video_render"]["fps"],
            "scenes": scenes,
            "total_frames": total,
            "duration_seconds": duration,
            "montage_plan": "montage_plan.json",
            "unique_source_count": len(assets),
            "size": size,
            "test_only": allow_test,
            "reason": None,
        }
        # All file dependencies and authoritative manifests are included in the final hash check.
        hashes = {audio["audio_path"]: audio["audio_sha256"]}
        for name in [
            "script.json",
            "script_state.json",
            "audio_manifest.json",
            "visual_materials.json",
            "montage_plan.json",
        ]:
            if (root / name).exists():
                hashes[name] = sha(root / name)
        for asset in assets:
            for dependency in asset_dependencies(asset):
                hashes[dependency] = sha(root / dependency)
            for preview in asset["motion"]["previews"]:
                hashes[preview["path"]] = preview["sha256"]
        manifest["input_hashes"] = hashes
        path = root / "render_manifest.json"
        atomic_write_json(path, manifest)
        attribution = {
            "schema_version": 1,
            "script_version_id": script["script_version_id"],
            "images": [
                {
                    "fragment_index": a.get("fragment_index", i),
                    "shot_id": a.get("shot_id"),
                    **a["source"],
                    "changes": a["changes"],
                    "covered_shots": scenes[i - 1].get("covered_shots", []),
                }
                for i, a in enumerate(assets, 1)
            ],
        }
        atomic_write_json(root / "attribution.json", attribution)
        if job.get("status") in {"ready", "test_passed"}:
            intact = all(
                (root / name).is_file() and sha(root / name) == expected
                for name, expected in job["output_hashes"].items()
            )
            if intact:
                validate_sequence(assets, scenes)
                if job.get("montage_plan_sha256") != sha(root / "montage_plan.json"):
                    raise ProductionError("Montage plan changed after saved approval.")
                manifest.update(
                    status=job["status"], output_hashes=job["output_hashes"]
                )
                atomic_write_json(path, manifest)
                return path
            # Rebuild/recheck changed local outputs from the accepted inputs and persisted
            # model results. Neither a missing file nor an old ready flag bypasses the gate.
            job["status"] = "started"
            self.journal.save()
        render_id = digest(
            {"scenes": scenes, "assets": assets, "video": self.settings["video_render"]}
        )
        self.restore_compatible_work(
            job, render_id, script, words, fragments, binding_proof
        )
        render_record = job.get("video_render")
        if (
            not render_record
            or render_record["id"] != render_id
            or not (root / "silent.mp4").exists()
            or sha(root / "silent.mp4") != render_record["sha256"]
        ):
            validate_sequence(assets, scenes)
            render_video(
                root,
                assets,
                scenes,
                total,
                size,
                self.settings["video_render"],
                media,
                self.compute,
            )
            job["video_render"] = {"id": render_id, "sha256": sha(root / "silent.mp4")}
            self.journal.save()
        appearance = job.get("appearance")
        segmentation = job.get("segmentation")
        if not segmentation:
            segmentation = self.segment(script, words, fragments, job)
        while True:
            # Disallow stale public ready output while correcting/rechecking a candidate.
            self.preserve_release()
            errors = None
            cues = None
            try:
                cues = materialize(
                    segmentation,
                    script,
                    words,
                    fragments,
                    self.settings["subtitle_segment"].get("equivalences", {}),
                    binding_proof=binding_proof,
                )
            except ProductionError as error:
                errors = {"mapping_error": str(error)}
            if cues:
                report = layout(
                    root,
                    cues,
                    self.settings["subtitle_layout"],
                    media,
                    tools["font_family"],
                    size,
                    appearance,
                    self.compute,
                    verified_words=words,
                    binding_proof=binding_proof,
                )
                # Проверяем покрытие текста и парные привязки после локальной вёрстки.
                materialize(
                    {"status": "ok", "reason": None, "cues": cues},
                    script,
                    words,
                    fragments,
                    self.settings["subtitle_segment"].get("equivalences", {}),
                    binding_proof=binding_proof,
                )
                if not report["passed"]:
                    errors = {"layout": report["issues"]}
            if errors:
                if segmentation.get("status") != "ok":
                    raise ProductionError(str(errors))
                groups = errors.get("layout", [])
                # Affected semantic group is the fragment, stable across cue splits/IDs.
                affected = (
                    {
                        next(
                            c["fragment_index"]
                            for c in cues
                            if c["cue_id"] == issue["cue_id"]
                        )
                        for issue in groups
                    }
                    if groups
                    else set(range(1, len(fragments) + 1))
                )
                for group in affected:
                    counter = str(group)
                    used = budget["subtitle_groups"].get(counter, 0)
                    if (
                        used
                        >= self.settings["subtitle_layout"][
                            "max_corrections_per_affected_group"
                        ]
                    ):
                        raise ProductionError(
                            "Subtitle constraints incompatible; correction budget exhausted for fragment "
                            + counter
                        )
                for group in affected:
                    counter = str(group)
                    budget["subtitle_groups"][counter] = (
                        budget["subtitle_groups"].get(counter, 0) + 1
                    )
                self.journal.save()
                neighbours = []
                if cues:
                    bad = {issue["cue_id"] for issue in groups}
                    for i, cue in enumerate(cues):
                        if cue["cue_id"] in bad:
                            neighbours.extend(cues[max(0, i - 1) : i + 2])
                segmentation = self.segment(
                    script,
                    words,
                    fragments,
                    job,
                    {
                        "measurements": errors,
                        "problem_cues_and_neighbours": neighbours,
                        "corrections_used": budget["subtitle_groups"],
                        "preserve_other_groups": True,
                    },
                )
                continue
            atomic_write_json(
                root / "subtitle_cues.json",
                {"cues": cues, "segmentation": segmentation},
            )
            render_id = digest(
                {
                    "scenes": scenes,
                    "assets": assets,
                    "video": self.settings["video_render"],
                }
            )
            render_record = job.get("video_render")
            if (
                not render_record
                or render_record["id"] != render_id
                or not (root / "silent.mp4").exists()
                or sha(root / "silent.mp4") != render_record["sha256"]
            ):
                render_video(
                    root,
                    assets,
                    scenes,
                    total,
                    size,
                    self.settings["video_render"],
                    media,
                    self.compute,
                )
                job["video_render"] = {
                    "id": render_id,
                    "sha256": sha(root / "silent.mp4"),
                }
                self.journal.save()
            music = self.settings["compose"]["music"]
            if music["enabled"]:
                from video_music import prepare_music

                audio_for_compose = prepare_music(
                    root, audio_path, duration, music, media, self.compute
                )
                music_manifest_name = f'music/{music["asset_id"]}.json'
                music_manifest = read(inside(root, music_manifest_name))
                hashes[music_manifest_name] = sha(inside(root, music_manifest_name))
                hashes[music_manifest["path"]] = sha(
                    inside(root, music_manifest["path"])
                )
                attribution["music"] = music_manifest
                atomic_write_json(root / "attribution.json", attribution)
                manifest["input_hashes"] = hashes
                atomic_write_json(path, manifest)
            else:
                audio_for_compose = audio_path
            compose_id = digest(
                {
                    "silent": sha(root / "silent.mp4"),
                    "audio": sha(audio_for_compose),
                    "ass": sha(root / "subtitles.ass"),
                    "settings": self.settings["compose"],
                }
            )
            previous = job.get("compose")
            if (
                not previous
                or previous["id"] != compose_id
                or not (root / "candidate.mp4").exists()
                or sha(root / "candidate.mp4") != previous["sha256"]
            ):
                candidate, volume = compose(
                    root,
                    audio_for_compose,
                    self.settings["compose"],
                    media,
                    self.compute,
                )
                job["compose"] = {
                    "id": compose_id,
                    "sha256": sha(candidate),
                    "source_loudness": volume,
                }
                self.journal.save()
            candidate = root / "candidate.mp4"
            attempts = root / "editing_attempts"
            attempts.mkdir(exist_ok=True)
            retained = attempts / (sha(candidate) + ".mp4")
            if not retained.exists():
                shutil.copyfile(candidate, retained)
            technical = technical_check(
                candidate,
                audio_path,
                duration,
                size,
                self.settings["video_render"]["fps"],
                total,
                media,
                hashes,
                root,
                self.compute,
            )
            quality = {
                "schema_version": 1,
                "mode": manifest["mode"],
                "candidate_sha256": sha(candidate),
                "technical": technical,
                "visual_batches": [],
                "ready": False,
                "scope": "Full technical decode; sampled visual readability/occlusion only. No listening, pronunciation or continuous-motion claim.",
            }
            atomic_write_json(root / "quality_report.json", quality)
            if not technical["passed"]:
                job["failed_step"] = (
                    "compose"
                    if "loudness" in technical.get("errors", [])
                    else "video_render"
                )
                self.journal.save()
                raise ProductionError(
                    "final_check technical failed; visual review skipped: "
                    + str(technical["errors"])
                )
            samples = sample_schedule(
                cues, scenes, total, self.settings["video_render"]["fps"]
            )
            frame_root = root / "final_frames" / sha(candidate)[:16]
            extract_frames(candidate, frame_root, samples, media, self.compute)
            from video_schedule_check import check_schedule

            schedule_report = check_schedule(
                root,
                assets,
                scenes,
                samples,
                size,
                media,
                self.compute,
                fps=self.settings["video_render"]["fps"],
            )
            technical["checks"]["unique_visual_sources"] = validate_sequence(
                assets, scenes
            )
            technical["checks"]["actual_scene_and_subtitle_schedule"] = schedule_report[
                "passed"
            ]
            technical["schedule_measurements"] = schedule_report
            technical["passed"] = technical["passed"] and schedule_report["passed"]
            if not technical["passed"]:
                quality["technical"] = technical
                atomic_write_json(root / "quality_report.json", quality)
                raise ProductionError(
                    "final_check actual scene/subtitle schedule failed; visual review skipped."
                )
            batches = []
            for start in range(0, len(samples), 12):
                batch = samples[start : start + 12]
                data = {
                    "frames": [
                        {k: v for k, v in f.items() if k != "path"} for f in batch
                    ],
                    "fragments": script["fragments"],
                    "visual_sections": scenes,
                    "must_keep": [
                        a["motion"]["composition"]["must_keep"] for a in assets
                    ],
                    "subtitle_settings": self.settings["subtitle_layout"],
                    "technical_report": technical,
                    "current_appearance": appearance,
                    "appearance_constraints": {
                        "vertical_shift_pixels": [-size[1] * 0.1, size[1] * 0.1],
                        "background_opacity": [0.5, 1],
                        "outline_pixels": [2, 6],
                        "font_size_unchanged": True,
                    },
                    "status_contract": "reviewed_frame_ids must list every supplied frame exactly once; approved has no issues.",
                }
                response = self.ask(
                    "final_check",
                    self.settings["final_check"]["visual"],
                    FinalReview,
                    data,
                    [(f["frame_id"], Path(f["path"])) for f in batch],
                )
                validate_batch(response, batch)
                batches.append(response)
                quality["visual_batches"] = batches
                atomic_write_json(root / "quality_report.json", quality)
            quality["samples"] = samples
            if ready(technical, batches, [f["frame_id"] for f in samples]):
                # Recheck candidate and inputs after API calls; TOCTOU cannot turn a different file into ready.
                if sha(candidate) != quality["candidate_sha256"] or any(
                    sha(inside(root, n)) != v for n, v in hashes.items()
                ):
                    raise ProductionError(
                        "Inputs/candidate changed during final review."
                    )
                quality["ready"] = not allow_test
                quality["test_passed"] = allow_test
                shutil.copyfile(
                    candidate, root / ("test_video.mp4" if allow_test else "video.mp4")
                )
                status = "test_passed" if allow_test else "ready"
                outputs = [
                    "test_video.mp4" if allow_test else "video.mp4",
                    "subtitles.srt",
                    "subtitles.ass",
                    "attribution.json",
                    "quality_report.json",
                    "montage_plan.json",
                ]
                atomic_write_json(root / "quality_report.json", quality)
                job.update(
                    status=status,
                    montage_plan_sha256=sha(root / "montage_plan.json"),
                    output_hashes={name: sha(root / name) for name in outputs},
                )
                self.journal.save()
                manifest.update(
                    status=status,
                    montage_plan_sha256=sha(root / "montage_plan.json"),
                    output_hashes=job["output_hashes"],
                    quality_report="quality_report.json",
                )
                atomic_write_json(path, manifest)
                print(
                    f"Готовый MP4: {root / ('test_video.mp4' if allow_test else 'video.mp4')}",
                    flush=True,
                )
                return path
            atomic_write_json(root / "quality_report.json", quality)
            if any(b["decision"] == "uncertain" for b in batches):
                raise ProductionError("Final visual review uncertain; no release.")
            if sha(candidate) != quality["candidate_sha256"] or any(
                sha(inside(root, n)) != v for n, v in hashes.items()
            ):
                raise ProductionError(
                    "Inputs/candidate changed during final review; visual repair not sent."
                )
            issues = [issue for batch in batches for issue in batch["issues"]]
            targets = {issue["target_step"] for issue in issues}
            from visual_repair import normalized_issues

            correction_id = digest(
                {
                    "candidate_sha256": sha(candidate),
                    "issues": normalized_issues(issues),
                }
            )
            reserved = budget.setdefault("final_correction_ids", [])
            if correction_id not in reserved:
                if (
                    budget["final_corrections"]
                    >= self.settings["final_check"]["max_correction_rounds"]
                ):
                    raise ProductionError(
                        f"Лимит финальных исправлений: {budget['final_corrections']}/{self.settings['final_check']['max_correction_rounds']}; видео не выпущено."
                    )
                budget["final_corrections"] += 1
                reserved.append(correction_id)
                self.journal.save()  # Резерв и ID записаны вместе: resume не расходует второй раунд.
            job["failed_step"] = ",".join(sorted(targets))
            # Route source/transition fixes back explicitly. Never silently change an approved crop.
            if targets & {"video_render", "visual_materials"}:
                atomic_write_json(
                    root / "correction_request.json",
                    {
                        "target_steps": sorted(targets),
                        "issues": issues,
                        "correction_round": budget["final_corrections"],
                        "source_audio_preserved": True,
                        "candidate_sha256": sha(candidate),
                        "correction_id": correction_id,
                    },
                )
                # A rendering/composition issue returns to the existing visual workflow;
                # the select/review models explicitly approve new motion before a new render.
                if (root / "assembly_inputs.json").exists():
                    raise ProductionError(
                        "Final check requested a visual change; saved-media assembly forbids new search/generation. See correction_request.json; audio and originals retained."
                    )
                from visual_materials import VisualMaterials

                if visuals.get("schema_version") == 2:
                    from shot_materials import ShotMaterials as VisualMaterials

                if visuals.get("settings_snapshot", {}).get("workflow_version") == 3:
                    from fast_materials import FastMaterials as VisualMaterials

                affected = {
                    issue["fragment_index"]
                    for issue in issues
                    if issue["target_step"] in {"video_render", "visual_materials"}
                }
                visual_settings = visuals["settings_snapshot"]
                corrected = VisualMaterials(
                    root, visual_settings, self.model
                ).repair_approved(script, visuals, affected, issues)
                return self.execute(script, audio, corrected, allow_test)
            if "subtitle_segment" in targets:
                segmentation = self.segment(
                    script,
                    words,
                    fragments,
                    job,
                    {
                        "final_issues": issues,
                        "correction_round": budget["final_corrections"],
                    },
                )
            if "subtitle_layout" in targets:
                proposals = [
                    b["subtitle_appearance"]
                    for b in batches
                    if b["subtitle_appearance"] is not None
                ]
                if not proposals or any(p != proposals[0] for p in proposals):
                    raise ProductionError(
                        "No unambiguous safe subtitle-position correction supplied."
                    )
                appearance = proposals[0]
                job["appearance"] = appearance
                self.journal.save()
            # Next loop recreates/burns subtitles and rechecks all final frames, reusing paid upstream steps.

    def preserve_release(self):
        path = self.directory / "video.mp4"
        if path.exists():
            history = self.directory / "accepted_video_history"
            history.mkdir(exist_ok=True)
            retained = history / (sha(path) + ".mp4")
            if not retained.exists():
                shutil.copyfile(path, retained)
            path.unlink()

    def run(self, script=None, audio=None, visuals=None, allow_test=False):
        path = self.directory / "render_manifest.json"
        try:
            if script is None:
                from research import ResearchError

                try:
                    script = approved_script(self.directory)
                except ResearchError as error:
                    raise UpstreamBlocked(str(error)) from None
            if audio is None:
                if not (self.directory / "audio_manifest.json").exists():
                    raise UpstreamBlocked("Accepted audio_manifest.json missing.")
                audio = read(self.directory / "audio_manifest.json")
            # Check audio first: report concrete upstream failure even when visuals are still searching.
            checked_audio(script, audio, self.directory, allow_test)
            if visuals is None:
                if not (self.directory / "visual_materials.json").exists():
                    raise UpstreamBlocked("Approved visual_materials.json missing.")
                visuals = read(self.directory / "visual_materials.json")
            # При остановке ремонта сначала продолжаем его журнал; старый approved не возвращается.
            state_path = self.directory / "visual_materials_state.json"
            if (
                visuals.get("settings_snapshot", {}).get("workflow_version") == 3
                and state_path.exists()
            ):
                pending = any(
                    p.get("status") != "completed"
                    or p.get("result_sha256")
                    != digest(visuals["fragments"][j["fragment_index"] - 1])
                    for j in read(state_path)["jobs"].values()
                    if (p := j.get("pending_repair"))
                    and p.get("script_version_id") == script["script_version_id"]
                )
                if pending:
                    from fast_materials import FastMaterials

                    request = read(self.directory / "correction_request.json")
                    visuals = FastMaterials(
                        self.directory, visuals["settings_snapshot"], self.model
                    ).repair_approved(script, visuals, set(), request["issues"])
            return self.execute(script, audio, visuals, allow_test)
        except (ProductionError, MediaError) as error:
            old = (
                read(path)
                if path.exists()
                else {"schema_version": 1, "mode": "test" if allow_test else "real"}
            )
            old.update(
                status=(
                    "blocked_upstream"
                    if isinstance(error, UpstreamBlocked)
                    else "blocked"
                ),
                reason=str(error),
            )
            old.pop("output_hashes", None)
            atomic_write_json(path, old)
            quality_path = self.directory / "quality_report.json"
            quality = (
                read(quality_path) if quality_path.exists() else {"schema_version": 1}
            )
            if quality.get("ready") or quality.get("test_passed"):
                history = self.directory / "quality_history"
                history.mkdir(exist_ok=True)
                retained = history / (sha(quality_path) + ".json")
                if not retained.exists():
                    shutil.copyfile(quality_path, retained)
            quality.update(
                ready=False, test_passed=False, status=old["status"], reason=str(error)
            )
            atomic_write_json(quality_path, quality)
            self.preserve_release()
            raise
        finally:
            atomic_write_json(
                self.directory / "render_compute.json",
                {
                    "operations": self.previous_compute + self.compute,
                    "api_cost_usd": None,
                    "note": "Local compute is separate from provider usage. Dollar costs only from Costs API.",
                },
            )


def run_editing(directory, settings=None, model=None):
    directory = Path(directory)
    with run_lock(directory):
        from progress import stage_progress

        with stage_progress("Монтаж и финальная проверка"):
            return EditingRun(directory, settings, model).run()
