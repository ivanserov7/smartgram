"""Передаёт только публичный JSON аналитики и HTML через прежнюю проверенную папку Drive."""

import json
import hashlib
from decimal import Decimal, localcontext, InvalidOperation
import os
import sys
import tempfile
from pathlib import Path

from dotenv import dotenv_values

from billing_api import ENV_PATH, local_secret_values
from build_review_archive import SECRET_VALUE
from file_io import atomic_write_json
from html_report_sync import fingerprint, sync_html_report, read_report
from review_archive_sync import ReviewSyncError, configured_sync_directory, rename_new_file, utc_now, validate_sync_directory


def sync_analytics(json_path, html_path):
    temporary = None
    try:
        directory = configured_sync_directory(ENV_PATH)
        if directory is None:
            raise ReviewSyncError("SMARTGRAM_REVIEW_SYNC_DIR пуст; аналитика осталась локально.")
        validate_sync_directory(directory)
        values = dotenv_values(ENV_PATH, interpolate=False)
        secrets = local_secret_values(values, include_environment=True)
        if json_path.is_symlink() or json_path.suffix != ".json":
            raise ReviewSyncError("Для аналитики нужен обычный JSON-файл.")
        content = json_path.read_bytes()
        text = content.decode("utf-8")
        data = json.loads(text)
        if not isinstance(data, dict) or data.get("schema_version") != 1 or data.get("source") != "OpenAI Costs API":
            raise ReviewSyncError("Неизвестная схема аналитики; JSON не передан.")
        if SECRET_VALUE.search(text) or any(secret in text for secret in secrets):
            raise ReviewSyncError("Обнаружен возможный секрет в аналитике; передача отменена.")
        if data.get('schema_revision', 1) >= 2:
            exported=dict(data);export_id=exported.pop('export_id',None)
            calculated=hashlib.sha256(json.dumps(exported,ensure_ascii=False,sort_keys=True,allow_nan=False).encode()).hexdigest()
            if not export_id or export_id != calculated:
                raise ReviewSyncError('Не совпадает контрольный идентификатор экспорта аналитики.')
            html_text=read_report(html_path,secrets).decode('utf-8')
            if f'name="smartgram-analytics-export" content="{export_id}"' not in html_text:
                raise ReviewSyncError('JSON и HTML относятся к разным версиям аналитики; указатель сохранён.')
            actual,allocated,residual=[data.get(n) for n in ['actual_charges','allocated_charges','unallocated_charges']]
            if actual is not None:
                with localcontext() as precision:
                    precision.prec=80
                    if not isinstance(allocated,dict) or not isinstance(residual,dict) or set(actual)!=set(allocated) or set(actual)!=set(residual) or any(Decimal(actual[c])!=Decimal(allocated[c])+Decimal(residual[c]) for c in actual):
                        raise ReviewSyncError('Нарушен баланс фактических начислений и распределения.')
        expected = fingerprint(content)
        with tempfile.NamedTemporaryFile(dir=directory, prefix=".Smartgram_analytics_", suffix=".tmp", delete=False) as file:
            temporary = Path(file.name)
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        if fingerprint(temporary.read_bytes()) != expected:
            raise ReviewSyncError("Не совпадает SHA-256 временной JSON-копии.")
        created = utc_now()
        for number in range(100):
            extra = f"_{number}" if number else ""
            destination = directory / f"Smartgram_analytics_{created:%Y%m%dT%H%M%SZ}{extra}.json"
            try:
                rename_new_file(temporary, destination)
                temporary = None
                break
            except FileExistsError:
                continue
        else:
            raise ReviewSyncError("Не удалось выбрать новое имя аналитики; прежние версии сохранены.")
        if fingerprint(destination.read_bytes()) != expected:
            raise ReviewSyncError("Не совпадают размер или SHA-256 опубликованного JSON.")
        html = sync_html_report(html_path, directory, secrets)
        validate_sync_directory(directory)
        atomic_write_json(directory / "latest_analytics.json", {"schema_version": 1,
            "analytics_name": destination.name, "created_at_utc": created.isoformat(), **expected,
            "html_report_name": html["report_name"], "html_size_bytes": html["size_bytes"], "html_sha256": html["sha256"]})
        print(f"Аналитика проверена и скопирована локально: {destination.name}; {html['report_name']}. latest_analytics.json обновлён.")
        print("Завершение облачной синхронизации не подтверждено.")
        return True
    except (OSError, ValueError, TypeError, InvalidOperation, ReviewSyncError) as error:
        message = str(error) if isinstance(error, ReviewSyncError) else "Ошибка чтения или сохранения файлов аналитики."
        print(f"Передача аналитики не завершена: {message} Локальные файлы и прежний latest_analytics.json сохранены.", file=sys.stderr)
        return False
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
