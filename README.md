# wanwan_skills

给 GsCore 人格用的一组**日记 / 配图 / 语音 / 手机自动化**技能插件：每天定时写日记、生成配图、发布到手帐站点并播报；通过手机（adb over ZeroTier）刷抖音、刷 B 站、给当前视频发评论。

![ICON](ICON.png)

## 功能

| 工具 | 作用 |
|------|------|
| `diary_write` | 写日记到 `data/wanwan/diary/YYYY-MM-DD.md`，同一天多次写会追加 |
| `diary_illustrate` | 生成配图（带参考图保持画风）→ 转存站点 → 返回站点 URL，同时发到对话 |
| `diary_publish` | 把某天日记发布到手帐站点（公开），支持配图；同日期同内容自动去重 |
| `diary_upload_image` | 把外链图片转存到站点，返回可用于配图的 URL |
| `diary_read` | 读本地日记（指定日期，或最近 N 篇） |
| `selfie_generate` | 用参考图生成人物自拍并发出 |
| `voice_say` | 用克隆音色把短句读成语音条发出 |
| `phone_look` / `phone_do` | 看 / 操作手机（点、滑、输入、开关应用等白名单动作） |
| `douyin_browse` / `douyin_comment` | 刷抖音 / 给**当前这条**发评论（自动跳过直播广告；全程不滑走视频；刷完评完自动锁屏收手机） |
| `douyin_reply` | 回复抖音评论区某条（按作者/内容锚定，保留「回复 @作者」归属前缀） |
| `douyin_post` | **发布抖音视频作品（创作者中心网页，不碰手机）**——只发 `data/video_out/` 白名单里的视频，标题 ≤40 字，同标题不重发 |
| `bilibili_browse` / `bilibili_comment` | 刷B站推荐 / 给**当前这条**发评论（同上；广告/会员购卡自动跳过） |
| `bilibili_reply` | 回复B站评论区某条（锚定逻辑同上） |
| `bilibili_post` | **发布B站文字动态（走 Web API，不碰手机）**——标题写成正文第一行 |

另有**每日日记定时任务**（`wanwan_diary_daily`，默认 18:00）：载入人格 → 读当天素材 → 写日记 → 生成配图 → 发布 → 把链接播报给主人；
以及**手机心跳看门狗**（`wanwan_phone_heartbeat`，每 5 分钟）：探测 adb 通道，断线 QQ 告警（30 分钟冷却）、恢复也报一声——配套手机侧 Magisk `service.d/98-keepalive.sh` 保活循环（守 5555 端口、ZT 心跳、Doze 豁免）。

## 安装

WebConsole → 插件商店 → 自定义 URL 安装：

```
https://github.com/<owner>/wanwan_skills.git
```

或手动：

```sh
cd <GsCore>/gsuid_core/plugins
git clone https://github.com/<owner>/wanwan_skills.git
```

然后在 WebConsole 里重载插件（或重启 core）。

## 配置

配置文件：`data/plugins_configs/wanwan_skills.json`（不存在时插件仍可加载，工具会提示未配置）。

```jsonc
{
  // 目标会话：日记播报发给谁。缺任一字段则定时任务跳过
  "ws_bot_id": "koishi",          // 适配器 WS 名，如 koishi / onebot
  "bot_id": "onebot",             // 平台名
  "bot_self_id": "10001",         // Bot 自身 QQ / 账号 ID
  "user_id": "20002",             // 主人 ID

  // 火山方舟 Seedream（生图）
  "ark_api_key": "...",
  "ark_base_url": "https://ark.cn-beijing.volces.com/api/plan/v3",
  "ark_model": "doubao-seedream-5-0-flash-260915",
  "ark_timeout": 120,
  "ark_max_bytes": 150000,        // 出图压缩上限（字节）

  // 小米 MiMo（音色克隆 TTS）
  "mimo_api_key": "...",
  "mimo_base_url": "https://token-plan-cn.xiaomimimo.com/v1",
  "mimo_tts_model": "mimo-v2.5-tts-voiceclone",
  "mimo_timeout": 180,
  "voice_file": "/gsuid_core/data/voice_src/ref_voice.mp3",  // 音色参考音频
  "voice_style": "说话风格描述",

  // 手帐站点（日记发布）
  "site_base_url": "https://example.com",
  "site_username": "...",
  "site_password_file": "/gsuid_core/data/wanwan/.site_password",  // 或直接 "site_password"
  "site_timeout": 60,

  // 形象参考图（自拍 / 配图保持画风）
  "ref_image": "/gsuid_core/data/wanwan/wanwan_ref.jpg",

  // 手机通道（adb over ZeroTier；刷/评/回复用）
  "phone_serial": "10.110.0.161:5555",

  // B站 Web API 登录态（bilibili_post 发动态用）：
  // 浏览器 F12 抄整段 Cookie 写进该文件（一行，含 SESSDATA 与 bili_jct）
  "bili_token_path": "/gsuid_core/data/wanwan/.bili_token"
}
```

密钥与登录态只落 `data/`（600 权限），不进仓库。

### 站点发布说明

站点按 **title 派生 URL slug**，且**剥离所有非 ASCII 字符**。因此默认标题写成
`Diary YYYY-MM-DD <中文名>`——英文前缀存活成为 slug，得到形如
`/blog/posts/diary-2026-11-07/` 的 URL。直接传纯中文标题会退化成 `/blog/posts/2026-11-07/`。

## 定时任务

启动时通过 `@on_core_start` 注册 cron（`wanwan_skills/tasks.py`）：

- 默认 **每天 18:00**，改 `CRON_HOUR` / `CRON_MINUTE` 后重启生效
- 执行体是**独立 session + 人格 system prompt**（不是框架的中性执行器），所以日记有本人格口吻
- 播报走 `emit_proactive_message`，并显式 `suppress_when_heartbeat_recent=False`
  （定时任务不该被刚发过的 heartbeat 压掉）

## 依赖

- Python 3.11+（GsCore 运行环境）
- `httpx`、`Pillow`（GsCore 已自带）

`pyproject.toml` 里的依赖会在 core 启动时按 `auto_install_dep` 自动安装。

## 开发

```sh
# 静态检查
ruff check wanwan_skills tests
ruff format --check wanwan_skills tests

# 单测（离线，不碰网络）
PYTHONPATH=.:<GsCore路径> python -m pytest tests/ -q
```

测试覆盖：配置解析、图片压缩、URL 生成、日记读写、站点响应结构、工具注册完整性、
任务 prompt 格式化。**不测**外部 API 真实调用。

### 已知的框架交互细节

- 工具用 `@ai_tools` 注册；漏装饰器不会报错、只会静默不注册，因此有
  `test_all_tool_functions_registered` 兜底。
- 定时任务的 agent 必须 `dynamic_tools=True`：`create_by="WanwanDiary"` 不在
  `_AGENTIC_CREATE_BY` 名单里，`None` 会走旧门判 `False` 导致**零工具**。
- 播报文本若含「能力缺失叙述」（如「没有那个工具」）会被框架输出闸门整条丢弃，
  prompt 里已明确禁止这类说法。

## License

[MIT](LICENSE)
