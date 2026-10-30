from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from datetime import date


class UsageLimiter:
    """内存中的画像生成冷却与每日配额，插件重启后清零。"""

    def __init__(
        self,
        *,
        cooldown_seconds: int,
        daily_limit: int,
        target_daily_limit: int,
    ) -> None:
        self.cooldown_seconds = max(0, int(cooldown_seconds or 0))
        self.daily_limit = max(0, int(daily_limit or 0))
        self.target_daily_limit = max(0, int(target_daily_limit or 0))
        self._last_used: dict[str, float] = {}
        self._daily: dict[str, tuple[str, int]] = {}
        self._target_daily: dict[tuple[str, str], tuple[str, int]] = {}

    def check(self, user_id: str, target_id: str) -> str | None:
        """返回拦截原因；None 表示允许。"""
        now = time.time()
        today = date.today().isoformat()

        if self.cooldown_seconds > 0:
            last = self._last_used.get(user_id)
            if last is not None and now - last < self.cooldown_seconds:
                wait = int(self.cooldown_seconds - (now - last)) + 1
                if wait >= 60:
                    remaining = f"{wait // 60} 分 {wait % 60} 秒"
                else:
                    remaining = f"{wait} 秒"
                return f"画像生成冷却中，请等待约 {remaining} 后再试"

        if self.daily_limit > 0:
            day, count = self._daily.get(user_id, (today, 0))
            if day == today and count >= self.daily_limit:
                return (
                    f"你今天的画像生成次数已用完（{self.daily_limit} 次/天），"
                    "明天再来吧"
                )

        if self.target_daily_limit > 0:
            day, count = self._target_daily.get((user_id, target_id), (today, 0))
            if day == today and count >= self.target_daily_limit:
                return (
                    "你今天查询该群友画像的次数已达上限"
                    f"（{self.target_daily_limit} 次/天），明天再试吧"
                )

        return None

    def record(self, user_id: str, target_id: str) -> None:
        """记录一次成功生成。"""
        now = time.time()
        today = date.today().isoformat()
        self._last_used[user_id] = now

        day, count = self._daily.get(user_id, (today, 0))
        self._daily[user_id] = (today, count + 1 if day == today else 1)

        key = (user_id, target_id)
        day, count = self._target_daily.get(key, (today, 0))
        self._target_daily[key] = (today, count + 1 if day == today else 1)


class ConcurrencyLimiter:
    """按群和全局限制同时运行的画像任务数。"""

    def __init__(self, *, group_limit: int, global_limit: int) -> None:
        self.group_limit = max(1, int(group_limit or 1))
        self._global = asyncio.Semaphore(max(1, int(global_limit or 1)))
        self._groups: dict[str, asyncio.Semaphore] = {}

    @asynccontextmanager
    async def limit(self, group_id: str):
        group = self._groups.setdefault(
            group_id,
            asyncio.Semaphore(self.group_limit),
        )
        async with group:
            async with self._global:
                yield
