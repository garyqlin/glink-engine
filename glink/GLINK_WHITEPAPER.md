# 🚀 Glink — 多 Agent 协作编排引擎

> **白皮书 v1.0 | 2026-05-25**
>
> 让战甲们真正一起干活，而不是各自为战。

---

## 一、概述

**Glink** 是一个**轻量级多 Agent 协作编排引擎**，专为 AI 战甲工作流设计。

传统上，多个 AI Agent 各自独立运行，信息无法共享，成果无法接力。Glink 通过 **Main Bus 共享黑板架构** 打破这个孤岛——让不同的战甲（重锤写代码、绘墨做 UI、大黄蜂填充数据、Laser 测试、Forge 质检）像一条流水线工人一样协同工作。

```
┌─────────────────────────────────────────────────────┐
│                    Glink 引擎                         │
│                                                      │
│  ┌──────┐  ┌──────┐  ┌──────┐  ┌──────┐  ┌──────┐  │
│  │重锤  │  │绘墨  │  │大黄蜂│  │Laser │  │Forge │  │
│  └──┬───┘  └──┬───┘  └──┬───┘  └──┬───┘  └──┬───┘  │
│     │         │         │         │         │       │
│     └─────────┴─────────┴─────────┴─────────┘       │
│                         │                            │
│                    ┌────▼────┐                       │
│                    │ Main Bus │                       │
│                    └─────────┘                       │
│   (JSONL 共享黑板: 每个项目一条时间线)                 │
└─────────────────────────────────────────────────────┘
```

**核心哲学：**

- **轻量** — 一个 Python 文件 + 一个 JSONL 文件就够，无外部依赖
- **渐进** — 从简单的顺序编排一路演进到智能路由 + 自动恢复
- **不改变战甲** — 战甲们不需要知道 Glink 的存在，通过 HTTP 标准 /ask 接口通信
- **可观察** — 每一步都有 Bus 事件记录，Dashboard 实时可见

---

## 二、核心概念

### 2.1 工作流（Workflow）

一个 YAML 文件定义一组步骤（Steps），描述"谁做什么、做完交给谁"。

```yaml
# 示例：沙盒建造游戏流水线
name: sandbox-builder
version: 0.2.0

global_context: |
  你是一个 3D 游戏开发专家。输出：单 HTML 文件
  技术栈：Three.js r160 + Cannon-es + OrbitControls

steps:
  - id: step-1
    executor: 重锤
    title: 场景初始化
    description: Three.js 场景 + 相机 + 光照 + 渲染循环
    output_file: projects/sandbox-builder/sandbox-builder-step1.html

  - id: step-2
    executor: 重锤
    title: 方块放置系统
    description: Raycasting + 网格吸附 + 6 种材质
    input_file: projects/sandbox-builder/sandbox-builder-step1.html
    output_file: projects/sandbox-builder/sandbox-builder-step2.html
```

### 2.2 Main Bus（共享黑板）

每个项目一个 `.jsonl` 文件，记录所有事件（`task.started` / `task.completed` / `task.failed` 等）。

```
projects/{project_name}.jsonl
```

每条事件：
```json
{
  "ts": "2026-05-24T22:45:41.123456",
  "type": "task.completed",
  "agent": "绘墨",
  "stage": "step-6",
  "data": { "title": "开始菜单+结束画面", "output_preview": "..." }
}
```

特性：
- **按时间追加**（append-only），永不覆盖
- **智能路由**：任务失败后自动轮询 fallback_agents
- **断点续跑**：支持中途崩溃后从 checkpoint 恢复
- **依赖等待**：步骤可声明依赖，Glink 自动等待前序完成

### 2.3 战甲（Agent Armors）

Glink 内置的 Agent 端口映射（唯一真源）：

| 战甲 | 端口 | 职责 |
|:---|:---:|:---|
| 标准版 / 扎古 | 8420 | 通用型，默认备用 |
| 重锤 (Hammer) | 8431 | 后端、数据库、工程代码 |
| 绘墨 (Ink) | 8432 | 前端 UI、视觉设计、体验 |
| 大黄蜂 (Bumblebee) | 8434 | 数据填充、搜索、执行 |
| Laser | 8435 | 测试、验证、文档 |
| Forge / 代码臂 | 8436 | 代码审查、质量闭环、代码艺术 |

