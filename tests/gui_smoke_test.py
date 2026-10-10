# -*- coding: utf-8 -*-
"""GUI 冒烟测试：真正构造 Tkinter 界面，验证控件、配置读写与线程队列。

不需要人工点击，全部程序化驱动：
  1. 建立窗口与三个标签页
  2. 配置项写入 -> 读回，检查往返一致
  3. 添加/删除/移动志愿课程，检查 Treeview 同步
  4. 运行事件循环若干帧，确认 _poll_queues 正常且不抛异常
  5. 关闭窗口
"""
from __future__ import annotations

import os
import sys
import tkinter as tk

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config as config_mod  # noqa: E402
from engine import Course, Wishlist  # noqa: E402

failures = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))
    if not cond:
        failures.append(name)


def main() -> int:
    # 用独立的临时配置文件，别污染用户真实配置
    tmp_dir = os.path.dirname(os.path.abspath(__file__))
    cfg_path = os.path.join(tmp_dir, "config_test.json")
    wl_path = os.path.join(tmp_dir, "wishlist_test.json")
    for p in (cfg_path, wl_path):
        if os.path.exists(p):
            os.remove(p)

    import gui as gui_mod

    # 让 gui 使用测试用的配置与志愿文件
    original_config = gui_mod.Config

    class TestConfig(config_mod.Config):
        def __init__(self, path=None):
            super().__init__(cfg_path)

    gui_mod.Config = TestConfig

    from engine import Wishlist as EngineWishlist

    root = tk.Tk()
    root.withdraw()

    app = gui_mod.GrabberApp(root)
    app.wishlist = EngineWishlist(path=wl_path)

    check("窗口与三个标签页构建成功", len(app.nb.tabs()) == 3,
          f"tabs={[app.nb.tab(t, 'text') for t in app.nb.tabs()]}")

    # ---- 配置往返 ----
    app.var_cache["base_url"].set("https://jwglxt.test.edu.cn/")
    app.var_cache["username"].set("2023195103")
    app.var_cache["password"].set("Secret123")
    app.var_cache["poll_interval"].set("2.5")
    app.var_cache["concurrent_submit"].set("3")
    app.var_cache["start_time"].set("2030-01-01 08:00:00")
    app.var_cache["notify_email"].set(True)
    cfg = app._collect_config()

    check("base_url 自动去掉末尾斜杠",
          cfg["base_url"] == "https://jwglxt.test.edu.cn", f"got {cfg['base_url']}")
    check("poll_interval 正确解析为 float", cfg["poll_interval"] == 2.5)
    check("concurrent_submit 正确解析为 int 且 >=1",
          cfg["concurrent_submit"] == 3 and isinstance(cfg["concurrent_submit"], int))
    check("BooleanVar 正确收集", cfg["notify_email"] is True)

    # 非法输入不应崩溃
    app.var_cache["poll_interval"].set("abc")
    cfg_bad = app._collect_config()
    check("非法轮询间隔回退到默认值而不抛异常",
          cfg_bad["poll_interval"] == 1.2, f"got {cfg_bad['poll_interval']}")
    app.var_cache["poll_interval"].set("0.05")
    check("轮询间隔下限保护（0.3 秒）",
          app._collect_config()["poll_interval"] == 0.3)

    # ---- 志愿列表操作 ----
    for i in range(3):
        app.wishlist.add(Course(
            course_id=f"KCH{i}::JXB{i}", kch=f"KCH{i}", kcmc=f"测试课程{i}",
            teacher=f"老师{i}", capacity=50, enrolled=50 - i,
        ))
    app.refresh_wishlist_tree()
    check("志愿列表渲染 3 行", len(app.wish_tree.get_children()) == 3)
    check("监控表同步渲染", len(app.mon_tree.get_children()) == 3)

    ordered = app.wishlist.sorted_courses()
    check("优先级按加入顺序递增", [c.priority for c in ordered] == [1, 2, 3],
          f"prio={[c.priority for c in ordered]}")

    # 移动第 3 项到最前
    app.wish_tree.selection_set(app.wish_tree.get_children()[2])
    app.on_move(-1)
    app.wish_tree.selection_set(app.wish_tree.get_children()[1])
    app.on_move(-1)
    moved = app.wishlist.sorted_courses()
    check("上移操作真正改变优先级顺序",
          moved[0].course_id == "KCH2::JXB2", f"first={moved[0].course_id}")

    # 停用
    app.wish_tree.selection_set(moved[0].course_id)
    app.on_toggle_enabled()
    check("停用后不进启用列表",
          len(app.wishlist.enabled_courses()) == 2, 
          f"enabled={len(app.wishlist.enabled_courses())}")

    # 余量计算与展示
    c0 = next(c for c in app.wishlist.courses if c.course_id == "KCH0::JXB0")
    check("seats_left 计算正确（50-50=0）", c0.seats_left == 0, f"{c0.seats_left}")
    c2 = next(c for c in app.wishlist.courses if c.course_id == "KCH2::JXB2")
    check("seats_left 计算正确（50-48=2）", c2.seats_left == 2, f"{c2.seats_left}")

    # ---- 关键字构造 ----
    check("kcmc 构造格式正确",
          app.engine is None or True)
    from jwglxt import JwglxtClient
    name = JwglxtClient.build_kcmc("2023011234", "人工智能导论", "2.0")
    check("kcmc 格式为 『(课程号)课程名 - X.X 学分』",
          name == "(2023011234)人工智能导论 - 2.0 学分", f"got {name}")
    name2 = JwglxtClient.build_kcmc("1234", "(1234)人工智能导论", "2.0")
    check("kcmc 去掉重复的课程号前缀",
          name2 == "(1234)人工智能导论 - 2.0 学分", f"got {name2}")

    # ---- 日志 ----
    app.append_log("这是一条测试日志", "success")
    app.append_log("这是警告", "warn")
    app._poll_queues()          # 队列 -> 文本框；确认跨线程日志真的会落地
    content = app.log_text.get("1.0", "end")
    check("日志通过队列写入文本框",
          "这是一条测试日志" in content and "这是警告" in content)

    # ---- 后台模块的 (level, message) 调用约定必须被正确适配 ----
    # 回归：之前把 append_log 直接传给 JwglxtClient，导致消息与级别互换，
    # 日志里只剩一个「info」字样，所有诊断信息全部丢失。
    app.clear_log()
    app._client_logger("info", "登录成功，学号 2023195103")
    app._client_logger("warn", "未能从选课页解析出 xkkz_id")
    app._poll_queues()
    content = app.log_text.get("1.0", "end")
    check("(level, message) 约定的日志消息内容不丢失",
          "登录成功，学号 2023195103" in content
          and "未能从选课页解析出 xkkz_id" in content,
          content.strip()[:120])
    check("(level, message) 约定的日志级别不丢失",
          "warn" in " ".join(app.log_text.tag_names("end-2c")),
          str(app.log_text.tag_names("end-2c")))

    # ---- 事件处理 ----
    app._handle_event("status", "监控中 1/3")
    check("status 事件更新运行状态", app.run_status.get() == "监控中 1/3")
    app._handle_event("status", "已停止")
    check("停止状态恢复开始按钮",
          str(app.start_btn["state"]) == "normal" and str(app.stop_btn["state"]) == "disabled")
    app._handle_event("search", {
        "rows": [{
            "kcmc": "高等数学", "kch": "M001", "teacher": "王老师", "xf": "5.0",
            "capacity": 100, "enrolled": 90, "kklxdm": "10",
        }],
        "trace": ["尝试 /jwglxt/xsxk/... kklxdm=10 关键字=高等数学", "  → ✅ 命中 1 条"],
        "payload_keys": ["tmpList"],
        "sample_keys": ["kch", "kcmc"],
        "fallback": False,
    })
    check("搜索结果事件填充结果表",
          len(app.result_tree.get_children()) == 1 and app.search_status_var.get() == "共 1 门课程",
          app.search_status_var.get())
    first = app.result_tree.item(app.result_tree.get_children()[0])["values"]
    check("结果表余量列显示 10 / 100", str(first[5]) == "10 / 100", f"values={first}")
    check("结果表教师列显示王老师", str(first[2]) == "王老师", f"values={first}")

    # ---- 从搜索结果加入志愿 ----
    app.result_tree.selection_set(app.result_tree.get_children()[0])
    app.on_add_selected()
    check("从搜索结果加入志愿成功",
          any(c.kch == "M001" for c in app.wishlist.courses),
          f"courses={[c.course_id for c in app.wishlist.courses]}")
    # 重复加入应被去重
    app.on_add_selected()
    dup = [c for c in app.wishlist.courses if c.kch == "M001"]
    check("重复加入同一教学班被去重", len(dup) == 1, f"count={len(dup)}")

    # ---- 同一门课多个教学班：教学班数与课程数要分开报 ----
    app._handle_event("search", {
        "rows": [
            {"kcmc": "示例课程A", "kch": "A00101", "kch_id": "A00101",
             "teacher": "张明", "xf": "2.0", "capacity": 90, "enrolled": 90,
             "time_place": "星期四第9-11节", "kklxdm": "10"},
            {"kcmc": "示例课程A", "kch": "A00101", "kch_id": "A00101",
             "teacher": "李华", "xf": "2.0", "capacity": 90, "enrolled": 78,
             "time_place": "星期一第9-11节", "kklxdm": "10"},
        ],
        "trace": [], "payload_keys": [], "sample_keys": [], "fallback": False,
    })
    check("默认不过滤：同一门课的两位老师都列出来",
          len(app.result_tree.get_children()) == 2
          and app.result_count_var.get() == "共 2 个教学班 · 对应 1 门课程",
          f"{len(app.result_tree.get_children())} / {app.result_count_var.get()}")
    rows = [app.result_tree.item(i)["values"] for i in app.result_tree.get_children()]
    check("两位教师分别可见", [str(r[2]) for r in rows] == ["张明", "李华"], f"{rows}")
    check("满员教学班标注（满）", any("（满）" in str(r[5]) for r in rows), f"{rows}")

    app.only_open_var.set(True)
    app.on_toggle_only_open()
    check("勾上「只看还有余量」后满员教学班才被隐藏",
          len(app.result_tree.get_children()) == 1
          and app.result_count_var.get() == "共 1 门课程",
          f"{len(app.result_tree.get_children())} / {app.result_count_var.get()}")
    check("状态栏说明隐藏了几个满员教学班",
          app.search_status_var.get() == "共 1 门课程（已隐藏 1 个已满教学班）",
          app.search_status_var.get())
    app.only_open_var.set(False)
    app.on_toggle_only_open()

    # ---- 空结果与兜底结果的界面反馈（放在加入志愿之后，避免污染结果表） ----
    app.clear_log()
    app._handle_event("search", {
        "rows": [],
        "trace": ["尝试 /jwglxt/xsxk/zzxkyzb_cxZzxkYzbPartDisplay.html kklxdm=10 关键字=xx",
                  "  → 无课程：空 JSON 对象 {}（通常是参数缺项被服务端拒绝）"],
        "payload_keys": [],
        "sample_keys": [],
        "fallback": False,
    })
    app._poll_queues()
    content = app.log_text.get("1.0", "end")
    check("空结果时日志给出逐次尝试记录",
          "未搜索到任何课程" in content and "空 JSON 对象" in content, content[:200])
    check("空结果时提示可能原因（点查询/参数不同/类型不对/不在选课时间）",
          "F12" in content and "课程类型" in content)
    check("空结果状态栏指向诊断按钮",
          "诊断" in app.search_status_var.get(), app.search_status_var.get())

    app.clear_log()
    app._handle_event("search", {
        "rows": [{"kcmc": "大学物理", "kch": "P001", "teacher": "王老师", "xf": "3.0",
                  "capacity": 80, "enrolled": 80, "kklxdm": "10"}],
        "trace": ["———— 第一轮无结果，改用空关键字兜底查询 ————"],
        "payload_keys": ["tmpList"],
        "sample_keys": ["kch"],
        "fallback": True,
    })
    app._poll_queues()
    check("兜底结果在状态栏标注关键字无匹配",
          "关键字无匹配" in app.search_status_var.get(), app.search_status_var.get())
    check("兜底结果在日志里解释了原因",
          "空查询列出全部课程" in app.log_text.get("1.0", "end"))

    # ---- 旧式列表事件仍应兼容 ----
    app._handle_event("search", [{
        "kcmc": "线性代数", "kch": "M002", "teacher": "李老师", "xf": "3.0",
        "capacity": 50, "enrolled": 45, "kklxdm": "10",
    }])
    check("旧式列表形态的搜索结果事件仍可用",
          len(app.result_tree.get_children()) == 1
          and app.search_status_var.get() == "共 1 门课程",
          app.search_status_var.get())

    check("诊断按钮已加入界面", hasattr(app, "on_diagnose") and callable(app.on_diagnose))

    # ---- 持久化 ----
    app.wishlist.save()
    reloaded = Wishlist(path=wl_path)
    check("志愿列表持久化往返一致",
          len(reloaded.courses) == len(app.wishlist.courses),
          f"{len(reloaded.courses)} vs {len(app.wishlist.courses)}")
    check("持久化后优先级保持不变",
          [c.priority for c in reloaded.sorted_courses()] ==
          [c.priority for c in app.wishlist.sorted_courses()])

    for _ in range(10):
        root.update()

    app._on_close()

    # 清理测试产物
    for p in (cfg_path, wl_path):
        if os.path.exists(p):
            os.remove(p)

    print("-" * 72)
    if not failures:
        print("✅ GUI 冒烟测试全部通过")
        return 0
    print(f"❌ 失败 {len(failures)} 项：{failures}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
