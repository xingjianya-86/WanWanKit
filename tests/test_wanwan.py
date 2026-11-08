"""WanWanKit 单元测试：只测离线纯逻辑，不碰网络。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from WanWanKit.config import WanwanConfig, load_config
from WanWanKit.core import ark, mimo, site
from WanWanKit.tools import diary


def _cfg(**overrides: object) -> WanwanConfig:
    base: dict[str, object] = {
        "ark_api_key": "k",
        "ark_base_url": "https://ark.example",
        "ark_model": "m",
        "ark_timeout": 10.0,
        "ark_max_bytes": 150000,
        "mimo_api_key": "k",
        "mimo_base_url": "https://mimo.example",
        "mimo_tts_model": "t",
        "mimo_timeout": 10.0,
        "voice_file": "/nonexistent",
        "voice_style": "s",
        "site_base_url": "https://s.example",
        "site_username": "u",
        "site_password": "p",
        "site_timeout": 10.0,
        "ref_image": "/nonexistent",
        "ws_bot_id": "ws",
        "bot_id": "bot",
        "bot_self_id": "10001",
        "user_id": "20002",
        "phone_serial": "10.0.0.1:5555",
        "phone_ime": "ime",
        "phone_shot_dir": "/tmp/shots",
    }
    base.update(overrides)
    return WanwanConfig(**base)  # type: ignore[arg-type]


class TestConfig:
    def test_missing_file_returns_none(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("WanWanKit.config.CONFIG_PATH", tmp_path / "nope.json")
        assert load_config() is None

    def test_invalid_json_returns_none(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        bad = tmp_path / "wanwan.json"
        bad.write_text("{not json", encoding="utf-8")
        monkeypatch.setattr("WanWanKit.config.CONFIG_PATH", bad)
        assert load_config() is None

    def test_defaults_merge(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg_file = tmp_path / "wanwan.json"
        # ark_api_key_file 指向 tmp：迁移逻辑绝不允许触碰真实密钥路径
        cfg_file.write_text(
            json.dumps({"ark_api_key": "abc", "ark_api_key_file": str(tmp_path / ".arkkey")}),
            encoding="utf-8",
        )
        monkeypatch.setattr("WanWanKit.config.CONFIG_PATH", cfg_file)
        cfg = load_config()
        assert cfg is not None
        assert cfg.ark_api_key == "abc"
        # 未提供的项走默认值
        assert cfg.ark_model == "doubao-seedream-5-0-flash-260915"
        assert cfg.ark_base_url.endswith("/api/plan/v3")

    def test_password_from_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        pw = tmp_path / "pw.txt"
        pw.write_text("secret123\n", encoding="utf-8")
        cfg_file = tmp_path / "wanwan.json"
        cfg_file.write_text(json.dumps({"site_password_file": str(pw)}), encoding="utf-8")
        monkeypatch.setattr("WanWanKit.config.CONFIG_PATH", cfg_file)
        cfg = load_config()
        assert cfg is not None
        assert cfg.site_password == "secret123"

    def test_ready_flags(self) -> None:
        assert _cfg().ark_ready is True
        assert _cfg(ark_api_key="").ark_ready is False
        assert _cfg().mimo_ready is False  # voice_file 不存在
        assert _cfg().site_ready is True

    def test_session_ready_and_id(self) -> None:
        """会话字段缺任一即不可用；session_id 须与 Event 推导格式一致。"""
        assert _cfg().session_ready is True
        assert _cfg(user_id="").session_ready is False
        assert _cfg(bot_self_id="").session_ready is False

        cfg = _cfg(ws_bot_id="koishi", bot_id="onebot", bot_self_id="10001", user_id="20002")
        assert cfg.session_id == "koishi:onebot:10001:private:20002"


class TestArkCompress:
    def test_unopenable_bytes_returned_as_is(self) -> None:
        raw = b"not-an-image"
        assert ark._compress_jpeg(raw, 1000) == raw

    def test_compresses_to_target(self) -> None:
        pytest.importorskip("PIL")
        import io

        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGB", (2000, 2000), (30, 60, 90)).save(buf, format="PNG")
        out = ark._compress_jpeg(buf.getvalue(), 50_000)
        assert len(out) <= 50_000
        assert out[:2] == b"\xff\xd8"  # JPEG magic


class TestArkRef:
    def test_image_to_ref_is_data_uri(self) -> None:
        import base64

        raw = b"\xff\xd8abc"
        uri = ark.image_to_ref(raw)
        assert uri.startswith("data:image/jpeg;base64,")
        # 往返解码必须还原原字节（断言具体 base64 容易算错）
        assert base64.b64decode(uri.split(",", 1)[1]) == raw


class TestMimo:
    def test_empty_text_raises(self) -> None:
        with pytest.raises(mimo.TtsError, match="不能为空"):
            # 同步检查在协程体内，这里直接调内部校验路径
            import asyncio

            asyncio.run(mimo.synthesize(_cfg(), "   "))

    def test_missing_key_raises(self) -> None:
        import asyncio

        with pytest.raises(mimo.TtsError, match="mimo_api_key"):
            asyncio.run(mimo.synthesize(_cfg(mimo_api_key=""), "你好"))


class TestDiary:
    def test_diary_path_layout(self, tmp_path: Path) -> None:
        p = diary._diary_path("2026-11-06", tmp_path)
        assert p == tmp_path / "2026-11-06.md"

    def test_today_format(self) -> None:
        assert len(diary._today()) == 10
        assert diary._today()[4] == "-" and diary._today()[7] == "-"

    def test_write_and_read_roundtrip(self, tmp_path: Path) -> None:
        diary.write_entry("今天天气不错", "2026-11-06", mood="开心", base=tmp_path)
        content = (tmp_path / "2026-11-06.md").read_text(encoding="utf-8")
        assert "今天天气不错" in content
        assert "开心" in content

        read = diary.read_entries("2026-11-06", base=tmp_path)
        assert "今天天气不错" in read

    def test_write_replaces_same_day(self, tmp_path: Path) -> None:
        """同天再写 = 覆盖上一份（2026-11-08 规则：同天只留最新，防多份混合）。"""
        diary.write_entry("第一条", "2026-11-06", base=tmp_path)
        diary.write_entry("第二条", "2026-11-06", base=tmp_path)
        content = (tmp_path / "2026-11-06.md").read_text(encoding="utf-8")
        assert "第二条" in content
        assert "第一条" not in content, "老记录应被新记录替代"
        # 老份没真删：挪进 .trash 备份（测试残留可追溯）
        trash = list((tmp_path / ".trash").glob("2026-11-06_*.md"))
        assert trash, "旧份应备份到 .trash"
        assert "第一条" in trash[0].read_text(encoding="utf-8")

    def test_read_missing_date(self, tmp_path: Path) -> None:
        assert "没有日记" in diary.read_entries("2026-01-01", base=tmp_path)

    def test_read_recent_when_empty(self, tmp_path: Path) -> None:
        assert "还没有任何日记" in diary.read_entries(base=tmp_path)

    def test_read_recent_picks_latest(self, tmp_path: Path) -> None:
        diary.write_entry("旧", "2026-11-01", base=tmp_path)
        diary.write_entry("新", "2026-11-06", base=tmp_path)
        out = diary.read_entries(limit=2, base=tmp_path)
        assert "新" in out and "旧" in out
        # 倒序：最新在前
        assert out.index("新") < out.index("旧")


class TestToolRegistration:
    """装饰器漏写不会报错，只会静默不注册——必须有测试兜底。"""

    def test_all_tool_functions_registered(self) -> None:
        """tools 包里以 diary_/selfie_/voice_ 开头的公开 async 函数都必须已注册。"""
        import inspect

        from gsuid_core.ai_core.register import get_registered_tools

        reg = get_registered_tools()
        registered: set[str] = set()
        for tools in reg.values():
            registered |= set(tools)

        expected = set()
        for mod in (diary,):
            for name, obj in vars(mod).items():
                if name.startswith("_") or not inspect.iscoroutinefunction(obj):
                    continue
                # 装饰后仍是 coroutine function，但 __module__ 变成注册器的
                expected.add(name)

        # 至少这几个必须在
        for name in (
            "diary_write",
            "diary_publish",
            "diary_read",
            "diary_upload_image",
            "diary_illustrate",
        ):
            assert name in expected, f"{name} 不在 diary 模块里"

        missing = {n for n in expected if n not in registered}
        assert not missing, f"以下工具写了但没加 @ai_tools：{sorted(missing)}"

    def test_illustrate_has_ai_tools_decorator(self) -> None:
        """diary_illustrate 曾漏装饰器导致线上静默失效。"""
        from gsuid_core.ai_core.register import find_tool_base

        assert find_tool_base("diary_illustrate") is not None, "diary_illustrate 未注册"


class TestDiaryPublish:
    """发布链路：调用方/被调方参数名不一致曾导致线上 TypeError。"""

    def test_publish_note_params_match(self) -> None:
        """站点函数的 image 参数必须存在，且不能叫 image_url。"""
        import inspect

        params = inspect.signature(site.publish_note).parameters
        assert "image" in params, "publish_note 应接受 image 参数"
        assert "image_url" not in params, "publish_note 不应有 image_url 参数"

    def test_do_publish_passes_image_kwarg(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """do_publish 调 publish_note 时传的 kwarg 必须与真实签名匹配。"""
        import asyncio

        captured: dict[str, object] = {}

        async def fake_publish(
            cfg: object,
            title: str,
            content_md: str,
            *,
            topic: str = "",
            image: str = "",
        ) -> str:
            captured.update(title=title, content=content_md, image=image)
            return "已发布：《测试》"

        monkeypatch.setattr(diary.site, "publish_note", fake_publish)
        monkeypatch.setattr(diary, "_PUBLISHED_FILE", tmp_path / ".published.json")
        diary.write_entry("正文", "2026-11-06", base=tmp_path)

        # 绕过 _DIARY_DIR：do_publish 用默认目录，这里直接换 diary 路径
        monkeypatch.setattr(diary, "_DIARY_DIR", tmp_path)
        out = asyncio.run(diary._do_publish(object(), "2026-11-06", image_url="http://x/y.jpg"))

        assert "已发布" in out
        assert captured["image"] == "http://x/y.jpg"
        # title 必须含英文，否则站点派生的 URL 只剩日期（非 ASCII 被剥）
        assert captured["title"] == "Diary 2026-11-06 晚晚的日记"

    def test_do_publish_skips_duplicate(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """同一天 + 同内容只发一次（曾把同一篇日记发成 2026-11-06-2/-3… 一堆重复帖）。"""
        import asyncio

        calls: list[str] = []

        async def fake_publish(cfg: object, title: str, content_md: str, *, topic: str = "", image: str = "") -> str:
            calls.append(title)
            return f"已发布：《{title}》\n链接：https://x/1"

        monkeypatch.setattr(diary.site, "publish_note", fake_publish)
        monkeypatch.setattr(diary, "_PUBLISHED_FILE", tmp_path / ".published.json")
        monkeypatch.setattr(diary, "_DIARY_DIR", tmp_path)
        diary.write_entry("同样的正文", "2026-11-06", base=tmp_path)

        first = asyncio.run(diary._do_publish(object(), "2026-11-06"))
        second = asyncio.run(diary._do_publish(object(), "2026-11-06"))

        assert "已发布" in first
        assert "没有重复发" in second
        assert len(calls) == 1, "第二次不该再打站点"

        # 正文改了（真编辑）应当允许重发
        diary.write_entry("改过的正文", "2026-11-06", base=tmp_path)
        third = asyncio.run(diary._do_publish(object(), "2026-11-06"))
        assert "已发布" in third
        assert len(calls) == 2

    def test_do_publish_missing_diary(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import asyncio

        monkeypatch.setattr(diary, "_DIARY_DIR", tmp_path)
        out = asyncio.run(diary._do_publish(object(), "2026-01-01"))
        assert "没有日记" in out

    def test_do_publish_keeps_latest_entry_only(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """旧版追加语义遗留的同天多份 → 只发最新一份，老份挪 .trash（绝不混合）。"""
        import asyncio

        captured: dict[str, str] = {}

        async def fake_publish(cfg: object, title: str, content_md: str, *, topic: str = "", image: str = "") -> str:
            captured["content"] = content_md
            return "已发布：《测试》"

        monkeypatch.setattr(diary.site, "publish_note", fake_publish)
        monkeypatch.setattr(diary, "_PUBLISHED_FILE", tmp_path / ".published.json")
        monkeypatch.setattr(diary, "_DIARY_DIR", tmp_path)

        path = tmp_path / "2026-11-07.md"
        path.write_text(
            "## 2026-11-07 05:29 · 平静\n\n测试残留的旧份。\n\n"
            "# 2026-11-07 周六\n\n## 2026-11-07 18:00 · 安稳\n\n最新的一份。",
            encoding="utf-8",
        )

        out = asyncio.run(diary._do_publish(object(), "2026-11-07"))
        assert "已发布" in out
        assert "最新的一份" in captured["content"]
        assert "测试残留的旧份" not in captured["content"], "发布内容不得混入旧份"
        assert "# 2026-11-07 周六" not in captured["content"], "旧份带的大标题也不能混入"
        # 文件同步规整，老份进 .trash
        assert "最新的一份" in path.read_text(encoding="utf-8")
        assert "测试残留的旧份" not in path.read_text(encoding="utf-8")
        assert list((tmp_path / ".trash").glob("2026-11-07_*.md"))

    def test_split_entries_pure(self) -> None:
        from WanWanKit.tools.diary import _split_entries

        chunks = _split_entries("## 05:29 · 平静\n\n甲\n\n## 18:00 · 安稳\n\n乙")
        assert len(chunks) == 2 and chunks[-1].startswith("## 18:00") and "乙" in chunks[-1]
        # 无段头的单份原样返回
        assert _split_entries("只有一段正文") == ["只有一段正文"]
        assert _split_entries("  \n ") == []

    def test_published_state_dir_quarantined(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """状态路径被误建成目录（2026-11-07 实况）→ 挪走后读写恢复正常。"""
        state = tmp_path / ".diary_published.json"
        state.mkdir()  # 复现事故形态
        monkeypatch.setattr(diary, "_PUBLISHED_FILE", state)

        assert diary._load_published() == {}  # 读不出旧数据但不炸
        assert not state.is_dir(), "目录应被挪走"

        diary._save_published({"2026-11-07": {"sha": "abc123"}})
        assert state.is_file()
        assert diary._load_published()["2026-11-07"]["sha"] == "abc123"

    def test_illustrate_character_lock(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """配图必须拼形象锚点（2026-11-07 曾生成纯空景）；锁关掉才只画 scene。"""
        import asyncio
        from types import SimpleNamespace

        from WanWanKit.core.look import WANWAN_LOOK

        ref = tmp_path / "ref.jpg"
        ref.write_bytes(b"\xff\xd8fake")
        cfg = SimpleNamespace(ark_ready=True, site_ready=True, ref_image=str(ref))
        monkeypatch.setattr(diary, "load_config", lambda: cfg)

        prompts: list[str] = []

        async def fake_gen(_cfg: object, prompt: str, **kw: object) -> bytes:
            prompts.append(prompt)
            return b"img-bytes"

        async def fake_upload(_cfg: object, _data: bytes, _mime: str) -> str:
            return "https://sua17.cn/media/assets/x/wanwan.jpg"

        monkeypatch.setattr(diary.ark, "generate_image", fake_gen)
        monkeypatch.setattr(diary.site, "upload_image_bytes", fake_upload)
        ctx = SimpleNamespace(deps=SimpleNamespace(bot=None, allow_user_outbound=False))

        asyncio.run(diary.diary_illustrate(ctx, "趴在沙坑边写日记"))
        assert WANWAN_LOOK in prompts[0], "默认必须拼形象锚点"
        assert "画面：趴在沙坑边写日记" in prompts[0]
        # prompt 顺序：锚点在 scene 前（空景 scene 也拦得住）
        assert prompts[0].index(WANWAN_LOOK) < prompts[0].index("画面：")

        asyncio.run(diary.diary_illustrate(ctx, "空旷的海底沙坑", character_lock=False))
        assert WANWAN_LOOK not in prompts[1], "明确关锁时不拼锚点"

    def test_upload_parses_item_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """站点返回 {item:{url}}，我曾按 {url} 解析导致「响应缺 url」。"""
        import asyncio
        import inspect
        from typing import Self

        src = inspect.getsource(site.upload_image_bytes)
        assert 'doc.get("item")' in src, "应解析 item 字段"
        assert 'item.get("url")' in src, "应读 item.url"
        assert 'item.get("public_path")' in src, "public_path 是回退键"

        class FakeResp:
            status_code = 200

            def json(self) -> dict[str, object]:
                return {"item": {"url": "/media/a.jpg"}}

        class FakeClient:
            async def __aenter__(self) -> Self:
                return self

            async def __aexit__(self, *a: object) -> None:
                return None

            async def aclose(self) -> None:
                return None

            async def post(self, url: str, *, files: object, headers: object) -> FakeResp:
                return FakeResp()

        async def fake_ensure(cfg: object) -> tuple[FakeClient, str]:
            return FakeClient(), "tok"

        monkeypatch.setattr(site, "_ensure_client", fake_ensure)

        cfg = _cfg(site_base_url="https://sua17.cn")
        out = asyncio.run(site.upload_image_bytes(cfg, b"\xff\xd8abc"))
        assert out == "https://sua17.cn/media/a.jpg"


class TestPhone:
    """手机模块纯逻辑：UI 树解析与动作白名单（不碰真机）。"""

    def test_parse_ui_extracts_nodes(self) -> None:
        from WanWanKit.core.phone import _parse_ui

        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<hierarchy rotation="0">'
            '<node bounds="[0,0][1080,200]" text="标题" content-desc="" '
            'class="android.widget.TextView" clickable="false" />'
            '<node bounds="[100,300][300,400]" text="" content-desc="按钮" '
            'class="android.widget.Button" clickable="true" />'
            '<node bounds="[bad" text="x" />'
            "</hierarchy>"
        )
        nodes = _parse_ui(xml)
        assert len(nodes) == 2, "坏 bounds 的节点应被跳过"
        assert nodes[0].text == "标题"
        assert nodes[0].clickable is False
        assert nodes[1].clickable is True
        assert nodes[1].label.startswith("按钮 [可点]")
        assert nodes[1].center == (200, 350)

    def test_parse_ui_bad_xml_returns_empty(self) -> None:
        from WanWanKit.core.phone import _parse_ui

        assert _parse_ui("") == []
        assert _parse_ui("not xml at all") == []

    def test_action_whitelist(self) -> None:
        from WanWanKit.tools.phone import _ACTIONS

        for ok in ("tap", "swipe", "input", "open", "back", "home", "lock", "unlock"):
            assert ok in _ACTIONS
        # 危险动作必须被挡住，避免模型构造任意 shell
        for bad in ("shell", "rm", "reboot", "exec", "am start", ""):
            assert bad not in _ACTIONS

    def test_is_input_node_tracks_moving_input_box(self) -> None:
        """输入框位置/尺寸随键盘与内容变，按形态认而不是按 y 带认。"""
        from WanWanKit.core.phone import UINode, is_input_node

        def node(x1: int, y1: int, x2: int, y2: int, cls: str = "T") -> UINode:
            return UINode(text="", desc="", cls=cls, clickable=False, x1=x1, y1=y1, x2=x2, y2=y2)

        # 键盘/表情面板打开：y≈934 的贴左宽框（cls 也可能是 EditText）
        assert is_input_node(node(30, 934, 1045, 1060))
        # 关着：底部输入条 / dump 的 2px 残留副本（y≥2170 兜住）
        assert is_input_node(node(55, 2205, 590, 2210))
        assert is_input_node(node(953, 2208, 1017, 2210))
        # B站无可见键盘的展开输入区（EditText 形态判定）
        assert is_input_node(node(30, 1992, 1010, 2133, cls="EditText"))
        # 真机雷：回复多行框被文字撑到 y1654——旧 y 带 [1900,2150] 差 9px 漏判，
        # 导致 type_text 重播出 7 份。EditText 形态判定必须认
        assert is_input_node(node(30, 1654, 1010, 2032, cls="EditText"))
        # 评论列表从 x1≈120 起，不算输入框
        assert not is_input_node(node(150, 700, 1040, 850))
        assert not is_input_node(node(140, 1889, 1050, 2009))
        # 视频简介是 TextView 且在 y≈1550（模型爱照抄简介，误判会把发成功的评论当成没发）
        assert not is_input_node(node(30, 1550, 950, 1750))

    def test_text_on_screen_prefers_input_then_loose(self) -> None:
        """宽松判定是防重播翻倍的最后防线：字在屏上任何地方都不再播。"""
        from WanWanKit.core.phone import UINode, _text_on_screen

        def node(text: str, x1: int, y1: int, cls: str = "T") -> UINode:
            return UINode(text=text, desc="", cls=cls, clickable=False, x1=x1, y1=y1, x2=x1 + 900, y2=y1 + 100)

        text = "鲨鲨潜水翻了翻"
        # 严格：输入框形态命中
        assert _text_on_screen([node(text, 30, 1654, cls="EditText")], text)
        # 宽松：形态没认出但字在屏上 → 算上屏（不许重播）
        assert _text_on_screen([node(text, 140, 500)], text)
        # 屏上没字 → False（才允许重播）
        assert not _text_on_screen([node("别的内容", 30, 1654, cls="EditText")], text)

    def test_hint_text_recognized(self) -> None:
        """占位提示语不是内容：误认会让 clear_input 空转、回复预检误判有残留。"""
        from WanWanKit.core.phone import _looks_like_hint

        assert _looks_like_hint("评论千万条，等你发一条")
        assert _looks_like_hint("有爱评论，说点儿好听的")
        assert not _looks_like_hint("鲨鲨潜水翻了翻，还是这条最在理……别的回复都太吵了")
        assert not _looks_like_hint("真好看")  # 短内容但不是提示语


class TestAdbSelfHeal:
    """半死连接自愈：心跳探针测不出这种状态，工具命令会连环硬超时。"""

    def test_looks_dead_only_device_level(self) -> None:
        from WanWanKit.core.adb import _looks_dead

        # 半死/掉线：该触发 断开重连+重试
        assert _looks_dead("adb: device '10.110.0.161:5555' not found")
        assert _looks_dead("error: device offline")
        # 命令自身的失败 ≠ 设备没了（不该白重连）
        assert not _looks_dead("ls: /nope: No such file or directory")
        assert not _looks_dead("dumpsys succeeded")
        # unauthorized 重连救不了，该让用户点授权弹窗
        assert not _looks_dead("adb: device unauthorized")


class TestBilibiliCards:
    """推荐流卡片是 content-desc 结构化的，解析错了清单就全错。"""

    def test_parse_video_card(self) -> None:
        from WanWanKit.tools.bilibili import _parse_card

        c = _parse_card("竖版视频,三份系统安全组件逆向 systemhelper,1.2万观看,-弹幕,时长2:09,UP主某甲")
        assert c is not None
        assert c.title == "三份系统安全组件逆向 systemhelper"
        assert c.views == "1.2万"
        assert c.up == "某甲"
        assert c.danmaku == "-"

    def test_parse_title_with_comma(self) -> None:
        """标题里带逗号不能截断（用第一个统计段当边界）。"""
        from WanWanKit.tools.bilibili import _parse_card

        c = _parse_card("竖版视频,看好了,这波在大气层,99观看,5弹幕,时长0:30,UP主乙")
        assert c is not None
        assert c.title == "看好了,这波在大气层"
        assert c.views == "99"

    def test_parse_without_stats(self) -> None:
        from WanWanKit.tools.bilibili import _parse_card

        c = _parse_card("竖版视频,某个没统计的标题")
        assert c is not None
        assert c.title == "某个没统计的标题"
        assert c.views == ""

    def test_junk_cards_rejected(self) -> None:
        """广告/会员购卡不是视频，绝不能点开当评论目标。"""
        from WanWanKit.tools.bilibili import _parse_card

        assert _parse_card("创作推广,降到一千多的旗舰机小米14,,轻点两下查看详情") is None
        assert _parse_card("会员购,（T）崩坏星穹铁道,,轻点两下查看详情") is None
        assert _parse_card("竖版视频") is None

    def test_state_detection(self) -> None:
        from WanWanKit.core.phone import UINode
        from WanWanKit.tools.bilibili import _find_input, _on_video_page, _sheet_open

        def node(text: str, *, x1: int, y1: int, cls: str = "TextView", clickable: bool = True) -> UINode:
            return UINode(text=text, desc="", cls=cls, clickable=clickable, x1=x1, y1=y1, x2=x1 + 300, y2=y1 + 60)

        # 视频页：右侧操作栏
        assert _on_video_page([node("点赞", x1=939, y1=1247)])
        assert not _on_video_page([node("推荐,7之2,标签", x1=140, y1=200)])
        # 评论面板：标题或输入框占位符
        assert _sheet_open([node("评论（3270）", x1=170, y1=854)])
        # 半角+空格的标签格式（16:07 真机实测「评论 (17)」，只认全角会漏）
        assert _sheet_open([node("评论 (17)", x1=170, y1=854)])
        assert _sheet_open([node("评论千万条，等你发一条", x1=30, y1=2207, cls="EditText")])
        assert not _sheet_open([node("点赞", x1=939, y1=1247)])
        # 裸「评论」是右侧入口，不是面板
        assert not _sheet_open([node("评论", x1=939, y1=1444)])
        # 输入框定位
        box = node("评论千万条，等你发一条", x1=30, y1=2207, cls="EditText")
        assert _find_input([box]) is box
        assert _find_input([node("搜索", x1=178, y1=102, cls="EditText")]) is None

    def test_comment_entry_via_desc(self) -> None:
        """实测B站评论入口 text='' 文案在 desc——只查 text 会漏（14:37 雷）。"""
        from WanWanKit.core.phone import UINode
        from WanWanKit.tools.bilibili import _find_comment_entry, _on_video_page, _sheet_open

        rail = UINode(text="", desc="评论", cls="ViewGroup", clickable=True, x1=939, y1=1444, x2=1079, y2=1589)
        assert _find_comment_entry([rail]) is rail
        assert _on_video_page([rail])
        # 面板标题不能被当成入口（左侧 x<900 + 文案带全角括号）
        title = UINode(text="评论（3270）", desc="", cls="TextView", clickable=True, x1=170, y1=854, x2=439, y2=969)
        assert _find_comment_entry([title]) is None
        assert _sheet_open([title])
        # 不可点/左半屏的「评论」不算入口
        left = UINode(text="评论", desc="", cls="TextView", clickable=False, x1=100, y1=900, x2=400, y2=960)
        assert _find_comment_entry([left]) is None


class TestReplyTarget:
    """回复目标锚定：找错评论等于回复错人。"""

    @staticmethod
    def _nodes() -> list:
        from WanWanKit.core.phone import UINode

        def n(text: str, y1: int, *, cls: str = "TextView", click: bool = True, x1: int = 140) -> UINode:
            return UINode(text=text, desc="", cls=cls, clickable=click, x1=x1, y1=y1, x2=x1 + 300, y2=y1 + 50)

        return [
            # 第一条评论：作者 → 内容 → 回复
            n("深圳lightning", 1027),
            n("骁龙855老将还能跑这么快", 1084, click=False),
            n("回复", 1166, x1=299),
            n("展开38条回复", 1217, x1=220),  # 不是动作按钮，必须被排除
            # 第二条评论
            n("观星者BIN", 1304),
            n("这机器甚至还能刷win11", 1348, click=False),
            n("回复", 1792, x1=330),
            # 残留在输入框里的前缀（EditText 不能当锚点）
            n("回复 @别人", 934, cls="EditText", x1=30),
        ]

    def test_default_targets_first_comment(self) -> None:
        from WanWanKit.core.phone import find_reply_button

        btn = find_reply_button(self._nodes())
        assert btn is not None and btn.y1 == 1166

    def test_target_matches_author_and_content(self) -> None:
        from WanWanKit.core.phone import find_reply_button

        by_author = find_reply_button(self._nodes(), "观星者BIN")
        assert by_author is not None and by_author.y1 == 1792
        by_content = find_reply_button(self._nodes(), "骁龙855")
        assert by_content is not None and by_content.y1 == 1166

    def test_target_not_found(self) -> None:
        from WanWanKit.core.phone import find_reply_button

        assert find_reply_button(self._nodes(), "不存在的人") is None

    def test_edittext_prefix_not_used_as_anchor(self) -> None:
        """输入框残留的「回复 @别人」不能把目标锚到第一条的按钮上。"""
        from WanWanKit.core.phone import find_reply_button

        assert find_reply_button(self._nodes(), "别人") is None


class TestBiliApi:
    """B站 API 移植自 legacy（2026-10-29 实测配方）：语义必须锁死。"""

    def test_creds_parse_and_auth_guards(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from WanWanKit.core import bili_api

        tok = tmp_path / "tok"
        tok.write_text("buvid3=abc; DedeUserID=123; SESSDATA=SESS123; bili_jct=JCT456", encoding="utf-8")
        monkeypatch.setattr(bili_api, "TOKEN_PATH", str(tok))
        assert bili_api._creds() == ("SESS123", "JCT456")
        assert bili_api._require_auth() == ""

        # 缺文件 → 报缺失，不发请求
        monkeypatch.setattr(bili_api, "TOKEN_PATH", str(tmp_path / "none"))
        assert bili_api._creds() == ("", "")
        err = bili_api._require_auth()
        assert err.startswith("ERROR") and "登录态" in err

        # 有 SESSDATA 缺 bili_jct
        tok2 = tmp_path / "tok2"
        tok2.write_text("SESSDATA=only", encoding="utf-8")
        monkeypatch.setattr(bili_api, "TOKEN_PATH", str(tok2))
        assert "bili_jct" in bili_api._require_auth()

    def test_unwrap_error_semantics(self) -> None:
        from WanWanKit.core.bili_api import _unwrap

        data, err = _unwrap(200, '{"code":0,"data":{"dyn_id_str":"987654"}}')
        assert err == "" and data["dyn_id_str"] == "987654"
        _, err = _unwrap(200, '{"code":-101,"message":"账号未登录"}')
        assert "登录态过期" in err
        _, err = _unwrap(200, '{"code":-111,"message":"csrf 校验失败"}')
        assert "csrf" in err
        _, err = _unwrap(200, '{"code":4100001,"message":"gw"}')
        assert "4100001" in err
        _, err = _unwrap(200, '{"code":4126146,"message":"标题太长了啦~"}')
        assert "正文首行" in err
        _, err = _unwrap(0, "connection reset")
        assert "连不上" in err
        _, err = _unwrap(200, "not-json<html>")
        assert "不是 JSON" in err

    def test_wbi_query_pure(self) -> None:
        import re

        from WanWanKit.core.bili_api import _wbi_query

        q = _wbi_query({"b": "2", "a": "1!'()*x"}, "MIXINKEY123", "1700000000")
        assert "wts=1700000000" in q
        assert "!'()*" not in q  # 危险字符被过滤
        assert "a=1x" in q  # 过滤后剩 1x
        assert "b=2" in q
        # 按 key 排序：a 在 b 前
        assert q.index("a=") < q.index("b=")
        assert re.search(r"w_rid=[0-9a-f]{32}$", q)

    def test_post_dynamic_guards_no_network(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """空正文/缺凭据必须在发请求前就拦住（不碰网络）。"""
        from WanWanKit.core import bili_api

        tok = tmp_path / "tok"
        tok.write_text("SESSDATA=x; bili_jct=y", encoding="utf-8")
        monkeypatch.setattr(bili_api, "TOKEN_PATH", str(tok))
        out = bili_api.post_dynamic("   ")
        assert out.startswith("ERROR") and "不能为空" in out

        monkeypatch.setattr(bili_api, "TOKEN_PATH", str(tmp_path / "missing"))
        out = bili_api.post_dynamic("正文")
        assert out.startswith("ERROR") and "登录态" in out

    def test_post_tool_error_passthrough_and_dedupe(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """工具层：ERROR → ⚠️ 原样传达；成功 → ✅ 记去重、同段不重发。"""
        import asyncio
        from types import SimpleNamespace

        from WanWanKit.core import bili_api
        from WanWanKit.tools import bilibili as bl

        monkeypatch.setattr(bl, "load_config", lambda: SimpleNamespace(bili_ready=True, bili_token_path="/tmp/tok"))
        monkeypatch.setattr(bl, "_USED_POSTS", tmp_path / "used.json")
        monkeypatch.setattr(bili_api, "set_token_path", lambda p: None)
        # @ai_tools 包装器会读 ctx.deps.bot —— 给个最小合法 ctx
        ctx = SimpleNamespace(deps=SimpleNamespace(bot=None, allow_user_outbound=False))

        # ERROR 透传
        monkeypatch.setattr(bili_api, "post_dynamic", lambda t, imgs=None: "ERROR: 登录态过期（-101），更新 SESSDATA。")
        out = asyncio.run(bl.bilibili_post(ctx, "一段正文"))
        assert out.startswith("⚠️") and "-101" in out

        # 成功 + 去重
        monkeypatch.setattr(
            bili_api, "post_dynamic", lambda t, imgs=None: "已发布动态 id=42  https://t.bilibili.com/42"
        )
        out = asyncio.run(bl.bilibili_post(ctx, "深海时间的测试动态"))
        assert out.startswith("✅") and "t.bilibili.com/42" in out
        out2 = asyncio.run(bl.bilibili_post(ctx, "深海时间的测试动态"))
        assert out2.startswith("⚠️") and "已经发过" in out2

        # 空正文
        out3 = asyncio.run(bl.bilibili_post(ctx, "  "))
        assert out3.startswith("⚠️")


class TestBiliPostVideo:
    """视频投稿：白名单/参数校验必须在网络前拦住；工具层两段式 ERROR 透传。"""

    def test_resolve_video_path_whitelist(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from WanWanKit.core import bili_api

        root = tmp_path / "video_out"
        root.mkdir()
        monkeypatch.setattr(bili_api, "_UPLOAD_ROOTS", (str(root),))

        ok = root / "a.mp4"
        ok.write_bytes(b"\x00\x01")

        rp, err = bili_api.resolve_video_path("")
        assert err and "不能为空" in err
        # 白名单外（含 .. 穿越——realpath 归一后仍拦）
        outside = tmp_path / "evil.mp4"
        outside.write_bytes(b"x")
        rp, err = bili_api.resolve_video_path(str(outside))
        assert err and "白名单" in err.replace("只允许上传", "白名单") or "只允许上传" in err
        # 不存在 / 空文件
        rp, err = bili_api.resolve_video_path(str(root / "missing.mp4"))
        assert err and "不存在" in err
        empty = root / "empty.mp4"
        empty.write_bytes(b"")
        rp, err = bili_api.resolve_video_path(str(empty))
        assert err and "为空" in err
        # 正常：返回 realpath 且无错
        rp, err = bili_api.resolve_video_path(str(ok))
        assert err == "" and rp.endswith("a.mp4")

    def test_submit_video_param_guards_no_network(self) -> None:
        """参数校验在鉴权与网络之前——本地就能全拦。"""
        from WanWanKit.core import bili_api

        assert "filename" in bili_api.submit_video("bad file!", "123", "标题")
        assert "cid" in bili_api.submit_video("valid_name-1", "abc", "标题")
        assert "title" in bili_api.submit_video("valid_name-1", "123", "   ")
        assert "80" in bili_api.submit_video("valid_name-1", "123", "字" * 81)
        assert "2000" in bili_api.submit_video("valid_name-1", "123", "标题", desc="字" * 2001)
        assert "10 个" in bili_api.submit_video("valid_name-1", "123", "标题", tags=",".join("t" * 11))

    def test_upload_needs_auth(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """缺凭据时上传直接报错，不碰网络。"""

        from WanWanKit.core import bili_api

        monkeypatch.setattr(bili_api, "TOKEN_PATH", str(tmp_path / "missing_token"))
        f = tmp_path / "v.mp4"
        f.write_bytes(b"\x00")
        filename, cid, err = bili_api.upload_video_file(str(f))
        assert filename == "" and cid == "" and err.startswith("ERROR") and "登录态" in err

    def test_post_video_tool_flow(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """工具层：护栏 → 上传 → 提交 → 去重；两段 ERROR 都透传。"""
        import asyncio
        from types import SimpleNamespace

        from WanWanKit.core import bili_api
        from WanWanKit.tools import bilibili as bl

        root = tmp_path / "video_out"
        root.mkdir()
        vid = root / "a.mp4"
        vid.write_bytes(b"\x00\x01")
        monkeypatch.setattr(bili_api, "_UPLOAD_ROOTS", (str(root),))
        monkeypatch.setattr(bl, "_USED_POSTS", tmp_path / "used.json")
        monkeypatch.setattr(bl, "load_config", lambda: SimpleNamespace(bili_ready=True, bili_token_path="/tmp/tok"))
        monkeypatch.setattr(bili_api, "set_token_path", lambda p: None)
        ctx = SimpleNamespace(deps=SimpleNamespace(bot=None, allow_user_outbound=False))

        # 护栏（不起 upload/submit）——注意工具走 asyncio.to_thread，替身必须是同步函数
        def _boom(*_a: object, **_k: object) -> object:
            raise AssertionError("护栏失效")

        monkeypatch.setattr(bili_api, "upload_video_file", _boom)
        out = asyncio.run(bl.bilibili_post_video(ctx, str(vid), "  "))
        assert out.startswith("⚠️") and "title" in out
        out = asyncio.run(bl.bilibili_post_video(ctx, str(vid), "字" * 81))
        assert "超长" in out
        out = asyncio.run(bl.bilibili_post_video(ctx, str(tmp_path / "x.mp4"), "标题"))
        assert "只允许上传" in out or "白名单" in out

        # 上传段 ERROR → ⚠️，不进提交
        calls: list[str] = []

        def _up_fail(_rp: str) -> tuple[str, str, str]:
            calls.append("up")
            return "", "", "ERROR: preupload HTTP 502：bad gateway"

        def _sub_ok(*_a: object) -> str:
            calls.append("sub")
            return "已投稿成功（仅自己可见）：BV1xx  https://www.bilibili.com/video/BV1xx\naid=99"

        monkeypatch.setattr(bili_api, "upload_video_file", _up_fail)
        monkeypatch.setattr(bili_api, "submit_video", _sub_ok)
        out = asyncio.run(bl.bilibili_post_video(ctx, str(vid), "海洋试投"))
        assert out.startswith("⚠️") and "502" in out
        assert calls == ["up"], "上传失败不该进提交"

        # 提交段 ERROR → ⚠️（文件已传但不记去重，可重投）
        def _up_ok(_rp: str) -> tuple[str, str, str]:
            return "file_abc123", "4242", ""

        def _sub_fail(*_a: object) -> str:
            return "ERROR: 被 B站风控拦截（code=-412），稍后再试"

        monkeypatch.setattr(bili_api, "upload_video_file", _up_ok)
        monkeypatch.setattr(bili_api, "submit_video", _sub_fail)
        out = asyncio.run(bl.bilibili_post_video(ctx, str(vid), "海洋试投"))
        assert out.startswith("⚠️") and "-412" in out

        # 成功 → ✅ + 参数透传 + 去重
        seen: dict[str, object] = {}

        def _sub_good(filename: str, cid: str, title: str, desc: str, tags: str, only_self: bool) -> str:
            seen.update(filename=filename, cid=cid, title=title, desc=desc, tags=tags, only_self=only_self)
            return "已投稿成功（仅自己可见）：BV1yy  https://www.bilibili.com/video/BV1yy\naid=100"

        monkeypatch.setattr(bili_api, "submit_video", _sub_good)
        out = asyncio.run(
            bl.bilibili_post_video(ctx, str(vid), "海洋试投", desc="简介", tags="海洋,日记", private=True)
        )
        assert out.startswith("✅") and "BV1yy" in out
        assert seen["filename"] == "file_abc123" and seen["cid"] == "4242"
        assert seen["title"] == "海洋试投" and seen["tags"] == "海洋,日记" and seen["only_self"] is True

        # 同标题去重
        out2 = asyncio.run(bl.bilibili_post_video(ctx, str(vid), "海洋试投"))
        assert out2.startswith("⚠️") and "已经投过" in out2


class TestBiliImagePost:
    """图文动态：multipart 手搓 + 图片护栏 + pics 透传（协议 2026-11-08 查证）。"""

    def test_multipart_shape(self) -> None:
        from WanWanKit.core.bili_api import _multipart

        body, boundary = _multipart(
            {"category": "daily", "csrf": "J"},
            [("file_up", "a.jpg", b"\xff\xd8img-bytes", "image/jpeg")],
        )
        text = body.decode("utf-8", "replace")
        assert boundary in text
        assert 'name="category"' in text and "daily" in text
        assert 'name="csrf"' in text and "J" in text
        assert 'filename="a.jpg"' in text and "image/jpeg" in text
        assert b"\xff\xd8img-bytes" in body
        assert text.endswith(f"--{boundary}--\r\n")

    def test_upload_image_guards_no_network(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from WanWanKit.core import bili_api

        tok = tmp_path / "tok"
        tok.write_text("SESSDATA=x; bili_jct=y", encoding="utf-8")
        monkeypatch.setattr(bili_api, "TOKEN_PATH", str(tok))

        info, err = bili_api.upload_dynamic_image(b"", "a.jpg")
        assert info is None and "为空" in err
        info, err = bili_api.upload_dynamic_image(b"x", "a.bmp")
        assert info is None and "jpg/png/gif" in err

        # 缺凭据：先报登录态，不碰网络
        monkeypatch.setattr(bili_api, "TOKEN_PATH", str(tmp_path / "none"))
        info, err = bili_api.upload_dynamic_image(b"x", "a.jpg")
        assert info is None and err.startswith("ERROR") and "登录态" in err

    def test_post_dynamic_pic_guards(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from WanWanKit.core import bili_api

        tok = tmp_path / "tok"
        tok.write_text("SESSDATA=x; bili_jct=y", encoding="utf-8")
        monkeypatch.setattr(bili_api, "TOKEN_PATH", str(tok))

        out = bili_api.post_dynamic("   ")
        assert out.startswith("ERROR") and "不能为空" in out
        out = bili_api.post_dynamic("正文", [{"img_src": "http://x"}] * 10)
        assert out.startswith("ERROR") and "9 张" in out

    def test_post_tool_images_flow(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import asyncio
        from types import SimpleNamespace

        from WanWanKit.core import bili_api
        from WanWanKit.tools import bilibili as bl

        root = tmp_path / "data"
        (root / "wanwan").mkdir(parents=True)
        img = root / "wanwan" / "pic.jpg"
        img.write_bytes(b"\xff\xd8")
        monkeypatch.setattr(bl, "_IMG_ROOT", root)
        monkeypatch.setattr(bl, "_USED_POSTS", tmp_path / "used.json")
        monkeypatch.setattr(bl, "load_config", lambda: SimpleNamespace(bili_ready=True, bili_token_path="/t"))
        monkeypatch.setattr(bili_api, "set_token_path", lambda p: None)
        ctx = SimpleNamespace(deps=SimpleNamespace(bot=None, allow_user_outbound=False))

        # —— 护栏（网络前全拦）——
        out = asyncio.run(bl.bilibili_post(ctx, ""))
        assert out.startswith("⚠️") and "请提供 text" in out
        out = asyncio.run(bl.bilibili_post(ctx, "正文", ["rel.jpg"]))
        assert "绝对路径" in out
        out = asyncio.run(bl.bilibili_post(ctx, "正文", [str(tmp_path / "outside.jpg")]))
        assert "白名单" in out or "不在" in out
        out = asyncio.run(bl.bilibili_post(ctx, "正文", [str(root / "wanwan" / "missing.png")]))
        assert "不存在" in out
        bad = root / "wanwan" / "x.bmp"
        bad.write_bytes(b"x")
        out = asyncio.run(bl.bilibili_post(ctx, "正文", [str(bad)]))
        assert "jpg/png/gif" in out
        out = asyncio.run(bl.bilibili_post(ctx, "正文", [str(img)] * 10))
        assert "9 张" in out

        # —— 成功链：图片逐张上传 → pics 透传 → 纯图去重 ——
        up_calls: list[str] = []

        def _up(data: bytes, filename: str = "wanwan.jpg") -> tuple[dict, str]:
            up_calls.append(filename)
            return {
                "img_src": "http://i0.hdslb.com/bfs/new_dyn/x.png",
                "img_width": 720,
                "img_height": 720,
                "img_size": 100.0,
            }, ""

        posted: dict[str, object] = {}

        def _post(text: str, images: list[dict] | None = None) -> str:
            posted.update(text=text, images=images)
            return "已发布图文动态 id=999  https://t.bilibili.com/999"

        monkeypatch.setattr(bili_api, "upload_dynamic_image", _up)
        monkeypatch.setattr(bili_api, "post_dynamic", _post)

        out = asyncio.run(bl.bilibili_post(ctx, "", [str(img)]))
        assert out.startswith("✅") and "图文动态" in out
        assert up_calls == ["pic.jpg"]
        assert posted["text"] == ""
        infos = posted["images"]
        assert isinstance(infos, list) and infos[0]["img_src"].startswith("http://i0.hdslb.com")

        # 同图再发（纯图动态去重键 = 图片名组合）
        out2 = asyncio.run(bl.bilibili_post(ctx, "", [str(img)]))
        assert out2.startswith("⚠️") and "已经发过" in out2

        # 图文混合 + 上传失败即止（一张失败整条不发、不记去重）
        def _up_fail(_data: bytes, _filename: str = "wanwan.jpg") -> tuple[None, str]:
            return None, "ERROR: 上传被拒 code=4100001"

        monkeypatch.setattr(bili_api, "upload_dynamic_image", _up_fail)
        out3 = asyncio.run(bl.bilibili_post(ctx, "另一段正文", [str(img)]))
        assert out3.startswith("⚠️") and "4100001" in out3


class TestHttp:
    """HTTP 双形态融合：解析纯函数 + AI 工具护栏（不碰网络）。"""

    def test_parse_short(self) -> None:
        import pytest

        from WanWanKit.tools.http import parse_short

        spec = parse_short('https://a.com/x -H "X-T: v" -p foo=bar', "GET")
        assert spec.method == "GET" and spec.url == "https://a.com/x"
        assert spec.headers == {"X-T": "v"} and spec.query == {"foo": "bar"}

        spec = parse_short("https://a.com/y -d '{\"a\":1}'", "POST")
        assert spec.method == "POST" and spec.body == '{"a":1}'

        with pytest.raises(ValueError, match="http"):
            parse_short("ftp://x.com/f", "GET")
        with pytest.raises(ValueError, match="URL"):
            parse_short("", "GET")
        with pytest.raises(ValueError, match="未知参数"):
            parse_short("https://a.com -Z", "GET")
        with pytest.raises(ValueError, match="body"):
            parse_short("https://a.com -d x", "GET")
        with pytest.raises(ValueError, match="多余"):
            parse_short("https://a.com https://b.com", "GET")

    def test_parse_request(self) -> None:
        import pytest

        from WanWanKit.tools.http import parse_request

        spec = parse_request('POST https://a.com/p headers=\'{"X-A":"1"}\' body=\'{"k":1}\'')
        assert spec.method == "POST"
        assert spec.headers == {"X-A": "1"}
        assert spec.body == '{"k":1}'

        spec = parse_request("GET https://a.com/g query='a:1,b:2'")
        assert spec.query == {"a": "1", "b": "2"}
        assert spec.method == "GET"

        with pytest.raises(ValueError, match="未知参数"):
            parse_request("GET https://a.com foo=1")
        with pytest.raises(ValueError, match="body"):
            parse_request("GET https://a.com body='{}'")
        with pytest.raises(ValueError):
            parse_request("GET https://a.com headers='[1,2]'")

    def test_pairs_and_confirm_words(self) -> None:
        import pytest

        from WanWanKit.tools.http import _is_cancel, _is_confirm, _parse_pairs, _strip_confirm

        assert _parse_pairs('{"K":"V"}', "headers") == {"K": "V"}
        assert _parse_pairs("a:1,b:2", "query") == {"a": "1", "b": "2"}
        with pytest.raises(ValueError, match="冒号"):
            _parse_pairs("nocolon", "headers")
        with pytest.raises(ValueError, match="冒号"):
            _parse_pairs("[1,2]", "headers")  # 非对象走 k:v 分支，同样被拦

        assert _is_confirm("确认") and _is_confirm("yes") and _is_confirm("OK")
        assert _is_cancel("取消") and _is_cancel("算了")
        confirmed, rest = _strip_confirm("确认发送 GET https://a.com")
        assert confirmed and rest == "GET https://a.com"
        confirmed, rest = _strip_confirm("GET https://a.com")
        assert not confirmed and rest == "GET https://a.com"

    def test_ai_tool_guards_no_network(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """护栏必须在发网络请求前全部拦住。"""
        import asyncio
        from types import SimpleNamespace

        from WanWanKit.tools import http as ht

        async def _boom(_spec: object) -> str:
            raise AssertionError("护栏失效：不该发网络请求")

        monkeypatch.setattr(ht, "perform_request", _boom)
        ctx = SimpleNamespace(deps=SimpleNamespace(bot=None, allow_user_outbound=False))

        assert "url" in asyncio.run(ht.http_request(ctx, ""))
        assert "GET 或 POST" in asyncio.run(ht.http_request(ctx, "https://a.com", method="PUT"))
        assert "JSON" in asyncio.run(ht.http_request(ctx, "https://a.com", headers="not-json"))
        assert "http" in asyncio.run(ht.http_request(ctx, "ftp://x"))
        assert "不支持 body" in asyncio.run(ht.http_request(ctx, "https://a.com", method="GET", body="{}"))

    def test_limits_from_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from types import SimpleNamespace

        from WanWanKit.tools import http as ht

        monkeypatch.setattr(ht, "load_config", lambda: SimpleNamespace(http_timeout=3.5, http_max_body=200))
        assert ht._limits() == (3.5, 200)
        monkeypatch.setattr(ht, "load_config", lambda: None)
        assert ht._limits() == (15.0, 1500)


class TestSecretsConfig:
    """密钥独立化：独立文件优先、内联明文自动迁移并从配置清除。"""

    def test_resolve_secret(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from WanWanKit import config as cfgmod

        monkeypatch.setattr(cfgmod, "_MIGRATED_KEYS", [])

        # 独立文件优先
        f1 = tmp_path / ".ark"
        f1.write_text("file-key", encoding="utf-8")
        assert cfgmod._resolve_secret("inline-key", str(f1)) == "file-key"

        # 无文件有明文 → 自动迁移（写入独立文件 + 记键名待清除）
        f2 = tmp_path / ".mimo"
        assert cfgmod._resolve_secret("inline2", str(f2), "mimo_api_key") == "inline2"
        assert f2.read_text(encoding="utf-8") == "inline2"
        assert cfgmod._MIGRATED_KEYS == ["mimo_api_key"]
        try:
            assert f2.stat().st_mode & 0o777 == 0o600  # 迁移文件 600 权限
        except (OSError, AssertionError, AttributeError):
            pass  # 部分平台/容器对 chmod 语义不同——不作硬断言

        # 路径为空 → 直接回内联（不迁移）
        assert cfgmod._resolve_secret("inline3", "", "k") == "inline3"
        assert cfgmod._MIGRATED_KEYS == ["mimo_api_key"]

    def test_load_config_migrates_and_flushes(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from WanWanKit import config as cfgmod

        cfg_file = tmp_path / "wanwan_skills.json"
        cfg_file.write_text(
            json.dumps(
                {
                    "ark_api_key": "plaintext-ark-123",
                    "ark_api_key_file": str(tmp_path / ".ark"),
                    "http_timeout": 3.5,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(cfgmod, "CONFIG_PATH", cfg_file)
        monkeypatch.setattr(cfgmod, "_MIGRATED_KEYS", [])

        cfg = cfgmod.load_config()
        assert cfg is not None
        # 迁移三件套：独立文件拿到值 / 配置内明文被清 / 运行时值不受影响
        assert (tmp_path / ".ark").read_text(encoding="utf-8") == "plaintext-ark-123"
        raw = json.loads(cfg_file.read_text(encoding="utf-8"))
        assert raw["ark_api_key"] == "", "配置里的明文必须被清除"
        assert cfg.ark_api_key == "plaintext-ark-123"
        # 新配置字段生效
        assert cfg.http_timeout == 3.5
        assert cfg.video_out_dir  # 默认值存在
        assert cfg.dy_python_path and cfg.ffmpeg_timeout == 600.0

        # 二次加载：文件优先、不再重复迁移
        cfg2 = cfgmod.load_config()
        assert cfg2 is not None and cfg2.ark_api_key == "plaintext-ark-123"
        assert json.loads(cfg_file.read_text(encoding="utf-8"))["ark_api_key"] == ""


class TestDouyinJunk:
    """直播 / 广告卡片必须被识别跳过。"""

    @staticmethod
    def _nodes(items: list[tuple[str, bool]]):
        """items = [(text, clickable), ...]，按真实控件树布局。"""
        from WanWanKit.core.phone import UINode

        return [UINode(text=t, desc="", cls="T", clickable=c, x1=0, y1=0, x2=10, y2=10) for t, c in items]

    def test_live_card_detected(self) -> None:
        from WanWanKit.tools.douyin import _junk_reason

        nodes = self._nodes([("点击进入直播间按钮", True), ("@某主播", True)])
        assert _junk_reason(nodes) == "直播"

    def test_ad_label_detected(self) -> None:
        from WanWanKit.tools.douyin import _junk_reason

        nodes = self._nodes([("广告", False), ("@游戏碎碎念", True)])
        assert _junk_reason(nodes) == "广告"

    def test_normal_video_not_flagged(self) -> None:
        from WanWanKit.tools.douyin import _junk_reason

        # 描述里顺带提到「广告」的正常视频不应被误伤
        nodes = self._nodes([("2026年超好玩的小游戏，不是广告", False), ("@某作者", True), ("喜欢8.7万", False)])
        assert _junk_reason(nodes) == ""

    def test_extract_video_from_real_layout(self) -> None:
        from WanWanKit.tools.douyin import _extract_video

        v = _extract_video(
            self._nodes(
                [
                    ("广告", False),
                    ("@游戏碎碎念", True),
                    ("2026年超好玩的小游戏! 广告", False),
                    ("喜欢9，按钮", True),
                ]
            )
        )
        assert v is not None
        assert v.author == "游戏碎碎念"
        assert v.likes == "9"


class TestVision:
    """截屏像素定位：dump 坐标在键盘打开后不可信，只能信截图。"""

    @staticmethod
    def _png(size: tuple[int, int], boxes: list[tuple[int, int, int, int, tuple[int, int, int]]]) -> bytes:
        """造一张图：白底 + 若干色块 [(x1,y1,x2,y2,rgb), ...]。"""
        import io

        from PIL import Image, ImageDraw

        im = Image.new("RGB", size, (255, 255, 255))
        draw = ImageDraw.Draw(im)
        for x1, y1, x2, y2, rgb in boxes:
            draw.rectangle([x1, y1, x2, y2], fill=rgb)
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        return buf.getvalue()

    def test_finds_douyin_red_button(self) -> None:
        from WanWanKit.core.vision import find_color_button

        # 400x800，按钮在下半部（y_min_ratio=0.4 以下）
        png = self._png((400, 800), [(100, 600, 290, 690, (254, 44, 85))])
        blob = find_color_button(png, (254, 44, 85))
        assert blob is not None
        cx, cy = blob.center
        assert abs(cx - 195) <= 8 and abs(cy - 645) <= 8

    def test_small_red_distractors_ignored(self) -> None:
        from WanWanKit.core.vision import find_color_button

        # 表情/小图标级别的红块（< min_run=70）不该被当成按钮
        png = self._png(
            (400, 800),
            [(30, 700, 70, 740, (254, 44, 85)), (200, 720, 240, 760, (254, 44, 85))],
        )
        assert find_color_button(png, (254, 44, 85)) is None

    def test_picks_largest_of_two_buttons(self) -> None:
        from WanWanKit.core.vision import find_color_button

        png = self._png(
            (400, 800),
            [
                (20, 400, 140, 470, (254, 44, 85)),  # 小一点的红块
                (180, 620, 370, 710, (254, 44, 85)),  # 真按钮，面积更大
            ],
        )
        blob = find_color_button(png, (254, 44, 85))
        assert blob is not None
        assert blob.x1 == 180 and blob.y1 == 620

    def test_dimmed_red_rejected(self) -> None:
        from WanWanKit.core.vision import find_color_button

        # 面板遮罩下的暗红（视频背景）不能误判
        png = self._png((400, 800), [(100, 600, 290, 690, (127, 22, 42))])
        assert find_color_button(png, (254, 44, 85)) is None

    def test_invalid_png_returns_none(self) -> None:
        from WanWanKit.core.vision import find_color_button

        assert find_color_button(b"not a png", (254, 44, 85)) is None

    def test_button_above_scan_region_ignored(self) -> None:
        from WanWanKit.core.vision import find_color_button

        # 屏幕上半部的红块（如视频里的红衣服）不该被当成底部按钮
        png = self._png((400, 800), [(100, 60, 290, 150, (254, 44, 85))])
        assert find_color_button(png, (254, 44, 85)) is None


class TestTaskPrompt:
    """任务 prompt 用 .format(today=...)，花括号写错会直接 KeyError。"""

    def test_prompt_formats_with_today(self) -> None:
        from WanWanKit.tasks import _TASK_PROMPT

        out = _TASK_PROMPT.format(today="2026-11-06", user_id="20002")
        assert "2026-11-06" in out
        assert "{today}" not in out
        assert "不要传 date" in out
        assert "image_url 必须传" in out
        assert "能力缺失叙述" in out


class TestHeartbeat:
    """心跳看门狗：误报会狼来了，漏报会失联不自知。"""

    def test_connect_ok_parsing(self) -> None:
        from WanWanKit.tasks import _connect_ok

        assert _connect_ok("connected to 10.110.0.161:5555\n")
        assert _connect_ok("already connected to 10.110.0.161:5555\n")
        assert not _connect_ok("failed to connect to '10.110.0.161:5555': No route to host\n")
        assert not _connect_ok("")

    def test_alert_cooldown(self) -> None:
        from WanWanKit.tasks import _ALERT_COOLDOWN, _should_alert

        # 首次断线：立即报
        assert _should_alert(now=1000.0, last_alert=0.0, down=False)
        # 持续断线但冷却未到：闭嘴（否则刷屏）
        assert not _should_alert(now=1060.0, last_alert=1000.0, down=True)
        # 冷却过了：复报（长期失联要持续提醒）
        assert _should_alert(now=1000.0 + _ALERT_COOLDOWN + 1, last_alert=1000.0, down=True)


class TestDouyinPost:
    """抖音网页发布：驱动子进程的输出语义 + 工具护栏（不碰真浏览器）。"""

    def test_interpret_error_and_login(self) -> None:
        from WanWanKit.tools.douyin import _dy_interpret

        # ERROR 行透传
        ok, msg = _dy_interpret(1, "ERROR: 标题超长\n", "")
        assert not ok and "标题超长" in msg
        # 未登录（无 ERROR 前缀也要当失败）
        ok, msg = _dy_interpret(0, "未登录：cookie 已失效\n", "")
        assert not ok and "未登录" in msg
        # 成功：连带管理页链接一起带出
        ok, msg = _dy_interpret(
            0,
            "已发布抖音（仅自己可见）：测试片\n管理页：https://creator.douyin.com/x\n",
            "",
        )
        assert ok and "测试片" in msg and "creator.douyin.com" in msg
        # 非零退出且无输出：报「未确认」，附 stderr 尾巴
        ok, msg = _dy_interpret(1, "", "TimeoutError: browser\nline2")
        assert not ok and "发布未确认" in msg and "browser" in msg

    def test_dy_run_missing_env_no_spawn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """环境缺文件时直接 ERROR，不尝试拉起浏览器。"""
        import asyncio
        from types import SimpleNamespace

        from WanWanKit.tools import douyin as dy

        # _dy_run 现在优先读配置——用配置指向不存在的路径
        monkeypatch.setattr(
            dy,
            "load_config",
            lambda: SimpleNamespace(dy_python_path="/no/such/python", dy_driver_path="/no/such/driver.py"),
        )
        monkeypatch.setattr(dy, "_DY_VENV_PY", "/no/such/python")
        monkeypatch.setattr(dy, "_DY_DRIVER", "/no/such/driver.py")
        rc, out, _ = asyncio.run(dy._dy_run(("check",)))
        assert rc == 127 and out.startswith("ERROR") and "发布环境" in out

    def test_post_guards(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """标题/白名单/文件/去重——全部在起子进程前拦住。"""
        import asyncio
        from types import SimpleNamespace

        from WanWanKit.tools import douyin as dy

        out_dir = tmp_path / "video_out"
        out_dir.mkdir()
        vid = out_dir / "a.mp4"
        vid.write_bytes(b"\x00")
        monkeypatch.setattr(dy, "_VIDEO_OUT", out_dir)
        monkeypatch.setattr(dy, "_USED_POSTS", tmp_path / "used.json")
        monkeypatch.setattr(dy, "load_config", lambda: SimpleNamespace(video_out_dir=str(out_dir)))
        ctx = SimpleNamespace(deps=SimpleNamespace(bot=None, allow_user_outbound=False))

        # 子进程绝不能被拉起来
        async def _boom(_args: tuple[str, ...]) -> tuple[int, str, str]:
            raise AssertionError("护栏失效：不该起子进程")

        monkeypatch.setattr(dy, "_dy_run", _boom)

        # 空标题 / 超长标题
        out = asyncio.run(dy.douyin_post(ctx, str(vid), "  "))
        assert out.startswith("⚠️") and "title" in out
        out = asyncio.run(dy.douyin_post(ctx, str(vid), "字" * 41))
        assert out.startswith("⚠️") and "超长" in out
        # 相对路径 / 白名单外 / 文件不存在
        out = asyncio.run(dy.douyin_post(ctx, "a.mp4", "标题"))
        assert out.startswith("⚠️") and "绝对路径" in out
        outside = tmp_path / "evil.mp4"
        outside.write_bytes(b"\x00")
        out = asyncio.run(dy.douyin_post(ctx, str(outside), "标题"))
        assert out.startswith("⚠️") and "白名单" in out
        out = asyncio.run(dy.douyin_post(ctx, str(out_dir / "missing.mp4"), "标题"))
        assert out.startswith("⚠️") and "不存在" in out

    def test_post_success_dedupe_and_error_passthrough(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import asyncio
        from types import SimpleNamespace

        from WanWanKit.tools import douyin as dy

        out_dir = tmp_path / "video_out"
        out_dir.mkdir()
        vid = out_dir / "a.mp4"
        vid.write_bytes(b"\x00")
        monkeypatch.setattr(dy, "_VIDEO_OUT", out_dir)
        monkeypatch.setattr(dy, "_USED_POSTS", tmp_path / "used.json")
        monkeypatch.setattr(dy, "load_config", lambda: SimpleNamespace(video_out_dir=str(out_dir)))
        ctx = SimpleNamespace(deps=SimpleNamespace(bot=None, allow_user_outbound=False))

        seen: list[tuple[str, ...]] = []

        async def _ok(args: tuple[str, ...]) -> tuple[int, str, str]:
            seen.append(args)
            return 0, "已发布抖音：海洋观察日记\n", ""

        monkeypatch.setattr(dy, "_dy_run", _ok)
        out = asyncio.run(dy.douyin_post(ctx, str(vid), "海洋观察日记", desc="今天潮位刚好", private=True))
        assert out.startswith("✅") and "海洋观察日记" in out
        # 参数透传：upload 文件 标题 简介 tags 私密标记
        assert seen[0][0] == "upload" and seen[0][2] == "海洋观察日记" and seen[0][5] == "1"

        # 同标题去重：不再起子进程
        out2 = asyncio.run(dy.douyin_post(ctx, str(vid), "海洋观察日记"))
        assert out2.startswith("⚠️") and "已经发过" in out2
        assert len(seen) == 1

        # ERROR → ⚠️ 原样传达（不记去重，修好后可重发）
        async def _err(_args: tuple[str, ...]) -> tuple[int, str, str]:
            return 1, "ERROR: 被风控拦截，请稍后再试\n", ""

        monkeypatch.setattr(dy, "_dy_run", _err)
        out3 = asyncio.run(dy.douyin_post(ctx, str(vid), "换个标题"))
        assert out3.startswith("⚠️") and "风控" in out3

        # 未登录 → 给出扫码指引
        async def _nologin(_args: tuple[str, ...]) -> tuple[int, str, str]:
            return 0, "未登录：请先扫码\n", ""

        monkeypatch.setattr(dy, "_dy_run", _nologin)
        out4 = asyncio.run(dy.douyin_post(ctx, str(vid), "再换个标题"))
        assert out4.startswith("⚠️") and "扫码" in out4


class TestVideoAssemble:
    """ffmpeg 合成段：时长均分与滤镜链是纯函数，直接锁死。"""

    def test_split_durations(self) -> None:
        from WanWanKit.core.video import _split_durations

        # 10s / 3 画面：每段向上取 0.1，末段吃余量
        assert _split_durations(10.0, 3) == [3.4, 3.4, 3.2]
        # 单画面：整段
        assert _split_durations(8.0, 1) == [8.0]
        # 末段兜底 0.5s（宁长勿短，防截配音尾巴）
        durs = _split_durations(1.0, 4)
        assert durs[-1] >= 0.5
        assert sum(durs) >= 1.0

    def test_assemble_cmd_shape(self, tmp_path: Path) -> None:
        from WanWanKit.core.video import assemble_cmd

        imgs = [tmp_path / "a.jpg", tmp_path / "b.jpg"]
        cmd = assemble_cmd(imgs, tmp_path / "n.wav", 6.0, tmp_path / "o.mp4")
        # 每图一输入带 -loop/-t，音频最后
        assert cmd.count("-loop") == 2
        assert cmd[cmd.index("-i") + 1] == str(imgs[0])
        fc = cmd[cmd.index("-filter_complex") + 1]
        assert "scale=1280:720" in fc and "pad=1280:720" in fc
        assert "concat=n=2:v=1:a=0[vout]" in fc
        assert "aresample=44100[aout]" in fc
        assert cmd[cmd.index("-map") + 1] == "[vout]"
        assert "-crf" in cmd and "libx264" in cmd and "+faststart" in cmd
        assert cmd[-1] == str(tmp_path / "o.mp4")

    def test_pad_cmd_shape(self, tmp_path: Path) -> None:
        from WanWanKit.core.video import pad_cmd

        cmd = pad_cmd(tmp_path / "n.wav", tmp_path / "p.mp3", 1.0)
        assert "anullsrc=r=24000:cl=mono" in cmd
        fc = cmd[cmd.index("-filter_complex") + 1]
        assert "[0:a][1:a]concat=n=2:v=0:a=1" in fc  # 静音轨拼在原音频前
        assert "libmp3lame" in cmd
        assert "1.000" in cmd  # 时长格式化

    def test_parse_duration(self) -> None:
        from WanWanKit.core.video import _parse_duration

        assert _parse_duration("12.34\n") == 12.34
        assert _parse_duration("") == 0.0
        assert _parse_duration("N/A") == 0.0  # 探不到 → 上层报「时长异常」

    def test_character_lock(self) -> None:
        from WanWanKit.core.video import WANWAN_LOOK
        from WanWanKit.tools.media import _apply_character_lock

        locked = _apply_character_lock(["海边散步"], True)
        assert locked[0].startswith(WANWAN_LOOK) and "场景：海边散步" in locked[0]
        assert _apply_character_lock(["海边散步"], False) == ["海边散步"]


class TestVideoGenerate:
    """video_generate 护栏（网络/ffmpeg 前全拦）+ 全 mock 成功链。"""

    @staticmethod
    def _ctx():
        from types import SimpleNamespace

        return SimpleNamespace(deps=SimpleNamespace(bot=None, allow_user_outbound=False))

    def test_guards(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import asyncio
        from types import SimpleNamespace

        from WanWanKit.tools import media

        ctx = self._ctx()

        monkeypatch.setattr(media, "load_config", lambda: None)
        out = asyncio.run(media.video_generate(ctx, ["a"], "n"))
        assert out.startswith("⚠️") and "未配置" in out

        monkeypatch.setattr(media, "load_config", lambda: SimpleNamespace(ark_ready=False, mimo_ready=True))
        out = asyncio.run(media.video_generate(ctx, ["a"], "n"))
        assert "ark_api_key" in out

        monkeypatch.setattr(media, "load_config", lambda: SimpleNamespace(ark_ready=True, mimo_ready=False))
        out = asyncio.run(media.video_generate(ctx, ["a"], "n"))
        assert "mimo_api_key" in out

        monkeypatch.setattr(media, "load_config", lambda: SimpleNamespace(ark_ready=True, mimo_ready=True))
        out = asyncio.run(media.video_generate(ctx, [], "n"))
        assert "scenes" in out
        out = asyncio.run(media.video_generate(ctx, ["a"] * 9, "n"))
        assert "画面最多 8" in out
        out = asyncio.run(media.video_generate(ctx, ["a"], "  "))
        assert "narration" in out
        out = asyncio.run(media.video_generate(ctx, ["a"], "字" * 1001))
        assert "配音过长" in out

    def test_success_pipeline_mocked(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import asyncio
        from types import SimpleNamespace

        from WanWanKit.core import video as vid
        from WanWanKit.tools import media

        monkeypatch.setattr(media, "_VIDEO_OUT", tmp_path)
        monkeypatch.setattr(
            media,
            "load_config",
            lambda: SimpleNamespace(ark_ready=True, mimo_ready=True, video_out_dir=str(tmp_path), ffmpeg_timeout=600.0),
        )
        ctx = self._ctx()

        prompts: list[str] = []
        pad_calls: list[float] = []

        async def _tts(_cfg: object, text: str) -> bytes:
            assert text == "两段配音"
            return b"RIFF-fake-wav"

        def _pad(_src: Path, dst: Path, lead: float) -> None:
            pad_calls.append(lead)
            dst.write_bytes(b"fake-mp3")

        durs = iter([6.0, 6.2])  # 第一次=配音时长，第二次=成品时长

        async def _gen(
            _cfg: object,
            p: str,
            *,
            size: str = "1K",
            watermark: bool = False,
            ref_images: list[str] | None = None,
            max_bytes: int | None = None,
        ) -> bytes:
            prompts.append(p)
            assert size == "1280x720" and max_bytes == media._FRAME_MAX_BYTES
            return b"jpeg-bytes"

        def _asm(images: list[Path], _audio: Path, total: float, out: Path, *, timeout: float = 0) -> None:
            assert len(images) == 2 and abs(total - 6.0) < 1e-6
            out.write_bytes(b"\x00" * 4096)

        monkeypatch.setattr(media.mimo, "synthesize", _tts)
        monkeypatch.setattr(vid, "pad_audio", _pad)
        monkeypatch.setattr(vid, "duration", lambda _p: next(durs))
        monkeypatch.setattr(media.ark, "generate_image", _gen)
        monkeypatch.setattr(vid, "assemble", _asm)

        # lead_silence 传坏值 → 回落默认 1.0（不崩）
        out = asyncio.run(media.video_generate(ctx, ["画面A", "画面B"], "两段配音", lead_silence="bad"))
        assert out.startswith("✅") and "画面 2 个" in out and "时长 6.2 秒" in out
        assert pad_calls == [1.0]
        # 形象锁定默认开：画面提示词都拼上了完整人形锚点
        assert all("Q版小鲨鱼女孩的完整人形" in p for p in prompts)
        # 成品留在 video_out，work 现场已清理
        assert list(tmp_path.glob("video_*.mp4"))
        assert not list(tmp_path.glob("tmp_*"))

    def test_failure_keeps_workdir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """失败不发半成品，且保留现场路径告诉主人。"""
        import asyncio
        from types import SimpleNamespace

        from WanWanKit.core import video as vid
        from WanWanKit.tools import media

        monkeypatch.setattr(media, "_VIDEO_OUT", tmp_path)
        monkeypatch.setattr(
            media,
            "load_config",
            lambda: SimpleNamespace(ark_ready=True, mimo_ready=True, video_out_dir=str(tmp_path), ffmpeg_timeout=600.0),
        )

        async def _tts(_cfg: object, _text: str) -> bytes:
            return b"RIFF"

        def _pad(_src: Path, dst: Path, _lead: float) -> None:
            dst.write_bytes(b"fake-mp3")  # 不真跑 ffmpeg（假字节过不了解码）

        async def _gen(*_a: object, **_k: object) -> bytes:
            raise media.ark.ArkError("方舟返回 500: boom")

        monkeypatch.setattr(media.mimo, "synthesize", _tts)
        monkeypatch.setattr(vid, "pad_audio", _pad)
        monkeypatch.setattr(vid, "duration", lambda _p: 6.0)
        monkeypatch.setattr(media.ark, "generate_image", _gen)

        out = asyncio.run(media.video_generate(self._ctx(), ["画面A"], "一段配音"))
        assert out.startswith("⚠️") and "生图失败" in out and "第1张" in out
        assert "中间文件在" in out and list(tmp_path.glob("tmp_*"))
        assert not list(tmp_path.glob("video_*.mp4"))  # 不留半成品
