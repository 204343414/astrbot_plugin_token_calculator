from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.core.message.components import Plain
from astrbot.core.provider.entites import ProviderRequest, LLMResponse

import os
import json
from datetime import date
from typing import Dict, Any

@register("TokenCalculator", "rinen0721", "计算并显示Token消耗的插件 + 每用户额度限制（自然语言对话超额拦截）", "1.1.0", "https://github.com/204343414/astrbot_plugin_token_calculator")
class TokenCalculator(Star):
    cacuToken: bool = True
    tokenMsg: str = ""
    llmResponsed: bool = False

    def __init__(self, context: Context):
        super().__init__(context)
        
        # 插件配置（通过 _conf_schema.json 在管理面板配置）
        self.plugin_config: Dict[str, Any] = {
            "enabled": True,
            "default_quota": 1000000,
            "daily_reset": True,
            "daily_quota": 500000,
            "user_quotas": {},
            "admin_user_ids": []
        }
        
        # 尝试从 context 获取真实配置（AstrBot 会自动注入）
        try:
            if hasattr(context, "config") and context.config:
                plugin_name = "astrbot_plugin_token_calculator"
                if plugin_name in context.config:
                    self.plugin_config.update(context.config[plugin_name])
        except Exception as e:
            logger.warning(f"加载插件配置失败，使用默认值: {e}")
        
        self.enabled = self.plugin_config.get("enabled", True)
        self.admin_ids = set(str(x) for x in self.plugin_config.get("admin_user_ids", []))
        
        # 使用数据文件
        self.data_dir = os.path.join(os.path.dirname(__file__), "data")
        os.makedirs(self.data_dir, exist_ok=True)
        self.usage_file = os.path.join(self.data_dir, "usage.json")
        self.usage: Dict[str, Any] = self._load_usage()
        
        logger.info("TokenCalculator 插件已加载（含用户额度限制）")

    def _load_usage(self) -> Dict[str, Any]:
        """加载使用量数据"""
        if os.path.exists(self.usage_file):
            try:
                with open(self.usage_file, "r", encoding="utf-8") as f:
                    return json.load(f)
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
        """获取用户唯一标识（优先 sender_id）"""
        try:
            # 优先使用 sender.user_id
            if hasattr(event, "message_obj") and event.message_obj and event.message_obj.sender:
                sender = event.message_obj.sender
                if hasattr(sender, "user_id") and sender.user_id:
                    return str(sender.user_id)
            # 备选
            if hasattr(event, "get_sender_id"):
                uid = event.get_sender_id()
                if uid:
                    return str(uid)
            # 最后备选 session
            return str(event.unified_msg_origin)
        except Exception:
            return str(event.unified_msg_origin)

    def _get_user_quota(self, user_id: str) -> int:
        """获取用户有效额度"""
        user_quotas = self.plugin_config.get("user_quotas", {})
        if user_id in user_quotas:
            try:
                return int(user_quotas[user_id])
            except:
                pass
        
        if self.plugin_config.get("daily_reset", True):
            return int(self.plugin_config.get("daily_quota", 500000))
        else:
            return int(self.plugin_config.get("default_quota", 1000000))

    def _get_current_usage(self, user_id: str) -> int:
        """获取用户当前使用量（自动处理每日重置）"""
        if "users" not in self.usage:
            self.usage["users"] = {}
        
        user_entry = self.usage["users"].get(user_id, {
            "cumulative_tokens": 0,
            "daily_tokens": 0,
            "daily_date": date.today().isoformat()
        })
        
        today = date.today().isoformat()
        daily_reset = self.plugin_config.get("daily_reset", True)
        
        if daily_reset:
            if user_entry.get("daily_date") != today:
                user_entry["daily_tokens"] = 0
                user_entry["daily_date"] = today
                self.usage["users"][user_id] = user_entry
                self._save_usage()
        
        if daily_reset:
            return int(user_entry.get("daily_tokens", 0))
        else:
            return int(user_entry.get("cumulative_tokens", 0))

    def _add_usage(self, user_id: str, tokens: int):
        """增加用户使用量"""
        if "users" not in self.usage:
            self.usage["users"] = {}
        
        if user_id not in self.usage["users"]:
            self.usage["users"][user_id] = {
                "cumulative_tokens": 0,
                "daily_tokens": 0,
                "daily_date": date.today().isoformat()
            }
        
        user_entry = self.usage["users"][user_id]
        today = date.today().isoformat()
        daily_reset = self.plugin_config.get("daily_reset", True)
        
        # 确保每日重置逻辑
        if daily_reset and user_entry.get("daily_date") != today:
            user_entry["daily_tokens"] = 0
            user_entry["daily_date"] = today
        
        user_entry["cumulative_tokens"] = user_entry.get("cumulative_tokens", 0) + tokens
        
        if daily_reset:
            user_entry["daily_tokens"] = user_entry.get("daily_tokens", 0) + tokens
        
        self.usage["users"][user_id] = user_entry
        self._save_usage()

    def _is_admin(self, user_id: str) -> bool:
        return user_id in self.admin_ids

    # ==================== 指令 ====================

    @filter.command("CacuToken")
    async def CacuToken(self, event: AstrMessageEvent):
        """输入/CacuToken以开启/关闭Token计算显示"""
        self.cacuToken = not self.cacuToken
        if self.cacuToken:
            yield event.plain_result("开启计算Token功能")
        else:
            yield event.plain_result("关闭计算Token功能")

    @filter.command("token_toggle")
    async def token_toggle(self, event: AstrMessageEvent):
        """开启/关闭用户Token额度限制功能"""
        self.enabled = not self.enabled
        status = "开启" if self.enabled else "关闭"
        yield event.plain_result(f"用户Token额度限制已{status}")

    @filter.command("token_usage")
    async def token_usage(self, event: AstrMessageEvent):
        """查询自己当前Token使用情况"""
        user_id = self._get_user_id(event)
        usage = self._get_current_usage(user_id)
        quota = self._get_user_quota(user_id)
        remaining = max(0, quota - usage)
        
        mode = "每日" if self.plugin_config.get("daily_reset", True) else "累计"
        yield event.plain_result(
            f"【Token使用情况】\n"
            f"用户ID: {user_id}\n"
            f"模式: {mode}\n"
            f"已消耗: {usage} tokens\n"
            f"额度: {quota} tokens\n"
            f"剩余: {remaining} tokens"
        )

    @filter.command("token_reset")
    async def token_reset(self, event: AstrMessageEvent, target_user: str = None):
        """管理员重置用户Token使用量（/token_reset 或 /token_reset <user_id>）"""
        admin_id = self._get_user_id(event)
        if not self._is_admin(admin_id):
            yield event.plain_result("只有管理员可以使用此命令")
            return
        
        if target_user:
            # 重置指定用户
            if "users" in self.usage and target_user in self.usage["users"]:
                del self.usage["users"][target_user]
                self._save_usage()
                yield event.plain_result(f"已重置用户 {target_user} 的Token使用量")
            else:
                yield event.plain_result(f"未找到用户 {target_user} 的记录")
        else:
            # 重置所有
            self.usage = {"users": {}}
            self._save_usage()
            yield event.plain_result("已重置所有用户的Token使用量")

    @filter.command("token_stats")
    async def token_stats(self, event: AstrMessageEvent):
        """管理员查看所有用户Token统计（简要）"""
        admin_id = self._get_user_id(event)
        if not self._is_admin(admin_id):
            yield event.plain_result("只有管理员可以使用此命令")
            return
        
        users = self.usage.get("users", {})
        if not users:
            yield event.plain_result("暂无使用记录")
            return
        
        lines = ["【Token使用统计（前10名）】"]
        sorted_users = sorted(
            users.items(),
            key=lambda x: x[1].get("cumulative_tokens", 0),
            reverse=True
        )[:10]
        
        for uid, data in sorted_users:
            cum = data.get("cumulative_tokens", 0)
            daily = data.get("daily_tokens", 0)
            lines.append(f"{uid}: 累计 {cum} | 今日 {daily}")
        
        yield event.plain_result("\n".join(lines))

    # ==================== 核心钩子 ====================

    @filter.on_llm_request()
    async def check_user_quota(self, event: AstrMessageEvent, req: ProviderRequest):
        """在调用LLM前检查用户额度（超额拦截自然语言对话）"""
        if not self.enabled:
            return
        
        user_id = self._get_user_id(event)
        
        # 管理员无限制
        if self._is_admin(user_id):
            return
        
        quota = self._get_user_quota(user_id)
        usage = self._get_current_usage(user_id)
        
        if usage >= quota:
            # 超额，拦截并回复提示
            event.set_result(
                MessageEventResult().message(
                    f"【Token额度限制】\n"
                    f"您的{mode}额度已用尽（已用 {usage}/{quota} tokens）。\n"
                    f"自然语言对话已暂停。\n"
                    f"请使用指令或联系管理员。"
                )
            )
            logger.info(f"用户 {user_id} Token额度超限，已拦截")
            return

    @filter.on_llm_response()
    async def on_llm_resp(self, event: AstrMessageEvent, resp: LLMResponse):
        """记录Token消耗 + 原有显示功能"""
        if self.enabled:
            try:
                user_id = self._get_user_id(event)
                completion = resp.raw_completion
                if completion is not None:
                    usage = getattr(completion, "usage", None)
                    if usage is not None:
                        total_tokens = getattr(usage, "total_tokens", 0)
                        if total_tokens > 0:
                            self._add_usage(user_id, total_tokens)
                            logger.info(f"用户 {user_id} 本次消耗 {total_tokens} tokens")
            except Exception as e:
                logger.warning(f"记录用户Token使用量失败: {e}")
        
        # 原有Token显示功能
        if self.cacuToken:
            try:
                completion = resp.raw_completion
                if completion is None:
                    self.tokenMsg = "(无法获取Token用量信息，可能是当前provider不支持)"
                    return
                usage = completion.usage
                if usage is None:
                    self.tokenMsg = "(无法获取Token用量信息，可能是当前provider不支持)"
                    return
                completion_tokens = usage.completion_tokens
                prompt_tokens = usage.prompt_tokens
                total_tokens = usage.total_tokens
                self.tokenMsg = f"(completion_tokens:{completion_tokens},prompt_tokens:{prompt_tokens},token总消耗:{total_tokens})"
                self.llmResponsed = True
            except Exception:
                self.tokenMsg = "(TokenCalculator插件无法获取信息或者出现未知错误)"

    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent):
        """原有：在消息末尾追加Token信息"""
        if self.cacuToken and self.llmResponsed:
            try:
                result = event.get_result()
                chain = result.chain
                chain.append(Plain(self.tokenMsg))
                self.llmResponsed = False
            except Exception:
                raise RuntimeError("CacuToken插件在回复消息的时候出现错误")
