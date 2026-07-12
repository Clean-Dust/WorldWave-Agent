<p align="center">
  <br>
  <img alt="Worldwave" src="data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSI2MDAiIGhlaWdodD0iMTIwIiB2aWV3Qm94PSIwIDAgNjAwIDEyMCI+PHJlY3Qgd2lkdGg9IjYwMCIgaGVpZ2h0PSIxMjAiIGZpbGw9IiMwZTBlMGUiLz48dGV4dCB4PSIzMDAiIHk9IjUwIiBmb250LWZhbWlseT0iQXJpYWwsIHNhbnMtc2VyaWYiIGZvbnQtc2l6ZT0iMzYiIGZpbGw9IiM2MGE1ZmEiIHRleHQtYW5jaG9yPSJtaWRkbGUiIGZvbnQtd2VpZ2h0PSJib2xkIj7imp3imJ0gV09STERXQVZFPC90ZXh0Pjx0ZXh0IHg9IjMwMCIgeT0iOTAiIGZvbnQtZmFtaWx5PSJBcmlhbCwgc2Fucy1zZXJpZiIgZm9udC1zaXplPSIxOCIgZmlsbD0iIzg4ODg4OCIgdGV4dC1hbmNob3I9Im1pZGRsZSI+cGVyc2lzdGVudCBjb2dudGl0aXZlIGVudGl0eSBmcmFtZXdvcms8L3RleHQ+PC9zdmc+" width="600" height="120" alt="Worldwave">
  <br>
</p>

<p align="center">
  <a href="https://github.com/Clean-Dust/worldwave/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue" alt="License"></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.10+-blue" alt="Python"></a>
  <a href="#"><img src="https://img.shields.io/badge/version-0.8.0-informational" alt="Version"></a>
  <a href="#"><img src="https://img.shields.io/badge/tests-584_passed-brightgreen" alt="Tests"></a>
  <a href="#"><img src="https://img.shields.io/badge/tools-192-orange" alt="Tools"></a>
  <a href="#"><img src="https://img.shields.io/badge/modules-213-blueviolet" alt="Modules"></a>
</p>

<p align="center">
  <a href="#english">English</a> &nbsp;|&nbsp;
  <a href="#chinese">简体中文</a>
</p>

---

<h2 id="english">Worldwave</h2>

A **persistent cognitive entity** framework. One agent, one timeline — regardless of which platform you use to talk to it.

Every AI agent framework today (Claude Code, Codex, OpenClaw, Hermes) is session-based: close the window, lose the context. Worldwave models the agent as a **continuously running entity** with its own memory, identity, and state that survives restarts.

### What makes this different

| Existing approach | Worldwave |
|---|---|
| **Session-based** — state dies with the process | **Entity-based** — state persists across restarts, platforms, and time |
| **Passive memory** — agent receives RAG results, cannot edit what it knows | **Self-editing memory** — agent calls `remember()` / `forget()` to manage its own knowledge base |
| **Single-platform** — Telegram bot ≠ terminal agent | **Cross-platform identity** — all platforms resolve to the same `entity_id`, sharing one timeline |
| **Stateless worker** — each request spawns a fresh context | **State machine** — auto-hydrates on message, auto-persists on idle, sleeps when unused |
| **Flat vector search** — facts conflict with no time dimension | **Temporal knowledge graph** — facts have `valid_from`/`valid_until`/`superseded_by`; outdated facts are superseded, not deleted |
| **External graph DB** (Neo4j) required for multi-hop reasoning | **SQLite CTE** recursive queries for typed edge traversal — zero extra dependencies |

Currently serving 13 messaging platforms, 192 tools across 16 categories, with P2P node gossip in active testing.

### Architecture

