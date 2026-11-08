"""HTTP 请求工具（聊天命令 + AI 工具双形态）。

融合自两个历史插件（均为 GsCore `plugin_developer_agent` 生成，2026-11-08
合并进 WanWanKit）：
- httpreq：`httpget` / `httppost` 快捷命令（shlex 参数风格，直接执行）
- http_tool：`http` 统一命令 + 按钮确认交互（防误发高危请求）

融合取舍：
- GET 低危 → `httpget` 直接执行（快捷）
- POST 高危 → `httppost` / `http` 带确认（按钮或回复确认词，60s 未确认取消）
- 语法两种都保留（-H/-p/-d 快捷风格 与 headers=/query=/body= JSON 风格）
- AI 工具 `http_request`：模型侧直通（主人对话内受控执行，不走按钮）
- 权限 pm=0（仅主人）：可对任意 URL 发请求，属高危工具

命令：
  httpget <url> [-H "k: v"]... [-p k=v]...              GET（直接执行）
  httppost <url> [-H ...] [-p ...] [-d 请求体]           POST（需确认）
  http <METHOD> <URL> [headers=..] [query=..] [body=..]  统一入口（需确认）
  http帮助 / httpreq帮助                                  输出用法
"""

from __future__ import annotations

import json
import shlex
import time
from dataclasses import dataclass
from typing import Any

import httpx
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.register import ai_tools
from gsuid_core.bot import Bot
from gsuid_core.logger import logger
from gsuid_core.message_models import Button
from gsuid_core.models import Event
from gsuid_core.sv import SV
from pydantic_ai import RunContext

from ..config import load_config

_CAP_DOMAIN = "HTTP 请求"
_COVERS = [
    "发 HTTP GET/POST 请求",
    "调用外部 API / 接口",
    "抓取网页或接口数据",
]

sv = SV("HTTP 请求工具", pm=0)

TIMEOUT = 15.0  # 未配置时的回退默认（可由 http_timeout 覆盖）
MAX_BODY = 1500  # 未配置时的回退默认（可由 http_max_body 覆盖）
CONFIRM_WORDS: tuple[str, ...] = ("确认发送", "确认执行", "确认", "yes", "y", "ok")
CANCEL_WORDS: tuple[str, ...] = ("取消", "算了", "no", "n")


def _limits() -> tuple[float, int]:
    """超时与响应截断长度：读插件配置，未配置回退默认。"""
    cfg = load_config()
    if cfg is None:
        return TIMEOUT, MAX_BODY
    return cfg.http_timeout or TIMEOUT, cfg.http_max_body or MAX_BODY


USAGE = (
    "HTTP 工具用法（仅主人）：\n"
    '  httpget <url> [-H "k: v"]... [-p k=v]...              GET（直接执行）\n'
    "  httppost <url> [-H ...] [-p ...] [-d 请求体]           POST（需确认）\n"
    "  http <METHOD> <URL> [headers=..] [query=..] [body=..]  统一入口（需确认）\n"
    "  http帮助                                                本说明\n"
    "示例：\n"
    "  httpget https://httpbin.org/get -p foo=bar\n"
    '  httppost https://httpbin.org/post -H "Content-Type: application/json" -d \'{"a":1}\'\n'
    '  http POST https://httpbin.org/post headers=\'{"X-Token":"abc"}\' body=\'{"a":1}\'\n'
    "确认词：确认 / 确认发送 / yes；取消词：取消 / 算了"
)


# ---------------------------------------------------------------- 解析


@dataclass
class RequestSpec:
    """一次待发送的 HTTP 请求描述。"""

    method: str
    url: str
    headers: dict[str, str]
    query: dict[str, str]
    body: str | None


def _check_url(url: str) -> str:
    if not (url.startswith("http://") or url.startswith("https://")):
        raise ValueError("URL 必须以 http:// 或 https:// 开头")
    return url


