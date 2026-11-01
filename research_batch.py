"""Последовательно запускает только исследователя на постоянном наборе тем."""

import argparse
import json
import time
from pathlib import Path
from tempfile import mkdtemp

from file_io import atomic_write_json
from research import ResearchError
from research_batch_report import build_batch_report
from research_engine import ResearchRunError, run_research
from html_report_sync import sync_generated_report


DEFAULT_TOPICS = Path(__file__).with_name("research_topics.json")


def load_topics(path: Path) -> list[str]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        topics = value["topics"]
        if not isinstance(topics, list) or not topics or any(not isinstance(item, str) or not item.strip() for item in topics):
            raise ValueError
        return topics
    except (OSError, ValueError, KeyError, TypeError):
        raise ResearchError("Нужен JSON с непустым списком topics.") from None


def run_topic_set(topics: list[str], output_root=None, run=run_research) -> Path:
    root = Path(output_root or Path(__file__).parent / "outputs")
    root.mkdir(parents=True, exist_ok=True)
    directory = Path(mkdtemp(prefix="research_batch_", dir=root))
    results = [{"topic": topic, "facts": [], "sources": [], "rounds": [], "enough": None, "missing_questions": [],
                "execution": {"state": "not_started", "error": None, "duration_seconds": None}} for topic in topics]
    atomic_write_json(directory / "topics.json", {"topics": topics})
    print(f"Папка набора исследований: {directory}", flush=True)
    for index, topic in enumerate(topics):
        print(f"\nТема {index + 1}/{len(topics)}: {topic}", flush=True)
        try:
            result = run(topic, output_root=directory)
        except ResearchRunError as error:
            result = json.loads((error.directory / "research.json").read_text())
            print(f"Сбой: {error}", flush=True)
        except (ResearchError, OSError) as error:
            result = {**results[index], "execution": {"state": "failed", "error": str(error), "duration_seconds": None}}
            print(f"Сбой до сохранения исследования: {error}", flush=True)
        results[index] = result
        atomic_write_json(directory / "batch.json", {"results": results})
        build_batch_report(results, directory / "report.html")
    print(f"HTML-отчёт: {directory / 'report.html'}", flush=True)
    return directory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--topics-file", type=Path, default=DEFAULT_TOPICS)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        topics = load_topics(args.topics_file)
        if args.dry_run:
            print("Dry-run: только исследователь, без API и без запуска сценариста.")
            print('\n'.join(topics))
            return 0
        directory = run_topic_set(topics, output_root=args.output_root)
        # Передаём итоговый отчёт один раз, а не каждый промежуточный снимок набора.
        copied = sync_generated_report(directory / "report.html")
        results = json.loads((directory / "batch.json").read_text())["results"]
        return 1 if not copied or any(result["execution"]["state"] == "failed" for result in results) else 0
    except ResearchError as error:
        print(error)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
