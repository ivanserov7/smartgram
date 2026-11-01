"""Проверка встроенных растров: не путаем base64 с ключами и не пропускаем вложения."""

import base64
import binascii
import hashlib
import io
import re
import struct
import zlib
from PIL import Image

from build_review_archive import SECRET_VALUE
from review_archive_sync import ReviewSyncError

IMAGE_URL = re.compile(r'data:image/(jpeg|png);base64,([A-Za-z0-9+/=]+)(?=["\'])')


def validate_jpeg(content):
    """Только baseline JPEG от нашего Pillow: JFIF, таблицы, один скан, конец без хвоста."""
    if not content.startswith(b"\xff\xd8"):
        raise ValueError("JPEG signature")
    position = 2
    frame = False
    while position + 4 <= len(content):
        if content[position] != 255:
            raise ValueError("JPEG marker")
        marker = content[position + 1]
        position += 2
        size = struct.unpack_from(">H", content, position)[0]
        if size < 2 or position + size > len(content):
            raise ValueError("JPEG segment")
        body = content[position + 2 : position + size]
        position += size
        if marker == 0xE0:
            if size != 16 or not body.startswith(b"JFIF\0"):
                raise ValueError("JPEG metadata")
        elif marker == 0xC0:
            if frame or len(body) < 6 or body[0] != 8:
                raise ValueError("JPEG frame")
            frame = True
        elif marker in (0xDB, 0xC4, 0xDD):
            pass
        elif marker == 0xDA:
            if not frame:
                raise ValueError("JPEG scan before frame")
            while position < len(content):
                location = content.find(b"\xff", position)
                if location < 0 or location + 1 >= len(content):
                    raise ValueError("JPEG truncated scan")
                next_byte = content[location + 1]
                if next_byte == 0 or 0xD0 <= next_byte <= 0xD7:
                    position = location + 2
                    continue
                if next_byte == 0xD9 and location + 2 == len(content):
                    return
                raise ValueError("JPEG extra scan/attachment")
            raise ValueError("JPEG missing end")
        else:
            raise ValueError("JPEG unknown metadata/marker")
    raise ValueError("JPEG incomplete")


def validate_png(content):
    if not content.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("PNG signature")
    position = 8
    chunks = []
    sizes = {b"IHDR": 13, b"IEND": 0, b"pHYs": 9, b"gAMA": 4, b"sRGB": 1}
    while position + 12 <= len(content):
        size = struct.unpack_from(">I", content, position)[0]
        kind = content[position + 4 : position + 8]
        end = position + 12 + size
        if end > len(content) or kind not in {*sizes, b"IDAT"}:
            raise ValueError("PNG attachment")
        if kind in sizes and size != sizes[kind]:
            raise ValueError("PNG chunk length")
        expected = struct.unpack_from(">I", content, position + 8 + size)[0]
        if zlib.crc32(content[position + 4 : position + 8 + size]) != expected:
            raise ValueError("PNG CRC")
        chunks.append(kind)
        position = end
        if kind == b"IEND":
            break
    if (
        not chunks
        or position != len(content)
        or chunks[0] != b"IHDR"
        or chunks[-1] != b"IEND"
        or chunks.count(b"IHDR") != 1
        or b"IDAT" not in chunks
    ):
        raise ValueError("PNG structure/trailing bytes")


def text_without_checked_images(text, secret_values):
    """HTML не меняется. Фильтруем только представление для поиска ложных совпадений."""
    pattern = re.compile(SECRET_VALUE.pattern.encode("ascii"))
    secrets = [value.encode() for value in secret_values if value]
    checked = set()

    def check(match):
        signature = hashlib.sha256(match.group(0).encode()).digest()
        if signature in checked:
            return "[RASTER_CHECKED]"
        try:
            content = base64.b64decode(match.group(2), validate=True)
            if not content or len(content) > 15 * 1024 * 1024:
                raise ValueError("Image size")
            (validate_jpeg if match.group(1) == "jpeg" else validate_png)(content)
            with Image.open(io.BytesIO(content), formats=["JPEG", "PNG"]) as image:
                if image.width * image.height > 40_000_000 or getattr(
                    image, "is_animated", False
                ):
                    raise ValueError("Image dimensions/animation")
                image.load()
                pixels = image.tobytes()
            if (
                pattern.search(content)
                or pattern.search(pixels)
                or any(value in content or value in pixels for value in secrets)
            ):
                raise ReviewSyncError(
                    "В содержимом встроенного изображения обнаружен возможный секрет; передача отменена."
                )
        except (
            ValueError,
            binascii.Error,
            OSError,
            struct.error,
            Image.DecompressionBombError,
        ) as error:
            raise ReviewSyncError(
                "Встроенное изображение повреждено или содержит неподдерживаемые данные; передача отменена."
            ) from None
        checked.add(signature)
        return "[RASTER_CHECKED]"

    return IMAGE_URL.sub(check, text)
