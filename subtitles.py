"""Exact script token coverage, verified audio IDs, font metrics and libass raster bounds."""

import math
import re
import shutil
from pathlib import Path
from typing import Literal
from PIL import Image, ImageChops, ImageFont
from pydantic import Field, StrictInt
from model_api import StrictModel
from media_tools import run_command
from file_io import atomic_write_json
from production_state import ProductionError, sha


class Line(StrictModel):
    first_token_id: StrictInt
    last_token_id: StrictInt


class Cue(StrictModel):
    cue_id: StrictInt
    first_token_id: StrictInt
    last_token_id: StrictInt
    first_audio_word_id: StrictInt
    last_audio_word_id: StrictInt
    lines: list[Line] = Field(min_length=1)


class Segmentation(StrictModel):
    status: Literal["ok", "uncertain", "blocked"]
    reason: str | None
    cues: list[Cue]


_NUMBERS = [
    "ноль",
    "один",
    "два",
    "три",
    "четыре",
    "пять",
    "шесть",
    "семь",
    "восемь",
    "девять",
    "десять",
    "одиннадцать",
    "двенадцать",
    "тринадцать",
    "четырнадцать",
    "пятнадцать",
    "шестнадцать",
    "семнадцать",
    "восемнадцать",
    "девятнадцать",
]
_TENS = [
    "",
    "",
    "двадцать",
    "тридцать",
    "сорок",
    "пятьдесят",
    "шестьдесят",
    "семьдесят",
    "восемьдесят",
    "девяносто",
]
_HUNDREDS = [
    "",
    "сто",
    "двести",
    "триста",
    "четыреста",
    "пятьсот",
    "шестьсот",
    "семьсот",
    "восемьсот",
    "девятьсот",
]


