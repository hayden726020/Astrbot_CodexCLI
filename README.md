# Astrbot_CodexCLI

> **OpenAI-Compatible HTTP Bridge & Sandboxed Runner for OpenAI Codex CLI & AstrBot**

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Docker](https://img.shields.io/badge/docker-ready-blue.svg)](https://www.docker.com/)

**Astrbot_CodexCLI** 是一个专为 [AstrBot](https://github.com/Soulter/AstrBot) 与 OpenAI Codex CLI 打造的高性能、企业级安全 HTTP 桥接适配器。

通过将 Codex CLI 的底层交互封装为标准 OpenAI 格式的 `/v1/chat/completions` 与 `/v1/models` 接口，可以让 AstrBot（或任何标准 OpenAI 兼容客户端）无缝将 Codex CLI 作为大语言模型及多模态后端使用，并扩展支持隔离的联网搜索与受控生图能力。

---

## 🌟 核心特性 (Key Features)

- **标准 OpenAI 协议兼容**：提供标准 `/v1/chat/completions` 与 `/v1/models` 接口，支持纯文本与多模态图片输入（Base64 URL 自动解码校验与预处理）。
- **进程级安全隔离沙箱 (Strict Sandboxing)**：
  - 基于容器只读文件系统 (`read_only: true`)、最小特权 (`cap_drop: [ALL]`)、`no-new-privileges`。
  - 严格限制并发请求数 (`BoundedSemaphore`)、单次调用内存与超时熔断控制。
  - 输入多模态图像执行尺寸/像素安全校验（基于 Pillow），阻断像素爆炸反序列化与超大 Payload 攻击。
- **权限与 Token 强隔离**：
  - 聊天接口、联网搜索接口与生图接口采用互相独立的 32 字节 Bearer Token，杜绝权限混淆。
- **Codex 原生扩展能力接入**：
  - **隔离联网搜索 (`/v1/ext/search`)**：基于 Codex CLI 原生 Web Search 能力，具备进程级每小时滑动窗口限流与安全语境过滤。
  - **受控文生图 (`/v1/ext/image`)**：支持专属 Thread 临时目录安全提取有效 PNG 图片，配套 SQLite 原子事务每日配额管理。
- **宿主安全代理转发器 (Relay)**：
  - 包含 `relay.py` 本地 Unix Domain Socket 转发机制，避免容器开启全开放宿主网络，安全打通必要的网络代理通道。
- **高测试覆盖率**：内置完整的单元测试覆盖（聊天适配、安全校验、图片生成防伪、多进程监管等）。

---

## 🏗️ 架构概览 (Architecture)

```text
┌─────────────────┐       HTTP (Bearer Auth)       ┌────────────────────────────────┐
│     AstrBot     │ ─────────────────────────────> │          codex-bridge          │
│   (Bot Engine)  │ <───────────────────────────── │  - supervisor.py (Lifecycle)   │
└─────────────────┘      OpenAI Compatible API     │  - bridge.py (OpenAI Adapter)  │
                                                   │  - image_generation.py         │
                                                   └───────────────┬────────────────┘
                                                                   │ Spawn subprocess
                                                                   ▼
                                                   ┌────────────────────────────────┐
                                                   │        OpenAI Codex CLI        │
                                                   │      (Sandboxed execution)     │
                                                   └───────────────┬────────────────┘
                                                                   │ Proxy via Unix Socket
                                                                   ▼
                                                   ┌────────────────────────────────┐
                                                   │    proxy-host (relay.py)       │
                                                   │   Host Network <-> Socket      │
                                                   └────────────────────────────────┘
```

---

## 📁 目录结构 (Directory Structure)

```text
.
├── bridge/
│   ├── bridge.py              # OpenAI 兼容 HTTP 服务与 Codex CLI 封装核心
│   ├── image_generation.py    # 图片生成结果安全校验与提取
│   ├── relay.py               # 安全 Unix Domain Socket <-> 宿主代理中继
│   ├── supervisor.py          # 容器内多进程守护与生命周期管理
│   └── tests/                 # 离线单测套件 (40+ 测试用例)
│       ├── test_bridge.py
│       ├── test_image_generation.py
│       ├── test_relay.py
│       └── test_supervisor.py
├── bridge-compose.yaml        # Docker Compose 完整编排定义
├── docker-compose.yml         # 标准 Docker Compose 软链/别名
├── bridge.env.example         # 环境变量配置模板
├── requirements.txt           # 生产核心依赖
├── LICENSE                    # MIT 开源协议
└── README.md                  # 项目文档
```

---

## 🚀 快速开始 (Quick Start)

### 1. 准备环境与 Codex 凭据

确保你的服务器/本地环境已安装：
- Docker & Docker Compose
- 已登录认证的 OpenAI Codex CLI 运行时目录（包含 `node_modules` 与认证 Token 文件）。

### 2. 配置环境变量

从模板创建生产配置文件：

```bash
cp bridge.env.example bridge.env
```

编辑 `bridge.env` 并配置必要参数：

```env
BRIDGE_HOST=0.0.0.0
BRIDGE_PORT=18098

# 填入高强度随机 Bearer Token (至少 32 字符)
BRIDGE_TOKEN=your-ultra-secure-32-char-random-token-here

# 挂载的 Codex 可执行路径与认证根目录
CODEX_BIN=/codex/node_modules/.bin/codex
CODEX_HOME=/auth
```

### 3. 启动容器编排

使用 Docker Compose 启动桥接服务及代理中继：

```bash
docker compose -f bridge-compose.yaml up -d
```

查看运行日志，确认服务监听成功：

```bash
docker compose -f bridge-compose.yaml logs -f codex-bridge
```

### 4. 在 AstrBot 中配置模型提供商

进入 AstrBot 控制台 (WebUI) -> 服务提供商配置：

- **提供商类型**：`OpenAI` (或 OpenAI-Compatible)
- **API Base URL**：`http://codex-bridge:18098/v1`（同一 Docker 网络下）或 `http://<IP>:18098/v1`
- **API Key**：你在 `bridge.env` 中配置的 `BRIDGE_TOKEN`
- **模型名称**：`gpt-6-luna` (或在 bridge.py 中配置的模型名)

---

## ⚙️ 环境变量详解 (Configuration Reference)

| 变量名 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `BRIDGE_HOST` | `127.0.0.1` | 桥接 HTTP 服务绑定的 IP 地址 |
| `BRIDGE_PORT` | `8098` | 桥接 HTTP 服务端口（Compose 映射为 `18098`） |
| `BRIDGE_TOKEN` | *必填* | 主聊天接口认证 Bearer Token（长度 >= 32） |
| `CODEX_BIN` | `codex` | Codex CLI 可执行文件路径 |
| `CODEX_HOME` | `~/.codex` | Codex CLI 配置与认证 Token 根目录 |
| `BRIDGE_WEB_SEARCH_ENABLED` | `false` | 是否开启 Codex 原生联网搜索端点 |
| `BRIDGE_WEB_SEARCH_TOKEN` | - | 独立搜索 Token（长度 >= 32，不能与主 Token 相同） |
| `BRIDGE_WEB_SEARCH_RATE_PER_HOUR` | `20` | 单进程搜索滚动限流（次/小时） |
| `BRIDGE_IMAGE_ENABLED` | `false` | 是否开启 Codex 原生生图端点 |
| `BRIDGE_IMAGE_TOKEN` | - | 独立生图 Token（长度 >= 32，不能与其他 Token 相同） |
| `BRIDGE_IMAGE_BUDGET_DB` | - | SQLite 每日生图配额账本数据库路径 |

---

## 🧪 运行单元测试 (Running Tests)

本项目具备完整的离线测试套件，使用标准库 unittest：

```bash
# 安装测试依赖
pip install -r requirements.txt

# 运行桥接与安全组件测试
python -m unittest discover -s bridge/tests -p "test_*.py" -v
```

---

## 🔒 安全说明 (Security Policy)

- **请勿泄露 Token**：切勿将配置了真实 Token 的 `bridge.env` 提交到公开 Git 仓库。
- **只读挂载**：生产环境建议将 Codex 运行时目录 `./codex-runtime:/codex:ro` 及脚本 `./bridge:/bridge:ro` 保持为只读挂载。
- **单向代理转发**：`relay.py` 通过 Unix Domain Socket 进行代理转发，避免直接开放宿主网络导致容器逃逸或全网穿透。

---

## 📄 开源许可证 (License)

本项目采用 [MIT 许可证](LICENSE)。