def _parse_pairs(raw: str, name: str) -> dict[str, str]:
    """解析 headers/query 值：优先 JSON 对象，退化为 k:v,k:v 逗号分隔。"""
    raw = raw.strip()
    pairs: dict[str, str] = {}
    if raw.startswith("{"):
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError(f"{name} 必须是 JSON 对象")
        for key, value in data.items():
            pairs[str(key)] = str(value)
        return pairs
    for seg in raw.split(","):
        seg = seg.strip()
        if not seg:
            continue
        if ":" not in seg:
            raise ValueError(f"{name} 片段 {seg} 缺少冒号，应为 k:v")
        key, value = seg.split(":", 1)
        pairs[key.strip()] = value.strip()
    return pairs


def parse_request(text: str) -> RequestSpec:
    """解析 `http` 统一命令参数（http_tool 风格）。解析失败抛 ValueError。"""
    try:
        tokens = shlex.split(text)
    except ValueError as exc:
        raise ValueError(f"引号不匹配：{exc}") from exc
    if not tokens:
        raise ValueError("参数为空")

    method = "GET"
    index = 0
    if tokens[0].upper() in ("GET", "POST"):
        method = tokens[0].upper()
        index = 1
    if index >= len(tokens):
        raise ValueError("缺少 URL")
    url = _check_url(tokens[index])
    index += 1

    headers: dict[str, str] = {}
    query: dict[str, str] = {}
    body: str | None = None
    for token in tokens[index:]:
        if "=" not in token:
            raise ValueError(f"无法解析参数 {token}，应为 key=value 形式")
        key, value = token.split("=", 1)
        key = key.lower()
        if key == "headers":
            headers = _parse_pairs(value, "headers")
        elif key == "query":
            query = _parse_pairs(value, "query")
        elif key == "body":
            body = value
        else:
            raise ValueError(f"未知参数 {key}，仅支持 headers / body / query")
    if method == "GET" and body:
        raise ValueError("GET 请求不支持 body，请改用 POST")
    return RequestSpec(method=method, url=url, headers=headers, query=query, body=body)


def parse_short(text: str, method: str) -> RequestSpec:
    """解析 `httpget`/`httppost` 快捷参数（httpreq 风格：-H/-p/-d）。"""
    try:
        tokens = shlex.split(text)
    except ValueError as exc:
        raise ValueError(f"引号不匹配：{exc}") from exc

    headers: dict[str, str] = {}
    query: dict[str, str] = {}
    body = ""
    url = ""
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in ("-H", "--header"):
            if i + 1 >= len(tokens):
                raise ValueError('-H 需要跟一个"名称: 值"参数')
            pair = tokens[i + 1]
            if ":" not in pair:
                raise ValueError(f'-H 参数格式应为"名称: 值"，收到：{pair}')
            k, v = pair.split(":", 1)
            headers[k.strip()] = v.strip()
            i += 2
        elif tok in ("-p", "--param"):
            if i + 1 >= len(tokens):
                raise ValueError("-p 需要跟一个 k=v 参数")
            pair = tokens[i + 1]
            if "=" not in pair:
                raise ValueError(f"-p 参数格式应为 k=v，收到：{pair}")
            k, v = pair.split("=", 1)
            query[k.strip()] = v
            i += 2
        elif tok in ("-d", "--data"):
            if i + 1 >= len(tokens):
                raise ValueError("-d 需要跟一个请求体参数")
            body = tokens[i + 1]
            i += 2
        elif tok.startswith("-"):
            raise ValueError(f"未知参数：{tok}")
        else:
            if url:
                raise ValueError(f"多余的参数：{tok}")
            url = _check_url(tok)
            i += 1

    if not url:
        raise ValueError("缺少 URL")
    if method == "GET" and body:
        raise ValueError("GET 请求不支持 body，请改用 httppost")
    return RequestSpec(method=method, url=url, headers=headers, query=query, body=body or None)


# ---------------------------------------------------------------- 执行与格式化


def _json_object(raw: str) -> dict[str, Any] | None:
    """body 是 JSON 对象时返回该对象，否则返回 None（按原文发送）。"""
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if isinstance(data, dict):
        return {str(key): value for key, value in data.items()}
    return None


