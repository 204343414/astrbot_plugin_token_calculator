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
    "1.5.0",
    "https://github.com/204343414/astrbot_plugin_token_calculator",
)
class TokenCalculator(Star):
    cacuToken: bool = True

    def __init__(self, context: Context):
        super().__init__(context)

        self.plugin_config: Dict[str, Any] = {
            "enabled": True,
            "default_quota": 1000000,
            "daily_reset": True,
            "daily_quota": 500000,
            "user_quotas": {},
            "track_completion_only": False,
            "show_debug_info": False,
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

        logger.info("TokenCalculator 插件已加载（含用户额度限制 / 超额硬拦截 / 自动上下文重置）")

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
        """保存使用量数据"""
        try:
            with open(self.usage_file, "w", encoding="utf-8") as f:
                json.dump(self.usage, f, ensure_ascii=False, indent=2)
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

    # ==================== 指令 ====================

    @filter.command("CacuToken")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def CacuToken(self, event: AstrMessageEvent):
        """切换 Token 计算显示"""
        self.cacuToken = not self.cacuToken
        yield event.plain_result(
            "开启计算Token功能" if self.cacuToken else "关闭计算Token功能"
        )

    @filter.command("token_toggle")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def token_toggle(self, event: AstrMessageEvent):
        """切换用户 Token 额度限制"""
        self.enabled = not self.enabled
        status = "开启" if self.enabled else "关闭"
        yield event.plain_result(f"用户Token额度限制已{status}")

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
            f"剩余: {remaining} tokens"
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
        yield event.plain_result(f"Token Debug 模式已{status}（群聊测试推荐开启）")

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
            result = self._build_quota_exceeded_result(usage, quota)
            if result is not None:
                event.set_result(result)
            event.stop_event()
            logger.warning(
                f"[TokenCalculator] 用户 {user_id} Token额度超限，已硬拦截。"
                f" usage={usage} quota={quota} mode={self.quota_exceeded_response_mode}"
            )

    @filter.on_llm_response()
    async def on_llm_resp(self, event: AstrMessageEvent, resp: LLMResponse):
        """记录 Token 消耗 + 构造显示文本"""
        if self.enabled:
            try:
                user_id = self._get_user_id(event)
                completion = getattr(resp, "raw_completion", None)
                usage = getattr(completion, "usage", None) if completion else None
                if usage is not None:
                    if self.track_completion_only:
                        tokens_to_add = self._safe_int(
                            getattr(usage, "completion_tokens", 0) or 0,
                            0,
                        )
                    else:
                        tokens_to_add = self._safe_int(
                            getattr(usage, "total_tokens", 0) or 0,
                            0,
                        )

                    if tokens_to_add > 0:
                        before_usage, after_usage = self._add_usage(user_id, tokens_to_add)
                        quota = self._get_user_quota(user_id)
                        logger.info(
                            f"用户 {user_id} 本次消耗 {tokens_to_add} tokens "
                            f"(completion_only={self.track_completion_only}) "
                            f"before={before_usage} after={after_usage} quota={quota}"
                        )
                        if before_usage < quota <= after_usage:
                            logger.warning(
                                f"[TokenCalculator] 用户 {user_id} 已在本次响应后达到/超过额度，"
                                f"后续请求将被拦截。after={after_usage} quota={quota}"
                            )
            except Exception as e:
                logger.warning(f"记录用户Token使用量失败: {e}")

        if self.cacuToken:
            try:
                completion = getattr(resp, "raw_completion", None)
                if completion is None or getattr(completion, "usage", None) is None:
                    event.set_extra(
                        "_token_calculator_msg",
                        "(无法获取Token用量信息，可能是当前provider不支持)",
                    )
                    return
                usage = completion.usage
                completion_tokens = self._safe_int(
                    getattr(usage, "completion_tokens", 0) or 0,
                    0,
                )
                prompt_tokens = self._safe_int(
                    getattr(usage, "prompt_tokens", 0) or 0,
                    0,
                )
                total_tokens = self._safe_int(
                    getattr(usage, "total_tokens", 0) or 0,
                    0,
                )

                if self.debug_mode:
                    token_msg = (
                        f"(prompt:{prompt_tokens}, "
                        f"completion:{completion_tokens}, "
                        f"total:{total_tokens})"
                    )
                else:
                    token_msg = (
                        f"(completion_tokens:{completion_tokens},"
                        f"prompt_tokens:{prompt_tokens},"
                        f"token总消耗:{total_tokens})"
                    )
                event.set_extra("_token_calculator_msg", token_msg)
            except Exception:
                event.set_extra(
                    "_token_calculator_msg",
                    "(TokenCalculator插件无法获取信息或者出现未知错误)",
                )

    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent):
        """在消息末尾追加 Token 信息"""
        if not self.cacuToken:
            return

        token_msg = event.get_extra("_token_calculator_msg")
        if not token_msg:
            return

        try:
            result = event.get_result()
            if result is None:
                return
            if result.chain is None:
                result.chain = []
            result.chain.append(Plain(token_msg))
        except Exception:
            raise RuntimeError("CacuToken插件在回复消息的时候出现错误")