---

## 三、架构详解

### 3.1 分层架构

```
┌────────────────────────────────────────────────┐
│                 调用端 / API                       │
│  ┌──────────┐  ┌────────────┐  ┌────────────┐  │
│  │ HTTP API │  │   CLI      │  │ Dashboard  │  │
│  │  :8426   │  │ 命令行     │  │  Web UI    │  │
│  └────┬─────┘  └─────┬──────┘  └──────┬─────┘  │
├───────┴──────────────┴─────────────────┴────────┤
│                Glink 引擎层                       │
│                                                   │
│  ┌─────────────────────────────────────────┐     │
│  │       工作流编排器 (Workflow Engine)       │     │
│  │  - 步骤解析 + 依赖管理                   │     │
│  │  - 智能路由 (planned → fallback agents)  │     │
│  │  - 重试循环 (默认 2 次)                  │     │
│  │  - 断点续跑 (checkpoint JSON)            │     │
│  └────────────────┬────────────────────────┘     │
│                   │                               │
│  ┌────────────────▼────────────────────────┐     │
│  │        Agent 通信层 (agent_client)        │     │
│  │  - AGENT_PORTS 统一映射                  │     │
│  │  - HTTP POST /ask 标准化通信             │     │
│  │  - input_file 强制传递 + 输出指令        │     │
│  └────────────────┬────────────────────────┘     │
│                   │                               │
│  ┌────────────────▼────────────────────────┐     │
│  │        Main Bus (共享时间线 / 黑板)       │     │
│  │  - projects/{name}.jsonl append-only     │     │
│  │  - 8 种事件类型                          │     │
│  │  - stage 域隔离                          │     │
│  └─────────────────────────────────────────┘     │
├──────────────────────────────────────────────────┤
│                 基础设施层                         │
│                                                   │
│  ┌─────────┐  ┌──────────┐  ┌───────────────┐  │
│  │ PID守护  │  │ 自恢复   │  │ HTTP Server   │  │
│  │ pidfile  │  │ cron 自检│  │ 独立进程存活   │  │
│  │          │  │ +告警    │  │ :8426 独立    │  │
│  └─────────┘  └──────────┘  └───────────────┘  │
└──────────────────────────────────────────────────┘
```

### 3.2 执行流程

当一个工作流被触发时：

```
① glink-daemon sandbox-builder

② 读取 workflows/sandbox-builder.yaml
   ↓
③ 从 checkpoint 或 step-1 开始
   ↓
④ 解析 step:
   - 检查依赖 (depends_on)
   - 读取 input_file 并注入任务上下文
   - 智能路由 (planned_agent → fallback)
   ↓
⑤ 写 Bus: task.started
   ↓
⑥ HTTP 调用战甲: POST /ask {message, session}
   ↓
⑦ 轮询等待完成 (期间保持 HTTP 长连接)
   ↓
⑧ 写 Bus: task.completed / task.failed
   ↓
⑨ 写入 checkpoint.json
   ↓
⑩ 循环至下一 step
   ↓
⑪ 所有步骤完成 → 写 project.update (completed)
```

### 3.3 智能路由

当首选 Agent 不可用时，Glink 自动按配置的 `fallback_agents` 顺序轮询：

```yaml
- executor: 绘墨
  fallback_agents: ["重锤", "标准版"]
```

路由过程：
1. 先检查绘墨端口 (8432) → 不通则跳到重锤 (8431)
2. 不通则跳到标准版 (8420)
3. 所有 fallback 都不通 → 步骤失败，非可选步骤阻断流水线

---

## 四、状态模型

每一 step 运行时有 4 种状态：

| 状态 | 含义 | 图标 |
|:---|:---|:---:|
| `ok` | 成功完成 | 🟢 |
| `running` | 正在执行 | 🟡 |
| `wait` | 等待依赖完成 | ⚪ |
| `failed` | 失败 | 🔴 |

整个项目还有：
- `started` — 刚开始
- `running` — 执行中
- `completed` — 全部完成
- `failed` — 某必选步骤失败

