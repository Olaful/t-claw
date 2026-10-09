# t-claw — AI Agent Framework

个人AI助手，支持多 Agent 协作、记忆系统、任务调度、多媒体生成、MCP 工具协议等核心能力。

---

## 目录

- [项目简介](#项目简介)
- [技术栈](#技术栈)
- [项目结构](#项目结构)
- [环境要求](#环境要求)
- [快速开始](#快速开始)
  - [Docker Compose 一键启动](#docker-compose-一键启动)
  - [本地开发模式](#本地开发模式)
- [环境变量配置](#环境变量配置)
- [核心功能](#核心功能)
- [API 文档](#api-文档)
- [前端页面](#前端页面)
- [配置系统](#配置系统)
- [测试](#测试)
- [开发规范](#开发规范)

---

## 项目简介

t-claw 是一个面向个人使用的 AI Agent，采用前后端分离 + 异步任务队列的微服务架构。核心特性包括：

- **多 Agent 模式** — 支持单 Agent、Subagent 派生、团队协作（Team）等多种运行模式
- **记忆系统** — 短期记忆 + 长期记忆 + 会话记忆，支持自动提取与整理
- **多媒体生成** — 图片生成、语音合成（TTS）、音乐生成、视频生成，均基于 Provider 工厂模式
- **MCP 工具协议** — 完整实现 Model Context Protocol 客户端，支持 stdio / SSE / streamable-http 多种传输
- **任务调度** — 内置定时任务调度器（Scheduler）+ Celery 异步任务队列
- **斜杠命令** — 可扩展的命令系统，支持前端本地执行与后端 API 执行
- **工作区隔离** — 每个 Agent / 会话拥有独立沙箱目录，支持 session / agent / shared 三种隔离粒度
- **上下文压缩** — 多级压缩策略（微压缩 / 自动压缩 / 响应式压缩），有效控制 Token 消耗
- **PDF 解析** — 支持 MinerU 云端解析与 pymupdf 本地解析双 Provider
- **扩展工具** — 从 `.t-claw/extensions/` 目录动态加载自定义工具

---

## 技术栈

| 层级 | 技术 |
|------|------|
| **后端框架** | Python 3.12 + FastAPI + Uvicorn |
| **数据库** | PostgreSQL 16（asyncpg 异步驱动） |
| **缓存/消息** | Redis 7 |
| **任务队列** | Celery 5 + Redis Broker |
| **ORM** | SQLAlchemy 2.0（异步模式） |
| **LLM 接入** | OpenAI SDK + Anthropic SDK |
| **MCP 客户端** | mcp SDK 1.29.1 |
| **日志** | structlog |
| **前端框架** | Vue 3.5 + TypeScript + Vite 5 |
| **UI 组件库** | Lit 3（Web Components） |
| **状态管理** | Pinia 2 |
| **路由** | Vue Router 4（Hash 模式） |
| **样式** | TailwindCSS 3 |
| **容器化** | Docker + Docker Compose |

---

## 项目结构

```
t-claw/
├── backend/                    # 后端服务
│   ├── app/
│   │   ├── agents/             # Agent 模式（base / single / multi / subagent）
│   │   ├── api/v1/             # REST API 路由
│   │   │   ├── agents.py       #   Agent 管理
│   │   │   ├── chat.py         #   对话 / SSE 流
│   │   │   ├── tools.py        #   工具管理
│   │   │   ├── teams.py        #   团队协作
│   │   │   ├── tasks.py        #   任务管理
│   │   │   ├── memory.py       #   记忆系统
│   │   │   ├── events.py       #   事件订阅
│   │   │   ├── jobs.py         #   后台作业
│   │   │   ├── slash_commands.py # 斜杠命令
│   │   │   └── settings.py     #   配置管理
│   │   ├── core/               # 核心模块
│   │   │   ├── memory_system/  #   记忆系统（存储 / 提取 / 整理）
│   │   │   ├── scheduler/      #   定时任务调度器
│   │   │   ├── task_system/    #   文件型任务系统
│   │   │   ├── team_system/    #   团队协作系统
│   │   │   ├── compressor.py   #   上下文压缩
│   │   │   ├── planner.py      #   规划器
│   │   │   ├── reflector.py    #   反思器
│   │   │   ├── prompt_engine.py #  提示词引擎
│   │   │   ├── event_bus.py    #   事件总线
│   │   │   └── ...
│   │   ├── models/             # 数据模型（ORM + Schema）
│   │   ├── services/           # 业务服务层
│   │   ├── tools/              # 工具系统
│   │   │   ├── builtin_tools.py #  内置工具（web_search / bash / file 等）
│   │   │   ├── image_generate.py # 图片生成
│   │   │   ├── music_generate.py # 音乐生成
│   │   │   ├── video_generate.py # 视频生成
│   │   │   ├── pdf_parse.py    #   PDF 解析
│   │   │   ├── tts/            #   TTS 语音合成
│   │   │   ├── mcp/            #   MCP 协议客户端
│   │   │   ├── image_providers/  # 图片 Provider
│   │   │   ├── music_providers/  # 音乐 Provider
│   │   │   ├── video_providers/  # 视频 Provider
│   │   │   ├── pdf_providers/    # PDF Provider
│   │   │   ├── registry.py     #   工具注册表
│   │   │   └── ...
│   │   ├── skills/             # 技能加载器
│   │   └── utils/              # 工具函数（数据库 / Redis / 日志）
│   ├── migrations/             # 数据库迁移脚本
│   ├── tests/                  # 单元测试
│   ├── main.py                 # FastAPI 应用入口
│   ├── celery_app.py           # Celery 配置
│   ├── config.py               # （app/config.py 为实际配置模块）
│   ├── requirements.txt        # Python 依赖
│   └── Dockerfile
├── frontend/
│   ├── ui/                     # Lit Web Components 组件库
│   │   ├── src/components/     #   基础 UI 组件
│   │   ├── src/layouts/        #   布局组件
│   │   └── package.json
│   ├── vue/                    # Vue 前端应用
│   │   ├── src/
│   │   │   ├── pages/          #   页面（Dashboard / Chat / Agents / Teams / Memory / Settings）
│   │   │   ├── components/     #   Vue 组件
│   │   │   ├── composables/    #   组合式函数
│   │   │   ├── stores/         #   Pinia 状态管理
│   │   │   └── router/         #   路由配置
│   │   └── package.json
│   └── Dockerfile
├── .t-claw/                # 框架配置与运行时目录
│   ├── settings.json           #   主配置文件
│   ├── workspace/              #   工作区（Agent / 会话 / 子Agent 沙箱）
│   ├── memory/                 #   记忆存储
│   ├── agents/                 #   Agent 配置
│   ├── extensions/             #   扩展工具
│   ├── plugin-mcp/             #   MCP 插件配置
│   ├── plugin-skills/          #   技能插件
│   ├── tasks/                  #   文件型任务
│   ├── teams/                  #   团队配置
│   ├── jobs/                   #   后台作业
│   └── scheduled-tasks/        #   定时任务
├── docker-compose.yml          # Docker Compose 编排
├── .env.example                # 环境变量模板
└── .gitignore
```

---

## 环境要求

| 组件 | 最低版本 | 说明 |
|------|----------|------|
| Python | 3.12+ | 后端运行环境 |
| Node.js | 20+ | 前端构建环境 |
| PostgreSQL | 16 | 主数据库 |
| Redis | 7 | 缓存 + 消息队列 |
| Docker & Docker Compose | 可选 | 容器化部署 |

---

## 快速开始

### Docker Compose 一键启动

```bash
# 1. 克隆项目
git clone <repository-url>
cd t-claw

# 2. 复制并编辑环境变量
cp .env.example backend/.env
# 编辑 backend/.env，填入数据库密码、LLM API Key 等

# 3. 启动全部服务
docker-compose up -d

# 4. 查看日志
docker-compose logs -f
```

服务启动后访问地址：

| 服务 | 地址 | 说明 |
|------|------|------|
| 前端应用 | http://localhost:5173 | Vue 开发服务器 |
| 后端 API | http://localhost:8000 | FastAPI 服务 |
| API 文档 | http://localhost:8000/docs | Swagger UI |
| 健康检查 | http://localhost:8000/health | 服务状态 |
| PostgreSQL | localhost:5432 | 数据库 |
| Redis | localhost:6379 | 缓存 |

### 本地开发模式

#### 1. 启动基础设施

```bash
# 仅启动 PostgreSQL 和 Redis
docker-compose up -d postgres redis
```

#### 2. 启动后端

```bash
cd backend

# 安装依赖
pip install -r requirements.txt

# 复制并编辑环境变量
cp ../.env.example .env

# 启动后端服务
python main.py
```

后端默认监听 `0.0.0.0:8000`，开启 `DEBUG=true` 时支持热重载。

#### 3. 启动 Celery Worker（可选）

```bash
cd backend

# 启动 Worker（处理异步任务）
celery -A celery_app worker --loglevel=info

# 启动 Beat（处理定时任务，另开终端）
celery -A celery_app beat --loglevel=info
```

#### 4. 启动前端

```bash
# 先构建 UI 组件库
cd frontend/ui
npm install
npm run build

# 启动 Vue 开发服务器
cd ../vue
npm install
npm run dev
```

前端开发服务器运行在 `http://localhost:5173`，API 请求通过 Vite 代理转发至后端 `localhost:8000`。

---

## 环境变量配置

所有环境变量参见 [.env.example](.env.example)，核心配置项如下：

```bash
# 应用配置
APP_NAME=AI Agent Framework
DEBUG=false
HOST=0.0.0.0
PORT=8000
API_PREFIX=/api/v1

# 数据库
DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/agent_framework

# Redis
REDIS_URL=redis://localhost:6379/0

# Celery（Broker 和 Backend 使用不同 DB 编号避免冲突）
CELERY_BROKER_URL=redis://localhost:6379/1
CELERY_RESULT_BACKEND=redis://localhost:6379/2

# LLM 配置
OPENAI_API_KEY=your-openai-api-key
OPENAI_BASE_URL=https://api.openai.com/v1
ANTHROPIC_API_KEY=your-anthropic-api-key
DEFAULT_LLM_MODEL=gpt-4-turbo-preview

# Agent 配置
MAX_CONCURRENT_AGENTS=100
AGENT_TIMEOUT_SECONDS=3600

# 记忆配置
MEMORY_ENABLED=true
SHORT_TERM_MEMORY_SIZE=50

# 上下文压缩
CONTEXT_WINDOW_SIZE=128000
COMPRESSION_THRESHOLD=0.8
```

### 配置优先级

配置来源优先级（高到低）：

1. **环境变量** / `backend/.env` 文件
2. **`.t-claw/settings.json`** 配置文件

`settings.json` 支持嵌套 JSON 结构，可通过点号访问（如 `settings.agent.subagent.enabled`），同时自动扁平化为大写键（如 `AGENT_SUBAGENT_ENABLED`）。

### .t-claw 目录

框架使用 `.t-claw/` 目录存储运行时配置与数据，路径解析优先级：

1. 环境变量 `HAPPYCLAW_HOME`
2. `backend/.env` 中的 `HAPPYCLAW_HOME`
3. 默认值 `~/.t-claw`

项目根目录下的 `.t-claw/` 包含默认配置，首次启动时会自动拷贝到 `HAPPYCLAW_HOME`。

---

## 核心功能

### Agent 模式

| 模式 | 说明 |
|------|------|
| **Single Agent** | 单 Agent 对话，支持工具调用 |
| **Multi Agent** | 多 Agent 并行/串行协作 |
| **Subagent** | Agent 可派生子 Agent，最大嵌套层级可配（默认 2 层） |
| **Team** | 团队协作模式，支持 Lead Agent 分配任务给队友 |

### 记忆系统

- **短期记忆** — 基于滑动窗口的近期对话记忆
- **长期记忆** — 持久化存储，支持自动提取与定期整理
- **会话记忆** — 单次会话内的上下文摘要，支持自然暂停触发更新
- **记忆预提取** — 独立 LLM 配置，在对话开始时预加载相关记忆

### 多媒体生成

所有多媒体功能均采用 Provider 工厂模式，易于扩展：

| 功能 | 内置 Provider | 说明 |
|------|--------------|------|
| **图片生成** | OpenAI Images / HTTP 通用 | 支持 DALL-E 等模型 |
| **语音合成** | Edge TTS / MiniMax / 阿里云 | 支持流式输出、多音色 |
| **音乐生成** | 阿里云 / 音超 | 支持歌词生成、多首独立返回 |
| **视频生成** | 阿里云 / MiniMax | 支持多种视频模型 |
| **PDF 解析** | MinerU / Local (pymupdf) | 云端高精度 / 本地离线解析 |

### 工具系统

- **内置工具** — web_search、bash_command、read_file、write_file、todo_write 等
- **MCP 工具** — 通过 MCP 协议接入外部工具服务器
- **扩展工具** — 从 `.t-claw/extensions/` 目录动态加载
- **技能系统** — 从 `.t-claw/plugin-skills/` 加载 Markdown 格式的技能定义
- **工具搜索** — 可选的语义工具搜索，帮助 Agent 从大量工具中精准选择
- **权限控制** — 工具级别的权限管理，支持按 Agent / 层级禁用

### 任务与调度

- **文件型任务系统** — 基于文件的任务存储，支持并发认领与文件监听
- **定时调度器** — Cron 表达式支持，带抖动防惊群
- **Celery 异步任务** — 长时间运行的 Agent 任务与定时任务
- **后台作业** — 记忆提取/整理等后台任务，支持并发控制

### 斜杠命令

可扩展的命令系统，格式 `/command arg1 arg2`：

- 前端命令选择框 + 参数候选输入
- 纯前端本地执行 或 调用后端 API
- 消息类型 `slash_command_request/response`，入库但不发送给 LLM

### 上下文压缩

多级压缩策略，在 Token 预算内最大化保留有效信息：

| 策略 | 触发条件 | 说明 |
|------|----------|------|
| **微压缩** | TTL 过期 | 对白名单工具结果进行紧凑化 |
| **自动压缩** | 达到阈值 (85%) | 警告 → 强制压缩 (95%) |
| **响应式压缩** | LLM 返回上下文超长错误 | 自动重试压缩后重新发送 |

---

## API 文档

启动后端后访问 `http://localhost:8000/docs` 查看完整的 Swagger API 文档。

主要 API 分组：

| 路径前缀 | 说明 |
|----------|------|
| `/api/v1/agents` | Agent CRUD、配置管理 |
| `/api/v1/chat` | 对话管理、SSE 流式消息 |
| `/api/v1/tools` | 工具列表、启用/禁用 |
| `/api/v1/teams` | 团队创建、成员管理 |
| `/api/v1/tasks` | 任务提交、状态查询 |
| `/api/v1/memory` | 记忆查看、提取触发 |
| `/api/v1/events` | 事件订阅（SSE） |
| `/api/v1/jobs` | 后台作业管理 |
| `/api/v1/slash-commands` | 斜杠命令列表与执行 |
| `/api/v1/settings` | 运行时配置管理 |

---

## 前端页面

| 路由 | 页面 | 说明 |
|------|------|------|
| `/` | Dashboard | 仪表盘总览 |
| `/chat/:agentId?` | Chat | 对话界面（SSE 流式） |
| `/agents` | Agents | Agent 管理 |
| `/teams` | Teams | 团队协作管理 |
| `/memory` | Memory | 记忆系统管理 |
| `/settings` | Settings | 系统配置（LLM / 工具 / TTS 等） |

前端使用 Vue 3 + Pinia 状态管理，UI 组件基于 Lit Web Components 构建。

---

## 配置系统

主配置文件为 `.t-claw/settings.json`，支持以下主要配置区块：

```jsonc
{
  "agent": {
    "defaults": {
      "sandbox": { "scope": "session" }  // 隔离粒度
    },
    "subagent": {
      "enabled": true,
      "max_spawn_level": 2,          // 最大派生层级
      "max_concurrent": 5,           // 最大并发子Agent
      "allowed_tools_per_level": {}  // 各层级可用工具
    }
  },
  "tts": {
    "auto": "always",                // off/always/inbound/tagged
    "provider": "edge_tts",
    "stream_mode": "two-way"
  },
  "pdf": {
    "provider": "mineru",
    "providers": { ... }
  },
  "tools": { ... },                  // 工具配置
  "logging": { ... },                // 日志配置
  "memory": { ... }                  // 记忆配置
}
```

配置支持运行时通过 API 修改（`/api/v1/settings`），修改后自动刷新内存缓存。

---

## 测试

```bash
cd backend

# 运行全部测试
pytest

# 运行特定测试
pytest tests/test_mcp_manager.py
pytest tests/test_memory.py
pytest tests/test_event_bus.py

# 带覆盖率
pytest --cov=app --cov-report=html
```

---

## 开发规范

### 后端

- 使用 `structlog` 进行结构化日志记录
- 所有数据库操作使用异步模式（`async/await`）
- Provider 工厂模式：多媒体工具统一通过 Factory 注册与创建
- 配置统一通过 `settings` 对象访问，支持点号和大写两种风格

### 前端

- Vue 3 Composition API + `<script setup>` 风格
- 状态管理使用 Pinia Store
- UI 组件库基于 Lit，构建产物供 Vue 应用引用
- API 请求基础路径：`/api/v1`

### 提交规范

建议遵循 Conventional Commits：

```
feat: 新增 xxx 功能
fix: 修复 xxx 问题
refactor: 重构 xxx 模块
docs: 更新文档
test: 补充测试用例
```

---

## License

Private — All rights reserved.
