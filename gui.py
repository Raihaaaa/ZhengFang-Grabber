# -*- coding: utf-8 -*-
"""正方教务抢课助手 —— 图形界面（Tkinter，零额外 UI 依赖）。

三个标签页：
  系统配置：账号、教务地址、选课参数（支持一键自动探测）
  课程选择：搜索课程 / 手动添加，维护志愿列表与优先级
  抢课监控：启停、实时余量与结果、日志输出

线程模型：所有网络请求都在子线程执行，通过 queue 把日志/结果回传主线程，
避免 Tkinter 跨线程更新控件。
"""
from __future__ import annotations

import os
import queue
import re
import sys
import threading
import tkinter as tk
import traceback
from datetime import datetime, timedelta
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Any, Dict, List, Optional

from config import Config, app_dir
from engine import Course, GrabEngine, Wishlist
from jwglxt import JwglxtClient, JwglxtError, LoginError
from notifier import Notifier

APP_TITLE = "正方教务抢课助手 v1.2"
POLL_MS = 150
VERSION_LABEL = "v1.2"

# ------------------------------------------------------------------ #
# 配色：现代浅色 · 卡片式
# ------------------------------------------------------------------ #
FONT = "Microsoft YaHei UI"
MONO = "Consolas"
FS = 11        # 基准字号（v1.2 整体放大，原为 9）
FS_SM = 10     # 次要说明文字
C_WINDOW = "#eef2f9"    # 窗口底色（浅灰蓝）
C_CARD = "#ffffff"      # 卡片底色
C_LINE = "#dbe3ef"      # 卡片描边
C_TEXT = "#1f2937"
C_MUTED = "#6b7280"
C_PRIMARY = "#2563eb"
C_PRIMARY_D = "#1d4ed8"
C_PRIMARY_L = "#dbeafe"
C_OK = "#059669"
C_WARN = "#d97706"
C_ERR = "#dc2626"
C_HEAD = "#1e293b"      # 顶部标题条

LEVEL_TAGS = {
    "info": "info",
    "success": "success",
    "warn": "warn",
    "error": "error",
    "debug": "debug",
}


class GrabberApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.cfg = Config()
        self.wishlist = Wishlist()
        self.log_queue: "queue.Queue[tuple]" = queue.Queue()
        self.event_queue: "queue.Queue[tuple]" = queue.Queue()
        self.engine: Optional[GrabEngine] = None
        self._enriching = False
        self._closing = False

        self.status_var = tk.StringVar(value="就绪")
        self.search_status_var = tk.StringVar(value="")
        self.var_cache: Dict[str, tk.Variable] = {}

        root.title(APP_TITLE)
        root.geometry("1320x900")
        root.minsize(1120, 760)
        root.configure(bg=C_WINDOW)

        self._build_style()
        self._build_header()
        self._build_statusbar()
        self._build_notebook()

        self.refresh_wishlist_tree()
        self._poll_queues()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------ #
    # 样式与框架
    # ------------------------------------------------------------------ #
    def _build_style(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("clam")   # clam 才认 bordercolor / borderwidth 这些配置
        except tk.TclError:
            pass

        # --- 基础容器与文字 ---
        # 约定：TFrame/TLabel 的底色是窗口色；卡片内部一律用 Card.* 样式，
        #       这样不会在白色卡片上出现一块块灰底。
        style.configure("TFrame", background=C_WINDOW)
        style.configure("Card.TFrame", background=C_CARD)
        style.configure("TLabel", background=C_CARD, foreground=C_TEXT, font=(FONT, FS))
        style.configure("Window.TLabel", background=C_WINDOW, foreground=C_TEXT, font=(FONT, FS))
        style.configure("Title.TLabel", background=C_WINDOW, foreground=C_TEXT,
                        font=(FONT, 18, "bold"))
        style.configure("Hint.TLabel", background=C_WINDOW, foreground=C_MUTED, font=(FONT, FS_SM))
        style.configure("CardHint.TLabel", background=C_CARD, foreground=C_MUTED, font=(FONT, FS_SM))
        style.configure("CardStrong.TLabel", background=C_CARD, foreground=C_TEXT,
                        font=(FONT, 12, "bold"))

        # --- 卡片（白色 + 细描边，模拟柔和阴影卡片） ---
        style.configure("Card.TLabelframe", background=C_CARD, bordercolor=C_LINE,
                        borderwidth=1, relief="solid")
        style.configure("Card.TLabelframe.Label", background=C_CARD, foreground=C_PRIMARY,
                        font=(FONT, 12, "bold"))

        # --- 按钮 ---
        style.configure("Primary.TButton", font=(FONT, 12, "bold"), padding=(22, 11),
                        background=C_PRIMARY, foreground="#ffffff",
                        borderwidth=0, focuscolor=C_PRIMARY)
        style.map("Primary.TButton",
                  background=[("disabled", "#a9c0ee"), ("pressed", C_PRIMARY_D),
                              ("active", C_PRIMARY_D)],
                  foreground=[("disabled", "#f2f6ff")])
        style.configure("Secondary.TButton", font=(FONT, FS), padding=(16, 9),
                        background=C_CARD, foreground=C_TEXT, borderwidth=1,
                        bordercolor=C_LINE, lightcolor=C_CARD, darkcolor=C_CARD,
                        focuscolor=C_CARD)
        style.map("Secondary.TButton",
                  background=[("active", "#eef4ff"), ("pressed", C_PRIMARY_L)],
                  bordercolor=[("active", C_PRIMARY)],
                  foreground=[("active", C_PRIMARY_D), ("disabled", "#9aa4b2")])
        style.configure("Danger.TButton", font=(FONT, FS), padding=(16, 9),
                        background=C_CARD, foreground=C_ERR, borderwidth=1,
                        bordercolor="#f3c2c2", lightcolor=C_CARD, darkcolor=C_CARD,
                        focuscolor=C_CARD)
        style.map("Danger.TButton",
                  background=[("active", "#fdecec")], bordercolor=[("active", C_ERR)])

        # --- 输入控件 ---
        style.configure("TEntry", fieldbackground=C_CARD, bordercolor=C_LINE,
                        lightcolor=C_LINE, darkcolor=C_LINE, insertcolor=C_TEXT,
                        padding=7, foreground=C_TEXT)
        style.map("TEntry", bordercolor=[("focus", C_PRIMARY)],
                  lightcolor=[("focus", C_PRIMARY)], darkcolor=[("focus", C_PRIMARY)])
        style.configure("TCombobox", fieldbackground=C_CARD, background=C_CARD,
                        bordercolor=C_LINE, arrowcolor=C_MUTED, padding=6)
        style.map("TCombobox", fieldbackground=[("readonly", C_CARD)],
                  bordercolor=[("focus", C_PRIMARY)])
        style.configure("TCheckbutton", background=C_CARD, foreground=C_TEXT,
                        font=(FONT, FS), focuscolor=C_CARD)
        style.map("TCheckbutton", background=[("active", C_CARD)],
                  foreground=[("disabled", "#9aa4b2")])

        # --- 表格 ---
        style.configure("Treeview", rowheight=34, font=(FONT, FS),
                        background=C_CARD, fieldbackground=C_CARD, foreground=C_TEXT,
                        borderwidth=0)
        style.configure("Treeview.Heading", font=(FONT, FS, "bold"), background="#f2f5fa",
                        foreground="#334155", relief="flat", padding=(8, 9),
                        borderwidth=0)
        style.map("Treeview.Heading", background=[("active", "#e6ecf7")])
        style.map("Treeview", background=[("selected", C_PRIMARY_L)],
                  foreground=[("selected", C_PRIMARY_D)])
        # 去掉 Treeview 外面那圈立体边框，看起来才像卡片里的表格
        style.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])

        # --- 标签页 ---
        style.configure("TNotebook", background=C_WINDOW, borderwidth=0,
                        tabmargins=(4, 6, 4, 0))
        style.configure("TNotebook.Tab", padding=(26, 12), font=(FONT, 12),
                        background="#dde5f2", foreground=C_MUTED, borderwidth=0)
        style.map("TNotebook.Tab",
                  background=[("selected", C_CARD)],
                  foreground=[("selected", C_PRIMARY)])

        style.configure("TSeparator", background=C_LINE)
        style.configure("TProgressbar", background=C_PRIMARY, troughcolor="#e2e8f0",
                        borderwidth=0, thickness=8)

    # ------------------------------------------------------------------ #
    # 页面框架：顶部标题条 / 内容区 / 底部状态条
    # ------------------------------------------------------------------ #
    def _build_header(self) -> None:
        bar = tk.Frame(self.root, bg=C_HEAD, height=76)
        bar.pack(fill="x")
        bar.pack_propagate(False)

        left = tk.Frame(bar, bg=C_HEAD)
        left.pack(side="left", padx=(20, 0), pady=12)
        tk.Label(left, text="🎯  正方教务抢课助手", bg=C_HEAD, fg="#ffffff",
                 font=(FONT, 17, "bold")).pack(anchor="w")
        tk.Label(left, text="定时开抢 · 余量监控 · 抢到即通知 · 多志愿批量",
                 bg=C_HEAD, fg="#94a3b8", font=(FONT, FS_SM)).pack(anchor="w")

        tk.Label(bar, text=VERSION_LABEL, bg=C_HEAD, fg="#94a3b8",
                 font=(FONT, FS)).pack(side="right", padx=(0, 20))
        self.header_status = tk.Label(bar, text="● 未连接", bg=C_HEAD, fg="#f59e0b",
                                      font=(FONT, FS, "bold"))
        self.header_status.pack(side="right", padx=(0, 16))

    def _build_statusbar(self) -> None:
        bar = tk.Frame(self.root, bg=C_CARD, height=36)
        bar.pack(fill="x", side="bottom")
        bar.pack_propagate(False)
        tk.Frame(self.root, bg=C_LINE, height=1).pack(fill="x", side="bottom")
        tk.Label(bar, text="状态", bg=C_CARD, fg=C_MUTED, font=(FONT, FS)).pack(
            side="left", padx=(20, 8))
        self.status_label = tk.Label(bar, textvariable=self.status_var, bg=C_CARD,
                                     fg=C_OK, font=(FONT, FS, "bold"))
        self.status_label.pack(side="left")
        tk.Label(bar, text=f"配置目录：{app_dir()}", bg=C_CARD, fg=C_MUTED,
                 font=(FONT, FS_SM)).pack(side="right", padx=(0, 20))

    def _build_notebook(self) -> None:
        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill="both", expand=True, padx=16, pady=(12, 10))
        self.tab_cfg = ttk.Frame(self.nb)
        self.tab_course = ttk.Frame(self.nb, padding=16)
        self.tab_run = ttk.Frame(self.nb, padding=16)
        self.nb.add(self.tab_cfg, text="①  系统配置")
        self.nb.add(self.tab_course, text="②  课程选择")
        self.nb.add(self.tab_run, text="③  抢课监控")
        # 配置页内容比窗口高，套一层滚动区，小屏幕上也能滚到最下面的「结果通知」
        self._cfg_canvas, self.cfg_body = self._make_scrollable(self.tab_cfg)
        self._build_tab_config()
        self._build_tab_course()
        self._build_tab_run()

    def _make_scrollable(self, page):
        """给标签页加垂直滚动（内容高度超过窗口时仍能全部够到）。"""
        canvas = tk.Canvas(page, bg=C_WINDOW, highlightthickness=0, bd=0)
        vsb = ttk.Scrollbar(page, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas, padding=16)
        window = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=vsb.set)
        canvas.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        inner.bind("<Configure>",
                   lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfigure(window, width=e.width))

        def _wheel(event):
            # 只有指针确实停在配置页里才滚它，免得抢了日志框/表格的滚轮
            w = canvas.winfo_containing(event.x_root, event.y_root)
            while w is not None:
                if w is canvas or w is inner:
                    break
                w = getattr(w, "master", None)
            else:
                return
            canvas.yview_scroll(-1 if getattr(event, "delta", 0) > 0 else 1, "units")

        canvas.bind_all("<MouseWheel>", _wheel)
        return canvas, inner

    # ------------------------------------------------------------------ #
    # 标签页 1：系统配置
    # ------------------------------------------------------------------ #
    def _card(self, parent, title: str, padding: int = 14):
        """统一的白色卡片容器（带标题）。"""
        card = ttk.LabelFrame(parent, text=f"  {title}  ", style="Card.TLabelframe",
                              padding=padding)
        return card

    def _entry_row(self, parent, row: int, label: str, key: str,
                   width: int = 46, show: Optional[str] = None,
                   hint: str = "") -> ttk.Entry:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=4, padx=(0, 8))
        var = tk.StringVar(value=str(self.cfg.get(key, "")))
        self.var_cache[key] = var
        entry = ttk.Entry(parent, textvariable=var, width=width, show=show)
        entry.grid(row=row, column=1, sticky="w", pady=4)
        if hint:
            ttk.Label(parent, text=hint, style="CardHint.TLabel").grid(
                row=row, column=2, sticky="w", padx=(10, 0)
            )
        return entry

    def _build_tab_config(self) -> None:
        f = getattr(self, "cfg_body", self.tab_cfg)

        # --- 账号 ---
        acc = self._card(f, "账号与系统地址")
        acc.pack(fill="x")
        self._entry_row(acc, 0, "教务系统地址", "base_url", width=52,
                        hint="例如 https://jwglxt.xxx.edu.cn（末尾不要 /）")
        self._entry_row(acc, 1, "学号", "username")
        self._entry_row(acc, 2, "密码", "password", show="*")

        btns = ttk.Frame(acc, style="Card.TFrame")
        btns.grid(row=3, column=1, sticky="w", pady=(10, 0))
        ttk.Button(btns, text="测试登录", style="Secondary.TButton",
                   command=self.on_test_login).pack(side="left")
        ttk.Button(btns, text="一键探测选课参数", style="Secondary.TButton",
                   command=self.on_autodetect).pack(side="left", padx=8)
        ttk.Button(btns, text="检查选课阶段", style="Secondary.TButton",
                   command=self.on_check_stage).pack(side="left")
        ttk.Button(btns, text="保存配置", style="Primary.TButton",
                   command=self.on_save_config).pack(side="left", padx=(10, 0))

        # --- 选课参数 ---
        par = self._card(f, "选课参数（多数可留空，由「一键探测」填充）")
        par.pack(fill="x", pady=(12, 0))
        left = ttk.Frame(par, style="Card.TFrame")
        left.pack(side="left", fill="both", expand=True)
        right = ttk.Frame(par, style="Card.TFrame")
        right.pack(side="left", fill="both", expand=True, padx=(20, 0))

        specs_left = [
            ("学年 xkxnm", "xkxnm"),
            ("学期 xkxqm", "xkxqm"),
            ("年级 njdm_id", "njdm_id"),
            ("专业 zyh_id", "zyh_id"),
            ("班级 bh_id", "bh_id"),
            ("校区 xqh_id", "xqh_id"),
        ]
        specs_right = [
            ("学院 jg_id", "jg_id"),
            ("课程类型 kklxdm", "kklxdm"),
            ("选课轮次 xklc", "xklc"),
            ("菜单号 gnmkdm", "gnmkdm"),
            ("xkkz_id（可粘贴）", "xkkz_id_10"),
        ]
        for i, (label, key) in enumerate(specs_left):
            var = tk.StringVar(value=str(self.cfg.get(key, "")))
            self.var_cache[key] = var
            ttk.Label(left, text=label).grid(row=i, column=0, sticky="w", pady=3, padx=(0, 8))
            ttk.Entry(left, textvariable=var, width=20).grid(row=i, column=1, sticky="w", pady=3)
        for i, (label, key) in enumerate(specs_right):
            var = tk.StringVar(value=str(self.cfg.get(key, "")))
            self.var_cache[key] = var
            ttk.Label(right, text=label).grid(row=i, column=0, sticky="w", pady=3, padx=(0, 8))
            ttk.Entry(right, textvariable=var, width=20).grid(row=i, column=1, sticky="w", pady=3)

        ttk.Label(
            par,
            text="提示：学期 12=第一学期，3=第二学期（5~8 月）。若「一键探测」失败，可先手动填这几项。",
            style="CardHint.TLabel",
        ).pack(side="bottom", anchor="w", pady=(10, 0))
        ttk.Button(
            par, text="从浏览器复制的链接/表单里提取 xkkz_id", style="Secondary.TButton",
            command=self.on_extract_xkkz,
        ).pack(side="bottom", anchor="w", pady=(6, 0))

        # --- 抢课行为 ---
        beh = self._card(f, "抢课策略")
        beh.pack(fill="x", pady=(12, 0))
        row = ttk.Frame(beh, style="Card.TFrame")
        row.pack(fill="x")

        self.var_cache["poll_interval"] = tk.StringVar(value=str(self.cfg.get("poll_interval")))
        self.var_cache["monitor_interval"] = tk.StringVar(
            value=str(self.cfg.get("monitor_interval") or 0)
        )
        self.var_cache["concurrent_submit"] = tk.StringVar(
            value=str(self.cfg.get("concurrent_submit") or 1)
        )
        self.var_cache["start_time"] = tk.StringVar(value=str(self.cfg.get("start_time") or ""))

        ttk.Label(row, text="轮询间隔(秒)").grid(row=0, column=0, sticky="w")
        ttk.Entry(row, textvariable=self.var_cache["poll_interval"], width=8).grid(row=0, column=1, padx=(6, 18))
        ttk.Label(row, text="单课刷新间隔(秒,0=每轮)").grid(row=0, column=2, sticky="w")
        ttk.Entry(row, textvariable=self.var_cache["monitor_interval"], width=8).grid(row=0, column=3, padx=(6, 18))
        ttk.Label(row, text="并发提交数").grid(row=0, column=4, sticky="w")
        ttk.Entry(row, textvariable=self.var_cache["concurrent_submit"], width=8).grid(row=0, column=5, padx=(6, 0))

        row2 = ttk.Frame(beh, style="Card.TFrame")
        row2.pack(fill="x", pady=(10, 0))
        ttk.Label(row2, text="定时开抢").grid(row=0, column=0, sticky="w")
        ttk.Entry(row2, textvariable=self.var_cache["start_time"], width=24).grid(row=0, column=1, padx=(6, 6))
        ttk.Label(row2, text="格式 YYYY-MM-DD HH:MM:SS，留空=立即开始", style="CardHint.TLabel").grid(
            row=0, column=2, sticky="w"
        )
        ttk.Button(row2, text="+1分钟", style="Secondary.TButton",
                   command=lambda: self._quick_time(60)).grid(row=0, column=3, padx=8)
        ttk.Button(row2, text="清空", style="Secondary.TButton",
                   command=lambda: self.var_cache["start_time"].set("")).grid(row=0, column=4)

        self.var_cache["stop_when_all_done"] = tk.BooleanVar(
            value=bool(self.cfg.get("stop_when_all_done", True))
        )
        self.var_cache["submit_even_if_full"] = tk.BooleanVar(
            value=bool(self.cfg.get("submit_even_if_full", False))
        )
        row3 = ttk.Frame(beh, style="Card.TFrame")
        row3.pack(fill="x", pady=(10, 0))
        ttk.Checkbutton(row3, text="全部抢到后自动停止",
                        variable=self.var_cache["stop_when_all_done"]).pack(side="left")
        ttk.Checkbutton(row3, text="余量为 0 时也尝试提交（部分学校支持排队）",
                        variable=self.var_cache["submit_even_if_full"]).pack(side="left", padx=(16, 0))

        # --- 通知 ---
        ntf = self._card(f, "结果通知")
        ntf.pack(fill="x", pady=(12, 0))
        self.var_cache["notify_desktop"] = tk.BooleanVar(value=bool(self.cfg.get("notify_desktop", True)))
        self.var_cache["notify_email"] = tk.BooleanVar(value=bool(self.cfg.get("notify_email")))
        self.var_cache["notify_webhook"] = tk.BooleanVar(value=bool(self.cfg.get("notify_webhook")))
        ttk.Checkbutton(ntf, text="桌面弹窗", variable=self.var_cache["notify_desktop"]).grid(
            row=0, column=0, sticky="w"
        )
        ttk.Checkbutton(ntf, text="邮件通知", variable=self.var_cache["notify_email"]).grid(
            row=0, column=1, sticky="w", padx=(20, 0)
        )
        ttk.Checkbutton(ntf, text="Webhook（Server酱/PushPlus/企业微信）",
                        variable=self.var_cache["notify_webhook"]).grid(row=0, column=2, sticky="w", padx=(20, 0))

        for i, (label, key, show) in enumerate([
            ("SMTP 服务器", "smtp_host", None),
            ("SMTP 端口", "smtp_port", None),
            ("发件邮箱", "smtp_user", None),
            ("邮箱授权码", "smtp_pass", "*"),
            ("收件邮箱", "mail_to", None),
            ("Webhook 地址", "webhook_url", None),
        ]):
            ttk.Label(ntf, text=label).grid(row=1 + i // 3, column=(i % 3) * 2, sticky="w", pady=3)
            var = tk.StringVar(value=str(self.cfg.get(key, "")))
            self.var_cache[key] = var
            ttk.Entry(ntf, textvariable=var, width=22, show=show).grid(
                row=1 + i // 3, column=(i % 3) * 2 + 1, sticky="w", pady=3, padx=(6, 16)
            )

        ttk.Button(ntf, text="发送测试通知", style="Secondary.TButton",
                   command=self.on_test_notify).grid(row=4, column=0, sticky="w", pady=(10, 0))

    def _quick_time(self, seconds: int) -> None:
        target = datetime.now() + timedelta(seconds=seconds)
        self.var_cache["start_time"].set(target.strftime("%Y-%m-%d %H:%M:%S"))

    # ------------------------------------------------------------------ #
    # 标签页 2：课程选择
    # ------------------------------------------------------------------ #
    def _build_tab_course(self) -> None:
        f = self.tab_course

        box = self._card(f, "搜索课程", padding=12)
        box.pack(side="top", fill="x")
        line = ttk.Frame(box, style="Card.TFrame")
        line.pack(fill="x")
        ttk.Label(line, text="关键字").pack(side="left")
        self.search_var = tk.StringVar()
        ttk.Entry(line, textvariable=self.search_var, width=26).pack(side="left", padx=6)
        ttk.Label(line, text="课程类型").pack(side="left", padx=(8, 0))
        self.search_type_var = tk.StringVar(value="自动")
        ttk.Combobox(
            line, textvariable=self.search_type_var, width=8, state="readonly",
            values=["自动", "10", "16", "11", "12", "13"],
        ).pack(side="left", padx=6)
        # 默认**不过滤**：同一门课的多个教学班（不同老师/不同时间）必须都看得见，
        # 否则用户会以为「怎么只列了一位老师」。满员的行会带「（满）」标记。
        self.only_open_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            line, text="只看还有余量", variable=self.only_open_var,
            command=self.on_toggle_only_open,
        ).pack(side="left", padx=(6, 0))
        self.search_btn = ttk.Button(line, text="🔍  搜索", style="Primary.TButton",
                                     command=self.on_search)
        self.search_btn.pack(side="left", padx=(8, 0))
        ttk.Button(line, text="手动添加", style="Secondary.TButton",
                   command=self.on_manual_add).pack(side="left", padx=(8, 0))
        ttk.Button(line, text="诊断", style="Secondary.TButton",
                   command=self.on_diagnose).pack(side="left", padx=6)

        hint = ttk.Frame(box, style="Card.TFrame")
        hint.pack(fill="x", pady=(8, 0))
        ttk.Label(
            hint,
            text="同一门课若有多位老师 / 多个时间段，会分别列出（课程号相同），"
                 "可以只把想要的那个教学班加入志愿。",
            style="CardHint.TLabel",
        ).pack(side="left")
        ttk.Label(hint, textvariable=self.search_status_var,
                  style="CardHint.TLabel").pack(side="right")

        # 结果表与志愿列表用可拖动分隔条的上下分栏，两边都能拿到足够高度
        paned = ttk.Panedwindow(f, orient="vertical")
        paned.pack(side="top", fill="both", expand=True, pady=(12, 0))

        # 首次布局时把分隔条放在 55% 处：搜索结果是用得最多的一块，默认给它更多高度
        def _init_sash(_event=None) -> None:
            if getattr(self, "_sash_done", False):
                return
            try:
                height = paned.winfo_height()
            except tk.TclError:
                return
            if height > 240:
                try:
                    paned.sashpos(0, int(height * 0.55))
                    self._sash_done = True
                except tk.TclError:
                    pass
        paned.bind("<Configure>", _init_sash)

        res = self._card(paned, "搜索结果（双击一行加入志愿，可多选后批量加入）", padding=12)

        bar = ttk.Frame(res, style="Card.TFrame")
        bar.pack(fill="x", pady=(0, 8))
        self.result_count_var = tk.StringVar(value="还没有搜索")
        ttk.Label(bar, textvariable=self.result_count_var,
                  style="CardStrong.TLabel").pack(side="left")

        self.auto_enrich_var = tk.BooleanVar(value=bool(self.cfg.get("auto_enrich", True)))
        self.var_cache["auto_enrich"] = self.auto_enrich_var
        ttk.Checkbutton(bar, text="自动补全教师与余量", variable=self.auto_enrich_var).pack(
            side="left", padx=(16, 0))

        ttk.Button(bar, text="⬇  加入志愿列表", style="Primary.TButton",
                   command=self.on_add_selected).pack(side="right", padx=8)
        self.enrich_btn = ttk.Button(bar, text="👥  补全教师 / 余量 / 上课时间",
                                     style="Secondary.TButton", command=self.on_enrich)
        self.enrich_btn.pack(side="right")

        cols = ("kcmc", "kch", "teacher", "xf", "time_place", "quota", "kklxdm")
        self.result_tree = ttk.Treeview(res, columns=cols, show="headings", height=8,
                                        selectmode="extended")
        heads = {"kcmc": ("课程名称", 300), "kch": ("课程号", 112), "teacher": ("教师", 112),
                 "xf": ("学分", 60), "time_place": ("上课时间", 236),
                 "quota": ("余量/容量", 118), "kklxdm": ("类型", 64)}
        for c, (text, width) in heads.items():
            self.result_tree.heading(c, text=text)
            self.result_tree.column(c, width=width, anchor="w",
                                    stretch=(c in ("kcmc", "time_place")))
        self.result_tree.pack(side="left", fill="both", expand=True)
        sb1 = ttk.Scrollbar(res, orient="vertical", command=self.result_tree.yview)
        sb1.pack(side="left", fill="y")
        self.result_tree.configure(yscrollcommand=sb1.set)
        # 同一门课的多个教学班共用一色，相邻课程交替 → 一眼看出「这门课有两位老师」
        self.result_tree.tag_configure("grp0", background=C_CARD)
        self.result_tree.tag_configure("grp1", background="#f5f8fd")
        self.result_tree.bind("<Double-1>", lambda e: self.on_add_selected())
        self._search_rows: List[Dict[str, Any]] = []
        paned.add(res, weight=3)

        # 志愿列表
        wl = self._card(paned, "志愿列表（按优先级从上到下抢；勾选 = 启用）", padding=12)
        cols2 = ("enabled", "prio", "kcmc", "teacher", "quota", "status", "msg")

        # 先 pack 底部按钮行（side="bottom"），否则会被 expand=True 的表格挤没
        wact = ttk.Frame(wl, style="Card.TFrame")
        wact.pack(side="bottom", fill="x", pady=(10, 0))
        for text, cmd, pad in (
            ("启用/停用", self.on_toggle_enabled, 0),
            ("↑ 上移", lambda: self.on_move(-1), 6),
            ("↓ 下移", lambda: self.on_move(1), 0),
            ("删除", self.on_delete_course, 6),
            ("清空列表", self.on_clear_wishlist, 0),
            ("清除已抢到标记", self.on_clear_grabbed, 6),
            ("刷新余量", self.on_refresh_quota, 6),
        ):
            ttk.Button(wact, text=text, style="Secondary.TButton", command=cmd).pack(
                side="left", padx=pad)

        wtree = ttk.Frame(wl, style="Card.TFrame")
        wtree.pack(fill="both", expand=True)
        self.wish_tree = ttk.Treeview(wtree, columns=cols2, show="headings", height=7)
        heads2 = {"enabled": ("启用", 56), "prio": ("优先级", 68), "kcmc": ("课程", 280),
                  "teacher": ("教师", 104), "quota": ("余量/容量", 112),
                  "status": ("状态", 90), "msg": ("最近消息", 260)}
        for c, (text, width) in heads2.items():
            self.wish_tree.heading(c, text=text)
            self.wish_tree.column(c, width=width, anchor="w",
                                  stretch=(c in ("kcmc", "msg")))
        self.wish_tree.pack(side="left", fill="both", expand=True)
        sb2 = ttk.Scrollbar(wtree, orient="vertical", command=self.wish_tree.yview)
        sb2.pack(side="left", fill="y")
        self.wish_tree.configure(yscrollcommand=sb2.set)
        self.wish_tree.tag_configure("grabbed", background="#e7f8ef")
        self.wish_tree.bind("<Double-1>", lambda e: self.on_toggle_enabled())
        paned.add(wl, weight=2)

    # ------------------------------------------------------------------ #
    # 标签页 3：抢课监控
    # ------------------------------------------------------------------ #
    def _build_tab_run(self) -> None:
        f = self.tab_run
        top = self._card(f, "控制", padding=12)
        top.pack(fill="x")

        ctl = ttk.Frame(top, style="Card.TFrame")
        ctl.pack(fill="x")
        self.start_btn = ttk.Button(ctl, text="🚀  开始抢课", style="Primary.TButton",
                                    command=self.on_start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(ctl, text="⏹  停止", style="Secondary.TButton",
                                   command=self.on_stop, state="disabled")
        self.stop_btn.pack(side="left", padx=8)
        ttk.Button(ctl, text="清空日志", style="Secondary.TButton",
                   command=self.clear_log).pack(side="left", padx=(8, 0))
        ttk.Button(ctl, text="导出日志", style="Secondary.TButton",
                   command=self.export_log).pack(side="left", padx=8)

        self.run_status = tk.StringVar(value="未开始")
        ttk.Label(ctl, text="运行状态", style="CardHint.TLabel").pack(side="left", padx=(24, 6))
        self.run_status_label = tk.Label(ctl, textvariable=self.run_status, bg=C_CARD,
                                         fg=C_OK, font=(FONT, 13, "bold"))
        self.run_status_label.pack(side="left")

        mon = self._card(f, "实时监控", padding=12)
        mon.pack(fill="both", expand=True, pady=(12, 0))
        cols = ("prio", "kcmc", "teacher", "quota", "status", "attempts")
        self.mon_tree = ttk.Treeview(mon, columns=cols, show="headings", height=8)
        heads = {"prio": ("优先级", 68), "kcmc": ("课程", 300), "teacher": ("教师", 106),
                 "quota": ("余量/容量", 118), "status": ("状态", 124),
                 "attempts": ("提交次数", 92)}
        for c, (text, width) in heads.items():
            self.mon_tree.heading(c, text=text)
            self.mon_tree.column(c, width=width, anchor="w",
                                 stretch=(c in ("kcmc", "status")))
        self.mon_tree.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(mon, orient="vertical", command=self.mon_tree.yview)
        sb.pack(side="left", fill="y")
        self.mon_tree.configure(yscrollcommand=sb.set)
        self.mon_tree.tag_configure("grabbed", background="#e7f8ef")
        self.mon_tree.tag_configure("failed", background="#fdeeee")

        logf = self._card(f, "运行日志", padding=10)
        logf.pack(fill="both", expand=True, pady=(12, 0))
        self.log_text = tk.Text(logf, height=12, wrap="none", font=(MONO, 11),
                                background="#0f172a", foreground="#cbd5e1",
                                insertbackground="#cbd5e1", relief="flat",
                                borderwidth=0, highlightthickness=0, padx=8, pady=6)
        self.log_text.pack(side="left", fill="both", expand=True)
        lsb = ttk.Scrollbar(logf, orient="vertical", command=self.log_text.yview)
        lsb.pack(side="left", fill="y")
        self.log_text.configure(yscrollcommand=lsb.set)
        for tag, color in (("info", "#cbd5e1"), ("success", "#5eead4"), ("warn", "#fcd34d"),
                           ("error", "#fca5a5"), ("debug", "#7c8ba1"), ("time", "#7dd3fc")):
            self.log_text.tag_configure(tag, foreground=color)
        self.log_text.configure(state="disabled")

    # ------------------------------------------------------------------ #
    # 配置读写
    # ------------------------------------------------------------------ #
    def _collect_config(self) -> Dict[str, Any]:
        def num(key: str, default: float, cast=float):
            try:
                return cast(str(self.var_cache[key].get()).strip() or default)
            except (KeyError, ValueError):
                return default

        cfg = self.cfg.as_dict()
        for key, var in self.var_cache.items():
            if isinstance(var, tk.BooleanVar):
                cfg[key] = bool(var.get())
            else:
                cfg[key] = str(var.get())
        cfg["poll_interval"] = max(0.3, num("poll_interval", 1.2))
        cfg["monitor_interval"] = max(0.0, num("monitor_interval", 0.0))
        cfg["concurrent_submit"] = max(1, int(num("concurrent_submit", 1, int)))
        cfg["base_url"] = cfg.get("base_url", "").rstrip("/")
        cfg["smtp_port"] = int(num("smtp_port", 465, int) or 465)
        return cfg

    def _disk_bad_keys(self) -> list:
        """读磁盘上的 bad_keys —— 自愈探测是在工作线程里直接写 config.json 的。"""
        try:
            from config import load_config
            return list(load_config(self.cfg.path).get("bad_keys") or [])
        except Exception:  # noqa: BLE001
            return []

    def on_save_config(self) -> None:
        try:
            cfg = self._collect_config()
            # 合并而不是覆盖：界面保存不能把探测学到的坏字段抹掉
            merged = set(self.cfg.get("bad_keys") or [])
            merged |= set(cfg.get("bad_keys") or [])
            merged |= set(self._disk_bad_keys())
            cfg["bad_keys"] = sorted(merged)
            self.cfg.update(cfg)
            self.cfg.save()
            self.append_log("配置已保存到 config.json", "success")
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("保存失败", str(exc))

    def _sync_engine_cfg(self) -> None:
        """把界面上的最新配置同步给运行中的引擎。"""
        if self.engine:
            self.engine.cfg = self._collect_config()

    def on_test_login(self) -> None:
        self.on_save_config()
        cfg = self._collect_config()

        def work():
            try:
                client = JwglxtClient(cfg, logger=self._client_logger, config_path=self.cfg.path)
                msg = client.login()
                self.log_queue.put(("info", f"✅ 登录成功，学号 {client.su}"))
                self.event_queue.put(("conn", f"● 已连接 · {client.su}"))
                # 顺带问一句「现在能不能选课」。新正方在非选课时间对提交一律返回
                # 一个没有 flag 字段的通用空信封（HTTP 910），提前告知用户能省掉
                # 大量「为什么抢不上」的困惑。
                stage = client.check_stage()
                if stage.get("open") is False:
                    self.log_queue.put(("warn", f"⚠ 当前不在选课时间：{stage.get('msg')}"))
                elif stage.get("open") is True:
                    self.log_queue.put(("info", f"选课时间：{stage.get('msg')}"))
                else:
                    self.log_queue.put(("info", f"选课阶段未知：{stage.get('msg')}"))
            except Exception as exc:  # noqa: BLE001
                self.log_queue.put(("error", f"❌ 登录失败：{exc}"))

        self.append_log("正在测试登录…", "info")
        threading.Thread(target=work, daemon=True).start()

    def on_check_stage(self) -> None:
        """单独查一次服务端：现在是不是在选课时间。"""
        self.on_save_config()
        cfg = self._collect_config()

        def work():
            try:
                client = JwglxtClient(cfg, logger=self._client_logger, config_path=self.cfg.path)
                client.login()
                stage = client.check_stage()
                if stage.get("open") is False:
                    self.log_queue.put(("warn", f"⛔ 当前不在选课时间：{stage.get('msg')}"))
                    self.log_queue.put(("info", "   现在提交会被服务端拒绝。到点后程序会自动重试成功。"))
                elif stage.get("open") is True:
                    self.log_queue.put(("info", f"✅ 选课时间开放：{stage.get('msg')}"))
                else:
                    self.log_queue.put(("warn", f"无法判断选课阶段：{stage.get('msg')}"))
            except Exception as exc:  # noqa: BLE001
                self.log_queue.put(("error", f"检查选课阶段失败：{exc}"))

        self.append_log("正在检查选课阶段…", "info")
        threading.Thread(target=work, daemon=True).start()

    def on_autodetect(self) -> None:
        self.on_save_config()
        cfg = self._collect_config()

        def work():
            try:
                client = JwglxtClient(cfg, logger=self._client_logger, config_path=self.cfg.path)
                client.login()
                found = client.autodetect()
                self.log_queue.put(("info", f"探测结果：{found}"))
                self.event_queue.put(("autodetect", found))
            except Exception as exc:  # noqa: BLE001
                self.log_queue.put(("error", f"探测失败：{exc}"))

        self.append_log("正在登录并探测选课参数…", "info")
        threading.Thread(target=work, daemon=True).start()

    def on_extract_xkkz(self) -> None:
        """从剪贴板内容里提取 xkkz_id，支持直接粘贴 F12 的「表单」文本。

        认可三种形态：
            1. 纯 id：14564675131412412421
            2. 完整 URL：...cxZzxkYzbPartDisplay.html?gnmkdm=N253512&xkkz_id=1456…
            3. F12 → Network → Payload 里复制的表单文本：换行分隔的 k=v
        """
        try:
            text = self.clipboard_get()
        except Exception:  # noqa: BLE001  剪贴板为空或不可用
            text = ""
        if not text.strip():
            messagebox.showwarning(
                "剪贴板为空",
                "请先复制以下任意一个：\n"
                "· 浏览器地址栏里选课页的完整链接；或\n"
                "· F12 → Network → cxZzxkYzbPartDisplay → Payload 里 xkkz_id 那一行；或\n"
                "· 直接复制 xkkz_id 的值本身。",
            )
            return
        xkkz = self._parse_xkkz_from_text(text)
        if not xkkz:
            messagebox.showwarning(
                "没找到 xkkz_id",
                "剪贴板内容里没有可识别的 xkkz_id。\n\n"
                "请在浏览器打开选课界面并点一次「查询」，然后：\n"
                "1. 按 F12 → Network；\n"
                "2. 点开名为 cxZzxkYzbPartDisplay 的请求；\n"
                "3. 在 Payload/表单里找到 xkkz_id，把那一行（或它的值）复制过来。",
            )
            return
        for key in ("xkkz_id_10", "xkkz_id"):
            if key in self.var_cache:
                self.var_cache[key].set(xkkz)
        self.var_cache.setdefault("xkkz_id_10", tk.StringVar(value=xkkz))
        self.cfg["xkkz_id_10"] = xkkz
        self.on_save_config()
        preview = xkkz if len(xkkz) <= 24 else xkkz[:24] + "…"
        self.append_log(f"已提取 xkkz_id = {preview}（每个课程类型共用；如各类型不同请改填对应值）", "info")
        messagebox.showinfo(
            "提取成功",
            f"xkkz_id = {preview}\n\n已填入配置并保存。若你的学校不同课程类型有各自的轮次，\n"
            "请到「系统配置」里按类型分别填写 xkkz_id_10 / xkkz_id_16 等。",
        )

    def _prompt_xkkz_then_retry(self) -> None:
        """搜索因缺少 xkkz_id 失败时，弹窗让用户粘贴该值并立即重试。

        xkkz_id 属于「能从浏览器一秒拿到、但你不会想去找」的参数，所以这里
        不要求用户翻 F12：直接复制选课页地址栏链接、或 F12 里的表单文本、
        或值本身，粘进来会自动识别。
        """
        dlg = tk.Toplevel(self.root)
        dlg.title("需要「选课轮次标识」xkkz_id")
        dlg.transient(self.root)
        dlg.resizable(False, False)
        try:
            dlg.grab_set()
        except tk.TclError:
            pass

        tip = (
            "搜索失败：这个学校的选课页没有下发 xkkz_id（选课轮次标识），\n"
            "所有请求都被服务端当成参数缺项、只回了空对象 {}。\n\n"
            "获取方法（任选一种，拿到后直接粘到下面）：\n"
            "  ① 浏览器打开选课界面 → 点一次「查询」→ 复制地址栏完整链接；\n"
            "  ② 按 F12 → Network → 点开 cxZzxkYzbPartDisplay 请求 →\n"
            "     把 Payload / 表单那一栏整段复制过来；\n"
            "  ③ 直接把 xkkz_id 的值粘进来。\n\n"
            "如果 xkkz_id 不是明文数字（显示为一大串字母数字），也能直接粘。"
        )
        ttk.Label(dlg, text=tip, justify="left").pack(anchor="w", padx=14, pady=(12, 6))

        text = scrolledtext.ScrolledText(dlg, width=72, height=7)
        text.pack(fill="both", expand=True, padx=14)
        try:
            clip = self.clipboard_get()
        except Exception:  # noqa: BLE001
            clip = ""
        if clip.strip():
            text.insert("1.0", clip.strip())

        status = ttk.Label(dlg, text="", foreground="#b00")
        status.pack(anchor="w", padx=14)

        btns = ttk.Frame(dlg)
        btns.pack(fill="x", padx=14, pady=(4, 12))

        def do_apply() -> None:
            value = self._parse_xkkz_from_text(text.get("1.0", "end"))
            if not value:
                status.configure(
                    text="没认出 xkkz_id。请确认粘的是选课页链接、"
                         "F12 表单内容，或那一串 id 本身。"
                )
                return
            self.cfg["xkkz_id_auto"] = value
            self.cfg["xkkz_id_10"] = value
            if "xkkz_id_10" in self.var_cache:
                self.var_cache["xkkz_id_10"].set(value)
            self.on_save_config()
            self.append_log(f"已保存 xkkz_id = {value[:24]}…，正在重新搜索…", "info")
            dlg.destroy()
            self.on_search()

        ttk.Button(btns, text="用这个值重试", command=do_apply,
                   style="Primary.TButton").pack(side="right", padx=(8, 0))
        ttk.Button(btns, text="取消", command=dlg.destroy).pack(side="right")

        text.focus_set()
        dlg.wait_window()

    @staticmethod
    def _parse_xkkz_from_text(text: str) -> str:
        """从一段文本里抽出 xkkz_id，抽不到返回空串。"""
        text = (text or "").strip()
        if not text:
            return ""
        # 0. HTML 片段：name="xkkz_id" ... value="1456…"（顺序可能颠倒）
        m = re.search(
            r'name=["\']?[^"\'>]*[Xx]kkz[_]?[Ii]d["\']?[^>]{0,200}?value=["\']([0-9A-Za-z_\-]{4,})',
            text,
        )
        if m:
            return m.group(1)
        m = re.search(
            r'value=["\']([0-9A-Za-z_\-]{4,})["\'][^>]{0,200}?name=["\']?[^"\'>]*[Xx]kkz[_]?[Ii]d',
            text,
        )
        if m:
            return m.group(1)
        # 1. k=v（含前后有 & 或换行的情形；值可能被引号包着）
        m = re.search(
            r"[Xx]kkz[_]?[Ii]d\s*[=:]\s*['\"]?([0-9A-Za-z_\-]{4,})", text
        )
        if m:
            return m.group(1)
        # 2. 整行就是一个 id
        for line in text.splitlines():
            line = line.strip()
            if re.fullmatch(r"[0-9A-Za-z_\-]{6,}", line):
                return line
        # 3. 整段就是一个 id
        if re.fullmatch(r"[0-9A-Za-z_\-]{6,}", text):
            return text
        return ""

    def on_test_notify(self) -> None:
        cfg = self._collect_config()
        try:
            Notifier(cfg, logger=self._client_logger).send(
                "抢课助手 · 测试通知",
                f"如果你看到这条消息，说明通知渠道配置正确。\n时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
                async_=False,
            )
            self.append_log("测试通知已触发（桌面弹窗为阻塞式，关闭后继续）。", "info")
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("通知失败", str(exc))

    # ------------------------------------------------------------------ #
    # 搜索与志愿列表
    # ------------------------------------------------------------------ #
    def on_search(self) -> None:
        self.on_save_config()
        cfg = self._collect_config()
        keyword = self.search_var.get().strip()
        kk = self.search_type_var.get()
        kk_value = None if kk == "自动" else kk
        self.search_btn.configure(state="disabled")
        self.search_status_var.set("搜索中…")

        def work():
            try:
                client = JwglxtClient(cfg, logger=self._client_logger, config_path=self.cfg.path)
                client.login()
                client.fetch_xsxk_page()
                rows = client.search_courses(keyword, kk_value)
                self.event_queue.put(("search", {
                    "rows": rows,
                    "trace": list(client.search_trace),
                    "payload_keys": list(client.last_search_payload_keys),
                    "sample_keys": list(client.last_search_sample_keys),
                    "fallback": getattr(client, "keyword_fallback_used", False),
                }))
            except Exception as exc:  # noqa: BLE001
                self.log_queue.put(("error", f"搜索失败：{exc}"))
                self.event_queue.put(("search", {
                    "rows": [],
                    "trace": list(getattr(client, "search_trace", []) or []),
                    "need_xkkz": "xkkz_id" in str(exc) and "未取到" in str(exc),
                }))
                # 缺 xkkz_id 是「缺一个可手工补上的参数」，直接给出补参数入口
                if "xkkz_id" in str(exc) and "未取到" in str(exc):
                    self.event_queue.put(("need_xkkz", None))

        threading.Thread(target=work, daemon=True).start()

    def on_toggle_only_open(self) -> None:
        """「只看还有余量」开关：重绘结果表并同步状态栏的「已隐藏 N 个」提示。"""
        rows = getattr(self, "_search_rows", []) or []
        self._render_result_rows(rows)
        if rows:
            self.search_status_var.set(
                self._view_count_text(rows) + getattr(self, "_search_suffix", ""))

    def _visible_rows(self, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """按「只看还有余量」过滤（余量未知的先留着，避免没补全时全被藏掉）。"""
        if not getattr(self, "only_open_var", None) or not self.only_open_var.get():
            return list(rows)
        out = []
        for r in rows:
            cap, enr = r.get("capacity", -1), r.get("enrolled", -1)
            if cap is None or enr is None or cap <= 0 or enr < 0 or cap - enr > 0:
                out.append(r)
        return out

    def _view_count_text(self, rows: List[Dict[str, Any]]) -> str:
        view = self._visible_rows(rows)
        text = self._count_text(view)
        hidden = len(rows) - len(view)
        if hidden > 0:
            text += f"（已隐藏 {hidden} 个已满教学班）"
        return text

    def _render_result_rows(self, rows: List[Dict[str, Any]], update_count: bool = True) -> None:
        rows = self._visible_rows(rows)
        for item in self.result_tree.get_children():
            self.result_tree.delete(item)
        # 同一门课的多个教学班用同一种底色成组显示，方便一眼看出
        # 「这门课有两位老师 / 两个时间段」；相邻课程交替底色区分。
        last_kch = None
        group = -1
        for row in rows:
            kch = str(row.get("kch_id") or row.get("kch") or "")
            if kch != last_kch:
                group += 1
                last_kch = kch
            self.result_tree.insert("", "end", values=self._result_values(row),
                                    tags=(f"grp{group % 2}",))
        if update_count:
            self.result_count_var.set(self._count_text(rows))

    @staticmethod
    def _count_text(rows: List[Dict[str, Any]]) -> str:
        """课程数 vs 教学班数分开报——这正是「网站 50 门、软件 57 条」的来源。"""
        total = len(rows)
        courses = len({str(r.get("kch_id") or r.get("kch") or "") for r in rows})
        if not total:
            return "没有结果"
        if courses and courses != total:
            return f"共 {total} 个教学班 · 对应 {courses} 门课程"
        return f"共 {total} 门课程"

    @staticmethod
    def _result_values(row: Dict[str, Any]) -> tuple:
        cap, enr = row.get("capacity", -1), row.get("enrolled", -1)
        if cap is None or enr is None or cap < 0 or enr < 0:
            quota = "—"
        else:
            left = max(cap - enr, 0)
            quota = f"{left} / {cap}" + ("（满）" if left <= 0 else "")
        return (
            row.get("kcmc", ""),
            row.get("kch", ""),
            row.get("teacher", "") or "—",
            row.get("xf", ""),
            row.get("time_place", "") or "—",
            quota,
            row.get("kklxdm", ""),
        )

    def _apply_search_results(self, data: Any = None) -> None:
        """渲染搜索结果。

        data 可以是旧式的 rows 列表，也可以是搜索线程回传的诊断事件：
        {"rows": [...], "trace": [...], "payload_keys": [...],
         "sample_keys": [...], "fallback": bool}
        """
        event = data if isinstance(data, dict) else {"rows": data or []}
        rows = event.get("rows") or []
        self._search_trace = event.get("trace") or []
        self._search_shape = (event.get("payload_keys") or [], event.get("sample_keys") or [])
        if event.get("need_xkkz"):
            self._search_need_xkkz = True
        self.search_btn.configure(state="normal")
        self._search_rows = list(rows)
        self._search_suffix = ("（关键字无匹配，已列出全部课程）"
                               if rows and event.get("fallback") else "")
        self._render_result_rows(rows)
        if rows and event.get("fallback"):
            self.search_status_var.set(self._view_count_text(rows) + self._search_suffix)
            self.append_log(
                "关键字没有匹配到课程，已自动改为空查询列出全部课程，"
                "请从结果里挑选目标课程。",
                "warn",
            )
        else:
            self.search_status_var.set(self._view_count_text(rows))
        if not rows:
            self._explain_empty_search()
            return

        # 搜索结果本身不带教师/容量/时间，需要按课程逐个查教学班才能拿到。
        # 自动补全是默认行为（用户要的「分辨不同老师的同一门课」靠它）。
        missing = any(not r.get("teacher") for r in rows)
        if missing and self.auto_enrich_var.get():
            self._start_enrich()
        elif missing:
            self.append_log(
                "搜索结果不含教师/余量，点「👥 补全教师 / 余量 / 上课时间」可按教学班展开"
                "（同一门课不同老师会分行显示）。",
                "info",
            )

    # ------------------------------------------------------------------ #
    # 补全教师 / 余量 / 上课时间
    # ------------------------------------------------------------------ #
    def on_enrich(self) -> None:
        if not self._search_rows:
            messagebox.showinfo("提示", "请先搜索课程，再补全教师与余量。")
            return
        if self._enriching:
            self.append_log("补全正在进行中，请稍候…", "warn")
            return
        self._start_enrich()

    def _start_enrich(self) -> None:
        rows = list(self._search_rows)
        cfg = self._collect_config()
        self._enriching = True
        self.enrich_btn.configure(state="disabled")
        self.search_status_var.set("正在补全教师、余量与上课时间…")
        self.append_log(f"开始补全 {len(rows)} 门课程的教师/余量信息…", "info")

        def work():
            try:
                client = JwglxtClient(cfg, logger=self._client_logger, config_path=self.cfg.path)
                client.login()
                client.fetch_xsxk_page()

                def progress(done, total, name):
                    self.event_queue.put(("enrich_progress", (done, total, name)))

                full = client.enrich_rows(
                    rows, on_progress=progress, should_stop=lambda: self._closing
                )
                self.event_queue.put(("enriched", full))
            except Exception as exc:  # noqa: BLE001
                self.log_queue.put(("warn", f"补全教师/余量失败：{exc}"))
                self.event_queue.put(("enriched", None))

        threading.Thread(target=work, daemon=True).start()

    def _apply_enriched(self, rows: Any) -> None:
        self._enriching = False
        self.enrich_btn.configure(state="normal")
        if not rows:
            self.search_status_var.set(self._count_text(self._search_rows))
            return
        self._search_rows = list(rows)
        self._render_result_rows(rows)
        self.search_status_var.set(
            self._view_count_text(rows) + getattr(self, "_search_suffix", "")
            + " · 已带教师/余量")
        teachers = len({r.get("teacher") for r in rows if r.get("teacher")})
        self.append_log(
            f"补全完成：{self._count_text(rows)}，涉及 {teachers} 位教师。"
            "同一门课出现多行即代表不同老师或不同时间段，可分别加入志愿。",
            "success",
        )

    def _explain_empty_search(self) -> None:
        """搜不到时给出可执行的排查说明，而不是只显示「0 条」。"""
        trace = getattr(self, "_search_trace", []) or []
        payload_keys, sample_keys = getattr(self, "_search_shape", ([], []))
        self.search_status_var.set("0 条结果（点「诊断」看详情）")
        self.append_log("———— 未搜索到任何课程，以下是逐次尝试记录 ————", "warn")
        for line in trace[-24:]:
            self.append_log("  " + line)
        if payload_keys:
            self.append_log(f"服务器返回的顶层字段：{', '.join(payload_keys)}")
        if sample_keys:
            self.append_log(f"课程行实际字段名：{', '.join(sample_keys)}")
        if getattr(self, "_search_need_xkkz", False):
            self.append_log(
                "本次失败的根因是缺少 xkkz_id（选课轮次标识）：所有尝试都返回空对象 {}。"
                "点上方「从浏览器复制的链接/表单里提取 xkkz_id」按钮即可手工补上，"
                "不用再去 F12 里翻参数。",
                "warn",
            )
        self.append_log(
            "可能原因：① 选课页需要先点「查询」才出课程（本程序已自动为空关键字补一次查询）；"
            "② 你的学校搜索接口参数不同（请在浏览器 F12 → Network 里点「查询」，"
            "把 cxZzxkYzbPartDisplay 请求的表单参数贴给开发者）；"
            "③ 课程类型代码不对，试试把「课程类型」改成 10 / 16 逐个搜索；"
            "④ 当前不在选课时间内，服务端不返回课程。",
            "warn",
        )

    def on_diagnose(self) -> None:
        """主动跑一遍登录 + 解析 + 搜索，把协议细节写进日志，便于对照浏览器。"""
        self.on_save_config()
        cfg = self._collect_config()
        self.append_log("========== 开始诊断 ==========")
        self.search_btn.configure(state="disabled")

        def work():
            try:
                client = JwglxtClient(cfg, logger=self._client_logger, config_path=self.cfg.path)
                client.login()
                self.append_log("[1/4] 登录成功" if client.logged_in else "[1/4] 登录状态未知")
                html = client.fetch_xsxk_page()
                self.append_log(f"[2/4] 选课页 {len(html)} 字符")
                if client.xkkz_map:
                    for k, v in client.xkkz_map.items():
                        self.append_log(f"      kklxdm={k} → xkkz_id={v}")
                else:
                    self.append_log("      没有解析到任何 xkkz_id（选课轮次未开放或不在此页面）", "warn")
                found = client.autodetect()
                self.append_log(f"[3/4] 自动探测结果：{found or '（空）'}")
                keyword = self.search_var.get().strip()
                rows = client.search_courses(keyword, None)
                for line in client.search_trace:
                    self.append_log("      " + line)
                if client.last_search_sample_keys:
                    self.append_log(
                        "      课程行字段：" + ", ".join(client.last_search_sample_keys)
                    )
                self.append_log(f"[4/4] 搜索到 {len(rows)} 条")
                self.event_queue.put(("search", {
                    "rows": rows,
                    "trace": list(client.search_trace),
                    "payload_keys": list(client.last_search_payload_keys),
                    "sample_keys": list(client.last_search_sample_keys),
                    "fallback": getattr(client, "keyword_fallback_used", False),
                }))
            except Exception as exc:  # noqa: BLE001
                self.append_log(f"诊断失败：{type(exc).__name__}: {exc}", "error")
                self.event_queue.put(("search", {"rows": []}))

        threading.Thread(target=work, daemon=True).start()

    def on_add_selected(self) -> None:
        sel = self.result_tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先在搜索结果里选中课程（可按住 Ctrl 多选）。")
            return
        added = 0
        for iid in sel:
            index = self.result_tree.index(iid)
            if index >= len(self._search_rows):
                continue
            course = Course.from_search_item(self._search_rows[index])
            if self.wishlist.add(course):
                added += 1
        self.refresh_wishlist_tree()
        self.append_log(f"已加入 {added} 门课程到志愿列表。", "success")

    def on_manual_add(self) -> None:
        win = tk.Toplevel(self.root)
        win.title("手动添加课程")
        win.transient(self.root)
        win.grab_set()
        fields = [("课程号 kch", "kch"), ("课程名", "kcmc"), ("教师", "teacher"),
                  ("学分", "xf"), ("教学班 jxb（可空）", "jxb"),
                  ("xkkz_id（可空）", "xkkz_id"), ("课程类型 kklxdm", "kklxdm")]
        vars_: Dict[str, tk.StringVar] = {}
        for i, (label, key) in enumerate(fields):
            ttk.Label(win, text=label).grid(row=i, column=0, sticky="w", padx=10, pady=5)
            v = tk.StringVar(value="10" if key == "kklxdm" else "")
            vars_[key] = v
            ttk.Entry(win, textvariable=v, width=34).grid(row=i, column=1, padx=10, pady=5)

        def do_add():
            if not vars_["kch"].get().strip():
                messagebox.showwarning("提示", "课程号必填。", parent=win)
                return
            course = Course(
                course_id=f"{vars_['kch'].get().strip()}::{vars_['jxb'].get().strip()}",
                kch=vars_["kch"].get().strip(),
                kcmc=vars_["kcmc"].get().strip(),
                teacher=vars_["teacher"].get().strip(),
                xf=vars_["xf"].get().strip(),
                jxb=vars_["jxb"].get().strip(),
                xkkz_id=vars_["xkkz_id"].get().strip(),
                kklxdm=vars_["kklxdm"].get().strip() or "10",
            )
            if self.wishlist.add(course):
                self.append_log(f"已手动添加：{course.title}", "success")
            else:
                self.append_log("该课程已在志愿列表中。", "warn")
            self.refresh_wishlist_tree()
            win.destroy()

        ttk.Button(win, text="添加", command=do_add).grid(row=len(fields), column=0, pady=12)
        ttk.Button(win, text="取消", command=win.destroy).grid(row=len(fields), column=1, pady=12)

    def refresh_wishlist_tree(self) -> None:
        for iid in self.wish_tree.get_children():
            self.wish_tree.delete(iid)
        for course in self.wishlist.sorted_courses():
            cap, enr = course.capacity, course.enrolled
            quota = "未知" if cap < 0 or enr < 0 else f"{max(cap - enr, 0)}/{cap}"
            status = "已抢到 ✅" if course.grabbed else ("监控中" if course.enabled else "已停用")
            self.wish_tree.insert("", "end", iid=course.course_id, values=(
                "☑" if course.enabled else "☐", course.priority, course.kcmc or course.kch,
                course.teacher, quota, status, course.last_msg,
            ))
        self.refresh_monitor_tree()

    def refresh_monitor_tree(self) -> None:
        for iid in self.mon_tree.get_children():
            self.mon_tree.delete(iid)
        for course in self.wishlist.sorted_courses():
            cap, enr = course.capacity, course.enrolled
            quota = "未知" if cap < 0 or enr < 0 else f"{max(cap - enr, 0)}/{cap}"
            status = "已抢到 ✅" if course.grabbed else ("监控中" if course.enabled else "已停用")
            tag = "grabbed" if course.grabbed else ""
            self.mon_tree.insert("", "end", iid=course.course_id, tags=(tag,), values=(
                course.priority, course.kcmc or course.kch, course.teacher,
                quota, status, course.attempts,
            ))

    def _selected_course(self) -> Optional[Course]:
        sel = self.wish_tree.selection()
        if not sel:
            return None
        cid = sel[0]
        return next((c for c in self.wishlist.courses if c.course_id == cid), None)

    def on_toggle_enabled(self) -> None:
        course = self._selected_course()
        if not course:
            return
        course.enabled = not course.enabled
        self.wishlist.save()
        self.refresh_wishlist_tree()

    def on_move(self, delta: int) -> None:
        sel = self.wish_tree.selection()
        if not sel:
            return
        ordered = self.wishlist.sorted_courses()
        index = next((i for i, c in enumerate(ordered) if c.course_id == sel[0]), None)
        if index is None:
            return
        new_index = self.wishlist.move(index, delta)
        self.refresh_wishlist_tree()
        children = self.wish_tree.get_children()
        if new_index < len(children):
            self.wish_tree.selection_set(children[new_index])

    def on_delete_course(self) -> None:
        course = self._selected_course()
        if not course:
            return
        if messagebox.askyesno("确认", f"从志愿列表移除「{course.title}」？"):
            self.wishlist.remove(course.course_id)
            self.refresh_wishlist_tree()

    def on_clear_wishlist(self) -> None:
        if not self.wishlist.courses:
            return
        if messagebox.askyesno("确认", "清空整个志愿列表？此操作不可撤销。"):
            self.wishlist.courses = []
            self.wishlist.save()
            self.refresh_wishlist_tree()

    def on_clear_grabbed(self) -> None:
        """清除「已抢到」标记，让同一批课可以重新开抢。

        默认情况下引擎不会重复提交已经抢到的课程 —— 新正方在计划内选课中
        对已选课程重复提交，有机会把已选课程退掉。只有用户明确点这里，
        才会清掉标记重新抢。
        """
        marked = [c for c in self.wishlist.courses if c.grabbed]
        if not marked:
            messagebox.showinfo("提示", "当前没有已抢到的课程。")
            return
        if not messagebox.askyesno(
            "确认",
            f"将清除 {len(marked)} 门课程的「已抢到」标记。\n\n"
            "清除后再次开始抢课会重新向教务系统提交这些课程，"
            "重复提交有可能导致已选课程被退掉。确定继续？",
        ):
            return
        self.wishlist.clear_grabbed()
        self.refresh_wishlist_tree()
        self.append_log("已清除「已抢到」标记。", "warn")

    def on_refresh_quota(self) -> None:
        self.on_save_config()
        cfg = self._collect_config()
        courses = self.wishlist.enabled_courses()
        if not courses:
            messagebox.showinfo("提示", "志愿列表为空。")
            return
        self.append_log("正在刷新余量…", "info")

        def work():
            client = JwglxtClient(cfg, logger=self._client_logger, config_path=self.cfg.path)
            try:
                client.login()
                client.fetch_xsxk_page()
            except Exception as exc:  # noqa: BLE001
                self.log_queue.put(("error", f"刷新失败：{exc}"))
                return
            for course in courses:
                try:
                    if not course.xkkz_id:
                        for kk in [course.kklxdm] + list(cfg.get("kklxdm_candidates") or []):
                            try:
                                course.xkkz_id = client.get_xkkz_id(str(kk))
                                course.kklxdm = str(kk)
                                break
                            except JwglxtError:
                                continue
                    classes = client.get_jxb_ids(course.kch, course.xkkz_id,
                                                 course.kcmc or course.kch, course.kklxdm,
                                                 kch_id=course.kch_id)
                    target = next(
                        (c for c in classes if course.jxb and c.get("jxb") == course.jxb),
                        classes[0] if classes else None,
                    )
                    if target:
                        course.capacity = int(target.get("capacity", -1))
                        course.enrolled = int(target.get("enrolled", -1))
                        self.log_queue.put((
                            "info",
                            f"{course.title}：余量 {max(course.seats_left, 0)}/"
                            f"{course.capacity if course.capacity >= 0 else '未知'}",
                        ))
                    else:
                        self.log_queue.put(("warn", f"{course.title}：未查到教学班。"))
                except Exception as exc:  # noqa: BLE001
                    self.log_queue.put(("warn", f"{course.title}：{exc}"))
            self.wishlist.save()
            self.event_queue.put(("refresh_quota", None))

        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------------ #
    # 启停
    # ------------------------------------------------------------------ #
    def on_start(self) -> None:
        if self.engine and self.engine.running:
            messagebox.showinfo("提示", "抢课引擎已在运行。")
            return
        try:
            self.on_save_config()
            cfg = self._collect_config()
            self.engine = GrabEngine(
                cfg, self.wishlist,
                log_cb=lambda msg, level="info": self.log_queue.put((level, msg)),
                status_cb=lambda status: self.event_queue.put(("status", status)),
                event_cb=lambda kind: self.event_queue.put(("engine_event", kind)),
                config_path=self.cfg.path,
            )
            self.engine.start()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("无法启动", str(exc))
            return
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.run_status.set("启动中…")
        self.nb.select(self.tab_run)

    def on_stop(self) -> None:
        if self.engine:
            self.engine.stop()
            self.run_status.set("正在停止…")

    def _on_close(self) -> None:
        if self.engine and self.engine.running:
            if not messagebox.askyesno("确认退出", "抢课引擎正在运行，确定要退出吗？"):
                return
            self.engine.stop()
        self._closing = True
        self.root.destroy()

    # ------------------------------------------------------------------ #
    # 日志与队列
    # ------------------------------------------------------------------ #
    def append_log(self, message: str, level: str = "info") -> None:
        """线程安全地写日志（可从任意线程调用）。"""
        self.log_queue.put((level, message))

    def _client_logger(self, level: str, message: str = "info") -> None:
        """适配 JwglxtClient / Notifier 的 (level, message) 调用约定。

        这些后台模块历史上按 (level, message) 调用日志回调，而界面统一用
        (message, level)。若直接把 append_log 传进去，消息与级别会互换，
        日志里就只剩一个「info」/「warn」字样 —— 这正是之前诊断信息丢失的原因。
        """
        if message in ("info", "warn", "error", "success") and level not in (
            "info", "warn", "error", "success"
        ):
            level, message = message, level
        self.append_log(message, level)

    def clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def export_log(self) -> None:
        content = self.log_text.get("1.0", "end")
        path = filedialog.asksaveasfilename(
            title="导出日志", defaultextension=".txt",
            initialfile=f"grab-log-{datetime.now():%Y%m%d-%H%M%S}.txt",
            filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")],
        )
        if not path:
            return
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        self.append_log(f"日志已导出：{path}", "success")

    def _append_log_text(self, level: str, message: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{ts}] ", "time")
        self.log_text.insert("end", f"{message}\n", LEVEL_TAGS.get(level, "info"))
        self.log_text.see("end")
        self.log_text.configure(state="disabled")
        # 只保留最近 2000 行，避免长跑内存膨胀
        if int(self.log_text.index("end-1c").split(".")[0]) > 2000:
            self.log_text.configure(state="normal")
            self.log_text.delete("1.0", "500.0")
            self.log_text.configure(state="disabled")

    def _poll_queues(self) -> None:
        try:
            while True:
                level, message = self.log_queue.get_nowait()
                self._append_log_text(level, message)
        except queue.Empty:
            pass
        try:
            while True:
                kind, payload = self.event_queue.get_nowait()
                self._handle_event(kind, payload)
        except queue.Empty:
            pass
        self.root.after(POLL_MS, self._poll_queues)

    def _handle_event(self, kind: str, payload: Any) -> None:
        if kind == "status":
            self.run_status.set(str(payload))
            self.status_var.set(str(payload))
            if str(payload) in ("已停止", "异常终止", "登录失败", "已取消"):
                self.start_btn.configure(state="normal")
                self.stop_btn.configure(state="disabled")
            return
        if kind == "search":
            self._apply_search_results(payload)
            return
        if kind == "enrich_progress":
            try:
                done, total, name = payload
                self.search_status_var.set(f"正在补全 {done}/{total}：{name}")
            except Exception:  # noqa: BLE001
                pass
            return
        if kind == "enriched":
            self._apply_enriched(payload)
            return
        if kind == "conn":
            self.header_status.configure(text=str(payload), fg="#34d399")
            return
        if kind == "need_xkkz":
            self._prompt_xkkz_then_retry()
            return
        if kind == "refresh_quota":
            self.refresh_wishlist_tree()
            return
        if kind == "autodetect":
            self._apply_autodetect(payload or {})
            return
        if kind == "engine_event":
            self.refresh_wishlist_tree()
            return

    def _apply_autodetect(self, found: Dict[str, Any]) -> None:
        mapping = {
            "njdm_id": "njdm_id", "zyh_id": "zyh_id", "bh_id": "bh_id",
            "jg_id": "jg_id", "xqh_id": "xqh_id", "xkxnm": "xkxnm", "xkxqm": "xkxqm",
        }
        applied = []
        for src, dst in mapping.items():
            if found.get(src) and dst in self.var_cache:
                self.var_cache[dst].set(str(found[src]))
                applied.append(f"{dst}={found[src]}")
        xkkz_map = found.get("xkkz_map") or {}
        if xkkz_map:
            first_type, first_id = next(iter(xkkz_map.items()))
            if "xkkz_id_10" in self.var_cache and not self.var_cache["xkkz_id_10"].get():
                self.var_cache["xkkz_id_10"].set(str(first_id))
                applied.append(f"xkkz_id({first_type})={str(first_id)[:12]}…")
            for kk, xkkz in xkkz_map.items():
                self.cfg[f"xkkz_id_{kk}"] = xkkz
        self.on_save_config()
        if applied:
            messagebox.showinfo("探测完成", "已自动填入：\n" + "\n".join(applied))
        else:
            messagebox.showwarning(
                "探测结果有限",
                "未能自动识别选课参数。请用浏览器打开选课页，\n"
                "按 F12 查看 network 请求中的 njdm_id / zyh_id / xkkz_id 并手工填写。",
            )


def main() -> None:
    root = tk.Tk()
    try:
        # Windows 高 DPI 适配
        if sys.platform == "win32":
            from ctypes import windll
            windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    app = GrabberApp(root)
    app.append_log(f"{APP_TITLE} 已启动。首次使用请先填写教务地址、学号与密码，然后点「测试登录」。", "info")
    try:
        root.mainloop()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        try:
            messagebox.showerror("程序异常", traceback.format_exc())
        except Exception:
            pass
