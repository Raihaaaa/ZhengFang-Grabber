"""搜索兜底能力测试：模拟「必须先点查询才出课程」的学校。

背景（用户反馈）：某些学校的选课页打开后课程列表是空的，必须先点一次
「查询」按钮，课程才出现。真实原因通常在服务端：`cxZzxkYzbPartDisplay`
在参数不完整时只回一个空 JSON 对象 `{}`，而界面把「0 条」当成了结论。

本测试用假服务器精确复现这种行为，验证 JwglxtClient.search_courses 的
候选矩阵（关键字→空关键字→换入口→去 kklxdm）确实能兜底搜出课程，
并且失败时会把每一次尝试记录到 search_trace 供界面展示。

运行：python tests/search_fallback_test.py
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

from jwglxt import JwglxtClient, JwglxtError  # noqa: E402
from pkcs1 import b64_to_int, rsa_encrypt_password  # noqa: E402

# ---------------------------------------------------------------------- #
# RSA 密钥：直接复用 mock_server_test 的密钥生成（1024 位）
# ---------------------------------------------------------------------- #
from mock_server_test import (  # noqa: E402
    EXPONENT_B64,
    MODULUS_B64,
    N,
    D,
)

USERNAME = "2023195103"
PASSWORD = "TestPass123"
CSRF = "mock-csrf-token"
XKKZ = "1a2b3c4d5e6f708192a3b4c5d6e7f809"

STATS = {"login": 0, "search": 0}

INDEX_HTML = """<html><body>
<a onclick="queryCourse(this,'10','%s','2025','12')">公共基础课</a>
</body></html>""" % XKKZ

LOGIN_HTML = f"""<html><body>
<form method="post" action="/jwglxt/xtgl/login_slogin.html">
  <input type="hidden" name="csrftoken" value="{CSRF}">
</form></body></html>"""

COURSES = [
    {"kch": "2023011234", "jxb": "AAAABBBBCCCCDDDD1111", "kcmc": "人工智能导论",
     "xf": "2.0", "jsxm": "张老师", "jxbrs": "60", "yxzrs": "58",
     "njdm_id": "2023", "zyh_id": "1951"},
    {"kch": "2023015678", "jxb": "EEEEFFFFGGGGHHHH2222", "kcmc": "大学物理",
     "xf": "3.0", "jsxm": "王老师", "jxbrs": "80", "yxzrs": "80",
     "njdm_id": "2023", "zyh_id": "1951"},
]


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
        elif path.endswith("index_initMenu.html") or "initMenu" in path:
            self._send("<html><body>menu</body></html>")
        elif "cxZzxkYzbIndex" in path:
            self._send(INDEX_HTML)
        elif "cxZzxkYzbChoosed" in path:
            self._send("<html><body>已选课程列表</body></html>")
        else:
            self._send("not found", 404)

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8")
        form = {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}

        if path.endswith("login_slogin.html"):
            STATS["login"] += 1
            if form.get("csrf_token") != CSRF or form.get("yhm") != USERNAME:
                self._send("csrf/user error")
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
            STATS["search"] += 1
            keyword = form.get("filter_list[0]", "")
            # ---- 复刻「必须先点查询」的学校行为 ----
            # 服务端只认带 xkkz_id 的请求；缺了它一律只回空对象 {}，
            # 界面上的表现就是「打开页面没有课程，必须点一次查询」。
            has_xkkz = bool(form.get("xkkz_id"))
            if not has_xkkz:
                self._json({})
                return
            rows = [r for r in COURSES if not keyword or keyword in r["kcmc"] or keyword in r["kch"]]
            self._json({"tmpList": rows, "totalResult": len(rows)})
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
        "njdm_id": "2023",
        "zyh_id": "1951",
        "bh_id": "23195103",
        "xkxnm": "2025",
        "xkxqm": "12",
        "kklxdm_candidates": ["10"],
    }
    logs = []
    client = JwglxtClient(cfg, logger=lambda level, msg="info": logs.append((level, msg)))
    client.login()
    client.fetch_xsxk_page()
    check("登录成功", client.logged_in)
    check("解析出 xkkz_id", client.xkkz_map.get("10") == XKKZ, str(client.xkkz_map))

    # 场景 1：关键字搜索 —— 服务端只认带 xkkz_id 的请求，必须能正常搜到
    client.search_trace = []
    rows = client.search_courses("人工智能", "10")
    check("带关键字搜索能拿到课程", len(rows) == 1,
          f"rows={[r['kcmc'] for r in rows]}")
    check("关键字命中的结果不标记为兜底", client.keyword_fallback_used is False)
    check("失败尝试会写进 search_trace", len(client.search_trace) >= 1,
          f"{len(client.search_trace)} 条")
    check("trace 里有命中标记", any("命中" in t for t in client.search_trace))

    # 场景 2：关键字匹配不到任何课程 —— 会走到第二轮空关键字兜底，
    # 必须明确标记 keyword_fallback_used，避免界面把「全部课程」当成关键字命中
    client.search_trace = []
    rows = client.search_courses("不存在的课程XYZ", "10")
    check("关键字无匹配时由空查询兜底列出课程", len(rows) == 2, f"{len(rows)} 条")
    check("兜底结果被标记 keyword_fallback_used=True",
          client.keyword_fallback_used is True)
    check("trace 记录了第二轮兜底", any("空关键字兜底" in t for t in client.search_trace),
          " / ".join(client.search_trace[:3]))
    check("响应形状被记录下来（便于诊断字段名）",
          "tmpList" in client.last_search_payload_keys
          or "totalResult" in client.last_search_payload_keys,
          str(client.last_search_payload_keys))

    # 场景 3：空关键字搜索可直接拿到全部课程（等价于直接点查询）
    client.search_trace = []
    rows = client.search_courses("", "10")
    check("空关键字搜索可直接拿到全部课程", len(rows) == 2, f"{len(rows)} 条")
    check("空关键字本身不算兜底", client.keyword_fallback_used is False)

    server.shutdown()
    print(f"\n{passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
