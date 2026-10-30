from __future__ import annotations

import asyncio
import base64
import io
from pathlib import Path
from urllib.parse import unquote, urlparse

import aiohttp
from PIL import Image as PILImage
from PIL import ImageFile, ImageSequence
from astrbot.api import logger

MAX_IMAGE_BYTES = 8 * 1024 * 1024
ImageFile.LOAD_TRUNCATED_IMAGES = True


async def fetch_media_bytes(
    session: aiohttp.ClientSession,
    source: str,
    *,
    max_bytes: int = MAX_IMAGE_BYTES,
) -> bytes | None:
    """下载图片；过期链接或非图片内容返回 None，不抛异常。"""
    source = str(source or "").strip()
    if not source:
        return None
    try:
        if source.startswith("base64://"):
            content = base64.b64decode(source.removeprefix("base64://"))
        elif source.startswith(("http://", "https://")):
            async with session.get(source) as response:
                response.raise_for_status()
                content = await response.content.read(max_bytes + 1)
        elif source.startswith("file://"):
            path_text = unquote(urlparse(source).path)
            if path_text.startswith("/") and len(path_text) > 2 and path_text[2] == ":":
                path_text = path_text[1:]
            content = await asyncio.to_thread(Path(path_text).read_bytes)
        else:
            path = Path(source)
            if not path.is_file():
                return None
            content = await asyncio.to_thread(path.read_bytes)
    except Exception as e:
        logger.debug("[portrayal] 下载图片失败: %s", e)
        return None

    if not content or len(content) > max_bytes:
        return None
    return content


def normalize_image_bytes(
    content: bytes,
    *,
    max_bytes: int = MAX_IMAGE_BYTES,
) -> bytes | None:
    """把图片转成 PNG（动图取首帧），避免视觉模型拒绝特殊 GIF/WebP。"""
    if not content:
        return None
    try:
        image = PILImage.open(io.BytesIO(content))
        image.seek(0)
        if image.mode not in {"RGB", "RGBA"}:
            image = image.convert("RGBA")
        output = io.BytesIO()
        image.save(output, format="PNG", optimize=True)
        data = output.getvalue()
    except Exception as e:
        logger.debug("[portrayal] 图片规范化失败: %s", e)
        return None
    if not data or len(data) > max_bytes:
        return None
    return data


def prepare_pack_image(
    content: bytes,
    *,
    max_bytes: int = MAX_IMAGE_BYTES,
) -> tuple[bytes, str] | None:
    """为 meme_manager 准备图片：GIF 保留动画并修复截断，其他图转 PNG。"""
    if not content:
        return None
    try:
        image = PILImage.open(io.BytesIO(content))
        image_format = str(image.format or "").upper()
    except Exception as e:
        logger.debug("[portrayal] 图片解析失败: %s", e)
        return None

    if image_format == "GIF":
        frames: list[PILImage.Image] = []
        durations: list[int] = []
        try:
            for frame in ImageSequence.Iterator(image):
                frames.append(frame.convert("RGBA"))
                durations.append(int(frame.info.get("duration", 100) or 100))
        except Exception as e:
            logger.debug("[portrayal] GIF 帧解析中止（保留已读取帧）: %s", e)

        if frames:
            output = io.BytesIO()
            try:
                frames[0].save(
                    output,
                    format="GIF",
                    save_all=True,
                    append_images=frames[1:],
                    duration=durations,
                    loop=int(image.info.get("loop", 0) or 0),
                    disposal=2,
                )
                data = output.getvalue()
                if data and len(data) <= max_bytes:
                    return data, ".gif"
            except Exception as e:
                logger.debug("[portrayal] GIF 重编码失败: %s", e)

    normalized = normalize_image_bytes(content, max_bytes=max_bytes)
    if normalized:
        return normalized, ".png"
    return None
