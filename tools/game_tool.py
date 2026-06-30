#!/usr/bin/env python3
"""
Glink Game Tool — 3D 遊戲開發能力模組
======================================

功能：
  - post_process: 給 Three.js 遊戲添加 Bloom + SSAO + ToneMapping
  - scaffold: 生成遊戲骨架
  - validate: 驗證遊戲文件完整性

使用方式：通過 Glink daemon API 調用
  POST /tool/game {"action": "post_process", "file": "/path/to/game.html"}
"""

import os
import re
import json
import shutil
import logging
from typing import Any

logger = logging.getLogger("glink-tool-game")

# ── Tool 元數據 ──
TOOL_INFO = {
    "name": "game-tool",
    "version": "1.0.0",
    "display_name": "🎮 3D 遊戲開發工具",
    "description": "Three.js 遊戲後處理、骨架生成、驗證",
    "actions": ["post_process", "scaffold", "validate"],
    "tags": ["game", "3d", "three-js", "post-processing"],
}


# ═══════════════════════════════════════════════════════════
# Post-Processing 注入
# ═══════════════════════════════════════════════════════════

_POST_PROCESS_IMPORTS = """
  // ── Glink Game Tool: Post-Processing ──
  import {
    EffectComposer
  } from 'https://cdn.jsdelivr.net/npm/three@0.174.0/examples/jsm/postprocessing/EffectComposer.js';
  import { RenderPass } from 'https://cdn.jsdelivr.net/npm/three@0.174.0/examples/jsm/postprocessing/RenderPass.js';
  import { UnrealBloomPass } from 'https://cdn.jsdelivr.net/npm/three@0.174.0/examples/jsm/postprocessing/UnrealBloomPass.js';
  import { SSAOPass } from 'https://cdn.jsdelivr.net/npm/three@0.174.0/examples/jsm/postprocessing/SSAOPass.js';
  import { OutputPass } from 'https://cdn.jsdelivr.net/npm/three@0.174.0/examples/jsm/postprocessing/OutputPass.js';
"""

_POST_PROCESS_CODE = """
  // ── Glink Game Tool: Post-Processing Setup ──
  const composer = new EffectComposer(renderer);
  const renderPass = new RenderPass(scene, camera);
  composer.addPass(renderPass);

  // UnrealBloomPass — 輝光效果
  const bloomPass = new UnrealBloomPass(
    new THREE.Vector2(window.innerWidth, window.innerHeight),
    0.3,   // strength
    0.5,   // radius
    0.1    // threshold
  );
  composer.addPass(bloomPass);

  // SSAOPass — 環境光遮蔽（接觸陰影）
  const ssaoPass = new SSAOPass(scene, camera);
  ssaoPass.kernelRadius = 8;
  ssaoPass.minDistance = 0.005;
  ssaoPass.maxDistance = 0.1;
  composer.addPass(ssaoPass);

  // OutputPass — 色調映射（必須最後）
  const outputPass = new OutputPass();
  composer.addPass(outputPass);

  // 窗口縮放時更新 composer
  window.addEventListener('resize', () => {
    const w = window.innerWidth;
    const h = window.innerHeight;
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
    renderer.setSize(w, h);
    composer.setSize(w, h);
  });
"""

_POST_PROCESS_LOOP = """
    // ── Glink Game Tool: Post-Processing Render ──
    composer.render();
"""


