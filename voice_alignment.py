"""OpenAI подтверждает текст; GPT сравнивает смысл, Deepgram задаёт слова и время."""

import hashlib
import json
import math
import copy
import unicodedata
import re
from typing import Literal

from pydantic import Field, StrictInt, StrictStr, StrictBool
from model_api import StrictModel
from voice_errors import AudioContractError, AudioAlignmentError


class AudioIssue(StrictModel):
    fragment_index: StrictInt = Field(ge=1)
    expected: StrictStr = Field(min_length=1)
    observed_deepgram: StrictStr
    observed_openai: StrictStr | None
    reason: StrictStr = Field(min_length=1)


class WordRange(StrictModel):
    fragment_index: StrictInt = Field(ge=1)
    first_word_id: StrictInt | None
    last_word_id: StrictInt | None


class AudioCheck(StrictModel):
    decision: Literal["Можно использовать", "Есть расхождения", "Не уверен"]
    issues: list[AudioIssue]
    fragments: list[WordRange]


class FragmentEvidence(StrictModel):
    fragment_index: StrictInt = Field(ge=1)
    result: Literal[
        "match", "minor_asr_variation", "confirmed_material_error", "unresolved"
    ]
    deepgram_supports_script: StrictBool
    openai_supports_script: StrictBool
    same_material_error_in_both: StrictBool
    changes_meaning: StrictBool
    explanation: StrictStr = Field(min_length=1)


class VerifiedAudioCheck(AudioCheck):
    verification_version: Literal[2]
    assessments: list[FragmentEvidence]


TEXT_POLICY_VERSION = "openai-primary-v1"


def literal_evidence(text):
    """Case/punctuation/ё only. Keep words, numeric signs, decimals and math units."""
    text=unicodedata.normalize("NFC",text).casefold().replace("ё","е").replace("−","-")
    tokens=re.findall(r"[+-]?\d+(?:[.,]\d+)*|[^\W\d_]+|[%‰°×÷=/<>≤≥≠^√+]",text)
    return " ".join(tokens)


def text_authority(script, transcripts, value=None):
    if not transcripts or len(transcripts)!=2 or not isinstance(transcripts[1],str) or not transcripts[1].strip():
        return {"policy_version":TEXT_POLICY_VERSION,"approved":False,"basis":"missing_openai_transcript"}
    expected=" ".join(f["text"] for f in script["fragments"])
    exact=bool(literal_evidence(expected)) and literal_evidence(expected)==literal_evidence(transcripts[1])
    assessments=(value or {}).get("assessments",[])
    semantic=(len(assessments)==len(script["fragments"]) and
        [a.get("fragment_index") for a in assessments]==list(range(1,len(script["fragments"])+1)) and
        all(a.get("openai_supports_script") is True for a in assessments))
    return {"policy_version":TEXT_POLICY_VERSION,"approved":exact or semantic,
        "basis":"normalized_full_openai_match" if exact else "saved_gpt_openai_semantic_confirmation" if semantic else "openai_not_confirmed",
        "normalized_full_match":exact,"original_gpt_decision":(value or {}).get("decision"),
        "approval_input_sha256":hashlib.sha256(json.dumps({'script_version_id':script.get('script_version_id'),
            'fragments':script['fragments'],'openai_text':transcripts[1]},ensure_ascii=False,sort_keys=True).encode()).hexdigest(),
        "timing_source":"deepgram_words","deepgram_text_can_veto":False}


def primary_review(value, script, transcripts):
    authority=text_authority(script,transcripts,value)
    result=copy.deepcopy(value)
    if not authority["approved"]:return result,authority
    result.update(decision="Можно использовать",issues=[])
    if result.get("verification_version")==2:
        old={a.get("fragment_index"):a for a in result.get("assessments",[]) if isinstance(a,dict)}
        deepgram_exact=literal_evidence(transcripts[0])==literal_evidence(" ".join(f["text"] for f in script["fragments"]))
        result["assessments"]=[{
            "fragment_index":i,"result":"match" if deepgram_exact else "minor_asr_variation",
            "deepgram_supports_script":deepgram_exact or old.get(i,{}).get("deepgram_supports_script") is True,
            "openai_supports_script":True,"same_material_error_in_both":False,"changes_meaning":False,
            "explanation":("OpenAI подтверждает сценарий ("+authority["basis"]+"). "
                "Ошибки текста Deepgram не являются ошибками озвучки; Deepgram остаётся источником слов и времени. "
                "Исходное GPT-решение сохранено отдельно.")}
            for i in range(1,len(script["fragments"])+1)]
    return result,authority


