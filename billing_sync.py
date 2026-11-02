"""Одна команда: чтение последних 30 дней Costs/Usage, сверка журналов, JSON и HTML."""

import argparse
import json
import hashlib
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
from pathlib import Path

from billing_api import BillingClient, BillingError, discover_scope, load_billing_settings, redact, usage_parameters
from billing_allocation import canonical_rows, classify, distribute, provider_quantity
from billing_history import import_calls, ready_videos, run_metadata
from billing_report import build_analytics, save_exports
from file_io import atomic_write, json_lock
from dotenv import dotenv_values
from billing_api import ENV_PATH, local_secret_values

OUTPUT_ROOT = Path(__file__).parent / "outputs"
BILLING_DIRECTORY = OUTPUT_ROOT / "billing"


def now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def write_private_json(path, value, secrets=()):
    # Decimal хранится строкой: исходная десятичная сумма сохраняется без float-округлений.
    content = json.dumps(redact(value, secrets), ensure_ascii=False, indent=2, default=str) + "\n"
    atomic_write(path, content.encode("utf-8"))


def read_optional(path):
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text())
        if not isinstance(value, dict):
            raise ValueError
        if value.get("schema_version") != 1 or any(not isinstance(value.get(name), list) for name in ("costs", "usage", "web_usage")):
            raise ValueError
        if not isinstance(value.get("scope"), dict) or not isinstance(value.get("period"), dict) or not isinstance(value.get("synced_at_utc"), str):
            raise ValueError
        return value
    except (OSError, ValueError):
        raise BillingError(f"Повреждён локальный {path.name}. Предыдущие данные не заменены.") from None


def fetch_snapshot(api, directory, now):
    scope = discover_scope(api)
    end = int(now.timestamp())
    start = int((now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=29)).timestamp())
    period = {"start_time": start, "end_time": end,
              "start_utc": datetime.fromtimestamp(start, timezone.utc).isoformat(), "end_utc": now.isoformat(), "days": 30}
    parameters = usage_parameters(start, end, scope, ["project_id", "api_key_id", "line_item"])
    costs = api.pages("/organization/costs", parameters)
    # Снимок сырых страниц хранится отдельно, не публикуется и не попадает в ZIP.
    raw_directory = directory / "raw" / now.strftime("%Y%m%dT%H%M%S%fZ")
    raw_directory.mkdir(parents=True)
    secrets = (api.settings.admin_key, api.settings.generation_key)
    write_private_json(raw_directory / "costs.json", {"period": period, "parameters": parameters, "pages": costs}, secrets)
    rows = canonical_rows(costs, "costs", scope)
    usage, web_usage, warnings = [], [], []
    for name, path, groups in (
        ("usage", "/organization/usage/completions", ["project_id", "api_key_id", "model", "service_tier", "batch"]),
        ("web_usage", "/organization/usage/web_search_calls", ["project_id", "api_key_id", "model", "context_level"])):
        try:
            params = usage_parameters(start, end, scope, groups)
            pages = api.pages(path, params)
            write_private_json(raw_directory / (name + ".json"), {"period": period, "parameters": params, "pages": pages}, secrets)
            values = canonical_rows(pages, name, scope)
            if name == "usage":
                usage = values
            else:
                web_usage = values
        except BillingError as error:
            warnings.append(str(error))
    return {"schema_version": 1, "synced_at_utc": now_iso(), "period": period, "scope": scope,
            "costs": rows, "usage": usage, "web_usage": web_usage, "warnings": warnings,
            "raw_directory": str(raw_directory), "admin_http_requests": api.request_count}


def pending_charges(snapshot):
    """Positive Usage with no matching Costs category is pending, never a zero bill."""
    pending=[]
    if not snapshot:return pending
    for kind in ['usage','web_usage']:
        for usage in snapshot.get(kind,[]):
            categories=['web_requests'] if kind=='web_usage' else ['uncached_tokens','output_tokens','cached_tokens','cache_write_tokens']
            for unit in categories:
                quantity=provider_quantity(usage,unit)
                if quantity is None or quantity<=0:continue
                matches=[]
                for row in snapshot['costs']:
                    category=classify(row.get('line_item')) or {}
                    if row['start_time']!=usage['start_time'] or row['end_time']!=usage['end_time'] or row['api_key_id']!=usage['api_key_id'] or row['project_id']!=usage['project_id']:continue
                    if category.get('category')==unit and (category.get('model')==usage.get('model') or category.get('generic_tool')):matches.append(row)
                if not matches:
                    pending.append({'start_time':usage['start_time'],'end_time':usage['end_time'],'project_id':usage['project_id'],
                        'api_key_id':usage['api_key_id'],'model':usage.get('model'),'category':unit,'provider_units':quantity,
                        'amount':None,'status':'pending_costs','reason':'Usage API уже содержит потребление, сопоставимая статья Costs API ещё не получена.'})
    return pending


def sync_billing(directory=None, output_root=None, client_factory=BillingClient, force=True, now=None, offline=False):
    """Сериализует снимок, распределение и пару JSON/HTML одного экспорта."""
    directory, root = Path(directory or BILLING_DIRECTORY), Path(output_root or OUTPUT_ROOT)
    directory.mkdir(parents=True, exist_ok=True)
    with json_lock(directory / "sync_state.json"):
        return _sync_billing(directory, root, client_factory, force, now, offline)


