"""B站 Web API 客户端（文字动态发布 + 删除）。

移植自 legacy MCP `bili_publish.py`（2026-10-29 四组对照实测通过的配方，
2026-11-07 并入插件）：JSON body + w_ 前缀 query + WBI 签名。
凭据 = cfg.bili_token_path 指向的一行浏览器 Cookie（SESSDATA; bili_jct）。

纪律沿袭：只发主人确认过的正文（本层不改写不代写）；失败原样返回
ERROR 开头原因，不自动重试乱闯；无凭据直接报缺失、不发任何请求。

网络用阻塞 urllib——调用方须 `asyncio.to_thread(post_dynamic, ...)`，
避免阻塞 GsCore 事件循环。代理：opener 显式空代理 + 容器 no_proxy
已含 .bilibili.com/.b23.tv（不再改进程级环境变量，插件与 core 共进程）。
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"

TOKEN_PATH = "/gsuid_core/data/wanwan/.bili_token"
TIMEOUT = 15.0

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

_WBI_TAB = [
    46,
    47,
    18,
    2,
    53,
    8,
    23,
    32,
    15,
    50,
    10,
    31,
    58,
    3,
    45,
    35,
    27,
    43,
    5,
    49,
    33,
    9,
    42,
    19,
    29,
    28,
    14,
    39,
    12,
    38,
    41,
    13,
    37,
    48,
    7,
    16,
    24,
    55,
    40,
    61,
    26,
    17,
    0,
    1,
    60,
    51,
    30,
    4,
    22,
    25,
    54,
    21,
    56,
    59,
    6,
    63,
    57,
    62,
    11,
    36,
    20,
    34,
    44,
    52,
]


def set_token_path(path: str) -> None:
    """由工具层把 cfg.bili_token_path 注进来（配置可换位置）。"""
    global TOKEN_PATH
    if path and path.strip():
        TOKEN_PATH = path.strip()


def _open_retry(req, timeout: float = TIMEOUT, attempts: int = 3):
    """网络抖动重试：连接/超时类异常重试3次（1s/2s 退避），HTTP 错误原样抛出。"""
    last = None
    for i in range(attempts):
        try:
            return _opener.open(req, timeout=timeout)
        except urllib.error.HTTPError:
            raise
        except Exception as exc:  # noqa: BLE001
            last = exc
            if i < attempts - 1:
                time.sleep(1.0 + i)
    raise last  # type: ignore[misc]


def _read_token() -> str:
    try:
        with open(TOKEN_PATH, encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def _creds() -> tuple[str, str]:
    """返回 (SESSDATA, bili_jct)；缺文件或缺字段返回空串。"""
    raw = _read_token()
    sess = ""
    jct = ""
    m = re.search(r"SESSDATA=([^;\s]+)", raw)
    if m:
        sess = m.group(1)
    m = re.search(r"bili_jct=([^;\s]+)", raw)
    if m:
        jct = m.group(1)
    return sess, jct


def _require_auth() -> str:
    sess, jct = _creds()
    if not sess:
        return (
            "ERROR: 缺少登录态。把 B站 Cookie 写进 bili_token 文件，格式："
            "`SESSDATA=xxx; bili_jct=yyy`（浏览器 F12 抄整段）。"
        )
    if not jct:
        return "ERROR: 登录态里缺 bili_jct（写操作必须的 csrf 令牌），补齐后再试。"
    return ""


def _brief(text: object, limit: int = 200) -> str:
    flat = " ".join(str(text or "").split())
    return flat[:limit] + ("..." if len(flat) > limit else "")


def _wbi_query(params: dict[str, str], mixin_key: str, wts: str) -> str:
    """WBI 签名的纯计算段：过滤危险字符 → 按 key 排序 → urlencode → md5。"""
    clean = {k: "".join(c for c in str(v) if c not in "!'()*") for k, v in params.items()}
    clean["wts"] = wts
    query = urllib.parse.urlencode(dict(sorted(clean.items())))
    w_rid = hashlib.md5((query + mixin_key).encode("utf-8")).hexdigest()
    return query + "&w_rid=" + w_rid


def _wbi_sign(params: dict[str, str]) -> tuple[str, str]:
    """取 WBI 密钥（每次现取 nav，B站会轮换）并签名，返回 (query串, err)。"""
    req = urllib.request.Request(
        "https://api.bilibili.com/x/web-interface/nav",
        headers={"User-Agent": UA, "Referer": "https://www.bilibili.com/"},
    )
    try:
        with _open_retry(req) as resp:
            nav = json.loads(resp.read().decode("utf-8", "replace"))
        wbi = (nav.get("data") or {}).get("wbi_img") or {}
        img = wbi.get("img_url", "").rsplit("/", 1)[-1].split(".")[0]
        sub = wbi.get("sub_url", "").rsplit("/", 1)[-1].split(".")[0]
    except Exception as exc:  # noqa: BLE001
        return "", f"ERROR: 取 WBI 密钥失败（{_brief(exc, 80)}）。"
    if not img or not sub:
        return "", "ERROR: nav 响应缺 wbi_img（可能被风控，code={}）。".format(nav.get("code"))
    mixin = "".join((img + sub)[i] for i in _WBI_TAB)[:32]
    return _wbi_query(params, mixin, str(int(time.time()))), ""


def _post_json(url: str, payload: dict, origin: str = "https://t.bilibili.com") -> tuple[int, str]:
    """JSON POST（create/dyn 实测必须 JSON body，表单格式过不了）。"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "User-Agent": UA,
            "Content-Type": "application/json",
            "Cookie": _read_token(),
            "Referer": origin + "/",
            "Origin": origin,
            "Accept": "application/json, text/plain, */*",
        },
    )
    try:
        with _open_retry(req) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return 0, f"{type(exc).__name__}: {exc}"