def apply_post_process(filepath: str, bloom_strength: float = 0.3) -> dict[str, Any]:
    """
    給 Three.js 遊戲 HTML 添加後處理（Bloom + SSAO + ToneMapping）。
    
    返回 {ok, filepath, changes_made}
    """
    if not os.path.exists(filepath):
        return {"ok": False, "error": f"File not found: {filepath}"}

    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()

    changes = []

    # 1. 檢查是否已經有 Post-Processing
    if "EffectComposer" in content and "composer.render" in content:
        return {"ok": True, "filepath": filepath, "changes_made": ["Already has post-processing, skipped"]}

    # 2. 添加 Imports — 在最後一個 import 之後插入
    import_match = list(re.finditer(r"import\s+.*?from\s+['\"].*?['\"]\s*;?\n?", content, re.MULTILINE))
    if import_match:
        last_import = import_match[-1]
        insert_pos = last_import.end()
        content = content[:insert_pos] + _POST_PROCESS_IMPORTS + content[insert_pos:]
        changes.append("Added post-processing imports")

    # 3. 添加 Setup Code — 在 renderer.setSize 或調用 requestAnimationFrame 之前
    setup_code = _POST_PROCESS_CODE.replace("0.3", str(bloom_strength))
    
    # 尋找插入點：在 render loop 之前（第一個 requestAnimationFrame 或 animate 函數調用前）
    raf_match = re.search(r"requestAnimationFrame\s*\(", content)
    if raf_match:
        # 找 animate 函數定義
        animate_match = re.search(r"(function\s+animate|const\s+animate\s*=|let\s+animate\s*=)", content)
        if animate_match:
            # 在 animate 函數定義之前插入 setup
            insert_pos = animate_match.start()
            content = content[:insert_pos] + setup_code + "\n" + content[insert_pos:]
            changes.append("Added composer setup before animate()")
        else:
            # 在 requestAnimationFrame 之前插入
            insert_pos = raf_match.start()
            # 往前找行首
            line_start = content.rfind("\n", 0, insert_pos) + 1
            content = content[:line_start] + setup_code + "\n" + content[line_start:]
            changes.append("Added composer setup before requestAnimationFrame")
    else:
        # 在文件尾部插入
        content += "\n" + setup_code
        changes.append("Added composer setup at end of file")

    # 4. 替換 renderer.render 為 composer.render
    render_match = re.search(r"renderer\.render\s*\(\s*scene\s*,\s*camera\s*\)\s*;?", content)
    if render_match:
        content = content[:render_match.start()] + _POST_PROCESS_LOOP.strip() + content[render_match.end():]
        changes.append("Replaced renderer.render() with composer.render()")

    # 5. 檢查 ACESFilmic tone mapping
    if "ACESFilmic" not in content:
        # 在 renderer 創建處添加 toneMapping
        tm_code = "\nrenderer.toneMapping = THREE.ACESFilmicToneMapping;\nrenderer.toneMappingExposure = 1.0;\n"
        renderer_match = re.search(r"new\s+THREE\.WebGLRenderer\s*\([^)]*\)", content)
        if renderer_match:
            insert_pos = renderer_match.end()
            content = content[:insert_pos] + content[insert_pos:].replace(
                "\n", tm_code + "\n", 1
            )
            # Simpler: just insert after the renderer creation line
            line_after = content.find("\n", renderer_match.end()) + 1
            content = content[:line_after] + tm_code + content[line_after:]
            changes.append("Added ACESFilmic tone mapping")

    # 6. 寫回文件
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)

    backup_path = filepath + ".bak"
    shutil.copy2(filepath, backup_path)
    changes.append(f"Backup saved: {backup_path}")

    return {
        "ok": True,
        "filepath": filepath,
        "backup": backup_path,
        "changes_made": changes,
    }


# ═══════════════════════════════════════════════════════════
# Scaffold — 遊戲骨架生成
# ═══════════════════════════════════════════════════════════

_GAME_SCAFFOLD = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>3D Game — Glink Scaffold</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { overflow: hidden; background: #1a1a2e; font-family: sans-serif; }
  canvas { display: block; }
  #info {
    position: fixed; top: 12px; left: 12px; color: rgba(255,255,255,0.7);
    font-size: 13px; font-family: monospace;
    text-shadow: 0 1px 4px rgba(0,0,0,0.8);
    pointer-events: none; z-index: 10;
  }
</style>
</head>
<body>
<div id="info">🎮 Glink Game Scaffold · Use the tool pipeline!</div>

<script type="importmap">
{
  "imports": {
    "three": "https://cdn.jsdelivr.net/npm/three@0.174.0/build/three.module.js",
    "three/addons/": "https://cdn.jsdelivr.net/npm/three@0.174.0/examples/jsm/"
  }
}
</script>

<script type="module">
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

// ── Scene ──
const scene = new THREE.Scene();
scene.background = new THREE.Color(0x87ceeb); // 天藍色
scene.fog = new THREE.Fog(0x87ceeb, 50, 100);

// ── Camera ──
const camera = new THREE.PerspectiveCamera(60, window.innerWidth / window.innerHeight, 0.1, 200);
camera.position.set(20, 15, 20);
camera.lookAt(0, 0, 0);

// ── Renderer ──
const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setSize(window.innerWidth, window.innerHeight);
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
renderer.shadowMap.enabled = true;
renderer.shadowMap.type = THREE.PCFSoftShadowMap;
document.body.appendChild(renderer.domElement);

// ── Controls ──
const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
controls.dampingFactor = 0.08;
controls.target.set(0, 2, 0);

// ── Lights ──
const ambientLight = new THREE.AmbientLight(0x404060, 0.5);
scene.add(ambientLight);

const dirLight = new THREE.DirectionalLight(0xffeedd, 1.2);
dirLight.position.set(30, 30, 20);
dirLight.castShadow = true;
dirLight.shadow.mapSize.width = 1024;
dirLight.shadow.mapSize.height = 1024;
dirLight.shadow.camera.near = 0.5;
dirLight.shadow.camera.far = 60;
dirLight.shadow.camera.left = -30;
dirLight.shadow.camera.right = 30;
dirLight.shadow.camera.top = 30;
dirLight.shadow.camera.bottom = -30;
scene.add(dirLight);

const hemiLight = new THREE.HemisphereLight(0x87ceeb, 0x362d28, 0.4);
scene.add(hemiLight);

// ── Ground ──
const groundGeo = new THREE.PlaneGeometry(60, 60);
const groundMat = new THREE.MeshStandardMaterial({
  color: 0x4a7c3f,
  roughness: 0.9,
  metalness: 0.0,
});
const ground = new THREE.Mesh(groundGeo, groundMat);
ground.rotation.x = -Math.PI / 2;
ground.receiveShadow = true;
scene.add(ground);

