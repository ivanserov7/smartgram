"""Исследование на подставных страницах и ответах API. Настоящая сеть запрещена."""

import copy
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from artifacts import validate_research
from demo_data import DEMO_TEXTS, DemoResponses, demo_research
import main
import pipeline
import research_engine as engine
from research import ResearchError
from research_evidence import core_support, text_hash


URLS = ["https://institute.example/biology", "https://university.example/biology"]
TEXT = " ".join(DEMO_TEXTS)
LIMITS = {"RESEARCH_MAX_SEARCH_ROUNDS": 2, "RESEARCH_MAX_PAGES": 10}


def fake_page(url):
    return {"url": url, "status": "available", "text": TEXT + " " + url, "final_url": url, "reason": "", "truncated": False}


def review_for(url):
    return {"url": url, "decision": "accepted", "reason": "ТЕСТ: страница по теме.", "authority_reason": "ТЕСТ: автор компетентен по тексту.",
            "material_kind": "Тестовый научный материал", "independence_group": url, "original_work_url": url, "derived_from_urls": []}


def stage_payload(name, data):
    if name == "SourceReviews":
        return {"reviews": [review_for(page["url"]) for page in data["pages"]]}
    if name == "ExtractedFacts":
        return {"facts": [{"text": text, "evidence": [{"url": page["url"], "quote": text} for page in data["pages"]]}
                          for text in DEMO_TEXTS], "warnings": []}
    if name == "EvidenceChecks":
        return {"checks": [{"fact_id": fact["fact_id"], "evidence_id": item["evidence_id"], "verdict": "supported", "reason": "ТЕСТ: подтверждено подставным проверяющим."}
                           for fact in data["facts"] for item in fact["evidence"]]}
    if name == "ResearchAssessment":
        return {"explanation": "ТЕСТ: подставное объяснение.", "warnings": [], "blocking_issues": [],
                "topic_assessment": {"can_explain_topic": True, "reason": "ТЕСТ: достаточно материала.", "core_fact_ids": [2, 3, 4]}}
    raise AssertionError(name)


class FakeResearchAPI:
    model = "test-no-real-api"

    def __init__(self, sources=None, change=None):
        self.sources = sources if sources is not None else [{"url": url, "title": "Тестовый источник"} for url in URLS]
        self.change = change
        self.calls = []

    def close(self):
        pass

    def search(self, topic, number, feedback):
        self.calls.append(("search", {"round": number, "feedback": feedback}))
        return self.sources(number) if callable(self.sources) else self.sources

    def generate(self, schema, instructions, data):
        self.calls.append((schema.__name__, copy.deepcopy(data)))
        value = stage_payload(schema.__name__, data)
        if self.change:
            self.change(schema.__name__, value)
        return schema.model_validate(value)


def grounded_fixture():
    result = demo_research("Тестовая тема", "success")
    pages = [{**fake_page(url), "title": "Тестовый источник", "assessment": review_for(url), "text_sha256": text_hash(fake_page(url)["text"])} for url in URLS]
    for fact in result["facts"]:
        fact["sources"] = [{"url": url, "title": "Тестовый источник"} for url in URLS]
        fact["evidence"] = [{"url": page["url"], "quote": fact["text"], "text_sha256": page["text_sha256"],
                             "semantic_status": "supported", "semantic_reason": "ТЕСТ"} for page in pages]
    result.update(schema_version=3, mode="real", sources=[{"url": url, "title": "Тестовый источник"} for url in URLS], source_pages=pages)
    result["core_support"] = core_support(result["facts"], result["topic_assessment"], pages)
    return result




class ResearchReportTests(unittest.TestCase):
    def test_report_includes_rejections_quotes_independence_and_usage(self):
        from research_report import render_report
        result = grounded_fixture()
        result["source_pages"].append({"url": "https://blocked.example/page", "title": "ТЕСТ", "status": "unavailable",
                                       "reason": "HTTP 403", "text": "", "assessment": {"decision": "rejected", "reason": "HTTP 403"}})
        page = render_report(result, {"requests": [{"operation": "SourceReviews", "model": "test-exact-model",
                                                  "status": "completed", "usage": {"input_tokens": 123, "output_tokens": 45}}]})
        self.assertIn("HTTP 403", page)
        self.assertIn("test-exact-model", page)
        self.assertIn("123", page)
        self.assertIn("<blockquote>", page)
        self.assertIn("независимых групп 2", page)
        self.assertIn("ТЕСТ: автор компетентен", page)

    def test_equal_text_and_unknown_origin_do_not_count_as_two_sources(self):
        result = grounded_fixture()
        result["source_pages"][1]["text"] = result["source_pages"][0]["text"]
        proof = core_support(result["facts"], result["topic_assessment"], result["source_pages"])
        self.assertEqual(proof["status"], "single_source")
        result = grounded_fixture()
        for page in result["source_pages"]:
            page["assessment"]["independence_group"] = "unknown"
            page["assessment"]["original_work_url"] = ""
        proof = core_support(result["facts"], result["topic_assessment"], result["source_pages"])
        self.assertEqual(proof["status"], "single_source")


    def test_same_publisher_different_original_works_can_be_independent(self):
        from research_evidence import independent_groups
        pages = []
        urls = ["https://journal.example/article-a", "https://journal.example/article-b"]
        for number, url in enumerate(urls):
            page = {**fake_page(url), "assessment": review_for(url)}
            page["assessment"].update(independence_group="original", original_work_url=f"https://papers.example/study-{number}")
            pages.append(page)
        self.assertEqual(len(independent_groups(urls, pages)), 2)

    def test_unknown_origin_does_not_add_to_one_known_confirmation(self):
        from research_evidence import independent_groups
        pages = [{**fake_page(url), "assessment": review_for(url)} for url in URLS]
        pages[1]["assessment"].update(independence_group="unknown", original_work_url="")
        self.assertEqual(len(independent_groups(URLS, pages)), 1)




if __name__ == "__main__":
    unittest.main()
