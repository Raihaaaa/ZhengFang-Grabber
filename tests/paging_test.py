"""翻页与「提交参数」回归测试。

复刻两个真实环境才暴露出来的缺陷（某高校 2026 年实测）：

1. **只能搜到少量课程** —— 搜索接口每页只回一个窗口，而且响应里根本没有
   总页数字段。旧代码把「第几页」直接当 `kspage` 发、`jspage` 写死成 10，
   于是第 2 页发出的是 `kspage=2&jspage=10`，窗口永远停在最前面，
   用户看到的就是「一门课都翻不出来 / 只能搜到 11 条」。
2. **提交选课恒回 `flag=0`** —— 旧 `submit()` 自己拼了十来个字段，
   `njdm_id`/`zyh_id`/`xkxqm` 全是空串；选课页明明下发了一百多个字段。
   现在改为以搜索请求体为基座，本测试守住「页面下发的身份/学期参数
   必须出现在提交体里」。
3. **提交被权限网关挡回「无操作权限！」** —— 端点对（`zzxkyzbjk_xkBcZyZzxkYzb.html`）
   也带齐了 19 个字段，仍然 `flag=0`：权限网关先判查询串里的 `gnmkdm`，
   没有它一律拒绝（换成假参数也回同一句话，所以那不是业务判定）。
   本测试的 mock 复刻这道闸门，守住「提交必须带 `gnmkdm`」。

运行：python tests/paging_test.py
"""

from __future__ import annotations

import base64
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from jwglxt import JwglxtClient  # noqa: E402
from mock_server_test import (  # noqa: E402
    D,
    EXPONENT_B64,
    MODULUS_B64,
    N,
)

USERNAME = "2023195103"
PASSWORD = "TestPass123"
CSRF = "mock-csrf-token"
XKKZ = "1a2b3c4d5e6f708192a3b4c5d6e7f809"

TOTAL_COURSES = 25
ALL_COURSES = [
    {
        "kch": f"T{i:06d}",
        "kch_id": f"KCHID{i:016X}",
        "jxb_id": f"JXB{i:020d}",
        "kcmc": f"测试课程{i:02d}",
        "xf": "1.5",
        "jxbrl": "60",
        "yxzrs": str(i),
    }
    for i in range(1, TOTAL_COURSES + 1)
]

# 选课页：整整一百多个字段的缩影 —— 关键的身份/学期参数都在这里下发
INDEX_HTML = """<html><body>
<input type="hidden" name="njdm_id" value="2026">
<input type="hidden" name="zyh_id" value="55FC5FB99D5BC395E0635D01020AF870">
<input type="hidden" name="bh_id" value="01202601">
<input type="hidden" name="jg_id" value="01">
<input type="hidden" name="xqh_id" value="1">
<input type="hidden" name="xkxnm" value="2026">
<input type="hidden" name="xkxqm" value="3">
<input type="hidden" name="xslbdm" value="wlb">
<input type="hidden" name="xsbj" value="0">
<input type="hidden" name="pageNumber" value="15">
<input type="hidden" name="localeKey" value="zh_CN">
<a onclick="queryCourse(this,'10','%s','2026','3')">通识选修课</a>
</body></html>""" % XKKZ

LOGIN_HTML = f"""<html><body>
<form method="post" action="/jwglxt/xtgl/login_slogin.html">
  <input type="hidden" name="csrftoken" value="{CSRF}">
</form></body></html>"""

SEEN = {"windows": [], "submit_bodies": [], "submit_paths": [], "search_forms": []}


