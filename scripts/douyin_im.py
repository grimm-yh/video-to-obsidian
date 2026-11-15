#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""douyin_im.py — 抖音私信观察器（第一步：验证接口 + 提链接可行性）。

分工：
    login  弹可见窗口，扫码登录小号；登录态存独立 profile（与抓视频的临时 profile 分开）
    scan   打开消息页，CDP 监听页面自己发的请求 + 抓 DOM 文本，落盘供分析

设计约束（沿用 fetch_douyin 的实测结论）：
    - 启动/采集/关闭必须同一进程内串行完成
    - 复用端口前先验证有可用页面，僵尸按 PID 清理（只杀占调试端口的进程）
    - 登录必须有窗口，headless 只用于 scan

用法：
    python douyin_im.py login                 # 扫码登录小号（弹窗，等扫码）
    python douyin_im.py scan --seconds 30     # 观察消息页 30s，产出观察 JSON
    python douyin_im.py scan --show           # 观察时也弹窗口（调试用）
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
except Exception:
    pass

from fetch_douyin import (CDP, _kill_pids, _port_pages, _port_pids, _truthy,
                          _version, shutdown)
import config


def log(m: str) -> None:
    print(f"[im] {m}", flush=True)


PROFILE = os.path.join(os.path.expanduser("~"), ".vto-douyin-im-profile")
DEFAULT_PORT = 9225
MESSAGE_URL = "https://www.douyin.com/message"

# 消息相关接口/长连接的 URL 关键词，命中就保留响应体
KEYWORDS = ("im/", "message", "msg", "conversation", "chat", "send_video",
            "share", "websocket", "ws")


def ensure_browser(exe: str, port: int, profile: str, first_url: str,
                   wait: int = 30, headless: bool = False) -> bool:
    if _version(port):
        if _port_pages(port):
            log(f"复用已存在的实例 :{port}")
            return True
        zombies = _port_pids(port)
        log(f":{port} 上是僵尸实例，按 PID 清理: {zombies}")
        _kill_pids(zombies)
    os.makedirs(profile, exist_ok=True)
    args = [exe,
            f"--remote-debugging-port={port}",
            "--remote-allow-origins=*",
            f"--user-data-dir={profile}",
            "--no-first-run", "--no-default-browser-check",
            "--disable-popup-blocking", "--mute-audio"]
    if headless:
        args.append("--headless=new")
    args.append(first_url)
    log("启动实例（后台模式）" if headless else "启动实例（会弹窗口）")
    import subprocess
    subprocess.Popen(args, creationflags=0x00000008 | 0x00000200, close_fds=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for i in range(wait):
        time.sleep(1)
        if _version(port):
            log(f"端口就绪，用时 {i + 1}s")
            return True
    log("✗ 端口未就绪")
    return False


def pick_douyin_page(port: int) -> dict | None:
    import urllib.request
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5) as r:
        pages = [t for t in json.loads(r.read().decode()) if t.get("type") == "page"]
    for p in pages:
        if "douyin.com" in (p.get("url") or ""):
            return p
    return pages[0] if pages else None


def create_tab(port: int, url: str) -> dict | None:
    """CDP 强开新标签页。ego 新 profile 首启会停在 chrome://onboarding/ 欢迎页，
    不自动创建目标页面，只能这样绕过去。"""
    try:
        v = _version(port)
        if not v:
            return None
        import websocket
        w = websocket.create_connection(v["webSocketDebuggerUrl"], timeout=10)
        w.send(json.dumps({"id": 1, "method": "Target.createTarget",
                           "params": {"url": url}}))
        result = None
        deadline = time.time() + 10
        while time.time() < deadline:
            raw = w.recv()
            msg = json.loads(raw)
            if msg.get("id") == 1:
                result = msg.get("result")
                break
        w.close()
        return result
    except Exception:
        return None


def wait_douyin_page(port: int, url: str, tries: int = 15) -> dict | None:
    """轮询抖音标签页；等不到就强开一个再等。"""
    page = None
    for _ in range(tries):
        time.sleep(1)
        page = pick_douyin_page(port)
        if page and "douyin.com" in (page.get("url") or ""):
            return page
    if create_tab(port, url):
        for _ in range(10):
            time.sleep(1)
            page = pick_douyin_page(port)
            if page and "douyin.com" in (page.get("url") or ""):
                log("✓ 已通过 CDP 强开抖音标签页")
                return page
    return None


