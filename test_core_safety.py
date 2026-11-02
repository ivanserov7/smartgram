"""Offline regressions for duplicate paid work and safe assembly publication."""

import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

import main
from demo_data import DemoResponses
from file_io import atomic_copy, exclusive_lock
from production_batch import run_batch
from production_state import ProductionError, ProductionServiceError, Journal
from research import ResearchError
from script_workflow import approved_script, run_scriptwriter
from test_scriptwriter import fixture


class CoreSafetyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        output = redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)

    def test_demo_cannot_resume_real_production(self):
        with patch("sys.argv", ["main.py", "--demo", "--resume-production", "/missing"]), \
             patch.object(main, "continue_production") as production, \
             redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as stopped:
                main.main()
        self.assertEqual(stopped.exception.code, 2)
        production.assert_not_called()

    def test_scenes_dry_run_does_not_read_files(self):
        with patch("sys.argv", ["main.py", "--scenes-file", "/missing/scenes.json", "--dry-run"]), \
             patch.object(main, "voiceover_from_file") as voice:
            self.assertEqual(main.main(), 0)
        voice.assert_not_called()

    def test_interrupted_script_generation_is_not_paid_twice(self):
        generate = Mock(side_effect=KeyboardInterrupt)
        with self.assertRaises(KeyboardInterrupt):
            run_scriptwriter(fixture(), self.root, generate)
        state = json.loads((self.root / "script_state.json").read_text())
        self.assertEqual(state["pending_generation"]["version_number"], 1)
        self.assertEqual(state["versions"], [])
        with self.assertRaisesRegex(ResearchError, "автоматический повтор"):
            run_scriptwriter(fixture(), self.root, generate)
        self.assertEqual(generate.call_count, 1)

    def test_interrupted_production_operation_is_unknown_service_outcome(self):
        journal = Journal(self.root, "test", {})
        operation = Mock(side_effect=KeyboardInterrupt)
        with self.assertRaises(KeyboardInterrupt):
            journal.operation("image_generation", {"scene": 1}, operation)
        resumed = Journal(self.root, "test", {})
        with self.assertRaises(ProductionServiceError):
            resumed.operation("image_generation", {"scene": 1}, operation)
        self.assertEqual(operation.call_count, 1)

    def test_script_lock_prevents_second_invocation_before_any_request(self):
        generate = Mock()
        with exclusive_lock(self.root / ".script.lock"):
            with self.assertRaisesRegex(ResearchError, "уже работает"):
                run_scriptwriter(fixture(), self.root, generate)
        generate.assert_not_called()
        self.assertFalse((self.root / "script_state.json").exists())

    def test_stale_duration_feedback_cannot_revoke_current_approval(self):
        responses = DemoResponses()
        result = run_scriptwriter(fixture(), self.root, responses.generate)
        before = (self.root / "script_state.json").read_bytes()
        generate = Mock()
        for version in (None, "different-version"):
            with self.subTest(version=version), self.assertRaisesRegex(ResearchError, "другой версии"):
                run_scriptwriter(fixture(), self.root, generate, duration_feedback={
                    "measured_duration_seconds": 90, "script_version_id": version})
        self.assertEqual((self.root / "script_state.json").read_bytes(), before)
        self.assertEqual(approved_script(self.root), result)
        generate.assert_not_called()

    def test_unsafe_and_duplicate_assembly_names_fail_before_any_work(self):
        specification = self.root / "inputs.json"
        output = self.root / "outputs"
        for names in (["../outside"], ["/absolute"], ["a\\b"], [""], ["a\nb"], ["same", "same"]):
            specification.write_text(json.dumps({"entries": [{"name": name} for name in names]}))
            with self.subTest(names=names), patch("production_batch.snapshot_inputs") as snapshot:
                with self.assertRaises(ProductionError):
                    run_batch(specification, output)
                snapshot.assert_not_called()
                self.assertFalse(output.exists())

    def test_assembly_lock_prevents_competing_snapshot(self):
        specification = self.root / "inputs.json"
        specification.write_text(json.dumps({"entries": [{
            "name": "Осьминог 1", "topic": "Осьминог", "script_file": "script.json",
            "audio_manifest": "audio_manifest.json", "materials_file": "visual_materials.json"}]}))
        output = self.root / "outputs"
        with exclusive_lock(output / ".assembly.lock"), patch("production_batch.snapshot_inputs") as snapshot:
            with self.assertRaisesRegex(ProductionError, "Another assembly"):
                run_batch(specification, output)
        snapshot.assert_not_called()

    def test_missing_assembly_sources_fail_before_creating_output(self):
        specification = self.root / "inputs.json"
        specification.write_text(json.dumps({"entries": [{"name": "video", "topic": "Topic"}]}))
        output = self.root / "outputs"
        with self.assertRaisesRegex(ProductionError, "script_file"):
            run_batch(specification, output)
        self.assertFalse(output.exists())

    def test_atomic_copy_replaces_symlink_without_writing_outside_output(self):
        source, outside, target = (self.root / name for name in ("source", "outside", "target"))
        source.write_bytes(b"new-video")
        outside.write_bytes(b"keep-existing")
        target.symlink_to(outside)
        checksum = hashlib.sha256(source.read_bytes()).hexdigest()
        self.assertEqual(atomic_copy(source, target, expected_sha256=checksum), checksum)
        self.assertFalse(target.is_symlink())
        self.assertEqual(target.read_bytes(), b"new-video")
        self.assertEqual(outside.read_bytes(), b"keep-existing")

    def test_copy_hash_mismatch_preserves_previous_release(self):
        source, target = self.root / "source", self.root / "target"
        source.write_bytes(b"changed-video")
        target.write_bytes(b"previous-release")
        with self.assertRaises(ValueError):
            atomic_copy(source, target, expected_sha256="wrong")
        self.assertEqual(target.read_bytes(), b"previous-release")
        self.assertEqual({p.name for p in self.root.iterdir()}, {"source", "target"})


if __name__ == "__main__":
    unittest.main()