```
                    ┌─────────────────────────────┐
                    │       Platform Layer          │
                    │  Terminal │ Telegram │ HTTP   │
                    │  Discord  │  Feishu  │ Slack  │
                    │  Signal   │  WeChat  │ 更多…  │
                    └────────────┬────────────────┘
                                 ▼
                    ┌─────────────────────────────┐
                    │     IdentityResolver         │
                    │  all platform IDs → entity_id │
                    │  SQLite, auto-create on first │
                    │  contact from any platform    │
                    └────────────┬────────────────┘
                                 ▼
                    ┌─────────────────────────────┐
                    │   Entity State Machine        │
                    │  ┌───────────────────────┐   │
                    │  │ working_memory (agent  │   │
                    │  │ can self-edit)         │   │
                    │  │ preferences            │   │
                    │  │ last_context summary   │   │
                    │  │ active_goal            │   │
                    │  └───────────────────────┘   │
                    │  hydrate ←→ persist ←→ sleep │
                    └────────────┬────────────────┘
                                 ▼
                    ┌─────────────────────────────┐
                    │   Spiral Cognitive Loop       │
                    │  PERCEIVE → RECALL → PLAN    │
                    │    → ACT → EVALUATE           │
                    │    → LEARN → CONSOLIDATE      │
                    │  (7 phase, LLM-driven)        │
                    └────────────┬────────────────┘
                                 ▼
        ┌───────────┬───────────┼───────────┬───────────┐
        ▼           ▼           ▼           ▼           ▼
   ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐
   │ Memory  │ │Tools 192│ │Subcon-  │ │P2P Net  │ │Coding   │
   │ 8-layer │ │16 cats  │ │scious   │ │Federated│ │Engine   │
   │ w/tempo-│ │Auto-    │ │Local ML │ │Gossip   │ │Defensive│
   │ ral KG  │ │registry │ │700KB NN │ │Merkle   │ │Edit+AST │
   └─────────┘ └─────────┘ └─────────┘ └─────────┘ └─────────┘
```

### Demo: cross-platform continuity

```
# Terminal — start a task
$ ww run "I'm working on a Python project called 'nexus'"

  [Entity: ent_a1b2c3] Context loaded (1 prior interaction)
  Worldwave: Got it. I'll remember the project name. What's the focus area?

$ ww run "It's a CLI tool for managing Docker containers"

  Worldwave: Understood — 'nexus' is a Docker CLI tool.
  *calls remember(key='project_name', value='nexus')*
  *calls remember(key='project_focus', value='Docker CLI tool')*

# ... later, from Telegram ...
User: What was that project I mentioned earlier?

  [Entity: ent_a1b2c3] Context loaded — last interaction 8 min ago via terminal
  Worldwave: You're working on 'nexus', a CLI tool for managing Docker containers.
  You mentioned it about 8 minutes ago in the terminal. Need help with anything specific?
```

The agent knew because `remember()` stored the facts in entity state, and entity state is loaded regardless of which platform the next message comes from.

### Quick Start

**Requirements:** Python 3.10+, 512 MB RAM (idle), 2 GB RAM (under load). No GPU required.

```bash
# Clone and install
git clone https://github.com/Clean-Dust/worldwave.git
cd worldwave
pip install -e .

# Configure — at minimum, set one LLM API key
cp .env.example .env
# Edit .env:
#   DEEPSEEK_API_KEY=sk-...    (DeepSeek V4 Flash/Pro)
#   ANTHROPIC_API_KEY=sk-...   (optional)
#   OPENAI_API_KEY=sk-...      (optional)

# Run a single task
ww run "Hello, what can you do?"
```

**Server mode** (API + Web UI + Telegram gateway):

```bash
python server.py
# → API:        http://localhost:9300
# → Web UI:     http://localhost:9300/ww/webui/
# → API docs:   http://localhost:9300/docs

# With Telegram gateway:
#   Set TELEGRAM_WW_TOKEN in .env, restart server
```

