from __future__ import annotations

import asyncio
from dataclasses import dataclass
from time import time
from typing import Any

from astrbot.api import logger
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

from .config import PluginConfig
from .message_cache import CachedMessages, MessageCacheStorage


@dataclass
class MessageQueryResult:
    """Store collected messages and query metadata."""

    texts: list[str]
    media: list[dict[str, Any]]
    scanned_messages: int
    from_cache: bool

    @property
    def count(self) -> int:
        return len(self.texts)

    @property
    def media_count(self) -> int:
        return len(self.media)

    @property
    def is_empty(self) -> bool:
        return not self.texts and not self.media


# =========================
# message manager
# =========================


class MessageManager:
    """Manage group-level scans and per-user message caches.

    Queries in the same group share scan progress. Each scanned page caches
    messages for every user, and later queries continue from the group cursor.
    """

    def __init__(self, config: PluginConfig):
        self.cfg = config.message
        self._storage = MessageCacheStorage(config.cache_dir)

        # user cache: group:user -> messages
        self._user_cache, self._group_cursor = self._storage.load()
        if self._purge_expired():
            self._storage.save(self._user_cache, self._group_cursor)

        # group cursor: group -> message_seq
        # group lock: serialize history scans within the same group
        self._group_locks: dict[str, asyncio.Lock] = {}

    # =========================
    # cache helpers
    # =========================

    def _user_key(self, group_id: str, user_id: str) -> str:
        return f"{group_id}:{user_id}"

    def _get_user_cache(
        self, group_id: str, user_id: str
    ) -> CachedMessages | None:
        if self._purge_expired():
            self.save_cache()

        return self._user_cache.get(self._user_key(group_id, user_id))

    def _purge_expired(self) -> bool:
        """Remove expired user caches and orphan group cursors."""
        now = time()
        expired_groups: set[str] = set()
        for key, cached in tuple(self._user_cache.items()):
            if now - cached.timestamp > self.cfg.cache_ttl:
                expired_groups.add(key.split(":", 1)[0])
                del self._user_cache[key]

        changed = bool(expired_groups)
        for group_id in tuple(self._group_cursor):
            if not any(
                key.startswith(f"{group_id}:") for key in self._user_cache
            ):
                del self._group_cursor[group_id]
                changed = True
        return changed

    def _count_group_cached_messages(self, group_id: str) -> int:
        """Count total cached messages for a group across all users."""
        return sum(
            len(cached.texts)
            for key, cached in self._user_cache.items()
            if key.startswith(f"{group_id}:")
        )

    def clear_cache(self):
        self._user_cache.clear()
        self._group_cursor.clear()
        self._storage.clear()

    def save_cache(self) -> None:
        """Persist the current in-memory message cache."""
        self._purge_expired()
        self._storage.save(self._user_cache, self._group_cursor)

    # =========================
    # message parsing
    # =========================

    def _collect_messages(
        self,
        group_id: str,
        messages: list[dict[str, Any]],
    ):
        """Cache one page of group messages by user."""
        now = time()

        for msg in messages:
            user_id = str(msg["sender"]["user_id"])
            if self.cfg.is_protected_user(user_id):
                continue
            try:
                msg_time = int(msg.get("time") or 0)
            except (TypeError, ValueError):
                msg_time = 0

            key = self._user_key(group_id, user_id)
            cached = self._user_cache.get(key)
            if cached is None:
                cached = CachedMessages(texts=[], timestamp=now)
                self._user_cache[key] = cached

            segments = msg.get("message") or []
            for seg in segments:
                seg_type = seg.get("type")
                data = seg.get("data") or {}

                if seg_type == "text":
                    text = str(data.get("text") or "").strip()
                    if text:
                        cached.texts.append(text)
                    continue

                item: dict[str, Any] | None = None
                if seg_type == "image":
                    source = str(
                        data.get("url")
                        or data.get("file")
                        or data.get("path")
                        or ""
                    )
                    summary = str(
                        data.get("summary")
                        or data.get("file")
                        or ""
                    ).strip()
                    if source or summary:
                        item = {
                            "kind": "image",
                            "url": source,
                            "file": str(data.get("file") or ""),
                            "file_id": str(data.get("file_id") or ""),
                            "path": str(data.get("path") or ""),
                            "summary": summary,
                            "sub_type": str(data.get("sub_type") or ""),
                        }
                elif seg_type == "mface":
                    source = str(data.get("url") or data.get("file") or "")
                    summary = str(
                        data.get("summary")
                        or data.get("emoji_id")
                        or ""
                    ).strip()
                    if source or summary:
                        item = {
                            "kind": "mface",
                            "url": source,
                            "file": str(data.get("file") or ""),
                            "file_id": str(data.get("file_id") or ""),
                            "path": str(data.get("path") or ""),
                            "summary": summary,
                            "emoji_id": str(data.get("emoji_id") or ""),
                        }
                elif seg_type == "face":
                    face_id = str(data.get("id") or "").strip()
                    if face_id:
                        item = {"kind": "face", "id": face_id}

                if item is not None:
                    item["after_text_index"] = len(cached.texts)
                    item["msg_time"] = msg_time
                    cached.media.append(item)

            cached.media = cached.media[-self.cfg.max_msg_count :]
            cached.timestamp = now

            if not cached.texts and not cached.media:
                del self._user_cache[key]

    # =========================
    # public api
    # =========================

    async def get_user_texts(
        self,
        event: AiocqhttpMessageEvent,
        target_id: str,
        *,
        max_rounds: int,
        force_refresh: bool = False,
    ) -> MessageQueryResult:
        """Get the target user history from the current group.

        Args:
            event: Current group message event.
            target_id: Target user ID.
            max_rounds: Maximum number of history pages to query.
            force_refresh: Drop this user's cache and rescan the group history.

        Returns:
            The collected texts and query metadata.
        """
        group_id = str(event.get_group_id())
        target_id = str(target_id)
        cache_key = self._user_key(group_id, target_id)

        if force_refresh:
            self._user_cache.pop(cache_key, None)
            self._group_cursor[group_id] = 0
            cached = None
        else:
            # ---------- check user cache first ----------
            cached = self._get_user_cache(group_id, target_id)
            if cached and len(cached.texts) >= self.cfg.max_msg_count:
                return MessageQueryResult(
                    texts=cached.texts[: self.cfg.max_msg_count],
                    media=cached.media[-self.cfg.max_msg_count :],
                    scanned_messages=0,
                    from_cache=True,
                )

        texts = list(cached.texts) if cached else []
        media = list(cached.media) if cached else []

        # ---------- determine scan strategy ----------
        max_fetchable = max_rounds * self.cfg.per_query_count
        group_cached_count = self._count_group_cached_messages(group_id)

        # If group cache already covers what this query could fetch, skip scanning
        if not force_refresh and group_cached_count >= max_fetchable:
            return MessageQueryResult(
                texts=texts[: self.cfg.max_msg_count],
                media=media[-self.cfg.max_msg_count :],
                scanned_messages=0,
                from_cache=True,
            )

        if force_refresh:
            needed_rounds = max_rounds
        else:
            # Only scan the missing rounds: deficit ÷ per_query_count, rounded up
            deficit = max_fetchable - group_cached_count
            needed_rounds = min(
                max_rounds,
                (deficit + self.cfg.per_query_count - 1) // self.cfg.per_query_count
            )

        rounds = 0
        cache_changed = False

        # Resume from the shared group scan cursor.
        message_seq = self._group_cursor.get(group_id, 0)
        group_lock = self._group_locks.setdefault(group_id, asyncio.Lock())

        # ---------- scan group messages ----------
        while rounds < needed_rounds and len(texts) < self.cfg.max_msg_count:
            try:
                # message_seq is a message ID, not an offset.
                async with group_lock:
                    cached = self._get_user_cache(group_id, target_id)
                    if cached and len(cached.texts) >= self.cfg.max_msg_count:
                        texts = list(cached.texts)
                        media = list(cached.media)
                        break

                    message_seq = self._group_cursor.get(group_id, 0)
                    result: dict[str, Any] = await event.bot.api.call_action(
                        "get_group_msg_history",
                        group_id=group_id,
                        message_seq=message_seq,
                        count=self.cfg.per_query_count,
                        reverseOrder=True,
                    )
                    messages = result.get("messages", [])
                    if messages:
                        message_seq = messages[0]["message_id"]
                        self._group_cursor[group_id] = message_seq
                        self._collect_messages(group_id, messages)
                        cache_changed = True

                messages = result.get("messages", [])
                if not messages:
                    break

                # Refresh the target cache after collecting the page.
                cached = self._get_user_cache(group_id, target_id)
                if cached:
                    texts = list(cached.texts)
                    media = list(cached.media)

            except Exception as e:
                logger.error(e)
                break

            rounds += 1

        if cache_changed:
            self.save_cache()

        return MessageQueryResult(
            texts=texts[: self.cfg.max_msg_count],
            media=media[-self.cfg.max_msg_count :],
            scanned_messages=rounds * self.cfg.per_query_count,
            from_cache=cached is not None,
        )