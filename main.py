import asyncio
import base64
import time

import aiohttp

from astrbot.api import logger, sp
from astrbot.api.event import filter
from astrbot.api.star import Context, Star
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.message.components import At
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)
from astrbot.core.provider.entities import ProviderRequest

from .core.clone_policy import build_clone_system_prompt
from .core.config import PluginConfig
from .core.db import UserProfileDB
from .core.entry import EntryService
from .core.limits import ConcurrencyLimiter, UsageLimiter
from .core.llm import LLMService
from .core.media import MediaCaptionService
from .core.meme_pack import MemePackService
from .core.message import MessageManager
from .core.model import UserProfile


class PortrayalPlugin(Star):
    PORTRAIT_FIELDS = {
        "general": ("portrait", "画像"),
        "positive": ("portrait_positive", "正画像"),
        "negative": ("portrait_negative", "负画像"),
        "match": ("portrait_match", "找对象"),
        "clone": ("clone_prompt", "克隆人格"),
    }

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.cfg = PluginConfig(config, context)
        self.db = UserProfileDB(self.cfg)
        self.msg = MessageManager(self.cfg)
        self.entry_service = EntryService(self.cfg)
        self.llm = LLMService(self.cfg)
        self.media_captioner = MediaCaptionService(self.cfg, self.context)
        self.meme_pack_service = MemePackService(self.cfg, self.context)
        self.usage_limiter = UsageLimiter(
            cooldown_seconds=self.cfg.limits.cooldown_seconds,
            daily_limit=self.cfg.limits.daily_limit,
            target_daily_limit=self.cfg.limits.target_daily_limit,
        )
        self.concurrency_limiter = ConcurrencyLimiter(
            group_limit=self.cfg.limits.group_concurrency,
            global_limit=self.cfg.limits.global_concurrency,
        )

    async def initialize(self):
        pass

    async def terminate(self):
        self.msg.save_cache()

    async def _view_portrayal(self, event: AiocqhttpMessageEvent):
        """输出本地已保存的指定类型画像正文。"""
        ats = [str(seg.qq) for seg in event.get_messages()[1:] if isinstance(seg, At)]
        if not ats:
            yield event.plain_result("命令格式：查看画像 [正/负/找对象] @群友")
            return
        target_id = ats[0]
        if self.cfg.message.is_protected_user(target_id):
            yield event.plain_result("该用户在保护名单中，不允许查询")
            return
        profile = self.db.get(target_id)
        if not profile:
            yield event.plain_result("本地暂无该用户画像记录")
            return

        parts = event.message_str.strip().split()
        kind = parts[1] if len(parts) > 1 else ""
        if kind in {"正", "正画像"}:
            key = "positive"
        elif kind in {"负", "负画像", "逆", "逆画像"}:
            key = "negative"
        elif kind in {"找对象", "匹配", "match"}:
            key = "match"
        else:
            key = "general"

        field, label = self.PORTRAIT_FIELDS[key]
        text = getattr(profile, field, "")
        if not text.strip():
            yield event.plain_result(
                f"本地暂无该用户的【{label}】记录，请先运行：{label} @群友"
            )
            return
        yield event.plain_result(f"【{profile.nickname}】的{label}\n{text}")

    @filter.command("查看画像")
    async def view_portrayal(self, event: AiocqhttpMessageEvent):
        """
        查看画像 @群友
        """
        async for res in self._view_portrayal(event):
            yield res

    def _build_help_text(self) -> str:
        descriptions = {
            "画像": "综合性格分析",
            "正画像": "优势向分析",
            "负画像": "缺点向分析",
            "逆画像": "缺点向分析",
            "找对象": "伴侣匹配建议",
            "克隆人格": "生成克隆人格提示词",
        }
        lines = ["【人物画像指令】"]
        for entry in self.entry_service.entries:
            desc = descriptions.get(entry.command, "自定义提示词")
            admin = "（管理员）" if entry.need_admin else ""
            lines.append(f"· {entry.command} @群友 [轮数] — {desc}{admin}")
        lines.extend(
            [
                "· 查看画像 [正/负/逆/找对象] @群友 — 查看已保存的画像",
                "· 切换人格 @群友 — 切换到克隆人格（管理员）",
                "· 恢复人格 — 恢复切换前的人格（管理员）",
                "· 画像帮助 — 显示本帮助",
                "",
                "画像/正画像/负画像/找对象/克隆人格/查看画像/画像帮助 可直接发送；",
                "切换人格/恢复人格 需使用 / 前缀或先 @bot 唤醒。",
                "轮数省略时使用配置默认值；正/负/找对象需先生成对应类型。",
            ]
        )
        limits = self.cfg.limits
        if (
            (limits.cooldown_seconds or 0) > 0
            or (limits.daily_limit or 0) > 0
            or (limits.reuse_hours or 0) > 0
        ):
            lines.append(
                "生成类命令有冷却/每日次数限制；同类型画像会复用缓存，"
                "管理员可用「强制」刷新。"
            )
        if self.cfg.media.enable:
            pack_note = "，并自动同步 meme_manager 图包" if self.cfg.media.auto_pack else ""
            lines.append(f"克隆人格会分析图片/表情包{pack_note}。")
        return "\n".join(lines)

    @filter.command("画像帮助", alias={"画像help"})
    async def portrayal_help(self, event: AstrMessageEvent):
        """画像帮助 查看可用指令"""
        yield event.plain_result(self._build_help_text())

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def portrayal_without_prefix(self, event: AiocqhttpMessageEvent):
        """无前缀的「查看画像」/「画像帮助」入口，避免与标准命令重复处理。"""
        if event.is_at_or_wake_command:
            return
        cmd = event.message_str.partition(" ")[0]
        if cmd == "查看画像":
            async for res in self._view_portrayal(event):
                yield res
        elif cmd in {"画像帮助", "画像help"}:
            yield event.plain_result(self._build_help_text())

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        if not self.cfg.inject_prompt:
            return
        if not event.message_str:
            return
        sender_id = event.get_sender_id()
        profile = self.db.get(sender_id)
        if not profile:
            return
        info = profile.to_text()
        req.system_prompt += f"\n\n### 当前对话用户的背景信息\n{info}\n\n"

    async def _sync_clone_meme_pack(
        self,
        uid: str,
        nickname: str,
        media: list[dict],
        event,
    ) -> None:
        try:
            await self.meme_pack_service.sync(
                uid=uid,
                nickname=nickname,
                media=media,
                event=event,
            )
        except Exception:
            logger.exception("[portrayal] 自动同步克隆表情包失败")

    def _portrait_key(self, command: str) -> str:
        if "克隆" in command:
            return "clone"
        if "正" in command:
            return "positive"
        if "负" in command or "逆" in command:
            return "negative"
        if "对象" in command:
            return "match"
        return "general"

    def _is_force_refresh(self, event: AstrMessageEvent) -> bool:
        tokens = event.message_str.split()
        return any(token in {"强制", "刷新", "force"} for token in tokens[1:])

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def get_portrayal(self, event: AiocqhttpMessageEvent):
        """
        画像 @群友 <查询轮数>
        """
        cmd = event.message_str.partition(" ")[0]
        prompt = self.entry_service.get_entry(cmd)
        if not prompt:
            return
        if prompt.need_admin and not event.is_admin():
            return

        ats = [str(seg.qq) for seg in event.get_messages()[1:] if isinstance(seg, At)]
        if not ats:
            yield event.plain_result("命令格式：画像 @群友 <查询轮数>")
            return

        # 检查权限
        target_id = ats[0]
        if self.cfg.message.is_protected_user(target_id):
            yield event.plain_result("该用户在保护名单中，不允许查询")
            return

        admin = event.is_admin()
        force = self._is_force_refresh(event)
        if force and not admin:
            yield event.plain_result("仅管理员可使用「强制」刷新")
            return

        portrait_key = self._portrait_key(cmd)
        field, _ = self.PORTRAIT_FIELDS[portrait_key]
        reuse_seconds = max(0, int(self.cfg.limits.reuse_hours or 0)) * 3600

        cached_profile = self.db.get(target_id)
        if cached_profile and not force and reuse_seconds > 0:
            cached_text = getattr(cached_profile, field, "")
            cached_at = cached_profile.portrait_updated_at.get(portrait_key, 0)
            if (
                cached_text.strip()
                and cached_at
                and time.time() - cached_at < reuse_seconds
            ):
                yield event.plain_result(cached_text)
                return

        if not (admin and self.cfg.limits.admin_exempt):
            reason = self.usage_limiter.check(
                str(event.get_sender_id()), target_id
            )
            if reason:
                yield event.plain_result(reason)
                return

        # 解析查询轮数（忽略“强制”等附加词）
        number_tokens = [
            token
            for token in event.message_str.split()[1:]
            if token.isdigit()
        ]
        query_rounds = self.cfg.message.get_query_rounds(
            number_tokens[-1] if number_tokens else None
        )

        # 获取基本信息
        info = await event.bot.get_stranger_info(user_id=int(target_id), no_cache=True)
        profile = UserProfile.from_qq_data(target_id, data=dict(info))
        if old_profile := self.db.get(target_id):
            profile.portrait = old_profile.portrait
            profile.portrait_positive = old_profile.portrait_positive
            profile.portrait_negative = old_profile.portrait_negative
            profile.portrait_match = old_profile.portrait_match
            profile.portrait_updated_at = dict(old_profile.portrait_updated_at)
            profile.timestamp = old_profile.timestamp
            profile.clone_prompt = old_profile.clone_prompt

        yield event.plain_result(
            f"正在发起{query_rounds}轮查询来获取{profile.nickname}的聊天记录..."
        )

        async with self.concurrency_limiter.limit(
            str(event.get_group_id() or "unknown")
        ):
            # 获取聊天记录
            result = await self.msg.get_user_texts(
                event,
                profile.user_id,
                max_rounds=query_rounds,
                force_refresh=force,
            )
            if result.is_empty:
                yield event.plain_result("没有查询到该群友的任何消息")
                return
            media_part = (
                f"，其中{result.media_count}条图片/表情包"
                if result.media_count
                else ""
            )
            if result.from_cache and result.scanned_messages <= 0:
                yield event.plain_result(
                    f"命中缓存，已提取到{result.count}条{profile.nickname}的聊天记录"
                    f"{media_part}，正在{cmd}..."
                )
            else:
                yield event.plain_result(
                    f"已从{result.scanned_messages}条群消息中提取到"
                    f"{result.count}条{profile.nickname}的聊天记录{media_part}，"
                    f"正在{cmd}..."
                )

            media = result.media
            if media and self.cfg.media.enable:
                try:
                    media = await self.media_captioner.caption(
                        media,
                        umo=event.unified_msg_origin,
                    )
                except Exception as e:
                    logger.warning(f"[portrayal] 图片/表情包描述失败，回退文本分析: {e}")
                    media = result.media

            # LLM 分析画像
            try:
                content = await self.llm.generate_portrait(
                    result.texts,
                    media,
                    profile,
                    prompt.content,
                    umo=event.unified_msg_origin,
                )
            except Exception as e:
                logger.error(f"LLM 调用失败：{e}")
                yield event.plain_result(f"分析失败：{e}")
                return

            if not (admin and self.cfg.limits.admin_exempt):
                self.usage_limiter.record(str(event.get_sender_id()), target_id)

            now = int(time.time())
            setattr(profile, field, content)
            profile.portrait_updated_at[portrait_key] = now
            profile.timestamp = now
            self.db.set(profile)

            if (
                "克隆" in cmd
                and self.cfg.media.enable
                and self.cfg.media.auto_pack
                and media
            ):
                asyncio.create_task(
                    self._sync_clone_meme_pack(
                        target_id,
                        profile.nickname,
                        media,
                        event,
                    )
                )

            yield event.plain_result(content)

    async def _set_conversation_persona_id(
        self, umo: str, cid: str, persona_id: str | None
    ) -> None:
        """设置会话人格；None 表示清除会话级人格，回落到配置默认人格。"""
        if persona_id is not None:
            await self.context.conversation_manager.update_conversation_persona_id(
                umo, persona_id, cid
            )
            return

        # AstrBot 的 update_conversation 会忽略 persona_id=None，只能直接清空字段。
        from astrbot.core.db.po import ConversationV2
        from sqlmodel import col, update

        async with self.context.conversation_manager.db.get_db() as session:
            async with session.begin():
                await session.execute(
                    update(ConversationV2)
                    .where(col(ConversationV2.conversation_id) == cid)
                    .values(persona_id=None)
                )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("切换人格")
    async def switch_persona(self, event: AiocqhttpMessageEvent):
        """
        切换人格 @群友
        """
        ats = [str(seg.qq) for seg in event.get_messages()[1:] if isinstance(seg, At)]
        if not ats:
            yield event.plain_result("命令格式：切换人格 @群友")
            return

        target_id = ats[0]
        if self.cfg.message.is_protected_user(target_id):
            yield event.plain_result("该用户在保护名单中，不允许切换")
            return

        profile = self.db.get(target_id)
        if not profile or not profile.clone_prompt.strip():
            yield event.plain_result(
                "该群友暂无可用的克隆人格，请先执行“克隆人格 @群友”"
            )
            return

        umo = event.unified_msg_origin
        cid = await self.context.conversation_manager.get_curr_conversation_id(umo)
        if not cid:
            yield event.plain_result(
                "当前没有对话，请先开始对话或使用 /new 创建一个对话。"
            )
            return

        conversation = await self.context.conversation_manager.get_conversation(
            umo, cid
        )
        switch_state = await sp.get_async(
            scope="umo",
            scope_id=umo,
            key="portrayal_switch_state",
            default=None,
        )
        if not isinstance(switch_state, dict) or not switch_state:
            switch_state = {
                "original_persona_id": (
                    conversation.persona_id if conversation else None
                ),
                "conversation_id": cid,
            }
        switch_state["clone_persona_id"] = profile.persona_id
        await sp.put_async(
            scope="umo",
            scope_id=umo,
            key="portrayal_switch_state",
            value=switch_state,
        )

        force_applied_persona_id = (
            await sp.get_async(
                scope="umo",
                scope_id=umo,
                key="session_service_config",
                default={},
            )
        ).get("persona_id")

        # 切换前保存 bot 原始昵称 / 头像字节（仅在启用同步时）
        if self.cfg.sync_bot_profile:
            saved_info = await sp.get_async(
                scope="umo",
                scope_id=umo,
                key="portrayal_original_bot_info",
                default=None,
            )
            if not saved_info:
                try:
                    login_info = await event.bot.get_login_info()
                    bot_user_id = str(login_info.get("user_id", ""))
                    avatar_b64 = ""
                    if bot_user_id:
                        avatar_url = (
                            f"https://q4.qlogo.cn/headimg_dl?dst_uin={bot_user_id}&spec=640"
                        )
                        try:
                            timeout = aiohttp.ClientTimeout(total=15)
                            async with aiohttp.ClientSession(
                                timeout=timeout
                            ) as session:
                                async with session.get(avatar_url) as resp:
                                    resp.raise_for_status()
                                    avatar_bytes = await resp.read()
                                    avatar_b64 = base64.b64encode(
                                        avatar_bytes
                                    ).decode()
                        except Exception as e:
                            logger.warning(f"下载 bot 原始头像失败：{e}")

                    await sp.put_async(
                        scope="umo",
                        scope_id=umo,
                        key="portrayal_original_bot_info",
                        value={
                            "nickname": login_info.get("nickname", ""),
                            "user_id": bot_user_id,
                            "avatar_b64": avatar_b64,
                        },
                    )
                except Exception as e:
                    logger.warning(f"获取 bot 原始资料失败：{e}")

        system_prompt = build_clone_system_prompt(
            profile.clone_prompt,
            aggression=self.cfg.clone.aggression,
            guardrail_enable=self.cfg.clone.guardrail_enable,
            custom_guardrail=self.cfg.clone.guardrail_text,
        )
        try:
            await self.context.persona_manager.update_persona(
                persona_id=profile.persona_id,
                system_prompt=system_prompt,
            )
        except ValueError:
            await self.context.persona_manager.create_persona(
                persona_id=profile.persona_id,
                system_prompt=system_prompt,
            )

        await self.context.conversation_manager.update_conversation_persona_id(
            umo, profile.persona_id, cid
        )

        # 清空当前对话历史
        await self.context.conversation_manager.update_conversation(
            umo, cid, history=[]
        )

        force_warn_msg = ""
        if force_applied_persona_id:
            force_warn_msg = "提醒：由于自定义规则，您现在切换的人格将不会生效。"

        pack_note = ""
        if self.cfg.media.enable and self.cfg.media.auto_pack:
            try:
                pack_ready = await self.meme_pack_service.ensure_binding(target_id)
            except Exception:
                logger.warning("[portrayal] 切换时检查表情包图库失败", exc_info=True)
                pack_ready = False
            pack_note = (
                " 表情包图库已绑定。"
                if pack_ready
                else " 表情包图库尚未就绪；如刚克隆完请稍等，或执行一次「克隆人格 强制 @群友」。"
            )

        yield event.plain_result(
            f"已将当前对话切换为【{profile.nickname}】的克隆人格，对话历史已清空。"
            f"如需还原，请使用：恢复人格。{force_warn_msg}{pack_note}"
        )

        if self.cfg.sync_bot_profile:
            # 同步 bot 昵称
            try:
                await event.bot.set_qq_profile(nickname=profile.nickname)
                logger.debug(f"已同步bot的昵称为: {profile.nickname}")
            except Exception as e:
                logger.error(f"同步 bot 昵称失败：{e}")

            # 同步 bot 头像
            avatar_url = (
                f"https://q4.qlogo.cn/headimg_dl?dst_uin={profile.user_id}&spec=640"
            )
            try:
                await event.bot.set_qq_avatar(file=avatar_url)
                logger.debug(f"已同步bot的头像为: {avatar_url}")
            except Exception as e:
                logger.error(f"同步 bot 头像失败：{e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("恢复人格")
    async def restore_persona(self, event: AiocqhttpMessageEvent):
        """
        恢复人格
        """
        umo = event.unified_msg_origin
        cid = await self.context.conversation_manager.get_curr_conversation_id(umo)

        switch_state = await sp.get_async(
            scope="umo",
            scope_id=umo,
            key="portrayal_switch_state",
            default=None,
        )
        original_info = await sp.get_async(
            scope="umo",
            scope_id=umo,
            key="portrayal_original_bot_info",
            default=None,
        )

        if not isinstance(switch_state, dict):
            switch_state = None
        if not isinstance(original_info, dict):
            original_info = None

        if not switch_state and not original_info:
            yield event.plain_result("未找到人格切换记录，未做任何修改。")
            return

        persona_msg = "未找到人格切换记录，人格未改动。"
        if switch_state:
            original_persona_id = switch_state.get("original_persona_id")
            if original_persona_id:
                try:
                    await self.context.persona_manager.get_persona(
                        original_persona_id
                    )
                except ValueError:
                    logger.warning(
                        f"原人格 {original_persona_id} 已不存在，回落到配置默认人格"
                    )
                    original_persona_id = None

            if cid:
                await self._set_conversation_persona_id(umo, cid, original_persona_id)
                await self.context.conversation_manager.update_conversation(
                    umo, cid, history=[]
                )
                if original_persona_id:
                    persona_msg = (
                        f"已恢复切换前的人格【{original_persona_id}】，"
                        "对话历史已清空。"
                    )
                else:
                    persona_msg = (
                        "已清除会话级人格，回落到配置默认人格，对话历史已清空。"
                    )
            else:
                persona_msg = "未找到当前对话，人格未改动。"

        # 还原 bot 昵称 / 头像
        restored_nickname = ""
        avatar_restored = False
        restore_failed = False
        if original_info:
            nickname = original_info.get("nickname", "")
            avatar_b64 = original_info.get("avatar_b64", "")
            if nickname:
                try:
                    await event.bot.set_qq_profile(nickname=nickname)
                    restored_nickname = nickname
                    logger.debug(f"已还原bot的昵称为: {nickname}")
                except Exception as e:
                    restore_failed = True
                    logger.error(f"还原 bot 昵称失败：{e}")
            if avatar_b64:
                try:
                    # 用 base64 字节原样塞回去；不能用 dst_uin URL，
                    # 因为那个 URL 在切换后已经指向克隆群友的头像了
                    await event.bot.set_qq_avatar(file=f"base64://{avatar_b64}")
                    avatar_restored = True
                    logger.debug("已用缓存的原图还原bot头像")
                except Exception as e:
                    restore_failed = True
                    logger.error(f"还原 bot 头像失败：{e}")
            if not restore_failed:
                await sp.remove_async(
                    scope="umo",
                    scope_id=umo,
                    key="portrayal_original_bot_info",
                )

        deleted_clone = False
        clone_persona_id = (
            switch_state.get("clone_persona_id") if switch_state else None
        )
        if self.cfg.delete_clone_persona_on_restore and clone_persona_id:
            try:
                await self.context.persona_manager.delete_persona(clone_persona_id)
                deleted_clone = True
                logger.info(f"已删除克隆人格: {clone_persona_id}")
            except Exception as e:
                logger.warning(f"删除克隆人格失败：{e}")

        if switch_state and cid:
            await sp.remove_async(
                scope="umo",
                scope_id=umo,
                key="portrayal_switch_state",
            )

        msg = persona_msg
        if restored_nickname:
            msg += f" bot 昵称已还原为【{restored_nickname}】。"
        if original_info and not avatar_restored and original_info.get("avatar_b64"):
            msg += "（头像还原失败，原图缓存已保留，可重试）"
        elif original_info and restore_failed:
            msg += "（部分 bot 资料还原失败，缓存已保留，可重试）"
        elif original_info:
            msg += "（bot 昵称/头像已还原）"
        else:
            msg += "（未找到原始 bot 资料缓存，昵称/头像未改动）"
        if deleted_clone:
            msg += " 克隆人格已删除。"
        elif (
            self.cfg.delete_clone_persona_on_restore
            and clone_persona_id
            and not deleted_clone
        ):
            msg += "（克隆人格删除失败，请到面板手动处理）"

        yield event.plain_result(msg)
