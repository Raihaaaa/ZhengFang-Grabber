# -*- coding: utf-8 -*-
"""正方教务抢课助手 —— 启动入口。

用法：
    python run.py            启动图形界面
    python run.py --version  查看版本
"""
from __future__ import annotations

import sys

VERSION = "1.2.0"


def check_dependencies() -> bool:
    """检查必需依赖，缺失时给出清晰的中文提示。"""
    missing = []
    try:
        import requests  # noqa: F401
    except ImportError:
        missing.append("requests")

    if not missing:
        return True

    hint = (
        "缺少必需依赖：\n\n    " + "\n    ".join(missing) + "\n\n"
        "请在本程序目录执行：\n\n"
        f"    pip install {' '.join(missing)}\n\n"
        "或一次性安装全部依赖：\n\n"
        "    pip install -r requirements.txt\n"
    )
    print(hint)
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("缺少依赖", hint)
        root.destroy()
    except Exception:
        pass
    return False


def main() -> int:
    if "--version" in sys.argv:
        print(f"正方教务抢课助手 v{VERSION}")
        return 0

    if not check_dependencies():
        return 1

    from gui import main as gui_main
    gui_main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
