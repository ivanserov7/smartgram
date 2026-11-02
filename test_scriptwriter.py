"""Новый сценарист: подставные API и HTTP-транспорт; реальные платные вызовы запрещены."""

import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import httpx
from openai import OpenAI

from demo_data import DEMO_TEXTS, DemoResponses, demo_research
from fact_checker import VerificationDraft, validate_verification
from research import ResearchError
from script_config import ScriptSettings, load_script_settings
from script_workflow import approved_script, run_scriptwriter
from scriptwriter import ScriptDraft, narration_text, script_identity, validate_script


def fixture():
    from test_research_two_stage import collection_payload
    fact = collection_payload()["facts"][0]
    return {"schema_version": 4, "mode": "real", "topic": "Тестовая тема", "facts": [fact],
            "sources": [{"url": fact["evidence"][0]["url"], "title": "Тест"}],
            "unconfirmed_candidates": [], "notes": ["Оговорка"], "contradictions": [], "rounds": [],
            "missing_questions": [], "enough": True, "created_at_utc": "2026-11-01T00:00:00Z",
            "execution": {"state": "completed", "error": None}}


class ScriptwriterTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.research = fixture()
        self.responses = DemoResponses()
        self.calls = []
        output = redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)

    def generate(self, schema, instructions, data):
        self.calls.append((schema, instructions, copy.deepcopy(data)))
        return self.responses.generate(schema, instructions, data)

    def run_script(self, **kwargs):
        return run_scriptwriter(self.research, self.root, self.generate, **kwargs)

    def state(self):
        return json.loads((self.root / "script_state.json").read_text())

    def test_success_exactly_two_steps_narration_and_no_planner(self):
        result = self.run_script()
        self.assertEqual([call[0] for call in self.calls], [ScriptDraft, VerificationDraft])
        self.assertEqual(result["narration_text"], ' '.join(DEMO_TEXTS))
        self.assertNotIn("Учебная схема", result["narration_text"])
        self.assertEqual(self.calls[0][2]["research"], self.research)
        self.assertEqual(self.calls[1][2]["fragments"], result["fragments"])
        self.assertEqual(self.state()["revisions_used"], 0)
        self.run_script()
        self.assertEqual(len(self.calls), 2)

    def test_enough_true_required_before_any_call(self):
        for enough in (False, None):
            self.research["enough"] = enough
            self.research["missing_questions"] = ["Нет основного объяснения"]
            with self.assertRaises(ResearchError):
                self.run_script()
        self.assertEqual(self.calls, [])

    def test_reject_invalid_contract_and_old_format(self):
        for value in ({"text": "Старый текст"}, {"fragments": []}, {"fragments": [{"text": "a", "visual": " "}]},
                      {"fragments": [{"text": "a", "visual": "b", "duration": 5}]},
                      {"fragments": [{"text": 123, "visual": "b"}]}, {"fragments": [{"text": "a", "visual": "b"}], "text": "a"}):
            with self.subTest(value=value), self.assertRaises(ResearchError):
                validate_script(value)
        with self.assertRaises(ValueError):
            ScriptDraft.model_validate_json("{broken")

    def test_text_and_visual_issues_rewrite_whole_version(self):
        for field in ("text", "visual"):
            directory = self.root / field
            calls = []
            def generate(schema, instructions, data):
                calls.append((schema, copy.deepcopy(data)))
                if schema is ScriptDraft:
                    return ScriptDraft(fragments=[{"text": "Текст один", "visual": "Схема один"}, {"text": "Текст два", "visual": "Схема два"}])
                if len(calls) == 2:
                    return VerificationDraft(approved=False, issues=[{"fragment_index": 1, "field": field,
                        "phrase": data["fragments"][1][field], "problem": "Проблема", "correction": "Исправьте", "source_url": None}])
                return VerificationDraft(approved=True, issues=[])
            result = run_scriptwriter(self.research, directory, generate)
            self.assertEqual(len(calls), 4)
            self.assertEqual(calls[2][1]["previous"]["fragments"], result["fragments"])
            self.assertEqual(calls[2][1]["feedback"]["issues"][0]["field"], field)
            self.assertEqual(calls[3][1]["fragments"], result["fragments"])

    def test_context_issue_in_first_text_or_visual_uses_existing_revision_loop(self):
        # Ответы API подставные: проверяем передачу замечания, не способность Python понимать смысл.
        for field in ("text", "visual"):
            with self.subTest(field=field):
                calls = []
                unclear = {"fragments": [{"text": "Внутри них содержится пигмент.", "visual": "Внутренняя деталь крупным планом."}]}
                clear = {"fragments": [{"text": "Почему листья меняют цвет? Это связано с пигментами.",
                                         "visual": "Листья разной окраски на дереве, затем условная схема пигментов."}]}
                def generate(schema, instructions, data):
                    calls.append((schema, instructions, copy.deepcopy(data)))
                    if schema is ScriptDraft:
                        return ScriptDraft.model_validate(unclear if len(calls) == 1 else clear)
                    if len(calls) == 2:
                        return VerificationDraft(approved=False, issues=[{
                            "fragment_index": 0, "field": field, "phrase": data["fragments"][0][field],
                            "problem": "В начале не обозначено само явление." if field == "text" else "Первый visual не знакомит с объектом.",
                            "correction": "Сначала обозначьте изменение цвета листьев и покажите листья.", "source_url": None}])
                    return VerificationDraft(approved=True, issues=[])
                result = run_scriptwriter(self.research, self.root / field, generate)
                self.assertEqual([call[0] for call in calls], [ScriptDraft, VerificationDraft, ScriptDraft, VerificationDraft])
                self.assertEqual(calls[2][2]["feedback"]["issues"][0]["fragment_index"], 0)
                self.assertEqual(calls[2][2]["feedback"]["issues"][0]["field"], field)
                self.assertEqual(calls[3][2]["fragments"], clear["fragments"])
                self.assertEqual(result["fragments"], clear["fragments"])
                self.assertIn("без заголовка", calls[0][1])
                self.assertIn("мысленно скрыв название темы", calls[1][1])

    def test_clear_declarative_opening_has_no_local_question_mark_gate(self):
        script = {"fragments": [{"text": "Смена цвета листьев связана с пигментами; разберём этот процесс.",
                                  "visual": "Дерево с листьями разной окраски."}]}
        calls = []
        def generate(schema, instructions, data):
            calls.append((schema, instructions))
            return ScriptDraft.model_validate(script) if schema is ScriptDraft else VerificationDraft(approved=True, issues=[])
        result = run_scriptwriter(self.research, self.root, generate)
        self.assertEqual(result["fragments"], script["fragments"])
        self.assertEqual(len(calls), 2)
        self.assertIn("Не отклоняй столь же понятное", calls[1][1])
        self.assertIn("сохранив фактическую точность и существенные оговорки", calls[1][1])

    def test_budget_zero_and_two_and_resume_exhausted(self):
        for budget in (0, 2):
            directory = self.root / str(budget)
            responses = DemoResponses("fail")
            with self.assertRaisesRegex(ResearchError, "бюджет"):
                run_scriptwriter(self.research, directory, responses.generate, settings=replace(ScriptSettings(), max_revisions=budget))
            self.assertEqual(responses.script_calls, 1 + budget)
            self.assertEqual(responses.check_calls, 1 + budget)
            with self.assertRaisesRegex(ResearchError, "бюджет"):
                run_scriptwriter(self.research, directory, responses.generate, settings=replace(ScriptSettings(), max_revisions=99))
            self.assertEqual(responses.script_calls, 1 + budget)
            with self.assertRaises(ResearchError):
                approved_script(directory)

    def test_shared_duration_budget_after_fact_revision(self):
        self.responses = DemoResponses("repair")
        self.run_script()
        self.assertEqual(self.state()["revisions_used"], 1)
        run_scriptwriter(self.research, self.root, self.generate, duration_feedback={
            "measured_duration_seconds": 52.1, "script_version_id": approved_script(self.root)["script_version_id"]})
        self.assertEqual(self.state()["revisions_used"], 2)
        self.assertEqual(self.calls[-2][2]["feedback"]["duration"]["measured_duration_seconds"], 52.1)
        with self.assertRaisesRegex(ResearchError, "бюджет"):
            run_scriptwriter(self.research, self.root, self.generate, duration_feedback={
                "measured_duration_seconds": 49, "script_version_id": approved_script(self.root)["script_version_id"]})
        self.assertEqual(len(self.calls), 6)
        with self.assertRaises(ResearchError):
            approved_script(self.root)

    def test_short_text_not_rejected_by_word_estimate(self):
        def generate(schema, *args):
            return ScriptDraft(fragments=[{"text": "Коротко.", "visual": "Схема."}]) if schema is ScriptDraft else VerificationDraft(approved=True, issues=[])
        result = run_scriptwriter(self.research, self.root, generate)
        self.assertEqual(result["narration_text"], "Коротко.")

    def test_technical_failure_stops_and_keeps_last_version(self):
        def generate(schema, instructions, data):
            if schema is VerificationDraft:
                raise ResearchError("OpenAI HTTP 429: точная причина")
            return self.responses.generate(schema, instructions, data)
        with self.assertRaisesRegex(ResearchError, "429"):
            run_scriptwriter(self.research, self.root, generate)
        self.assertEqual(self.state()["status"], "technical_error")
        self.assertEqual(self.state()["revisions_used"], 0)
        self.assertTrue((self.root / "script_1.json").exists())
        self.assertFalse((self.root / "script.json").exists())
        with self.assertRaisesRegex(ResearchError, "автоматический повтор"):
            run_scriptwriter(self.research, self.root, lambda *args: self.fail("Repeated API"))

    def test_invalid_review_does_not_approve(self):
        script = {"fragments": [{"text": "Фраза", "visual": "Схема"}]}
        issue = {"fragment_index": 0, "field": "visual", "phrase": "Схема", "problem": "Проблема", "correction": "Исправьте", "source_url": None}
        for payload in ({"approved": True, "issues": [issue]}, {"approved": False, "issues": []},
                        {"approved": "true", "issues": []}, {"approved": False, "issues": [{**issue, "fragment_index": 9}]},
                        {"approved": False, "issues": [{**issue, "phrase": "Выдуманная фраза"}]},
                        {"approved": False, "issues": [{**issue, "field": "unknown"}]}):
            with self.subTest(payload=payload), self.assertRaises(ResearchError):
                validate_verification(payload, script)

    def test_downstream_adapter_no_model_and_rejects_stale_data(self):
        from scene_planner import scenes_from_script
        from visuals import load_scenes
        from commons_api import VisualError
        from voiceover import voiceover_from_file
        result = self.run_script()
        plan = scenes_from_script(result)
        path = self.root / "scenes.json"
        path.write_text(json.dumps(plan))
        self.assertEqual(load_scenes(path)["script_version_id"], result["script_version_id"])
        self.assertIsNone(plan["scenes"][0]["estimated_duration_seconds"])
        plan["scenes"][0]["visual_description"] = "Старое изображение"
        path.write_text(json.dumps(plan))
        with self.assertRaises(VisualError):
            load_scenes(path)
        current = self.state()
        current["status"] = "needs_revision"
        (self.root / "script_state.json").write_text(json.dumps(current))
        with self.assertRaises(ResearchError):
            approved_script(self.root)

    def test_resume_refuses_old_text_script(self):
        (self.root / "script.json").write_text('{"text":"Старый сценарий"}')
        with self.assertRaisesRegex(ResearchError, "Старый текстовый"):
            self.run_script()
        self.assertEqual(self.calls, [])

    def test_settings_zero_and_invalid(self):
        path = self.root / ".env"
        path.write_text("SCRIPT_MAX_REVISIONS=0\nSCRIPT_WRITING_MODEL=chosen-model\n")
        _, settings = load_script_settings(path, require_key=False)
        self.assertEqual(settings.max_revisions, 0)
        self.assertEqual(settings.writing_model, "chosen-model")
        for line in ("SCRIPT_MAX_REVISIONS=-1", "SCRIPT_WRITING_TIMEOUT_SECONDS=0", "SCRIPT_VERIFICATION_CONTEXT_SIZE=bad"):
            path.write_text(line)
            with self.assertRaises(ResearchError):
                load_script_settings(path, require_key=False)

    def test_pipeline_script_only_never_starts_downstream(self):
        import pipeline
        source = self.root / "input.json"
        source.write_text(json.dumps(self.research))
        with patch.object(pipeline, "ScriptClient") as client, \
             patch.object(pipeline, "load_script_settings", return_value=("test", ScriptSettings())), \
             patch.object(pipeline, "research_topic", side_effect=AssertionError("Repeated research")), \
             patch.object(pipeline, "voiceover_from_file", side_effect=AssertionError("TTS called")), \
             patch.object(pipeline, "collect_visuals", side_effect=AssertionError("Visuals called")):
            client.return_value.generate = self.generate
            folder = pipeline.run_pipeline(research_file=source, output_root=self.root / "runs", stop_after="script")
        self.assertEqual(len(self.calls), 2)
        self.assertTrue((folder / "scenes.json").exists())
        self.assertFalse((folder / "audio_manifest.json").exists())

    def test_report_and_batch_keep_failures_and_escape_html(self):
        from script_report import build_script_report
        from script_batch import run_script_batch
        from research_batch import load_topics, DEFAULT_TOPICS
        research_root = self.root / "saved"
        research_root.mkdir()
        topics = load_topics(DEFAULT_TOPICS)
        for index, topic in enumerate(topics):
            folder = research_root / f"research_{index}"
            folder.mkdir()
            research = {**self.research, "topic": topic}
            (folder / "research.json").write_text(json.dumps(research))
        calls = []
        def run(research_file, output_root, stop_after):
            calls.append((research_file, stop_after))
            raise ResearchError("TEST failure <script>unsafe</script>")
        folder = run_script_batch(research_root, self.root / "batch", run=run)
        page = (folder / "report.html").read_text()
        self.assertEqual(len(calls), 3)
        self.assertNotIn("<script>unsafe</script>", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertIn("Технический сбой", page)
        self.assertIn("Аудио не создавалось", page)
        for topic in topics:
            self.assertIn(topic, page)

    def test_resume_command_dry_run_no_files_or_api(self):
        import main
        with patch("sys.argv", ["main.py", "--resume-script", "/missing/run", "--dry-run"]), \
             patch.object(main, "resume_script_run", side_effect=AssertionError("Read/API forbidden")):
            self.assertEqual(main.main(), 0)

    def test_result_page_reads_fragments_and_hides_outdated_assets(self):
        from result_page import build_result_page
        from scene_planner import scenes_from_script
        result = self.run_script()
        (self.root / "research.json").write_text(json.dumps(self.research))
        (self.root / "scenes.json").write_text(json.dumps(scenes_from_script(result)))
        (self.root / "visual_review.json").write_text(json.dumps({"script_version_id": "obsolete", "scenes": [
            {"scene_id": 1, "selected": {"title": "OUTDATED IMAGE", "local_path": "missing.jpg"}}]}))
        (self.root / "audio_manifest.json").write_text(json.dumps({"script_version_id": "obsolete", "scenes": []}))
        page = build_result_page(self.root).read_text()
        self.assertIn("Можно использовать", page)
        self.assertIn("Учебная схема", page)
        self.assertNotIn("OUTDATED IMAGE", page)
        self.assertIn("фактическая длительность неизвестна", page)

    def test_changed_visual_changes_downstream_cache_version(self):
        from visuals import scene_fingerprint
        from scene_planner import scenes_from_script
        first = self.run_script()
        changed = {"fragments": copy.deepcopy(first["fragments"])}
        changed["fragments"][0]["visual"] = "Новое описание"
        changed.update(script_version_id=script_identity(changed), mode="real")
        scene1 = scenes_from_script(first)["scenes"][0]
        scene2 = scenes_from_script(changed)["scenes"][0]
        self.assertNotEqual(scene_fingerprint(scene1, "real"), scene_fingerprint(scene2, "real"))
        self.assertNotEqual(scene1["script_version_id"], scene2["script_version_id"])


class ScriptTransportTests(unittest.TestCase):
    def test_requests_and_journal_use_existing_client(self):
        import script_requests
        from test_research import search_payload
        sent = []
        def handler(request):
            body = json.loads(request.content)
            sent.append(body)
            payload = search_payload()
            value = {"approved": True, "issues": []} if "tools" in body else {"fragments": [{"text": "Фраза", "visual": "Схема"}]}
            if "tools" not in body:
                payload["output"] = payload["output"][1:]
            payload["output"][-1]["content"][0]["text"] = json.dumps(value)
            payload["usage"] = {"input_tokens": 12, "output_tokens": 5, "total_tokens": 17}
            return httpx.Response(200, json=payload)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            client = OpenAI(api_key="test-placeholder", http_client=httpx.Client(transport=httpx.MockTransport(handler)))
            with patch.object(script_requests, "OpenAI", return_value=client), \
                 patch.object(script_requests, "load_script_settings", return_value=("test-placeholder", ScriptSettings())), redirect_stdout(io.StringIO()):
                api = script_requests.ScriptClient(root)
                run_scriptwriter(fixture(), root, api.generate)
                api.close()
            self.assertEqual(len(sent), 2)
            self.assertNotIn("tools", sent[0])
            self.assertEqual(sent[1]["max_tool_calls"], 8)
            self.assertEqual(sent[1]["tool_choice"], "required")
            self.assertTrue(sent[0]["text"]["format"]["strict"])
            self.assertNotIn("max_output_tokens", sent[0])
            journal = json.loads((root / "script_usage.json").read_text())
            self.assertEqual(len(journal["requests"]), 2)
            self.assertEqual(journal["requests"][1]["usage"]["input_tokens"], 12)
            self.assertNotIn("test-placeholder", (root / "script_usage.json").read_text())

    def test_api_errors_no_auto_retry_and_no_false_success(self):
        import script_requests
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(429, json={"error": {"code": "insufficient_quota", "message": "test-placeholder", "type": "quota"}})
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            client = OpenAI(api_key="test-placeholder", max_retries=0, http_client=httpx.Client(transport=httpx.MockTransport(handler)))
            with patch.object(script_requests, "OpenAI", return_value=client), \
                 patch.object(script_requests, "load_script_settings", return_value=("test-placeholder", ScriptSettings())), redirect_stdout(io.StringIO()):
                api = script_requests.ScriptClient(root)
                with self.assertRaisesRegex(ResearchError, "insufficient_quota") as caught:
                    run_scriptwriter(fixture(), root, api.generate)
                api.close()
            self.assertEqual(len(calls), 1)
            self.assertNotIn("test-placeholder", str(caught.exception))
            self.assertFalse((root / "script.json").exists())

    def test_refusal_incomplete_json_and_missing_web_are_technical_errors(self):
        import script_requests
        from test_research import search_payload
        for kind in ("invalid_json", "incomplete", "refusal", "no_web", "timeout"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                calls = []
                def handler(request):
                    calls.append(request)
                    body = json.loads(request.content)
                    if kind == "timeout":
                        raise httpx.ReadTimeout("test timeout", request=request)
                    payload = search_payload()
                    value = {"approved": True, "issues": []} if "tools" in body else {"fragments": [{"text": "Фраза", "visual": "Схема"}]}
                    payload["output"][-1]["content"][0]["text"] = json.dumps(value)
                    if "tools" not in body or kind == "no_web":
                        payload["output"] = payload["output"][1:]
                    if kind == "invalid_json":
                        payload["output"][-1]["content"][0]["text"] = "{broken"
                    elif kind == "incomplete":
                        payload["status"] = "incomplete"
                        payload["incomplete_details"] = {"reason": "max_output_tokens"}
                    elif kind == "refusal":
                        payload["output"][-1]["content"] = [{"type": "refusal", "refusal": "test refusal"}]
                    return httpx.Response(200, json=payload)
                client = OpenAI(api_key="test-placeholder", max_retries=0, http_client=httpx.Client(transport=httpx.MockTransport(handler)))
                with patch.object(script_requests, "OpenAI", return_value=client), \
                     patch.object(script_requests, "load_script_settings", return_value=("test-placeholder", ScriptSettings())), redirect_stdout(io.StringIO()):
                    api = script_requests.ScriptClient(root)
                    with self.assertRaises(ResearchError):
                        run_scriptwriter(fixture(), root, api.generate)
                    api.close()
                self.assertEqual(len(calls), 2 if kind == "no_web" else 1)
                self.assertEqual(json.loads((root / "script_state.json").read_text())["status"], "technical_error")
                self.assertFalse((root / "script.json").exists())

    def test_unfinished_journal_cannot_repeat_request_after_restart(self):
        import script_requests
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "script_usage.json").write_text(json.dumps({"requests": [{"stage": "script_writing", "status": "started"}]}))
            with patch.object(script_requests, "load_script_settings", return_value=("test-placeholder", ScriptSettings())), \
                 patch.object(script_requests, "OpenAI", side_effect=AssertionError("New request client forbidden")):
                with self.assertRaisesRegex(ResearchError, "Автоматический повтор отключён"):
                    script_requests.ScriptClient(root)


if __name__ == "__main__":
    unittest.main()
