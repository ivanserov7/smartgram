"""Идентичность исходника и непрерывные визуальные участки без повторных появлений."""

import copy
import unicodedata
from urllib.parse import unquote, urlsplit

from production_state import ProductionError, digest

POLICY = "first_appearance_continuous_v1"


def source_identity(value):
    """ID и хеш относятся к исходнику ДО кропа; совпадения любого признака достаточно."""
    asset = value.get("asset", value)
    source = asset.get("source", asset)
    origin = source.get("origin") or "wikimedia"
    provider_id = None
    if origin == "pexels" and source.get("pexels_id"):
        provider_id = "pexels:" + str(source["pexels_id"])
    elif origin in {"generated", "deterministic"}:
        provider_id = (
            origin
            + ":"
            + str(
                source.get("candidate_id")
                or asset.get("candidate_id")
                or source.get("prompt_sha256")
                or ""
            )
        )
    else:
        title = source.get("file_title")
        if not title and "commons.wikimedia.org" in source.get("source_page", ""):
            title = unquote(urlsplit(source["source_page"]).path).removeprefix("/wiki/")
        if title:
            title = unicodedata.normalize(
                "NFC", unquote(title).replace("_", " ")
            ).strip()
            provider_id = "wikimedia:" + title
        elif source.get("page_id"):
            provider_id = "wikimedia_page:" + str(source["page_id"])
    hashes = []
    for name in (
        "source_sha1",
        "raw_sha256",
        "source_original_sha256",
        "file_sha256",
        "original_sha256",
    ):
        for item in (source, asset):
            value = item.get(name)
            if name == "original_sha256" and not value:
                value = item.get("motion", {}).get("original_sha256")
            if value:
                hashes.append(
                    ("sha1:" if name == "source_sha1" else "sha256:")
                    + str(value).lower()
                )
    return {"provider_id": provider_id, "hashes": sorted(set(hashes))}


def identity_keys(value):
    identity = source_identity(value)
    keys = set(identity["hashes"])
    if identity["provider_id"]:
        keys.add("provider:" + identity["provider_id"])
    return keys


def same_source(left, right):
    return bool(identity_keys(left) & identity_keys(right))


def assert_unique_sources(assets):
    seen = set()
    for asset in assets:
        keys = identity_keys(asset)
        if not keys:
            raise ProductionError(
                "Не подтверждена идентичность исходника визуального участка."
            )
        if seen & keys:
            raise ProductionError(
                "Один исходник появляется в нескольких визуальных участках."
            )
        seen.update(keys)
    return True


def first_appearance_sequence(assets, timeline):
    """A,A,B → A,B; A,B,A → A,B с продлением B. Аудио и сценарий не меняются."""
    if len(assets) != len(timeline) or not assets:
        raise ProductionError("Нет полного соответствия материалов и таймкодов.")
    # Объединяем алиасы заранее: разные ID одного файла и перекодировки одного ID.
    parents = list(range(len(assets)))
    aliases = {}

    def find(index):
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    for index, asset in enumerate(assets):
        keys = identity_keys(asset)
        if not keys:
            raise ProductionError("Неизвестный исходник; нельзя проверить повторы.")
        for key in keys:
            if key in aliases:
                parents[find(index)] = find(aliases[key])
            aliases[key] = index
    groups = [find(i) for i in range(len(assets))]
    selected = []
    sections = []
    seen = set()
    previous = 0
    for index, (asset, item, group) in enumerate(zip(assets, timeline, groups)):
        start, end = item["display_start"], item["display_end"]
        if abs(start - previous) > 1e-6 or end <= start:
            raise ProductionError("Пробел/пересечение в исходной визуальной привязке.")
        previous = end
        covered = {
            k: item[k]
            for k in (
                "fragment_index",
                "shot_id",
                "display_start",
                "display_end",
                "first_word_id",
                "last_word_id",
            )
            if k in item
        }
        covered["original_asset_identity"] = source_identity(asset)
        if group not in seen:
            seen.add(group)
            selected.append(copy.deepcopy(asset))
            section = dict(
                item,
                section_id="section_" + str(len(selected)),
                source_identity=source_identity(asset),
                covered_shots=[],
            )
            sections.append(section)
        else:
            sections[-1]["display_end"] = end
        covered["action"] = (
            "first_appearance"
            if group not in groups[:index]
            else (
                "adjacent_merge"
                if index and groups[index - 1] == group
                else "return_skipped_continue_current"
            )
        )
        sections[-1]["covered_shots"].append(covered)
    assert_unique_sources(selected)
    for section in sections:
        section["fragment_indices"] = list(
            dict.fromkeys(c["fragment_index"] for c in section["covered_shots"])
        )
        section["covered_shot_ids"] = [
            c.get("shot_id") for c in section["covered_shots"]
        ]
    if abs(sections[-1]["display_end"] - timeline[-1]["display_end"]) > 1e-6:
        raise ProductionError("Потеряна длительность аудио.")
    return selected, sections


def validate_sequence(assets, scenes):
    if len(assets) != len(scenes):
        raise ProductionError("Число исходников и визуальных участков различается.")
    assert_unique_sources(assets)
    for asset, scene in zip(assets, scenes):
        if scene.get("source_identity") and scene["source_identity"] != source_identity(
            asset
        ):
            raise ProductionError("Исходник участка изменился после восстановления.")
    return True


def journal_source_in_use(directory, state, candidate, owner):
    """Заняты только успешно назначенные материалы этого ролика, не отклонённые попытки."""
    from pathlib import Path
    from production_state import read

    claims = []
    script_path = Path(directory) / "script.json"
    script = read(script_path) if script_path.exists() else None
    for job in state.get("jobs", {}).values():
        index = job.get("fragment_index")
        if (
            script
            and index
            and (
                index > len(script["fragments"])
                or job.get("fragment") != script["fragments"][index - 1]
            )
        ):
            continue
        if job.get("superseded"):
            continue
        entries = list(job.get("approved", {}).values())
        if job.get("status") == "approved" and job.get("result"):
            entries.append(job["result"])
        for entry in entries:
            if entry.get("status") == "approved" and entry.get("asset"):
                claims.append(
                    (
                        entry.get("shot_id")
                        or "fragment:" + str(entry.get("fragment_index", index)),
                        entry["asset"],
                    )
                )
    path = Path(directory) / "visual_materials.json"
    if path.exists():
        manifest = read(path)
        if not script or manifest.get("script_version_id") == script.get(
            "script_version_id"
        ):
            for fragment in manifest.get("fragments", []):
                if fragment.get("status") != "approved":
                    continue
                for entry in fragment.get("shots", [fragment]):
                    if entry.get("status") == "approved" and entry.get("asset"):
                        claims.append(
                            (
                                entry.get("shot_id")
                                or "fragment:" + str(fragment["fragment_index"]),
                                entry["asset"],
                            )
                        )
    owners = {
        saved_owner for saved_owner, asset in claims if same_source(candidate, asset)
    }
    return bool(owners - {owner})


def legacy_source_in_use(directory, candidate, owner):
    from pathlib import Path
    from production_state import read

    # Проверенное решение заменяет предварительный подбор той же сцены.
    # После отказа Vision источник освобождается, даже если metadata-манифест старый.
    entries = {}
    for filename in ("visual_manifest.json", "visual_review.json"):
        path = Path(directory) / filename
        if path.exists():
            entries.update({e["scene_id"]: e for e in read(path).get("scenes", [])})
    owners = {
        e["scene_id"]
        for e in entries.values()
        if e.get("status") == "selected"
        and e.get("selected")
        and same_source(candidate, e["selected"])
    }
    return bool(owners - {owner})
