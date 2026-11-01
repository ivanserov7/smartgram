"""Persistent, content-addressed operations shared by visuals and editing."""

import base64
import fcntl
import hashlib
import io
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path

from PIL import Image, ImageOps
from dotenv import dotenv_values
from openai import OpenAI, APIError
from file_io import atomic_write_json
from openai_meter import OpenAIMeter
from billing_api import redact

ROOT = Path(__file__).resolve().parent


class ProductionError(Exception):
    pass


class ProductionServiceError(ProductionError):
    """Общая ошибка доступа/сети: дальнейшие запросы не исправят её."""


class UpstreamBlocked(ProductionError):
    pass


def digest(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()
    ).hexdigest()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def inside(root, relative):
    path = (Path(root) / relative).resolve()
    if not path.is_relative_to(Path(root).resolve()) or not path.is_file():
        raise ProductionError("Missing input or path outside run: " + str(relative))
    return path


def config():
    value = read(Path(os.environ.get("SMARTGRAM_RUN_CONFIG", ROOT / "visuals_editing_config.json")))
    # Prompt files are authoritative editable copies of the complete specification.
    for stage, step, name in [
        ("visuals", "queries", "visual_queries"),
        ("visuals", "selection", "visual_select"),
        ("visuals", "review", "visual_review"),
        ("editing", "subtitle_segment", "subtitle_segment"),
    ]:
        value[stage][step]["prompt"] = (
            (ROOT / "prompts" / f"{name}.txt").read_text().rstrip("\n")
        )
    value["editing"]["final_check"]["visual"]["prompt"] = (
        (ROOT / "prompts/final_check.txt").read_text().rstrip("\n")
    )
    # Local fixes invalidate render caches even if provider inputs have not changed.
    # Per-fragment/search correction counters remain independent of these signatures.
    for stage, names in {
        "visuals": [
            "visual_materials.py",
            "visual_shots.py",
            "visual_sequence.py",
            "shot_materials.py",
            "fast_materials.py",
            "visual_repair.py",
            "visual_generation.py",
            "commons_requests.py",
            "commons_materials.py",
            "pexels_materials.py",
            "visual_geometry.py",
            "commons_api.py",
            "image_files.py",
            "production_state.py",
        ],
        "editing": [
            "editing.py",
            "editing_inputs.py",
            "visual_repair.py",
            "visual_sequence.py",
            "video_render.py",
            "video_schedule_check.py",
            "subtitles.py",
            "subtitle_binding.py",
            "video_music.py",
            "media_tools.py",
            "production_state.py",
        ],
    }.items():
        value[stage]["implementation_sha256"] = {
            name: sha(ROOT / name) for name in names
        }
    return value


@contextmanager
def run_lock(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".production.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ProductionError(
                "Another production command is running in this directory."
            ) from None
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


class Journal:
    def __init__(self, directory, name, snapshot):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / f"{name}_state.json"
        self.state = (
            read(self.path)
            if self.path.exists()
            else {"schema_version": 1, "operations": {}, "jobs": {}, "snapshots": {}}
        )
        if self.state.get("schema_version") != 1:
            raise ProductionError("Incompatible production state; source retained.")
        self.snapshot_id = digest(snapshot)
        self.state["snapshots"][self.snapshot_id] = snapshot
        self.save()

    def save(self):
        atomic_write_json(self.path, self.state)

    def operation(self, step, identity, function):
        key = digest({"step": step, "identity": identity})
        previous = self.state["operations"].get(key)
        if previous:
            if previous["status"] == "completed":
                return previous["result"]
            raise (
                ProductionServiceError
                if previous.get("service_error")
                else ProductionError
            )(
                f'{step}: previous attempt {previous["status"]}; automatic repetition disabled. {previous.get("reason", "Interrupted operation; cost unknown.")}'
            )
        entry = {
            "step": step,
            "identity": identity,
            "status": "started",
            "started_at": time.time(),
            "cost_usd": None,
        }
        self.state["operations"][key] = entry
        self.save()  # Reserve before any network action, including failures and interruptions.
        started = time.monotonic()
        try:
            result = function()
            entry.update(status="completed", result=result)
            return result
        except Exception as error:
            entry.update(
                service_error=isinstance(error, ProductionServiceError),
                status="failed",
                reason=(
                    str(error)
                    if isinstance(error, ProductionError)
                    else type(error).__name__
                ),
            )
            raise
        finally:
            entry["duration_seconds"] = time.monotonic() - started
            self.save()


def image_part(path, label, side=1536, detail="high"):
    # Full originals remain local and hashed; bounded image input is explicitly a vision proxy.
    with Image.open(path) as source:
        image = ImageOps.exif_transpose(source).convert("RGB")
        image.thumbnail((side, side), Image.Resampling.LANCZOS)
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=90)
    return [
        {"type": "input_text", "text": label},
        {
            "type": "input_image",
            "detail": detail,
            "image_url": "data:image/jpeg;base64,"
            + base64.b64encode(output.getvalue()).decode(),
        },
    ]


class ModelClient:
    def __init__(self, directory):
        self.directory = Path(directory)

    def __call__(self, step, settings, schema, data, images=()):
        values = dotenv_values(ROOT / ".env", interpolate=False)
        api_key = values.get("OPENAI_API_KEY")
        if not api_key:
            raise ProductionError("OPENAI_API_KEY missing; no request sent.")
        content = [
            {
                "type": "input_text",
                "text": json.dumps(data, ensure_ascii=False, allow_nan=False),
            }
        ]
        for label, path in images:
            content.extend(image_part(path, label, settings.get("image_size", 1536), settings.get("image_detail", "high")))
        body = {
            "model": settings["model"],
            "reasoning": {"effort": settings["reasoning_effort"]},
            "instructions": settings["prompt"],
            "input": [{"role": "user", "content": content}],
            "store": False,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": step,
                    "strict": True,
                    "schema": schema.model_json_schema(),
                }
            },
        }
        record = {
            "step": step,
            "stage": "visuals" if step.startswith("visual_") else "editing",
            "parameters": {key: body[key] for key in ("model", "reasoning", "store")},
            "request": data,
            "instructions": settings["prompt"],
            "schema": schema.model_json_schema(),
            "images": [{"label": label, "sha256": sha(path)} for label, path in images],
            "cost_usd": None,
        }
        meter = OpenAIMeter(self.directory, record["stage"], api_key, record=record)
        meter.telemetry["step"] = step
        try:
            with OpenAI(
                api_key=api_key,
                timeout=settings["timeout_seconds"],
                max_retries=settings["api_retries"],
            ) as client:
                response = meter.create(client, **body)
            record["response"] = response.model_dump(mode="json")
            if response.status != "completed" or not response.output_text:
                raise ProductionError(step + ": incomplete response; no approval.")
            result = schema.model_validate_json(response.output_text).model_dump()
            record["status"] = "completed"
            meter.save()
            return result
        except Exception as error:
            meter.processing_failed(error)
            details = redact(getattr(error, "body", None), (api_key,))
            error_type = (
                ProductionServiceError
                if isinstance(error, APIError)
                else ProductionError
            )
            raise error_type(
                f'{step}: {type(error).__name__}: {details or "request/response validation failed"}'
            ) from None
