"""Provider cache and search-budget regressions; every HTTP call is mocked."""

import copy
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import httpx

from commons_requests import CommonsPaused, CommonsRequests
from fast_materials import FastMaterials, candidate_id
from file_io import atomic_write_json
from pexels_materials import search, verify_source
from production_state import config, read
from test_pexels_materials import photo


class ProviderCacheTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings = copy.deepcopy(config()["visuals"])

    def runner(self, name):
        runner = FastMaterials(self.root / name, self.settings, Mock())
        runner.limits = self.settings["search"]
        return runner

    def prior_pexels_pool(self, *, tamper=False):
        previous = self.runner("previous")
        with httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"photos": [photo()]})
            )
        ) as client, patch(
            "pexels_materials.dotenv_values",
            return_value={"PEXELS_API_KEY": "offline-placeholder"},
        ):
            gateway = CommonsRequests(
                previous.journal,
                {"number": 1, "calls": 0},
                "previous",
                client,
                self.settings["search"],
                sleep=lambda seconds: None,
            )
            candidate = search(gateway, "ocean")["candidates"][0]
        if tamper:
            candidate["author"] = "Unverified replacement"
        cid = candidate_id(candidate)
        previous.pool["candidates"][cid] = dict(candidate, candidate_id=cid)
        previous.save_pool()
        target = self.root / "current"
        target.mkdir()
        atomic_write_json(
            target / "visual_previous.json", {"directory": str(previous.directory)}
        )
        return self.runner("current"), cid

    def test_current_fast_pool_is_imported_and_pexels_provenance_copied(self):
        runner, cid = self.prior_pexels_pool()
        self.assertIn(cid, runner.pool["candidates"])
        gateway = SimpleNamespace(get=Mock(side_effect=AssertionError("No Commons query")))
        runner.refresh_pool({"budget": {}}, gateway)
        candidate = runner.pool["candidates"][cid]
        self.assertFalse(candidate["needs_refresh"])
        self.assertNotIn(cid, runner.pool["global_rejections"])
        verify_source(candidate, runner.directory)
        gateway.get.assert_not_called()
        saved = read(runner.pool_path)
        self.assertFalse(saved["candidates"][cid]["needs_refresh"])

    def test_pexels_process_key_takes_precedence_over_local_file(self):
        runner = self.runner("environment")
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={"photos": [photo()]})

        with httpx.Client(transport=httpx.MockTransport(handler)) as client, patch.dict(
            os.environ, {"PEXELS_API_KEY": "process-placeholder"}
        ), patch("pexels_materials.dotenv_values", return_value={"PEXELS_API_KEY": "file-placeholder"}):
            gateway = CommonsRequests(
                runner.journal, {"number": 1, "calls": 0}, "environment", client,
                self.settings["search"], sleep=lambda seconds: None,
            )
            result = search(gateway, "ocean")
        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(requests[0].headers["authorization"], "process-placeholder")

    def test_tampered_pexels_provenance_is_rejected_before_copy(self):
        runner, cid = self.prior_pexels_pool(tamper=True)
        gateway = SimpleNamespace(get=Mock(side_effect=AssertionError("No Commons query")))
        runner.refresh_pool({"budget": {}}, gateway)
        self.assertIn(cid, runner.pool["global_rejections"])
        self.assertFalse(runner.pool["candidates"][cid]["needs_refresh"])
        gateway.get.assert_not_called()

    def test_cached_snapshot_never_overwrites_current_provider_evidence(self):
        runner, cid = self.prior_pexels_pool()
        relative = runner.pool["candidates"][cid]["rights_evidence"]["snapshot_path"]
        destination = runner.directory / relative
        destination.parent.mkdir(parents=True)
        current = {"photos": [], "current_response": True}
        atomic_write_json(destination, current)
        runner.refresh_pool({"budget": {}}, SimpleNamespace(get=Mock()))
        self.assertIn(cid, runner.pool["global_rejections"])
        self.assertEqual(read(destination), current)

    def search_job(self):
        rd = {"number": 1, "calls": 0, "outcomes": []}
        job = {"rounds": [rd], "budget": {}}
        shots = [{"queries": ["ocean"], "visual_task": "Ocean"}]
        return job, rd, shots

    def test_empty_optional_provider_on_last_call_stops_search_normally(self):
        runner = self.runner("budget")
        runner.limits["max_search_api_calls_per_fragment_per_round"] = 1
        job, rd, shots = self.search_job()
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={"photos": []})

        with httpx.Client(transport=httpx.MockTransport(handler)) as client, patch(
            "pexels_materials.dotenv_values",
            return_value={"PEXELS_API_KEY": "offline-placeholder"},
        ):
            gateway = CommonsRequests(
                runner.journal, rd, "budget", client, runner.limits,
                sleep=lambda seconds: None,
            )
            runner.acquire_cards(job, rd, gateway, shots)
        self.assertEqual([request.url.host for request in requests], ["api.pexels.com"])
        self.assertEqual(rd["calls"], 1)
        self.assertEqual(len(job["search_stops"]), 1)
        self.assertEqual(job["search_stops"][0]["query"], "ocean")

    def test_service_pause_is_not_disguised_as_search_exhaustion(self):
        runner = self.runner("service")
        job, rd, shots = self.search_job()
        with patch("fast_materials.search_first", side_effect=CommonsPaused("Service unavailable")):
            with self.assertRaises(CommonsPaused):
                runner.acquire_cards(job, rd, Mock(), shots)
        self.assertNotIn("search_stops", job)

    def test_exhausted_provider_route_reaches_fragment_generation(self):
        runner = self.runner("fallback")
        runner.limits.update(
            max_search_api_calls_per_fragment_per_round=1,
            search_rounds_limit=1,
        )
        atomic_write_json(runner.directory / "research.json", {"facts": []})
        shot = {"shot_id": "s1", "queries": ["ocean"], "visual_task": "Ocean"}
        runner.plan = Mock(side_effect=lambda fragment, index, job: job.update(shots=[shot]))
        runner.examine = Mock()
        runner.fragment_result = Mock(return_value={"status": "approved"})

        def generated(job, rd):
            job["approved"]["s1"] = {"status": "approved"}

        runner.create_missing = Mock(side_effect=generated)
        client = httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"photos": []})
            )
        )
        with patch("fast_materials.create_commons_client", return_value=client), patch(
            "pexels_materials.dotenv_values",
            return_value={"PEXELS_API_KEY": "offline-placeholder"},
        ):
            result = runner.fragment({"text": "An ocean scene"}, 1)
        self.assertEqual(result["status"], "approved")
        runner.create_missing.assert_called_once()
        job = next(iter(runner.journal.state["jobs"].values()))
        self.assertEqual(job["status"], "approved")
        self.assertEqual(len(job["search_stops"]), 1)


if __name__ == "__main__":
    unittest.main()