---

## 五、API 参考

### HTTP API（端口 8426）

#### 查询项目状态

```
GET /status
→ { project_name, total_steps, status, steps: [...] }
```

每 step 包含 `{ index, title, status, agent, stage }`。

#### 查询 Agent 在线状态

```
GET /status/agents
→ { agents: [ { name, port, online } ] }
```

#### 查询 Bus 事件

```
GET /status/events?n=20
→ { events: [...] }
```

#### 重启工作流

```
POST /restart          # 从当前 checkpoint 恢复
POST /restart?force    # 强制从 step-1 重跑
POST /restart?step=6   # 从第 6 步开始
```

#### 健康检查

```
GET /health
→ { "status": "ok", "service": "glink-daemon-v0.5" }
```

### CLI

```bash
python3 glink-daemon.py <项目名>           # 自动断点续跑
python3 glink-daemon.py <项目名> --force   # 从 step-1 重跑
python3 glink-daemon.py <项目名> --step N  # 从第 N 步开始
python3 glink-daemon.py <项目名> --serve   # 只启动 API，不跑工作流

#### 工作流排队（v1.2 规划）

当前 Glink 一次只处理一个工作流。排队设计已在 roadmap：

```json
// glink-config.yaml 可配置，
// POST /queue/add { project: "foo" }
// GET  /queue/status
// POST /queue/cancel
// 后台线程：完成当前工作流后自动调度队列中下一个
```

队列的核心约束：
- Bus 是单文件 JSONL（fcntl 文件锁），不支持并发写入
- 多工作流同时跑 → 不同 bus 文件 + 不同端口实例
- 同实例队列 → 追加到 `queue.json`，串行轮转

---

## 六、Main Bus：最简里程碑

Main Bus 是 Glink 的心跳——所有 Agent 通过它共享状态。它不是数据库，不是消息队列，只是**一个按行追加的 JSONL 文件**。

### 事件类型

| 事件 | 含义 | 触发者 |
|:---|:---|:---|
| `task.created` | 任务被创建 | Glink 编排器 |
| `task.assigned` | 被分派给某 Agent | Glink 编排器 |
| `task.started` | Agent 开始执行 | 执行 Agent |
| `task.completed` | 成功完成（含 output_preview）| 执行 Agent |
| `task.failed` | 失败 | 执行 Agent |
| `task.log` | 执行中的日志 | 执行 Agent |
| `project.update` | 项目整体状态变更 | Glink 编排器 |

### Python API

```python
from main_bus import write, read, latest, status

# 写入事件
write("myproject", "task.started", "Laser", {"title": "测试"})

# 读取最近 20 条
entries = read("myproject", limit=20)

# 获取最新完成事件
entry = latest("myproject", event_type="task.completed")