async def perform_request(spec: RequestSpec) -> str:
    """用 httpx 异步发送 GET/POST，返回格式化结果文本（永不抛异常）。"""
    timeout, max_body = _limits()
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            if spec.method == "GET":
                resp = await client.get(spec.url, headers=spec.headers or None, params=spec.query or None)
            elif spec.body is None:
                resp = await client.post(spec.url, headers=spec.headers or None, params=spec.query or None)
            else:
                json_body = _json_object(spec.body)
                if json_body is not None:
                    resp = await client.post(
                        spec.url,
                        headers=spec.headers or None,
                        params=spec.query or None,
                        json=json_body,
                    )
                else:
                    resp = await client.post(
                        spec.url,
                        headers=spec.headers or None,
                        params=spec.query or None,
                        content=spec.body.encode("utf-8"),
                    )
    except httpx.HTTPError as exc:
        return f"⚠️ 请求失败：{type(exc).__name__}: {exc}"
    elapsed = time.perf_counter() - started
    return _format_response(spec, resp, elapsed, max_body)


def _format_response(spec: RequestSpec, resp: httpx.Response, elapsed: float, max_body: int) -> str:
    """把响应回整理成可读文本（响应头取前 10 条，响应体超长截断）。"""
    header_lines = [f"{k}: {v}" for k, v in list(resp.headers.items())[:10]]
    body_text = resp.text
    total = len(body_text)
    if total > max_body:
        body_text = f"{body_text[:max_body]}\n……（共 {total} 字符，已截断）"
    return (
        f"✅ {spec.method} {resp.url}\n"
        f"状态：{resp.status_code} {resp.http_version}｜耗时 {elapsed:.2f}s\n"
        f"响应头（前 {len(header_lines)} 条）：\n" + "\n".join(header_lines) + f"\n响应体：\n{body_text}"
    )


def build_summary(spec: RequestSpec) -> str:
    """生成发送前给用户确认的请求摘要。"""
    lines = [f"方法：{spec.method}", f"URL：{spec.url}"]
    if spec.query:
        lines.append(f"query：{json.dumps(spec.query, ensure_ascii=False)}")
    if spec.headers:
        lines.append(f"headers：{json.dumps(spec.headers, ensure_ascii=False)}")
    if spec.body:
        lines.append(f"body：{spec.body[:300]}")
    return "\n".join(lines)


# ---------------------------------------------------------------- 确认交互


def _is_confirm(reply: str) -> bool:
    reply = reply.strip().lower()
    return any(reply == w or reply.startswith(w) for w in CONFIRM_WORDS)


def _is_cancel(reply: str) -> bool:
    reply = reply.strip().lower()
    return any(reply == w or reply.startswith(w) for w in CANCEL_WORDS)


async def _confirm(bot: Bot, ev: Event, spec: RequestSpec) -> bool:
    """按钮 + 回复双通道确认；60s 未确认/取消一律不发。"""
    resp = await bot.receive_resp(
        reply=(
            f"即将发送 HTTP 请求：\n{build_summary(spec)}\n\n点「确认发送」按钮或回复「确认」执行，回复「取消」放弃。"
        ),
        option_list=[
            Button("✅ 确认发送", "确认发送", "已确认"),
            Button("❌ 取消", "取消", "已取消"),
        ],
        timeout=60,
        unsuported_platform=True,
    )
    if resp is None:
        await bot.send("⚠️ 未收到确认，已取消（未发送请求）")
        return False
    reply = resp.text.strip().lower()
    if _is_cancel(reply):
        await bot.send("已取消，未发送请求。")
        return False
    if not _is_confirm(reply):
        await bot.send(f"未识别的回复「{reply}」，已取消（未发送请求）。")
        return False
    return True


# ---------------------------------------------------------------- 聊天命令


async def _run_confirmed(bot: Bot, ev: Event, spec: RequestSpec) -> None:
    if not await _confirm(bot, ev, spec):
        return
    logger.info(f"[wanwan] HTTP 命令执行：{spec.method} {spec.url}")
    await bot.send(await perform_request(spec))


@sv.on_command("httpget")
async def httpget_handler(bot: Bot, ev: Event) -> None:
    """发起 HTTP GET 请求（低危，直接执行）。"""
    text = ev.text.strip()
    if not text or text in ("帮助", "help"):
        return await bot.send(USAGE)
    try:
        spec = parse_short(text, "GET")
    except ValueError as exc:
        return await bot.send(f"⚠️ 参数解析失败：{exc}\n\n{USAGE}")
    logger.info(f"[wanwan] HTTP 命令执行：GET {spec.url}")
    await bot.send(await perform_request(spec))


