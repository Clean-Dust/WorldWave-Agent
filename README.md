<p align="center">
  <br>
  <picture>
    <source media="(prefers-color-scheme: dark)">
    <img alt="Worldwave" src="data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSI2MDAiIGhlaWdodD0iMTIwIiB2aWV3Qm94PSIwIDAgNjAwIDEyMCI+PHJlY3Qgd2lkdGg9IjYwMCIgaGVpZ2h0PSIxMjAiIGZpbGw9IiMwZTBlMGUiLz48dGV4dCB4PSIzMDAiIHk9IjUwIiBmb250LWZhbWlseT0iQXJpYWwsIHNhbnMtc2VyaWYiIGZvbnQtc2l6ZT0iMzYiIGZpbGw9IiM2MGE1ZmEiIHRleHQtYW5jaG9yPSJtaWRkbGUiIGZvbnQtd2VpZ2h0PSJib2xkIj7imp3imJ0gV09STERXQVZFPC90ZXh0Pjx0ZXh0IHg9IjMwMCIgeT0iOTAiIGZvbnQtZmFtaWx5PSJBcmlhbCwgc2Fucy1zZXJpZiIgZm9udC1zaXplPSIxOCIgZmlsbD0iIzg4ODg4OCIgdGV4dC1hbmNob3I9Im1pZGRsZSI+cGVyc2lzdGVudCBjb2duaXRpdmUgZW50aXR5IGZyYW1ld29yazwvdGV4dD48L3N2Zz4=" width="600" height="120" alt="Worldwave">
  </picture>
  <br>
</p>

<p align="center">
  <a href="https://github.com/Clean-Dust/worldwave/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License"></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.10%2B-blue.svg" alt="Python"></a>
  <a href="#"><img src="https://img.shields.io/badge/version-0.8.0-informational" alt="Version"></a>
  <a href="#"><img src="https://img.shields.io/badge/tests-584%20passed-brightgreen" alt="Tests"></a>
  <a href="#"><img src="https://img.shields.io/badge/tools-192-orange" alt="Tools"></a>
  <a href="#"><img src="https://img.shields.io/badge/lines-86,510-lightgrey" alt="Lines"></a>
</p>

<p align="center">
  <a href="#english">English</a> &nbsp;|&nbsp;
  <a href="#chinese">简体中文</a>
</p>

---

<h2 id="english">Worldwave</h2>

A persistent cognitive entity framework. Worldwave runs as a single continuous agent — no sessions, no context resets, no "new chat" button. Every platform (terminal, Telegram, HTTP API) connects to the same entity timeline.

### Architecture

```
┌─────────────────────────────────────────────────────┐
│                  Worldwave                           │
│                                                      │
│  ┌──────────┐  ┌──────────┐  ┌──────────────────┐  │
│  │ Terminal │  │ Telegram │  │  HTTP / Web UI    │  │
│  └────┬─────┘  └────┬─────┘  └────────┬─────────┘  │
│       └──────────────┼────────────────┘             │
│                      ▼                               │
│  ┌──────────────────────────────────────────────┐   │
│  │         IdentityResolver                     │   │
│  │    all platform IDs → single entity_id       │   │
│  └──────────────────┬───────────────────────────┘   │
│                     ▼                                │
│  ┌──────────────────────────────────────────────┐   │
│  │         Entity State Machine                 │   │
│  │    hydrate ⟷ process ⟷ persist ⟷ sleep       │   │
│  └──────────────────┬───────────────────────────┘   │
│                     ▼                                │
│  ┌──────────────────────────────────────────────┐   │
│  │         Spiral Cognitive Loop (7 phase)       │   │
│  │  PERCEIVE → RECALL → PLAN → ACT → EVALUATE    │   │
│  │  → LEARN → CONSOLIDATE                        │   │
│  └──────────────────┬───────────────────────────┘   │
│                     ▼                                │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌────────┐  │
│  │  Memory  │ │ 192 Tools│ │   P2P    │ │  MCP   │  │
│  │  8-layer │ │ 16 cats  │ │ Network  │ │ Bridge │  │
│  └──────────┘ └──────────┘ └──────────┘ └────────┘  │
└─────────────────────────────────────────────────────┘
```

