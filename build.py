# -*- coding: utf-8 -*-
"""打包脚本：把抢课助手编译成 exe（同时产出单文件版与文件夹版）。

用法：
    pip install pyinstaller
    python build.py

产物：
    dist/正方抢课助手.exe                    单文件版（运行时会解压到系统临时目录）
    dist/正方抢课助手-文件夹版/正方抢课助手.exe  文件夹版（**不写临时目录**）

为什么要两个版本：单文件版每次启动都要把内置的 Python 运行时解压到
`%TEMP%\\_MEIxxxxxx`，部分安全软件（实测：火绒）会拦截这一步，报
「Could not create the temporary directory」。文件夹版没有解压过程，
不受该限制，属于遇到拦截时的兜底方案。

注意：Tkinter 是标准库，PyInstaller 会自动收集；RSA 加密是纯 Python 实现，
      无需任何 Crypto hidden-import，打包产物也不依赖 VC 运行库。
      脚本会固定 PYTHONHASHSEED=0 并在失败时自动重试，避免 PyInstaller
      分析阶段的偶发崩溃。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

APP_NAME = "正方抢课助手"
DIR_BUILD_NAME = "正方抢课助手-文件夹版"
ENTRY = "run.py"
ICON = "app.ico"  # 可选，存在则使用
ATTEMPTS = 5      # PyInstaller 分析阶段偶发崩溃（0xC0000005），失败就重试


def _clear_pycache() -> None:
    """构建前删掉所有 `__pycache__`。

    PyInstaller 的分析阶段会读 `.pyc`；实测**陈旧或被污染的 `__pycache__`** 会让它抛
    `TypeError: required field "id" missing from Name` /
    `SystemError: Unmatched paren in format` / 0xC0000005 访问冲突，
    而且重试多少次都一样 —— 清掉缓存后立刻正常。所以每次构建（含重试）前先清一次。
    """
    removed = 0
    for root, dirs, _files in os.walk("."):
        for d in list(dirs):
            if d == "__pycache__":
                shutil.rmtree(os.path.join(root, d), ignore_errors=True)
                dirs.remove(d)
                removed += 1
    if removed:
        print(f"已清理 {removed} 个 __pycache__ 目录")


def build(onefile: bool) -> int:
    """跑一次 PyInstaller；onefile=False 时产出文件夹版。"""
    name = APP_NAME if onefile else DIR_BUILD_NAME
    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--onefile" if onefile else "--onedir",
        "--windowed",                 # 不弹黑色控制台
        "--name", name,
        "--hidden-import", "charset_normalizer",
        "--exclude-module", "numpy",
        "--exclude-module", "matplotlib",
        "--exclude-module", "PIL",
    ]
    if os.path.exists(ICON):
        args += ["--icon", ICON]
    args.append(ENTRY)

    # PyInstaller 的分析阶段依赖各模块的遍历顺序，Python 默认的随机哈希种子
    # 会让同一个命令时好时坏（实测报过 TypeError / SystemError: Unmatched paren
    # in format / 0xC0000005 访问冲突）。固定种子后即可稳定成功。
    env = dict(os.environ)
    env["PYTHONHASHSEED"] = "0"

    print("执行：", " ".join(args))
    result = 1
    for attempt in range(1, ATTEMPTS + 1):
        _clear_pycache()
        if attempt > 1:
            print(f"\n第 {attempt} 次重试…")
        result = subprocess.call(args, env=env)
        if result == 0:
            break
    if result != 0:
        print(
            "打包失败（已重试 %d 次）。\n"
            "提示：PyInstaller 分析偶发崩溃时重跑一次通常就好了；"
            "若始终失败，可尝试 pip install -U pyinstaller 后重试。" % ATTEMPTS
        )
    return result


def main() -> int:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("未安装 PyInstaller，请先执行：\n    pip install pyinstaller")
        return 1

    # 先做文件夹版：它是遇到安全软件拦截时的推荐产物，不能被单文件版的
    # 偶发崩溃连累。单文件版失败只警告，不算整体失败。
    rc_dir = build(onefile=False)
    print()
    rc_one = build(onefile=True)
    if rc_dir != 0:
        print("文件夹版打包失败，请重跑一次 python build.py。")
        return rc_dir
    if rc_one != 0:
        print(
            "\n注意：单文件版这次没打出来（PyInstaller 分析阶段偶发崩溃）。\n"
            "      文件夹版已经可用，直接用它即可；想再要单文件版就重跑一次 python build.py。"
        )

    # PyInstaller 的文件夹版会把 exe 命名成与文件夹同名（正方抢课助手-文件夹版.exe），
    # 看着别扭，这里改成统一的 正方抢课助手.exe。改名不影响它查找同级的 _internal。
    old_exe = os.path.join("dist", DIR_BUILD_NAME, DIR_BUILD_NAME + ".exe")
    new_exe = os.path.join("dist", DIR_BUILD_NAME, APP_NAME + ".exe")
    if os.path.exists(old_exe) and not os.path.exists(new_exe):
        os.replace(old_exe, new_exe)

    print(f"\n打包完成：")
    print(f"  单文件版：{os.path.abspath(os.path.join('dist', APP_NAME + '.exe'))}")
    print(f"  文件夹版：{os.path.abspath(os.path.join('dist', DIR_BUILD_NAME, APP_NAME + '.exe'))}")
    print("提示：把 exe 单独放到一个空目录里首次运行，会自动生成 config.json。")
    print("      若单文件版报「Could not create the temporary directory」，")
    print("      说明安全软件（如火绒）拦截了临时目录解压，请改用文件夹版，")
    print("      或把 exe 加入安全软件的信任区。")

    # 顺手把说明文档复制到 dist（文件夹版旁边也放一份）
    for extra in ("README.md", "requirements.txt"):
        if os.path.exists(extra):
            shutil.copy2(extra, os.path.join("dist", extra))
            shutil.copy2(extra, os.path.join("dist", DIR_BUILD_NAME, extra))
    if os.path.isdir("docs"):
        dst = os.path.join("dist", "docs")
        if os.path.isdir(dst):
            shutil.rmtree(dst, ignore_errors=True)
        shutil.copytree("docs", dst)
    return 0


if __name__ == "__main__":
    sys.exit(main())