def _all_cookies(cdp: CDP) -> list[dict]:
    try:
        cdp.send("Network.enable")
        res = cdp.send("Network.getAllCookies")
        return res.get("cookies") or []
    except Exception:
        try:
            res = cdp.send("Network.getCookies")
            return res.get("cookies") or []
        except Exception:
            return []


def cmd_login(args) -> int:
    exe = config.get("browser_exe", None)
    if not exe or not os.path.isfile(exe):
        log("✗ 找不到浏览器，先跑 python scripts/config.py 确认 browser_exe")
        return 2
    os.makedirs(PROFILE, exist_ok=True)
    if not ensure_browser(exe, args.port, PROFILE, "https://www.douyin.com/",
                          wait=40, headless=False):
        return 3
    page = wait_douyin_page(args.port, "https://www.douyin.com/")
    if not page:
        log("✗ 没等到抖音标签页")
        shutdown(args.port)
        return 4
    log(f"附着标签页：{(page.get('url') or '')[:80]}")
    cdp = CDP(page["webSocketDebuggerUrl"], recv_timeout=30)
    ok = False
    try:
        cdp.send("Network.enable")
        log("等待扫码登录…（最多 180s，请用小号扫码）")
        for i in range(90):
            time.sleep(2)
            cookies = _all_cookies(cdp)
            names = {c["name"] for c in cookies
                     if "douyin.com" in (c.get("domain") or "")}
            if {"sessionid", "sessionid_ss"} & names:
                ok = True
                log(f"✓ 登录成功（第 {i * 2 + 2}s 检测到 sessionid）")
                break
    finally:
        cdp.close()
        shutdown(args.port)
    if ok:
        log(f"✓ 登录态已存入 {PROFILE}，之后 scan 不再需要扫码")
        return 0
    log("✗ 超时未登录")
    return 5


def _is_json_response(url: str, body: str) -> bool:
    if not body:
        return False
    s = body.lstrip()
    return s.startswith(("{", "[")) and len(body) > 10


