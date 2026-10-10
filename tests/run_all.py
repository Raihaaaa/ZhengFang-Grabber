# -*- coding: utf-8 -*-
"""一键运行全部离线测试。

    python tests/run_all.py

六个测试都不联网、不接触真实教务系统：
  mock_server_test.py    本地起一个模拟正方服务器，跑通登录→搜索→取教学班→提交全链路
  search_fallback_test.py 复刻「必须先点查询才出课程」的学校，验证搜索兜底与诊断记录
  paging_test.py         复刻「只能搜到少量课程」与「提交恒回 flag=0」，守住翻页窗口
                         与提交体必须带页面下发的身份/学期参数
  xkkz_parse_test.py     轮次标识 xkkz_id 的各种页面形态解析、取值优先级与剪贴板提取
  gui_smoke_test.py      真正构造 Tkinter 界面，验证控件、配置往返、志愿列表与事件队列
  engine_test.py         引擎控制路径：定时开抢、start/stop 生命周期、已抢到不重复提交
"""
from __future__ import annotations

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def main() -> int:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    # 本脚本自身的输出也要固定成 UTF-8：控制台默认 GBK 时，
    # 带 emoji 的收尾提示会抛 UnicodeEncodeError 把退出码变成 1。
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001  (老版本解释器没有 reconfigure)
        pass
    failed = []
    for script in (
        "mock_server_test.py",
        "search_fallback_test.py",
        "paging_test.py",
        "xkkz_parse_test.py",
        "gui_smoke_test.py",
        "engine_test.py",
    ):
        path = os.path.join(HERE, script)
        print(f"\n{'=' * 72}\n运行 {script}\n{'=' * 72}", flush=True)
        rc = subprocess.call([sys.executable, path], env=env)
        if rc != 0:
            failed.append(script)

    print(f"\n{'=' * 72}", flush=True)
    if failed:
        print(f"[FAIL] 失败的测试：{failed}")
        return 1
    print("[OK] 全部测试通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
