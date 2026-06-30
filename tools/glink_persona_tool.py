#!/usr/bin/env python3
"""
Glink Persona Tool — 任務→人格匹配注入

/glink/tool/persona 端點

Actions:
  match       — 匹配任務，返回最佳人格
  inject      — 匹配任務，返回注入後的 prompt
  status      — 引擎狀態

Example:
  POST /tool/persona
  {"action": "match", "params": {"task": "幫我做一個後台CRUD"}}
  
  POST /tool/persona  
  {"action": "inject", "params": {
    "task": "幫我做一個後台CRUD",
    "prompt": "你是一個全棧工程師...",
    "top_k": 1
  }}
"""

import os, sys, json
from pathlib import Path

# 將 persona skill 目錄加入 sys.path
PERSONA_DIR = Path.home() / "opprime" / "skills" / "persona"
if str(PERSONA_DIR) not in sys.path:
    sys.path.insert(0, str(PERSONA_DIR))

def execute(action, params):
    """Glink tool 入口 — 由 api.py 調用"""
    
    if action == "status":
        return _status()
    elif action == "match":
        return _match(params)
    elif action == "inject":
        return _inject(params)
    else:
        return {"error": f"Unknown action: {action}"}


def _status():
    """引擎狀態檢查"""
    index_path = PERSONA_DIR / "persona-index.json"
    return {
        "ready": index_path.exists(),
        "index_size": index_path.stat().st_size if index_path.exists() else 0,
        "index_path": str(index_path)
    }


def _match(params):
    """匹配任務→人格"""
    from persona_tool import match_persona, glink_tool
    task = params.get("task", "")
    top_k = params.get("top_k", 3)
    
    if not task:
        return {"matched": False, "error": "task is required"}
    
    result = glink_tool({"task": task, "top_k": top_k})
    return result


def _inject(params):
    """匹配任務，返回注入後的 prompt"""
    task = params.get("task", "")
    prompt = params.get("prompt", "")
    top_k = params.get("top_k", 1)
    
    if not task:
        return {"matched": False, "error": "task is required"}
    
    from persona_tool import inject_persona
    result = inject_persona(task, prompt, top_k)
    
    # 也返回匹配信息
    from persona_tool import match_persona
    matches = match_persona(task, top_k)
    
    return {
        "matched": bool(matches),
        "persona": [
            {"name": m["name"], "division": m["division"], "score": m["score"], "emoji": m["emoji"]}
            for m in matches[:3]
        ] if matches else [],
        "injected_prompt": result
    }
