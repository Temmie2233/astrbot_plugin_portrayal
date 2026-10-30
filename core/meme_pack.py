from __future__ import annotations

import asyncio
import hashlib
import importlib
import io
import json
import time
from pathlib import Path
from typing import Any

import aiohttp
from astrbot.api import logger
from astrbot.core.star.star import star_map

from .config import PluginConfig
from .media_fetch import fetch_media_bytes, prepare_pack_image


class _UploadFile:
    """MemeManager 上传接口需要的最小 FileStorage 兼容对象。"""

    def __init__(self, filename: str, content: bytes) -> None:
        self.filename = filename
        self.stream = io.BytesIO(content)


class MemePackService:
    """把克隆目标的图片/表情包自动同步到 meme_manager 的 persona 图包。"""

    def __init__(self, cfg: PluginConfig, context) -> None:
        self.cfg = cfg
        self.context = context

    def _find_meme_manager(self):
        for meta in star_map.values():
            if not getattr(meta, "activated", True):
                continue
            if not getattr(meta, "star_cls", None):
                continue
            name = str(getattr(meta, "name", "") or "")
            root = str(getattr(meta, "root_dir_name", "") or "")
            if name == "meme_manager" or root == "astrbot_plugin_meme_manager":
                return meta
        return None

    def _load_modules(self, meta):
        module = getattr(meta, "module", None)
        package = str(getattr(module, "__package__", "") or "")
        if not package:
            module_path = str(getattr(meta, "module_path", "") or "")
            package = module_path.rsplit(".", 1)[0] if "." in module_path else module_path
        storage = importlib.import_module(f"{package}.backend.packs.storage")
        images = importlib.import_module(f"{package}.backend.packs.images")
        return storage, images

    @staticmethod
    def _update_category(
        storage,
        pack_id: str,
        category: str,
        description: str,
    ) -> None:
        pack_dir = Path(storage.PACKS_DIR) / pack_id
        metadata_path = pack_dir / "memes_data.json"
        manifest_path = pack_dir / "manifest.json"

        metadata: dict[str, Any] = {}
        try:
            raw = json.loads(metadata_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                metadata = raw
        except Exception:
            pass
        metadata[category] = description
        metadata_path.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        manifest: dict[str, Any] = {}
        try:
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                manifest = raw
        except Exception:
            pass
        categories = manifest.setdefault("categories", {})
        if not isinstance(categories, dict):
            categories = {}
            manifest["categories"] = categories
        categories[category] = {"description": description}
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @staticmethod
    def _bind_persona(
        storage,
        *,
        pack_id: str,
        persona_id: str,
    ) -> None:
        payload = storage.get_selection_rules()
        rules = payload.get("rules", []) if isinstance(payload, dict) else []
        if not isinstance(rules, list):
            rules = []
        default_rule = next(
            (rule for rule in rules if rule.get("scope") == "default"),
            None,
        )
        if not default_rule:
            default_pack_id = (
                payload.get("default_pack_id")
                if isinstance(payload, dict)
                else ""
            ) or "builtin-default"
            default_rule = {
                "id": "default",
                "scope": "default",
                "pack_id": default_pack_id,
            }

        rule_id = f"portrayal-{persona_id}"
        new_rule = {
            "id": rule_id,
            "scope": "persona",
            "target": persona_id,
            "pack_id": pack_id,
        }
        remaining = [
            rule
            for rule in rules
            if rule.get("scope") != "default"
            and rule.get("id") != rule_id
            and not (
                rule.get("scope") == "persona"
                and rule.get("target") == persona_id
            )
        ]
        storage.save_selection_rules(remaining + [new_rule, default_rule])

    async def ensure_binding(self, uid: str) -> bool:
        """图包已存在时，确保 meme_manager 的 persona 规则指向它。"""
        if not self.cfg.media.enable or not self.cfg.media.auto_pack:
            return False
        meta = self._find_meme_manager()
        if meta is None:
            return False
        try:
            storage, _ = self._load_modules(meta)
        except Exception as e:
            logger.warning("[portrayal] 加载 meme_manager 接口失败: %s", e)
            return False

        pack_id = f"portrayal_{uid}"
        try:
            if not (Path(storage.PACKS_DIR) / pack_id).is_dir():
                return False
        except Exception:
            return False

        try:
            await asyncio.to_thread(
                self._bind_persona,
                storage,
                pack_id=pack_id,
                persona_id=pack_id,
            )
        except Exception as e:
            logger.warning("[portrayal] 切换时绑定表情包失败: %s", e)
            return False
        return True

    async def sync(
        self,
        *,
        uid: str,
        nickname: str,
        media: list[dict[str, Any]],
        event: Any | None = None,
    ) -> dict[str, Any] | None:
        if not self.cfg.media.enable or not self.cfg.media.auto_pack:
            return None
        limit = max(0, int(self.cfg.media.pack_max_images or 0))
        if limit <= 0:
            return None
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

        selected = [
            item
            for item in media
            if str(item.get("kind") or "") in {"image", "mface"}
            and (item.get("url") or item.get("file") or item.get("path"))
            and recent(item)
        ][-limit:]
        if not selected:
            logger.info("[portrayal] 未找到可入包的图片/表情包，跳过图包同步")
            return None

        meta = self._find_meme_manager()
        if meta is None:
            logger.debug("[portrayal] 未找到 meme_manager，跳过自动建包")
            return None
        try:
            storage, images = self._load_modules(meta)
        except Exception as e:
            logger.warning("[portrayal] 加载 meme_manager 接口失败: %s", e)
            return None

        timeout = aiohttp.ClientTimeout(total=20)
        direct_ok = 0
        recovered = 0
        semaphore = asyncio.Semaphore(3)

        client = None
        if event is not None:
            try:
                from astrbot.core.utils.quoted_message.onebot_client import (
                    OneBotClient,
                )

                client = OneBotClient(event)
            except Exception as e:
                logger.debug("[portrayal] OneBotClient 不可用: %s", e)

        async with aiohttp.ClientSession(
            timeout=timeout,
            headers={"User-Agent": "Mozilla/5.0 AstrBot-Portrayal/1.0"},
        ) as session:

            async def fetch_one(item: dict[str, Any]) -> bytes | None:
                nonlocal direct_ok, recovered
                async with semaphore:
                    source = str(
                        item.get("url")
                        or item.get("path")
                        or item.get("file")
                        or ""
                    )
                    if source:
                        content = await fetch_media_bytes(session, source)
                        if content:
                            direct_ok += 1
                            return content

                    if client is None:
                        return None
                    file = str(item.get("file") or "").strip()
                    file_id = str(item.get("file_id") or "").strip()
                    attempts: list[dict[str, str]] = []
                    if file:
                        attempts.append({"file": file})
                    if file_id:
                        attempts.append({"file_id": file_id})
                    if file:
                        attempts.extend(
                            [{"id": file}, {"image": file}]
                        )
                    for action in ("get_image", "get_file"):
                        for params in attempts:
                            try:
                                data = await client.call(action, params)
                            except Exception as e:
                                logger.debug(
                                    "[portrayal] %s 失败 %s: %s",
                                    action,
                                    params,
                                    e,
                                )
                                continue
                            if not isinstance(data, dict):
                                continue
                            resolved = str(
                                data.get("file")
                                or data.get("url")
                                or data.get("path")
                                or ""
                            ).strip()
                            if not resolved and data.get("base64"):
                                resolved = "base64://" + str(data["base64"])
                            if not resolved:
                                continue
                            content = await fetch_media_bytes(session, resolved)
                            if content:
                                recovered += 1
                                return content
                    return None

            fetched = await asyncio.gather(*(fetch_one(item) for item in selected))

        logger.info(
            "[portrayal] 图包候选 %d 张：直链成功 %d，get_image 补取 %d，失败 %d",
            len(selected),
            direct_ok,
            recovered,
            len(selected) - direct_ok - recovered,
        )

        files: list[tuple[bytes, str]] = []
        seen: set[str] = set()
        for content in fetched:
            if not content:
                continue
            digest = hashlib.sha256(content).hexdigest()
            if digest in seen:
                continue
            prepared = prepare_pack_image(content)
            if not prepared:
                continue
            prepared_content, extension = prepared
            seen.add(digest)
            files.append((prepared_content, extension))
        if not files:
            logger.info("[portrayal] 图片下载失败或格式不支持，跳过图包同步")
            return None

        pack_id = f"portrayal_{uid}"
        persona_id = f"portrayal_{uid}"
        category = str(self.cfg.media.pack_category or "克隆表情").strip() or "克隆表情"
        description = f"{nickname} 常用的表情包与图片（由画像克隆自动维护）"

        try:
            await asyncio.to_thread(
                storage.create_local_pack,
                f"画像克隆-{nickname}"[:80],
                description,
                None,
                pack_id,
            )
        except ValueError:
            pass
        except Exception as e:
            logger.warning("[portrayal] 创建表情包失败: %s", e)
            return None

        semantic_manager = getattr(meta.star_cls, "semantic_task_manager", None)
        if semantic_manager is None:
            logger.info(
                "[portrayal] meme_manager 语义化不可用，图包已绑定但暂时不会自动发图"
            )
        guarded = False
        if semantic_manager is not None:
            try:
                semantic_manager.begin_external_pack_operation(
                    pack_id, "同步克隆表情"
                )
                guarded = True
            except Exception as e:
                logger.info("[portrayal] 表情包正在语义化，跳过本次更新: %s", e)
                return None

        added = 0
        try:
            try:
                await asyncio.to_thread(
                    self._update_category,
                    storage,
                    pack_id,
                    category,
                    description,
                )
            except Exception as e:
                logger.debug("[portrayal] 更新表情包分类失败: %s", e)

            for index, (content, extension) in enumerate(files):
                upload = _UploadFile(f"{uid}_{index}{extension}", content)
                try:
                    await asyncio.to_thread(
                        images.add_emoji_to_category,
                        category,
                        upload,
                        pack_id,
                    )
                    added += 1
                except Exception as e:
                    if isinstance(
                        e, getattr(images, "DuplicateEmojiError", ())
                    ):
                        continue
                    logger.debug("[portrayal] 写入表情包失败: %s", e)
        finally:
            if guarded:
                semantic_manager.end_external_pack_operation(pack_id)

        try:
            await asyncio.to_thread(
                self._bind_persona,
                storage,
                pack_id=pack_id,
                persona_id=persona_id,
            )
        except Exception as e:
            logger.warning("[portrayal] 绑定克隆人格表情包失败: %s", e)

        if semantic_manager is not None and added > 0:
            try:
                await semantic_manager.start(pack_id, mode="full")
                logger.info(
                    "[portrayal] 已为 %s 更新表情包并启动语义化（新增 %d 张）",
                    persona_id,
                    added,
                )
            except Exception as e:
                logger.warning("[portrayal] 启动表情包语义化失败: %s", e)

        return {"pack_id": pack_id, "added": added}
