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

    def test_is_input_node_tracks_moving_input_box(self) -> None:
        """输入框位置随键盘状态变，固定 y 过滤会漏判（曾致 type_text 文字翻倍）。"""
        from wanwan_skills.core.phone import UINode, is_input_node

        def node(x1: int, y1: int, x2: int, y2: int) -> UINode:
            return UINode(text="", desc="", cls="T", clickable=False, x1=x1, y1=y1, x2=x2, y2=y2)

        # 键盘/表情面板打开：输入框是 y≈934 的贴左宽框
        assert is_input_node(node(30, 934, 1045, 1060))
        # 关着：底部输入条
        assert is_input_node(node(55, 2205, 590, 2210))
        # dump 里的 2px 残留副本（在底部条区域）
        assert is_input_node(node(953, 2208, 1017, 2210))
        # B站没有可见键盘，展开输入区在 y≈1992
        assert is_input_node(node(30, 1992, 1010, 2133))
        # 评论列表节点从 x1≈120 起，不算输入框
        assert not is_input_node(node(150, 700, 1040, 850))
        # B站评论列表 x1≈140
        assert not is_input_node(node(140, 1889, 1050, 2009))
        # 视频简介在 y≈1550（模型爱照抄简介，误判会把发成功的评论当成没发）
        assert not is_input_node(node(30, 1550, 950, 1750))


class TestBilibiliCards:
    """推荐流卡片是 content-desc 结构化的，解析错了清单就全错。"""

    def test_parse_video_card(self) -> None:
        from wanwan_skills.tools.bilibili import _parse_card

        c = _parse_card("竖版视频,三份系统安全组件逆向 systemhelper,1.2万观看,-弹幕,时长2:09,UP主某甲")
        assert c is not None
        assert c.title == "三份系统安全组件逆向 systemhelper"
        assert c.views == "1.2万"
        assert c.up == "某甲"
        assert c.danmaku == "-"

    def test_parse_title_with_comma(self) -> None:
        """标题里带逗号不能截断（用第一个统计段当边界）。"""
        from wanwan_skills.tools.bilibili import _parse_card

        c = _parse_card("竖版视频,看好了,这波在大气层,99观看,5弹幕,时长0:30,UP主乙")
        assert c is not None
        assert c.title == "看好了,这波在大气层"
        assert c.views == "99"

    def test_parse_without_stats(self) -> None:
        from wanwan_skills.tools.bilibili import _parse_card

        c = _parse_card("竖版视频,某个没统计的标题")
        assert c is not None
        assert c.title == "某个没统计的标题"
        assert c.views == ""

    def test_junk_cards_rejected(self) -> None:
        """广告/会员购卡不是视频，绝不能点开当评论目标。"""
        from wanwan_skills.tools.bilibili import _parse_card

        assert _parse_card("创作推广,降到一千多的旗舰机小米14,,轻点两下查看详情") is None
        assert _parse_card("会员购,（T）崩坏星穹铁道,,轻点两下查看详情") is None
        assert _parse_card("竖版视频") is None

    def test_state_detection(self) -> None:
        from wanwan_skills.core.phone import UINode
        from wanwan_skills.tools.bilibili import _find_input, _on_video_page, _sheet_open

        def node(text: str, *, x1: int, y1: int, cls: str = "TextView", clickable: bool = True) -> UINode:
            return UINode(text=text, desc="", cls=cls, clickable=clickable, x1=x1, y1=y1, x2=x1 + 300, y2=y1 + 60)

        # 视频页：右侧操作栏
        assert _on_video_page([node("点赞", x1=939, y1=1247)])
        assert not _on_video_page([node("推荐,7之2,标签", x1=140, y1=200)])
        # 评论面板：标题或输入框占位符
        assert _sheet_open([node("评论（3270）", x1=170, y1=854)])
        assert _sheet_open([node("评论千万条，等你发一条", x1=30, y1=2207, cls="EditText")])
        assert not _sheet_open([node("点赞", x1=939, y1=1247)])
        # 输入框定位
        box = node("评论千万条，等你发一条", x1=30, y1=2207, cls="EditText")
        assert _find_input([box]) is box
        assert _find_input([node("搜索", x1=178, y1=102, cls="EditText")]) is None

    def test_comment_entry_via_desc(self) -> None:
        """实测B站评论入口 text='' 文案在 desc——只查 text 会漏（14:37 雷）。"""
        from wanwan_skills.core.phone import UINode
        from wanwan_skills.tools.bilibili import _find_comment_entry, _on_video_page, _sheet_open

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