**One-click deployment** on any Linux/macOS machine:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/Clean-Dust/worldwave/main/deploy.sh)
```

**Platform-specific notes:**

| Platform | Status | Notes |
|----------|--------|-------|
| Linux (x86_64) | Full support | Primary target. Ubuntu 22.04+, Debian 12+ |
| macOS (Apple Silicon) | Supported | `pip install -e .` works. `browser` extras need `playwright install` |
| Windows (WSL2) | Supported | Run inside WSL2. Native Windows packaging pending |
| Windows (native) | Partial | `pip install -e .` works. Some tools (shell, sandbox) degraded |

**Optional dependencies:**

```bash
pip install -e ".[browser]"     # Playwright-based web automation
pip install -e ".[nats]"        # NATS JetStream for distributed queue
pip install -e ".[stt]"         # Whisper speech-to-text
pip install -e ".[test]"        # pytest + test dependencies
pip install -e ".[all]"         # Everything
```

### Feature Overview

| Category | Modules | Description |
|----------|---------|-------------|
| **Cognitive Engine** | `core/loop.py` | 7-phase spiral: perceive, recall, plan, act, evaluate, learn, consolidate |
| **Entity Continuity** | `wavegate/identity.py`, `core/entity_state.py` | Cross-platform identity resolution, persistent state with auto-hydration/sleep |
| **Memory System** | `core/memory/` (12 modules) | Hippocampus buffer, amygdala scoring, sleep consolidation, reconsolidation, recall with spreading activation, temporal validity tracking, typed knowledge graph edges with CTE multi-hop traversal |
| **Self-Editing Memory** | `core/memory/tools.py` | Agent calls `remember()` / `forget()` / `recall_mine()` to manage its own knowledge |
| **Gateway** | `gateway/` (25 modules) | Adapters for Telegram, Discord, Slack, Signal, WeChat, Feishu, DingTalk, WhatsApp, LINE, Matrix, Webhook |
| **Tools** | `tools/registry.py` | 192 tools across 16 categories: shell, file, web, browser, memory, coding, contacts, scheduling, platform messaging, MCP bridge, credentials, plugins, hooks, slash commands, voice, computer use |
| **Subconscious** | `core/subconscious/` (30 modules) | Local ML: DeepRiskNet (~700 KB), decision trees, PPO, CFR, nighttime schema induction. Reads numerical features only — never raw conversations |
| **P2P Network** | `p2p/` (16 modules) | Node discovery (mDNS, DHT, HTTP tracker), Merkle chain for model provenance, gossip protocol, NAT relay, Nostr relay pool. Mining optional, off by default |
| **Coding Engine** | `coding/` (12+ modules) | Defensive code editing with backup/rollback, AST-aware search (ast-grep), progressive loading for large repos, capability mutex for concurrent edits |
| **Computer Use** | `core/computer_use/` (12 modules) | 7-tier progressive screen capture, UI Automation tree, set-of-mark visual grounding, browser stealth control, vision loop |
| **Biomimetic** | `core/` (45 modules) | Global workspace (7-item capacity), basal ganglia (Go/NoGo gate), circadian rhythm, cascade bus, predictive model, skill solidification, self-model introspection |
| **Sandbox** | `sandbox/` | Isolated execution for untrusted code |
| **CLI** | `ww_cli.py` | `ww run`, `ww chat`, `ww mascot`, `ww pairing`, `ww tools`, `ww config`, `ww deploy` |
| **Plugins** | `core/plugins.py` | Plugin marketplace: discover, install, enable/disable lifecycle |

### Design Decisions

- **Zero external ML dependencies for core.** DeepRiskNet, blockchain, P2P, memory scoring — all Python stdlib. No numpy, sklearn, or torch required.
- **Everything optional, everything off by default.** Mining, self-hosting, auto-evolution — disabled until explicitly enabled in config.
- **No external graph database.** Knowledge graph edges use SQLite CTE recursive queries. No Neo4j, no Redis required.
- **Single execution path.** `ww run` and `ww chat` use identical code path — no dual-mode complexity.
- **Entity-first.** The framework models a persistent identity, not a disposable chat session.
- **Agent manages its own memory.** The agent calls `remember()` / `forget()` as tools — it decides what to retain, not a background heuristic.

### Roadmap

| Version | Focus | Status |
|---------|-------|--------|
| v0.8 | Persistent cognitive entity, self-editing memory, temporal KG | ✓ Done |
| v0.9 | MCP Bridge full support, Web UI streaming, Docker sandbox hardening | In progress |
| v1.0 | Multi-node P2P auto-discovery, model gossip end-to-end tested, production deployment guide | Planned |
| v1.5 | Public benchmarks vs OpenClaw / Claude Code / Codex, plugin registry | Planned |

### Contributing

Areas where help is most needed:

- **Multi-node P2P testing** — the gossip/federation layer has full unit test coverage but needs real multi-machine validation
- **Windows native packaging** — currently WSL2-only; a native Windows launcher is needed
- **Gateway adapter contributions** — additional platform adapters (WhatsApp Business API, Microsoft Teams, etc.)
- **Performance profiling** — memory usage under sustained load, token budget optimization

For bug reports and feature requests, open an issue. For code contributions, please:

1. Fork the repo
2. Create a feature branch
3. Ensure `pytest tests/` passes (584 tests)
4. Open a PR with a clear description

### License

MIT — see [LICENSE](LICENSE).

---

<h2 id="chinese">Worldwave · 简体中文</h2>

一个**持久认知主体**（Persistent Cognitive Entity）框架。无论你从终端、Telegram、飞书还是网页接入，连接的都是同一个实体、同一条时间线。

当前所有 AI 代理框架（Claude Code、Codex、OpenClaw、Hermes）都基于会话：关闭窗口就丢失上下文。Worldwave 将代理建模为一个**持续运行的实体**，拥有自己的记忆、身份和状态，重启不丢失。

### 为什么不同

| 现有方案 | Worldwave |
|---|---|
| **基于会话** — 进程结束，状态消失 | **基于实体** — 状态跨重启、跨平台、跨时间持久存在 |
| **被动记忆** — 代理接收 RAG 结果，无法编辑所知 | **自主编辑记忆** — 代理调用 `remember()` / `forget()` 管理自己的知识库 |
| **单平台** — Telegram bot ≠ 终端代理 | **跨平台统一身份** — 所有平台解析为同一 `entity_id`，共享单条时间线 |
| **无状态 worker** — 每次请求重新构建上下文 | **状态机** — 收到消息自动唤醒载入，空闲自动持久化休眠 |
| **扁平向量搜索** — 事实冲突，无时间维度 | **时态知识图谱** — 事实有 `valid_from`/`valid_until`/`superseded_by`；过时事实标记取代而非删除 |
| **需要外部图数据库**（Neo4j）进行多跳推理 | **SQLite CTE** 递归查询实现类型化边遍历 — 零额外依赖 |

目前已接入 13 个即时通讯平台，192 个工具覆盖 16 个类别，P2P 节点 gossip 正在测试中。

### 架构

```
                    ┌─────────────────────────────┐
                    │        平台接入层             │
                    │  终端机 │ Telegram │ HTTP    │
                    │  Discord│  飞书    │ Slack   │
                    │  Signal │  微信    │ 更多…   │
                    └────────────┬────────────────┘
                                 ▼
                    ┌─────────────────────────────┐
                    │     IdentityResolver         │
                    │  所有平台 ID → entity_id      │
                    │  SQLite 持久化，首次接触      │
                    │  任何平台即自动创建实体        │
                    └────────────┬────────────────┘
                                 ▼
                    ┌─────────────────────────────┐
                    │   Entity State Machine        │
                    │  ┌───────────────────────┐   │
                    │  │ working_memory（代理    │   │
                    │  │ 可自主编辑）            │   │
                    │  │ preferences 偏好设置    │   │
                    │  │ last_context 上次摘要   │   │
                    │  │ active_goal 当前任务    │   │
                    │  └───────────────────────┘   │
                    │  载入 ←→ 持久化 ←→ 休眠      │
                    └────────────┬────────────────┘
                                 ▼
                    ┌─────────────────────────────┐
                    │   螺旋认知循环（7 阶段）       │
                    │  感知 → 回忆 → 规划           │
                    │    → 行动 → 评估              │
                    │    → 学习 → 巩固              │
                    │  （LLM 驱动，每阶段可检查点）   │
                    └────────────┬────────────────┘
                                 ▼
        ┌───────────┬───────────┼───────────┬───────────┐
        ▼           ▼           ▼           ▼           ▼
   ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐
   │ 记忆系统 │ │192 工具 │ │ 潜意识  │ │ P2P 网络│ │ 编程引擎 │
   │ 8 层记忆 │ │16 类别  │ │ 本地 ML │ │ 联邦学习│ │ 防御编辑 │
   │ 时态图谱 │ │自动注册 │ │ 700KB   │ │ Gossip  │ │ AST搜索 │
   └─────────┘ └─────────┘ └─────────┘ └─────────┘ └─────────┘
