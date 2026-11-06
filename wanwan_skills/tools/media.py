"""媒体工具：自拍生成（Seedream）与语音发言（MiMo TTS）。"""

from __future__ import annotations

from pathlib import Path

from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.register import ai_tools
from gsuid_core.logger import logger
from gsuid_core.segment import MessageSegment
from gsuid_core.utils.resource_manager import RM
from pydantic_ai import RunContext

from ..config import load_config
from ..core import ark, mimo

_CAP_DOMAIN = "晚晚形象"
_COVERS = [
    "给自己拍一张照片",
    "生成一张自拍 / 立绘",
    "用语音说话 / 发语音条",
    "把一段话读出来",
]


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
