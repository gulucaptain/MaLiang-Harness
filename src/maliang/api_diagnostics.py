"""Explain provider failures without logging credentials or response bodies."""

from __future__ import annotations

import os
from urllib.parse import urlsplit

import openai


def _proxy_address() -> str | None:
    value = os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY")
    value = value or os.environ.get("all_proxy") or os.environ.get("ALL_PROXY")
    if not value:
        return None
    parsed = urlsplit(value)
    if not parsed.hostname:
        return None
    return f"{parsed.hostname}:{parsed.port}" if parsed.port else parsed.hostname


def explain_api_error(error: Exception) -> str | None:
    """Return a safe, actionable explanation for known OpenAI SDK failures."""
    cause: BaseException | None = error
    while cause is not None:
        if isinstance(cause, openai.AuthenticationError):
            return "API 认证失败：检查 OPENAI_API_KEY 是否有效。"
        if isinstance(cause, openai.PermissionDeniedError):
            return "API 拒绝访问：检查账号对当前模型及端点的权限。"
        if isinstance(cause, openai.NotFoundError):
            return "API 未找到请求的模型或路由：检查 MALIANG_MODEL 与 OPENAI_BASE_URL。"
        if isinstance(cause, openai.RateLimitError):
            return "API 达到速率或额度限制：检查账号额度，稍后重试。"
        if isinstance(cause, openai.BadRequestError):
            return "API 拒绝请求参数：检查模型是否支持 Responses API 与当前调用参数。"
        if isinstance(cause, openai.APITimeoutError):
            return "模型 API 请求超时：服务未在配置的 timeout_seconds 内返回。可续跑任务，或增大模型超时；不代表 DNS 或密钥无效。"
        if isinstance(cause, openai.APIConnectionError):
            proxy = _proxy_address()
            if proxy:
                return (
                    f"无法连接 API（当前 HTTPS 代理：{proxy}）。"
                    "检查代理进程和端口，或在终端移除无效的代理环境变量。"
                )
            return "无法连接 API：检查 DNS、网络和 OPENAI_BASE_URL。"
        if isinstance(cause, openai.APIStatusError):
            return f"API 返回 HTTP {cause.status_code}：检查模型、权限及服务状态。"
        cause = cause.__cause__
    return None