@sv.on_command("httppost")
async def httppost_handler(bot: Bot, ev: Event) -> None:
    """发起 HTTP POST 请求（高危，需确认）。"""
    text = ev.text.strip()
    if not text or text in ("帮助", "help"):
        return await bot.send(USAGE)
    try:
        spec = parse_short(text, "POST")
    except ValueError as exc:
        return await bot.send(f"⚠️ 参数解析失败：{exc}\n\n{USAGE}")
    await _run_confirmed(bot, ev, spec)


@sv.on_command(("http", "http请求", "http工具"))
async def http_command_handler(bot: Bot, ev: Event) -> None:
    """统一入口：解析 `http <METHOD> <URL> [headers=..] [query=..] [body=..]` 并确认执行。"""
    text = ev.text.strip()
    if not text or text in ("帮助", "help"):
        return await bot.send(USAGE)
    confirmed, args = _strip_confirm(text)
    try:
        spec = parse_request(args)
    except ValueError as exc:
        return await bot.send(f"⚠️ 参数错误：{exc}\n\n{USAGE}")
    if confirmed:
        logger.info(f"[wanwan] HTTP 命令执行（免确认前缀）：{spec.method} {spec.url}")
        await bot.send(await perform_request(spec))
        return
    await _run_confirmed(bot, ev, spec)


@sv.on_command(("http帮助", "httpreq帮助"))
async def http_help_handler(bot: Bot, ev: Event) -> None:
    """输出 HTTP 请求工具用法。"""
    await bot.send(USAGE)


def _strip_confirm(raw: str) -> tuple[bool, str]:
    """若参数以确认词开头，去掉它并返回 (True, 剩余参数)。"""
    for word in CONFIRM_WORDS:
        if raw == word:
            return True, ""
        if raw.startswith(word + " "):
            return True, raw[len(word) + 1 :].strip()
    return False, raw


# ---------------------------------------------------------------- AI 工具


@ai_tools(
    category="common",
    capability_domain=_CAP_DOMAIN,
    covers=_COVERS,
    code_callable=False,
    timeout=45.0,
)
async def http_request(
    ctx: RunContext[ToolContext],
    url: str,
    method: str = "GET",
    headers: str = "",
    query: str = "",
    body: str = "",
) -> str:
    """发一次 HTTP GET/POST 请求并返回状态码、响应头摘要与响应体。

    调外部 API / 抓接口数据时用这个。只支持 GET 与 POST；headers/query
    传 JSON 字符串（如 '{"X-Token":"abc"}'）；body 仅 POST，JSON 对象会按
    JSON 发送、其余按原文。响应体超 1500 字符自动截断。

    Args:
        url: 目标地址，必须以 http:// 或 https:// 开头。
        method: GET 或 POST，默认 GET。
        headers: 请求头 JSON 字符串，可空。
        query: 查询参数 JSON 字符串，可空。
        body: POST 请求体，可空（JSON 对象自动按 JSON 发送）。
    """
    clean_url = (url or "").strip()
    if not clean_url:
        return "⚠️ 请提供 url"
    m = (method or "GET").strip().upper()
    if m not in ("GET", "POST"):
        return "⚠️ method 只支持 GET 或 POST"

    try:
        hdrs = _parse_pairs(headers, "headers") if headers.strip() else {}
        qs = _parse_pairs(query, "query") if query.strip() else {}
    except (ValueError, json.JSONDecodeError) as exc:
        return f"⚠️ headers/query 解析失败（应为 JSON 对象）：{exc}"

    try:
        spec = RequestSpec(
            method=m,
            url=_check_url(clean_url),
            headers=hdrs,
            query=qs,
            body=body.strip() or None,
        )
    except ValueError as exc:
        return f"⚠️ {exc}"
    if m == "GET" and spec.body:
        return "⚠️ GET 请求不支持 body，请改用 POST"

    out = await perform_request(spec)
    if out.startswith("⚠️"):
        return out
    logger.info(f"[wanwan] HTTP AI 工具执行：{m} {clean_url}")
    return out
