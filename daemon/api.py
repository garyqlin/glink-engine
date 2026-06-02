# SPDX-License-Identifier: MIT
"""Glink Daemon — HTTP API 服务器（17 个端点）"""

import json
import os
import socketserver
import sys
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

from .checks import self_restart
from .config import get_config as config_get
from .config import get_server_port
from .core import AGENT_PORTS, load_workflow, probe_agent
from .log import get_reporter, log_warn

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "bus"))
from bus import main_bus
from bus.agent_client import load_workflow as _shared_load_workflow
from bus.project_memory import GLK_ROOT, _glk_path, sanitize, project_init, project_list, project_get, project_read_context, project_update_context

_REST_PROJECT: dict[str, str] = {"name": "testglink"}


def set_project(name: str) -> None:
    _REST_PROJECT["name"] = name


def get_project() -> str:
    return _REST_PROJECT.get("name", "testglink")


class DashHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def _qstr(self):
        if "?" not in self.path:
            return {}
        return dict(p.split("=", 1) for p in self.path.split("?")[1].split("&") if "=" in p)

    def _verify_token(self) -> bool:
        """验证请求 Token。从环境变量 GLINK_API_TOKEN 读取，未设置时跳过检查。"""
        token = os.environ.get("GLINK_API_TOKEN", "")
        if not token:
            return True  # 未设置 Token，跳过检查（向后兼容）

        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer ") and auth[7:] == token:
            return True

        qs_token = self._qstr().get("token", "")
        return qs_token == token

    def _require_token(self):
        """验证请求 Token，失败时返回 401"""
        if not self._verify_token():
            self.send_json({"error": "unauthorized"}, 401)
            return False
        return True

    def send_json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False)
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body.encode("utf-8"))))
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        path = self.path.split("?")[0]
        # P0-B: Token 认证（/health 除外）
        if path != "/health" and not self._require_token():
            return
        qstr = self._qstr()

        if path == "/restart":
            is_force = qstr.get("force", "").lower() in ("true", "1")
            proj = get_project()
            self.send_json(
                {
                    "status": "ok",
                    "message": f"重启 {proj} {'(force)' if is_force else ''}",
                }
            )
            Thread(target=lambda: self_restart(proj, force=is_force), daemon=True).start()

        # ── Project Memory POST 端点（共享记忆） ────────────────
        elif path == "/project/create":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else b"{}"
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                self.send_json({"error": "invalid JSON"}, 400)
                return
            project_id = data.get("project_id", qstr.get("project_id", ""))
            if not project_id:
                self.send_json({"error": "project_id is required"}, 400)
                return
            context = data.get("context", "")
            result = project_init(project_id, context)
            self.send_json(result)

        elif path.startswith("/project/"):
            parts = path.split("/")
            if len(parts) >= 4:
                action = parts[2]
                project_id = parts[3]
                if action == "update":
                    length = int(self.headers.get("Content-Length", 0))
                    body = self.rfile.read(length) if length else b"{}"
                    try:
                        data = json.loads(body)
                    except json.JSONDecodeError:
                        self.send_json({"error": "invalid JSON"}, 400)
                        return
                    result = project_update_context(
                        project_id,
                        context=data.get("context", ""),
                        event_type=data.get("event_type", ""),
                        agent=data.get("agent", ""),
                        detail=data.get("detail", ""),
                    )
                    self.send_json(result)
                else:
                    self.send_json({"error": f"unknown action {action!r}"}, 404)
            else:
                self.send_json({"error": "invalid path"}, 400)

        else:
            self.send_json({"error": "not found"}, 404)

    def do_GET(self):
        path = self.path.split("?")[0]
        # P0-B: Token 认证
        if not self._require_token():
            return
        qstr = self._qstr()
        proj = get_project()

        if path == "/status":
            self.send_json(self._build_status(proj))

        elif path == "/status/agents":
            agents_out = [{"name": n, "port": p, "online": probe_agent(n)[0]} for n, p in AGENT_PORTS.items()]
            self.send_json({"agents": agents_out})

        elif path == "/status/events":
            try:
                n = int(qstr.get("n", 20))
            except ValueError:
                self.send_json({"error": 'query param "n" must be integer'}, 400)
                return
            n = min(max(n, 1), 1000)
            events = main_bus.read(proj, limit=n)
            s_map = {
                "task.completed": "ok",
                "task.failed": "fail",
                "task.started": "run",
                "task.skipped": "skip",
            }
            ev_out = [
                {
                    "ts": e.get("ts", ""),
                    "type": e["type"],
                    "agent": e.get("agent", "?"),
                    "status_class": s_map.get(e["type"], "wait"),
                    "stage": e.get("stage", ""),
                }
                for e in events[-n:]
            ]
            self.send_json({"events": ev_out})

        elif path == "/bus/latest":
            events = main_bus.read(proj, limit=1)
            self.send_json({"event": events[-1] if events else None})

        elif path == "/health":
            self.send_json({"status": "ok", "service": "glink-daemon-v0.5"})

        elif path == "/events/stream":
            self._handle_sse(proj)

        elif path == "/intel/step":
            stage = qstr.get("stage", "")
            if not stage:
                self.send_json({"error": "need ?stage=xxx"}, 400)
                return
            events = main_bus.read(proj, limit=2000)
            related = [e for e in events if e.get("stage") == stage]
            completions = [e for e in related if e["type"] == "task.completed"]
            failures = [e for e in related if e["type"] == "task.failed"]
            logs = [e for e in related if e["type"] == "task.log"]
            started = [e for e in related if e["type"] == "task.started"]
            step_info = {}
            try:
                wf = _shared_load_workflow(proj)
                for idx, s in enumerate(wf.get("steps", [])):
                    if s.get("stage", f"step-{idx + 1}") == stage:
                        step_info = s
                        break
            except Exception:
                pass
            # 如果 workflow 和 bus 都查不到这个 stage，返回 404
            if not step_info and not started and not completions and not failures:
                self.send_json({"error": f"stage '{stage}' not found", "stage": stage}, 404)
                return
            out = {
                "stage": stage,
                "title": step_info.get("title", ""),
                "description": step_info.get("description", "")[:500],
                "executor": step_info.get("executor", ""),
                "fallback_agents": step_info.get("fallback_agents", []),
                "input_file": step_info.get("input_file", ""),
                "output_file": step_info.get("output_file", ""),
                "optional": step_info.get("optional", False),
                "status": "wait",
                "attempts": 0,
                "planned_agent": step_info.get("executor", ""),
                "actual_agent": "",
                "run_start": "",
                "run_end": "",
                "duration_sec": 0,
                "output_preview": "",
                "errors": [],
                "logs": len(logs),
            }
            if started:
                out["status"] = "running"
                out["run_start"] = started[-1].get("ts", "")
                out["actual_agent"] = started[-1].get("agent", "")
            if completions:
                last = completions[-1]
                out["status"] = "ok"
                out["run_end"] = last.get("ts", "")
                out["actual_agent"] = last.get("agent", "")
                out["output_preview"] = ((last.get("data", {}) or {}).get("output_preview", ""))[:500]
            if failures:
                out["status"] = "failed"
                out["errors"] = [(f.get("data", {}) or {}).get("error", "")[:200] for f in failures]
            out["attempts"] = len(started)
            if out["run_start"] and out["run_end"]:
                try:
                    fmt = "%Y-%m-%dT%H:%M:%S.%f"
                    t0 = datetime.strptime(out["run_start"][:26], fmt)
                    t1 = datetime.strptime(out["run_end"][:26], fmt)
                    out["duration_sec"] = round((t1 - t0).total_seconds())
                except Exception:
                    pass
            self.send_json(out)

        elif path == "/intel/agents":
            agents_out = []
            for name, port in AGENT_PORTS.items():
                online, _ = probe_agent(name)
                label = (
                    "🛡️"
                    if name in ("标准版", "扎古")
                    else "🔨"
                    if name == "重锤"
                    else "🎨"
                    if name == "绘墨"
                    else "🐝"
                    if name == "大黄蜂"
                    else "🔬"
                    if name == "Laser"
                    else "⚒️"
                )
                agents_out.append(
                    {
                        "name": name,
                        "port": port,
                        "online": online,
                        "label": label,
                        "last_seen": "—",
                    }
                )
            self.send_json({"agents": agents_out})

        elif path == "/intel/timeline":
            try:
                limit = int(qstr.get("n", 100))
            except ValueError:
                self.send_json({"error": 'query param "n" must be integer'}, 400)
                return
            limit = min(max(limit, 1), 1000)
            events = main_bus.read(proj, limit=limit)
            s_map = {
                "task.completed": "ok",
                "task.failed": "fail",
                "task.started": "run",
                "task.skipped": "skip",
            }
            timeline = []
            for e in events:
                t = e["type"]
                preview = ((e.get("data", {}) or {}).get("output_preview", ""))[:200] if t == "task.completed" else ""
                err = ((e.get("data", {}) or {}).get("error", ""))[:200] if t == "task.failed" else ""
                timeline.append(
                    {
                        "ts": e.get("ts", ""),
                        "type": t,
                        "agent": e.get("agent", "?"),
                        "stage": e.get("stage", ""),
                        "status": s_map.get(t, "wait"),
                        "preview": preview,
                        "error": err,
                        "title": ((e.get("data", {}) or {}).get("title", "")),
                    }
                )
            self.send_json({"project": proj, "total": len(timeline), "events": timeline})

        elif path == "/reporter":
            r = get_reporter()
            rtype = type(r).__name__
            has_rep = hasattr(r, "reporters")
            channels = [type(rep).__name__ for rep in r.reporters] if has_rep else [rtype]
            self.send_json({"type": rtype, "channels": channels})

        # ── Project Memory API（共享记忆） ─────────────────────
        elif path == "/project/list":
            try:
                projects = project_list()
                self.send_json({"projects": projects})
            except Exception as e:
                self.send_json({"error": str(e)}, 500)

        elif path.startswith("/project/"):
            # 路由: /project/<action>/<project_id>
            parts = path.split("/")
            if len(parts) >= 4:
                action = parts[2]
                project_id = parts[3]
                if action == "get":
                    result = project_get(project_id)
                    if result:
                        self.send_json(result)
                    else:
                        self.send_json({"error": f"project {project_id!r} not found"}, 404)
                elif action == "context":
                    context = project_read_context(project_id)
                    if context is not None:
                        self.send_json({"project_id": project_id, "context": context})
                    else:
                        self.send_json({"error": f"project {project_id!r} has no context"}, 404)
                else:
                    self.send_json({"error": f"unknown action {action!r}"}, 404)
            else:
                self.send_json({"error": "invalid path"}, 400)

        else:
            self.send_json({"error": "not found"}, 404)

    def _build_status(self, project_name):
        events = main_bus.read(project_name, limit=500)
        steps_cfg = []
        try:
            wf = load_workflow(project_name)
            steps_cfg = wf.get("steps", [])
        except Exception as exc:
            return {
                "project_name": project_name,
                "total_steps": 0,
                "run_start": "",
                "steps": [],
                "error": f"加载工作流失败: {exc}",
            }

        stage_status = {}
        stage_agent = {}
        stage_start = {}
        for e in events:
            s = e.get("stage", "")
            if not s:
                continue
            t = e["type"]
            if t == "task.started":
                if s not in stage_status:
                    stage_status[s] = "run"
                    stage_agent[s] = e.get("agent", "?")
                    stage_start[s] = e.get("ts", "")
            elif t == "task.completed":
                stage_status[s] = "ok"
                stage_agent[s] = e.get("agent", stage_agent.get(s, "?"))
            elif t == "task.failed":
                stage_status[s] = "fail"
                stage_agent[s] = e.get("agent", stage_agent.get(s, "?"))
            elif t == "task.skipped":
                stage_status[s] = "skip"

        steps_out = []
        for i, step in enumerate(steps_cfg):
            stage = step.get("stage", f"step-{i + 1}")
            s_status = stage_status.get(stage, "wait")
            started = stage_start.get(stage, "")
            duration = ""
            if started and s_status == "ok":
                for e in events:
                    if e.get("stage") == stage and e["type"] == "task.completed":
                        try:
                            ts_end = datetime.fromisoformat(e["ts"])
                            ts_start = datetime.fromisoformat(started)
                            secs = (ts_end - ts_start).seconds
                            duration = f"{secs // 60}m{secs % 60}s"
                        except Exception:
                            pass
                        break
            steps_out.append(
                {
                    "index": i + 1,
                    "title": step.get("title", stage),
                    "stage": stage,
                    "agent": stage_agent.get(stage, step.get("executor", "—")),
                    "status": s_status,
                    "status_class": s_status,
                    "duration": duration,
                }
            )

        proj_started = ""
        for e in events:
            ev_type = e.get("event", e.get("type", ""))
            if ev_type == "project.update":
                proj_started = e.get("ts", e.get("timestamp", ""))

        return {
            "project_name": project_name,
            "total_steps": len(steps_cfg),
            "run_start": proj_started,
            "steps": steps_out,
            "error": None,
        }

    def _handle_sse(self, project_name):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        last_poll = time.time()
        last_event_count = 0

        def _poll():
            nonlocal last_event_count, last_poll
            try:
                events = main_bus.read(project_name, limit=50)
                cnt = len(events)
                if cnt != last_event_count:
                    new_events = events[-(cnt - last_event_count) :] if cnt > last_event_count else []
                    last_event_count = cnt
                    status_payload = self._build_status(project_name)
                    self.wfile.write(
                        (f"event: status\ndata: {json.dumps(status_payload, ensure_ascii=False)}\n\n").encode()
                    )
                    self.wfile.flush()
                    for e in new_events:
                        self.wfile.write(
                            (f"event: bus_event\ndata: {json.dumps(e, ensure_ascii=False, default=str)}\n\n").encode()
                        )
                        self.wfile.flush()
                    last_poll = time.time()
                    return True
            except Exception:
                return False
            return False

        _poll()
        while True:
            if time.time() - last_poll > config_get("server.sse_heartbeat", 60):
                try:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                except Exception:
                    log_warn("SSE client disconnected (keepalive write failed)")
                    break
                last_poll = time.time()
            if not _poll():
                log_warn("SSE client disconnected")
                break
            time.sleep(config_get("server.sse_poll_interval", 2))


def _run_server():
    socketserver.TCPServer.allow_reuse_address = True
    port = get_server_port()
    srv = HTTPServer(("", port), DashHandler)
    print(f"  📡 Dashboard API: http://127.0.0.1:{port}")
    srv.serve_forever()


def start_api_server():
    t = Thread(target=_run_server, daemon=True)
    t.start()
