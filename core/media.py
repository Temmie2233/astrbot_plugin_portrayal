from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
from pathlib import Path
from typing import Any

import aiohttp
from astrbot.api import logger

from .config import PluginConfig
from .media_fetch import fetch_media_bytes, normalize_image_bytes

CAPTION_PROMPT = (
    "请用一到两句中文描述这张图片/表情包：画面内容、人物或动物、图中文字、"
    "情绪，以及它通常表达的梗或使用场景。只输出描述，不要评价。"
)
CAPTION_CACHE_TTL = 7 * 24 * 60 * 60


class MediaCaptionService:
    """为画像/克隆分析生成图片和表情包的简短描述。"""

    def __init__(self, cfg: PluginConfig, context) -> None:
        self.cfg = cfg
        self.context = context
        self.cache_path = cfg.cache_dir / "image_captions.json"
        self._cache: dict[str, dict[str, Any]] = self._load_cache()
        self._lock = asyncio.Lock()

    def _load_cache(self) -> dict[str, dict[str, Any]]:
        if not self.cache_path.is_file():
            return {}
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_cache(self) -> None:
        try:
            temporary = self.cache_path.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(self._cache, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary.replace(self.cache_path)
        except Exception as e:
            logger.debug("[portrayal] 保存图片描述缓存失败: %s", e)

    def _provider(self, umo: str | None):
        provider_id = str(self.cfg.media.caption_provider_id or "").strip()
        if not provider_id:
            try:
                app_cfg = self.context.get_config(umo=umo)
                provider_id = str(
                    app_cfg.get("provider_settings", {}).get(
                        "default_image_caption_provider_id", ""
                    )
                    or ""
                ).strip()
            except Exception:
                provider_id = ""
        provider = self.context.get_provider_by_id(provider_id) if provider_id else None
        if provider is None:
            provider = self.cfg.get_provider(umo=umo)
        return provider

    def _cache_key(self, item: dict[str, Any]) -> str:
        source = str(item.get("url") or item.get("file") or item.get("path") or "")
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
        return f"{item.get('kind') or 'media'}:{digest}"

    def _cached_caption(self, key: str) -> str | None:
        cached = self._cache.get(key)
        if not isinstance(cached, dict):
            return None
        if time.time() - float(cached.get("ts") or 0) > CAPTION_CACHE_TTL:
            return None
        caption = str(cached.get("caption") or "").strip()
        return caption or None

    async def _caption_item(
        self,
        item: dict[str, Any],
        provider,
        timeout: float,
        session: aiohttp.ClientSession,
    ) -> str:
        kind = str(item.get("kind") or "")
        summary = str(item.get("summary") or "").strip()
        if kind == "mface" and summary:
            return summary
        if kind == "face":
            return f"QQ表情 id={item.get('id') or '?'}"

        source = str(item.get("url") or item.get("file") or item.get("path") or "")
        if not source:
            return summary or "未识别内容"

        key = self._cache_key(item)
        cached = self._cached_caption(key)
        if cached:
            return cached

        content = await fetch_media_bytes(session, source)
        if not content:
            return summary or "未识别内容"
        normalized = normalize_image_bytes(content)
        if not normalized:
            return summary or "未识别内容"
        image_ref = "base64://" + base64.b64encode(normalized).decode("ascii")

        try:
            response = await asyncio.wait_for(
                provider.text_chat(
                    prompt=CAPTION_PROMPT,
                    image_urls=[image_ref],
                ),
                timeout=timeout,
            )
            caption = str(getattr(response, "completion_text", "") or "").strip()
        except Exception as e:
            logger.debug("[portrayal] 图片描述失败: %s", e)
            caption = ""

        if not caption:
            return summary or "未识别内容"

        self._cache[key] = {"caption": caption, "ts": int(time.time())}
        return caption

    async def caption(
        self,
        media: list[dict[str, Any]],
        *,
        umo: str | None = None,
    ) -> list[dict[str, Any]]:
        """返回带 caption 字段的媒体副本，不修改缓存中的原始数据。"""
        items = [dict(item) for item in media]
        if not self.cfg.media.enable or not items:
            return items

        max_images = max(0, int(self.cfg.media.max_images or 0))
        if max_images <= 0:
            return items

        max_age_hours = max(
            0, int(getattr(self.cfg.media, "max_age_hours", 0) or 0)
        )

        def recent(item: dict[str, Any]) -> bool:
            timestamp = item.get("msg_time")
            if max_age_hours <= 0:
                return True
            if not isinstance(timestamp, (int, float)) or timestamp <= 0:
                return False
            return time.time() - float(timestamp) <= max_age_hours * 3600

        candidates = [
            index
            for index, item in enumerate(items)
            if str(item.get("kind") or "") in {"image", "mface", "face"}
            and recent(item)
        ][-max_images:]

        provider = self._provider(umo)
        timeout = max(1.0, float(self.cfg.media.caption_timeout or 15))
        semaphore = asyncio.Semaphore(3)

        async def run(
            index: int,
            session: aiohttp.ClientSession,
        ) -> None:
            async with semaphore:
                items[index]["caption"] = await self._caption_item(
                    items[index], provider, timeout, session
                )

        if provider is not None:
            session_timeout = aiohttp.ClientTimeout(
                total=max(15.0, timeout * 2)
            )
            async with aiohttp.ClientSession(
                timeout=session_timeout
            ) as session:
                await asyncio.gather(
                    *(run(index, session) for index in candidates)
                )
        else:
            for index in candidates:
                items[index]["caption"] = (
                    str(items[index].get("summary") or "").strip() or "未识别内容"
                )

        async with self._lock:
            self._save_cache()
        return items
