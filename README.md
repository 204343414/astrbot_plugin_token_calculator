# TokenCalculator

用于计算和显示每一次调用 LLM 对话消耗的 Token，并提供用户额度限制与超额硬拦截。

## 主要功能

- 显示每次回复的 `prompt_tokens / completion_tokens / total_tokens`
- 按用户统计 Token 使用量
- 支持 **每日额度** / **累计额度**
- 用户超额后在 `on_llm_request` 阶段 **硬拦截**，不会继续调用 LLM
- 支持自动检测当前会话上下文过大，并在下一次请求前自动清空当前会话上下文
- 提供 `/token_test` 管理员测试命令，方便压测和验证额度边界

## 命令

- `/CacuToken`：开启 / 关闭 Token 显示（管理员）
- `/token_toggle`：开启 / 关闭额度限制（管理员）
- `/token_usage`：查看当前用户 Token 使用情况
- `/token_reset [user_id]`：重置 Token 使用量（管理员）
- `/token_stats`：查看前 10 名统计（管理员）
- `/token_reset_all`：清空所有统计（管理员）
- `/token_debug`：切换 debug token 显示（管理员）
- `/token_test <user_id> <used_tokens>`：直接设置某个用户当前模式的已消耗 token，便于测试拦截逻辑（管理员）

## 推荐配置

### 群聊防烧钱包

```json
{
  "daily_reset": true,
  "daily_quota": 500000,
  "quota_exceeded_response_mode": "silent",
  "auto_reset_context_tokens": 120000
}
```

含义：
- 每日额度 50 万
- 超额后静默拦截，不继续调用 LLM
- 当前会话上下文过大时，自动清空上下文，避免一轮对话吃掉太多 prompt token

## 说明

- 本插件依赖 provider 返回 usage 信息；如果当前 provider 不提供 usage，将无法准确统计。
- 自动上下文重置基于 AstrBot 当前会话的 `conversation.token_usage` 与上下文轮数近似判断。
- 若你只希望按 token 控制上下文，请将 `auto_reset_context_turns` 保持为 `0`。
