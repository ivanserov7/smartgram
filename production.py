"""Continue accepted inputs through visuals and editing, without upstream API calls."""

import argparse
import sys
from pathlib import Path
from research import ResearchError
from media_tools import MediaError
from production_state import ProductionError, read
from editing_inputs import snapshot_inputs, import_manifest
from visual_materials import run_visual_materials
from editing import run_editing


def continue_production(directory, stage="all"):
    directory = Path(directory)
    if stage in {"all", "visuals"}:
        result = run_visual_materials(directory)
        if stage == "visuals" and read(result)["status"] != "approved":
            raise ProductionError(
                "Visual materials are not approved; see visual_materials.json and preserved attempts."
            )
    # Even unsuccessful visual runs can report the independent concrete audio blocker.
    if stage in {"all", "editing"}:
        run_editing(directory)
    return directory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--script-file", type=Path)
    source.add_argument("--resume", type=Path)
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument(
        "--audio-manifest",
        type=Path,
        help="Import manifest and immutable WAV without generating speech",
    )
    parser.add_argument(
        "--materials-file",
        type=Path,
        help="Import approved originals/motion/previews without new search",
    )
    parser.add_argument("--stage", choices=["visuals", "editing", "all"], default="all")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.script_file and not args.run_dir:
        parser.error(
            "--script-file requires a separate --run-dir for an immutable snapshot."
        )
    if args.resume and args.run_dir:
        parser.error("--resume uses its existing run directory.")
    if args.dry_run:
        print(
            "Approved script → visual_queries/search/select/prepare/review (independent of audio) → accepted audio/hash/words/display_times → video_render/subtitle_segment/subtitle_layout/compose/final_check. Resume preserves budgets; no files read, no API calls."
        )
        return 0
    try:
        directory = args.resume or snapshot_inputs(
            args.script_file.parent, args.run_dir, include_audio=args.stage != "visuals"
        )
        if args.audio_manifest:
            import_manifest(args.audio_manifest, directory, "audio")
        if args.materials_file:
            import_manifest(args.materials_file, directory, "visuals")
        continue_production(directory, args.stage)
        print(directory)
        return 0
    except (ProductionError, ResearchError, MediaError, OSError, ValueError) as error:
        print("Production stopped: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
