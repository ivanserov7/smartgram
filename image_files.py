"""Проверка JPEG/PNG и предварительного соответствия метаданных сцене."""

import hashlib
import io
import re
import warnings
from urllib.parse import unquote, urlsplit

from PIL import Image, ImageOps, UnidentifiedImageError

from commons_api import MAX_IMAGE_BYTES, VisualError, accepted_license


# Минимальный кадр 720×1280 без увеличения при вертикальном кадрировании 9:16.
MIN_WIDTH = 720
MIN_HEIGHT = 1280
MAX_PIXELS = 40_000_000
PREVIEW_SIZE = 1024
PREVIEW_VERSION = 1
STOP_WORDS = {
    "the", "and", "with", "for", "from", "this", "that", "file", "image", "photo", "photograph",
    "picture", "jpg", "jpeg", "png", "изображение", "фотография", "сцена", "картинка", "это", "или", "для",
}


def inspect_image(content: bytes, minimum_width=MIN_WIDTH, minimum_height=MIN_HEIGHT) -> dict:
    """Проверяет структуру, полное декодирование, формат, размер и разрешение файла."""
    if len(content) > MAX_IMAGE_BYTES:
        raise VisualError("Изображение превышает 10 МиБ.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content), formats=["JPEG", "PNG"]) as image:
                if image.width * image.height > MAX_PIXELS or getattr(image, "is_animated", False):
                    raise VisualError("Изображение слишком велико по числу пикселей или является анимацией.")
                image.verify()
            with Image.open(io.BytesIO(content), formats=["JPEG", "PNG"]) as image:
                image.load()
                width, height = image.size
                orientation = image.getexif().get(274, 1)
                if orientation in {5, 6, 7, 8}:
                    width, height = height, width
                image_format = image.format
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, Image.DecompressionBombWarning, Image.DecompressionBombError):
        raise VisualError("Файл изображения повреждён или не является допустимым JPEG/PNG.") from None
    if width < minimum_width or height < minimum_height:
        raise VisualError(f"Недостаточное разрешение {width}×{height}: нужно не менее {minimum_width}×{minimum_height}.")
    return {"width": width, "height": height, "format": image_format, "orientation": orientation,
            "file_size_bytes": len(content), "file_sha256": hashlib.sha256(content).hexdigest()}


def tokens(text: str) -> set[str]:
    """Простое сравнение слов, без модели и без обещания смыслового распознавания картинки."""
    words = set(re.findall(r"[a-zа-яё0-9]{3,}", text.casefold())) - STOP_WORDS
    # Небольшая нормализация для русских окончаний и английского множественного числа.
    return {word[:5] if re.search("[а-яё]", word) else word[:-1] if len(word) > 4 and word.endswith("s") else word
            for word in words}


def make_vision_preview(content: bytes) -> bytes:
    """Одинаковое JPEG-превью без обрезки: ориентация EXIF, пропорции, белая подложка."""
    inspect_image(content)
    with Image.open(io.BytesIO(content)) as original:
        image = ImageOps.exif_transpose(original).convert("RGBA")
        image.thumbnail((PREVIEW_SIZE, PREVIEW_SIZE), Image.Resampling.LANCZOS)
        background = Image.new("RGB", image.size, "white")
        background.paste(image, mask=image.getchannel("A"))
        buffer = io.BytesIO()
        background.save(buffer, format="JPEG", quality=90, subsampling=0)
    return buffer.getvalue()


def relevance_score(scene: dict, candidate: dict) -> tuple[float, list[str]]:
    query = tokens(scene["search_query"])
    title = tokens(candidate["title"] + " " + candidate["file_title"])
    description = tokens(candidate["description"])
    wanted = tokens(scene.get("title", "") + " " + scene["visual_description"])
    matched = query & (title | description)
    coverage = len(matched) / max(1, len(query))
    if not matched or coverage < 0.35:
        return 0.0, []
    score = (0.7 * coverage + 0.2 * len(wanted & (title | description)) / max(1, len(wanted))
             + 0.1 * len(query & title) / max(1, len(query)))
    return round(score, 4), sorted(matched)


def crop_quality(width: int, height: int) -> float:
    """Число пригодных пикселей после кадрирования 9:16, не просто площадь оригинала."""
    return min(width, height * 9 / 16) * min(height, width * 16 / 9)


def candidate_problem(candidate: dict) -> str:
    """Отсеивает неподдерживаемые материалы и неполную атрибуцию до скачивания."""
    for field in ("title", "file_title", "source_page", "download_url", "author"):
        if not isinstance(candidate.get(field), str) or not candidate[field].strip():
            return f"В метаданных отсутствует корректное поле {field}."
    if (not isinstance(candidate.get("original_mime"), str) or not isinstance(candidate.get("mime"), str)
            or candidate["original_mime"] not in {"image/jpeg", "image/png"} or candidate["mime"] not in {"image/jpeg", "image/png"}):
        return "Поддерживаются только оригиналы и превью JPEG/PNG."
    if (not isinstance(candidate.get("license"), str) or not isinstance(candidate.get("license_url"), str)
            or not candidate.get("license_id")
            or accepted_license(candidate["license"], candidate["license_url"]) != candidate["license_id"]):
        return "Лицензия неизвестна, не поддерживается или её название не соответствует ссылке."
    try:
        page = urlsplit(candidate["source_page"])
        if page.scheme != "https" or page.netloc != "commons.wikimedia.org" or not unquote(page.path).startswith("/wiki/File:"):
            return "Нет корректной страницы источника Commons."
    except ValueError:
        return "Некорректная страница источника."
    if type(candidate.get("width")) is not int or type(candidate.get("height")) is not int:
        return "Некорректные размеры в метаданных."
    if candidate["width"] < MIN_WIDTH or candidate["height"] < MIN_HEIGHT:
        return "Разрешение превью по метаданным недостаточно для кадра 720×1280."
    return ""