# 获取项目状态
info = status("myproject")
```

### CLI 接口

```bash
GLINK_PROJECT=myproject python3 main_bus.py write task.started Laser '{"title":"测试"}'
GLINK_PROJECT=myproject python3 main_bus.py read 20
GLINK_PROJECT=myproject python3 main_bus.py status
GLINK_PROJECT=myproject python3 main_bus.py latest task.completed
```

---

## 七、实战案例

### 7.1 沙盒建造游戏（v1.0 完成）

**项目**：`sandbox-builder`
**步骤**：10 步 × 5 种战甲
**产出**：97KB / 2751 行 HTML 文件（双击即玩）

| 步骤 | 战甲 | 功能 | 耗时 |
|:---|:---|---:|---:|
| 1 | 重锤 | Three.js 场景 + 相机 + 光照 | ~5min |
| 2 | 重锤 | Raycasting 方块放置/删除 | ~5min |
| 3 | 重锤 | Canvas 程序生成 6 种材质贴图 | ~5min |
| 4 | 重锤 | Cannon-es 物理同步 | ~5min |
| 5 | 绘墨 | Glassmorphism UI 工具栏 + 分数面板 | ~8min |
| 6 | 绘墨 | 开始菜单 + 结束画面 + ESC 退出 | ~12min |
| 7 | 大黄蜂 | localStorage 3 槽保存/读取 | ~6min |
| 8 | 大黄蜂 | 计分系统 + 6 种成就徽章 | ~6min |
| 9 | Laser | 全流程黑盒测试 | ~5min |
| 10 | Forge | 完整代码审查 + 质量报告 | ~6min |

**成果**：
- Three.js r160 + Cannon-es 全功能 3D 沙盒
- 6 种程序生成材质（草/土/木/石/砖/玻璃）
- 开始菜单 / 结束画面 / ESC 退出
- 3 槽位保存/读取（localStorage）
- 计分系统 + 6 种成就（建筑新星 → 精密建筑师）
- 玻璃拟态 UI（backdrop-filter）

### 7.2 诺保科CRM 升级（试验中）

**项目**：`testglink`
**步骤**：8 步
**范围**：需求分析 → 数据库迁移 → API 补全 → 后台页面 → 小程序 → 演示数据 → 集成测试 → 验收

---

## 八、基础设施层

### 自动恢复

Glink 通过自带的守护机制确保稳定性：

```
┌──────────────────────────────────────┐
│          cron 每 3 分钟自检            │
│  glink-healthcheck.sh                 │
│                                      │
│  ① 检查 pidfile 对应的进程是否存活      │
│  ② 如果挂了 → 读取 boot 时间戳          │
│  ③ 30 秒内重启 → 从 checkpoint 恢复    │
│  ④ 超过 30 秒 → 发飞书卡片告警（红色）  │
└──────────────────────────────────────┘
```

### 深度错误恢复（P2 深度修复）

除基础 pidfile 自检外，Glink 提供 step 级别的错误恢复链：

**检测层**：
- 每步写入 `checkpoint.json`（tmp + rename 原子写入，fcntl 文件锁）
- `find_resume_point()` 从 Bus 事件回溯已完成的 step
- 不依赖 LLM 状态，纯事件驱动恢复

**恢复层**：
```python
def self_restart(project, force=False):
    if not force:
        ck = load_checkpoint(project)           # 读 checkpoint
        if ck.step_index >= 0:
            resume = ck.step_index + 1           # 续跑下一步
    Popen([DAEMON_SCRIPT, f"--step={resume}"])  # 新进程接力
```

**兜底层**（容错链）：
1. **pidfile 快速自检**（亚秒级）→ 发现挂掉 →
2. **healthcheck 脚本**每 3 分钟 → 中断超过 30 秒告警 →
3. **checkpoint 恢复**（setp 级续跑）→
4. **飞书告警**（配置 `GLINK_ALERT_WEBHOOK` 后红色卡片推送到群）

如果 `self_restart` 本身失败（脚本路径错误、Python 不可用），不静默退出，而是记日志保持进程存活等待人工介入。

### 排队长队与序列化

Glink 当前采用**严格串行**模型：同一 daemon 进程一次只能跑一个工作流。多个项目并存时：

```python
# daemon 启动时不自动执行，仅启动 API server
python3 glink-daemon.py my-project --serve-only
# 通过 POST /restart?force=true 触发执行
# 或指定默认项目在 YAML 中：
# project:
#   default: sandbox-builder
```

**P3 规划**：
- 工作流排队（等待队列，前一个完成后自动启动下一个）
- 项目优先级标记（YAML 中 `priority: P0/P1/P2`）
- 多 daemon 实例（不同项目跑不同端口）

当前限制的原因：Main Bus 是单文件 JSONL，无并发写入保护层。并行安排在 v1.2+。

### 并发与并行（P2 待实现蓝图）

当前所有步骤严格串行。并行计划分两阶段：

**Phase 1 — 独立步骤并行**（v1.x）
```yaml
steps:
  - executor: 重锤
    title: 后端
    depends_on: []        # 无依赖 → 可并行
    parallel_group: 1     # 同一 group 的步骤同时执行
  - executor: 绘墨
    title: 前端    
    depends_on: []        # 无依赖 → 可并行
    parallel_group: 1
  - executor: 大黄蜂
    title: 集成测试
    depends_on: [step-1, step-2]  # 等待两组完成