def _unwrap(status: int, body: str) -> tuple[dict | None, str]:
    """返回 (data, err)。B站接口统一 {code, message, data}。"""
    if status == 0:
        return None, f"ERROR: 连不上 B站（{_brief(body, 140)}）。"
    try:
        doc = json.loads(body)
    except Exception:  # noqa: BLE001
        return None, f"ERROR: B站返回不是 JSON（HTTP {status}）：{_brief(body, 160)}"
    code = doc.get("code", -1)
    if code == 0:
        data = doc.get("data")
        return (data if isinstance(data, dict) else {}), ""
    msg = doc.get("message", "")
    if code == -101:
        return None, "ERROR: 登录态过期（-101），更新 bili_token 里的 SESSDATA。"
    if code == -111:
        return None, "ERROR: csrf 校验失败（-111），bili_jct 不对或与 SESSDATA 不匹配。"
    if code in (-352, -412):
        return None, f"ERROR: 被 B站风控拦截（code={code}）。稍后再试；屡次失败需换网络出口。"
    if code == 4126146:
        return None, "ERROR: B站拒收——正文别塞进 title 字段（title 恒为空串，标题=正文首行）。"
    if code == 4100001:
        return None, (
            "ERROR: 发布参数未过网关（4100001）。当前是实测配方（JSON body + w_ 前缀 query + "
            "WBI 签名），仍报说明接口变了，需主人重新 Copy as cURL 对照修复。"
        )
    return None, f"ERROR: B站返回 code={code}：{_brief(msg, 80)}"


def post_dynamic(text: str) -> str:
    """发布一条文字动态。成功返回 id 与链接；失败返回 ERROR 开头原因。"""
    auth_err = _require_auth()
    if auth_err:
        return auth_err
    text = (text or "").strip()
    if not text:
        return "ERROR: 动态正文不能为空。"
    _, jct = _creds()
    # upload_id 形如 {uid}_{unix秒}_{随机4位}（浏览器实测格式），uid 从 Cookie 取
    uid = ""
    m = re.search(r"(?:^|;\s*)DedeUserID=([^;\s]+)", _read_token())
    if m:
        uid = m.group(1)
    upload_id = "{}_{}_{}".format(uid or "0", int(time.time()), random.randint(1000, 9999))
    # w_ 前缀：bodyPayload 中间件把 body 的 upload_id/meta 搬进 query（漏则 4100001）
    query, err = _wbi_sign(
        {
            "platform": "web",
            "csrf": jct,
            "w_dyn_req.upload_id": upload_id,
            "w_dyn_req.meta": '{"app_meta":{"from":"create.dynamic.web","mobi_app":"web"}}',
        }
    )
    if err:
        return err
    payload = {
        "dyn_req": {
            "content": {
                "contents": [{"raw_text": text, "type": 1, "biz_id": ""}],
                # title 恒空：塞全文会在正文>约40字时 4126146（2026-10-29 实测）；
                # 页面不展示该字段，标题由正文首行承担。
                "title": "",
            },
            "scene": 2,
            "attach_card": None,
            "upload_id": upload_id,
            "meta": {"app_meta": {"from": "create.dynamic.web", "mobi_app": "web"}},
            "option": {"aigc": 2},
        }
    }
    status, body = _post_json("https://api.bilibili.com/x/dynamic/feed/create/dyn?" + query, payload)
    data, err = _unwrap(status, body)
    if err:
        return err
    dyn_id = None
    if isinstance(data, dict):
        dyn_id = data.get("dyn_id_str") or data.get("dynamic_id_str") or data.get("dyn_id")
        if dyn_id is None and isinstance(data.get("dyn"), dict):
            dyn_id = data["dyn"].get("id_str") or data["dyn"].get("id")
    if not dyn_id:
        return f"ERROR: 发布响应缺动态 id：{_brief(body, 160)}"
    return f"已发布动态 id={dyn_id}  https://t.bilibili.com/{dyn_id}"


def delete_dynamic(dyn_id: str) -> str:
    """按动态 id 删除文字动态。成功返回确认；失败返回 ERROR 开头原因。"""
    auth_err = _require_auth()
    if auth_err:
        return auth_err
    dyn_id = (dyn_id or "").strip()
    if not re.fullmatch(r"\d{5,25}", dyn_id):
        return "ERROR: dyn_id 应为纯数字动态 id。"
    _, jct = _creds()
    url = "https://api.bilibili.com/x/dynamic/feed/operate/remove?platform=web&csrf=" + urllib.parse.quote(jct)
    status, body = _post_json(url, {"dyn_id_str": dyn_id})
    _, err = _unwrap(status, body)
    if err:
        return err
    return f"已删除动态 id={dyn_id}。"