```

### 演示：跨平台连续性

```
# 终端 — 开启一个任务
$ ww run "我在做一个叫 'nexus' 的 Python 项目"

  [Entity: ent_a1b2c3] 上下文已载入（1 次历史交互）
  Worldwave: 好的，我记住了项目名。主要做什么的？

$ ww run "是个管理 Docker 容器的 CLI 工具"

  Worldwave: 明白了 — 'nexus' 是一个 Docker CLI 工具。
  *调用 remember(key='project_name', value='nexus')*
  *调用 remember(key='project_focus', value='Docker CLI 工具')*

# ... 过了一会，从 Telegram 发消息 ...
用户: 我之前提到的那个项目叫什么来着？

  [Entity: ent_a1b2c3] 上下文已载入 — 上次交互 8 分钟前，来自终端
  Worldwave: 你在做 'nexus'，一个管理 Docker 容器的 CLI 工具。
  大约 8 分钟前在终端里提到的。需要具体帮什么吗？
```

代理之所以记得，是因为 `remember()` 将事实存入了实体状态，而实体状态在下次任何平台发来消息时都会被载入。

### 快速开始

**硬件要求：** Python 3.10+，空闲 512 MB 内存，负载下 2 GB。不需要 GPU。

```bash
# 克隆并安装
git clone https://github.com/Clean-Dust/worldwave.git
cd worldwave
pip install -e .

