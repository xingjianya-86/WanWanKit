"""wanwan_skills 单元测试：只测离线纯逻辑，不碰网络。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from wanwan_skills.config import WanwanConfig, load_config
from wanwan_skills.core import ark, mimo, site
from wanwan_skills.tools import diary


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
        monkeypatch.setattr("wanwan_skills.config.CONFIG_PATH", tmp_path / "nope.json")
        assert load_config() is None

    def test_invalid_json_returns_none(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        bad = tmp_path / "wanwan.json"
        bad.write_text("{not json", encoding="utf-8")
        monkeypatch.setattr("wanwan_skills.config.CONFIG_PATH", bad)
        assert load_config() is None

    def test_defaults_merge(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg_file = tmp_path / "wanwan.json"
        cfg_file.write_text(json.dumps({"ark_api_key": "abc"}), encoding="utf-8")
        monkeypatch.setattr("wanwan_skills.config.CONFIG_PATH", cfg_file)
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
        monkeypatch.setattr("wanwan_skills.config.CONFIG_PATH", cfg_file)
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

    def test_write_appends_same_day(self, tmp_path: Path) -> None:
        diary.write_entry("第一条", "2026-11-06", base=tmp_path)
        diary.write_entry("第二条", "2026-11-06", base=tmp_path)
        content = (tmp_path / "2026-11-06.md").read_text(encoding="utf-8")
        assert "第一条" in content and "第二条" in content

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
        diary.write_entry("正文", "2026-11-06", base=tmp_path)

        # 绕过 _DIARY_DIR：do_publish 用默认目录，这里直接换 diary 路径
        monkeypatch.setattr(diary, "_DIARY_DIR", tmp_path)
        out = asyncio.run(diary._do_publish(object(), "2026-11-06", image_url="http://x/y.jpg"))

        assert "已发布" in out
        assert captured["image"] == "http://x/y.jpg"
        # title 必须含英文，否则站点派生的 URL 只剩日期（非 ASCII 被剥）
        assert captured["title"] == "Diary 2026-11-06 晚晚的日记"

    def test_do_publish_missing_diary(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import asyncio

        monkeypatch.setattr(diary, "_DIARY_DIR", tmp_path)
        out = asyncio.run(diary._do_publish(object(), "2026-01-01"))
        assert "没有日记" in out

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
        from wanwan_skills.core.phone import _parse_ui

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
        from wanwan_skills.core.phone import _parse_ui

        assert _parse_ui("") == []
        assert _parse_ui("not xml at all") == []

    def test_action_whitelist(self) -> None:
        from wanwan_skills.tools.phone import _ACTIONS

        for ok in ("tap", "swipe", "input", "open", "back", "home", "lock", "unlock"):
            assert ok in _ACTIONS
        # 危险动作必须被挡住，避免模型构造任意 shell
        for bad in ("shell", "rm", "reboot", "exec", "am start", ""):
            assert bad not in _ACTIONS


class TestTaskPrompt:
    """任务 prompt 用 .format(today=...)，花括号写错会直接 KeyError。"""

    def test_prompt_formats_with_today(self) -> None:
        from wanwan_skills.tasks import _TASK_PROMPT

        out = _TASK_PROMPT.format(today="2026-11-06", user_id="20002")
        assert "2026-11-06" in out
        assert "{today}" not in out
        assert "不要传 date" in out
        assert "image_url 必须传" in out
        assert "能力缺失叙述" in out
