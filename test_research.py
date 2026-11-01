"""Проверки на вымышленных данных: сеть и платные запросы не используются."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from openai import OpenAI

import main
import pipeline
import research
import research_requests
import research_engine
from test_research_two_stage import collection_payload
from research_config import ResearchSettings


SOURCE_URL = "https://example.org/science"


def search_payload(with_sources=True):
    """Минимальный ответ Responses API, а не настоящее исследование."""
    return {
        "id": "resp_test_search", "object": "response", "created_at": 0,
        "model": "test-model", "status": "completed",
        "parallel_tool_calls": True, "tool_choice": "required", "tools": [{"type": "web_search"}],
        "output": [
            {"id": "ws_test", "type": "web_search_call", "status": "completed",
             "action": {"type": "search", "query": "test", "sources": []}},
            {"id": "msg_test", "type": "message", "role": "assistant", "status": "completed",
             "content": [{"type": "output_text", "text": "Вымышленный текст для теста.",
                          "annotations": ([{"type": "url_citation", "url": SOURCE_URL,
                                           "title": "Тестовый источник", "start_index": 0,
                                           "end_index": 10}] if with_sources else [])}]},
        ],
    }


def draft_payload():
    return {"explanation": "Тестовое объяснение.", "facts": [
        {"text": f"Тестовый факт {number}.", "source_urls": [SOURCE_URL]}
        for number in range(5)
    ], "warnings": [], "blocking_issues": [],
        "topic_assessment": {"can_explain_topic": True, "reason": "Факты объясняют основную тему.", "core_fact_ids": [1, 2, 3]}}


class ResearchTests(unittest.TestCase):
    def run_with_transport(self, handler):
        """Подменяет HTTP-транспорт SDK, исключая реальную сеть."""
        client = OpenAI(api_key="test-not-a-real-key", max_retries=0,
                        http_client=httpx.Client(transport=httpx.MockTransport(handler)))
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(research_requests, "load_research_settings", return_value=("test-placeholder", ResearchSettings())), \
             patch.object(research_requests, "OpenAI", return_value=client), patch("builtins.print"):
            result = research_engine.run_research("Тестовая тема", output_root=Path(temporary))
            self.last_journal = json.loads((Path(result["run_directory"]) / "research_usage.json").read_text())
            return result

    def test_full_research_and_save(self):
        requests = []
        def handler(request):
            body = json.loads(request.content)
            requests.append(body)
            payload = search_payload()
            value = collection_payload() if "tools" in body else {"enough": True, "missing_questions": []}
            if "tools" not in body:
                payload["output"] = [payload["output"][1]]
            payload["output"][-1]["content"][0]["text"] = json.dumps(value)
            return httpx.Response(200, json=payload)
        result = self.run_with_transport(handler)
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0]["model"], "gpt-6-luna")
        self.assertEqual(requests[0]["max_tool_calls"], 8)
        self.assertEqual(requests[0]["reasoning"], {"effort": "medium"})
        self.assertEqual(requests[0]["tools"][0]["search_context_size"], "medium")
        self.assertNotIn("tools", requests[1])
        self.assertEqual(requests[1]["reasoning"], {"effort": "low"})
        self.assertTrue(all("max_output_tokens" not in body for body in requests))
        self.assertTrue(result["enough"])
        self.assertEqual(result["schema_version"], 4)
        records = self.last_journal["requests"]
        self.assertEqual([record["transport"]["timeout_seconds"] for record in records], [180, 120])
        self.assertTrue(all(record["transport"]["max_retries"] == 0 for record in records))
        self.assertEqual(records[0]["request"], requests[0])
        self.assertEqual(records[0]["response_id"], "resp_test_search")
        self.assertEqual(records[0]["web"]["calls"][0]["sources"], [])

    def test_web_metadata_does_not_invent_sources_or_queries(self):
        metadata = research_requests.web_metadata({"output": [{"type": "web_search_call", "id": "test", "status": "completed", "action": {"type": "search"}}]})
        self.assertIsNone(metadata["calls"][0]["sources"])
        self.assertIsNone(metadata["calls"][0]["action"].get("queries"))

    def test_no_web_action_does_not_become_research_from_memory(self):
        def handler(request):
            payload = search_payload()
            payload["output"] = [payload["output"][1]]
            payload["output"][0]["content"][0]["text"] = json.dumps(collection_payload())
            return httpx.Response(200, json=payload)
        with self.assertRaisesRegex(research.ResearchError, "веб-инструмента"):
            self.run_with_transport(handler)

    def test_api_errors_do_not_leak_key_or_retry(self):
        for status in (400, 401, 403, 404, 429, 500):
            calls = []
            def handler(request):
                calls.append(request)
                return httpx.Response(status, json={"error": {"message": "test-placeholder denied", "code": "test_error"}})
            with self.subTest(status=status), self.assertRaises(research.ResearchError) as caught:
                self.run_with_transport(handler)
            self.assertNotIn("test-placeholder", str(caught.exception))
            self.assertEqual(len(calls), 1)
            self.assertIn(str(status), str(caught.exception))

    def test_connection_error(self):
        def handler(request):
            raise httpx.ConnectError("Private", request=request)
        with self.assertRaisesRegex(research.ResearchError, "соединения"):
            self.run_with_transport(handler)

    def test_incomplete_response(self):
        def handler(request):
            payload = search_payload()
            payload["status"] = "incomplete"
            return httpx.Response(200, json=payload)
        with self.assertRaisesRegex(research.ResearchError, "полный текст"):
            self.run_with_transport(handler)

    def test_missing_settings(self):
        for settings, message in (({}, "OPENAI_API_KEY"),
                                  ({"OPENAI_API_KEY": "test"}, "OPENAI_MODEL")):
            with patch.object(research, "dotenv_values", return_value=settings):
                with self.assertRaisesRegex(research.ResearchError, message):
                    research.load_settings()

    def test_sources_from_search_metadata(self):
        payload = search_payload(False)
        payload["output"][0]["action"]["sources"] = [{"type": "url", "url": SOURCE_URL}]
        # Используем реальные модели SDK для проверки формы метаданных ответа.
        from openai.types.responses import Response
        response = Response.model_validate(payload)
        self.assertEqual(research.collect_sources(response)[0]["url"], SOURCE_URL)

    def test_invalid_json_response(self):
        def handler(request):
            payload = search_payload()
            payload["output"][-1]["content"][0]["text"] = "Не JSON"
            return httpx.Response(200, json=payload)
        with self.assertRaisesRegex(research.ResearchError, "JSON Schema"):
            self.run_with_transport(handler)

    def test_dry_run_never_runs_research(self):
        with patch("sys.argv", ["main.py", "--topic", "Почему закат красный?", "--dry-run"]), \
             patch.object(pipeline, "research_topic", side_effect=AssertionError("API called")), \
             patch("builtins.print"):
            self.assertEqual(main.main(), 0)

    def test_pipeline_stops_after_insufficient_research(self):
        result = {"topic": "test", "status": "insufficient_data", "explanation": "Недостаточно данных.",
                  "facts": [], "warnings": [], "sources": [], "schema_version": 2,
                  "blocking_issues": ["Нет материала для основного объяснения."],
                  "topic_assessment": {"can_explain_topic": False, "reason": "Нет фактов.", "core_fact_ids": []}}
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(pipeline, "research_topic", return_value=result) as run, \
             patch.object(pipeline, "write_and_check") as write, patch("builtins.print"):
            with self.assertRaisesRegex(research.ResearchError, "insufficient_data"):
                pipeline.run_pipeline("test", output_root=Path(directory))
            self.assertEqual(len(list(Path(directory).glob("*/research.json"))), 1)
            write.assert_not_called()
        self.assertEqual(run.call_count, 1)


if __name__ == "__main__":
    unittest.main()