def validate_issue_quotes(check, script, transcripts):
    for issue in check.issues:
        expected = literal_evidence(issue.expected)
        if not expected or expected not in literal_evidence(
            script["fragments"][issue.fragment_index - 1]["text"]
        ):
            raise AudioContractError(
                "Ошибка GPT-проверки: expected не является цитатой исходного фрагмента. Новый TTS не нужен."
            )
        observed = (
            literal_evidence(issue.observed_deepgram),
            literal_evidence(issue.observed_openai or ""),
        )
        if check.decision == "Есть расхождения" and expected in observed:
            raise AudioContractError(
                "Ошибка GPT-проверки: цитата ASR совпадает с expected (после е/ё и пунктуации), но вердикт объявляет подтверждённую ошибку. Это не основание нового TTS."
            )
        if transcripts:
            for text, source in zip(observed, transcripts):
                if text and text not in literal_evidence(source):
                    raise AudioContractError(
                        "Ошибка GPT-проверки: observed не является цитатой соответствующей сохранённой ASR. Новый TTS не нужен."
                    )


def validate_evidence(check, count):
    """Структурные основания и решение согласованы; смысл по-прежнему оценивает GPT."""
    if [item.fragment_index for item in check.assessments] != list(range(1, count + 1)):
        raise ValueError
    results = []
    for item in check.assessments:
        if item.result == "match" and (
            not item.deepgram_supports_script
            or not item.openai_supports_script
            or item.same_material_error_in_both
            or item.changes_meaning
        ):
            raise ValueError
        if item.result == "minor_asr_variation" and (
            not item.openai_supports_script
            or item.changes_meaning
            or item.same_material_error_in_both
        ):
            raise ValueError
        if item.result == "confirmed_material_error" and (
            item.deepgram_supports_script
            or item.openai_supports_script
            or not item.same_material_error_in_both
            or not item.changes_meaning
        ):
            raise ValueError
        if item.result == "unresolved" and item.same_material_error_in_both:
            raise ValueError
        results.append(item.result)
    expected = (
        "Не уверен"
        if "unresolved" in results
        else (
            "Есть расхождения"
            if "confirmed_material_error" in results
            else "Можно использовать"
        )
    )
    if check.decision != expected:
        raise ValueError
    if check.decision == "Есть расхождения":
        confirmed = {
            item.fragment_index
            for item in check.assessments
            if item.result == "confirmed_material_error"
        }
        if {issue.fragment_index for issue in check.issues} != confirmed:
            raise ValueError


def validate_words(raw, duration, tolerance):
    try:
        alternative = raw["results"]["channels"][0]["alternatives"][0]
        transcript, words = alternative["transcript"], alternative["words"]
        if (
            not isinstance(transcript, str)
            or not transcript.strip()
            or not isinstance(words, list)
            or not words
        ):
            raise ValueError
        previous = 0.0
        result = []
        for number, word in enumerate(words, 1):
            start, end, confidence = word["start"], word["end"], word["confidence"]
            if not isinstance(word["word"], str) or not word["word"].strip():
                raise ValueError
            if any(
                type(value) not in (int, float) or not math.isfinite(value)
                for value in (start, end, confidence)
            ):
                raise ValueError
            if end > duration:
                raise AudioContractError(
                    f"Непригодные таймкоды Deepgram: слово №{number} имеет end={end:.3f} с, "
                    f"а WAV длится {duration:.3f} с. Время не обрезалось."
                )
            if start < previous:
                raise AudioContractError(
                    f"Непригодные таймкоды Deepgram: слово №{number} начинается в {start:.3f} с, "
                    f"раньше конца предыдущего слова ({previous:.3f} с). Пересечение не исправлялось."
                )
            if not 0 <= confidence <= 1 or not 0 <= start < end or start < previous:
                raise ValueError
            previous = end
            result.append({**word, "word_id": number})
        return {
            "transcript": transcript,
            "words": result,
            "metadata": raw.get("metadata"),
            "audio_duration_seconds": duration,
        }
    except (KeyError, IndexError, TypeError, ValueError):
        raise AudioContractError(
            "Deepgram вернул пустую расшифровку или непригодные пословные таймкоды. Исходный ответ сохранён."
        ) from None


