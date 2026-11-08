# Changelog

## 1.0.0 (2026-11-08)

首个合并版本——**三合一**：`wanwan_skills` + `httpreq` + `http_tool` → **WanWanKit**。

### Added
- **HTTP 双形态工具**（`tools/http.py`）：
  - 聊天命令 `httpget`（GET 直发）、`httppost` / `http`（POST 按钮+回复双通道确认）
  - AI 工具 `http_request`（模型侧直通，JSON headers/query，响应自动截断）
- **配置全量自定义**：`dy_python_path` / `dy_driver_path` / `video_out_dir` /
  `ffmpeg_timeout` / `http_timeout` / `http_max_body`（原硬编码收编）
- **密钥独立化**：`ark_api_key_file` / `mimo_api_key_file` / `site_password_file`——
  一行一个 key、600 权限、配置只存路径；配置里残留明文**首次加载自动迁移并清除**
- 提交前密钥门禁 `scripts/check_secrets.sh`
- 文档三件套：README（功能矩阵/快速开始/架构/安全）+ `docs/INSTALL.md` + `docs/CONFIGURATION.md`

### Changed
- **改名 `wanwan_skills` → `WanWanKit`**（GitHub 仓库已 rename，旧 URL 自动转跳）。
  内层包同步改名——GsCore 嵌套加载要求内外层目录同名
- 配置文件名 `wanwan_skills.json`、数据目录 `data/wanwan/` **保持不变**（与插件名解耦）
- `pyproject.toml` 规范化：1.0.0 / MIT / 系统依赖说明
- 工具未配置提示文案统一为 WanWanKit

### Fixed
- 测试通过默认迁移路径把 fixture 值写进真实 `.ark_api_key` 的污染事故
  （已隔离到 tmp_path，并修复真实数据：真 key 回填、配置明文清除）
- `diary_illustrate` 缺形象锚点导致配图出现纯空景（现默认锁定晚晚本人）
- 同天多份日记混合发布（现同天只留最新一份，旧份进 `.trash`）
- 发布状态文件被误建成目录导致去重失效（现自动 quarantine 重建）

### 单测
- 91 项离线测试（配置/密钥迁移、日记、发布链路护栏、HTTP 解析、ffmpeg 命令构造、视频管线）
