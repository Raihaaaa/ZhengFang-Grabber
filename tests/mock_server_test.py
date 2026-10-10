# -*- coding: utf-8 -*-
"""离线端到端冒烟测试。

用一个本地 HTTP 服务器扮演「新正方教务系统」，实现真实的：
  - login_getPublicKey.html  （真实 RSA 公钥，base64 modulus/exponent）
  - login_slogin.html        （用私钥解密，校验 RSA/ECB/PKCS1 密码）
  - zzxkyzb_cxZzxkYzbIndex.html （含 queryCourse 与 firstXkkzId，用于解析 xkkz_id）
  - zzxkyzb_cxZzxkYzbPartDisplay.html （搜索，返回 tmpList）
  - zzxkyzbjk_cxJxbWithKchZzxkYzb.html （取教学班，返回 do_jxb_id + 余量）
  - zzxkyzb_xkBcZzxkYzb.html （提交，返回 flag）

然后让真实的 JwglxtClient / GrabEngine 打这套接口，验证全链路。
不访问任何真实教务系统。
"""
from __future__ import annotations

import base64
import json
import os
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pkcs1 import (  # noqa: E402
    b64_to_int, decode_public_key, encode_public_key,
    pkcs1_v15_decrypt, rsa_encrypt_password,
)


# --------------------------------------------------------------------------- #
# 测试用的纯 Python RSA 密钥生成（不依赖任何原生扩展）
# --------------------------------------------------------------------------- #
def is_probable_prime(n: int, rounds: int = 32) -> bool:
    if n < 2:
        return False
    for p in (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37):
        if n % p == 0:
            return n == p
    d, s = n - 1, 0
    while d % 2 == 0:
        d //= 2
        s += 1
    for _ in range(rounds):
        a = secrets.randbelow(n - 3) + 2
        x = pow(a, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(s - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def gen_prime(bits: int) -> int:
    while True:
        candidate = secrets.randbits(bits) | (1 << (bits - 1)) | 1
        if is_probable_prime(candidate):
            return candidate


def generate_keypair(bits: int = 1024):
    e = 65537
    while True:
        p, q = gen_prime(bits // 2), gen_prime(bits // 2)
        if p == q:
            continue
        n = p * q
        if n.bit_length() != bits:
            continue
        phi = (p - 1) * (q - 1)
        if phi % e == 0:
            continue
        d = pow(e, -1, phi)
        return n, e, d


N, E, D = generate_keypair(1024)
MODULUS_B64 = base64.b64encode(N.to_bytes((N.bit_length() + 7) // 8, "big")).decode()
EXPONENT_B64 = base64.b64encode(E.to_bytes(3, "big")).decode()  # AQAB

CSRF = "test-csrf-token-123456"
USERNAME = "2023195103"
PASSWORD = "TestPass123"
SU = USERNAME

# 统计
STATS = {"login": 0, "search": 0, "jxb": 0, "submit": 0}
# 可变状态：首次提交返回「人数已满」，第二次返回成功，用于验证重试逻辑
STATE = {"submit_calls": 0, "fail_first_n": 1}

INDEX_HTML = """<html><body>
<div id="main"></div>
<script>
  var su = "%s";
  function queryCourse(el, kklxdm, xkkzId, year, term) {}
</script>
<a onclick="queryCourse(this,'10','1a2b3c4d5e6f708192a3b4c5d6e7f809','2025','12')">公共基础课</a>
<input type="hidden" name="firstXkkzId" id="firstXkkzId" value="1a2b3c4d5e6f708192a3b4c5d6e7f809"></input>
<input type="hidden" name="njdm_id" value="2023">
<input type="hidden" name="zyh_id" value="1951">
<input type="hidden" name="xkxnm" value="2025">
<input type="hidden" name="xkxqm" value="12">
</body></html>""" % SU

LOGIN_HTML = f"""<html><body>
<form method="post" action="/jwglxt/xtgl/login_slogin.html">
  <input type="hidden" name="csrftoken" value="{CSRF}">
</form></body></html>"""

# 教学内容：课程号 -> 教学班列表
CLASSES = [
    {
        "jxb": "0F1E2D3C4B5A69788796A5B4C3D2E1F0",
        "do_jxb_id": "0F1E2D3C4B5A69788796A5B4C3D2E1F0",
        "kcmc": "人工智能导论",
        "kch": "2023011234",
        "xf": "2.0",
        "jsxm": "张老师",
        "jxbrs": "60",
        "yxzrs": "60",        # 满员 -> 触发“人数已满”
        "sksj": "周一 1-2节",
    },
    {
        "jxb": "A1B2C3D4E5F60718293A4B5C6D7E8F90",
        "do_jxb_id": "A1B2C3D4E5F60718293A4B5C6D7E8F90",
        "kcmc": "人工智能导论",
        "kch": "2023011234",
        "xf": "2.0",
        "jsxm": "李老师",
        "jxbrs": "60",
        "yxzrs": "58",        # 有 2 个余量
        "sksj": "周三 3-4节",
    },
]

SEARCH_ROWS = [
    {
        "kch": "2023011234", "jxb": c["jxb"], "kcmc": c["kcmc"], "xf": c["xf"],
        "jsxm": c["jsxm"], "jxbrs": c["jxbrs"], "yxzrs": c["yxzrs"],
        "sksj": c["sksj"], "njdm_id": "2023", "zyh_id": "1951",
    }
    for c in CLASSES
]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # 静音
        pass

    # ------------------------------------------------------------------ #
    def _send(self, body: str | bytes, status: int = 200, headers=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, status=200):
        self._send(json.dumps(obj, ensure_ascii=False), status,
                   {"Content-Type": "application/json; charset=utf-8"})

    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path
        if path.endswith("login_slogin.html"):
            self._send(LOGIN_HTML)
        elif path.endswith("login_getPublicKey.html"):
            self._json({"modulus": MODULUS_B64, "exponent": EXPONENT_B64, "time": "x"})
        elif "index_initMenu" in path:
            self._send(INDEX_HTML)
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
            yhm = form.get("yhm", "")
            mm = form.get("mm", "")
            csrf = form.get("csrf_token") or form.get("csrftoken") or ""
            if csrf != CSRF:
                self._send("csrf error", 200)
                return
            if yhm != USERNAME:
                self._send("bad user", 200)
                return
            try:
                cipher = base64.b64decode(mm)
                plain = pkcs1_v15_decrypt(N, D, cipher)
            except Exception:
                plain = None
            if plain != PASSWORD.encode("utf-8"):
                # 关键：验证 RSA 加密是否真的能被服务端私钥解开
                self._send("bad password (RSA decrypt mismatch)", 200)
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
            rows = [r for r in SEARCH_ROWS if not keyword or keyword in r["kcmc"] or keyword in r["kch"]]
            self._json({"tmpList": rows, "totalResult": len(rows)})
            return

        if "cxJxbWithKchZzxkYzb" in path:
            STATS["jxb"] += 1
            kch = form.get("kch_id", "")
            rows = [dict(c) for c in CLASSES if c["kch"] == kch]
            self._json(rows)
            return

        if "zzxkyzbjk_xkBcZyZzxkYzb" in path:
            # ★ 真实端点（自主选课）：`zzxkyzbjk_`，不是 `zzxkyzb_`。
            # 并且权限网关认查询串里的 gnmkdm —— 实测某高校：同一份提交体，
            # 不带 gnmkdm 回 {"flag":"0","msg":"无操作权限！"}（先判权限再判参数），
            # 带上就正常进业务逻辑。这里如实复刻，缺 gnmkdm 直接拒绝。
            STATS["submit"] += 1
            STATE["submit_calls"] += 1
            if "gnmkdm=" not in self.path:
                self._json({"flag": "0", "msg": "无操作权限！"})
                return
            jxb_ids = form.get("jxb_ids", "")
            kcmc = form.get("kcmc", "")
            # 校验必需参数都在
            required = ["kch_id", "jxb_ids", "xkkz_id", "xkxnm", "xkxqm", "njdm_id", "zyh_id"]
            missing = [k for k in required if not form.get(k)]
            if missing:
                self._json({"flag": "0", "msg": f"缺少参数 {missing}"})
                return
            if STATE["submit_calls"] <= STATE["fail_first_n"]:
                self._json({"flag": "-1", "msg": "60,60,60"})
                return
            self._json({"flag": "1", "msg": ""})
            return

        if "xkBcZzxkYzb" in path:
            # 旧路径（已废弃的处理器）：任何参数都只会回「未知异常」。
            self._json({"flag": "0", "msg": "出现未知异常，请与管理员联系！"})
            return

        self._send("not found", 404)


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    from jwglxt import JwglxtClient
    from engine import Course, GrabEngine, Wishlist
    import config as config_mod

    base = f"http://127.0.0.1:{port}"
    logs = []
    # 测试产物写在 tests/ 下再清理：系统临时目录在某些受限沙箱里不可写
    tmp_dir = os.path.dirname(os.path.abspath(__file__))

    class MockConfig(dict):
        pass

    cfg = dict(config_mod.DEFAULT_CONFIG)
    cfg.update({
        "base_url": base,
        "username": USERNAME,
        "password": PASSWORD,
        "njdm_id": "2023",
        "zyh_id": "1951",
        "bh_id": "23195103",
        "xkxnm": "2025",
        "xkxqm": "12",
        "poll_interval": 0.4,
        "monitor_interval": 0,
        "request_timeout": 5,
        "max_retries": 1,
        "max_attempts_per_round": 5,
        "notify_desktop": False,
        "notify_email": False,
        "notify_webhook": False,
        "stop_when_all_done": True,
        "submit_even_if_full": False,
    })

    failures = []

    def check(name, cond, detail=""):
        tag = "PASS" if cond else "FAIL"
        print(f"[{tag}] {name}" + (f"  -- {detail}" if detail else ""))
        if not cond:
            failures.append(name)

    print("=" * 72)
    print("离线端到端冒烟测试（本地模拟正方系统，不联网）")
    print("=" * 72)

    # ---- 1. 纯 Python RSA / PKCS#1 v1.5 ----
    check("b64_to_int 解析 RSA exponent=AQAB 得 65537",
          b64_to_int(EXPONENT_B64) == 65537, f"got {b64_to_int(EXPONENT_B64)}")
    check("b64_to_int 还原 modulus 与原始 n 一致", b64_to_int(MODULUS_B64) == N)

    der = encode_public_key(N, E)
    dec_n, dec_e = decode_public_key(der)
    check("DER 编解码往返一致（SubjectPublicKeyInfo）",
          (dec_n, dec_e) == (N, E), f"e={dec_e}")

    PADDING_OK = True
    for _ in range(30):  # 多次加密，确认随机填充每次都不同且都能解开
        ct = base64.b64decode(rsa_encrypt_password(PASSWORD, MODULUS_B64, EXPONENT_B64))
        if pkcs1_v15_decrypt(N, D, ct) != PASSWORD.encode("utf-8"):
            PADDING_OK = False
            break
    check("RSA 加密→私钥解密往返 30 次全部一致（随机填充正确）", PADDING_OK,
          "PKCS#1 v1.5 填充符合规范")

    ct1 = rsa_encrypt_password(PASSWORD, MODULUS_B64, EXPONENT_B64)
    ct2 = rsa_encrypt_password(PASSWORD, MODULUS_B64, EXPONENT_B64)
    check("相同密码两次加密结果不同（填充随机化）", ct1 != ct2)

    # 与 pycryptodome（若可用）交叉验证，确认和官方 login.js 语义一致
    try:
        from Crypto.PublicKey import RSA as _RSA  # type: ignore
        from Crypto.Cipher import PKCS1_v1_5 as _PK  # type: ignore
        _key = _RSA.construct((N, E))
        _ref = _PK.new(_key).encrypt(PASSWORD.encode("utf-8"))
        check("与 pycryptodome PKCS1_v1_5 交叉验证：自测私钥能解开 pycryptodome 密文",
              pkcs1_v15_decrypt(N, D, _ref) == PASSWORD.encode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"[SKIP] pycryptodome 交叉验证不可用（{type(exc).__name__}: {exc}）")

    # ---- 2. 登录 ----
    client = JwglxtClient(cfg, logger=lambda m, l="info": logs.append((l, m)))
    try:
        client.login()
        check("完整登录流程（csrf + RSA 密码）", True, f"su={client.su}")
    except Exception as exc:
        check("完整登录流程（csrf + RSA 密码）", False, str(exc))
        server.shutdown()
        return 1

    check("服务器收到登录请求并成功用私钥解密密码", STATS["login"] == 1)

    # ---- 3. xkkz_id 解析 ----
    html = client.fetch_xsxk_page()
    check("解析 xkkz_id（onclick queryCourse）",
          client.xkkz_map.get("10") == "1a2b3c4d5e6f708192a3b4c5d6e7f809",
          f"map={client.xkkz_map}")

    # ---- 4. 搜索 ----
    rows = client.search_courses("人工智能")
    check("搜索课程返回结果", len(rows) == 2, f"got {len(rows)} rows")
    check("搜索结果归一化字段正确",
          rows and rows[0]["kch"] == "2023011234" and rows[0]["teacher"] == "张老师",
          f"first={rows[0] if rows else None}")

    # ---- 5. 取教学班（余量）----
    classes = client.get_jxb_ids("2023011234", client.xkkz_map["10"], "人工智能导论", "10")
    check("取教学班拿到 do_jxb_id", len(classes) == 2 and bool(classes[0]["do_jxb_id"]))
    capacities = sorted(c["capacity"] - c["enrolled"] for c in classes)
    check("余量计算正确（一门满、一门余2）", capacities == [0, 2], f"seats={capacities}")

    # ---- 6. 直接提交（验证满员返回 -1）----
    full = next(c for c in classes if c["capacity"] - c["enrolled"] == 0)
    r = client.submit("2023011234", full["do_jxb_id"], client.xkkz_map["10"],
                      client.build_kcmc("2023011234", "人工智能导论", "2.0"), "10")
    check("满员提交返回 flag=-1（人数已满）", r["flag"] == "-1", f"resp={r}")

    # ---- 7. 抢课引擎全流程：应跳过满员班、抢到有余量的班 ----
    STATE["submit_calls"] = 0
    STATE["fail_first_n"] = 1  # 第一次提交模拟失败，验证重试

    wl = Wishlist(path=os.path.join(tmp_dir, "wishlist_test.json"))
    wl.courses = [Course(
        course_id="2023011234::A1B2C3D4E5F60718293A4B5C6D7E8F90",
        kch="2023011234", kcmc="人工智能导论", teacher="李老师", xf="2.0",
        jxb="A1B2C3D4E5F60718293A4B5C6D7E8F90", xkkz_id="", kklxdm="10",
    )]

    engine_logs = []
    engine = GrabEngine(
        cfg, wl,
        log_cb=lambda m, l="info": engine_logs.append((l, m)),
        status_cb=lambda s: engine_logs.append(("status", s)),
        event_cb=lambda k: None,
    )
    engine._thread = threading.Thread(target=engine._run, daemon=True)
    engine._stop.clear()
    engine._thread.start()
    engine.join(timeout=30)

    course = wl.courses[0]
    check("引擎自动补齐 xkkz_id", course.xkkz_id == "1a2b3c4d5e6f708192a3b4c5d6e7f809",
          f"xkkz_id={course.xkkz_id}")
    check("引擎读取到正确余量", course.seats_left == 2,
          f"{course.enrolled}/{course.capacity}")
    check("首次提交失败后自动重试并最终抢到", course.grabbed,
          f"attempts={course.attempts}, msg={course.last_msg}")
    check("抢到后引擎自动停止", engine.stats["success"] == 1,
          f"stats={engine.stats}")

    # ---- 8. 会话失效重连路径 ----
    client.logged_in = False
    try:
        client.search_courses("人工智能")
        check("会话失效后自动重新登录", STATS["login"] >= 2, f"login={STATS['login']}")
    except Exception as exc:
        check("会话失效后自动重新登录", False, str(exc))

    # ---- 9. 异常响应处理 ----
    try:
        client._post_json("/jwglxt/xsxk/nonexistent-endpoint.html", {})
        check("非法响应抛 JwglxtError", False, "没有抛异常")
    except Exception as exc:
        from jwglxt import JwglxtError
        check("非法响应抛 JwglxtError", isinstance(exc, JwglxtError), type(exc).__name__)

    server.shutdown()

    # 清理测试产物
    artifact = os.path.join(tmp_dir, "wishlist_test.json")
    if os.path.exists(artifact):
        os.remove(artifact)

    print("-" * 72)
    print(f"接口调用统计：{STATS}")
    print("引擎日志摘录：")
    for level, msg in engine_logs[-8:]:
        print(f"   [{level}] {msg}")
    print("-" * 72)
    if not failures:
        print("✅ 全部通过")
        return 0
    print(f"❌ 失败 {len(failures)} 项：{failures}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
