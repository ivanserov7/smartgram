"""Реальный локальный тест FFmpeg: синтетический MP4, не ролик конвейера и не речь."""

import argparse
import base64
import hashlib
import json
import math
import platform
import shutil
import struct
import tempfile
import wave
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path

from PIL import Image, ImageDraw

from file_io import atomic_write, atomic_write_json
from media_tools import MediaError, SUBTITLE_TEXT, inspect_tools, load_media_settings, run_command
from research_batch_report import details
from review_gallery import escape

DURATION = 5
FPS = 30


def create_background(path):
    """Крупная сетка и круги делают плавное приближение заметным на тесте."""
    image = Image.new("RGB", (2160, 3840), "#11213b")
    draw = ImageDraw.Draw(image)
    for x in range(0, 2160, 160):
        draw.line((x, 0, x, 3840), fill="#28415f", width=5)
    for y in range(0, 3840, 160):
        draw.line((0, y, 2160, y), fill="#28415f", width=5)
    draw.ellipse((280, 900, 1880, 2500), fill="#287cad", outline="#97e8ed", width=30)
    draw.ellipse((650, 1320, 1100, 1770), fill="#54b68a")
    draw.ellipse((1120, 1830, 1630, 2180), fill="#54b68a")
    image.save(path)


def create_sound(path):
    """Тихий синус 440 Гц с плавным входом/выходом, без TTS или сетевых вызовов."""
    rate = 48000
    samples = bytearray()
    for index in range(DURATION * rate):
        time = index / rate
        envelope = min(1, time / .1, (DURATION - time) / .1)
        value = int(32767 * .08 * envelope * math.sin(2 * math.pi * 440 * time))
        samples.extend(struct.pack("<h", value))
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(samples)


def create_subtitles(directory, family):
    content = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{family},58,&H00FFFFFF,&H00FFFFFF,&H00101010,&H90000000,0,0,0,0,100,100,0,0,1,3,1,2,60,60,300,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,0:00:05.00,Default,,0,0,0,,Проверка субтитров:\\Nёж, спутник, Земля
