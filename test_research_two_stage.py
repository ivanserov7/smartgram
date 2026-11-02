"""Двухэтапный исследователь: только подставные ответы, без настоящих запросов."""

import copy
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from artifacts import load_research, validate_research
from research import ResearchError
from research_config import ResearchSettings
from research_engine import ResearchRunError, run_research
from research_models import CollectionResult, SufficiencyResult, as_script_research


URL = "https://university.example/article"


def collection_payload(statement="Тестовый факт", availability="page_text", excerpt="Фрагмент тестового источника"):
    return {"facts": [{"statement": statement, "evidence": [{"url": URL, "title": None, "excerpt": excerpt,
            "source_reason": "ТЕСТ: источник соответствует теме.", "availability": availability,
            "availability_details": "ТЕСТ: состояние передано моделью.", "excerpt_kind": "unknown"}]}],
            "unconfirmed_candidates": [], "notes": [], "contradictions": []}


class FakeTwoStageAPI:
    settings = ResearchSettings()

    def __init__(self, enough=(True,), collections=None, fail=None):
        self.enough = enough
        self.collections = collections or [collection_payload()]
        self.fail = fail
        self.calls = []
        self.requests = []

    def close(self):
        pass

    def collect(self, topic, accumulated, missing_questions, round_number):
        self.calls.append({"stage": "collection", "topic": topic, "accumulated": copy.deepcopy(accumulated), "missing_questions": missing_questions})
        if self.fail == ("collection", round_number):
            raise ResearchError("ТЕСТ: HTTP 404 model_not_found")
        record = {"number": len(self.calls), "web": {"calls": [{"id": "test-search", "action": {"type": "search"}, "sources": None}], "citations": []}}
        self.requests.append(record)
        return CollectionResult.model_validate(self.collections[min(round_number - 1, len(self.collections) - 1)]), record

    def assess(self, topic, accumulated, round_number):
        self.calls.append({"stage": "assessment", "topic": topic, "accumulated": copy.deepcopy(accumulated)})
        if self.fail == ("assessment", round_number):
            raise ResearchError("ТЕСТ: API timeout")
        enough = self.enough[min(round_number - 1, len(self.enough) - 1)]
        return SufficiencyResult(enough=enough, missing_questions=[] if enough else ["Как устроен основной механизм?"]), {"number": len(self.calls)}


class TwoStageResearchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        output = redirect_stdout(StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)

    def run_case(self, api):
        return run_research("Тестовая тема", self.root, client_factory=lambda directory: api)

    def test_one_fact_one_source_is_enough_without_page_downloads_or_quote_comparison(self):
        api = FakeTwoStageAPI()
        with patch("httpx.Client.send", side_effect=AssertionError("Downloaded article")), \
             patch("research_evidence.validate_fact_quotes", side_effect=AssertionError("Compared quote")):
            result = self.run_case(api)
        validate_research(result)
        self.assertTrue(result["enough"])
        self.assertEqual(len(result["facts"]), 1)
        self.assertEqual(len(result["sources"]), 1)
        self.assertEqual(len(api.calls), 2)
        self.assertTrue(all(not value for value in api.calls[0]["accumulated"].values()))
        self.assertFalse(api.calls[0]["missing_questions"])
        self.assertIsNone(result["facts"][0]["evidence"][0]["title"])
        self.assertIsNone(result["facts"][0]["evidence"][0]["api_observation"]["successful_text_read_confirmed"])

    def test_missing_questions_and_useful_first_round_are_preserved(self):
        first = collection_payload("Первое утверждение")
        first["notes"] = ["Первая оговорка"]
        second = collection_payload("Дополнение")
        second["contradictions"] = ["ТЕСТ: есть противоречивые описания"]
        api = FakeTwoStageAPI(enough=(False, True), collections=[first, second])
        result = self.run_case(api)
        self.assertEqual(len(api.calls), 4)
        self.assertEqual(api.calls[2]["missing_questions"], ["Как устроен основной механизм?"])
        self.assertEqual(api.calls[2]["accumulated"]["facts"][0]["statement"], "Первое утверждение")
        self.assertEqual([fact["statement"] for fact in result["facts"]], ["Первое утверждение", "Дополнение"])
        self.assertIn("Первая оговорка", result["notes"])
        self.assertTrue(result["contradictions"])
        self.assertTrue((Path(result["run_directory"]) / "round_1.json").exists())

    def test_exhausted_rounds_remain_not_enough(self):
        api = FakeTwoStageAPI(enough=(False,))
        result = self.run_case(api)
        self.assertFalse(result["enough"])
        self.assertEqual(result["execution"]["state"], "completed")
        self.assertEqual(len(api.calls), 4)
        self.assertTrue(result["missing_questions"])

    def test_api_failure_is_not_a_negative_sufficiency_verdict(self):
        for failure in (("collection", 1), ("assessment", 1), ("collection", 2)):
            api = FakeTwoStageAPI(enough=(False,), fail=failure)
            with self.subTest(failure=failure), self.assertRaises(ResearchRunError) as caught:
                self.run_case(api)
            result = load_research(caught.exception.directory / "research.json")
            self.assertIsNone(result["enough"])
            self.assertEqual(result["execution"]["state"], "failed")
            self.assertTrue(result["execution"]["error"])

    def test_search_description_unavailable_unknown_or_missing_excerpt_are_candidates_only(self):
        for availability, excerpt in (("search_description", "Сниппет"), ("unavailable", "Не текст"), ("unknown", None), ("page_text", None)):
            api = FakeTwoStageAPI(enough=(False,), collections=[collection_payload(availability=availability, excerpt=excerpt)])
            result = self.run_case(api)
            self.assertFalse(result["facts"])
            self.assertTrue(result["unconfirmed_candidates"])
            self.assertFalse(result["enough"])

    def test_reported_open_does_not_invent_tool_event(self):
        api = FakeTwoStageAPI()
        payload = collection_payload()
        payload["facts"][0]["evidence"][0]["availability_details"] = "Я открыл и прочитал страницу"
        api.collections = [payload]
        result = self.run_case(api)
        observation = result["facts"][0]["evidence"][0]["api_observation"]
        self.assertEqual(observation["open_page_actions"], [])
        self.assertIsNone(observation["successful_text_read_confirmed"])

    def test_bad_url_is_execution_error_without_enough(self):
        payload = collection_payload()
        payload["facts"][0]["evidence"][0]["url"] = "file:///etc/passwd"
        with self.assertRaises(ResearchRunError) as caught:
            self.run_case(FakeTwoStageAPI(collections=[payload]))
        result = load_research(caught.exception.directory / "research.json")
        self.assertIsNone(result["enough"])

    def test_schema_requires_exactly_two_fields_for_assessment_and_consistent_verdict(self):
        self.assertEqual(set(SufficiencyResult.model_json_schema()["properties"]), {"enough", "missing_questions"})
        for payload in ({"enough": True, "missing_questions": ["что-то"]}, {"enough": False, "missing_questions": []},
                        {"enough": "yes", "missing_questions": []}, {"enough": True, "missing_questions": [], "reason": "extra"}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                SufficiencyResult.model_validate(payload)

    def test_script_adapter_keeps_urls_but_does_not_replace_primary_source_check(self):
        result = self.run_case(FakeTwoStageAPI())
        adapted = as_script_research(result)
        self.assertEqual(adapted["facts"][0]["sources"][0]["url"], URL)
        self.assertNotIn("source_pages", adapted)
        from scriptwriter import write_script
        calls = []
        from demo_data import DemoResponses
        responses = DemoResponses()
        def generate(schema, instructions, data):
            calls.append(data)
            return responses.generate(schema, instructions, data)
        write_script(result, generate=generate)
        self.assertEqual(calls[0]["research"], result)
        # Доказательства передаются проверяющему, который теперь читает источники веб-инструментом.

    def test_only_research_never_calls_script_or_voice(self):
        import pipeline
        result = self.run_case(FakeTwoStageAPI())
        with patch.object(pipeline, "research_topic", return_value=result), \
             patch.object(pipeline, "write_and_check", side_effect=AssertionError("Writer called")), \
             patch.object(pipeline, "voiceover_from_file", side_effect=AssertionError("Voice called")):
            pipeline.run_pipeline("Тестовая тема", output_root=self.root, stop_after="research")

    def test_persistent_topic_set_matches_user_message(self):
        from research_batch import load_topics, DEFAULT_TOPICS
        self.assertEqual(load_topics(DEFAULT_TOPICS), ["Как осьминог меняет цвет?", "Зачем в Древнем Египте строили пирамиды?", "Как GPS определяет местоположение?"])


    def test_batch_continues_after_error_and_report_shows_dash_not_false(self):
        from research_batch import run_topic_set
        import research_batch_report
        calls = []
        def run(topic, output_root):
            calls.append(topic)
            if len(calls) == 1:
                raise ResearchError("ТЕСТ: модель недоступна")
            result = run_research(topic, output_root=output_root, client_factory=lambda directory: FakeTwoStageAPI())
            return result
        folder = run_topic_set(["Первая", "Вторая", "Третья"], self.root, run=run)
        results = json.loads((folder / "batch.json").read_text())["results"]
        self.assertEqual(calls, ["Первая", "Вторая", "Третья"])
        self.assertIsNone(results[0]["enough"])
        self.assertEqual(results[0]["execution"]["state"], "failed")
        self.assertTrue(results[1]["enough"])
        page = (folder / "report.html").read_text()
        self.assertIn("ТЕСТ: модель недоступна", page)
        self.assertIn("Достаточность: <strong>—</strong>", page)
        self.assertNotIn("проверены дословно", page)

    def test_report_preserves_full_requests_and_escapes_html(self):
        from research_batch_report import render_batch
        result = self.run_case(FakeTwoStageAPI())
        record = {"number": 1, "stage": "collection", "round": 1, "status": "completed", "model": "test-model",
                  "response_id": "test-response", "started_at_utc": "test-time", "ended_at_utc": "test-time",
                  "duration_seconds": 1, "request": {"instructions": "<script>not executed</script>", "input": "Полные данные"},
                  "transport": {"timeout_seconds": 180, "max_retries": 0}, "response": {"output": []}, "web": None,
                  "usage": {"input_tokens": 10, "output_tokens": 20}, "error": None}
        page = render_batch([result], journals=[{"requests": [record]}])
        self.assertIn("&lt;script&gt;", page)
        self.assertNotIn("<script>", page)
        self.assertIn("Полные данные", page)
        self.assertIn("test-response", page)
        self.assertIn("unknown", page)


    def test_tool_observation_does_not_mix_different_query_pages(self):
        from research_engine import tool_observation
        record = {"web": {"calls": [{"id": "open", "status": "completed", "action": {"type": "open_page", "url": "https://example.org/view?id=1"}}]}}
        self.assertFalse(tool_observation("https://example.org/view?id=2", record)["open_page_actions"])
        self.assertTrue(tool_observation("https://example.org/view?id=1&utm_source=x", record)["open_page_actions"])
        self.assertIsNone(tool_observation("https://example.org/view?id=1", record)["successful_text_read_confirmed"])

    def test_research_settings_are_separate_and_limits_cannot_grow_silently(self):
        import research_config
        with patch.object(research_config, "dotenv_values", return_value={"OPENAI_API_KEY": "test-placeholder", "OPENAI_MODEL": "other-global-model"}):
            key, settings = research_config.load_research_settings()
        self.assertEqual(settings.search_model, "gpt-6-luna")
        self.assertEqual(settings.assessment_model, "gpt-6-luna")
        self.assertEqual(settings.max_tool_calls_per_round, 8)
        for changes in ({"RESEARCH_MAX_ROUNDS": "3"}, {"RESEARCH_MAX_TOOL_CALLS_PER_ROUND": "9"}):
            with patch.object(research_config, "dotenv_values", return_value={"OPENAI_API_KEY": "test-placeholder", **changes}), self.assertRaises(ResearchError):
                research_config.load_research_settings()


if __name__ == "__main__":
    unittest.main()