class TestDouyinJunk:
    """直播 / 广告卡片必须被识别跳过。"""

    @staticmethod
    def _nodes(items: list[tuple[str, bool]]):
        """items = [(text, clickable), ...]，按真实控件树布局。"""
        from wanwan_skills.core.phone import UINode

        return [UINode(text=t, desc="", cls="T", clickable=c, x1=0, y1=0, x2=10, y2=10) for t, c in items]

    def test_live_card_detected(self) -> None:
        from wanwan_skills.tools.douyin import _junk_reason

        nodes = self._nodes([("点击进入直播间按钮", True), ("@某主播", True)])
        assert _junk_reason(nodes) == "直播"

    def test_ad_label_detected(self) -> None:
        from wanwan_skills.tools.douyin import _junk_reason

        nodes = self._nodes([("广告", False), ("@游戏碎碎念", True)])
        assert _junk_reason(nodes) == "广告"

    def test_normal_video_not_flagged(self) -> None:
        from wanwan_skills.tools.douyin import _junk_reason

        # 描述里顺带提到「广告」的正常视频不应被误伤
        nodes = self._nodes([("2026年超好玩的小游戏，不是广告", False), ("@某作者", True), ("喜欢8.7万", False)])
        assert _junk_reason(nodes) == ""

    def test_extract_video_from_real_layout(self) -> None:
        from wanwan_skills.tools.douyin import _extract_video

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
        from wanwan_skills.core.vision import find_color_button

        # 400x800，按钮在下半部（y_min_ratio=0.4 以下）
        png = self._png((400, 800), [(100, 600, 290, 690, (254, 44, 85))])
        blob = find_color_button(png, (254, 44, 85))
        assert blob is not None
        cx, cy = blob.center
        assert abs(cx - 195) <= 8 and abs(cy - 645) <= 8

    def test_small_red_distractors_ignored(self) -> None:
        from wanwan_skills.core.vision import find_color_button

        # 表情/小图标级别的红块（< min_run=70）不该被当成按钮
        png = self._png(
            (400, 800),
            [(30, 700, 70, 740, (254, 44, 85)), (200, 720, 240, 760, (254, 44, 85))],
        )
        assert find_color_button(png, (254, 44, 85)) is None

    def test_picks_largest_of_two_buttons(self) -> None:
        from wanwan_skills.core.vision import find_color_button

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
        from wanwan_skills.core.vision import find_color_button

        # 面板遮罩下的暗红（视频背景）不能误判
        png = self._png((400, 800), [(100, 600, 290, 690, (127, 22, 42))])
        assert find_color_button(png, (254, 44, 85)) is None

    def test_invalid_png_returns_none(self) -> None:
        from wanwan_skills.core.vision import find_color_button

        assert find_color_button(b"not a png", (254, 44, 85)) is None

    def test_button_above_scan_region_ignored(self) -> None:
        from wanwan_skills.core.vision import find_color_button

        # 屏幕上半部的红块（如视频里的红衣服）不该被当成底部按钮
        png = self._png((400, 800), [(100, 60, 290, 150, (254, 44, 85))])
        assert find_color_button(png, (254, 44, 85)) is None


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


class TestHeartbeat:
    """心跳看门狗：误报会狼来了，漏报会失联不自知。"""

    def test_connect_ok_parsing(self) -> None:
        from wanwan_skills.tasks import _connect_ok

        assert _connect_ok("connected to 10.110.0.161:5555\n")
        assert _connect_ok("already connected to 10.110.0.161:5555\n")
        assert not _connect_ok("failed to connect to '10.110.0.161:5555': No route to host\n")
        assert not _connect_ok("")

    def test_alert_cooldown(self) -> None:
        from wanwan_skills.tasks import _ALERT_COOLDOWN, _should_alert

        # 首次断线：立即报
        assert _should_alert(now=1000.0, last_alert=0.0, down=False)
        # 持续断线但冷却未到：闭嘴（否则刷屏）
        assert not _should_alert(now=1060.0, last_alert=1000.0, down=True)
        # 冷却过了：复报（长期失联要持续提醒）
        assert _should_alert(now=1000.0 + _ALERT_COOLDOWN + 1, last_alert=1000.0, down=True)
