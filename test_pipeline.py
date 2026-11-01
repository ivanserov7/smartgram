"""Проверки на подставных ответах. Настоящие факты и внешняя сеть не проверяются."""

import json
from copy import deepcopy
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import httpx
from openai import OpenAI

import artifacts
import fact_checker
import main
import model_api
import pipeline
import scene_planner
from demo_data import DemoResponses, demo_research
from research import ResearchError
from voice_errors import VoiceoverError
from scriptwriter import ScriptDraft, write_script
from test_research import search_payload
from test_grounded_research import grounded_fixture


class ResearchValidationTests(unittest.TestCase):
    """Проверяет схему до сохранения Markdown и запуска дальнейших этапов."""

    def test_missing_fields_for_both_research_statuses(self):
        paths = [
            ("topic",), ("status",), ("explanation",), ("facts",), ("warnings",), ("blocking_issues",), ("sources",),
            ("topic_assessment",), ("topic_assessment", "can_explain_topic"),
            ("topic_assessment", "reason"), ("topic_assessment", "core_fact_ids"),
            ("facts", 0, "text"), ("facts", 0, "sources"),
            ("facts", 0, "sources", 0, "url"), ("facts", 0, "sources", 0, "title"),
            ("sources", 0, "url"), ("sources", 0, "title"),
        ]
        for status in ("researched", "insufficient_data"):
            for path in paths:
                with self.subTest(status=status, path=path):
                    data = demo_research("test", "success")
                    data["status"] = status
                    parent = data
                    for key in path[:-1]:
                        parent = parent[key]
                    parent.pop(path[-1])
                    with self.assertRaises(ResearchError) as caught:
                        artifacts.validate_research(data, demo=True)
                    self.assertIn(path[-1], str(caught.exception))

    def test_invalid_field_types_for_both_research_statuses(self):
        changes = [
            (("topic",), None), (("status",), []), (("explanation",), 123),
            (("warnings",), None), (("warnings",), "строка вместо списка"),
            (("warnings",), [None]), (("warnings",), [{}]),
            (("blocking_issues",), None), (("blocking_issues",), [None]),
            (("topic_assessment",), []), (("topic_assessment", "can_explain_topic"), "true"),
            (("topic_assessment", "reason"), None), (("topic_assessment", "core_fact_ids"), [True]),
            (("facts",), {}), (("facts",), [None]),
            (("facts", 0, "text"), []), (("facts", 0, "sources"), "не список"),
            (("facts", 0, "sources"), [None]), (("facts", 0, "sources", 0, "title"), 42),
            (("facts", 0, "sources", 0, "title"), " "),
            (("sources",), {}), (("sources",), [None]),
            (("sources", 0, "title"), None), (("sources", 0, "url"), []),
            (("sources", 0, "url"), "https://"), (("mode",), []),
        ]
        for status in ("researched", "insufficient_data"):
            for path, value in changes:
                with self.subTest(status=status, path=path, value=value):
                    data = demo_research("test", "success")
                    data["status"] = status
                    parent = data
                    for key in path[:-1]:
                        parent = parent[key]
                    parent[path[-1]] = deepcopy(value)
                    with self.assertRaises(ResearchError):
                        artifacts.validate_research(data, demo=True)

    def test_valid_insufficient_data_can_have_empty_lists(self):
        data = demo_research("test", "insufficient")
        data.update(facts=[], sources=[], limitations=[])
        artifacts.validate_research(data, demo=True)


class RealIntegrationTests(unittest.TestCase):
    """Проверяет реальный код интеграций с подменённым HTTP-транспортом."""

    def test_structured_api_request(self):
        responses = DemoResponses()
        draft = responses.generate(ScriptDraft, "", {})
        requests = []

        def handler(request):
            requests.append(json.loads(request.content))
            payload = search_payload()
            payload["output"] = [payload["output"][1]]
            payload["output"][0]["content"][0]["text"] = draft.model_dump_json()
            return httpx.Response(200, json=payload)

        client = OpenAI(api_key="test-placeholder", http_client=httpx.Client(transport=httpx.MockTransport(handler)))
        with patch.object(model_api, "load_settings", return_value=("test", "test-model")), \
             patch.object(model_api, "OpenAI", return_value=client):
            result = model_api.generate_structured(ScriptDraft, "Сценарий", {"topic": "test"})
        self.assertEqual(result, draft)
        self.assertTrue(requests[0]["text"]["format"]["strict"])
        self.assertFalse(requests[0]["store"])
        self.assertNotIn("tools", requests[0])


    def test_api_errors_do_not_expose_body(self):
        for status in (401, 429, 500):
            with self.subTest(status=status):
                transport = httpx.MockTransport(lambda request: httpx.Response(status, json={
                    "error": {"message": "DO-NOT-PRINT-THIS", "type": "api_error"}}))
                client = OpenAI(api_key="test-placeholder", max_retries=0,
                                http_client=httpx.Client(transport=transport))
                with patch.object(model_api, "load_settings", return_value=("test", "test-model")), \
                     patch.object(model_api, "OpenAI", return_value=client):
                    with self.assertRaises(ResearchError) as caught:
                        model_api.generate_structured(ScriptDraft, "test", {})
                self.assertNotIn("DO-NOT-PRINT-THIS", str(caught.exception))






if __name__ == "__main__":
    unittest.main()