def _sync_billing(directory, root, client_factory, force, now, offline):
    snapshot = read_optional(directory / "snapshot.json")
    state = {"status": "Ожидаем данные", "error": None,
             "last_success_at_utc": snapshot.get("synced_at_utc") if snapshot else None, "last_attempt_at_utc": now_iso()}
    settings = None if offline else load_billing_settings()
    api = None
    try:
        current = now or datetime.now(timezone.utc)
        scope_matches = not offline and snapshot and (
            settings.project_id == snapshot["scope"]["project_id"] and set(settings.api_key_ids) == set(snapshot["scope"]["api_key_ids"])
            if settings.project_id and settings.api_key_ids else snapshot and
            hashlib.sha256(settings.generation_key.encode()).hexdigest() in snapshot["scope"].get("key_fingerprints", {}))
        age = (current - datetime.fromisoformat(snapshot["synced_at_utc"].replace("Z", "+00:00"))).total_seconds() if snapshot else None
        fresh = scope_matches and age is not None and 0 <= age < 300
        if not offline and (force or not fresh):
            api = client_factory(settings)
            updated = fetch_snapshot(api, directory, current)
            write_private_json(directory / "snapshot.json", updated, (settings.admin_key, settings.generation_key))
            snapshot = updated
            write_private_json(directory / "scope.json", snapshot["scope"])
        state.update(status="Сохранённый снимок" if offline else "Из биллинга", last_success_at_utc=snapshot["synced_at_utc"] if snapshot else None)
    except BillingError as error:
        state.update(status="Ожидаем данные", error=str(error))
    finally:
        if api:
            api.close()
    scope = snapshot["scope"] if snapshot else {}
    history_roots=[root]
    if root.resolve()==OUTPUT_ROOT.resolve():
        history_roots += sorted(p for p in (OUTPUT_ROOT.parent/'.isolated').glob('*/work/outputs') if p.is_dir())
    calls, warnings = import_calls(history_roots, scope)
    warnings += (snapshot or {}).get("warnings", [])
    pending=pending_charges(snapshot)
    for item in pending:
        warnings.append(f"Ожидаем данные: Usage API показывает {item['model']} / {item['category']} за интервал {item['start_time']}, но сопоставимая статья Costs API пока не найдена. Это не нулевые расходы.")
    allocation = distribute(snapshot["costs"], calls, snapshot["usage"], snapshot.get("web_usage", []), snapshot["period"]) if snapshot else None
    with localcontext() as context:
        context.prec = 80
        analytics = build_analytics(snapshot, calls, allocation, ready_videos(history_roots), state, warnings, runs=run_metadata(history_roots))
    analytics["pending_charges"]=pending
    public_secrets=local_secret_values(dotenv_values(ENV_PATH,interpolate=False),include_environment=True)+([settings.admin_key,settings.generation_key] if settings else [])
    paths = save_exports(directory, redact(analytics, public_secrets))
    write_private_json(directory / "sync_state.json", state)
    return analytics, paths


def refresh_after_report():
    """Ошибка аналитики не прерывает отчёт/производство; новая генерация не запускается."""
    try:
        if not load_billing_settings().admin_key:
            return
        analytics, paths = sync_billing(force=False)
        publish_analytics(*paths)
        if analytics["sync"]["error"]:
            print("Биллинг: " + analytics["sync"]["error"], file=sys.stderr)
    except Exception:
        print("Не удалось обновить аналитику OpenAI; основные результаты сохранены. Запустите billing_sync.py для диагностики.", file=sys.stderr)


def publish_analytics(json_path, html_path):
    from analytics_sync import sync_analytics
    return sync_analytics(json_path, html_path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-directory", type=Path)
    parser.add_argument("--no-sync", action="store_true", help="Сохранить отчёт локально, без Drive")
    parser.add_argument("--offline", action="store_true", help="Пересчитать факт/оценки из сохранённого снимка и журналов, без API")
    args = parser.parse_args()
    try:
        analytics, paths = sync_billing(directory=args.output_directory, offline=args.offline)
        print(f"JSON: {paths[0]}\nHTML: {paths[1]}")
        print("Синхронизация: " + json.dumps(analytics["sync"], ensure_ascii=False))
        print("Из биллинга: " + json.dumps(analytics["actual_charges"], ensure_ascii=False))
        print("Распределено: " + json.dumps(analytics["allocated_charges"], ensure_ascii=False))
        print("Не распределено: " + json.dumps(analytics["unallocated_charges"], ensure_ascii=False))
        print("Стоимость вызовов · " + analytics['effective_costs']['label'] + ': ' + json.dumps(analytics['effective_costs'], ensure_ascii=False))
        print("Область: " + json.dumps(analytics["scope"], ensure_ascii=False))
        copied = True if args.no_sync else publish_analytics(*paths)
        return 0 if not analytics["sync"]["error"] and copied else 1
    except (BillingError, OSError) as error:
        print(str(error) if isinstance(error, BillingError) else "Ошибка локального сохранения аналитики.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
