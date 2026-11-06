"""每日日记定时任务：每天 18:00 用晚晚人格写日记并发布到 sua17.cn。

链路：cron → 载入晚晚人格 → 独立 session 跑一轮（读当天历史 →
diary_write → diary_publish）→ emit_proactive_message 播报给主人。

不用框架的 execute_scheduled_task：那个执行体是中性 prompt + 独立 session，
既没有人格口吻也没有当天记忆，写不出日记。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from gsuid_core.aps import scheduler
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.server import on_core_start

from .config import WanwanConfig, load_config

TASK_ID = "wanwan_diary_daily"
PERSONA = "晚晚"

CRON_HOUR = 18
CRON_MINUTE = 0

_TASK_PROMPT = """\
现在是每天 18:00 的日记时间。**今天是 {today}**。
**完成下面四步是硬任务，中途不许放弃**：

1. 素材（最多 2 次调用，失败就往下走）：
   调 read_chat_history(session_id="private:{user_id}", limit=20)；
   读不到就调一次 search_cognition 捞今天的片段。
   **禁止**用 run_code 探索文件系统、禁止反复重试读历史。
2. 调 diary_write 写今天的日记（200-400 字），
   以你第一人称口吻记下发生了什么、你的想法和心情，mood 填一个心情词。
   **不要传 date 参数**（默认就是今天，你传错会写到别的日期）。
3. 调 diary_illustrate 生成配图（画面取材自日记内容），拿到站点 URL。
4. 调 diary_publish 发布，**image_url 必须传上一步的 URL**，
   同样**不要传 date**（默认今天 = {today}）。

第 2 步是本次任务的核心，**即使素材没读到也必须先写完日记再继续**。

收尾汇报（**这条会被框架输出闸门审查，写错会整条丢弃**）：
只说结果——日记写好了 + 完整链接原样贴出
（形如 https://sua17.cn/blog/posts/{today}/）。

**严禁**出现这些说法（命中即丢弃整条播报）：
- 「读不到 / 没有那个工具 / 检索 / 换了别的办法 / 失败了」这类过程与能力缺失叙述
- 任何工具名、参数名、报错原文
- 日记正文复述

某步没做成就跳过它继续下一步，**汇报里只字不提**；全部失败才说一句「今天没写成」。
"""


def _build_event(cfg: WanwanConfig) -> Event:
    """构造指向目标私聊的 Event（定时任务无真实入站消息）。

    ``session_id`` 是 property、由会话标识字段推导，不能作构造参数。
    """
    ev = Event(
        bot_id=cfg.bot_id,
        bot_self_id=cfg.bot_self_id,
        user_id=cfg.user_id,
        user_type="direct",
        group_id=None,
        real_bot_id=cfg.bot_id,
        msg_id="",
        WS_BOT_ID=cfg.ws_bot_id,
    )
    if ev.session_id != cfg.session_id:
        # 字段改了会导致播报发错会话，宁可显式告警也不要静默跑偏
        logger.warning(f"[wanwan] 日记任务 session_id 不匹配: {ev.session_id} != {cfg.session_id}")
    return ev


async def _build_system_prompt() -> str | None:
    """载入晚晚人格原文并包上框架约束，失败返回 None。"""
    from gsuid_core.ai_core.base_prompts import join_framework_constraints
    from gsuid_core.ai_core.persona.resource import load_persona
    from gsuid_core.ai_core.prompt_group.store import render_prompt

    persona_content = await load_persona(PERSONA)
    if not persona_content:
        return None
    role = render_prompt(PERSONA, "role_playing_start")
    return join_framework_constraints(PERSONA, f"{role}\n{persona_content}")


async def run_daily_diary() -> None:
    """日记任务主体：跑一轮晚晚人格，写日记并发布，再播报结果。"""
    from gsuid_core.ai_core.gs_agent import create_agent
    from gsuid_core.ai_core.proactive import emit_proactive_message
    from gsuid_core.ai_core.utils import is_silence_marker

    cfg = load_config()
    if cfg is None:
        logger.error("[wanwan] 日记任务跳过：缺 plugins_configs/wanwan_skills.json")
        return
    if not cfg.session_ready:
        logger.error("[wanwan] 日记任务跳过：未配置 ws_bot_id/bot_id/bot_self_id/user_id")
        return

    system_prompt = await _build_system_prompt()
    if system_prompt is None:
        logger.error(f"[wanwan] 日记任务跳过：人格 {PERSONA} 载入失败")
        return

    today = datetime.now().strftime("%Y-%m-%d")
    ev = _build_event(cfg)
    session_id = f"wanwan_diary_{today}_{uuid.uuid4().hex[:8]}"

    agent = create_agent(
        system_prompt,
        create_by="WanwanDiary",
        persona_name=PERSONA,
        session_id=session_id,
        is_subagent=True,
        # WanwanDiary 不在 _AGENTIC_CREATE_BY，None 会走旧门判 False → 零工具
        dynamic_tools=True,
        # 日记要读素材 + 写 + 配图 + 发布，180s 会被读历史耗光
        wall_clock_budget=300,
    )
    agent.bind_budget_scope(ev)
    log_file = ""
    try:
        result = await agent.run(
            _TASK_PROMPT.format(today=today, user_id=cfg.user_id),
            ev=ev,
            return_mode="return",
            budget_gate=True,
            is_framework_injection=True,
        )
    except Exception as exc:  # noqa: BLE001 —— job 异常必须落日志，否则 APScheduler 只留一句
        logger.exception(f"[wanwan] 日记任务执行失败: {exc}")
        return
    finally:
        if agent._session_logger is not None:
            log_file = str(agent._session_logger._file_path)
            agent._session_logger.close()

    text = result if isinstance(result, str) else ""
    if not text.strip() or is_silence_marker(text):
        logger.info(f"[wanwan] 日记任务 {today} 无播报内容（工具已执行，模型静默收尾）")
        return

    sent = await emit_proactive_message(
        event=ev,
        message=text.strip(),
        source="scheduled_task",
        trigger_reason=TASK_ID,
        generator_log_files=[log_file] if log_file else None,
        persona_name=PERSONA,
        # 定时任务播报不该被刚发过的 heartbeat 压掉（默认 True 是给 Heartbeat 自己的）
        suppress_when_heartbeat_recent=False,
    )
    logger.info(f"[wanwan] 日记任务 {today} 播报{'成功' if sent else '被抑制'}")


@on_core_start
def register_diary_task() -> None:
    """启动时注册每日日记 cron（幂等，重启不叠加）。"""
    scheduler.add_job(
        func=run_daily_diary,
        trigger="cron",
        hour=CRON_HOUR,
        minute=CRON_MINUTE,
        id=TASK_ID,
        replace_existing=True,
        name="晚晚每日日记",
    )
    logger.info(f"[wanwan] 每日日记任务已注册：每天 {CRON_HOUR:02d}:{CRON_MINUTE:02d}")