```

**Phase 2 — 子工作流嵌套**（v1.3）
```yaml
steps:
  - executor: glink      # 特殊 executor
    sub_workflow: backend-workflow.yaml
  - executor: glink
    sub_workflow: frontend-workflow.yaml
```

并行执行的关键前提：
- Main Bus 写入锁（已基础支持 `fcntl.flock`）
- event 的 `type` + `stage` 双字段查询（已支持）
- Agent 调用需独立的 timeout 线程（已通过 `threading.Thread` 实现）

当前 **max_concurrent_steps: 1** 配置已预留，扩展时只需改配置值。

---

## 九、与同类对比

| 特性 | Glink | Airflow | Temporal | n8n | LangGraph |
|:---|:---:|:---:|:---:|:---:|:---:|
| 部署方式 | 单文件 Python | 需要数据库+Web Server | 需要gRPC Server | Node.js + Docker | Python 库 |
| 战甲通信 | HTTP /ask | Python callable | SDK | HTTP Node | Python callable |
| 状态持久 | JSONL 文件 | 数据库 | 数据库 | SQLite | 内存 |
| 智能路由 | ✅ 内置 | ❌ | ❌ | ✅ | ❌ |
| 断点续跑 | ✅ 内建 | ✅ | ✅ | ✅ | ❌ |
| 自动恢复 | ✅ pidfile + cron | ✅ | ✅ | ❌ | ❌ |
| 输入文件拼接 | ✅ 自动注入 | ❌ | ❌ | ❌ | ❌ |
| 学习成本 | 5分钟 | 2小时 | 4小时 | 1小时 | 1小时 |
| 外部依赖 | 无（纯 stdlib） | Postgres + Redis | gRPC + etcd | Node.js + Docker | Python |

Glink 的独特价值：**极简 + AI 战甲原生适配**。它不是通用工作流引擎，而是为 AI Agent 协作"定做"的。

---

## 十、使用入门

### 安装

```bash
# 只需要 Python 3.9+
git clone https://your-repo/glink.git
cd glink

# 无需任何 pip install（全 stdlib）
```

### 定义一个工作流

```yaml
# workflows/my-project.yaml
name: my-project
steps:
  - executor: 重锤
    title: 第一步
    description: 做什么
    output_file: projects/my-project/result-1.html

  - executor: 绘墨
    title: 第二步（在前序基础上增量修改）
    input_file: projects/my-project/result-1.html
    output_file: projects/my-project/result-2.html
```

### 运行

```bash
cd glink
python3 glink-daemon.py my-project
```

---

## 十一、未来发展 roadmap

| 阶段 | 功能 | 状态 |
|:---|:---|---:|
| v0.3 | 基本顺序编排 + HTTP 调用战甲 | ✅ |
| v0.4 | Dashboard UI + 智能路由 + fallback_agents | ✅ |
| v0.5 | 自动恢复 (pidfile/cron) + 绝对路径 + HTTP server 独立 | ✅ |
| v0.6 | 三大 Bug 修复：字段不一致、状态覆盖语义、并发无锁 + 安全加固 | ✅ |
| v0.7 | 抽取公用代码至 agent_client，消除重复 | ✅ |
| v1.0 | input_file 强制注入 + 输出路径控制（本白皮书发布） | ✅ |
| v1.1 | Dashboard UI 增强（实时进度可视化 + 指挥官看板） | ✅ |
| v1.2 | 工作流排队（queue.json + 顺序调度） | 🔜 |
| v1.3 | 条件分支（if_else 步骤 + 根据上一步结果路由） | 🔜 |
| v1.4 | 子工作流（嵌套编排，不同项目可嵌套） | 🔜 |
| v1.5 | 独立步骤并行执行（parallel_group + depends_on） | 🔜 |
| v1.6 | YAML 正式配置化 + daemon/config.py 全局配置加载 | ✅ |
| v1.7 | 深度错误恢复（原子 checkpoint + 多级容错链 + 重试 try/except） | ✅ |
| v1.8 | 历史项目可视化回放 | 🔜 |

---

> **Glink** — 让战甲一起干活。
>
> 项目位置：`/Users/gary/opprime/glink/`
> Daemon 端口：8426
> Dashboard：`http://127.0.0.1:8426/status`