def validate_check(
    value, script, words, duration, tolerance=0.02, confirmed=False, transcripts=None
):
    authority=text_authority(script,transcripts,value)
    if transcripts:
        value,authority=primary_review(value,script,transcripts)
    try:
        check = (
            VerifiedAudioCheck.model_validate(value)
            if "verification_version" in value
            else AudioCheck.model_validate(value)
        )
        count = len(script["fragments"])
        if isinstance(check, VerifiedAudioCheck):
            validate_evidence(check, count)
        if [entry.fragment_index for entry in check.fragments] != list(
            range(1, count + 1)
        ):
            raise ValueError
        if any(
            issue.fragment_index > count
            or not issue.reason.strip()
            or not issue.expected.strip()
            for issue in check.issues
        ):
            raise ValueError
        if isinstance(check, VerifiedAudioCheck):
            validate_issue_quotes(check, script, transcripts)
        if (
            check.decision == "Можно использовать"
            and check.issues
            or check.decision == "Есть расхождения"
            and not check.issues
        ):
            raise ValueError
        if (
            confirmed
            and check.decision == "Есть расхождения"
            and any(not issue.observed_openai for issue in check.issues)
        ):
            raise ValueError
        previous_end=0.0
        for number,word in enumerate(words,1):
            start,end=word["start"],word["end"]
            if any(type(v) not in (int,float) or not math.isfinite(v) for v in (start,end)) or not previous_end<=start<end<=duration or word.get("word_id",number)!=number:
                raise ValueError
            previous_end=end
        previous = 0
        mapped = []
        for entry, fragment in zip(check.fragments, script["fragments"]):
            first, last = entry.first_word_id, entry.last_word_id
            if first is None or last is None:
                if (
                    first is not None
                    or last is not None
                    or check.decision != "Не уверен"
                ):
                    raise ValueError
                continue
            if not 1 <= first <= last <= len(words) or first <= previous:
                raise ValueError
            if check.decision == "Можно использовать" and first != previous + 1:
                raise ValueError
            previous = last
            start, end = words[first - 1]["start"], words[last - 1]["end"]
            if end > duration or start >= duration:
                raise ValueError
            mapped.append(
                {
                    "fragment_index": entry.fragment_index,
                    **fragment,
                    "first_word_id": first,
                    "last_word_id": last,
                    "speech_start": start,
                    "speech_end": end,
                    "rounding_adjusted": False,
                }
            )
        if check.decision == "Можно использовать" and (
            len(mapped) != count or previous != len(words)
        ):
            raise ValueError
        for index, fragment in enumerate(mapped):
            fragment["display_start"] = 0.0 if index == 0 else fragment["speech_start"]
            fragment["display_end"] = (
                mapped[index + 1]["speech_start"]
                if index + 1 < len(mapped)
                else duration
            )
            if (
                fragment["speech_end"] > fragment["display_end"]
                or fragment["display_start"] >= fragment["display_end"]
            ):
                raise ValueError
        return check.model_dump(), mapped
    except (ValueError, KeyError, IndexError, TypeError):
        if authority["approved"]:
            raise AudioAlignmentError("Текст подтверждён OpenAI, но привязка к словам/таймкодам Deepgram непригодна. Новый TTS не нужен.",text_verified=True) from None
        raise AudioContractError(
            "Некорректный контракт проверки речи: decision/issues, индексы фрагментов/слов или полное покрытие таймкодов. Это не подтверждённая ошибка диктора."
        ) from None
