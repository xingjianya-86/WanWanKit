# WanWanKit 配置手册

配置文件：`data/plugins_configs/wanwan_skills.json`（**文件名固定**，与插件名解耦）。
配置**每次调用热读取**，改完保存即生效（无需重启；个别涉及 cron 的项需重启）。
文件不存在时插件仍可加载，相关工具会返回「未配置」提示。

```jsonc
{
  // 全部字段都有默认值，只写需要改的即可
}
```

---

## 1. 密钥管理（最重要，先读）

**原则：密钥与登录态绝不写进配置 JSON、绝不进 git、绝不进截图/文档。**

密钥一律放**独立文件**（一行一个纯值，无引号无空格），配置里只存**路径**：

| 密钥 | 默认文件路径 | 配置字段（路径） |
|------|-------------|------------------|
| 火山方舟 Key（生图） | `data/wanwan/.ark_api_key` | `ark_api_key_file` |
| 小米 MiMo Key（配音） | `data/wanwan/.mimo_api_key` | `mimo_api_key_file` |
| 手帐站点密码 | `data/wanwan/.site_password` | `site_password_file` |
| B站 Cookie（`SESSDATA; bili_jct`） | `data/wanwan/.bili_token` | `bili_token_path` |
| 抖音登录 Cookie | `data/mcp/.douyin_cookie.json` | （由驱动脚本自管） |

写入与授权：

```sh
echo -n 'your-key-here' > /gsuid_core/data/wanwan/.ark_api_key
chmod 600 /gsuid_core/data/wanwan/.ark_api_key
```

**明文自动迁移**：若配置 JSON 里残留明文密钥（如从旧版本升级），
插件首次加载时会自动将其写入对应独立文件并把配置里的明文清空。
迁移后可用下面命令自查：

```sh
grep -E '"(ark_api_key|mimo_api_key|site_password)"\s*:\s*"[^"]' plugins_configs/wanwan_skills.json
# 预期无输出（全部为空串）
```

**读取优先级**：独立文件 > 配置内联值（兼容保留，不推荐）> 空。

---

## 2. 目标会话（日记播报发给谁）

| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `ws_bot_id` | str | `""` | 适配器 WS 名，如 `koishi` / `onebot` |
| `bot_id` | str | `""` | 平台名 |
| `bot_self_id` | str | `""` | Bot 自身 QQ / 账号 ID |
| `user_id` | str | `""` | 主人 ID |

缺任一字段 → 每日日记定时任务跳过（便于开箱即用）。
四项齐 → `session_ready`，格式 `{ws}:{bot}:{bot_self}:private:{user}`。

---

## 3. AI 模型与服务地址（全部可自定义）

| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `ark_base_url` | str | `https://ark.cn-beijing.volces.com/api/plan/v3` | **生图服务地址，可自定义**（自建代理/网关） |
| `ark_model` | str | `doubao-seedream-5-0-flash-260915` | 生图模型 |
| `ark_timeout` | float | `120` | 单图生成超时（秒） |
| `ark_max_bytes` | int | `150000` | 聊天发图压缩上限（字节）；视频帧另有 500KB 上限 |
| `mimo_base_url` | str | `https://token-plan-cn.xiaomimimo.com/v1` | **配音服务地址，可自定义** |
| `mimo_tts_model` | str | `mimo-v2.5-tts-voiceclone` | 音色克隆模型 |
| `mimo_timeout` | float | `180` | 配音超时（秒） |
| `voice_file` | str | `/gsuid_core/data/voice_src/xilian_30s.mp3` | 音色参考音频 |
| `voice_style` | str | （预设） | 配音风格指令（自然语言） |
| `ref_image` | str | `/gsuid_core/data/wanwan/wanwan_ref.jpg` | 形象参考图（自拍/配图保持画风） |

> **可自定义 vs 平台固定端点**：上表 `*_base_url` 全部开放自定义。
> 而 B站 / 抖音的 API 端点（`api.bilibili.com`、`member.bilibili.com`、
> `creator.douyin.com` 等）**刻意不开放**——它们与平台签名、风控绑定，改了必然失败。

---

## 4. 手机通道（刷 / 评 / 回复）

| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `phone_serial` | str | `""` | `adb connect` 目标，如 `10.110.0.161:5555`；空 = 手机类工具返回未配置 |
| `phone_ime` | str | 小米搜狗输入法 | 输入法组件名（`type_text` 依赖 AdbKeyboard） |
| `phone_shot_dir` | str | `data/wanwan/phone_shots` | 截图落盘目录 |

配套：心跳看门狗每 5 分钟探测，断线 QQ 告警（30 分钟冷却）；
手机侧建议跑 Magisk `service.d/98-keepalive.sh`（守 5555 / ZT 心跳 / Doze 豁免）。

---

## 5. 发布凭据

| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `bili_token_path` | str | `data/wanwan/.bili_token` | B站 Cookie 文件（发动态/图文/投稿） |
| `site_base_url` | str | `""` | 手帐站点地址，**可自定义** |
| `site_username` | str | `""` | 站点账号 |
| `site_timeout` | float | `60` | 站点请求超时（秒） |

抖音发布凭据由驱动脚本自管（`dy_python_path` / `dy_driver_path` 指向的环境与 Cookie）。

---

## 6. 路径与超时（原硬编码，全部可自定义）

| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `video_out_dir` | str | `/gsuid_core/data/video_out` | 视频产物目录；**同时是 douyin_post / B站投稿的白名单** |
| `ffmpeg_timeout` | float | `600` | 短视频合成超时（秒） |
| `dy_python_path` | str | `/gsuid_core/data/douyin_venv/bin/python` | 抖音发布用的独立解释器 |
| `dy_driver_path` | str | `/gsuid_core/data/mcp/dy_driver.py` | 抖音发布驱动脚本 |
| `http_timeout` | float | `15` | HTTP 工具请求超时（秒） |
| `http_max_body` | int | `1500` | HTTP 工具响应体截断长度（字符） |

---

## 7. 配置示例（完整可用模板）

```jsonc
{
  // 会话
  "ws_bot_id": "koishi",
  "bot_id": "onebot",
  "bot_self_id": "10001",
  "user_id": "20002",

  // AI 模型（地址可自定义；密钥写独立文件，这里不写）
  "ark_base_url": "https://ark.cn-beijing.volces.com/api/plan/v3",
  "mimo_base_url": "https://token-plan-cn.xiaomimimo.com/v1",

  // 手机
  "phone_serial": "10.110.0.161:5555",

  // 发布
  "site_base_url": "https://example.com",
  "site_username": "wanwan",
  "bili_token_path": "/gsuid_core/data/wanwan/.bili_token",

  // 密钥文件路径（默认值，仅在你挪了位置时才需要写）
  "ark_api_key_file": "/gsuid_core/data/wanwan/.ark_api_key",
  "mimo_api_key_file": "/gsuid_core/data/wanwan/.mimo_api_key",
  "site_password_file": "/gsuid_core/data/wanwan/.site_password"
}
```

---

## 8. 自查清单

- [ ] 配置 JSON 里**没有**任何密钥明文（`grep` 见第 1 章命令）
- [ ] 密钥文件存在且 `chmod 600`
- [ ] 用到的每个能力，其配置字段都已填（对照第 3-6 章）
- [ ] 平台 Cookie 未过期（B站 / 抖音）
- [ ] `ffmpeg` / adb / douyin_venv 等系统依赖按 [安装手册](INSTALL.md) 就位