def number_words(value):
    n = int(value)
    if n < 20:
        return _NUMBERS[n]
    if n < 100:
        return _TENS[n // 10] + (" " + _NUMBERS[n % 10] if n % 10 else "")
    if n < 1000:
        return _HUNDREDS[n // 100] + (" " + number_words(n % 100) if n % 100 else "")
    # Larger/ordinal/decimal ambiguities are returned to the model as blocked, never guessed.
    return value


def canonical(text, aliases=None):
    text = text.casefold().replace("ё", "е")
    for short, long in [
        ("т.е.", "то есть"),
        ("т. е.", "то есть"),
        ("т.д.", "так далее"),
        ("т. д.", "так далее"),
    ]:
        text = text.replace(short, long)
    for identifier, forms in (aliases or {}).items():
        for form in sorted(forms, key=len, reverse=True):
            pattern = (
                r"(?<!\w)" + re.escape(form.casefold().replace("ё", "е")) + r"(?!\w)"
            )
            text = re.sub(pattern, " alias" + identifier + " ", text)
    text = re.sub(r"\b\d{1,3}\b", lambda m: number_words(m[0]), text)
    return "".join(re.findall(r"[\w]+", text))


def script_tokens(script):
    result = []
    offset = 0
    for index, fragment in enumerate(script["fragments"], 1):
        for match in re.finditer(r"\S+", fragment["text"]):
            result.append(
                {
                    "token_id": len(result) + 1,
                    "text": match[0],
                    "fragment_index": index,
                    "start": offset + match.start(),
                    "end": offset + match.end(),
                }
            )
        offset += len(fragment["text"]) + 1
    return result


def materialize(
    segmentation, script, words, fragments, aliases=None, *, binding_proof=None
):
    if segmentation["status"] != "ok":
        if segmentation["cues"]:
            raise ProductionError(
                "Uncertain/blocked segmentation must have no fabricated cues."
            )
        raise ProductionError(
            "subtitle_segment "
            + segmentation["status"]
            + ": "
            + str(segmentation.get("reason"))
        )
    if binding_proof is not None:
        from subtitle_binding import verify_binding, bound_range

        verify_binding(binding_proof, script, words, fragments, aliases)
    tokens = script_tokens(script)
    text = " ".join(f["text"] for f in script["fragments"])
    by_id = {w["word_id"]: w for w in words}
    ranges = {f["fragment_index"]: f for f in fragments}
    if len(by_id) != len(words) or sorted(by_id) != list(range(1, len(words) + 1)):
        raise ProductionError("Invalid audio word IDs.")
    previous_time = 0
    for word in words:
        start, end = word["start"], word["end"]
        if (
            any(
                type(v) not in (int, float) or not math.isfinite(v)
                for v in (start, end)
            )
            or not previous_time <= start < end
        ):
            raise ProductionError("Invalid interior word timing/order.")
        previous_time = end
    previous_token = previous_word = 0
    previous_end = 0
    result = []
    ids = set()

    def recover(first, last):
        if (
            type(first) is not int
            or type(last) is not int
            or not 1 <= first <= last <= len(tokens)
        ):
            raise ProductionError("Unknown/out-of-order script token IDs.")
        return text[tokens[first - 1]["start"] : tokens[last - 1]["end"]]

    for cue in segmentation["cues"]:
        cid = cue["cue_id"]
        first, last = cue["first_token_id"], cue["last_token_id"]
        af, al = cue["first_audio_word_id"], cue["last_audio_word_id"]
        if (
            cid in ids
            or first != previous_token + 1
            or af != previous_word + 1
            or af not in by_id
            or al not in by_id
            or al < af
        ):
            raise ProductionError(
                "Cue coverage has a gap, duplicate, unknown ID or reordering."
            )
        ids.add(cid)
        exact = recover(first, last)
        index = tokens[first - 1]["fragment_index"]
        fr = ranges[index]
        if (
            tokens[last - 1]["fragment_index"] != index
            or not fr["first_word_id"] <= af <= al <= fr["last_word_id"]
        ):
            raise ProductionError("Cue escapes verified fragment word binding.")
        spoken = " ".join(
            by_id[i].get("punctuated_word", by_id[i]["word"]) for i in range(af, al + 1)
        )
        if binding_proof is not None:
            if (af, al) != bound_range(binding_proof, first, last):
                raise ProductionError(
                    f"Cue {cid} does not follow verified script/Deepgram boundaries."
                )
        elif canonical(exact, aliases) != canonical(spoken, aliases):
            raise ProductionError(
                f"Ambiguous token/audio mapping in cue {cid}: return uncertain or regroup within verified fragment."
            )
        line_text = []
        cursor = first
        for line in cue["lines"]:
            if line["first_token_id"] != cursor or line["last_token_id"] > last:
                raise ProductionError("Line coverage gap or overlap.")
            line_text.append(recover(cursor, line["last_token_id"]))
            cursor = line["last_token_id"] + 1
        if cursor != last + 1:
            raise ProductionError("Lines do not cover cue exactly.")
        start, end = by_id[af]["start"], by_id[al]["end"]
        if (
            any(
                type(v) not in (int, float) or not math.isfinite(v)
                for v in (start, end)
            )
            or start < previous_end
            or end <= start
        ):
            raise ProductionError("Non-monotonic subtitle times.")
        result.append(
            {
                **cue,
                "text": exact,
                "line_text": line_text,
                "start": start,
                "end": end,
                "fragment_index": index,
            }
        )
        previous_token, previous_word, previous_end = last, al, end
    if previous_token != len(tokens) or previous_word != len(words):
        raise ProductionError("Incomplete script/audio coverage.")
    return result


def ass_time(seconds):
    # ASS centiseconds; absolute endpoints are quantized independently.
    n = round(seconds * 100)
    h, n = divmod(n, 360000)
    m, n = divmod(n, 6000)
    s, cs = divmod(n, 100)
    return f"{h}:{m:02}:{s:02}.{cs:02}"


def srt_time(seconds):
    n = round(seconds * 1000)
    h, n = divmod(n, 3600000)
    m, n = divmod(n, 60000)
    s, ms = divmod(n, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def ass_escape(text):
    # ASS has no portable escape for literal braces/backslash: use vector-safe Unicode glyphs.
    # Reject instead of changing the exact user text.
    if any(c in text for c in "{}\\"):
        raise ProductionError(
            "ASS control characters in source text are unsupported; exact text retained in diagnostics."
        )
    return text


def write_subtitles(directory, cues, settings, family, size, appearance=None):
    directory = Path(directory)
    width, height = size
    font_size = settings["font_pixels_at_1080_width"] * width / 1080
    margin = width * (100 - settings["width_percent"]) / 200
    bottom = height * settings["bottom_margin_percent"] / 100
    appearance = appearance or {}
    shift = appearance.get("vertical_shift_pixels", 0)
    if not -height * 0.1 <= shift <= height * 0.1:
        raise ProductionError(
            "Subtitle position correction exceeds configured safe range."
        )
    bottom += shift
    opacity = appearance.get("background_opacity", 0.5)
    outline = appearance.get("outline_pixels", 2)
    if not 0.5 <= opacity <= 1 or not 2 <= outline <= 6:
        raise ProductionError("Subtitle contrast correction outside safe limits.")
    backdrop = f"&H{round(255*(1-opacity)):02X}000000"
    # Two coincident text layers: black outline and a translucent box. Same font/geometry.
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
ScaledBorderAndShadow: yes
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Back,{family},{font_size},&H00FFFFFF,&H00FFFFFF,{backdrop},{backdrop},0,0,0,0,100,100,0,0,3,5,0,2,{round(margin)},{round(margin)},{round(bottom)},1
Style: Text,{family},{font_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,{outline},0,2,{round(margin)},{round(margin)},{round(bottom)},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []
    srt = []
    for i, cue in enumerate(cues, 1):
        lines = "\\N".join(ass_escape(line) for line in cue["line_text"])
        for layer, style in [(0, "Back"), (1, "Text")]:
            events.append(
                f'Dialogue: {layer},{ass_time(cue["start"])},{ass_time(cue["end"])},{style},,0,0,0,,{lines}'
            )
        srt.append(
            f'{i}\n{srt_time(cue["start"])} --> {srt_time(cue["end"])}\n'
            + "\n".join(cue["line_text"])
            + "\n"
        )
    (directory / "subtitles.ass").write_text(
        header + "\n".join(events) + "\n", encoding="utf-8"
    )
    (directory / "subtitles.srt").write_text("\n".join(srt), encoding="utf-8")
    return {
        "font_family": family,
        "font_pixels": font_size,
        "margin_pixels": margin,
        "bottom_pixels": bottom,
        "font_path": None,
    }


def font_filter(ass_path, font_directory):
    def quote(path):
        text = str(Path(path).resolve())
        if any(c in text for c in "':,;[]\\"):
            raise ProductionError(
                "Unsupported special character in FFmpeg filter path."
            )
        return text

    return f"ass=filename={quote(ass_path)}:fontsdir={quote(font_directory)}"


def fit_existing_lines(cues, font, allowed, max_lines):
    """Меняем только переносы внутри уже проверенного блока; слова/таймкоды неизменны."""
    changes = []
    if max_lines < 2:
        return changes
    for cue in cues:
        if all(font.getlength(line) + 14 <= allowed for line in cue["line_text"]):
            continue
        matches = list(re.finditer(r"\S+", cue["text"]))
        options = []
        for cut in range(1, len(matches)):
            left = cue["text"][: matches[cut - 1].end()]
            right = cue["text"][matches[cut].start() :]
            widths = [font.getlength(left) + 14, font.getlength(right) + 14]
            if max(widths) > allowed:
                continue
            dangling = matches[cut - 1].group().casefold().strip(".,:;!?") in {
                "в",
                "к",
                "с",
                "у",
                "и",
                "а",
                "о",
                "по",
                "на",
                "не",
                "из",
                "за",
                "до",
                "от",
                "для",
                "при",
                "без",
                "под",
                "над",
            }
            options.append(
                (
                    (dangling, max(widths), abs(widths[0] - widths[1])),
                    cut,
                    [left, right],
                )
            )
        if not options:
            continue  # Невместимый блок возвращается прежней проверке, лимиты не ослабляются.
        _, cut, new_lines = min(options)
        old = cue["line_text"]
        first = cue["first_token_id"]
        last = cue["last_token_id"]
        if len(matches) != last - first + 1:
            raise ProductionError("Нельзя восстановить токены блока для переноса.")
        cue["line_text"] = new_lines
        cue["lines"] = [
            {"first_token_id": first, "last_token_id": first + cut - 1},
            {"first_token_id": first + cut, "last_token_id": last},
        ]
        changes.append(
            {
                "cue_id": cue["cue_id"],
                "previous_lines": old,
                "lines": new_lines,
                "method": "exact_font_metrics_only_line_breaks",
            }
        )
    return changes


def split_overwide_verified_cues(cues, words, proof, font, allowed, settings):
    """Редкий невместимый блок делим только по доказанной парной границе слов, без новых секунд."""
    result = []
    changes = []
    for cue in cues:
        if all(font.getlength(line) + 14 <= allowed for line in cue["line_text"]):
            result.append(cue)
            continue
        matches = list(re.finditer(r"\S+", cue["text"]))
        options = []
        for block in proof["blocks"]:
            cut = block["last_token_id"]
            if not cue["first_token_id"] <= cut < cue["last_token_id"]:
                continue
            count = cut - cue["first_token_id"] + 1
            last_word = block["last_audio_word_id"]
            left = dict(
                cue,
                last_token_id=cut,
                last_audio_word_id=last_word,
                text=cue["text"][: matches[count - 1].end()],
                end=words[last_word - 1]["end"],
            )
            right = dict(
                cue,
                first_token_id=cut + 1,
                first_audio_word_id=last_word + 1,
                text=cue["text"][matches[count].start() :],
                start=words[last_word]["start"],
            )
            valid = True
            for part in (left, right):
                part["line_text"] = [part["text"]]
                part["lines"] = [
                    {
                        "first_token_id": part["first_token_id"],
                        "last_token_id": part["last_token_id"],
                    }
                ]
                fit_existing_lines([part], font, allowed, settings["max_lines"])
                duration = part["end"] - part["start"]
                if (
                    not settings["min_duration_ms"]
                    <= duration * 1000
                    <= settings["max_duration_ms"]
                    or len(part["text"]) / duration
                    > settings["max_characters_per_second"]
                    or any(
                        font.getlength(line) + 14 > allowed
                        for line in part["line_text"]
                    )
                ):
                    valid = False
            if valid:
                options.append(
                    (
                        (
                            max(len(left["text"]), len(right["text"])),
                            abs(
                                (left["end"] - left["start"])
                                - (right["end"] - right["start"])
                            ),
                        ),
                        left,
                        right,
                    )
                )
        if not options:
            result.append(cue)
            continue
        _, left, right = min(options, key=lambda item: item[0])
        result.extend([left, right])
        changes.append(
            {
                "original_cue_id": cue["cue_id"],
                "first_token_id": cue["first_token_id"],
                "last_token_id": cue["last_token_id"],
                "cut_after_token_id": left["last_token_id"],
                "cut_after_audio_word_id": left["last_audio_word_id"],
                "method": "existing_verified_word_boundary_no_timing_changes",
            }
        )
    for number, cue in enumerate(result, 1):
        cue["cue_id"] = number
    cues[:] = result
    return changes


def layout(
    directory,
    cues,
    settings,
    media,
    family,
    size,
    appearance=None,
    journal=None,
    *,
    verified_words=None,
    binding_proof=None,
):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    fonts = directory / "font"
    fonts.mkdir(exist_ok=True)
    exact_font = fonts / media.font.name
    if not exact_font.exists():
        shutil.copyfile(media.font, exact_font)
    if sha(exact_font) != sha(media.font):
        raise ProductionError("Render font copy/hash changed.")
    actual_font = ImageFont.truetype(
        str(media.font), round(settings["font_pixels_at_1080_width"] * size[0] / 1080)
    )
    line_changes = fit_existing_lines(
        cues,
        actual_font,
        size[0] * settings["width_percent"] / 100,
        settings["max_lines"],
    )
    splits = []
    if verified_words is not None and binding_proof is not None:
        splits = split_overwide_verified_cues(
            cues,
            verified_words,
            binding_proof,
            actual_font,
            size[0] * settings["width_percent"] / 100,
            settings,
        )
    details = write_subtitles(directory, cues, settings, family, size, appearance)
    details.update(font_path=str(media.font), font_sha256=sha(media.font))
    font = ImageFont.truetype(str(media.font), round(details["font_pixels"]))
    issues = []
    measurements = []
    for cue in cues:
        duration = cue["end"] - cue["start"]
        cps = len(cue["text"]) / duration
        reasons = []
        if len(cue["line_text"]) > settings["max_lines"]:
            reasons.append("max_lines")
        if (
            duration * 1000 < settings["min_duration_ms"] - 1e-6
            or duration * 1000 > settings["max_duration_ms"] + 1e-6
        ):
            reasons.append("duration_ms")
        if cps > settings["max_characters_per_second"]:
            reasons.append("characters_per_second")
        if round(cue["start"] * 100) >= round(cue["end"] * 100):
            reasons.append("ASS_quantized_duration")
        widths = [font.getlength(line) + 14 for line in cue["line_text"]]
        allowed = size[0] * settings["width_percent"] / 100
        if any(w > allowed for w in widths):
            reasons.append("font_metric_width")
        # Measure actual libass output, including box and outline. Bright magenta is absent
        # from configured black/white styles, so the union is observable at any alpha.
        probe_dir = directory / "raster" / str(cue["cue_id"])
        probe_dir.mkdir(parents=True, exist_ok=True)
        probe_cue = {**cue, "start": 0, "end": 1}
        write_subtitles(probe_dir, [probe_cue], settings, family, size, appearance)
        target = probe_dir / "raster.png"
        w, h = size
        _, stderr = run_command(
            [
                media.ffmpeg,
                "-hide_banner",
                "-loglevel",
                "verbose",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"color=c=0xFF00FF:s={w}x{h}:r=1:d=1",
                "-vf",
                font_filter(probe_dir / "subtitles.ass", fonts),
                "-frames:v",
                "1",
                "-threads",
                "1",
                target,
            ],
            timeout=60,
            journal=journal,
        )
        # Explicit fontsdir and observed font selection are both required; never assume fallback.
        selections = [line for line in stderr.splitlines() if "fontselect:" in line]
        postscript, _ = run_command(
            [media.font_query, "--format=%{postscriptname}", media.font],
            timeout=30,
            journal=journal,
        )
        expected_names = {
            name.strip() for name in postscript.split(",") if name.strip()
        }
        loaded = "Loading font file " + "'" + str(exact_font.resolve()) + "'" in stderr
        selected_names = [
            line.split(" -> ", 1)[1].split(",")[0].strip()
            for line in selections
            if " -> " in line
        ]
        if (
            not loaded
            or not selected_names
            or any(name not in expected_names for name in selected_names)
            or "failed to find any fallback" in stderr
        ):
            raise ProductionError(
                "libass did not load/select the measured TTF (including PostScript identity): "
                + str(selections)
            )
        with Image.open(target) as image:
            image = image.convert("RGB")
            reference = Image.new("RGB", image.size, image.getpixel((0, 0)))
            difference = ImageChops.difference(image, reference)
            # Ignore tiny RGB conversion noise; retain actual alpha-blended box edges.
            mask = difference.convert("L").point(lambda v: 255 if v > 8 else 0)
            bounds = mask.getbbox()
        if not bounds:
            reasons.append("empty_libass_raster")
        elif (
            bounds[0] < details["margin_pixels"] - 2
            or bounds[2] > w - details["margin_pixels"] + 2
            or bounds[1] <= 0
            or bounds[3] >= h
        ):
            reasons.append("libass_raster_outside_safe_area")
        measurement = {
            "cue_id": cue["cue_id"],
            "duration_ms": duration * 1000,
            "cps": cps,
            "line_widths": widths,
            "allowed_width": allowed,
            "libass_bounds": bounds,
            "font_selection": selections,
            "problems": reasons,
        }
        measurements.append(measurement)
        if reasons:
            issues.append(measurement)
    report = {
        "passed": not issues,
        "issues": issues,
        "measurements": measurements,
        "font": details,
        "line_break_adjustments": line_changes,
        "verified_block_splits": splits,
    }
    atomic_write_json(directory / "layout_report.json", report)
    return report
