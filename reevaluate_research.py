"""Явная переоценка старого research.json: один запрос OpenAI, без нового веб-поиска."""

import argparse
import copy
import hashlib
import sys
from functools import partial
from pathlib import Path
from typing import Literal

from pydantic import Field

from artifacts import load_research, save_json, validate_notes, validate_research
from model_api import StrictModel, generate_structured
from research import ResearchError, save_research, render_research_markdown
from file_io import atomic_write, atomic_write_json
from research_policy import ASSESSMENT_INSTRUCTIONS, SCHEMA_VERSION, TopicAssessment, research_blockers


class LimitationReview(StrictModel):
    limitation_id: int
    classification: Literal["warning", "blocking_issue"]
    reason: str = Field(min_length=1)


class ReassessmentDraft(StrictModel):
    warnings: list[str]
    blocking_issues: list[str]
    topic_assessment: TopicAssessment
    limitation_reviews: list[LimitationReview]


def apply_reassessment(original: dict, draft: ReassessmentDraft) -> dict:
    """Каждая старая оговорка должна получить явную оценку; её текст остаётся неизменным."""
    reviews = draft.limitation_reviews
    ids = [review.limitation_id for review in reviews]
    if sorted(ids) != list(range(1, len(original["limitations"]) + 1)):
        raise ResearchError("Переоценка не принята: пропущены, повторены или добавлены номера исходных limitations.")
    result = copy.deepcopy(original)
    warnings = list(original.get("warnings", [])) + draft.warnings
    issues = list(draft.blocking_issues)
    for review in reviews:
        text = original["limitations"][review.limitation_id - 1]
        target = warnings if review.classification == "warning" else issues
        target.append(text)
    assessment = draft.topic_assessment.model_dump()
    blockers = research_blockers(result["facts"], result["sources"], assessment, issues)
    result.update(schema_version=SCHEMA_VERSION, warnings=list(dict.fromkeys(warnings)),
                  blocking_issues=blockers, topic_assessment=assessment,
                  status="insufficient_data" if blockers else "researched", fact_check_status="not_performed")
    # Статус не повышается только из-за числа фактов; validate проверит оценку и номера опорных фактов.
    validate_research(result)
    return result


def reevaluate_research(path: Path, output_root=None, generate=generate_structured) -> Path:
    """Сохраняет новый результат отдельно; исходный файл и его оговорки не переписывает."""
    path = Path(path)
    original = load_research(path, allow_legacy=True)
    if "schema_version" in original:
        raise ResearchError("Исследование уже имеет новый формат. Эта команда предназначена для старого research.json.")
    if "warnings" in original:
        validate_notes(original, "warnings")
    # Папка появляется до API: даже неудачная вспомогательная проверка имеет свой run_id.
    directory = save_research(original, output_root or path.parent / "reassessments")
    atomic_write_json(directory / "research_origin.json", {"source_path": str(path.resolve()),
        "source_run_directory": original.get("run_directory") or str(path.parent.resolve())})
    if generate is generate_structured:
        generate = partial(generate_structured, directory=directory, stage="research_reassessment")
    draft = generate(ReassessmentDraft, (
        "Переоцени сохранённое исследование по новым правилам. Не выполняй веб-поиск, "
        "не добавляй знания, факты или источники. Используй только сохранённые материалы. "
        "Не доверяй старому статусу: проверь, позволяют ли факты ответить на исходную тему. "
        "Классифицируй КАЖДУЮ старую limitations по её limitation_id как warning или blocking_issue "
        "и объясни решение. Не удаляй сомнения, не выдавай предположения за проверенные сведения. "
        + ASSESSMENT_INSTRUCTIONS
    ), {"saved_research": original, "limitations": [
        {"limitation_id": number, "text": text} for number, text in enumerate(original["limitations"], 1)]})
    result = apply_reassessment(original, draft)
    result["reevaluation"] = {"source_file": str(path.resolve()), "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                              "previous_status": original["status"], "criteria_version": SCHEMA_VERSION,
                              "kind": "saved_material_assessment_not_fact_check", "web_search_performed": False,
                              "limitation_reviews": [review.model_dump() for review in draft.limitation_reviews]}
    atomic_write_json(directory / "research.json", result)
    atomic_write(directory / "research.md", render_research_markdown(result).encode("utf-8"))
    save_json(directory / "run.json", {"mode": "real", "topic": result["topic"], "stage": "research_reassessment",
                                       "status": "research_ready" if result["status"] == "researched" else "stopped",
                                       "reason": "; ".join(result["blocking_issues"])})
    print(f"Переоценённое исследование: {directory / 'research.json'}")
    print(f"Статус: {result['status']}. Сценарий и остальные этапы не запускались.")
    return directory


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--research-file", required=True, type=Path)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.dry_run:
        print("План: переоценить сохранённые материалы одним запросом OpenAI, без веб-поиска и запуска конвейера.")
        print("Dry-run: файлы не читаются, API не вызывается.")
        return 0
    try:
        directory = reevaluate_research(args.research_file, output_root=args.output_root)
        result = load_research(directory / "research.json")
        return 0 if result["status"] == "researched" else 1
    except ResearchError as error:
        print(f"Переоценка остановлена: {error}", file=sys.stderr)
        return 1
    except OSError:
        print("Не удалось прочитать или сохранить переоценку исследования.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
