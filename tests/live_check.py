# -*- coding: utf-8 -*-
"""真实环境体检脚本（会连接 config.json 里填写的教务系统，请在校内网运行）。

用法：
    python tests/live_check.py          # 只查，不动课表

流程：登录 → 打开选课页 → 搜索课程 → 取前几条的教学班与余量。
**不会提交选课**，所以不会改动你的课表。

排查「搜不到课程」时看三处输出：
    1. 选课页下发的参数（学期 / 专业 / 年级 / 轮次）
    2. 自动学到的坏字段（会让服务端回 HTML 错误页的页面字段）
    3. 每次尝试的记录 client.search_trace
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from config import load_config          # noqa: E402
from jwglxt import JwglxtClient, JwglxtError   # noqa: E402

SHOW_CLASSES = 3


def main() -> int:
    path = os.path.join(ROOT, "config.json")
    cfg = load_config(path)
    print(f"教务系统：{cfg.get('base_url')}    课程类型 kklxdm={cfg.get('kklxdm')}")
    print(f"已记住的坏字段：{cfg.get('bad_keys')}")

    def log(msg, level="info"):
        if level in ("warn", "error") or "已忽略" in msg:
            print(f"  [{level}] {msg}")

    client = JwglxtClient(cfg, logger=log, config_path=path)
    client.login()
    print(f"登录成功，学号 {client.su}")

    client.fetch_xsxk_page()
    kklxdm = str(cfg.get("kklxdm") or "10")
    keyword = str(cfg.get("filter_list") or "") if isinstance(cfg.get("filter_list"), str) else ""
    xkkz_id = client.get_xkkz_id(kklxdm)
    print(f"选课轮次 xkkz_id={xkkz_id}    课程类型={kklxdm}    关键字={keyword!r}")

    # pages=0 = 一直翻到没有为止（服务端每页只回一个窗口，见 README 协议第 7 节）
    rows = client.search_courses(keyword, kklxdm, pages=0)
    print(f"搜索到 {len(rows)} 条课程")
    for line in client.search_trace[-6:]:
        print("   trace:", line)

    for course in rows[:SHOW_CLASSES]:
        print(f"-- {course.get('kcmc')}（{course.get('kch')}）学分 {course.get('xf')}")
        try:
            classes = client.get_jxb_ids(
                course.get("kch", ""), xkkz_id, course.get("kcmc", ""), kklxdm,
                kch_id=course.get("kch_id", ""),
            )
        except JwglxtError as exc:
            print(f"     取教学班失败：{exc}")
            continue
        print(f"     教学班 {len(classes)} 个")
        for jxb in classes[:3]:
            cap, enrolled = jxb.get("capacity", -1), jxb.get("enrolled", -1)
            left = "未知" if cap < 0 else str(max(cap - enrolled, 0))
            print(f"       {jxb.get('jxb')}  {jxb.get('teacher') or '-'}  "
                  f"上课时间 {jxb.get('time_place') or '-'}  余量 {left}/{cap}")
    return 0 if rows else 1


if __name__ == "__main__":
    raise SystemExit(main())
