# Glink

> **Multi-Agent. One Bus. Zero Friction.**

Glink is a lightweight orchestration engine that turns your AI agents into a **collaborative assembly line**. Define a workflow in YAML, and Glink routes each step to the right agent — passing context, handling failures, and logging every heartbeat onto a shared JSONL blackboard. No databases, no message queues, no external dependencies.

---

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│                     Glink Engine                          │
│                                                           │
│   ┌────────┐  ┌────────┐  ┌──────────┐  ┌──────┐  ┌──────┐│
│   │  Hammer │  │  Ink   │  │Bumblebee │  │Laser │  │Forge ││
│   │ :8431   │  │ :8432  │  │  :8434   │  │:8435 │  │:8436 ││
│   └───┬─────┘  └───┬────┘  └────┬──────┘  └──┬───┘  └──┬───┘│
│       │            │            │            │          │    │
│       └────────────┴────────────┴────────────┴──────────┘    │
│                              │                               │
│                     ┌────────▼────────┐                      │
│                     │    Main Bus      │                      │
│                     │ JSONL Blackboard │                      │
│                     └─────────────────┘                      │
│         Append-only timeline — every agent reads & writes     │
└──────────────────────────────────────────────────────────┘
```

---

## Quick Start

```bash
# Clone & go
cd glink

# Run a workflow (resumes from last checkpoint automatically)
python3 glink-daemon.py sandbox-builder

# Force restart from step 1
python3 glink-daemon.py sandbox-builder --force

# Jump to a specific step
python3 glink-daemon.py sandbox-builder --step 4

# Serve-only mode (API without running workflow)
python3 glink-daemon.py --serve

# Dashboard
open http://127.0.0.1:8426
```

---

## Features

| Feature | Description |
|:--------|:------------|
| **YAML Workflows** | Define steps, agents, dependencies, and fallbacks in one file |
| **Main Bus** | JSONL blackboard — append-only, agent-agnostic, replayable |
| **Smart Routing** | Primary agent down? Auto-fallback to the next in line |
| **Checkpoint Resume** | Crash mid-workflow? Restart picks up exactly where it left off |
| **Dependency Graph** | Steps can `depends_on` each other; Glink handles ordering |
| **Retry Loop** | Auto-retry failed steps (configurable, default 2×) |
| **HTTP API + SSE** | Live status, agent health, and event stream on `:8426` |
| **Healthcheck Cron** | Self-healing — daemon restarts on crash, alerts via Feishu |
| **Zero Deps** | One Python file + one JSONL file. No pip install needed |

---

## Workflow Example

```yaml
name: sandbox-builder
version: 0.2.0

global_context: |
  Three.js r160 + Cannon-es. Output: single HTML file.

steps:
  - id: step-1
    executor: Hammer
    title: Scene setup
    description: Three.js scene + camera + lights + render loop
    output_file: projects/sandbox-builder/scene.html

  - id: step-2
    executor: Hammer
    title: Block placement
    description: Raycasting + grid snap + 6 materials
    input_file: projects/sandbox-builder/scene.html
    output_file: projects/sandbox-builder/blocks.html

  - id: step-5
    executor: Ink
    title: Glassmorphism UI
    description: Toolbar + score panel with backdrop-filter
    fallback_agents: [Hammer, Default]
    input_file: projects/sandbox-builder/blocks.html
    output_file: projects/sandbox-builder/ui.html
```

---

## Agent Roster

| Agent | Port | Specialty |
|:------|:----:|:----------|
| **Default / Zaku** | 8420 | Generalist, fallback for everything |
| **Hammer** | 8431 | Backend, databases, engineering code |
| **Ink** | 8432 | Frontend UI, visual design, CSS |
| **Bumblebee** | 8434 | Data, search, persistence |
| **Laser** | 8435 | Testing, validation, documentation |
| **Forge** | 8436 | Code review, quality gate, code artistry |

---

## API Reference

| Method | Endpoint | Description |
|:-------|:---------|:------------|
| `GET` | `/health` | Liveness check → `{"status":"ok"}` |
| `GET` | `/status` | Full project status + step-by-step progress |
| `GET` | `/status/agents` | Which agents are online right now |
| `GET` | `/status/events?n=20` | Last N Bus events |
| `POST` | `/restart` | Resume from last checkpoint |
| `POST` | `/restart?force` | Force restart from step 1 |
| `POST` | `/restart?step=N` | Jump to step N |

---

## Real-World Result

**sandbox-builder** — 10 steps × 5 agents → 97 KB / 2,751 lines of playable HTML.

Three.js sandbox game with physics, procedural textures, glassmorphism UI, save/load, scoring, and achievements — built entirely by agent collaboration, no human code touched.

---

## License

MIT © 2026 Opprime