# 配置 — 最少设置一个 LLM API key
cp .env.example .env
# 编辑 .env：
#   DEEPSEEK_API_KEY=sk-...    (DeepSeek V4 Flash/Pro)
#   ANTHROPIC_API_KEY=sk-...   (可选)
#   OPENAI_API_KEY=sk-...      (可选)

# 执行单次任务
ww run "你好，你能做什么？"
```

**服务器模式**（API + Web UI + Telegram 网关）：

```bash
python server.py
# → API:        http://localhost:9300
# → Web UI:     http://localhost:9300/ww/webui/
# → API 文档:   http://localhost:9300/docs

# 启用 Telegram 网关：
#   在 .env 中设置 TELEGRAM_WW_TOKEN，重启服务器
```

**一键部署**（任何 Linux/macOS 机器）：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/Clean-Dust/worldwave/main/deploy.sh)
```

**平台注意事项：**

| 平台 | 状态 | 备注 |
|------|------|------|
| Linux (x86_64) | 完整支持 | 主要目标平台。Ubuntu 22.04+、Debian 12+ |
| macOS (Apple Silicon) | 支持 | `pip install -e .` 可用。`browser` 扩展需要 `playwright install` |
| Windows (WSL2) | 支持 | 在 WSL2 内运行。原生 Windows 打包待开发 |
| Windows (原生) | 部分支持 | `pip install -e .` 可用。部分工具（shell、sandbox）功能受限 |

**可选依赖：**

```bash
pip install -e ".[browser]"     # Playwright 网页自动化
pip install -e ".[nats]"        # NATS JetStream 分布式队列
pip install -e ".[stt]"         # Whisper 语音转文字
pip install -e ".[test]"        # pytest 及测试依赖
pip install -e ".[all]"         # 全部安装
```

### 功能概览