// ── Demo Objects ──
// 一些測試方塊
const colors = [0xe74c3c, 0x3498db, 0x2ecc71, 0xf39c12, 0x9b59b6, 0x1abc9c];
const boxGeo = new THREE.BoxGeometry(1, 1, 1);
for (let i = 0; i < 30; i++) {
  const mat = new THREE.MeshStandardMaterial({
    color: colors[i % colors.length],
    roughness: 0.4 + Math.random() * 0.5,
    metalness: Math.random() * 0.3,
  });
  const mesh = new THREE.Mesh(boxGeo, mat);
  mesh.position.set(
    (Math.random() - 0.5) * 20,
    0.5 + Math.random() * 3,
    (Math.random() - 0.5) * 20
  );
  mesh.castShadow = true;
  mesh.receiveShadow = true;
  scene.add(mesh);
}

// ── Grid ──
const grid = new THREE.GridHelper(60, 20, 0x888888, 0x444444);
grid.position.y = 0.01;
scene.add(grid);

// ── Resize ──
window.addEventListener('resize', () => {
  camera.aspect = window.innerWidth / window.innerHeight;
  camera.updateProjectionMatrix();
  renderer.setSize(window.innerWidth, window.innerHeight);
});

// ── Animate ──
function animate() {
  requestAnimationFrame(animate);
  controls.update();
  renderer.render(scene, camera);
}
animate();

// 通知 Glink 工具系統
console.log('[Glink-Scaffold] Game scene ready');
</script>
</body>
</html>
"""


def generate_scaffold(output_path: str = "") -> dict[str, Any]:
    """生成 3D 遊戲骨架文件"""
    if not output_path:
        output_path = os.path.expanduser("~/Desktop/game-scaffold.html")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(_GAME_SCAFFOLD)

    return {
        "ok": True,
        "filepath": output_path,
        "size": len(_GAME_SCAFFOLD),
    }


# ═══════════════════════════════════════════════════════════
# Validate — 遊戲驗證
# ═══════════════════════════════════════════════════════════

def validate_game(filepath: str) -> dict[str, Any]:
    """驗證遊戲 HTML 文件的完整性"""
    if not os.path.exists(filepath):
        return {"ok": False, "error": f"File not found: {filepath}"}

    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()

    checks = {}

    # 基本檢查
    checks["file_size"] = {"ok": len(content) > 5000, "value": len(content)}

    # Three.js
    checks["three_import"] = {"ok": "three.module.js" in content or "three/build" in content, "value": "three.module.js" in content}
    checks["three_renderer"] = {"ok": "WebGLRenderer" in content, "value": "WebGLRenderer" in content}
    checks["render_loop"] = {"ok": "requestAnimationFrame" in content, "value": "requestAnimationFrame" in content}

    # 渲染質量
    checks["shadows"] = {"ok": "shadowMap" in content, "value": "shadowMap" in content}
    checks["antialias"] = {"ok": "antialias" in content, "value": "antialias" in content}

    # 後處理（非必須）
    checks["post_processing"] = {
        "ok": "EffectComposer" in content,
        "value": "EffectComposer" in content,
        "optional": True,
    }

    # PBR 材質（非必須）
    checks["pbr_materials"] = {
        "ok": "MeshStandardMaterial" in content or "MeshPhysicalMaterial" in content,
        "value": "MeshStandardMaterial" in content or "MeshPhysicalMaterial" in content,
        "optional": True,
    }

    failed = [k for k, v in checks.items() if not v.get("ok") and not v.get("optional")]
    warnings = [k for k, v in checks.items() if not v.get("ok") and v.get("optional")]

    return {
        "ok": len(failed) == 0,
        "filepath": filepath,
        "checks_passed": sum(1 for v in checks.values() if v.get("ok")),
        "checks_total": len(checks),
        "failed_checks": failed,
        "warnings": warnings,
        "details": checks,
    }


# ═══════════════════════════════════════════════════════════
# 執行入口
# ═══════════════════════════════════════════════════════════

def execute(action: str, params: dict = None) -> dict[str, Any]:
    """
    Glink Tool 執行入口。
    由 Glink daemon 的 /tool/game 端點調用。
    """
    if params is None:
        params = {}

    if action == "post_process":
        filepath = params.get("file", params.get("filepath", ""))
        if not filepath:
            return {"ok": False, "error": "Missing required param: file"}
        return apply_post_process(
            filepath=filepath,
            bloom_strength=params.get("bloom_strength", 0.3),
        )

    elif action == "scaffold":
        output = params.get("output", "")
        return generate_scaffold(output)

    elif action == "validate":
        filepath = params.get("file", params.get("filepath", ""))
        if not filepath:
            return {"ok": False, "error": "Missing required param: file"}
        return validate_game(filepath)

    else:
        return {"ok": False, "error": f"Unknown action: {action}", "available_actions": TOOL_INFO["actions"]}
