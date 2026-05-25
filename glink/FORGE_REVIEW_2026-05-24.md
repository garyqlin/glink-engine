# 🔍 Forge 代码评审报告 — Glink 项目

**评审范围**：`glink-daemon.py` (845行) · `bus/main_bus.py` (149行) · `glink.py` (205行)
**评审日期**：2026-05-24
**评审人**：Forge（代码臂）
**综合评分**：🟡 5.8/10

---

## 🔴 三大必修 Bug

### Bug ① | `save_checkpoint` 与 `load_checkpoint` 字段名不一致
- `load_checkpoint` 读 `last_completed_step_index`
- `save_checkpoint` 写 `step_index`
- **后果**：`load_checkpoint` 永远返回 -1，为死代码
- **修复**：统一为 `step_index`

### Bug ② | `find_resume_point` 覆盖 completed 状态
- 注释说"不覆盖 completed"，但代码 `step_status[stage] = "failed"` 直接覆盖
- **后果**：已完成 step 重启后被当失败重跑，可能重复调用付费 API

### Bug ③ | `main_bus.write` 并发写入无锁
- 多个进程同时 append 同一 JSONL，macOS 上 >512B 写入不保证原子性
- **修复**：加 `fcntl.flock(LOCK_EX)` 或迁 SQLite

## 🟡 立即修复（成本低收益高）

1. **Path traversal 漏洞** — `bus_path()`/`load_workflow` 加 project_name 白名单
2. **文件句柄泄漏** — `open(PIDFILE).read()` 无 `with` 语句
3. **常量顺序倒置** — `CHECKPOINT_FILE` 在 149 行定义，104 行已使用

## 其他问题

- Bus 全文件扫描（事件多了会慢）
- Agent 探测串行（7 agent × 3s = 最坏 21s）
- `call_agent`/`load_workflow` 在 daemon.py 和 glink.py 重复实现
- `--step=abc` 未做输入校验
- 无类型注解、日志用 print
- `/restart` 无鉴权

## 亮点

- 断点续跑设计优雅（Bus 重建状态，不依赖 checkpoint 文件）
- JSONL 容错合理（损坏行跳过）
- pidfile 双重检测（进程死 vs PID 复用）
- fallback 智能路由

## 推荐修复路径

1. 第 1 周：修 Bug ①②③ + 三个立即修复（约 30 行）
2. 第 2 周：提取 `bus/agent_client.py` 消除重复 + print → logging
3. 第 3 周：Bus 加锁或迁 SQLite + 拆分 daemon.py
4. 可选：pytest 测试套