def cmd_scan(args) -> int:
    exe = config.get("browser_exe", None)
    if not exe or not os.path.isfile(exe):
        log("✗ 找不到浏览器")
        return 2
    headless = not args.show and _truthy(config.get("headless", default=False))
    if not ensure_browser(exe, args.port, PROFILE, args.url,
                          wait=40, headless=headless):
        return 3
    page = wait_douyin_page(args.port, args.url)
    if not page:
        log("✗ 没等到抖音标签页")
        shutdown(args.port)
        return 4
    log(f"附着标签页：{(page.get('url') or '')[:80]}")
    cdp = CDP(page["webSocketDebuggerUrl"], recv_timeout=60)
    captures: list[dict] = []
    try:
        cdp.send("Network.enable")
        cdp.send("Page.enable")
        cdp.send("Runtime.enable")
        # 已开的实例可能停在别页，重新导航到目标页
        target = args.url
        if target not in (page.get("url") or ""):
            log(f"导航到 {target}")
            cdp.send("Page.navigate", {"url": target})
        else:
            log("刷新消息页")
            cdp.send("Page.reload")

        seen: set[str] = set()
        ws_frames: list[dict] = []
        click_log: list[str] = []
        start = time.time()

        def drain() -> None:
            """实时消化网络事件。响应体在 CDP 缓冲里存活很短，
            攒到最后再取会全部失效（实测 0 捕获），必须边走边收。"""
            for ev in list(cdp.events):
                method = ev.get("method")
                if method in ("Network.webSocketFrameReceived", "Network.webSocketFrameSent"):
                    p = ev["params"]
                    payload = (p.get("response") or {}).get("payloadData") or ""
                    if payload and len(ws_frames) < 500:
                        ws_frames.append({"dir": "recv" if method.endswith("Received") else "sent",
                                          "t": round(time.time() - start, 1),
                                          "len": len(payload), "payload": payload[:200000]})
                    continue
                if method != "Network.responseReceived":
                    continue
                p = ev["params"]
                resp = p.get("response") or {}
                url = resp.get("url") or ""
                low = url.lower()
                if "douyin.com" not in low:
                    continue
                if not any(k in low for k in KEYWORDS):
                    continue
                rid = p.get("requestId")
                if rid in seen:
                    continue
                seen.add(rid)
                body = ""
                try:
                    b = cdp.send("Network.getResponseBody", {"requestId": rid})
                    body = b.get("body") or ""
                    if b.get("base64Encoded"):
                        import base64
                        body = base64.b64decode(body).decode("utf-8", "replace")
                except Exception:
                    continue
                if not _is_json_response(url, body):
                    continue
                log(f"  ✓ {len(body):>6}B  {url[:110]}")
                captures.append({"url": url, "status": resp.get("status"),
                                 "len": len(body), "body": body[:300000]})

        def nap(sec: float) -> None:
            """带事件消化的 sleep。"""
            n = int(sec / 0.5)
            for _ in range(max(1, n)):
                drain()
                time.sleep(0.5)

        def js(expr: str):
            """执行 JS；出错时返回 None 并把异常写进 click_log，避免静默吞掉。"""
            try:
                r = cdp.send("Runtime.evaluate",
                             {"expression": expr, "returnByValue": True})
            except Exception as e:
                click_log.append(f"js-error: {type(e).__name__} {e}")
                return None
            if r.get("exceptionDetails"):
                det = r["exceptionDetails"].get("exception", {})
                click_log.append(f"js-exception: {str(det.get('description') or det)[:200]}")
                return None
            return (r.get("result") or {}).get("value")

        def click_xy(x: float, y: float, hover: float = 1.0) -> None:
            """真实输入序列：移入→悬停→按下→抬起。JS .click() 触发不了抖音的处理器。"""
            cdp.send("Input.dispatchMouseEvent",
                     {"type": "mouseMoved", "x": x - 30, "y": y - 20})
            time.sleep(0.3)
            cdp.send("Input.dispatchMouseEvent",
                     {"type": "mouseMoved", "x": x, "y": y})
            time.sleep(hover)
            cdp.send("Input.dispatchMouseEvent",
                     {"type": "mousePressed", "x": x, "y": y,
                      "button": "left", "clickCount": 1})
            time.sleep(0.12)
            cdp.send("Input.dispatchMouseEvent",
                     {"type": "mouseReleased", "x": x, "y": y,
                      "button": "left", "clickCount": 1})

        # 1. 点开 IM 面板（首次点击经常不生效，最多重试 4 次）
        rect = None
        for _ in range(20):  # reload 后等侧栏渲染出来
            rect = js("""
(function(){
  const spans = Array.from(document.querySelectorAll('span,div,p'))
    .filter(e => e.childElementCount === 0 && (e.textContent || '').trim() === '消息');
  if (!spans.length) return null;
  const el = (spans[0].closest('div') || spans[0]);
  const r = el.getBoundingClientRect();
  return {x: r.x + r.width / 2, y: r.y + r.height / 2};
})()
""")
            if rect:
                break
            nap(1)
        if not rect:
            log("✗ 20s 内没等到侧栏「消息」入口")
            shutdown(args.port)
            return 5
        opened = 0
        for attempt in range(4):
            click_xy(rect["x"], rect["y"])
            nap(4)
            opened = js("""
(function(){
  return Array.from(document.querySelectorAll('[data-e2e="conversation-item"]'))
    .filter(e => e.getBoundingClientRect().width > 50).length;
})()
""") or 0
            click_log.append(f"open-panel try{attempt + 1}: {opened} items")
            log(f"打开面板尝试 {attempt + 1}: 可见会话 {opened}")
            if opened:
                break
        if not opened:
            log("✗ IM 面板打不开（4 次尝试均失败）")
            shutdown(args.port)
            return 5

        # 2. 会话项坐标（带重试；面板偶发在查询间隙重渲染）
        items = None
        for _ in range(5):
            items = js("""
(function(){
  return Array.from(document.querySelectorAll('[data-e2e="conversation-item"]'))
    .filter(e => e.getBoundingClientRect().width > 50)
    .map(e => { const r = e.getBoundingClientRect();
      return {x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2),
              text: (e.innerText || '').slice(0, 60).replace(/\\n/g, ' ')}; });
})()
""")
            if items:
                break
            nap(2)
        items = items or []
        click_log.append(f"items: {json.dumps(items, ensure_ascii=False)[:1500]}")
        for it in items[:6]:
            log(f"  会话: {it['text'][:40]}")

        try:
            dom_text = js("""
(function(){
  const dlg = Array.from(document.querySelectorAll('[class*="im-saas-container"], [class*="im-dialog"], [class*="LeftPanel"]'))
    .find(e => e.getBoundingClientRect().width > 100);
  return (dlg || document.body).innerText;
})()
""") or ""
        except Exception:
            dom_text = ""
        log(f"面板 DOM {len(dom_text)} 字")

        # 3. 逐个点开会话，触发会话历史接口（每次点前重新取坐标）
        for i in range(min(args.convs, len(items) if items else 0)):
            fresh = js("""
(function(){
  const e = Array.from(document.querySelectorAll('[data-e2e="conversation-item"]'))
    .filter(e => e.getBoundingClientRect().width > 50)[%d];
  if (!e) return null;
  const r = e.getBoundingClientRect();
  return {x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2)};
})()
""" % i)
            pos = fresh or items[i]
            # 实测单击只聚焦，双击才打开聊天
            click_xy(pos["x"], pos["y"], hover=0.4)
            time.sleep(0.3)
            click_xy(pos["x"], pos["y"], hover=0.3)
            click_log.append(f"conv-{i}: double-clicked {items[i]['text'][:40]}")
            log(f"点开会话 {i + 1}: {items[i]['text'][:36]}")
            nap(6)

        # 收尾等待，把剩余推送也吃进来
        while time.time() - start < args.seconds:
            nap(1)

        # 全页文本兜底
        try:
            dom = cdp.send("Runtime.evaluate",
                           {"expression": "document.body.innerText",
                            "returnByValue": True})
            body_text = (dom.get("result") or {}).get("value") or ""
        except Exception:
            body_text = ""
        # 找「消息/私信」入口链接，确认真实跳转地址
        try:
            entries = cdp.send("Runtime.evaluate", {"expression": """
Array.from(document.querySelectorAll('a[href], [data-e2e], [class*="message"], [class*="im-"]'))
  .map(e => e.getAttribute('href') || e.getAttribute('data-e2e') || e.className)
  .filter(v => v && /message|im|私信|消息/.test(String(v)))
  .slice(0, 40)
""", "returnByValue": True})
            entry_hints = (entries.get("result") or {}).get("value") or []
        except Exception:
            entry_hints = []
        cur_url = ""
        try:
            cur_url = ((cdp.send("Runtime.evaluate",
                                 {"expression": "location.href",
                                  "returnByValue": True}).get("result") or {})
                       .get("value")) or ""
        except Exception:
            pass
    finally:
        cdp.close()
        shutdown(args.port)

    out = os.path.join(args.outdir, f"im-observe-{time.strftime('%H%M%S')}.json")
    os.makedirs(args.outdir, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"final_url": cur_url, "dom_text": dom_text[:20000],
                   "body_text": body_text[:20000],
                   "entry_hints": entry_hints, "captures": captures,
                   "ws_frames": ws_frames, "click_log": click_log},
                  f, ensure_ascii=False, indent=1)
    log(f"✓ 观察 {args.seconds}s：HTTP {len(captures)} 条，WS 帧 {len(ws_frames)} 帧，"
        f"DOM {len(dom_text)} 字")
    log(f"→ {out}")
    return 0 if (captures or ws_frames or dom_text) else 6


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("login", cmd_login), ("scan", cmd_scan)):
        sp = sub.add_parser(name)
        sp.add_argument("--port", type=int, default=DEFAULT_PORT)
        if name == "scan":
            sp.add_argument("--seconds", type=int, default=30,
                            help="观察时长秒数")
            sp.add_argument("--url", default=MESSAGE_URL,
                            help="要观察的页面 URL（默认消息页）")
            sp.add_argument("--show", action="store_true",
                            help="观察时也弹窗口（默认跟随配置 headless）")
            sp.add_argument("--outdir", default="douyin-im")
            sp.add_argument("--convs", type=int, default=3,
                            help="逐个点开前几个会话以触发历史接口")
        sp.set_defaults(fn=fn)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
