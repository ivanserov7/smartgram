"""Точные текстовые привязки кадров; без предположений о секундах озвучки."""

from pydantic import Field, StrictInt
from model_api import StrictModel
from production_state import ProductionError, UpstreamBlocked, digest


class ShotDraft(StrictModel):
    text: str
    visual_task: str
    queries: list[str] = Field(min_length=1, max_length=4)
    must_show: list[str]
    must_not_show: list[str]
    preferences: list[str]


class PlannedFragment(StrictModel):
    fragment_index: StrictInt
    shots: list[ShotDraft] = Field(min_length=1, max_length=3)


class VisualPlan(StrictModel):
    fragments: list[PlannedFragment] = Field(min_length=1)


def short_query(value):
    query = " ".join(value.split())
    if not query or len(query.split()) > 8 or len(query) > 120:
        raise ProductionError(
            "Поисковый запрос должен быть коротким: обычно 2–4 слова, до 8 для содержательных названий."
        )
    return query


def bind_shots(fragment, index, drafts):
    """Границы вычисляет код по дословным отрезкам. Ни символ не теряется."""
    original = fragment["text"]
    cursor, result = 0, []
    for item in drafts:
        quoted = item["text"].strip()
        start = cursor
        while cursor < len(original) and original[cursor].isspace():
            cursor += 1
        if not quoted or not original.startswith(quoted, cursor):
            raise ProductionError(
                "Текст кадра не совпадает с последовательным участком исходного фрагмента."
            )
        end = cursor + len(quoted)
        if (
            end < len(original)
            and original[end - 1].isalnum()
            and original[end].isalnum()
        ):
            raise ProductionError("Кадр обрывает слово.")
        if len(drafts) > 1 and len(quoted.split()) < 3:
            raise ProductionError("Короткая фраза искусственно разделена на кадры.")
        shot = {
            **item,
            "fragment_index": index,
            "text_start": start,
            "text_end": end,
            "text": original[start:end],
            "queries": list(dict.fromkeys(short_query(q) for q in item["queries"])),
        }
        shot["shot_id"] = (
            f"f{index}_"
            + digest({"fragment": fragment, "start": start, "end": end})[:12]
        )
        result.append(shot)
        cursor = end
    if original[cursor:].strip():
        raise ProductionError("Кадры не покрывают весь текст фрагмента.")
    if result:
        result[-1]["text_end"] = len(original)
        result[-1]["text"] = original[result[-1]["text_start"] :]
    return result


def validate_shot_ranges(fragment, shots):
    cursor = 0
    if not 1 <= len(shots) <= 3:
        raise ProductionError("Требуется от одного до трёх кадров на фрагмент.")
    for shot in shots:
        start, end = shot["text_start"], shot["text_end"]
        if (
            type(start) is not int
            or type(end) is not int
            or start != cursor
            or not start < end <= len(fragment["text"])
        ):
            raise ProductionError(
                "Пропуск, пересечение или неверный порядок текстовых диапазонов кадров."
            )
        if shot["text"] != fragment["text"][start:end]:
            raise ProductionError("Текст кадра изменён.")
        cursor = end
    if cursor != len(fragment["text"]):
        raise ProductionError("Неполное покрытие фрагмента кадрами.")


def entry_shots(entry):
    """Старое одно изображение на фрагмент читается без миграции и нового API."""
    return entry["shots"] if "shots" in entry else [entry]


def shot_timeline(script, visuals, audio_fragments, words, aliases=None, *, binding_proof=None):
    from subtitles import canonical,script_tokens
    if binding_proof is not None:
        from subtitle_binding import verify_binding,bound_range
        verify_binding(binding_proof,script,words,audio_fragments,aliases)
    tokens=script_tokens(script)
    result = []
    for text, visual, parent in zip(
        script["fragments"], visuals["fragments"], audio_fragments
    ):
        shots = entry_shots(visual)
        if len(shots) == 1:
            result.append(dict(parent, shot_id=shots[0].get("shot_id")))
            continue
        validate_shot_ranges(text, shots)
        position = parent["first_word_id"] - 1
        mapped = []
        for shot in shots:
            if binding_proof is not None:
                local=[t for t in tokens if t['fragment_index']==parent['fragment_index']]
                offset=local[0]['start']
                covered=[t for t in local if shot['text_start']<=t['start']-offset and t['end']-offset<=shot['text_end']]
                if not covered or ''.join(shot['text'].split())!=''.join(t['text'] for t in covered):
                    raise UpstreamBlocked('Shot boundary splits script tokens; repair alignment only.')
                first,last=bound_range(binding_proof,covered[0]['token_id'],covered[-1]['token_id'])
                if first!=position+1:raise UpstreamBlocked('Shot/word coverage gap.')
                end=last-1
            else:
                end=None
            expected = canonical(shot["text"], aliases or {})
            end = position if end is None else end
            while (
                binding_proof is None and
                end < parent["last_word_id"]
                and canonical(
                    " ".join(w["word"] for w in words[position : end + 1]),
                    aliases or {},
                )
                != expected
            ):
                end += 1
            if end >= parent["last_word_id"]:
                raise UpstreamBlocked(
                    "awaiting_word_timings: кадр нельзя однозначно связать с проверенными словами аудио."
                )
            mapped.append(
                dict(
                    parent,
                    shot_id=shot["shot_id"],
                    text=shot["text"],
                    first_word_id=position + 1,
                    last_word_id=end + 1,
                    speech_start=words[position]["start"],
                    speech_end=words[end]["end"],
                )
            )
            position = end + 1
        if position != parent["last_word_id"]:
            raise UpstreamBlocked(
                "awaiting_word_timings: остались непривязанные слова."
            )
        for i, item in enumerate(mapped):
            item["display_start"] = (
                parent["display_start"] if i == 0 else item["speech_start"]
            )
            item["display_end"] = (
                mapped[i + 1]["speech_start"]
                if i + 1 < len(mapped)
                else parent["display_end"]
            )
        result.extend(mapped)
    return result


def asset_dependencies(asset):
    names = [asset["original_path"]] + [p["path"] for p in asset["motion"]["previews"]]
    if asset.get("source_original_path"):
        names.append(asset["source_original_path"])
    proof = asset["source"].get("rights_evidence")
    if proof:
        names.append(proof["snapshot_path"])
    for key in ('provenance_path','raw_path','svg_path'):
        if asset['source'].get(key):names.append(asset['source'][key])
    return list(dict.fromkeys(names))


def approved_assets(visuals):
    for entry in visuals["fragments"]:
        for shot in entry_shots(entry):
            if shot.get("status") == "approved":
                yield shot["asset"]
