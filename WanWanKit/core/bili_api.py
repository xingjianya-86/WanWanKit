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
import os
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

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


def post_dynamic(text: str, images: list[dict[str, object]] | None = None) -> str:
    """发布一条动态（可带图 → 图文动态）。成功返回 id 与链接；失败返回 ERROR 开头原因。

    images 为 upload_dynamic_image 返回的信息列表（最多 9 张）；纯文字传 None。
    """
    auth_err = _require_auth()
    if auth_err:
        return auth_err
    text = (text or "").strip()
    pics = list(images or [])
    if not text and not pics:
        return "ERROR: 动态正文不能为空（或至少带一张图）。"
    if len(pics) > 9:
        return "ERROR: 图文动态最多 9 张图。"
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
    dyn_req: dict[str, object] = {
        "content": {
            "contents": [{"raw_text": text, "type": 1, "biz_id": ""}] if text else [],
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
    if pics:
        # 图片只进 pics 数组（contents 的 type=2 是 @用户，不是图——2026-11-08 查证）
        dyn_req["pics"] = pics
    payload = {"dyn_req": dyn_req}
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
    kind = "图文动态" if pics else "动态"
    return f"已发布{kind} id={dyn_id}  https://t.bilibili.com/{dyn_id}"


def _multipart(
    fields: dict[str, str],
    files: list[tuple[str, str, bytes, str]],
) -> tuple[bytes, str]:
    """手搓 multipart/form-data（urllib 无现成辅助），返回 (body, boundary)。"""
    boundary = "----wanwan" + uuid.uuid4().hex
    chunks: list[bytes] = []
    for k, v in fields.items():
        chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    for name, filename, data, mime in files:
        head = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; '
            f'filename="{filename}"\r\nContent-Type: {mime}\r\n\r\n'
        )
        chunks.append(head.encode("utf-8") + data + b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), boundary


def upload_dynamic_image(data: bytes, filename: str = "wanwan.jpg") -> tuple[dict | None, str]:
    """上传图文动态图片（draw/upload_bfs）。返回 (info, err)。

    info = {"img_src", "img_width", "img_height", "img_size"}——可直接进 dyn_req.pics。
    格式仅 jpg/png/gif；category=daily（日常动态，无 420px 下限）。
    """
    auth_err = _require_auth()
    if auth_err:
        return None, auth_err
    if not data:
        return None, "ERROR: 图片数据为空"
    suffix = (filename or "").rsplit(".", 1)[-1].lower()
    mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "gif": "image/gif"}.get(suffix)
    if mime is None:
        return None, f"ERROR: 图片格式仅支持 jpg/png/gif（收到 .{suffix}）"
    _, jct = _creds()
    body, boundary = _multipart(
        {"category": "daily", "biz": "new_dyn", "csrf": jct},
        [("file_up", filename, data, mime)],
    )
    st, resp, _h = _raw_request(
        "https://api.bilibili.com/x/dynamic/feed/draw/upload_bfs",
        data=body,
        headers={
            "User-Agent": UA,
            "Cookie": _read_token(),
            "Referer": "https://member.bilibili.com/",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
        timeout=60,
    )
    doc, err = _unwrap(st, _b(resp))
    if err:
        return None, err
    url = doc.get("image_url") if isinstance(doc, dict) else None
    if not isinstance(url, str) or not url:
        return None, f"ERROR: 上传成功但响应缺 image_url：{_b(resp)[:200]}"
    return {
        "img_src": url,
        "img_width": int(doc.get("image_width") or 0),
        "img_height": int(doc.get("image_height") or 0),
        "img_size": float(doc.get("img_size") or 0),
    }, ""


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


# ---------------------------------------------------------------- 视频投稿
# 协议移植自 legacy bili_publish.py（2026-10-29 创作中心实测）：
# 文件上传 preupload → init → 分片 PUT → complete；提交 predict 分区 → add/v3。

_UPLOAD_ROOTS = ("/gsuid_core/data/video_out", "/gsuid_core/data/upload_inbox")


def _b(body: bytes) -> str:
    return body.decode("utf-8", "replace")


def _raw_request(
    url: str,
    *,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    method: str | None = None,
    timeout: float = 30.0,
    attempts: int = 3,
) -> tuple[int, bytes, dict[str, str]]:
    """通用请求：返回 (status, body 字节, 响应头小写 dict)。

    连接/超时类抖动重试；HTTP 错误状态原样返回不抛——上传协议靠
    状态码 + body 文本判定（legacy 实测形态）。
    """
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with _open_retry(req, timeout=timeout, attempts=attempts) as resp:
            return resp.status, resp.read(), {k.lower(): v for k, v in resp.headers.items()}
    except urllib.error.HTTPError as exc:
        try:
            err_body = exc.read()
        except OSError:
            err_body = b""
        return exc.code, err_body, {k.lower(): v for k, v in (exc.headers or {}).items()}
    except Exception as exc:  # noqa: BLE001
        return 0, f"{type(exc).__name__}: {exc}".encode("utf-8", "replace"), {}


def resolve_video_path(p: str) -> tuple[str, str]:
    """白名单 realpath 检查：只允许 video_out / upload_inbox 下的视频文件。"""
    p = (p or "").strip()
    if not p:
        return "", "ERROR: 视频路径不能为空"
    rp = os.path.realpath(p)
    if not any(rp == root or rp.startswith(root + "/") for root in _UPLOAD_ROOTS):
        return "", "ERROR: 只允许上传 {} 下的视频".format(" / ".join(_UPLOAD_ROOTS))
    if not os.path.isfile(rp):
        return "", f"ERROR: 文件不存在：{rp}"
    if os.path.getsize(rp) <= 0:
        return "", f"ERROR: 文件为空：{rp}"
    return rp, ""


def upload_video_file(rp: str) -> tuple[str, str, str]:
    """upos 分片上传（preupload → init → PUT 分片 → complete）。

    返回 (filename 无后缀, biz_id/cid, err)——**只传文件，不发布**。
    """
    auth_err = _require_auth()
    if auth_err:
        return "", "", auth_err
    size = os.path.getsize(rp)
    name = os.path.basename(rp)
    base_hdr = {
        "User-Agent": UA,
        "Cookie": _read_token(),
        "Referer": "https://member.bilibili.com/",
    }

    # 1) preupload：拿 endpoint / upos_uri / 分片大小 / biz_id
    q = urllib.parse.urlencode(
        {
            "name": name,
            "r": "upos",
            "profile": "ugcfx/bup",
            "size": size,
            "probe_version": "20221109",
            "ssl": "0",
            "version": "2.14.0.0",
            "build": "2140000",
            "webVersion": "2.13.0",
        }
    )
    st, body, _h = _raw_request("https://member.bilibili.com/preupload?" + q, headers=base_hdr, timeout=30)
    if st != 200:
        return "", "", f"ERROR: preupload HTTP {st}：{_brief(_b(body), 160)}"
    try:
        doc = json.loads(_b(body))
    except ValueError:
        return "", "", f"ERROR: preupload 响应非 JSON：{_brief(_b(body), 200)}"
    if doc.get("OK") != 1:
        return "", "", f"ERROR: preupload 失败：{_brief(json.dumps(doc, ensure_ascii=False), 200)}"
    auth = doc.get("auth") or ""
    endpoint = doc.get("endpoint") or ""
    upos_uri = doc.get("upos_uri") or ""
    chunk_size = int(doc.get("chunk_size") or 10485760)
    biz_id = doc.get("biz_id")
    if not (auth and endpoint and upos_uri):
        return "", "", f"ERROR: preupload 缺关键字段：{_brief(json.dumps(doc, ensure_ascii=False), 220)}"
    base = "https:" + endpoint + "/" + upos_uri.split("://", 1)[1]
    uh = {**base_hdr, "X-Upos-Auth": auth}

    # 2) init：POST ?uploads= 拿 upload_id
    init_q = urllib.parse.urlencode(
        {
            "uploads": "",
            "output": "json",
            "profile": "ugcfx/bup",
            "filesize": size,
            "partsize": chunk_size,
            "biz_id": biz_id,
        }
    )
    st, body, _h = _raw_request(base + "?" + init_q, data=b"", headers=uh, method="POST", timeout=60)
    if st != 200:
        return "", "", f"ERROR: 初始化分片失败 HTTP {st}：{_brief(_b(body), 160)}"
    try:
        doc = json.loads(_b(body))
    except ValueError:
        return "", "", f"ERROR: 初始化响应非 JSON：{_brief(_b(body), 200)}"
    upload_id = doc.get("upload_id") or ""
    if doc.get("OK") != 1 or not upload_id:
        return "", "", f"ERROR: 初始化分片失败：{_brief(json.dumps(doc, ensure_ascii=False), 200)}"

    # 3) 分片 PUT（顺序上传，片级重试一次）
    n_chunks = (size + chunk_size - 1) // chunk_size
    parts: list[dict] = []
    with open(rp, "rb") as fh:
        for i in range(n_chunks):
            chunk = fh.read(chunk_size)
            if not chunk:
                return "", "", f"ERROR: 读文件到第 {i + 1} 片为空（文件大小变化？）"
            start = i * chunk_size
            end = start + len(chunk)
            pq = urllib.parse.urlencode(
                {
                    "partNumber": i + 1,
                    "uploadId": upload_id,
                    "chunk": i,
                    "chunks": n_chunks,
                    "size": len(chunk),
                    "start": start,
                    "end": end,
                    "total": size,
                }
            )
            ph = {**uh, "Content-Type": "application/octet-stream"}
            last_err = ""
            for _attempt in (1, 2):
                st, body, rh = _raw_request(base + "?" + pq, data=chunk, headers=ph, method="PUT", timeout=180)
                if st == 200:
                    break
                last_err = f"HTTP {st} {_brief(_b(body), 120)}"
            else:
                return "", "", f"ERROR: 第 {i + 1}/{n_chunks} 片上传失败（{last_err}）"
            if b"MULTIPART_PUT_SUCCESS" not in body and not body.strip():
                # 部分节点空 body 也算成功（HTTP 200），只拦明确报错
                if b"ERROR" in body.upper() or b"<Error" in body:
                    return "", "", f"ERROR: 第 {i + 1} 片被拒：{_brief(_b(body), 160)}"
            parts.append({"partNumber": i + 1, "eTag": rh.get("etag") or "etag"})

    # 4) complete 合片（POST；文档示例误写 PUT，POST 失败自动 PUT 兜底）
    cq = urllib.parse.urlencode(
        {
            "output": "json",
            "name": name,
            "profile": "ugcfx%2Fbup",
            "uploadId": upload_id,
            "biz_id": biz_id,
        }
    )
    payload = json.dumps({"parts": parts}).encode("utf-8")
    ch = {**uh, "Content-Type": "application/json"}
    st, body, _h = _raw_request(base + "?" + cq, data=payload, headers=ch, method="POST", timeout=60)
    if st != 200:
        st, body, _h = _raw_request(base + "?" + cq, data=payload, headers=ch, method="PUT", timeout=60)
    try:
        doc = json.loads(_b(body))
    except ValueError:
        return "", "", f"ERROR: 合片响应非 JSON（HTTP {st}）：{_brief(_b(body), 200)}"
    if doc.get("OK") != 1:
        return "", "", f"ERROR: 合片失败：{_brief(json.dumps(doc, ensure_ascii=False), 220)}"
    key = doc.get("key") or upos_uri
    filename = os.path.splitext(os.path.basename(str(key)))[0]
    if not filename:
        return "", "", "ERROR: 合片成功但取不到 filename"
    return filename, str(biz_id or ""), ""


def submit_video(
    filename: str,
    cid: str,
    title: str,
    desc: str = "",
    tags: str = "",
    only_self: bool = False,
) -> str:
    """把已上传的视频提交为 B站稿件（predict 分区 → add/v3）。

    成功返回可见性 + BV 链接 + aid；失败返回 ERROR 开头原因。
    """
    filename = (filename or "").strip()
    cid_s = (cid or "").strip()
    title = (title or "").strip()
    if not filename or not re.fullmatch(r"[0-9a-zA-Z_\-]{5,80}", filename):
        return "ERROR: filename 不对（应为上传返回的无后缀文件名）"
    if not cid_s.isdigit():
        return "ERROR: cid 必须是上传返回的数字"
    if not title:
        return "ERROR: title 不能为空"
    if len(title) > 80:
        return f"ERROR: 标题超过 80 字（当前 {len(title)}）"
    desc = (desc or "").strip()
    if len(desc) > 2000:
        return f"ERROR: 简介超过 2000 字（当前 {len(desc)}）"
    tag_str = ",".join(t.strip() for t in (tags or "").split(",") if t.strip()) or "AI视频"
    if len([t for t in tag_str.split(",") if t]) > 10:
        return "ERROR: 标签最多 10 个"
    auth_err = _require_auth()
    if auth_err:
        return auth_err

    _, jct = _creds()
    base_hdr = {
        "User-Agent": UA,
        "Cookie": _read_token(),
        "Referer": "https://member.bilibili.com/",
    }

    # 1) 预测分区（Web 端 tid 只能取 predict 第一个，官方文档口径）
    st, body, _h = _raw_request(
        "https://member.bilibili.com/x/vupre/web/archive/types/predict?csrf=" + jct,
        data=urllib.parse.urlencode({"filename": filename, "title": title}).encode("utf-8"),
        headers={
            **base_hdr,
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        },
        method="POST",
        timeout=30,
    )
    tid = 0
    if st == 200:
        try:
            doc = json.loads(_b(body))
            arr = doc.get("data") or []
            if isinstance(arr, list) and arr:
                tid = int(arr[0].get("id") or 0)
        except (ValueError, TypeError, AttributeError):
            tid = 0
    if tid <= 0:
        return f"ERROR: 预测分区失败（HTTP {st}：{_brief(_b(body), 160)}），稿件未提交，稍后重试"

    # 2) 提交稿件 add/v3（JSON body，csrf 进 query + body）
    ms = str(int(time.time() * 1000))
    payload = {
        "videos": [{"filename": filename, "title": title, "desc": "", "cid": int(cid_s)}],
        "cover43": "",
        "title": title,
        "copyright": 1,
        "tid": tid,
        "tag": tag_str,
        "desc_format_id": 9999,
        "desc": desc,
        "recreate": -1,
        "dynamic": "",
        "interactive": 0,
        "act_reserve_create": 0,
        "no_disturbance": 0,
        "no_reprint": 1,
        "subtitle": {"open": 0, "lan": ""},
        "dolby": 0,
        "lossless_music": 0,
        "up_selection_reply": False,
        "up_close_reply": False,
        "up_close_danmu": False,
        "web_os": 3,
        "is_only_self": 1 if only_self else 0,
        "csrf": jct,
    }
    url = "https://member.bilibili.com/x/vu/web/add/v3?ts=" + ms + "&csrf=" + urllib.parse.quote(jct)
    st, body, _h = _raw_request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={**base_hdr, "Content-Type": "application/json; charset=utf-8"},
        method="POST",
        timeout=60,
    )
    if st != 200:
        return f"ERROR: 投稿接口 HTTP {st}：{_brief(_b(body), 200)}"
    try:
        doc = json.loads(_b(body))
    except ValueError:
        return f"ERROR: 投稿响应非 JSON：{_brief(_b(body), 200)}"
    code = doc.get("code", -1)
    if code != 0:
        msg = doc.get("message", "")
        if code == -111:
            return "ERROR: csrf 校验失败（-111），bili_jct 不对或与 SESSDATA 不匹配"
        if code == -101:
            return "ERROR: 登录态过期（-101），更新 bili_token 里的 SESSDATA"
        if code in (-412, -352):
            return f"ERROR: 被 B站风控拦截（code={code}），稍后再试"
        return f"ERROR: 投稿被拒 code={code}：{_brief(msg, 140)}"
    d = doc.get("data") or {}
    aid = d.get("aid")
    bvid = d.get("bvid") or ""
    vis = "仅自己可见" if only_self else "公开"
    return (
        f"已投稿成功（{vis}）：{bvid or aid}  https://www.bilibili.com/video/{bvid}\n"
        f"aid={aid}（删除/撤回时优先传 aid，审核中的稿件按 BV 查不到）"
    )
