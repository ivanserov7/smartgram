"""Регрессии достаточности исследования и миграции. Все ответы модели подставные."""

import copy
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import httpx
from openai import OpenAI
from openai.types.responses import Response

import artifacts
import demo_data
import pipeline
import model_api
import reevaluate_research as reassessment
import research
import scriptwriter
from research import ResearchError
from test_research import SOURCE_URL, draft_payload, search_payload


SUNSET_WARNINGS = [
    "Нет количественной оценки влияния пыли и влаги.",
    "Нет региональных данных о метеоусловиях и загрязнении.",
    "Не рассмотрено влияние облачности на оттенки заката.",
    "Нет экспериментальных спектральных измерений для разных условий.",
]


def sunset_result(count=7, warnings=None, blocking_issues=None, can_explain=True):
    draft = draft_payload()
    texts = [*demo_data.DEMO_TEXTS,
             "Свет разных длин волн рассеивается в воздухе по-разному.",
             "Красный цвет заката связан с более длинным путём света в атмосфере."]
    draft.update(facts=[{"text": text, "source_urls": [SOURCE_URL]} for text in texts[:count]],
                 warnings=warnings or [], blocking_issues=blocking_issues or [],
                 topic_assessment={"can_explain_topic": can_explain,
                                   "reason": "Факты описывают спектр света, рассеяние и путь через атмосферу." if can_explain else "Нет достоверного основного механизма.",
                                   "core_fact_ids": list(range(1, min(count, 4) + 1)) if can_explain else []})
    return research.build_result("Почему закат красный?", "test-model", Response.model_validate(search_payload()),
                                 research.ResearchDraft.model_validate(draft))


def legacy_result():
    result = sunset_result(warnings=SUNSET_WARNINGS)
    for field in ("schema_version", "warnings", "blocking_issues", "topic_assessment"):
        result.pop(field)
    result.update(status="insufficient_data", limitations=list(SUNSET_WARNINGS))
    return result


def reassessment_draft(can_explain=True, classification="warning"):
    return reassessment.ReassessmentDraft(
        warnings=["Не использовать неподтверждённые численные детали."], blocking_issues=[],
        topic_assessment={"can_explain_topic": can_explain,
                          "reason": "Сохранённые факты объясняют общий механизм." if can_explain else "Главный механизм противоречив.",
                          "core_fact_ids": [2, 3, 4] if can_explain else []},
        limitation_reviews=[{"limitation_id": number, "classification": classification,
                             "reason": "Необязательная деталь общего объяснения."}
                            for number in range(1, 5)],
    )


class ResearchPolicyTests(unittest.TestCase):
    def test_seven_facts_and_four_minor_warnings_continue_pipeline(self):
        result = sunset_result(warnings=SUNSET_WARNINGS)
        artifacts.validate_research(result)
        self.assertEqual(result["status"], "researched")
        self.assertEqual(result["warnings"], SUNSET_WARNINGS)
        self.assertFalse(result["blocking_issues"])
        # Явный demo проверяет продолжение до остальных подставных этапов, без настоящих API.
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(demo_data, "demo_research", return_value={**result, "mode": "demo"}), redirect_stdout(StringIO()):
            folder = pipeline.run_pipeline(topic=result["topic"], demo=True, output_root=Path(temporary), stop_after="script")
            self.assertTrue((folder / "script.json").exists())
            state = json.loads((folder / "run.json").read_text())
            self.assertEqual(state["status"], "script_completed")

    def test_main_explanation_problem_stops_before_writer_and_is_saved(self):
        result = sunset_result(blocking_issues=["Центральное утверждение противоречит источникам."], can_explain=False)
        artifacts.validate_research(result)
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(pipeline, "research_topic", return_value=result), \
             patch.object(pipeline, "write_and_check") as writer, redirect_stdout(StringIO()):
            with self.assertRaisesRegex(ResearchError, "Центральное утверждение"):
                pipeline.run_pipeline(topic=result["topic"], output_root=Path(temporary))
            writer.assert_not_called()
            folder = next(Path(temporary).iterdir())
            self.assertEqual(json.loads((folder / "run.json").read_text())["status"], "stopped")
            self.assertTrue((folder / "research.md").exists())

    def test_three_substantive_facts_are_allowed(self):
        result = sunset_result(count=3)
        artifacts.validate_research(result)
        self.assertEqual(result["status"], "researched")
        self.assertEqual(len(result["facts"]), 3)

    def test_number_alone_does_not_make_topic_answerable(self):
        result = sunset_result(count=7, can_explain=False)
        artifacts.validate_research(result)
        self.assertEqual(result["status"], "insufficient_data")
        self.assertIn("основного механизма", " ".join(result["blocking_issues"]))

    def test_two_facts_stop_without_padding(self):
        result = sunset_result(count=2)
        artifacts.validate_research(result)
        self.assertEqual(len(result["facts"]), 2)
        self.assertEqual(result["status"], "insufficient_data")

    def test_missing_noncore_source_is_warning_if_main_explanation_survives(self):
        draft = draft_payload()
        draft["facts"][-1]["source_urls"] = ["https://invented.invalid/"]
        result = research.build_result("Тема", "test-model", Response.model_validate(search_payload()),
                                       research.ResearchDraft.model_validate(draft))
        artifacts.validate_research(result)
        self.assertEqual(result["status"], "researched")
        self.assertEqual(len(result["facts"]), 4)
        self.assertTrue(result["warnings"])

    def test_validator_does_not_accept_blind_status_upgrade(self):
        for change in ("assessment", "blockers", "ids", "sources"):
            with self.subTest(change=change):
                result = sunset_result()
                if change == "assessment":
                    result["topic_assessment"]["can_explain_topic"] = False
                elif change == "blockers":
                    result["blocking_issues"] = ["Нет основного объяснения"]
                elif change == "ids":
                    result["topic_assessment"]["core_fact_ids"] = []
                else:
                    result["sources"] = []
                with self.assertRaises(ResearchError):
                    artifacts.validate_research(result)

    def test_writer_receives_warnings_and_historical_limitations(self):
        result = sunset_result(warnings=SUNSET_WARNINGS)
        result["limitations"] = list(SUNSET_WARNINGS)
        calls = []
        responses = demo_data.DemoResponses()

        def generate(schema, instructions, data):
            calls.append((instructions, data))
            return responses.generate(schema, instructions, data)

        scriptwriter.write_script({**result, "mode": "demo"}, generate=generate, demo=True)
        instructions, data = calls[0]
        self.assertEqual(data["research"]["warnings"], SUNSET_WARNINGS)
        self.assertEqual(data["research"]["limitations"], SUNSET_WARNINGS)
        self.assertEqual(data["research"]["topic_assessment"], result["topic_assessment"])
        self.assertIn("существенные оговорки", instructions)


class ReassessmentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.path = self.folder / "research.json"
        self.original = legacy_result()
        self.path.write_text(json.dumps(self.original, ensure_ascii=False))

    def test_old_research_requires_explicit_reassessment(self):
        original = self.path.read_bytes()
        with self.assertRaisesRegex(ResearchError, "явной переоценки"):
            artifacts.load_research(self.path)
        self.assertEqual(self.path.read_bytes(), original)

    def test_reassessment_preserves_original_warnings_and_sources_without_search(self):
        original = self.path.read_bytes()
        calls = []

        def generate(schema, instructions, data):
            calls.append(data)
            return reassessment_draft()

        with patch.object(research, "research_topic", side_effect=AssertionError("Search forbidden")), redirect_stdout(StringIO()):
            folder = reassessment.reevaluate_research(self.path, generate=generate)
        result = artifacts.load_research(folder / "research.json")
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(result["status"], "researched")
        self.assertEqual(result["limitations"], SUNSET_WARNINGS)
        self.assertTrue(all(note in result["warnings"] for note in SUNSET_WARNINGS))
        self.assertEqual(result["facts"], self.original["facts"])
        self.assertEqual(result["sources"], self.original["sources"])
        self.assertEqual(result["reevaluation"]["previous_status"], "insufficient_data")
        self.assertFalse(result["reevaluation"]["web_search_performed"])

    def test_real_blocking_problem_is_not_promoted_to_researched(self):
        result = reassessment.apply_reassessment(self.original, reassessment_draft(can_explain=False))
        self.assertEqual(result["status"], "insufficient_data")
        self.assertIn("Главный механизм противоречив", " ".join(result["blocking_issues"]))
        result = reassessment.apply_reassessment(self.original, reassessment_draft(classification="blocking_issue"))
        self.assertTrue(all(note in result["blocking_issues"] for note in SUNSET_WARNINGS))
        self.assertEqual(result["status"], "insufficient_data")

    def test_omitted_duplicate_or_unknown_legacy_note_is_rejected(self):
        for ids in ([1, 2, 3], [1, 2, 3, 3], [1, 2, 3, 9]):
            with self.subTest(ids=ids):
                draft = reassessment_draft()
                draft.limitation_reviews = [reassessment.LimitationReview(limitation_id=number, classification="warning", reason="Тест") for number in ids]
                with self.assertRaisesRegex(ResearchError, "номера исходных"):
                    reassessment.apply_reassessment(self.original, draft)

    def test_invalid_core_fact_ids_reject_reassessment(self):
        draft = reassessment_draft()
        draft.topic_assessment.core_fact_ids = [999]
        with self.assertRaisesRegex(ResearchError, "core_fact_ids"):
            reassessment.apply_reassessment(self.original, draft)

    def test_legacy_fields_still_validated(self):
        for field, value in (("limitations", None), ("limitations", [None]), ("sources", [{}])):
            data = copy.deepcopy(self.original)
            data[field] = value
            with self.subTest(field=field), self.assertRaises(ResearchError):
                artifacts.validate_research(data, allow_legacy=True)

    def test_dry_run_reads_nothing_and_calls_no_api(self):
        with patch("sys.argv", ["reevaluate_research.py", "--research-file", "/missing/research.json", "--dry-run"]), \
             patch.object(reassessment, "reevaluate_research", side_effect=AssertionError("API forbidden")), redirect_stdout(StringIO()):
            self.assertEqual(reassessment.main(), 0)

    def test_reassessment_uses_one_structured_request_without_web_tool(self):
        requests = []

        def handler(request):
            requests.append(json.loads(request.content))
            payload = search_payload()
            payload["output"] = [payload["output"][1]]
            payload["output"][0]["content"][0]["text"] = reassessment_draft().model_dump_json()
            return httpx.Response(200, json=payload)

        client = OpenAI(api_key="test-placeholder", max_retries=0,
                        http_client=httpx.Client(transport=httpx.MockTransport(handler)))
        with patch.object(model_api, "load_settings", return_value=("test-placeholder", "test-model")), \
             patch.object(model_api, "OpenAI", return_value=client) as constructor, redirect_stdout(StringIO()):
            folder = reassessment.reevaluate_research(self.path, generate=model_api.generate_structured)
        self.assertEqual(len(requests), 1)
        self.assertNotIn("tools", requests[0])
        self.assertEqual(constructor.call_args.kwargs["max_retries"], 0)
        self.assertEqual(requests[0]["text"]["format"]["name"], "ReassessmentDraft")
        self.assertEqual(artifacts.load_research(folder / "research.json")["status"], "researched")


if __name__ == "__main__":
    unittest.main()
