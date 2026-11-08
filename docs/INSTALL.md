# WanWanKit 安装手册

从零把 WanWanKit 装进你的 GsCore，并按需开通各能力。

## 系统要求

| 项 | 要求 |
|----|------|
| GsCore | 0.11+（插件依赖 `@ai_tools`、`Plugins()`、`SV` 等框架 API） |
| Python | 3.11+（GsCore 运行环境） |
| 磁盘 | 约 10 MB（代码 + 配置；可选能力另计） |

Python 依赖仅 `httpx`、`Pillow`（GsCore 自带；`pyproject.toml` 声明的依赖会在 core 启动时按 `auto_install_dep` 自动安装）。

## 安装

### 方式一：插件商店（推荐）

拥有 master 权限的用户在聊天里发送：

```
core安装插件WanWanKit
```

按提示 `core重启` 生效。

### 方式二：手动 git clone

```sh
cd <GsCore>/gsuid_core/plugins
git clone https://github.com/xingjianya-86/WanWanKit.git
```

重启 core，或在 WebConsole → 插件里重载。

### 验证安装

启动日志出现即成功：

```
🔌 {WanWanKit} 就绪 (0.xx s)
```

## 能力矩阵与可选依赖

插件**开箱即用**的部分不需要任何额外依赖。以下为**可选能力**的系统级依赖：

| 能力 | 依赖 | 配置项 |
|------|------|--------|
| 人格内容（日记 / 自拍 / 语音 / 配图） | 火山方舟 API Key（生图）、小米 MiMo API Key（配音） | `ark_api_key_file`、`mimo_api_key_file` |
| 日记发布到手帐站点 | 站点账号密码 | `site_base_url`、`site_password_file` |
| **短视频生成** | 宿主 `ffmpeg` / `ffprobe`（`which ffmpeg` 验证） | `ffmpeg_timeout`、`video_out_dir` |
| **手机自动化**（刷 / 评 / 回复） | 手机开启 adb over TCP（建议 ZeroTier 内网） | `phone_serial`、`phone_ime` |
| **抖音发布** | 独立 `douyin_venv`（patchright 浏览器环境）+ 抖音登录 Cookie | `dy_python_path`、`dy_driver_path` |
| **B站发布**（动态 / 图文 / 视频投稿） | 浏览器 Cookie（`SESSDATA; bili_jct`） | `bili_token_path` |
| HTTP 工具 | 无 | `http_timeout`、`http_max_body` |

> 只装不用某能力时，对应工具会返回一条可读的「未配置」提示，不影响其他功能。

## 首次配置（分步）

### 1. 创建配置文件

`data/plugins_configs/wanwan_skills.json`（文件名固定，与插件名解耦）：

```sh
cat > /path/to/GsCore/data/plugins_configs/wanwan_skills.json <<'EOF'
{
  "ws_bot_id": "koishi",
  "bot_id": "onebot",
  "bot_self_id": "10001",
  "user_id": "20002"
}
EOF
```

字段含义见 [配置手册](CONFIGURATION.md)。**配置里不要写任何密钥明文**——见第 2 步。

### 2. 写入密钥（独立文件，绝不进 git）

密钥一律放 `data/wanwan/` 下的独立文件（一行一个值，600 权限）：

```sh
mkdir -p /path/to/GsCore/data/wanwan

# 火山方舟 Key（生图）
echo -n 'ark-xxxx-xxxx' > /path/to/GsCore/data/wanwan/.ark_api_key

# 小米 MiMo Key（配音）
echo -n 'tp-xxxx' > /path/to/GsCore/data/wanwan/.mimo_api_key

# 手帐站点密码
echo -n 'your-password' > /path/to/GsCore/data/wanwan/.site_password

# B站 Cookie（浏览器 F12 → Network → 任意请求 → Request Headers 抄整段）
echo -n 'SESSDATA=xxx; bili_jct=yyy' > /path/to/GsCore/data/wanwan/.bili_token

chmod 600 /path/to/GsCore/data/wanwan/.ark_api_key \
          /path/to/GsCore/data/wanwan/.mimo_api_key \
          /path/to/GsCore/data/wanwan/.site_password \
          /path/to/GsCore/data/wanwan/.bili_token
```

> **迁移提示**：如果你从旧版本升级、配置 JSON 里还有明文密钥，插件首次加载时会
> **自动**把它们写入上述独立文件并清空配置里的明文——升级后检查一次即可。

### 3. 按需开通可选能力

- **手机**：手机与 GsCore 同网（或 ZeroTier），开启无线调试后：
  `adb connect <手机IP>:5555`，把该地址填进 `phone_serial`
- **短视频**：`which ffmpeg` 确认可用
- **抖音发布**：准备 `douyin_venv`（patchright 环境）与登录 Cookie，
  路径填入 `dy_python_path` / `dy_driver_path`；用 `douyin_post` 前可先跑
  驱动脚本的 `check` 动作验证登录态
- **B站发布**：按第 2 步写入 Cookie 后，`bili_token_path` 指向该文件

### 4. 重启并验证

重启 core → 看到 `{WanWanKit} 就绪` → 对晚晚说「发一条测试动态」之类的指令，
或直接观察工具返回。

## 故障排查

| 现象 | 原因与解法 |
|------|-----------|
| 启动日志没有 `{WanWanKit} 就绪` | 内层包目录名必须与外层插件目录**同名**（`plugins/WanWanKit/WanWanKit/`）。手动 `git clone` 后检查是否多套了一层目录 |
| 工具提示「未配置」 | 对应字段没配——查 [配置手册](CONFIGURATION.md) 的分组表 |
| 「缺 B站登录态」 | `bili_token_path` 文件不存在或为空，按第 2 步重写 |
| 生图报 401 / 鉴权失败 | `.ark_api_key` 内容不对（注意 `echo -n` 不带换行） |
| 手机工具「手机通道未配置」 | `phone_serial` 为空；或 `adb connect <ip>:5555` 失败（看心跳告警日志） |
| 短视频生成报 ffmpeg 失败 | 宿主未装 ffmpeg，或 `ffmpeg_timeout` 太小 |
| 抖音发布报「缺少抖音发布环境」 | `dy_python_path` / `dy_driver_path` 指向的文件不存在 |
| 发布类工具报风控 / 登录过期 | 平台风控，稍后再试；登录过期则更新对应 Cookie 文件 |
| 配置改了不生效 | 配置每次调用热读取——确认改的是 `plugins_configs/wanwan_skills.json` 且 JSON 合法 |

## 升级

```sh
cd <GsCore>/gsuid_core/plugins/WanWanKit
git pull
# WebConsole 重载插件或重启 core
```

升级后建议跑一次密钥检查（确认没有明文残留）：

```sh
grep -E '"(ark|mimo|site)_.*key|password"' data/plugins_configs/wanwan_skills.json
# 预期：全部为 "" 或只有 *_file 路径字段
```

## 卸载

```sh
cd <GsCore>/gsuid_core/plugins
rm -rf WanWanKit
# 重启 core
```

数据（日记、密钥、配置）保留在 `data/` 下，需要时可手动清理。