| 类别 | 模块 | 说明 |
|------|------|------|
| **认知引擎** | `core/loop.py` | 7 阶段螺旋循环：感知、回忆、规划、行动、评估、学习、巩固 |
| **实体连续性** | `wavegate/identity.py`, `core/entity_state.py` | 跨平台身份解析，带自动休眠/唤醒的持久状态 |
| **记忆系统** | `core/memory/`（12 个模块） | 海马体缓冲、杏仁核情感评分、睡眠巩固、再巩固、扩散激活回忆、时间有效性追踪、带 CTE 多跳遍历的类型化知识图谱边 |
| **自主记忆编辑** | `core/memory/tools.py` | 代理调用 `remember()` / `forget()` / `recall_mine()` 管理自有知识 |
| **网关** | `gateway/`（25 个模块） | Telegram、Discord、Slack、Signal、微信、飞书、钉钉、WhatsApp、LINE、Matrix、Webhook 适配器 |
| **工具系统** | `tools/registry.py` | 192 个工具，16 个类别：shell、文件、网络、浏览器、记忆、编程、联系人、调度、平台消息、MCP 桥接、凭证、插件、钩子、斜杠命令、语音、桌面操作 |
| **潜意识** | `core/subconscious/`（30 个模块） | 本地 ML：DeepRiskNet（约 700 KB）、决策树、PPO、CFR、夜间模式归纳。仅读取数值特征——不读原始对话 |
| **P2P 网络** | `p2p/`（16 个模块） | 节点发现（mDNS、DHT、HTTP tracker）、Merkle 链模型溯源、gossip 协议、NAT 中继、Nostr 中继池。挖矿可选，默认关闭 |
| **编程引擎** | `coding/`（12+ 个模块） | 带备份/回滚的防御性代码编辑、AST 感知搜索（ast-grep）、大仓库渐进式加载、并发编辑能力互斥锁 |
| **桌面操作** | `core/computer_use/`（12 个模块） | 7 层级渐进式屏幕捕获、UI Automation 树提取、标记式视觉定位、浏览器隐身控制、视觉闭环 |
| **仿生模块** | `core/`（45 个模块） | 全局工作区（7 项容量）、基底核（Go/NoGo 动作门控）、昼夜节律、级联总线、预测模型、技能固化、自我模型内省 |
| **沙箱** | `sandbox/` | 不受信任代码的隔离执行环境 |
| **CLI** | `ww_cli.py` | `ww run`、`ww chat`、`ww mascot`、`ww pairing`、`ww tools`、`ww config`、`ww deploy` |
| **插件** | `core/plugins.py` | 插件市场：发现、安装、启用/禁用生命周期 |

### 设计决策

- **核心零外部 ML 依赖。** DeepRiskNet、区块链、P2P、记忆评分——全部 Python 标准库实现。不需要 numpy、sklearn、torch。
- **所有功能可选，默认关闭。** 挖矿、自托管、自动进化——全部默认禁用，需显式启用。
- **无外部图数据库。** 知识图谱边关系使用 SQLite CTE 递归查询。不需要 Neo4j，不需要 Redis。
- **单一执行路径。** `ww run` 与 `ww chat` 走同一代码路径——无双重模式复杂性。
- **实体优先。** 框架建模的是持久身份，而非一次性聊天线程。
- **代理自主管理记忆。** 代理通过 `remember()` / `forget()` 工具自行决定保留什么——而非背景启发式规则。

### 路线图

| 版本 | 重点 | 状态 |
|------|------|------|
| v0.8 | 持久认知主体、自主记忆编辑、时态知识图谱 | ✓ 已完成 |
| v0.9 | MCP Bridge 完整支持、Web UI 流式响应、Docker 沙箱加固 | 进行中 |
| v1.0 | 多节点 P2P 自动发现、model gossip 端到端测试、生产部署指南 | 计划中 |
| v1.5 | 公开基准测试对比 OpenClaw / Claude Code / Codex、插件注册表 | 计划中 |

### 参与贡献

最需要帮助的领域：

- **多节点 P2P 测试** — gossip/联邦层有完整单元测试，但需要真实多机验证
- **Windows 原生打包** — 目前仅支持 WSL2，需要原生 Windows 启动器
- **网关适配器贡献** — 新增平台适配器（WhatsApp Business API、Microsoft Teams 等）
- **性能分析** — 持续负载下的内存使用、token 预算优化

报告 bug 或提功能请求请开 issue。贡献代码请：

1. Fork 仓库
2. 创建功能分支
3. 确保 `pytest tests/` 通过（584 个测试）
4. 提交 PR 并附清晰说明

### 许可证

MIT — 详见 [LICENSE](LICENSE)。
