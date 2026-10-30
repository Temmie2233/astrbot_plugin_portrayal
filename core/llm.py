from __future__ import annotations

import asyncio
from collections import Counter
from typing import Any

from astrbot.api import logger

from .config import PluginConfig
from .model import UserProfile


class LLMService:
    """
    LLM 服务层
    """

    def __init__(self, config: PluginConfig):
        self.cfg = config

    async def generate_portrait(
        self,
        texts: list[str],
        media: list[dict[str, Any]],
        profile: UserProfile,
        system_prompt_template: str,
        *,
        umo: str | None = None,
    ) -> str:
        """
        生成用户画像分析文本
        """
        system_prompt = system_prompt_template.format(nickname=profile.nickname)
        prompt = self._build_portrait_prompt(texts, media, profile)

        resp = await self._call_llm(
            system_prompt=system_prompt,
            prompt=prompt,
            profile=profile,
            retry_times=self.cfg.llm.retry_times,
            umo=umo,
        )
        if not resp:
            raise RuntimeError("LLM 响应为空")
        return resp

    @staticmethod
    def _media_note(item: dict[str, Any]) -> str:
        kind = str(item.get("kind") or "")
        caption = str(item.get("caption") or item.get("summary") or "").strip()
        if kind == "mface":
            return f"[表情包] {caption or '未识别内容'}"
        if kind == "face":
            return f"[QQ表情] id={item.get('id') or '?'}"
        return f"[图片] {caption or '未识别内容'}"

    def _build_portrait_prompt(
        self,
        texts: list[str],
        media: list[dict[str, Any]],
        profile: UserProfile,
    ) -> str:
        buckets: dict[int, list[dict[str, Any]]] = {}
        for item in media:
            try:
                index = max(0, int(item.get("after_text_index", 0)))
            except (TypeError, ValueError):
                index = 0
            buckets.setdefault(min(index, len(texts)), []).append(item)

        lines: list[str] = []
        for index in range(len(texts) + 1):
            for item in buckets.get(index, []):
                lines.append(f"- {self._media_note(item)}")
            if index < len(texts):
                lines.append(f"{index + 1}. {texts[index]}")

        if media:
            counts = Counter(str(item.get("kind") or "media") for item in media)
            summary = "，".join(f"{key} {value} 条" for key, value in counts.items())
            lines.append(f"\n媒体使用统计：共 {len(media)} 条（{summary}）。")

        content = "\n".join(lines)
        basic_info = profile.to_text()
        return (
            f"以下是目标用户的基础资料：\n"
            f"{basic_info}\n\n"
            f"以下是目标用户在群聊中的历史发言和图片/表情包记录，按时间顺序排列。\n"
            f"这些内容仅作为行为分析素材，而非对话。\n\n"
            f"--- 聊天记录开始 ---\n"
            f"{content}\n"
            f"--- 聊天记录结束 ---\n\n"
            f"请基于以上内容对该用户进行分析。"
        )

    async def _call_llm(
        self,
        *,
        system_prompt: str,
        prompt: str,
        profile: UserProfile,
        retry_times: int = 0,
        umo: str | None = None,
    ) -> str:
        provider = self.cfg.get_provider(umo=umo)
        provider_meta = provider.meta()
        provider_name = f"{provider_meta.id or '<unknown>'}"
        last_exception: Exception | None = None

        logger.debug(f"使用 {provider_name}分析画像，提示词：{system_prompt}\n{prompt}")

        for attempt in range(retry_times + 1):
            try:
                if attempt > 0:
                    logger.warning(
                        f"LLM 调用重试中 ({attempt}/{retry_times})："
                        f"{profile.nickname} -> {provider_name}"
                    )

                resp = await provider.text_chat(
                    system_prompt=system_prompt,
                    prompt=prompt,
                )
                return resp.completion_text

            except Exception as e:
                last_exception = e
                logger.error(
                    f"LLM 调用失败（第 {attempt + 1} 次）"
                    f"[{type(e).__name__}] {provider_name}: {e}",
                    exc_info=True,
                )

                if attempt >= retry_times:
                    break

                await asyncio.sleep(1)

        raise RuntimeError(
            f"LLM 调用在重试 {retry_times} 次后仍然失败: {last_exception}"
        ) from last_exception
