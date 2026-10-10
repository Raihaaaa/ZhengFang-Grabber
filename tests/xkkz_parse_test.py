# -*- coding: utf-8 -*-
"""xkkz_id 解析与兜底测试（守护某高校这类「首页不下发轮次标识」的学校）。

真实背景：用户 www 反馈搜索时报「未取到 xkkz_id（选课轮次标识）」。
该学校的选课首页不带这个值，需要去其它选课页面找；找不到时必须给出
可操作的指引，而不是一句「请检查配置」。

    python tests/xkkz_parse_test.py
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from gui import GrabberApp  # noqa: E402
from jwglxt import JwglxtClient, JwglxtError  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"[PASS] {name}" + (f"  -- {extra}" if extra else ""))
    else:
        FAIL += 1
        print(f"[FAIL] {name}" + (f"  -- {extra}" if extra else ""))


# --------------------------------------------------------------------------- #
print("=" * 72)
print("xkkz_id 解析")
print("=" * 72)

# 1. 经典 onclick
html_onclick = """
<a href="#" onclick="queryCourse(this,'10','14564675131412412421','2020','2021')">必修</a>
<a href="#" onclick="queryCourse(this,'16','14564675131412412422','2020','2021')">选修</a>
"""
m = JwglxtClient.parse_xkkz_ids(html_onclick)
check("onclick 形态解析出两种课程类型", m.get("10") and m.get("16"), f"{m}")
check("onclick 的值正确", m["10"] == "14564675131412412421", m["10"])

# 2. 隐藏域（name 里带类型号）
html_hidden = '<input type="hidden" name="firstXkkzId10" id="firstXkkzId10" value="14564675131412412499">'
m = JwglxtClient.parse_xkkz_ids(html_hidden)
check("隐藏域 firstXkkzId10 解析成功", m.get("10") == "14564675131412412499", f"{m}")

# 3. JS 变量赋值：没有课程类型，应落到通配键
html_var = "var xkkz_id = '14564675131412412488';"
m = JwglxtClient.parse_xkkz_ids(html_var)
check("JS 变量形态落成通用值 '*'", m.get("*") == "14564675131412412488", f"{m}")

# 4. URL 查询串：kklxdm 与 xkkz_id 成对
html_url = ('"/jwglxt/xsxk/zzxkyzb_cxZzxkYzbPartDisplay.html?gnmkdm=N253512'
            '&kklxdm=10&xkkz_id=14564675131412412477"')
m = JwglxtClient.parse_xkkz_ids(html_url)
check("URL 查询串解析出 kklxdm->xkkz_id", m.get("10") == "14564675131412412477", f"{m}")

# 5. 假配对排除：kklxdm 的值不能被当成 xkkz_id
html_decoy = "kklxdm=10&xkkz_id=abc"
m = JwglxtClient.parse_xkkz_ids(html_decoy)
check("不会把 kklxdm 的 10 错当成 xkkz_id", m.get("10") != "10", f"{m}")

# 6. 无 xkkz_id 的页面
check("无关页面解析出空映射", JwglxtClient.parse_xkkz_ids("<html>hello</html>") == {})

# 7. Windows 2019 版另一种写法：dict 字面量
html_dict = "{'kklxdm': '10', 'xkkz_id': '14564675131412412466'}"
m = JwglxtClient.parse_xkkz_ids(html_dict)
check("python dict 形态解析成功", m.get("10") == "14564675131412412466", f"{m}")

# --------------------------------------------------------------------------- #
print()
print("=" * 72)
print("get_xkkz_id 取值优先级")
print("=" * 72)

cfg = {"base_url": "https://x.example", "username": "2023001", "password": "p"}
c = JwglxtClient(cfg)

c.xkkz_map = {}
try:
    c.get_xkkz_id("10")
    check("无任何轮次标识时抛 JwglxtError", False, "居然没抛异常")
except JwglxtError as exc:
    msg = str(exc)
    check("无任何轮次标识时抛 JwglxtError", True)
    check("错误信息给出 F12/Network 的具体位置", "cxZzxkYzbPartDisplay" in msg, msg[:60])
    check("错误信息给出 xkkz_id 关键词", "xkkz_id" in msg)

c.xkkz_map = {"*": "通用值123456789"}
check("通用 '*' 能被任意课程类型取用", c.get_xkkz_id("10") == "通用值123456789")

c.xkkz_map = {"16": "选修专用123456"}
check("缺少该类型时回退到任意已有值（各校常共用一个轮次）",
      c.get_xkkz_id("10") == "选修专用123456")

c.cfg["xkkz_id_10"] = "手工填写999"
c.xkkz_map = {"10": "页面解析111"}
check("配置里的手工值优先于页面解析", c.get_xkkz_id("10") == "手工填写999")

# --------------------------------------------------------------------------- #
print()
print("=" * 72)
print("界面上从剪贴板提取 xkkz_id")
print("=" * 72)

parse = GrabberApp._parse_xkkz_from_text

check("整行就是一个 id",
      parse("14564675131412412421") == "14564675131412412421")
check("URL 形态",
      parse("https://jwglxt.example.edu.cn/jwglxt/xsxk/zzxkyzb_cxZzxkYzbPartDisplay.html"
            "?gnmkdm=N253512&xkkz_id=14564675131412412421&kklxdm=10")
      == "14564675131412412421")
check("F12 表单文本形态",
      parse("kklxdm=10\nxkkz_id=14564675131412412421\nnjdm_id=2023")
      == "14564675131412412421")
check("HTML input 片段（name 在前）",
      parse('<input type="hidden" name="xkkz_id" value="14564675131412412421"/>')
      == "14564675131412412421")
check("HTML input 片段（value 在前）",
      parse('<input value="14564675131412412421" name="xkkz_id"/>')
      == "14564675131412412421")
check("JS 赋值形态",
      parse("var xkkz_id = '14564675131412412421';") == "14564675131412412421")
check("空文本返回空串", parse("") == "")
check("无关文本返回空串", parse("hello world") == "")
check("过短的串不被误认成 id", parse("12") == "")

# --------------------------------------------------------------------------- #
print()
print("=" * 72)
if FAIL:
    print(f"[FAIL] {FAIL} 项失败，{PASS} 项通过")
    sys.exit(1)
print(f"[OK] xkkz_id 解析与兜底测试全部通过（{PASS} 项）")
sys.exit(0)
