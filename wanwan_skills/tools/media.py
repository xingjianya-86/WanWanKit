"""媒体工具：自拍生成（Seedream）与语音发言（MiMo TTS）。"""

from __future__ import annotations

import asyncio
import shutil
import time
import uuid
from pathlib import Path

from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.register import ai_tools
from gsuid_core.logger import logger
from gsuid_core.segment import MessageSegment
from gsuid_core.utils.resource_manager import RM
from pydantic_ai import RunContext

from ..config import load_config
from ..core import ark, mimo, video

_CAP_DOMAIN = "晚晚形象"
_COVERS = [
    "给自己拍一张照片",
    "生成一张自拍 / 立绘",
    "用语音说话 / 发语音条",
    "把一段话读出来",
]

_VIDEO_DOMAIN = "晚晚短视频"
_VIDEO_COVERS = [
    "生成一条带配音的短视频",
    "把画面和配音合成视频",
    "制作视频作品 / 做一条视频",
    "给抖音/B站准备视频素材",
]
_VIDEO_OUT = Path("/gsuid_core/data/video_out")  # 与 douyin_post 白名单同目录
_FRAME_MAX_BYTES = 500000  # 视频帧压缩上限（聊天发图的 150KB 会伤画质）


async def _send_or_register(
    ctx: RunContext[ToolContext],
    segment: MessageSegment,
    *,
    kind: str,
) -> str:
    """主人格直发；能力代理只登记资源并回 ID。

    kind 用于回执文案区分「已发出」与「已登记」。
    """
    deps = ctx.deps
    payload = segment.data
    if deps.allow_user_outbound and deps.bot is not None:
        try:
            await deps.bot.send(segment)
            return "SENT"
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[wanwan] 直发失败，转登记: {exc}")
    # 直发不可用：登记成资源 ID，交给 send_message_by_ai
    rid = RM.register_audio(payload) if kind == "voice" else RM.register(payload)
    return f"RES:{rid}"


@ai_tools(
    category="common",
    capability_domain=_CAP_DOMAIN,
    covers=_COVERS,
    code_callable=False,
    timeout=180.0,
)
async def selfie_generate(
    ctx: RunContext[ToolContext],
    scene: str,
    prompt: str = "",
    size: str = "1K",
) -> str:
    """生成一张晚晚自己的照片并发出（固定形象：深蓝长发、鲨鱼鳍耳、白卫衣）。

    用参考图 + 固定形象描述保证「同一个人」。场景写具体（在做什么、
    什么光、什么构图），不要只写「自拍」。生图约 10-30 秒。

    Args:
        scene: 画面场景，如 "海边夕阳下比耶"、"坐在书桌前写日记"。
        prompt: 可选追加细节，如服装、表情、道具。会拼进形象模板。
        size: 输出尺寸，默认 "1K"。仅在需要更大图时改 "1.5K" / "2K"。
    """
    cfg = load_config()
    if cfg is None:
        return "⚠️ wanwan_skills 未配置（缺 plugins_configs/wanwan_skills.json）"
    if not cfg.ark_ready:
        return "⚠️ 未配置 ark_api_key，无法生图"
    if not cfg.ref_image or not Path(cfg.ref_image).exists():
        return f"⚠️ 参考图不存在：{cfg.ref_image}"

    try:
        ref_bytes = Path(cfg.ref_image).read_bytes()
    except OSError as exc:
        return f"⚠️ 参考图读取失败：{exc}"

    clean_scene = scene.strip()
    if not clean_scene:
        return "⚠️ 请提供 scene（画面场景）"

    full_prompt = (
        "同一人物设定，保持与参考图完全一致的长相、发型、发色、瞳色与服饰："
        "深蓝色长发渐变青色发尾、头顶鲨鱼鳍状发饰、蓝色鱼尾、"
        "白色连帽卫衣印蓝色鲨鱼图案、蓝色百褶短裙、白色过膝袜带蓝色蝴蝶结、白色运动鞋。"
        f"画面：{clean_scene}。"
    )
    if prompt.strip():
        full_prompt += f"细节：{prompt.strip()}。"

    try:
        img = await ark.generate_image(
            cfg,
            full_prompt,
            size=size,
            ref_images=[ark.image_to_ref(ref_bytes)],
        )
    except ark.ArkError as exc:
        return f"⚠️ 生图失败：{exc}"

    result = await _send_or_register(ctx, MessageSegment.image(img), kind="image")
    if result == "SENT":
        return f"✅ 已生成并发出自拍（{len(img) // 1024} KB）"
    return f"✅ 已生成自拍（{len(img) // 1024} KB），资源ID: {result[4:]}"


