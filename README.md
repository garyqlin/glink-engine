# Glink

> **Multi-Agent Workflow Engine. One Bus. API-First.**

Glink is a **programmatic orchestration engine** for multi-agent collaboration — it has **no human interface**. Your agents use Glink's API to organize and schedule complex workflows across multiple AI agents, passing context, handling failures, and logging every heartbeat onto a shared JSONL blackboard.

---

## How It Works

```
  Human
    │
    ▼
  Orchestrator Agent (your "main" agent)
    │  calls Glink API (POST /run, GET /status)
    ▼
┌──────────────────────────────────────────────────┐
│           Glink Engine (daemon :8426)            │
│  Routes workflow steps │ picks agents │ logs     │
│  Gate verification │ retry loop │ checkpoint     │
└──┬────────┬────────┬────────────────────────────┘
   │        │        │
   ▼        ▼        ▼
Agent A  Agent B  Agent C  (your specialized agents)
```

Glink is not a tool for humans to type commands into. It's a **backend engine** that lives behind your orchestrator agent — call it, don't click it.

---

## Quick Install

```bash
pip install git+https://github.com/garyqlin/glink-engine.git
```

Or clone and run in-place:

```bash
git clone https://github.com/garyqlin/glink-engine.git
cd glink-engine
pip install -e .
```

---

## How to Use It (Agent-to-Agent, Not Human-to-CLI)

### 1. Start the daemon

```bash
# Start Glink daemon in API-server mode (no workflow runs automatically)
python3 glink-daemon.py --serve
# → Listening on http://127.0.0.1:8426
```

### 2. Your orchestrator agent calls Glink

From your orchestrator agent's code:

```python
import requests

# Run a workflow by name — Glink handles step-by-step dispatch
resp = requests.post("http://127.0.0.1:8426/run", json={
    "workflow": "sandbox-builder",
    "force": False      # resume from last checkpoint
})

# Check progress
status = requests.get("http://127.0.0.1:8426/status").json()
```

Or use the Python SDK (bundled):

```python
from bus.main_bus import status, write
from daemon.core import run_workflow

# Your orchestrator code — this is how a main agent uses Glink
result = run_workflow("my-workflow", context={"task": "..."})
```

### 3. Define a workflow YAML

Workflows live in `workflows/`:

```yaml
name: research-pipeline
version: 1.0

steps:
  - id: step-1
    executor: Researcher
    title: Gather data
    description: Collect and summarize the latest information

  - id: step-2
    executor: Analyst
    title: Analyze
    description: Process data and identify patterns
    depends_on: [step-1]

  - id: step-3
    executor: Writer
    title: Generate report
    description: Produce final report from analysis
    depends_on: [step-2]
    fallback_agents: [Analyst]
```

---

## Architecture

```
                         ┌─────────────────────┐
                         │ Your AI Agents       │
                         │  (any LLM, any role) │
                         └──────┬──────┬──────┬─┘
                                │      │      │
                     ┌──────────▼──────▼──────▼────────┐
                     │        Main Bus                  │
                     │     JSONL Blackboard             │
                     └─────────────────────────────────┘
         Append-only event log — every agent reads & writes

     ┌─────────────────────────────────────────────────┐
     │        Glink Engine (daemon :8426)              │
     │  Routes steps → picks agents → logs results     │
     │  Checkpoints → retry loop → gate verification   │
     └─────────────────────────────────────────────────┘
```

---

## Key Concepts

| Concept | What it means |
|:--------|:--------------|
| **Orchestrator Agent** | Your "main" agent that calls Glink's API. Glink has no human UI — your main agent is the human's interface. |
| **Workflow YAML** | `workflows/*.yaml` — define step sequences, agent assignments, dependencies, and fallback agents |
| **Main Bus** | JSONL blackboard — append-only event log that every agent can read/write |
| **Gate Verification** | Each step can have gate conditions (`file_exists`, `output_contains`) that must pass before proceeding |
| **Checkpoint** | Every completed step saves a checkpoint; crash recovery resumes exactly where you left off |
| **Retry Loop** | Failed steps auto-retry with configurable max attempts and injected failure feedback |
| **Loop Engineering** | When a step fails, Glink injects the failure context into the retry prompt so the agent learns from its mistake |

---

## Agent Roster

Register your agents in `glink-daemon.py` or `bus/agent_client.py`:

```python
AGENT_PORTS = {
    "engineer":    "http://127.0.0.1:8431/ask",
    "designer":    "http://127.0.0.1:8432/ask",
    "tester":      "http://127.0.0.1:8433/ask",
}
```

Workflow steps reference agents by name; Glink routes the task automatically.

---

## API Reference

| Method | Endpoint | Called by | Description |
|:-------|:---------|:----------|:------------|
| `GET` | `/health` | Orchestrator / monitor | Liveness check → `{"status":"ok"}` |
| `GET` | `/status` | Orchestrator | Full project status + step-by-step progress |
| `GET` | `/status/agents` | Orchestrator | Which agents are online right now |
| `GET` | `/status/events?n=20` | Orchestrator | Last N Bus events |
| `POST` | `/run` | Orchestrator | Run a workflow (`{"workflow":"name", "force":bool}`) |
| `POST` | `/restart` | Orchestrator | Resume from last checkpoint |
| `POST` | `/restart?force` | Orchestrator | Force restart from step 1 |
| `POST` | `/restart?step=N` | Orchestrator | Jump to step N |

---

## Requirements

- Python ≥ 3.11
- No external databases, message queues, or containers needed
- Works with any LLM provider (OpenAI-compatible API)

---

## License

MIT
