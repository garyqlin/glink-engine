#!/usr/bin/env python3
"""
沙盒建造游戏 — 性能压力测试
通过 CDP (Chrome DevTools Protocol) 控制 headless Chrome，
注入 JS 脚本批量放置/删除方块，测量帧率/延迟/内存。
"""

import atexit
import json
import os
import statistics
import subprocess
import time
from typing import Any

import requests
import websocket

GAME_URL = "http://127.0.0.1:8081/sandbox-builder.html"
CDP_PORT = 9222
CDP_BASE = f"http://localhost:{CDP_PORT}"
CHROME_BIN = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

chrome_proc = None


def cleanup():
    global chrome_proc
    if chrome_proc:
        try:
            chrome_proc.terminate()
            chrome_proc.wait(timeout=5)
        except Exception:
            chrome_proc.kill()
        print("[CLEANUP] Chrome 已关闭")


atexit.register(cleanup)


def start_chrome():
    """启动 headless Chrome 并开启远程调试端口"""
    global chrome_proc
    print("[SETUP] 启动 Chrome headless ...")
    chrome_proc = subprocess.Popen(
        [
            CHROME_BIN,
            "--headless",
            f"--remote-debugging-port={CDP_PORT}",
            "--no-sandbox",
            "--disable-gpu",
            "--disable-software-rasterizer",
            "--window-size=1280,720",
            "--no-first-run",
            "--disable-extensions",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # 等待 Chrome 启动
    for _ in range(20):
        time.sleep(0.5)
        try:
            resp = requests.get(f"{CDP_BASE}/json/version", timeout=2)
            if resp.status_code == 200:
                print(f"[SETUP] Chrome 就绪 (PID={chrome_proc.pid})")
                return
        except Exception:
            pass
    raise RuntimeError("Chrome 启动超时")


def get_ws_url() -> str:
    """获取页面 WebSocket URL"""
    resp = requests.get(f"{CDP_BASE}/json", timeout=5)
    pages = resp.json()
    for p in pages:
        if p.get("url") and "sandbox-builder" in p.get("url", ""):
            return p["webSocketDebuggerUrl"]
    raise RuntimeError("未找到游戏页面")


class CDPClient:
    """CDP WebSocket 客户端"""

    def __init__(self, ws_url: str):
        self.ws = websocket.create_connection(ws_url, timeout=10)
        self._msg_id = 0

    def send(self, method: str, params: dict | None = None) -> dict:
        self._msg_id += 1
        msg = {"id": self._msg_id, "method": method}
        if params:
            msg["params"] = params
        self.ws.send(json.dumps(msg))
        return self._recv_result()

    def _recv_result(self) -> dict:
        """读取直到收到匹配的 result 消息"""
        while True:
            raw = self.ws.recv()
            data = json.loads(raw)
            if "id" in data and data["id"] == self._msg_id:
                if "error" in data:
                    raise RuntimeError(f"CDP错误: {data['error']}")
                return data.get("result", {})
            # 忽略非结果消息（如事件通知）

    def evaluate(self, expression: str, await_promise: bool = False) -> Any:
        """在页面中执行 JS 并返回结果"""
        result = self.send(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": await_promise,
                "timeout": 15000,
            },
        )
        r = result.get("result", {})
        if r.get("subtype") == "error":
            raise RuntimeError(f"JS错误: {r.get('description', 'unknown')}")
        return r.get("value")

    def close(self):
        self.ws.close()


def navigate_and_wait(client: CDPClient):
    """导航到游戏页面并等待 Three.js 初始化完成"""
    print("[NAV] 导航到游戏页面 ...")
    client.send("Page.enable")
    client.send("Page.navigate", {"url": GAME_URL})

    # 等待页面加载 & Three.js 初始化
    for i in range(30):
        time.sleep(1)
        try:
            ready = client.evaluate(
                "typeof placeBlock === 'function' && typeof blocks !== 'undefined' && blocks !== null"
            )
            if ready:
                print(f"[NAV] 游戏初始化完成 (等待 {i + 1}s)")
                time.sleep(1)  # 额外等一秒确保渲染稳定
                return
        except Exception as e:
            print(f"[NAV] 等待中... ({e})")
    raise RuntimeError("游戏初始化超时")


def wait_frames(client: CDPClient, n: int = 30):
    """等待 N 帧稳定后返回"""
    time.sleep(n / 60.0)


def perf_test(label: str, client: CDPClient, setup: str, n_blocks: int):
    """
    通用性能测试：
    1. 执行 setup JS
    2. 获取初始内存
    3. 批量放置 n_blocks 个方块，每 50 个记录一次时间
    4. 等待稳定后取 FPS
    5. 返回报告
    """
    print(f"\n{'=' * 60}")
    print(f"  [{label}] 放置 {n_blocks} 个方块")
    print(f"{'=' * 60}")

    # 执行 setup（例如清空场景）
    if setup:
        client.evaluate(setup)
        time.sleep(0.3)

    # 获取初始状态
    init_fps = client.evaluate("currentFPS")
    init_blocks = client.evaluate("blocks.length")
    print(f"  初始状态: {init_blocks} 块, FPS={init_fps}")

    # 批量放置 — 分批发，每批记录时间
    batch_size = 50
    batches = n_blocks // batch_size
    rest = n_blocks % batch_size

    # 类型轮换
    types_src = "[...Object.keys(BLOCK_TYPES)]"

    batch_times = []
    for b in range(batches):
        js_code = f"""
        (() => {{
            const types = {types_src};
            const start = performance.now();
            for (let i = 0; i < {batch_size}; i++) {{
                const t = types[i % types.length];
                let x = Math.round(Math.random() * 30 - 15);
                let z = Math.round(Math.random() * 30 - 15);
                let y = 0.5;
                // 简单堆叠
                while (isPositionOccupied({{x, y, z}}) && y < 10) y += 1;
                placeBlock(t, new THREE.Vector3(x, y, z));
            }}
            const end = performance.now();
            return end - start;
        }})()
        """
        elapsed = client.evaluate(js_code)
        batch_times.append(elapsed)
        print(f"  批次 {b + 1}/{batches}: {elapsed:.1f}ms ({batch_size} 块, {batch_size / elapsed * 1000:.0f} 块/s)")

    # 剩余
    if rest > 0:
        js_code = f"""
        (() => {{
            const types = {types_src};
            const start = performance.now();
            for (let i = 0; i < {rest}; i++) {{
                const t = types[i % types.length];
                let x = Math.round(Math.random() * 30 - 15);
                let z = Math.round(Math.random() * 30 - 15);
                let y = 0.5;
                while (isPositionOccupied({{x, y, z}}) && y < 10) y += 1;
                placeBlock(t, new THREE.Vector3(x, y, z));
            }}
            const end = performance.now();
            return end - start;
        }})()
        """
        elapsed = client.evaluate(js_code)
        batch_times.append(elapsed)
        print(f"  批次 {batches + 1}/{batches + 1}: {elapsed:.1f}ms ({rest} 块)")

    total_time = sum(batch_times)
    avg_batch = statistics.mean(batch_times) if batch_times else 0
    total_blocks = client.evaluate("blocks.length")

    # 等待 30 帧稳定后取 FPS
    wait_frames(client, 30)
    final_fps = client.evaluate("currentFPS")
    print(f"  最终: {total_blocks} 块, FPS={final_fps}")
    print(f"  总耗时: {total_time:.0f}ms, 平均 {avg_batch:.0f}ms/批次")

    return {
        "label": label,
        "target_blocks": n_blocks,
        "actual_blocks": total_blocks,
        "total_time_ms": round(total_time, 1),
        "avg_batch_ms": round(avg_batch, 1),
        "init_fps": init_fps,
        "final_fps": final_fps,
        "batches": len(batch_times),
    }


def perf_clear(label: str, client: CDPClient):
    """测试清空所有方块的耗时"""
    print(f"\n  清空测试: {label}")
    n = client.evaluate("blocks.length")
    elapsed = client.evaluate(
        """
        (() => {
            const start = performance.now();
            clearAllBlocks();
            const end = performance.now();
            return end - start;
        })()
        """
    )
    print(f"  清空 {n} 块: {elapsed:.1f}ms")
    return {"label": label, "cleared_blocks": n, "time_ms": round(elapsed, 1)}


def perf_save_load(client: CDPClient, n_blocks: int, slot: int):
    """
    测试 save/load 耗时
    1. 清空 → 放 n 块 → save(1) 计时
    2. 清空 → load(1) 计时
    """
    print(f"\n{'=' * 60}")
    print(f"  [Save/Load] 数据规模: {n_blocks} 块")
    print(f"{'=' * 60}")

    # 先放 n 块
    client.evaluate("clearAllBlocks()")
    time.sleep(0.3)
    types_src = "[...Object.keys(BLOCK_TYPES)]"
    js_place = f"""
    (() => {{
        const types = {types_src};
        for (let i = 0; i < {n_blocks}; i++) {{
            const t = types[i % types.length];
            let x = Math.round(Math.random() * 30 - 15);
            let z = Math.round(Math.random() * 30 - 15);
            let y = 0.5;
            while (isPositionOccupied({{x, y, z}}) && y < 10) y += 1;
            placeBlock(t, new THREE.Vector3(x, y, z));
        }}
    }})()
    """
    client.evaluate(js_place)
    time.sleep(0.5)
    placed = client.evaluate("blocks.length")
    print(f"  已放置: {placed} 块")

    # Save 计时
    save_time = client.evaluate(
        f"(() => {{ const s = performance.now(); saveGame({slot}); return performance.now() - s; }})()"
    )
    print(f"  save({slot}): {save_time:.1f}ms")

    # 清空 + Load 计时
    client.evaluate("clearAllBlocks()")
    time.sleep(0.3)
    load_time = client.evaluate(
        f"(() => {{ const s = performance.now(); loadGame({slot}); return performance.now() - s; }})()"
    )
    loaded = client.evaluate("blocks.length")
    print(f"  load({slot}): {load_time:.1f}ms → {loaded} 块")

    return {
        "blocks": placed,
        "save_ms": round(save_time, 1),
        "load_ms": round(load_time, 1),
        "loaded_blocks": loaded,
    }


def perf_memory(client: CDPClient, n_blocks: int):
    """监测内存：放 200 块前后对比"""
    print(f"\n{'=' * 60}")
    print(f"  [Memory] 放置 {n_blocks} 块前后内存变化")
    print(f"{'=' * 60}")

    client.evaluate("clearAllBlocks()")
    time.sleep(0.3)

    mem_before = client.send("Performance.getMetrics")
    js_heap_before = next(
        (m["value"] for m in mem_before.get("metrics", []) if m["name"] == "JSHeapUsedSize"),
        0,
    )
    print(f"  JS堆(前): {js_heap_before / 1024 / 1024:.2f} MB")

    types_src = "[...Object.keys(BLOCK_TYPES)]"
    js_place = f"""
    (() => {{
        const types = {types_src};
        for (let i = 0; i < {n_blocks}; i++) {{
            const t = types[i % types.length];
            let x = Math.round(Math.random() * 30 - 15);
            let z = Math.round(Math.random() * 30 - 15);
            let y = 0.5;
            while (isPositionOccupied({{x, y, z}}) && y < 10) y += 1;
            placeBlock(t, new THREE.Vector3(x, y, z));
        }}
    }})()
    """
    t0 = time.time()
    client.evaluate(js_place)
    place_time = (time.time() - t0) * 1000

    time.sleep(0.5)
    mem_after = client.send("Performance.getMetrics")
    js_heap_after = next(
        (m["value"] for m in mem_after.get("metrics", []) if m["name"] == "JSHeapUsedSize"),
        0,
    )
    delta_mb = (js_heap_after - js_heap_before) / 1024 / 1024
    per_block = delta_mb / n_blocks * 1024 if n_blocks > 0 else 0

    print(f"  JS堆(后): {js_heap_after / 1024 / 1024:.2f} MB")
    print(f"  增量: {delta_mb:+.2f} MB ({per_block:.1f} KB/块)")

    return {
        "blocks": n_blocks,
        "heap_before_mb": round(js_heap_before / 1024 / 1024, 2),
        "heap_after_mb": round(js_heap_after / 1024 / 1024, 2),
        "delta_mb": round(delta_mb, 2),
        "kb_per_block": round(per_block, 1),
        "place_time_ms": round(place_time, 1),
    }


def perf_rapid_click(client: CDPClient, n_iter: int):
    """
    快速连续放置/删除模拟
    每轮：连放 20 块 → 连删 20 块，重复 n_iter 次
    记录每轮耗时，看是否退化
    """
    print(f"\n{'=' * 60}")
    print(f"  [RapidClick] 快速放置/删除 {n_iter} 轮")
    print(f"{'=' * 60}")

    client.evaluate("clearAllBlocks()")
    time.sleep(0.3)

    round_times = []
    for r in range(n_iter):
        batch_size = 20
        js_code = f"""
        (() => {{
            const types = [...Object.keys(BLOCK_TYPES)];
            const start = performance.now();
            // 连放
            for (let i = 0; i < {batch_size}; i++) {{
                const t = types[i % types.length];
                let x = Math.round(Math.random() * 30 - 15);
                let z = Math.round(Math.random() * 30 - 15);
                let y = 0.5;
                while (isPositionOccupied({{x, y, z}}) && y < 10) y += 1;
                placeBlock(t, new THREE.Vector3(x, y, z));
            }}
            // 连删
            const toRemove = blocks.slice(-{batch_size});
            for (const b of toRemove) {{
                removeBlock(b.mesh);
            }}
            const end = performance.now();
            return end - start;
        }})()
        """
        elapsed = client.evaluate(js_code)
        round_times.append(elapsed)
        fps_now = client.evaluate("currentFPS")
        blocks_now = client.evaluate("blocks.length")
        print(f"  第 {r + 1:2d} 轮: {elapsed:6.1f}ms | 剩余 {blocks_now} 块 | FPS={fps_now}")

    avg_first_5 = statistics.mean(round_times[:5]) if len(round_times) >= 5 else statistics.mean(round_times)
    avg_last_5 = statistics.mean(round_times[-5:]) if len(round_times) >= 5 else statistics.mean(round_times)
    degradation = (avg_last_5 - avg_first_5) / avg_first_5 * 100 if avg_first_5 > 0 else 0

    print(f"  前5轮平均: {avg_first_5:.1f}ms → 后5轮平均: {avg_last_5:.1f}ms → 退化 {degradation:+.1f}%")

    return {
        "rounds": n_iter,
        "avg_first_5_ms": round(avg_first_5, 1),
        "avg_last_5_ms": round(avg_last_5, 1),
        "degradation_pct": round(degradation, 1),
        "all_rounds": [round(t, 1) for t in round_times],
    }


def generate_report(results: dict) -> str:
    """生成总结报告"""
    lines = []
    lines.append("")
    lines.append("=" * 70)
    lines.append("   🧱 沙盒建造游戏 — 性能压力测试报告")
    lines.append("=" * 70)
    lines.append("")

    # 1. 批量放置
    lines.append("━" * 70)
    lines.append("  📊 测试1: 批量放置 (100→500 方块)")
    lines.append("━" * 70)
    for r in results.get("batch_place", []):
        lines.append(
            f"  {r['label']:30s} | 实际 {r['actual_blocks']:4d} 块 | "
            f"总耗时 {r['total_time_ms']:6.1f}ms | "
            f"FPS {r['init_fps']}→{r['final_fps']}"
        )

    # 2. Save/Load
    lines.append("")
    lines.append("━" * 70)
    lines.append("  💾 测试2: Save/Load (100/500 块数据量)")
    lines.append("━" * 70)
    for r in results.get("save_load", []):
        lines.append(
            f"  {r['blocks']:4d} 块 | save: {r['save_ms']:6.1f}ms | "
            f"load: {r['load_ms']:6.1f}ms | 加载后 {r['loaded_blocks']} 块"
        )

    # 3. 内存
    lines.append("")
    lines.append("━" * 70)
    lines.append("  🧠 测试3: 内存变化")
    lines.append("━" * 70)
    for r in results.get("memory", []):
        lines.append(
            f"  {r['blocks']:4d} 块 | 堆 {r['heap_before_mb']:.1f}→{r['heap_after_mb']:.1f} MB "
            f"({r['delta_mb']:+.1f}MB, {r['kb_per_block']:.1f}KB/块)"
        )

    # 4. 快速点击
    lines.append("")
    lines.append("━" * 70)
    lines.append("  ⚡ 测试4: 快速连续点击 (放置+删除循环)")
    lines.append("━" * 70)
    for r in results.get("rapid_click", []):
        lines.append(
            f"  {r['rounds']} 轮 | 前5均值 {r['avg_first_5_ms']:.1f}ms → "
            f"后5均值 {r['avg_last_5_ms']:.1f}ms → 退化 {r['degradation_pct']:+.1f}%"
        )

    # 瓶颈分析
    lines.append("")
    lines.append("━" * 70)
    lines.append("  🔍 瓶颈分析")
    lines.append("━" * 70)

    bottlenecks = analyze_bottlenecks(results)
    for b in bottlenecks:
        lines.append(f"  {b}")

    lines.append("")
    lines.append("=" * 70)
    lines.append("  测试完成")
    lines.append("=" * 70)

    return "\n".join(lines)


def analyze_bottlenecks(results: dict) -> list[str]:
    """分析性能瓶颈"""
    issues = []

    # 检查 FPS 下降
    for r in results.get("batch_place", []):
        if r.get("final_fps", 60) < 20:
            issues.append(f"🚨 {r['label']}: FPS 降至 {r['final_fps']}（低于20），存在严重渲染瓶颈")
        elif r.get("final_fps", 60) < 40:
            issues.append(f"⚠️  {r['label']}: FPS 降至 {r['final_fps']}（低于40），存在轻度渲染瓶颈")

    # 检查 save/load
    for r in results.get("save_load", []):
        if r.get("save_ms", 0) > 500:
            issues.append(f"🐢 save({r['blocks']}块): {r['save_ms']}ms，JSON序列化+localStorage写入慢")
        if r.get("load_ms", 0) > 1000:
            issues.append(f"🐢 load({r['blocks']}块): {r['load_ms']}ms，需要优化重建流程")

    # 检查内存
    for r in results.get("memory", []):
        if r.get("kb_per_block", 0) > 50:
            issues.append(f"💾 每块内存 {r['kb_per_block']}KB（偏高），注意 Three.js Mesh+Physics 内存占用")

    # 检查快速点击退化
    for r in results.get("rapid_click", []):
        if r.get("degradation_pct", 0) > 20:
            issues.append(f"📈 快速操作退化 {r['degradation_pct']:.0f}%（>20%），可能存在 GC 压力或泄漏")
        elif r.get("degradation_pct", 0) > 10:
            issues.append(f"📈 快速操作轻度退化 {r['degradation_pct']:.0f}%（>10%），需关注")

    if not issues:
        issues.append("✅ 所有测试通过，未发现明显瓶颈")

    return issues


def main():
    start_chrome()
    time.sleep(1)

    # 导航到游戏
    client = CDPClient(get_ws_url())
    try:
        navigate_and_wait(client)

        # 启用性能指标收集
        client.send("Performance.enable")

        results: dict[str, list] = {
            "batch_place": [],
            "save_load": [],
            "memory": [],
            "rapid_click": [],
        }

        # === 测试1: 100 个方块 ===
        r = perf_test("测试1-A: 100块", client, "clearAllBlocks()", 100)
        results["batch_place"].append(r)

        # === 测试2: 500 个方块 (在已有 100 上追加 400) ===
        # 先清空再一次性放 500
        r = perf_test("测试1-B: 500块", client, "clearAllBlocks()", 500)
        results["batch_place"].append(r)

        # === Save/Load: 100 块数据 ===
        r = perf_save_load(client, 100, 1)
        results["save_load"].append(r)

        # === Save/Load: 500 块数据 ===
        r = perf_save_load(client, 500, 2)
        results["save_load"].append(r)

        # === 内存测试: 200 块 ===
        r = perf_memory(client, 200)
        results["memory"].append(r)

        # === 快速连续点击 ===
        r = perf_rapid_click(client, n_iter=15)
        results["rapid_click"].append(r)

        # === 清空后再测 200 块内存 ===
        r = perf_memory(client, 200)
        results["memory"].append(r)

        # 生成报告
        report = generate_report(results)
        print(report)

        # 写入报告文件
        report_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "perf_report.txt")
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(report)
        print(f"\n📄 报告已写入: {report_path}")

        # 同时写 JSON
        json_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "perf_report.json")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2, ensure_ascii=False)
        print(f"📄 JSON数据: {json_path}")

    finally:
        client.close()


if __name__ == "__main__":
    main()
