from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.core.message.components import Plain
from astrbot.core.provider.entities import ProviderRequest, LLMResponse

import json
import os
from datetime import date
from typing import Any, Dict


@register(
    "TokenCalculator",
    "rinen0721",
    "计算并显示每次 LLM 对话消耗的 Token，并支持每用户额度限制（自然语言对话超额拦截）",
    "1.6.0",
    "https://github.com/204343414/astrbot_plugin_token_calculator",
)
class TokenCalculator(Star):
    """
    TokenCalculator v1.6.0 (refurbished)

    变更说明 (不改变原有额度/拦截核心逻辑):
    - 显示开关统一由 show_debug_info / debug_mode 控制
      * debug_mode = False (默认): 纯拟人模式，不在消息末尾追加任何 token 信息
      * debug_mode = True : 追加丰富的 Token 调试信息 (输入/输出/本轮/累计/剩余等)
    - 旧的 cacuToken 开关已合并为 debug_mode 的兼容属性，/CacuToken 命令保留为 /token_debug 的别名并提示已弃用
    - token 用量统计与额度拦截始终在后台运行，与是否显示无关
    - 用量统计现已与 enabled 解耦：即使关闭额度限制也会继续统计，便于 /token_usage /token_stats 查询
    - 丰富 debug 输出格式，更清晰易读
    - 小修缮：异常处理更稳健、消息拼接带换行、日志更详细等
    """

    # ---- 兼容属性：旧版 cacuToken 映射到 debug_mode ----
    @property
    def cacuToken(self) -> bool:
        """向后兼容：旧代码读取 cacuToken 时返回 debug_mode"""
        return getattr(self, "_debug_mode", False)

    @cacuToken.setter
    def cacuToken(self, value: bool):
        self.debug_mode = bool(value)

    # debug_mode 使用 property 以便同步配置
    @property
    def debug_mode(self) -> bool:
        return getattr(self, "_debug_mode", False)

    @debug_mode.setter
    def debug_mode(self, value: bool):
        val = bool(value)
        self._debug_mode = val
        # 同步到 plugin_config，便于其他地方读取
        if hasattr(self, "plugin_config"):
            self.plugin_config["show_debug_info"] = val

    def __init__(self, context: Context):
        super().__init__(context)

        self.plugin_config: Dict[str, Any] = {
            "enabled": True,
            "default_quota": 1000000,
            "daily_reset": True,
            "daily_quota": 500000,
            "user_quotas": {},
            "track_completion_only": False,
            "show_debug_info": False,  # 关键：默认关闭，纯拟人
            "quota_exceeded_response_mode": "reply",
            "auto_reset_context_tokens": 0,
            "auto_reset_context_turns": 0,
        }

        try:
            plugin_name = "astrbot_plugin_token_calculator"
            cfg = self.context.config.get(plugin_name, {}) if hasattr(self.context, "config") else {}
            self.plugin_config.update(cfg)
        except Exception as e:
            logger.warning(f"加载插件配置失败，使用默认值: {e}")

        self.enabled = bool(self.plugin_config.get("enabled", True))
        self.track_completion_only = bool(self.plugin_config.get("track_completion_only", False))

        # 初始化 debug_mode（会同步到 plugin_config）
        self._debug_mode = False
        self.debug_mode = bool(self.plugin_config.get("show_debug_info", False))

        self.quota_exceeded_response_mode = self._normalize_response_mode(
            self.plugin_config.get("quota_exceeded_response_mode", "reply")
        )
        self.auto_reset_context_tokens = self._safe_int(
            self.plugin_config.get("auto_reset_context_tokens", 0),
            0,
        )
        self.auto_reset_context_turns = self._safe_int(
            self.plugin_config.get("auto_reset_context_turns", 0),
            0,
        )

        uq = self.plugin_config.get("user_quotas", {})
        if isinstance(uq, str):
            try:
                uq = json.loads(uq) if uq.strip() else {}
            except Exception:
                uq = {}
        self.user_quotas: Dict[str, int] = {
            str(k): int(v) for k, v in (uq or {}).items()
        }

        self.data_dir = os.path.join(os.path.dirname(__file__), "data")
        os.makedirs(self.data_dir, exist_ok=True)
        self.usage_file = os.path.join(self.data_dir, "usage.json")
        self.usage: Dict[str, Any] = self._load_usage()

        logger.info(
            f"TokenCalculator 插件已加载 v1.6.0 "
            f"(额度限制={'开' if self.enabled else '关'}, "
            f"debug显示={'开' if self.debug_mode else '关(拟人模式)'})"
        )

    # ==================== 工具方法 ====================

    def _safe_int(self, value: Any, default: int = 0) -> int:
        try:
            return int(value)
        except Exception:
            return default

    def _normalize_response_mode(self, value: Any) -> str:
        mode = str(value or "reply").strip().lower()
        return mode if mode in {"reply", "silent"} else "reply"

    def _is_admin(self, user_id: str) -> bool:
        """判断是否为管理员（直接继承 AstrBot 全局 admins_id）"""
        try:
            raw = self.context.config.get("admins_id", []) if hasattr(self.context, "config") else []
            if isinstance(raw, list):
                return str(user_id) in {str(x) for x in raw}
        except Exception:
            pass
        return False

    def _load_usage(self) -> Dict[str, Any]:
        """加载使用量数据"""
        if os.path.exists(self.usage_file):
            try:
                with open(self.usage_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict) and "users" in data:
                        return data
            except Exception as e:
                logger.error(f"读取 usage.json 失败: {e}")
        return {"users": {}}

    def _save_usage(self):
        """保存使用量数据（原子写入）"""
        try:
            tmp_path = self.usage_file + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self.usage, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self.usage_file)
        except Exception as e:
            logger.error(f"保存 usage.json 失败: {e}")

    def _get_user_id(self, event: AstrMessageEvent) -> str:
        """获取用户唯一标识（优先使用 AstrBot 标准方法）"""
        try:
            if hasattr(event, "get_sender_id"):
                uid = event.get_sender_id()
                if uid:
                    return str(uid)
            if hasattr(event, "message_obj") and event.message_obj and event.message_obj.sender:
                sender = event.message_obj.sender
                if hasattr(sender, "user_id") and sender.user_id:
                    return str(sender.user_id)
        except Exception:
            pass
        return str(event.unified_msg_origin)

    def _ensure_user_entry(self, user_id: str) -> Dict[str, Any]:
        if "users" not in self.usage:
            self.usage["users"] = {}
        if user_id not in self.usage["users"]:
            self.usage["users"][user_id] = {
                "cumulative_tokens": 0,
                "daily_tokens": 0,
                "daily_date": date.today().isoformat(),
                "warned_today": False,
            }
        return self.usage["users"][user_id]

    def _get_user_quota(self, user_id: str) -> int:
        """获取用户有效额度"""
        if user_id in self.user_quotas:
            return self.user_quotas[user_id]
        if self.plugin_config.get("daily_reset", True):
            return self._safe_int(self.plugin_config.get("daily_quota", 500000), 500000)
        return self._safe_int(self.plugin_config.get("default_quota", 1000000), 1000000)

    def _refresh_daily_if_needed(self, user_id: str) -> Dict[str, Any]:
        user_entry = self._ensure_user_entry(user_id)
        today = date.today().isoformat()
        daily_reset = bool(self.plugin_config.get("daily_reset", True))

        if daily_reset and user_entry.get("daily_date") != today:
            user_entry["daily_tokens"] = 0
            user_entry["daily_date"] = today
            self.usage["users"][user_id] = user_entry
            self._save_usage()
        return user_entry

    def _get_current_usage(self, user_id: str) -> int:
        """获取用户当前使用量（自动处理每日重置）"""
        user_entry = self._refresh_daily_if_needed(user_id)
        if self.plugin_config.get("daily_reset", True):
            return self._safe_int(user_entry.get("daily_tokens", 0), 0)
        return self._safe_int(user_entry.get("cumulative_tokens", 0), 0)

    def _add_usage(self, user_id: str, tokens: int) -> tuple[int, int]:
        """增加用户使用量，返回 (before_usage, after_usage)"""
        user_entry = self._refresh_daily_if_needed(user_id)
        before_usage = self._get_current_usage(user_id)

        user_entry["cumulative_tokens"] = self._safe_int(
            user_entry.get("cumulative_tokens", 0),
            0,
        ) + tokens
        if self.plugin_config.get("daily_reset", True):
            user_entry["daily_tokens"] = self._safe_int(
                user_entry.get("daily_tokens", 0),
                0,
            ) + tokens

        self.usage["users"][user_id] = user_entry
        self._save_usage()
        after_usage = self._get_current_usage(user_id)
        return before_usage, after_usage

    def _mode_text(self) -> str:
        return "每日" if self.plugin_config.get("daily_reset", True) else "累计"

    def _get_context_turns(self, req: ProviderRequest) -> int:
        contexts = getattr(req, "contexts", []) or []
        if isinstance(contexts, str):
            try:
                contexts = json.loads(contexts)
            except Exception:
                return 0
        if not isinstance(contexts, list):
            return 0

        user_turns = sum(
            1
            for item in contexts
            if isinstance(item, dict) and item.get("role") == "user"
        )
        return user_turns if user_turns > 0 else len(contexts) // 2

    def _get_context_token_usage(self, req: ProviderRequest) -> int:
        conversation = getattr(req, "conversation", None)
        if conversation is None:
            return 0
        return self._safe_int(getattr(conversation, "token_usage", 0), 0)

    async def _maybe_auto_reset_context(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
    ) -> bool:
        reasons = []
        current_context_tokens = self._get_context_token_usage(req)
        current_turns = self._get_context_turns(req)

        if self.auto_reset_context_tokens > 0 and current_context_tokens > 0:
            if current_context_tokens >= self.auto_reset_context_tokens:
                reasons.append(
                    f"context_tokens={current_context_tokens} >= {self.auto_reset_context_tokens}"
                )

        if self.auto_reset_context_turns > 0 and current_turns > 0:
            if current_turns >= self.auto_reset_context_turns:
                reasons.append(
                    f"context_turns={current_turns} >= {self.auto_reset_context_turns}"
                )

        if not reasons:
            return False

        try:
            conversation = getattr(req, "conversation", None)
            conversation_id = getattr(conversation, "cid", None) if conversation else None
            await self.context.conversation_manager.update_conversation(
                event.unified_msg_origin,
                conversation_id,
                history=[],
                token_usage=0,
            )
            if conversation is not None:
                conversation.history = "[]"
                conversation.token_usage = 0
            req.contexts = []
            if hasattr(event, "set_extra"):
                event.set_extra("_clean_group_context_session", True)
                event.set_extra(
                    "_token_calculator_auto_reset_reason",
                    " | ".join(reasons),
                )
            logger.warning(
                f"[TokenCalculator] 自动重置上下文: user={self._get_user_id(event)} "
                f"session={event.unified_msg_origin} reason={' | '.join(reasons)}"
            )
            return True
        except Exception as e:
            logger.warning(f"[TokenCalculator] 自动重置上下文失败: {e}")
            return False

    def _build_quota_exceeded_result(self, usage: int, quota: int) -> MessageEventResult | None:
        if self.quota_exceeded_response_mode == "silent":
            return None
        return MessageEventResult().message(
            f"【Token额度限制】\n"
            f"您的{self._mode_text()}token已用尽（已用 {usage}/{quota} tokens）。\n"
            f"今天似乎聊的太久了喔。\n"
            f"如果认为上下文太长，可输入/reset 重置上下文记忆。如需添加重要记忆，申请好友后解锁，可输入 /我的画像 查看自己的长期记忆。"
        ).stop_event()

    def _build_debug_token_msg(
        self,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
        user_id: str,
        after_usage: int,
        quota: int,
        auto_reset_reason: str | None = None,
        context_tokens: int = 0,
        context_turns: int = 0,
    ) -> str:
        """构建丰富的 debug 显示文本"""
        remaining = max(0, quota - after_usage)
        mode_text = self._mode_text()
        # 短 user_id 显示
        short_uid = user_id if len(user_id) <= 12 else f"{user_id[:6]}...{user_id[-4:]}"

        lines = [
            "",
            "—— Token Debug ——",
            f"输入: {prompt_tokens} | 输出: {completion_tokens} | 本轮: {total_tokens}",
            f"{mode_text}已用: {after_usage} / {quota} | 剩余: {remaining}",
            f"用户: {short_uid}",
        ]
        # 上下文信息（如果有）
        if context_tokens > 0 or context_turns > 0:
            ctx_parts = []
            if context_tokens > 0:
                ctx_parts.append(f"上下文tokens≈{context_tokens}")
            if context_turns > 0:
                ctx_parts.append(f"轮数≈{context_turns}")
            if ctx_parts:
                lines.append(" | ".join(ctx_parts))

        if auto_reset_reason:
            lines.append(f"⚠️ 上下文已自动重置: {auto_reset_reason}")

        return "\n".join(lines)

    # ==================== 指令 ====================

    @filter.command("CacuToken")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def CacuToken(self, event: AstrMessageEvent):
        """兼容旧指令：切换 Token 调试显示 (已弃用，请用 /token_debug)"""
        self.debug_mode = not self.debug_mode
        status = "开启" if self.debug_mode else "关闭"
        # cacuToken 属性已自动同步
        yield event.plain_result(
            f"【已弃用】CacuToken -> 已切换为 Token Debug {status}\n"
            f"建议改用 /token_debug\n"
            f"当前模式: {'调试显示开启' if self.debug_mode else '拟人模式（不显示token）'}"
        )
        logger.info(f"[TokenCalculator] 通过旧指令 /CacuToken 切换 debug_mode -> {self.debug_mode}")

    @filter.command("token_toggle")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def token_toggle(self, event: AstrMessageEvent):
        """切换用户 Token 额度限制"""
        self.enabled = not self.enabled
        status = "开启" if self.enabled else "关闭"
        yield event.plain_result(f"用户Token额度限制已{status}（统计仍在后台运行）")

    @filter.command("token_usage")
    async def token_usage(self, event: AstrMessageEvent):
        """查询当前 Token 使用情况"""
        user_id = self._get_user_id(event)
        usage = self._get_current_usage(user_id)
        quota = self._get_user_quota(user_id)
        remaining = max(0, quota - usage)
        yield event.plain_result(
            f"【Token使用情况】\n"
            f"用户ID: {user_id}\n"
            f"模式: {self._mode_text()}\n"
            f"已消耗: {usage} tokens\n"
            f"额度: {quota} tokens\n"
            f"剩余: {remaining} tokens\n"
            f"显示模式: {'Debug开启' if self.debug_mode else '拟人模式'}"
        )

    @filter.command("token_reset")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def token_reset(self, event: AstrMessageEvent, target_user: str = None):
        """
        管理员重置用户 Token 使用量。
        用法：/token_reset              -> 重置所有人
              /token_reset <user_id>    -> 重置指定用户
        """
        if target_user:
            users = self.usage.get("users", {})
            if target_user in users:
                del self.usage["users"][target_user]
                self._save_usage()
                yield event.plain_result(f"已重置用户 {target_user} 的Token使用量")
            else:
                yield event.plain_result(f"未找到用户 {target_user} 的记录")
        else:
            self.usage = {"users": {}}
            self._save_usage()
            yield event.plain_result("已重置所有用户的Token使用量")

    @filter.command("token_stats")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def token_stats(self, event: AstrMessageEvent):
        """管理员查看 Token 统计（前 10 名）"""
        users = self.usage.get("users", {})
        if not users:
            yield event.plain_result("暂无使用记录")
            return

        today = date.today().isoformat()
        if self.plugin_config.get("daily_reset", True):
            dirty = False
            for uid, data in users.items():
                if data.get("daily_date") != today and self._safe_int(data.get("daily_tokens", 0), 0) != 0:
                    data["daily_tokens"] = 0
                    data["daily_date"] = today
                    dirty = True
            if dirty:
                self._save_usage()

        sorted_users = sorted(
            users.items(),
            key=lambda x: self._safe_int(x[1].get("cumulative_tokens", 0), 0),
            reverse=True,
        )[:10]
        lines = ["【Token使用统计（前10名）】"]
        for uid, data in sorted_users:
            cum = self._safe_int(data.get("cumulative_tokens", 0), 0)
            daily = self._safe_int(data.get("daily_tokens", 0), 0)
            lines.append(f"{uid}: 累计 {cum} | 今日 {daily}")
        lines.append(f"\n显示模式: {'Debug' if self.debug_mode else '拟人'} | 额度限制: {'开' if self.enabled else '关'}")
        yield event.plain_result("\n".join(lines))

    @filter.command("token_reset_all")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def token_reset_all(self, event: AstrMessageEvent):
        """管理员一键清空所有 Token 使用记录"""
        self.usage = {"users": {}}
        self._save_usage()
        yield event.plain_result("✅ 已重置所有用户的Token使用记录")

    @filter.command("token_debug")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def token_debug(self, event: AstrMessageEvent):
        """切换 Debug 模式（显示 prompt/completion 拆分）"""
        self.debug_mode = not self.debug_mode
        status = "开启" if self.debug_mode else "关闭"
        mode_desc = "调试显示开启，将显示输入/输出/本轮消耗等详细信息" if self.debug_mode else "已切回拟人模式，不再显示token消耗"
        yield event.plain_result(f"Token Debug 模式已{status}\n{mode_desc}")
        logger.info(f"[TokenCalculator] debug_mode 切换为 {self.debug_mode} by {self._get_user_id(event)}")

    @filter.command("token_test")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def token_test(
        self,
        event: AstrMessageEvent,
        target_user: str = None,
        used_tokens: str = None,
    ):
        """
        管理员测试命令：直接设置某个用户当前模式的已消耗 Token。
        用法：/token_test <user_id> <used_tokens>
        例子：/token_test 123456789 499900
        """
        if not target_user or used_tokens is None:
            yield event.plain_result(
                "用法：/token_test <user_id> <used_tokens>\n例：/token_test 123456789 499900"
            )
            return

        tokens = self._safe_int(used_tokens, -1)
        if tokens < 0:
            yield event.plain_result("used_tokens 必须是大于等于 0 的整数")
            return

        entry = self._ensure_user_entry(str(target_user))
        today = date.today().isoformat()
        entry["daily_date"] = today

        if self.plugin_config.get("daily_reset", True):
            entry["daily_tokens"] = tokens
            entry["cumulative_tokens"] = max(
                self._safe_int(entry.get("cumulative_tokens", 0), 0),
                tokens,
            )
        else:
            entry["cumulative_tokens"] = tokens

        self.usage["users"][str(target_user)] = entry
        self._save_usage()

        quota = self._get_user_quota(str(target_user))
        remaining = max(0, quota - self._get_current_usage(str(target_user)))
        yield event.plain_result(
            f"【Token测试设置成功】\n"
            f"用户ID: {target_user}\n"
            f"模式: {self._mode_text()}\n"
            f"当前已消耗: {self._get_current_usage(str(target_user))} tokens\n"
            f"额度: {quota} tokens\n"
            f"剩余: {remaining} tokens"
        )

    # ==================== 核心钩子 ====================

    @filter.on_llm_request()
    async def check_user_quota(self, event: AstrMessageEvent, req: ProviderRequest):
        """LLM 调用前检查用户额度（超额硬拦截）"""
        await self._maybe_auto_reset_context(event, req)

        if not self.enabled:
            return

        user_id = self._get_user_id(event)

        if self._is_admin(user_id):
            return

        quota = self._get_user_quota(user_id)
        usage = self._get_current_usage(user_id)

        if usage >= quota:
            user_entry = self._ensure_user_entry(user_id)
            today = date.today().isoformat()
            if user_entry.get("daily_date") != today:
                user_entry["daily_tokens"] = 0
                user_entry["daily_date"] = today
                user_entry["warned_today"] = False
                self._save_usage()
            warned = user_entry.get("warned_today", False)
            result = self._build_quota_exceeded_result(usage, quota) if not warned else None
            if result is not None:
                event.set_result(result)
            event.stop_event()
            user_entry["warned_today"] = True
            self.usage["users"][user_id] = user_entry
            self._save_usage()
            logger.warning(
                f"[TokenCalculator] 用户 {user_id} Token额度超限，已硬拦截。"
                f" usage={usage} quota={quota} mode={self.quota_exceeded_response_mode}"
            )

    @filter.on_llm_response()
    async def on_llm_resp(self, event: AstrMessageEvent, resp: LLMResponse):
        """记录 Token 消耗 + 构造显示文本"""
        user_id = self._get_user_id(event)
        prompt_tokens = completion_tokens = total_tokens = 0
        after_usage = before_usage = 0
        quota = self._get_user_quota(user_id)

        # 1. 统计用量（始终在后台运行，与 enabled / debug_mode 无关）
        try:
            completion = getattr(resp, "raw_completion", None)
            usage = getattr(completion, "usage", None) if completion else None
            if usage is not None:
                prompt_tokens = self._safe_int(getattr(usage, "prompt_tokens", 0) or 0, 0)
                completion_tokens = self._safe_int(getattr(usage, "completion_tokens", 0) or 0, 0)
                total_tokens = self._safe_int(getattr(usage, "total_tokens", 0) or 0, 0)

                if self.track_completion_only:
                    tokens_to_add = completion_tokens
                else:
                    tokens_to_add = total_tokens or (prompt_tokens + completion_tokens)

                if tokens_to_add > 0:
                    before_usage, after_usage = self._add_usage(user_id, tokens_to_add)
                    quota = self._get_user_quota(user_id)
                    logger.info(
                        f"用户 {user_id} 本次消耗 {tokens_to_add} tokens "
                        f"(prompt={prompt_tokens}, completion={completion_tokens}) "
                        f"before={before_usage} after={after_usage} quota={quota}"
                    )
                    if before_usage < quota <= after_usage:
                        logger.warning(
                            f"[TokenCalculator] 用户 {user_id} 已在本次响应后达到/超过额度，"
                            f"后续请求将被拦截。after={after_usage} quota={quota}"
                        )
                else:
                    # 即使本次 0 token，也读取当前用量用于显示
                    after_usage = self._get_current_usage(user_id)
            else:
                after_usage = self._get_current_usage(user_id)
        except Exception as e:
            logger.warning(f"记录用户Token使用量失败: {e}")
            after_usage = self._get_current_usage(user_id)

        # 2. 构造调试显示文本（仅 debug_mode 开启时）
        if not self.debug_mode:
            # 拟人模式：清理可能残留的旧 extra，确保不显示
            if hasattr(event, "set_extra"):
                event.set_extra("_token_calculator_msg", None)
            return

        try:
            if total_tokens == 0 and prompt_tokens == 0 and completion_tokens == 0:
                # 尝试再次解析，以防上面统计分支未执行
                completion = getattr(resp, "raw_completion", None)
                usage = getattr(completion, "usage", None) if completion else None
                if usage is None:
                    event.set_extra(
                        "_token_calculator_msg",
                        "\n\n(Token用量信息不可用，当前provider可能不支持)",
                    )
                    return
                prompt_tokens = self._safe_int(getattr(usage, "prompt_tokens", 0) or 0, 0)
                completion_tokens = self._safe_int(getattr(usage, "completion_tokens", 0) or 0, 0)
                total_tokens = self._safe_int(getattr(usage, "total_tokens", 0) or 0, 0)

            # 获取上下文自动重置原因（如果有）
            auto_reset_reason = None
            if hasattr(event, "get_extra"):
                try:
                    auto_reset_reason = event.get_extra("_token_calculator_auto_reset_reason")
                except Exception:
                    pass

            # 尝试获取当前上下文状态用于显示
            context_tokens = context_turns = 0
            try:
                # resp 阶段拿不到 req，这里只能显示 0，或从 event extra 尝试
                pass
            except Exception:
                pass

            token_msg = self._build_debug_token_msg(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens or (prompt_tokens + completion_tokens),
                user_id=user_id,
                after_usage=after_usage or self._get_current_usage(user_id),
                quota=quota,
                auto_reset_reason=auto_reset_reason,
                context_tokens=context_tokens,
                context_turns=context_turns,
            )
            event.set_extra("_token_calculator_msg", token_msg)
        except Exception as e:
            logger.warning(f"构造 Token Debug 信息失败: {e}")
            event.set_extra(
                "_token_calculator_msg",
                f"\n\n(TokenCalculator 显示异常: {e})",
            )

    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent):
        """在消息末尾追加 Token 信息（仅 debug_mode）"""
        if not self.debug_mode:
            return

        token_msg = event.get_extra("_token_calculator_msg") if hasattr(event, "get_extra") else None
        if not token_msg:
            return

        try:
            result = event.get_result()
            if result is None:
                return
            if result.chain is None:
                result.chain = []
            # 确保消息前有换行，避免粘连
            if not str(token_msg).startswith("\n"):
                token_msg = "\n" + str(token_msg)
            result.chain.append(Plain(token_msg))
        except Exception as e:
            logger.error(f"[TokenCalculator] 追加 Token 信息失败: {e}", exc_info=True)
            # 不再抛 RuntimeError，避免打断正常回复