def pkcs1_v15_decrypt(n: int, d: int, cipher: bytes) -> bytes:
    k = (n.bit_length() + 7) // 8
    if len(cipher) != k:
        raise ValueError("bad length")
    m = pow(int.from_bytes(cipher, "big"), d, n).to_bytes(k, "big")
    if m[0] != 0 or m[1] != 2:
        raise ValueError("bad padding")
    idx = m.index(b"\x00", 2)
    return m[idx + 1:]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _send(self, body, status=200, headers=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path
        if path.endswith("login_slogin.html"):
            self._send(LOGIN_HTML)
        elif path.endswith("login_getPublicKey.html"):
            self._json({"modulus": MODULUS_B64, "exponent": EXPONENT_B64})
        elif "cxZzxkYzbIndex" in path:
            self._send(INDEX_HTML)
        elif "cxZzxkYzbChoosed" in path:
            self._send("<html><body>已选课程列表</body></html>")
        elif "initMenu" in path:
            self._send("<html><body>menu</body></html>")
        else:
            self._send("not found", 404)

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8")
        form = {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}

        if path.endswith("login_slogin.html"):
            if form.get("csrf_token") != CSRF:
                self._send("csrf error")
                return
            try:
                plain = pkcs1_v15_decrypt(N, D, base64.b64decode(form.get("mm", "")))
            except Exception:
                plain = None
            if plain != PASSWORD.encode("utf-8"):
                self._send("bad password")
                return
            self.send_response(302)
            self.send_header("Location", "/jwglxt/xtgl/index_initMenu.html")
            self.send_header("Set-Cookie", "JSESSIONID=mock-ok; Path=/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if "cxZzxkYzbPartDisplay" in path:
            try:
                ks = int(form.get("kspage") or 1)
                js = int(form.get("jspage") or 10)
            except ValueError:
                ks, js = 1, 10
            SEEN["windows"].append((ks, js))
            SEEN["search_forms"].append(form)
            # 服务端窗口语义：返回 [ks, js] 之间的行，并且每页多回一行
            # （实测某高校 kspage=1&jspage=10 会回 11 条），相邻窗口因此重叠。
            rows = ALL_COURSES[max(0, ks - 1): js + 1]
            self._json({"tmpList": rows, "sfxsjc": "1"})
            return

        if "cxJxbWithKch" in path:
            kch_id = form.get("kch_id") or ""
            hit = [c for c in ALL_COURSES if c["kch_id"] == kch_id]
            if not hit:
                self._json([])
                return
            c = hit[0]
            self._json([{
                "do_jxb_id": f"{c['jxb_id']}DO",
                "jxb_id": c["jxb_id"],
                "jxbrl": c["jxbrl"],
                "yxzrs": c["yxzrs"],
                "kch": c["kch"],
                "kcmc": c["kcmc"],
                "jxbzls": "30",
                "sksj": "星期一第1-2节",
            }])
            return

        if "BcZyZzxkYzb" in path or "BcZzxkYzb" in path:
            # 复刻真实权限网关：没有 gnmkdm 查询串就一律拒绝（先判权限，再判参数）。
            # 注意 path 是 urlparse(...).path，**不含查询串**，所以这里看 self.path。
            if "gnmkdm=" not in self.path:
                self._json({"flag": "0", "msg": "无操作权限！"})
                return
            SEEN["submit_bodies"].append(form)
            SEEN["submit_paths"].append(self.path)
            self._json({"flag": "1", "msg": "选课成功"})
            return

        self._send("not found", 404)


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    passed = failed = 0

    def check(name, cond, detail=""):
        nonlocal passed, failed
        if cond:
            passed += 1
            print(f"[PASS] {name}" + (f" -- {detail}" if detail else ""))
        else:
            failed += 1
            print(f"[FAIL] {name}" + (f" -- {detail}" if detail else ""))

    cfg = {
        "base_url": base,
        "username": USERNAME,
        "password": PASSWORD,
        "request_timeout": 5,
        "save_debug_html": False,
        # 关键：配置里已经存过轮次标识（真实用户就是这种状态）。
        # 旧代码遇到它就直接返回、再也不打开选课页，于是页面下发的
        # njdm_id/zyh_id/xkxqm 全丢，搜索只能回空列表。
        "xkkz_id_auto": XKKZ,
        "kklxdm": "10",
        "kklxdm_candidates": ["10"],
        # 这两个是内置默认里的**错误值**，必须被选课页下发的真值压掉
        "xslbdm": "421",
        "xsbj": "4294967296",
    }

    client = JwglxtClient(cfg, logger=lambda msg, level="info": None)
    client.login()

    # ---- 1) 配置里有 xkkz_id 时也必须补齐「选课页上下文」 ----
    client.search_courses("", "10")
    check("配置里已有 xkkz_id 时仍会打开选课页补页面参数",
          bool(client._page_params), f"{len(client._page_params)} 个字段")
    check("页面下发的 njdm_id 覆盖了配置缺省",
          client.effective_param("njdm_id", "") == "2026",
          repr(client.effective_param("njdm_id", "")))
    check("页面下发的 xsbdm/xsbj 压过内置错误默认值",
          client.effective_param("xslbdm", "") == "wlb"
          and client.effective_param("xsbj", "") == "0",
          f"xslbdm={client.effective_param('xslbdm', '')!r} "
          f"xsbj={client.effective_param('xsbj', '')!r}")
    check("页面级渲染字段不进请求体",
          "localeKey" not in SEEN["search_forms"][0]
          and "cdTsxx" not in SEEN["search_forms"][0],
          f"localeKey={SEEN['search_forms'][0].get('localeKey')!r}")

    # ---- 2) 翻页：必须把 25 条课程全部翻出来 ----
    SEEN["windows"].clear()
    rows = client.search_courses("", "10")
    kchs = [r["kch"] for r in rows]
    check("翻页把全部课程都搜出来了", len(rows) == TOTAL_COURSES,
          f"{len(rows)} 条（预期 {TOTAL_COURSES}）")
    check("结果没有重复课程", len(set(kchs)) == len(kchs), f"{len(set(kchs))}/{len(kchs)}")
    check("窗口整段平移（kspage/jspage 成对前进）",
          SEEN["windows"] == [(1, 10), (11, 20), (21, 30), (31, 40)],
          str(SEEN["windows"]))
    check("最后一页为空时正常收尾",
          any("翻页结束" in t for t in client.search_trace),
          " / ".join(t for t in client.search_trace if "页" in t)[:120])

    # ---- 3) 取教学班仍然用 kch_id ----
    first = rows[0]
    jxbs = client.get_jxb_ids(first["kch"], first["xkkz_id"], "", first["kklxdm"],
                              kch_id=first["kch_id"])
    check("取教学班能拿到 do_jxb_id", len(jxbs) == 1 and jxbs[0]["do_jxb_id"].endswith("DO"),
          str(jxbs[:1]))

    # ---- 4) 提交体必须复刻浏览器的 19 个字段，且带 gnmkdm 权限查询串 ----
    SEEN["submit_bodies"].clear()
    SEEN["submit_paths"].clear()
    target = jxbs[0]
    kcmc = client.build_kcmc(first["kch"], first["kcmc"], first["xf"])
    result = client.submit(first["kch_id"], target["do_jxb_id"], first["xkkz_id"], kcmc,
                           first["kklxdm"], kch=first["kch"], jxbzls=target.get("jxbzls", ""))
    sent = SEEN["submit_bodies"][0] if SEEN["submit_bodies"] else {}
    submitted = SEEN["submit_paths"][0] if SEEN["submit_paths"] else ""
    check("提交成功返回值被解析", result.get("flag") == "1", str(result))
    check("只提交了一次（首选请求就带上了 gnmkdm，无需重试）",
          len(SEEN["submit_bodies"]) == 1, f"实际 {len(SEEN['submit_bodies'])} 次")
    check("提交请求带 gnmkdm 与 su 查询串（否则权限网关回「无操作权限！」）",
          "gnmkdm=N253512" in submitted and f"su={USERNAME}" in submitted, submitted)
    for key, expect in (("njdm_id", "2026"), ("zyh_id", "55FC5FB99D5BC395E0635D01020AF870"),
                        ("xkxnm", "2026"), ("xkxqm", "3"),
                        ("xkkz_id", XKKZ), ("kch_id", first["kch_id"]),
                        ("jxb_ids", target["do_jxb_id"]), ("qz", "0")):
        check(f"提交体带上 {key}={expect[:18]}", sent.get(key) == expect,
              f"实际 {sent.get(key)!r}")
    check("提交体恰好是浏览器发的那 19 个字段",
          set(sent) == set(JwglxtClient.SUBMIT_FIELDS),
          f"多={sorted(set(sent) - set(JwglxtClient.SUBMIT_FIELDS))} "
          f"少={sorted(set(JwglxtClient.SUBMIT_FIELDS) - set(sent))}")
    check("提交体没有被塞进会让服务端报错的页面字段",
          "pageNumber" not in sent and "filter_list[0]" not in sent,
          f"pageNumber={sent.get('pageNumber')!r}")

    server.shutdown()
    print(f"\n{passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