@ai_tools(
    category="common",
    capability_domain=_CAP_DOMAIN,
    covers=_COVERS,
    code_callable=False,
    timeout=180.0,
)
async def voice_say(
    ctx: RunContext[ToolContext],
    text: str,
) -> str:
    """把一段话用晚晚的音色读出来，发成语音条。

    适合短句、撒娇、道晚安、语气重要的场合。超过 120 字的长内容
    改用正常文字回复，不要硬配。

    Args:
        text: 要读出来的原话，不要带表情符号和 markdown。
    """
    cfg = load_config()
    if cfg is None:
        return "⚠️ wanwan_skills 未配置（缺 plugins_configs/wanwan_skills.json）"
    if not cfg.mimo_ready:
        return "⚠️ 未配置 mimo_api_key 或音色样本缺失，无法配音"

    clean = text.strip()
    if not clean:
        return "⚠️ 请提供 text（要读出来的话）"
    if len(clean) > 120:
        return f"⚠️ 文本过长（{len(clean)} 字），语音只适合短句，请改用文字回复"

    try:
        audio = await mimo.synthesize(cfg, clean)
    except mimo.TtsError as exc:
        return f"⚠️ 配音失败：{exc}"

    result = await _send_or_register(ctx, MessageSegment.record(audio), kind="voice")
    if result == "SENT":
        return f"✅ 已发出语音（{len(audio) // 1024} KB，{len(clean)} 字）"
    return f"✅ 已生成语音，资源ID: {result[4:]}（用 send_message_by_ai 发出）"


def _apply_character_lock(scenes: list[str], enabled: bool) -> list[str]:
    """形象锁定：每个画面拼上晚晚完整人形锚点（主人 2026-10-31 硬性要求）。"""
    if not enabled:
        return scenes
    return [video.WANWAN_LOOK + "场景：" + s for s in scenes]