**Entity continuity** — the core differentiator. Every user has one permanent `entity_id`. When a message arrives from any platform, the entity's persistent state (working memory, preferences, last context) is loaded and injected into the spiral loop. After processing, the updated state is saved. No session boundaries.

**Self-editing memory** — the agent can call `remember(key, value)` and `forget(key)` to manage its own knowledge base. It is not a passive consumer of RAG results.

### Quick Start

```bash
# Install
git clone https://github.com/Clean-Dust/worldwave.git
cd worldwave
pip install -e .

# Configure
cp .env.example .env
# Edit .env with your API key (DeepSeek, OpenAI, or Anthropic)

# Run
ww run "Hello, what can you do?"
```

Or deploy as a server:

```bash
python server.py
# API at http://localhost:9300
# Web UI at http://localhost:9300/ww/webui/
```

One-click node deployment:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/Clean-Dust/worldwave/main/deploy.sh)
```

### Features

| Category | Modules | Description |
|----------|---------|-------------|
| **Cognitive Engine** | `core/loop.py` | 7-phase spiral loop: perceive, recall, plan, act, evaluate, learn, consolidate |
| **Entity Continuity** | `wavegate/identity.py`, `core/entity_state.py` | Cross-platform identity resolution, persistent state with auto-hydration/sleep |
| **Memory System** | `core/memory/` (12 modules) | Hippocampus buffer, amygdala emotional scoring, sleep consolidation, reconsolidation, recall with spreading activation, temporal validity tracking, typed knowledge graph edges with CTE multi-hop traversal |
| **Self-Editing Memory** | `core/memory/tools.py` | Agent can call `remember()` / `forget()` / `recall_mine()` to manage its own knowledge |
| **Gateway** | `gateway/` (25 modules) | Multi-platform adapters: Telegram, Discord, Slack, Signal, WeChat, Feishu, DingTalk, WhatsApp, LINE, Matrix, Webhook |
| **Tools** | `tools/registry.py` | 192 tools across 16 categories: shell, file, web, browser, memory, coding, contacts, scheduling, platform messaging, MCP bridge, credentials, plugins, hooks, slash commands, voice, computer use |
| **Subconscious** | `core/subconscious/` (30 modules) | Local ML engine: DeepRiskNet (~700 KB), decision trees, PPO, CFR contrastive learning, nighttime schema induction, context pressure sensing. Reads numerical features only — never raw conversations |
| **P2P Network** | `p2p/` (16 modules) | Decentralized node discovery (mDNS, DHT, HTTP tracker), Merkle chain for model update provenance, gossip protocol, NAT relay, Nostr relay pool, proof-of-contribution mining (optional, off by default) |
| **Coding Engine** | `coding/` (12+ modules) | Defensive code editing with backup/rollback, AST-aware search (ast-grep), progressive loading for large repos, capability mutex for concurrent edits |
| **Computer Use** | `core/computer_use/` (12 modules) | 7-tier progressive screen capture, UI Automation tree extraction, set-of-mark visual grounding, browser stealth control, vision loop |
| **Biomimetic Modules** | `core/` (45 modules) | Global workspace (7-item capacity), basal ganglia (Go/NoGo action gate), circadian rhythm, cascade bus, predictive model, skill solidification, self-model introspection |
| **Sandbox** | `sandbox/` | Isolated execution environment for untrusted code |
| **CLI** | `ww_cli.py` | Full-featured CLI: `ww run`, `ww chat`, `ww mascot`, `ww pairing`, `ww tools`, `ww config` |
| **Plugins** | `core/plugins.py` | Plugin marketplace with discovery, install, enable/disable lifecycle |

### Design Decisions

- **Zero external ML dependencies.** Core ML (DeepRiskNet, blockchain, P2P, memory scoring) uses Python stdlib only — no numpy, sklearn, or torch required for basic operation.
- **Everything is optional and off by default.** Mining, self-hosting plugins, auto-evolution — all disabled until explicitly enabled.
- **No external graph databases.** Knowledge graph edges use SQLite CTE recursive queries. No Neo4j, no Redis requirement.
- **Single execution path.** One code path for `ww run` and `ww chat` — no dual-mode complexity.
- **Entity-first, not session-first.** The framework models a persistent identity, not a disposable chat thread.

### Documentation

- `wwmd/` — Architecture reference (Simplified Chinese)
- `tests/` — 584 tests covering all subsystems
- `docs/` — Whitepaper and architecture docs

### Contributing

Contributions are welcome. Areas that need work:

- Multi-node P2P end-to-end testing
- Windows-native packaging
- Additional language support for the gateway
- Performance profiling and optimization

See `CONTRIBUTING.md` (coming soon) for guidelines.

### License

MIT — see [LICENSE](LICENSE).

---

<h2 id="chinese">Worldwave · 简体中文</h2>

一个持续认知主体框架。Worldwave 作为单一连续代理运行——没有会话、没有上下文重置、没有"新对话"按钮。每个平台（终端、Telegram、HTTP API）连接到同一条实体时间线。

### 架构

```
┌─────────────────────────────────────────────────────┐
│                  Worldwave                           │
│                                                      │
│  ┌──────────┐  ┌──────────┐  ┌──────────────────┐  │
│  │  终端机  │  │ Telegram │  │  HTTP / Web UI    │  │
│  └────┬─────┘  └────┬─────┘  └────────┬─────────┘  │
│       └──────────────┼────────────────┘             │
│                      ▼                               │
│  ┌──────────────────────────────────────────────┐   │
│  │         IdentityResolver                     │   │
│  │    所有平台 ID → 单一 entity_id               │   │
│  └──────────────────┬───────────────────────────┘   │
│                     ▼                                │
│  ┌──────────────────────────────────────────────┐   │
│  │         Entity State Machine                 │   │
│  │    载入 ⟷ 处理 ⟷ 持久化 ⟷ 休眠               │   │
│  └──────────────────┬───────────────────────────┘   │
│                     ▼                                │
│  ┌──────────────────────────────────────────────┐   │
│  │         螺旋认知循环（7 阶段）                  │   │
│  │  感知 → 回忆 → 规划 → 行动 → 评估 → 学习 → 巩固 │   │
│  └──────────────────┬───────────────────────────┘   │
│                     ▼                                │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌────────┐  │
│  │ 8层记忆  │ │ 192工具  │ │  P2P网络 │ │  MCP   │  │
│  │  系统    │ │ 16个类别 │ │  联邦学习 │ │  桥接  │  │
│  └──────────┘ └──────────┘ └──────────┘ └────────┘  │
└─────────────────────────────────────────────────────┘
```

**实体连续性** — 核心差异点。每个用户有一个永久的 `entity_id`。当消息从任意平台到达时，实体的持久状态（工作记忆、偏好、上次上下文）被载入并注入螺旋循环。处理完成后，更新后的状态被保存。没有会话边界。

**自主记忆编辑** — 代理可以调用 `remember(key, value)` 和 `forget(key)` 来管理自己的知识库。它不是被动的 RAG 结果消费者。

### 快速开始

```bash
# 安装
git clone https://github.com/Clean-Dust/worldwave.git
cd worldwave
pip install -e .