"""
    atomic_write(directory / "subtitles.ass", content.encode("utf-8"))
    atomic_write(directory / "subtitles.srt", ("1\n00:00:00,000 --> 00:00:05,000\n" + SUBTITLE_TEXT + "\n").encode("utf-8"))


def validate_probe(probe):
    """Оценивает настоящий ffprobe JSON, а не ожидаемые параметры команды."""
    try:
        video = [stream for stream in probe["streams"] if stream["codec_type"] == "video"]
        audio = [stream for stream in probe["streams"] if stream["codec_type"] == "audio"]
        if len(video) != 1 or len(audio) != 1:
            raise ValueError
        video, audio = video[0], audio[0]
        if (video["codec_name"] != "h264" or (video["width"], video["height"]) != (1080, 1920)
                or video["pix_fmt"] != "yuv420p" or Fraction(video["avg_frame_rate"]) != FPS
                or int(video["nb_read_frames"]) != DURATION * FPS or audio["codec_name"] != "aac"
                or int(audio["sample_rate"]) != 48000 or abs(float(probe["format"]["duration"]) - DURATION) > .1):
            raise ValueError
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        raise MediaError("Параметры созданного MP4 не соответствуют тесту: 1080×1920, 30 fps, 150 кадров, H.264/yuv420p и AAC/48 кГц, 5 с.") from None


def render(settings, directory, family, journal):
    filters = ("[0:v]zoompan=z='1+0.12*on/149':x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':"
               "d=150:s=1080x1920:fps=30,ass=filename=subtitles.ass:fontsdir=fonts,format=yuv420p[v];"
               "[1:a]loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000[a]")
    run_command([settings.ffmpeg, "-hide_banner", "-nostdin", "-y", "-i", "background.png", "-i", "test_sound.wav",
                 "-filter_complex", filters, "-map", "[v]", "-map", "[a]", "-frames:v", "150", "-t", "5",
                 "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-c:a", "aac", "-b:a", "128k",
                 "-ar", "48000", "-movflags", "+faststart", "test_vertical.mp4"], cwd=directory, journal=journal)
    # ASS используется в MP4, а SRT проверяется отдельным реальным запуском subtitles.
    run_command([settings.ffmpeg, "-hide_banner", "-nostdin", "-y", "-i", "background.png", "-vf",
                 f"scale=1080:1920,subtitles=filename=subtitles.srt:fontsdir=fonts:force_style='Fontname={family}'",
                 "-frames:v", "1", "-update", "1", "srt_frame.png"], cwd=directory, journal=journal)


def write_report(directory, result):
    frame = directory / "frame.png"
    media = ''
    if (directory / "test_vertical.mp4").exists():
        encoded = base64.b64encode((directory / "test_vertical.mp4").read_bytes()).decode()
        media += f'<video controls width="270" src="data:video/mp4;base64,{encoded}"></video>'
    if frame.exists():
        encoded = base64.b64encode(frame.read_bytes()).decode()
        media += f'<img width="270" alt="Кадр для проверки кириллицы" src="data:image/png;base64,{encoded}">'
    page = '<!doctype html><html lang="ru"><meta charset="utf-8"><title>Smartgram: проверка FFmpeg</title>'
    page += '<style>body{max-width:1100px;margin:auto;padding:24px;font:16px/1.6 system-ui}pre{white-space:pre-wrap;overflow-wrap:anywhere}video,img{vertical-align:top;margin:10px}</style>'
    page += '<h1>Проверка локальной среды монтажа</h1><p>Синтетический тест инструментов: это не готовый ролик Smartgram. Звук — локальный синус, не речь. API генерации не используются.</p>'
    page += '<p>Итог: ' + escape(result["status"]) + '. ' + escape(result.get("reason") or "") + '</p>' + media
    page += details("Проверки и реальные параметры", result) + '</html>'
    atomic_write(directory / "report.html", page.encode("utf-8"))


def check_media(directory):
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    journal = []
    result = {"status": "not_completed", "reason": None, "architecture": platform.machine(),
              "macos": platform.mac_ver()[0], "subtitle_text": SUBTITLE_TEXT, "api_requests": 0,
              "frame_review": "Не выполнялась; требуется осмотр извлечённого кадра.", "commands": journal}
    try:
        settings = load_media_settings()
        result["tools"] = inspect_tools(settings, journal)
        result["paths"] = {"ffmpeg": str(settings.ffmpeg), "ffprobe": str(settings.ffprobe), "font": str(settings.font)}
        create_background(directory / "background.png")
        create_sound(directory / "test_sound.wav")
        create_subtitles(directory, result["tools"]["font_family"])
        (directory / "fonts").mkdir(exist_ok=True)
        # У системного шрифта macOS есть защищённые атрибуты. Нужны только байты,
        # переносить системные флаги/расширенные атрибуты в outputs нельзя.
        shutil.copyfile(settings.font, directory / "fonts" / settings.font.name)
        render(settings, directory, result["tools"]["font_family"], journal)
        stdout, _ = run_command([settings.ffprobe, "-v", "error", "-count_frames", "-show_streams", "-show_format",
                                 "-of", "json", "test_vertical.mp4"], cwd=directory, journal=journal)
        result["ffprobe"] = json.loads(stdout)
        atomic_write_json(directory / "ffprobe.json", result["ffprobe"])
        validate_probe(result["ffprobe"])
        run_command([settings.ffmpeg, "-v", "error", "-nostdin", "-xerror", "-i", "test_vertical.mp4",
                     "-map", "0:v:0", "-map", "0:a:0", "-f", "null", "-"], cwd=directory, journal=journal)
        result["full_decode_passed"] = True
        run_command([settings.ffmpeg, "-v", "error", "-nostdin", "-y", "-ss", "2.5", "-i", "test_vertical.mp4",
                     "-frames:v", "1", "-update", "1", "frame.png"], cwd=directory, journal=journal)
        content = (directory / "test_vertical.mp4").read_bytes()
        result.update(status="technical_checks_passed_awaiting_frame_review", size_bytes=len(content),
                      sha256=hashlib.sha256(content).hexdigest())
    except (MediaError, OSError, ValueError) as error:
        result.update(status="blocked", reason=str(error))
    atomic_write_json(directory / "validation.json", result)
    write_report(directory, result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-directory", type=Path)
    parser.add_argument("--save-paths", action="store_true", help="После реальных технических проверок сохранить подтверждённые пути в .env.")
    args = parser.parse_args()
    root = Path(__file__).parent / "outputs"
    root.mkdir(exist_ok=True)
    directory = args.output_directory or Path(tempfile.mkdtemp(prefix="media_check_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ_"), dir=root))
    if directory.exists() and any(directory.iterdir()):
        parser.exit(1, "Выберите новую пустую папку: предыдущие проверки не перезаписываются.\n")
    result = check_media(directory)
    print(f"Проверка: {result['status']}. Результаты: {directory}")
    if result.get("reason"):
        print(result["reason"])
    if args.save_paths and result["status"] != "blocked":
        from dotenv import set_key
        env_path = Path(__file__).with_name(".env")
        for name, field in (("FFMPEG_PATH", "ffmpeg"), ("FFPROBE_PATH", "ffprobe"), ("SUBTITLE_FONT_PATH", "font")):
            set_key(env_path, name, result["paths"][field])
        env_path.chmod(0o600)
        print("Подтверждённые пути сохранены в локальном .env; остальные настройки не изменены.")
    return 1 if result["status"] == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