@ai_tools(
    category="common",
    capability_domain=_VIDEO_DOMAIN,
    covers=_VIDEO_COVERS,
    code_callable=False,
    timeout=900.0,
)
async def video_generate(
    ctx: RunContext[ToolContext],
    scenes: list[str],
    narration: str,
    lead_silence: float = 1.0,
    character_lock: bool = True,
) -> str:
    """生成一条带配音的短视频（画面提示词 + 配音全文 → mp4，落在 video_out）。

    三段管线：MiMo 音色克隆配音 → Seedream 逐画面生图 → ffmpeg 按配音
    时长均分画面合成 720p。产出文件可交给 douyin_post / B站投稿发布。
    所有画面建议用同一句风格前缀保证画风一致；画面与配音顺序一一对应。

    Args:
        scenes: 每个画面的图像提示词，1~8 个（只写场景与动作；默认自动拼
            晚晚的完整人形形象锚点，不用重复描述外形）。
        narration: 配音全文（中文口语化，1000 字以内）。
        lead_silence: 开头静音秒数 0~10，默认 1（开头留 1 秒只给画面）。
        character_lock: 默认 true；只有明确要空景时才设 false。
    """
    cfg = load_config()
    if cfg is None:
        return "⚠️ wanwan_skills 未配置（缺 plugins_configs/wanwan_skills.json）"
    if not cfg.ark_ready:
        return "⚠️ 未配置 ark_api_key，无法生图"
    if not cfg.mimo_ready:
        return "⚠️ 未配置 mimo_api_key 或音色样本缺失，无法配音"

    clean_scenes = [s.strip() for s in (scenes or []) if s and s.strip()]
    if not clean_scenes:
        return "⚠️ 请提供 scenes（至少 1 个画面提示词）"
    if len(clean_scenes) > video.MAX_SCENES:
        return f"⚠️ 画面最多 {video.MAX_SCENES} 个，当前 {len(clean_scenes)} 个"
    nar = (narration or "").strip()
    if not nar:
        return "⚠️ 请提供 narration（配音全文）"
    if len(nar) > video.MAX_NARRATION:
        return f"⚠️ 配音过长（{len(nar)} 字），上限 {video.MAX_NARRATION} 字"
    try:
        lead = max(0.0, min(float(lead_silence or 0), 10.0))
    except (TypeError, ValueError):
        lead = 1.0

    clean_scenes = _apply_character_lock(clean_scenes, character_lock)

    ts = time.strftime("%Y%m%d_%H%M%S")
    work = _VIDEO_OUT / f"tmp_{ts}_{uuid.uuid4().hex[:6]}"
    work.mkdir(parents=True, exist_ok=True)
    out = _VIDEO_OUT / f"video_{ts}_{uuid.uuid4().hex[:4]}.mp4"
    try:
        # 1) 配音（先配音：拿到时长才能切画面）
        audio = work / "narration.wav"
        audio.write_bytes(await mimo.synthesize(cfg, nar))
        if lead > 0:
            padded = work / "narration_padded.mp3"
            await asyncio.to_thread(video.pad_audio, audio, padded, lead)
            audio = padded
        total = await asyncio.to_thread(video.duration, audio)
        if total < 1.0:
            return f"⚠️ 配音时长异常（{total:.2f}s），请重试（中间文件在 {work}）"
        if total / len(clean_scenes) < 0.3:
            return (
                f"⚠️ 配音太短（{total:.1f}s）配 {len(clean_scenes)} 个画面不够分，"
                f"减少画面或加长配音（中间文件在 {work}）"
            )

        # 2) 生图（并行，失败整体报错不发半成品）
        results = await asyncio.gather(
            *[
                ark.generate_image(cfg, p, size=f"{video.W}x{video.H}", max_bytes=_FRAME_MAX_BYTES)
                for p in clean_scenes
            ],
            return_exceptions=True,
        )
        fails = [f"第{i + 1}张: {r}" for i, r in enumerate(results) if isinstance(r, Exception)]
        if fails:
            return "⚠️ 生图失败（" + "；".join(fails)[:400] + f"）（中间文件在 {work}）"
        imgs: list[Path] = []
        for i, data in enumerate(results):
            p = work / f"scene_{i + 1:02d}.jpg"
            p.write_bytes(data)  # type: ignore[arg-type]
            imgs.append(p)

        # 3) 合成
        await asyncio.to_thread(video.assemble, imgs, audio, total, out)
        size = out.stat().st_size
        dur = await asyncio.to_thread(video.duration, out)
        if dur < 1.0:
            return f"⚠️ 合成结果时长异常，请重试（中间文件在 {work}）"
    except (ark.ArkError, mimo.TtsError, video.VideoError, OSError) as exc:
        return f"⚠️ 生成失败：{exc}（中间文件在 {work}）"
    except Exception as exc:  # noqa: BLE001
        return f"⚠️ 生成失败（{type(exc).__name__}: {str(exc)[:300]}）（中间文件在 {work}）"

    try:
        shutil.rmtree(work, ignore_errors=True)
    except OSError:  # noqa: S110 —— 清理失败不影响成品
        pass

    logger.info(f"[wanwan] 短视频已生成：{out.name} {len(clean_scenes)}画面 {dur:.1f}s")
    return (
        f"✅ 已生成短视频：{out}\n"
        f"画面 {len(clean_scenes)} 个 | 时长 {dur:.1f} 秒 | {size / 1024 / 1024:.1f} MB | "
        "配音 小米MiMo·音色克隆\n"
        "可交给 douyin_post 发布（先跟主人确认标题与是否公开）"
    )
