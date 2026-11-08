# WanWanKit

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)
[![GsCore 0.11+](https://img.shields.io/badge/GsCore-0.11%2B-brightgreen.svg)](https://github.com/Genshin-bots/gsuid_core)

给 GsCore 人格用的**全能力箱**插件：日记手账、语音自拍、短视频生成、**抖音 / B 站全链路（刷 · 评 · 回复 · 图文视频发布）**、手机自动化与 HTTP 工具。

> 由三个历史插件三合一而成：`wanwan_skills`（人格技能）+ `httpreq` / `http_tool`（HTTP 工具，重复的 AI 生成品）。

![ICON](ICON.png)

## 功能矩阵

### 人格内容

| 工具 | 作用 |
|------|------|
| `diary_write` | 写日记到 `data/wanwan/diary/YYYY-MM-DD.md`，**同一天再写会覆盖上一份**（旧份挪 `.trash` 备份）——同天只留最新，发布绝不混合 |
| `diary_illustrate` | 生成配图（带参考图保持画风，**默认锁定晚晚本人在场**、严禁空景）→ 转存站点 → 返回站点 URL，同时发到对话 |
| `diary_publish` | 把某天日记发布到手帐站点（公开）——**只发当天最新一份**；同日期同内容自动去重 |
| `diary_upload_image` / `diary_read` | 外链图片转存站点 / 读本地日记 |
| `selfie_generate` | 用参考图生成人物自拍并发出 |
| `voice_say` | 用克隆音色把短句读成语音条发出 |
| `video_generate` | **生成带配音短视频**（Seedream 生图 + MiMo 配音 + ffmpeg 合成 720p → `video_out_dir`），画面默认锁定晚晚本人 |

### 手机自动化（adb over TCP）

| 工具 | 作用 |
|------|------|
| `phone_look` / `phone_do` | 看 / 操作手机（点、滑、输入、开关应用等白名单动作） |
| `douyin_browse` / `douyin_comment` | 刷抖音 / 给**当前这条**发评论（自动跳过直播广告；全程不滑走视频；刷完评完自动锁屏收手机） |
| `douyin_reply` | 回复抖音评论区某条（按作者/内容锚定，保留「回复 @作者」归属前缀） |
| `bilibili_browse` / `bilibili_comment` | 刷B站推荐 / 给**当前这条**发评论（广告/会员购卡自动跳过） |
| `bilibili_reply` | 回复B站评论区某条（锚定逻辑同上） |

### 内容发布（API / 网页模式，不碰手机）

| 工具 | 作用 |
|------|------|
| `douyin_post` | **发布抖音视频作品**（创作者中心网页）——只发 `video_out_dir` 白名单视频，标题 ≤40 字，同标题不重发 |
| `bilibili_post` | **发布B站动态（文字/图文，Web API）**——`images` 可带 1-9 张本地图 |
| `bilibili_post_video` | **B站视频投稿**（分片上传→分区预测→提交）——`private=true` 仅自己可见（测试档） |

### HTTP 工具（双形态，仅主人）

| 入口 | 作用 |
|------|------|
| `httpget <url> [-H "k: v"] [-p k=v]` | GET 直接执行 |
| `httppost <url> ... [-d body]` / `http <METHOD> <URL> ...` | POST **带确认**（按钮或回复「确认」，60s 未确认取消） |
| `http_request`（AI 工具） | 模型侧直通 GET/POST，headers/query 传 JSON，响应体自动截断 |

### 定时任务

- **每日日记**（默认 18:00）：载入人格 → 读素材 → 写日记 → 配图 → 发布 → 播报链接
- **手机心跳看门狗**（每 5 分钟）：adb 断线 QQ 告警（30 分钟冷却）、恢复也报一声

## 快速开始

### 方式一：插件商店（推荐）

聊天发送（需 master 权限）：

```
core安装插件WanWanKit
```

> 商城上架进行中（PR 审核）。在此之前用方式二。

### 方式二：手动安装

```sh
cd <GsCore>/gsuid_core/plugins
git clone https://github.com/xingjianya-86/WanWanKit.git
```

重启 core（或 WebConsole 重载插件），看到 `{WanWanKit} 就绪` 即成功。

### 首次配置

按 **[安装手册](docs/INSTALL.md)** 走：依赖检查 → 配置文件 → 密钥写入。
全部配置项见 **[配置手册](docs/CONFIGURATION.md)**。

## 架构

```
WanWanKit/                     ← 仓库根 = 插件根（GsCore 嵌套结构：内层包与外层目录同名）
├── __init__.py / __nest__.py   ← 插件壳标记（加载器识别）
├── pyproject.toml              ← 元数据（依赖仅 httpx + Pillow，系统依赖见安装手册）
├── ICON.png / LICENSE
├── docs/
│   ├── INSTALL.md              ← 安装手册（依赖矩阵 / 分步 / 故障排查）
│   └── CONFIGURATION.md        ← 配置手册（全配置项 + 密钥管理）
├── scripts/check_secrets.sh    ← 提交前密钥扫描门禁
├── WanWanKit/                  ← 内层包（业务代码，全部相对导入）
│   ├── config.py               ← 配置加载（密钥独立文件优先 + 明文自动迁移）
│   ├── tasks.py                ← 定时任务（日记 cron / 心跳看门狗）
│   ├── core/                   ← adb · ark · mimo · bili_api · site · video · phone · vision · look
│   └── tools/                  ← diary · media · phone · douyin · bilibili · http
└── tests/                      ← 91 项离线单测（不碰网络）
```

## 安全

- **密钥与登录态绝不进 git**：全部落 `data/wanwan/` 独立文件（600 权限），配置 JSON 里只存**路径**；配置里残留的明文会在首次加载时**自动迁移到独立文件并清除**
- 仓库提交前跑 `scripts/check_secrets.sh` 扫描门禁（命中即阻断）
- 平台固定端点（`api.bilibili.com` / `creator.douyin.com` 等）**不开放自定义**（协议与风控绑定）；服务型地址（ark / mimo / site 的 `base_url`）全部可自定义

## 定时任务与框架交互细节

- 执行体是**独立 session + 人格 system prompt**，日记有本人格口吻；播报走 `emit_proactive_message`
- 工具用 `@ai_tools` 注册；漏装饰器不会报错、只会静默不注册，因此有
  `test_all_tool_functions_registered` 兜底
- 定时任务的 agent 必须 `dynamic_tools=True`（`create_by="WanwanDiary"` 不在
  `_AGENTIC_CREATE_BY` 名单里，`None` 会走旧门判 `False` 导致**零工具**）
- 播报文本若含「能力缺失叙述」会被框架输出闸门整条丢弃，prompt 里已禁止

## 开发

```sh
# 静态检查
ruff check WanWanKit tests
ruff format --check WanWanKit tests

# 提交前密钥门禁（必跑）
sh scripts/check_secrets.sh

# 单测（离线，不碰网络）
PYTHONPATH=.:<GsCore路径> python -m pytest tests/ -q
```

测试覆盖：配置解析与密钥迁移、图片压缩、日记读写、站点响应结构、工具注册完整性、
发布链路护栏、HTTP 解析、ffmpeg 命令构造。**不测**外部 API 真实调用。

## License

[MIT](LICENSE)
