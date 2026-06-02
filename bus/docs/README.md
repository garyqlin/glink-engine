# Main Bus — 共享项目时间线

> 所有 Agent 通过此文件读写项目级共享记忆

## 概述

Main Bus 是 Glink 多 Agent 协作的核心通信层。它是一个 **JSONL 文件系统**，每个项目一条 `.jsonl` 文件，记录所有 Agent 的任务事件。

## 事件类型

| 类型 | 含义 | 触发方 |
|:---|:---|:---|
| `task.created` | 任务创建 | Glink 编排器 |
| `task.assigned` | 任务分派 | Glink 编排器 |
| `task.started` | 任务开始执行 | 执行 Agent |
| `task.completed` | 任务完成 | 执行 Agent |
| `task.failed` | 任务失败 | 执行 Agent |
| `task.log` | 执行过程中的日志 | 执行 Agent |
| `project.update` | 项目状态更新 | Glink 编排器 |

## API 参考

### Python API

```python
from main_bus import write, read, latest, status

# 写入事件
write("testglink", "task.started", "Laser", {"title": "测试"})

# 读取最近 20 条事件
entries = read("testglink", limit=20)

# 获取最新事件
entry = latest("testglink", event_type="task.completed")

# 获取项目状态
s = status("testglink")
```

### CLI

```bash
# 写入
GLINK_PROJECT=testglink python3 main_bus.py write task.started Laser '{"title":"测试"}'

# 读取
GLINK_PROJECT=testglink python3 main_bus.py read 20

# 状态
GLINK_PROJECT=testglink python3 main_bus.py status

# 最新
GLINK_PROJECT=testglink python3 main_bus.py latest task.completed
```

## 数据存储

- 项目文件：`projects/{project_name}.jsonl`
- 格式：每行一个 JSON 对象
- 事件结构：`{ts, type, agent, data, stage}`
