"""Проверяет нового сценариста на постоянных темах, используя сохранённые исследования."""

import argparse
import json
from pathlib import Path
from tempfile import mkdtemp

from file_io import atomic_write_json
from html_report_sync import sync_generated_report
from pipeline import run_pipeline
from research import ResearchError
from research_batch import DEFAULT_TOPICS, load_topics
from script_report import build_script_report


def run_script_batch(research_batch: Path, output_root=None, run=run_pipeline) -> Path:
    root = Path(output_root or Path(__file__).parent / "outputs")
    root.mkdir(parents=True, exist_ok=True)
    directory = Path(mkdtemp(prefix="script_batch_", dir=root))
    topics = load_topics(DEFAULT_TOPICS)
    entries = []
    candidates = list(research_batch.glob("research_*/research.json"))
    for topic in topics:
        print(f"\nСценарист: {topic}", flush=True)
        entry = {"topic": topic, "directory": None, "error": None}
        try:
            matches = [path for path in candidates if json.loads(path.read_text())["topic"] == topic]
            if len(matches) != 1:
                raise ResearchError("Не найдено однозначное сохранённое исследование темы; укажите правильную папку набора.")
            result = run(research_file=matches[0], output_root=directory, stop_after="script")
            entry["directory"] = str(result.resolve())
        except (ResearchError, OSError, ValueError, KeyError) as error:
            entry["directory"] = str(error.directory.resolve()) if getattr(error, "directory", None) else None
            entry["error"] = str(error) if isinstance(error, ResearchError) else "Не удалось прочитать исследование или сохранить результат."
            print(entry["error"], flush=True)
        entries.append(entry)
        atomic_write_json(directory / "batch.json", {"entries": entries})
        build_script_report(entries, directory / "report.html")
    print(f"HTML-отчёт: {directory / 'report.html'}", flush=True)
    return directory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--research-batch", required=True, type=Path)
    parser.add_argument("--output-root", type=Path)
    args = parser.parse_args()
    directory = run_script_batch(args.research_batch, args.output_root)
    copied = sync_generated_report(directory / "report.html")
    entries = json.loads((directory / "batch.json").read_text())["entries"]
    return 1 if not copied or any(entry["error"] for entry in entries) else 0


if __name__ == "__main__":
    raise SystemExit(main())
