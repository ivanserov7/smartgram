"""Costs/Usage и распределение: только временные папки и подставной HTTP, без платных API."""

import copy
import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import httpx
from openai import OpenAI

from billing_api import BillingClient, BillingError, BillingSettings, discover_scope, load_billing_settings
from billing_allocation import canonical_rows, distribute
from billing_history import import_calls, consumption
from billing_report import build_analytics
from billing_sync import sync_billing
from billing_sync import refresh_after_report as refresh_billing_under_test
from openai_meter import OpenAIMeter

SCOPE = {"project_id": "proj_test", "api_key_ids": ["key_test"], "key_fingerprints": {}}
SETTINGS = BillingSettings("test-admin-value", "test-generation-value", "proj_test", ("key_test",), max_retries=0)


def page(results, start=0, end=86400, **extra):
    return {"object": "page", "data": [{"start_time": start, "end_time": end, "results": results}], "has_more": False, "next_page": None, **extra}


def cost(value="1.000000000000000001", item="test-model, input_tokens"):
    return {"object": "organization.costs.result", "project_id": "proj_test", "api_key_id": "key_test", "line_item": item,
            "amount": {"value": value, "currency": "usd"}}


def usage():
    return {"project_id": "proj_test", "api_key_id": "key_test", "model": "test-model", "service_tier": "default", "batch": False,
            "num_model_requests": 2, "input_tokens": 150, "output_tokens": 80, "input_cached_tokens": 30, "input_cache_write_tokens": 30}