# 配置
cp .env.example .env
# 编辑 .env，填入 API key（DeepSeek、OpenAI 或 Anthropic）

# 运行
ww run "你好，你能做什么？"
```

或作为服务器部署：

```bash
python server.py
# API 地址：http://localhost:9300
# Web UI：http://localhost:9300/ww/webui/
```

一键节点部署：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/Clean-Dust/worldwave/main/deploy.sh)
```

### 功能概览

| 类别 | 模块 | 说明 |
|------|------|------|
| **认知引擎** | `core/loop.py` | 7 阶段螺旋循环：感知、回忆、规划、行动、评估、学习、巩固 |
| **实体连续性** | `wavegate/identity.py`, `core/entity_state.py` | 跨平台身份解析、带自动休眠/唤醒的持久状态 |
| **记忆系统** | `core/memory/`（12 个模块） | 海马体缓冲、杏仁核情感评分、睡眠巩固、再巩固、扩散激活回忆、时间有效性追踪、带 CTE 多跳遍历的类型化知识图谱边 |
| **自主记忆** | `core/memory/tools.py` | 代理可调用 `remember()` / `forget()` / `recall_mine()` 管理自有知识 |
| **网关** | `gateway/`（25 个模块） | 多平台适配器：Telegram、Discord、Slack、Signal、微信、飞书、钉钉、WhatsApp、LINE、Matrix、Webhook |
| **工具** | `tools/registry.py` | 192 个工具，16 个类别：shell、文件、网络、浏览器、记忆、编程、联系人、调度、平台消息、MCP 桥接、凭证、插件、钩子、斜杠命令、语音、桌面操作 |
| **潜意识** | `core/subconscious/`（30 个模块） | 本地 ML 引擎：DeepRiskNet（约 700 KB）、决策树、PPO、CFR 对比学习、夜间模式归纳、上下文压力感知。仅读取数值特征——不读原始对话 |
| **P2P 网络** | `p2p/`（16 个模块） | 去中心化节点发现（mDNS、DHT、HTTP tracker）、Merkle 链模型更新溯源、gossip 协议、NAT 中继、Nostr 中继池、贡献证明挖矿（可选，默认关闭） |
| **编程引擎** | `coding/`（12+ 个模块） | 带备份/回滚的防御性代码编辑、AST 感知搜索（ast-grep）、大仓库渐进式加载、并发编辑能力互斥锁 |
| **桌面操作** | `core/computer_use/`（12 个模块） | 7 层级渐进式屏幕捕获、UI Automation 树提取、标记式视觉定位、浏览器隐身控制、视觉闭环 |
| **仿生模块** | `core/`（45 个模块） | 全局工作区（7 项容量）、基底核（Go/NoGo 动作门控）、昼夜节律、级联总线、预测模型、技能固化、自我模型内省 |
| **沙箱** | `sandbox/` | 不受信任代码的隔离执行环境 |
| **CLI** | `ww_cli.py` | 全功能命令行：`ww run`、`ww chat`、`ww mascot`、`ww pairing`、`ww tools`、`ww config` |
| **插件** | `core/plugins.py` | 插件市场：发现、安装、启用/禁用生命周期 |

### 设计决策

- **零外部 ML 依赖。** 核心 ML（DeepRiskNet、区块链、P2P、记忆评分）仅使用 Python 标准库——基本运行不需要 numpy、sklearn 或 torch。
- **所有功能可选，默认关闭。** 挖矿、自托管插件、自动进化——全部默认禁用，需显式启用。
- **无外部图数据库。** 知识图谱边关系使用 SQLite CTE 递归查询。不需要 Neo4j，不需要 Redis。
- **单一执行路径。** `ww run` 和 `ww chat` 走同一条代码路径——无双重模式复杂性。
- **实体优先，非会话优先。** 框架建模的是持久身份，而非一次性聊天线程。

### 文档

- `wwmd/` — 架构参考（简体中文）
- `tests/` — 584 个测试，覆盖所有子系统

### 参与贡献

欢迎贡献。以下领域需要帮助：

- 多节点 P2P 端到端测试
- Windows 原生打包
- 网关的多语言支持
- 性能分析与优化

### 许可证

MIT — 详见 [LICENSE](LICENSE)。
