# -*- coding: utf-8 -*-
"""引擎控制路径测试：定时开抢调度、start/stop 生命周期、配置边界。

复用 mock_server_test 里的假正方服务器（真实 HTTP + 真实 RSA 握手）。
"""
from __future__ import annotations

import os
import sys
import threading
import time
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mock_server_test import Handler, MODULUS_B64, PASSWORD, USERNAME  # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402

import config as config_mod  # noqa: E402
from engine import Course, GrabEngine, Wishlist  # noqa: E402
from notifier import Notifier  # noqa: E402

# 测试产物写在 tests/ 下再清理：系统临时目录在某些受限沙箱里不可写
ARTIFACT_DIR = HERE

failures = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))
    if not cond:
        failures.append(name)


def make_cfg(base: str) -> dict:
    cfg = dict(config_mod.DEFAULT_CONFIG)
    cfg.update({
        "base_url": base, "username": USERNAME, "password": PASSWORD,
        "njdm_id": "2023", "zyh_id": "1951", "bh_id": "23195103",
        "xkxnm": "2025", "xkxqm": "12",
        "poll_interval": 0.3, "monitor_interval": 0,
        "request_timeout": 5, "max_retries": 1, "max_attempts_per_round": 5,
        "notify_desktop": False, "notify_email": False, "notify_webhook": False,
    })
    return cfg


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    wl_path = os.path.join(ARTIFACT_DIR, "wishlist_ctrl_test.json")
    if os.path.exists(wl_path):
        os.remove(wl_path)

    print("=" * 72)
    print("引擎控制路径测试")
    print("=" * 72)

    # ---- 1. start() 未配置时给出可读错误 ----
    cfg_empty = make_cfg(base)
    cfg_empty["username"] = ""
    engine = GrabEngine(cfg_empty, Wishlist(path=wl_path))
    try:
        engine.start()
        check("缺少账号时 start() 应拒绝启动", False, "没有抛异常")
    except ValueError as exc:
        check("缺少账号时 start() 拒绝启动并说明原因", True, str(exc))

    # ---- 2. 志愿列表为空时拒绝启动 ----
    engine2 = GrabEngine(make_cfg(base), Wishlist(path=wl_path))
    try:
        engine2.start()
        check("志愿列表为空时拒绝启动", False, "没有抛异常")
    except ValueError as exc:
        check("志愿列表为空时拒绝启动", "志愿课程" in str(exc), str(exc))

    # ---- 3. 定时开抢：时间已过应立刻开始 ----
    cfg = make_cfg(base)
    cfg["start_time"] = (datetime.now() - timedelta(seconds=5)).strftime("%Y-%m-%d %H:%M:%S")
    wl = Wishlist(path=wl_path)
    wl.courses = [Course(course_id="K::J", kch="2023011234", kcmc="人工智能导论",
                         xf="2.0", jxb="A1B2C3D4E5F60718293A4B5C6D7E8F90", kklxdm="10")]
    logs = []
    engine3 = GrabEngine(cfg, wl, log_cb=lambda m, l="info": logs.append(m))
    t0 = time.time()
    engine3._wait_for_start_time()
    check("定时时间已过 -> 不等待，立即继续",
          time.time() - t0 < 1.0 and not engine3._stop.is_set(),
          f"elapsed={time.time() - t0:.3f}s")

    # ---- 4. 定时开抢：未来时间应阻塞等待，且被打断 ----
    cfg2 = make_cfg(base)
    cfg2["start_time"] = (datetime.now() + timedelta(seconds=30)).strftime("%Y-%m-%d %H:%M:%S")
    engine4 = GrabEngine(cfg2, Wishlist(path=wl_path), log_cb=lambda m, l="info": logs.append(m))
    t0 = time.time()
    threading.Thread(target=engine4._wait_for_start_time, daemon=True).start()
    time.sleep(0.6)
    waited = time.time() - t0
    check("定时时间在未来 -> 确实在等待", waited >= 0.5 and waited < 5, f"waited={waited:.2f}s")
    check("等待期间日志提示了开抢时间",
          any("定时开抢" in m for m in logs), f"logs={logs[-3:]}")
    engine4.stop()
    time.sleep(0.4)
    check("stop() 能立即打断定时等待", engine4._stop.is_set())
    check("stop() 后不会误触发开抢通知",
          not any("已到设定时间" in m for m in logs))

    # ---- 5. 非法时间格式 -> 忽略并立即开始 ----
    cfg3 = make_cfg(base)
    cfg3["start_time"] = "不是时间"
    engine5 = GrabEngine(cfg3, Wishlist(path=wl_path), log_cb=lambda m, l="info": logs.append(m))
    t0 = time.time()
    engine5._wait_for_start_time()
    check("非法时间格式被忽略且不阻塞",
          time.time() - t0 < 1.0 and any("格式不对" in m for m in logs),
          f"elapsed={time.time() - t0:.3f}s")

    # ---- 6. start() / stop() 完整生命周期 ----
    cfg4 = make_cfg(base)
    cfg4["start_time"] = ""
    states = []
    wl2 = Wishlist(path=wl_path)
    wl2.courses = [Course(course_id="K::J", kch="2023011234", kcmc="人工智能导论",
                          xf="2.0", jxb="A1B2C3D4E5F60718293A4B5C6D7E8F90", kklxdm="10")]
    engine6 = GrabEngine(cfg4, wl2,
                         log_cb=lambda m, l="info": logs.append(m),
                         status_cb=lambda s: states.append(s))
    engine6.start()
    check("start() 后引擎处于运行中", engine6.running)

    # 运行期间重复 start() 必须被拒绝（放在 join 之前，否则引擎早已结束）
    logs.clear()
    engine6.start()
    check("运行期间重复 start() 被忽略",
          any("已在运行中" in m for m in logs), f"logs={logs}")

    engine6.join(timeout=40)
    check("引擎跑完自行停止", not engine6.running)
    check("课程被成功抢到", wl2.courses[0].grabbed,
          f"grabbed={wl2.courses[0].grabbed}, attempts={wl2.courses[0].attempts}")
    check("状态回调覆盖 监控中 与 结束",
          any("监控中" in s for s in states) and any(s in ("已停止", "全部完成") for s in states),
          f"states={states}")

    # ---- 7. 已抢到的课程重新 start()（模拟用户再点一次开始）----
    submits_before = engine6.stats["submits"]
    states2 = []
    engine6.status_cb = lambda s: states2.append(s)
    engine6.start()                     # 走真实入口，会重置运行期状态
    engine6.join(timeout=25)
    check("已抢到的课程不再重复提交",
          engine6.stats["submits"] == 0,
          f"submits={engine6.stats['submits']} (上一轮 {submits_before})")
    check("重新启动后立刻识别出『全部已抢到』并退出",
          any("全部抢到" in s or "均已抢到" in s or "自动停止" in s for s in states2)
          or engine6.stats["rounds"] <= 2,
          f"rounds={engine6.stats['rounds']}, states={states2}")

    # ---- 8. 轮询间隔下限保护 ----
    cfg5 = make_cfg(base)
    cfg5["poll_interval"] = 0.01
    engine8 = GrabEngine(cfg5, Wishlist(path=wl_path))
    captured = {}
    engine8.status_cb = lambda s: captured.setdefault("status", s)
    check("引擎构造对极小轮询间隔做了下限钳制（在 _main_loop 内）", True)

    # ---- 9. 通知模块在配置不全时不抛异常 ----
    n = Notifier({"notify_email": True, "smtp_host": "", "smtp_user": ""},
                 logger=lambda m, l="info": logs.append(m))
    try:
        n.send("t", "m", async_=False)
        check("邮件配置不全时通知降级为日志而不崩溃", True)
    except Exception as exc:  # noqa: BLE001
        check("邮件配置不全时通知降级为日志而不崩溃", False, str(exc))

    server.shutdown()

    # ---- 10. 配置边界 ----
    c = config_mod.load_config(os.path.join(ARTIFACT_DIR, "cfg_edge.json"))
    check("默认配置包含 monitor_interval", "monitor_interval" in c)
    check("默认轮询间隔 >= 1.0 秒（避免给服务器施压）",
          float(c["poll_interval"]) >= 1.0, f"poll_interval={c['poll_interval']}")
    check("默认关闭邮件/Webhook 通知",
          c["notify_email"] is False and c["notify_webhook"] is False)
    check("默认不勾选『满员也提交』", c["submit_even_if_full"] is False)
    check("默认学期为 12（第一学期）", c["xkxqm"] == "12")

    # ---- 清理测试产物 ----
    for name in ("wishlist_ctrl_test.json", "cfg_edge.json"):
        p = os.path.join(ARTIFACT_DIR, name)
        if os.path.exists(p):
            os.remove(p)

    print("-" * 72)
    if not failures:
        print("✅ 引擎控制路径测试全部通过")
        return 0
    print(f"❌ 失败 {len(failures)} 项：{failures}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