def call(number, incoming, outgoing, cached, write):
    raw = {"input_tokens": incoming, "output_tokens": outgoing, "input_tokens_details": {"cached_tokens": cached, "cache_write_tokens": write},
           "output_tokens_details": {"reasoning_tokens": outgoing // 2}}
    return {"call_id": f"call_{number}", "video_id": f"video_{number}", "video_title": "Тест", "run_id": f"run_{number}", "stage": "test",
            "api_key_id": "key_test", "project_id": "proj_test", "model": "test-model", "service_tier": "default", "batch": False,
            "start_time": number, "status": "completed", "usage": raw, "units": consumption(raw), "duration_seconds": 1,
            "attempts": [{"attempt_number": 1}], "attempts_complete": True, "retry_attempts_observed": 0,
            "allocated_costs": {}, "missing_historical_fields": [], "estimated_cost": None}


def two_calls():
    return [call(1, 100, 20, 20, 20), call(2, 50, 60, 10, 10)]


class BillingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def client(self, handler, settings=SETTINGS):
        client = httpx.Client(transport=httpx.MockTransport(handler))
        self.addCleanup(client.close)
        return BillingClient(settings, client=client, sleep=lambda _: None)

    def test_pagination_and_precise_decimal(self):
        requests = []
        def handler(request):
            requests.append(request)
            second = request.url.params.get("page") == "next"
            text = '{"data":[{"start_time":0,"end_time":86400,"results":[{"amount":{"currency":"usd","value":0.1234567890123456789}}]}],"has_more":false}' if second else '{"data":[],"has_more":true,"next_page":"next"}'
            return httpx.Response(200, text=text)
        pages = self.client(handler).pages("/organization/costs", [("bucket_width", "1d")])
        self.assertEqual(len(requests), 2)
        self.assertEqual(pages[1]["data"][0]["results"][0]["amount"]["value"], Decimal("0.1234567890123456789"))
        self.assertEqual(requests[0].headers["authorization"], "Bearer test-admin-value")

    def test_duplicate_pages_do_not_double_count_conflicting_duplicate_rejected(self):
        first = page([cost()])
        rows = canonical_rows([first, first], "costs", SCOPE)
        self.assertEqual(len(rows), 1)
        with self.assertRaises(BillingError):
            canonical_rows([first, page([cost("2")])], "costs", SCOPE)
        foreign = cost()
        foreign["api_key_id"] = "key_other"
        with self.assertRaises(BillingError):
            canonical_rows([page([foreign])], "costs", SCOPE)

    def test_bad_pagination_no_infinite_loop(self):
        client = self.client(lambda _: httpx.Response(200, json={"data": [], "has_more": True, "next_page": "same"}))
        with self.assertRaisesRegex(BillingError, "пагинация"):
            client.pages("/organization/costs", [])
        self.assertEqual(client.request_count, 2)

    def test_authorization_errors_are_not_zero_and_not_retried(self):
        for status in (401, 403):
            client = self.client(lambda _: httpx.Response(status, json={"error": {"message": "test-admin-value", "code": "denied"}}),
                                 BillingSettings("test-admin-value", "other", max_retries=2))
            with self.assertRaisesRegex(BillingError, f"HTTP {status}") as caught:
                client.pages("/organization/costs", [])
            self.assertNotIn("test-admin-value", str(caught.exception))
            self.assertEqual(client.request_count, 1)

    def test_network_and_temporary_http_errors_have_bounded_retries(self):
        for kind in ("network", "http"):
            def handler(request):
                if kind == "network":
                    raise httpx.ReadTimeout("private details", request=request)
                return httpx.Response(503, json={"error": {"message": "temporary"}})
            client = self.client(handler, BillingSettings("test-admin-value", "other", max_retries=2))
            with self.assertRaises(BillingError):
                client.pages("/organization/costs", [])
            self.assertEqual(client.request_count, 3)

    def test_discovery_uses_mask_not_all_organization_costs(self):
        def handler(request):
            if request.url.path.endswith("/projects"):
                return httpx.Response(200, json={"data": [{"id": "proj_test"}], "has_more": False})
            return httpx.Response(200, json={"data": [{"id": "key_test", "redacted_value": "test-...value"}], "has_more": False})
        client = self.client(handler, BillingSettings("test-admin-value", "test-generation-value"))
        scope = discover_scope(client)
        self.assertEqual(scope["api_key_ids"], ["key_test"])
        self.assertEqual(scope["key_fingerprints"][hashlib.sha256(b"test-generation-value").hexdigest()], "key_test")
        client = self.client(lambda _: httpx.Response(200, json={"data": [], "has_more": False}), BillingSettings("test", "other"))
        with self.assertRaisesRegex(BillingError, "однозначно"):
            discover_scope(client)

    def test_input_output_and_cache_allocated_separately(self):
        rows = canonical_rows([page([cost("1", "test-model, input_tokens"), cost("2", "test-model, output_tokens"),
                                           cost("0.3", "test-model, cached_input_tokens")])], "costs", SCOPE)
        calls = two_calls()
        result = distribute(rows, calls, canonical_rows([page([usage()])], "usage", SCOPE))
        self.assertEqual(result["actual"], {"usd": "3.3"})
        self.assertEqual(result["allocated"], {"usd": "3.3"})
        self.assertEqual(result["unallocated"], {"usd": "0"})
        output_shares = result["rows"][1]["shares"]
        self.assertEqual([Decimal(item["amount"]) for item in output_shares], [Decimal("0.5"), Decimal("1.5")])
        self.assertEqual(calls[0]["units"]["output_tokens"], 20)  # reasoning не складывается повторно.
        self.assertEqual(sum(Decimal(call["allocated_costs"]["usd"]) for call in calls), Decimal("3.3"))
        again = distribute(rows, calls, canonical_rows([page([usage()])], "usage", SCOPE))
        self.assertEqual(again, result)

    def test_exact_residual_and_updated_charges(self):
        rows = canonical_rows([page([cost()])], "costs", SCOPE)
        calls = two_calls()
        result = distribute(rows, calls, canonical_rows([page([usage()])], "usage", SCOPE))
        self.assertEqual(sum(Decimal(item["amount"]) for item in result["rows"][0]["shares"]), Decimal(cost()["amount"]["value"]))
        rows[0]["amount"]["value"] = "2"
        result = distribute(rows, calls, canonical_rows([page([usage()])], "usage", SCOPE))
        self.assertEqual(sum(Decimal(call["allocated_costs"]["usd"]) for call in calls), Decimal("2"))

    def test_partial_history_does_not_reweight_whole_bill_or_block_known_response(self):
        rows=canonical_rows([page([cost()])],'costs',SCOPE)
        provider=canonical_rows([page([usage()])],'usage',SCOPE)
        calls=two_calls()[:1]
        result=distribute(rows,calls,provider)
        self.assertGreater(Decimal(result['allocated']['usd']),0)
        self.assertGreater(Decimal(result['unallocated']['usd']),0)
        self.assertEqual(Decimal(result['actual']['usd']),Decimal(result['allocated']['usd'])+Decimal(result['unallocated']['usd']))
        calls=two_calls();calls[0]['attempts'].append({'attempt_number':2})
        result=distribute(rows,calls,provider)
        self.assertEqual(result['allocated'],result['actual'])
        self.assertFalse(calls[0]['allocation_complete'])
        for item in ['storage per hour','test-model, web_search_calls']:
            current=copy.deepcopy(rows);current[0]['line_item']=item
            result=distribute(current,two_calls(),provider)
            self.assertEqual(result['actual'],result['unallocated'])

    def test_legacy_import_deduplicates_and_does_not_guess_key_id(self):
        folder = self.root / "run"
        folder.mkdir()
        record = {"response_id": "resp_same", "model": "test-model", "usage": {"input_tokens": 10, "output_tokens": 2}, "status": "completed"}
        for name in ("research_usage.json", "script_usage.json"):
            (folder / name).write_text(json.dumps({"requests": [record]}))
        calls, warnings = import_calls(self.root, SCOPE)
        self.assertEqual(len(calls), 1)
        self.assertIsNone(calls[0]["api_key_id"])
        self.assertIsNone(calls[0]["start_time"])
        self.assertIn("api_key_id", calls[0]["missing_historical_fields"])
        self.assertIsNone(calls[0]["units"]["cache_write_tokens"])
        self.assertFalse(warnings)

    def test_missing_key_and_no_ready_videos_do_not_become_zero(self):
        with patch("billing_sync.load_billing_settings", return_value=BillingSettings("", "")):
            data, paths = sync_billing(self.root / "billing", self.root)
        self.assertIsNone(data["actual_charges"])
        self.assertIsNone(data["average_ready_video_cost"])
        self.assertIn("OPENAI_ADMIN_KEY", data["sync"]["error"])
        self.assertTrue(paths[1].exists())

    def test_resync_updates_snapshot_no_duplicates_and_keeps_previous_on_failure(self):
        directory = self.root / "billing"
        now = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
        start = int(now.replace(hour=0).timestamp())
        charge = ["1.23"]
        def factory(settings):
            def handler(request):
                if charge[0] == "error":
                    return httpx.Response(403, json={"error": {"code": "denied"}})
                values = [cost(charge[0])] if request.url.path.endswith("/costs") else []
                self.assertEqual(request.url.params.get_list("api_key_ids"), ["key_test"])
                return httpx.Response(200, json=page(values, start, start+86400))
            return self.client(handler)
        with patch("billing_sync.load_billing_settings", return_value=SETTINGS):
            first, _ = sync_billing(directory, self.root, factory, now=now)
            charge[0] = "2.34"
            second, _ = sync_billing(directory, self.root, factory, now=now+timedelta(seconds=1))
            charge[0] = "error"
            third, _ = sync_billing(directory, self.root, factory, now=now+timedelta(seconds=2))
        self.assertEqual(first["actual_charges"], {"usd": "1.23"})
        self.assertEqual(second["actual_charges"], {"usd": "2.34"})
        self.assertEqual(third["actual_charges"], second["actual_charges"])
        self.assertTrue(third["sync"]["error"])
        self.assertEqual(third["sync"]["last_success_at_utc"], second["sync"]["last_success_at_utc"])
        self.assertEqual(len(json.loads((directory / "snapshot.json").read_text())["costs"]), 1)
        self.assertNotIn(SETTINGS.admin_key, (directory / "analytics.json").read_text())

    def test_no_rows_and_explicit_zero_are_different(self):
        state = {"last_success_at_utc": "now"}
        snapshot = {"period": {"start_time": 0, "end_time": 86400}, "scope": SCOPE, "costs": []}
        empty = build_analytics(snapshot, [], distribute([], [], []), {}, state, [])
        snapshot["costs"] = canonical_rows([page([cost("0")])], "costs", SCOPE)
        zero = build_analytics(snapshot, [], distribute(snapshot["costs"], [], []), {}, state, [])
        self.assertIsNone(empty["actual_charges"])
        self.assertEqual(zero["actual_charges"], {"usd": "0"})

    def test_meter_observes_sdk_retries_and_redacts_errors(self):
        from test_research import search_payload
        sent = []
        def handler(request):
            sent.append(request)
            if len(sent) == 1:
                return httpx.Response(500, headers={"x-request-id": "req_failed"}, json={"error": {"message": "temporary"}})
            payload = search_payload()
            payload["usage"] = {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14}
            payload["service_tier"] = "default"
            return httpx.Response(200, headers={"x-request-id": "req_ok"}, json=payload)
        client = OpenAI(api_key="test-generation-value", max_retries=1, http_client=httpx.Client(transport=httpx.MockTransport(handler)))
        self.addCleanup(client.close)
        meter = OpenAIMeter(self.root, "test_stage", "test-generation-value")
        response = meter.create(client, model="test-model", input="test")
        self.assertEqual(len(sent), 2)
        self.assertEqual(len(meter.telemetry["attempts"]), 2)
        self.assertIsNone(meter.telemetry["attempts"][0]["usage"])
        self.assertEqual(meter.telemetry["provider_request_id"], "req_ok")
        self.assertEqual(meter.telemetry["usage"]["input_tokens"], 10)
        self.assertNotIn("test-generation-value", (self.root / "openai_usage.json").read_text())
        self.assertNotIn("authorization", (self.root / "openai_usage.json").read_text().lower())

    def test_real_line_item_names_use_categories_without_token_total_ratio(self):
        rows = canonical_rows([page([cost("1", "test-model, input"), cost("2", "test-model, output"), cost("0.3", "test-model, cached input")])], "costs", SCOPE)
        result = distribute(rows, two_calls(), canonical_rows([page([usage()])], "usage", SCOPE))
        self.assertEqual(result["allocated"], {"usd": "3.3"})
        self.assertTrue(all(row["status"] == "Распределено" for row in result["rows"]))

    def test_conflicting_imports_are_not_used_for_allocation(self):
        folder = self.root / "conflict"
        folder.mkdir()
        record = {"response_id": "resp_same", "model": "test-model", "usage": {"input_tokens": 10, "output_tokens": 2}, "status": "completed"}
        (folder / "research_usage.json").write_text(json.dumps({"requests": [record]}))
        record["usage"]["input_tokens"] = 20
        (folder / "script_usage.json").write_text(json.dumps({"requests": [record]}))
        calls, warnings = import_calls(self.root, SCOPE)
        self.assertEqual(len(calls), 1)
        self.assertIsNone(calls[0]["units"]["input_tokens"])
        self.assertTrue(warnings)

    def test_billing_failure_does_not_break_primary_report(self):
        import billing_sync
        with patch.object(billing_sync, "load_billing_settings", return_value=SETTINGS), \
             patch.object(billing_sync, "sync_billing", side_effect=OSError("test")), redirect_stderr(io.StringIO()) as output:
            refresh_billing_under_test()
        self.assertIn("основные результаты сохранены", output.getvalue())

    def test_admin_secret_reflection_is_not_written_to_exports_or_archive(self):
        from billing_api import redact
        from build_review_archive import read_safe_content
        admin = "sk-admin-" + "x" * 60
        text = json.dumps(redact({"error": {"message": admin}, "Authorization": "Bearer " + admin}, (admin,)))
        self.assertNotIn(admin, text)
        path = self.root / "accidental.py"
        path.write_text(admin)
        with self.assertRaises(ValueError):
            read_safe_content(path)

    def test_quantity_mismatch_and_missing_usage_do_not_allocate(self):
        row = cost("1", "test-model, input")
        row.update(quantity="99", quantity_unit="tokens")
        rows = canonical_rows([page([row])], "costs", SCOPE)
        result = distribute(rows, two_calls(), canonical_rows([page([usage()])], "usage", SCOPE))
        self.assertEqual(result["unallocated"], {"usd": "1"})
        self.assertIn("единицы", result["rows"][0]["reason"].lower())

    def test_public_key_ids_are_not_mistaken_for_secrets(self):
        from billing_api import local_secret_values
        values = {"OPENAI_ADMIN_KEY": "private-admin-value", "OPENAI_API_KEY": "private-generation-value",
                  "OPENAI_BILLING_API_KEY_IDS": "key_public", "GEMINI_API_KEY": "private-google-value"}
        self.assertEqual(local_secret_values(values), ["private-admin-value", "private-generation-value", "private-google-value"])
        self.assertNotIn("private-admin-value", repr(BillingSettings("private-admin-value", "private-generation-value")))

    def test_mixed_payment_modes_cannot_assign_whole_line_to_default(self):
        rows = canonical_rows([page([cost("1", "test-model, input")])], "costs", SCOPE)
        modes = canonical_rows([page([usage(), {**usage(), "service_tier": "flex"}])], "usage", SCOPE)
        result = distribute(rows, two_calls(), modes)
        self.assertEqual(result["unallocated"], {"usd": "1"})
        self.assertIn("режим", result["rows"][0]["reason"])

    def test_archive_rejects_opaque_secret_from_env_before_copy(self):
        from build_review_archive import read_safe_content
        path = self.root / "accidental.py"
        path.write_text("opaque-unrecognized-value")
        with self.assertRaises(ValueError):
            read_safe_content(path, ["opaque-unrecognized-value"])

    def test_realtime_audio_usage_not_allocated_as_generic_text_tokens(self):
        rows=canonical_rows([page([cost('1','test-model, output')])],'costs',SCOPE)
        calls=two_calls();calls[0]['usage']['output_token_details']={'audio_tokens':20,'text_tokens':0}
        result=distribute(rows,calls,canonical_rows([page([usage()])],'usage',SCOPE))
        self.assertEqual(result['unallocated'],{'usd':'1'})
        self.assertIn('аудиотокены',result['rows'][0]['reason'])


class AnalyticsTransferTests(unittest.TestCase):
    def test_json_html_checksums_and_old_pointer_on_error(self):
        import analytics_sync
        from review_archive_sync import MARKER_NAME, DRIVE_FOLDER_ID
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            drive = root / "drive"
            drive.mkdir()
            (drive / MARKER_NAME).write_text(json.dumps({"drive_folder_id": DRIVE_FOLDER_ID}))
            json_path, html_path = root / "analytics.json", root / "report.html"
            json_path.write_text('{"schema_version":1,"source":"OpenAI Costs API"}')
            html_path.write_text('<html>Расходы</html>')
            with patch.object(analytics_sync, "configured_sync_directory", return_value=drive), \
                 patch.object(analytics_sync, "dotenv_values", return_value={}), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertTrue(analytics_sync.sync_analytics(json_path, html_path))
                pointer = drive / "latest_analytics.json"
                first = pointer.read_bytes()
                metadata = json.loads(first)
                self.assertEqual(hashlib.sha256((drive / metadata["analytics_name"]).read_bytes()).hexdigest(), metadata["sha256"])
                with patch.object(analytics_sync, "atomic_write_json", side_effect=OSError("test")):
                    self.assertFalse(analytics_sync.sync_analytics(json_path, html_path))
                self.assertEqual(pointer.read_bytes(), first)
                self.assertEqual(len(list(drive.glob("Smartgram_analytics_*.json"))), 2)


if __name__ == "__main__":
    unittest.main()
