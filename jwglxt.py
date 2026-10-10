# -*- coding: utf-8 -*-
"""新正方教务系统（jwglxt）客户端。

覆盖完整链路：
    1. 取登录页 -> 解析 csrf_token
    2. GET  /jwglxt/xtgl/login_getPublicKey.html -> {modulus, exponent}(base64)
    3. RSA/ECB/PKCS1 加密密码 -> base64，POST /jwglxt/xtgl/login_slogin.html
    4. 选课页 -> 解析 xkkz_id（各课程类型）
    5. POST zzxkyzb_cxZzxkYzbPartDisplay.html 搜索课程
    6. POST zzxkyzbjk_cxJxbWithKchZzxkYzb.html 取 do_jxb_id（教师/容量/时间也在这里）
    7. POST zzxkyzbjk_xkBcZyZzxkYzb.html?gnmkdm=N253512&su=<学号> 提交选课
       -> {"flag":"1"} 成功（**必须带 gnmkdm 查询串**，否则回「无操作权限！」）

所有路径、字段名均来自公开的协议分析与实测经验（详见 README「协议说明」）。
教务系统各校有魔改，字段值与页面结构可能不同，因此探测逻辑全部做了容错，
并且所有选课参数都可以在配置里手工覆盖。
"""
from __future__ import annotations

import base64
import binascii
import json
import random
import re
import threading
import time
from collections import OrderedDict
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests
from requests.adapters import HTTPAdapter

try:
    from urllib3.util.retry import Retry
except ImportError:  # pragma: no cover
    from requests.packages.urllib3.util.retry import Retry  # type: ignore

# RSA / PKCS#1 v1.5 全部使用纯 Python 实现（pkcs1.py），
# 不依赖 pycryptodome 的原生扩展 —— 避免在启用应用程序控制策略的 Windows 上
# 因 `_pkcs1_decode.pyd` 被拦截而整个软件无法启动（WinError 4551 / error 0x11c7）。
from pkcs1 import b64_to_int, rsa_encrypt_password  # noqa: F401  (对外保持同名导出)


# --------------------------------------------------------------------------- #
# 异常
# --------------------------------------------------------------------------- #
class JwglxtError(Exception):
    """教务系统交互异常基类。"""


class LoginError(JwglxtError):
    """登录失败。"""


class SessionExpired(JwglxtError):
    """会话失效，需要重新登录。"""


# --------------------------------------------------------------------------- #
# 客户端
# --------------------------------------------------------------------------- #
class JwglxtClient:
    """线程安全的正方教务客户端。"""

    LOGIN_PAGE = "/jwglxt/xtgl/login_slogin.html"
    PUBLIC_KEY = "/jwglxt/xtgl/login_getPublicKey.html"
    LOGIN_POST = "/jwglxt/xtgl/login_slogin.html"
    INDEX = "/jwglxt/xtgl/index_initMenu.html"

    XSXK_PAGE = "/jwglxt/xsxk/zzxkyzb_cxZzxkYzbIndex.html"
    CHOOSED_PAGE = "/jwglxt/xsxk/zzxkyzb_cxZzxkYzbChoosed.html"
    CHOOSED_DISPLAY = "/jwglxt/xsxk/zzxkyzb_cxZzxkYzbChoosedDisplay.html"
    MENU_PAGE = "/jwglxt/xtgl/index_initMenu.html"
    SEARCH = "/jwglxt/xsxk/zzxkyzb_cxZzxkYzbPartDisplay.html"
    JXB = "/jwglxt/xsxk/zzxkyzbjk_cxJxbWithKchZzxkYzb.html"
    # ★ 自主选课的真实提交端点。它定义在「已选」面板加载的
    # `zzxkYzbChoosedZy.js` 的 `saveCourse()` 里，实测某高校可用：
    #     $.post(_path+"/xsxk/zzxkyzbjk_xkBcZyZzxkYzb.html", {...})
    # 注意中间是 **`zzxkyzbjk_`**（教学班查询 `zzxkyzbjk_cxJxbWithKchZzxkYzb.html`
    # 是同一个族）。旧代码写的是 `zzxkyzb_xkBcZzxkYzb.html`，那个路径下确实也挂着
    # 一个处理器，但它属于别的选课模式，无论传什么参数都回
    # 「出现未知异常，请与管理员联系！」 —— 这就是「点抢课抢不上」的真正原因。
    SUBMIT = "/jwglxt/xsxk/zzxkyzbjk_xkBcZyZzxkYzb.html"
    # 旧路径，作为兜底保留（部分老版本/魔改部署仍用这个名字）。
    SUBMIT_LEGACY = "/jwglxt/xsxk/zzxkyzb_xkBcZzxkYzb.html"

    # 提交体的字段集合 —— 与浏览器 `saveCourse()` 的 $.post **完全一致，19 项**。
    # 浏览器一个多余的字段都不发；以前我们以搜索体为基座塞进一百多个字段，
    # 服务端并不需要（这也是一个 910「未知处理器」的诱因）。
    SUBMIT_FIELDS = (
        "jxb_ids", "kch_id", "kcmc", "rwlx", "rlkz", "cdrlkz", "rlzlkz",
        "sxbj", "xxkbj", "qz", "cxbj", "xkkz_id", "njdm_id", "zyh_id",
        "kklxdm", "xklc", "xkxnm", "xkxqm", "jcxx_id",
    )
    # 服务端认定的「选课成功」标志位（见 saveCourse 的分支）。
    SUBMIT_OK_FLAGS = frozenset({"1", "3", "6"})

    # ⚠️ 已废弃：不要再用「一键选课」接口做选课阶段探测。
    # 它对假 xkkz_id 一律回「对不起，当前时间不可选课！」（其实那是在判轮次是否
    # 合法，跟时间无关，会给出**假阴性**），而对真 xkkz_id 一旦该功能真的开着，
    # 它会直接把志愿里的课全选一遍。`check_stage()` 已改为「真 xkkz_id + 假
    # jxb_ids 打提交接口」。这两个常量保留只为兼容老脚本。
    STAGE_CHECK = "/jwglxt/xsxk/zzxkyzb_xkZzxkyzbQuickly.html"
    STAGE_PROBE_ID = "__ZFG_STAGE_PROBE__"

    # 这些是本软件自己的配置项，**不是**教务协议的字段，绝不能混进请求体。
    # （`_search_body` 第二层会转发配置文件里用户手写的协议字段，所以必须显式
    # 排除自己的键；实测以前提交体里混进了 bad_keys / smtp_host / notify_*
    # 等二十多个垃圾字段。）
    NON_PROTOCOL_KEYS = frozenset({
        "base_url", "username", "password", "remember_password",
        "poll_interval", "monitor_interval", "request_timeout", "max_retries",
        "max_attempts_per_round", "concurrent_submit", "start_time",
        "stop_when_all_done", "submit_even_if_full",
        "notify_desktop", "notify_email", "smtp_host", "smtp_port",
        "smtp_user", "smtp_pass", "mail_to", "notify_webhook",
        "webhook_url", "webhook_body_key", "verbose_log", "save_debug_html",
        "bad_keys", "kklxdm_candidates",
    })

    # 搜索接口每页条数（页面用隐藏域 xkmcjzxskcs 配置，取不到时按 10 算）。
    # 服务端只认 kspage/jspage 这一对窗口边界，所以翻页必须整窗口平移。
    PAGE_SIZE = 10
    # 一次「查询」最多翻多少页（防御性上限，正常学校 5~10 页就到底了）
    MAX_PAGES = 60

    # 页面级状态字段：发到搜索/教学班/提交接口会让服务端抛异常并回 HTML 错误页。
    # 实测某高校：带上 pageNumber 就得到 `<!doctype html>...<title>错误`，
    # 去掉就正常返回 11 条课程。localeKey / cdTsxx / pageNumber 都是渲染状态，
    # 不属于业务参数（pageNumber 是该 HTML 页面自己的分页状态，跟搜索窗口无关）。
    PAGE_LEVEL_KEYS = ("localeKey", "cdTsxx", "pageNumber")

    def __init__(self, cfg: Dict[str, Any], logger=None, config_path: str | None = None):
        self.cfg = cfg
        self.log = logger or (lambda msg, level="info": None)
        # 配置路径：探测到「会让服务端报错的字段」后写回，避免每次启动都重探一遍
        self.config_path = config_path
        self.base_url = (cfg.get("base_url") or "").rstrip("/")
        self.username = (cfg.get("username") or "").strip()
        self.password = cfg.get("password") or ""
        self.timeout = float(cfg.get("request_timeout") or 8)
        self.max_retries = int(cfg.get("max_retries") or 3)
        self.save_debug_html = bool(cfg.get("save_debug_html"))

        self.session = requests.Session()
        adapter = HTTPAdapter(
            pool_connections=32,
            pool_maxsize=32,
            max_retries=Retry(total=0),  # 重试自己做，便于精确控制
        )
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "X-Requested-With": "XMLHttpRequest",
            "Connection": "keep-alive",
        })

        self.logged_in = False
        self.su = self.username
        # 各课程类型 -> xkkz_id
        self.xkkz_map: Dict[str, str] = {}
        # 最近一次搜索的尝试记录，便于在界面上诊断「为什么搜不到」
        self.search_trace: List[str] = []
        self.last_search_payload_keys: List[str] = []
        self.last_search_sample_keys: List[str] = []
        # 为 True 表示结果来自「空关键字兜底」，即关键字本身没匹配到课程
        self.keyword_fallback_used = False
        # 选课页最近一次的 HTML 与从中解析出的服务端参数
        self._xsxk_html: str = ""
        self._page_params: Dict[str, Any] = {}
        self._typed_keys: Optional[Dict[str, str]] = None
        # 运行时发现的「会让服务端报错」的请求字段（页面字段 + 配置字段共用）
        self._bad_keys: set = set(self.PAGE_LEVEL_KEYS) | set(cfg.get("bad_keys") or [])
        # 最近一次 POST 的 HTTP 状态码（910 是「没进业务逻辑」的信号，见 submit()）
        self._last_status: int = 200
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ #
    # 底层请求
    # ------------------------------------------------------------------ #
    def _url(self, path: str) -> str:
        if path.startswith("http"):
            return path
        return self.base_url + path

    def _request(self, method: str, path: str, *, retry: bool = True, **kw) -> requests.Response:
        url = self._url(path)
        attempts = self.max_retries if retry else 1
        last_exc: Optional[Exception] = None
        for i in range(attempts):
            try:
                resp = self.session.request(method, url, timeout=self.timeout, **kw)
                return resp
            except requests.RequestException as exc:
                last_exc = exc
                if i < attempts - 1:
                    time.sleep(min(0.4 * (2 ** i), 3.0) + random.uniform(0, 0.2))
        raise JwglxtError(f"网络请求失败：{method} {url} -> {last_exc}")

    def _ensure_url(self) -> None:
        if not self.base_url:
            raise JwglxtError("未配置教务系统地址（base_url），请在「系统配置」里填写。")
        if not self.base_url.startswith("http"):
            raise JwglxtError("教务系统地址必须以 http:// 或 https:// 开头。")

    # ------------------------------------------------------------------ #
    # 登录
    # ------------------------------------------------------------------ #
    @staticmethod
    def _extract_csrf(html: str) -> Optional[str]:
        patterns = [
            r'name="csrftoken"\s+value="([^"]+)"',
            r'name="csrf_token"\s+value="([^"]+)"',
            r'id="csrftoken"\s+value="([^"]+)"',
            r"csrftoken['\"]?\s*[:=]\s*['\"]([^'\"]+)",
            r'name=["\']_csrf["\']\s+value=["\']([^"\']+)',
        ]
        for pat in patterns:
            m = re.search(pat, html, re.I)
            if m:
                return m.group(1)
        return None

    def fetch_public_key(self) -> Tuple[str, str]:
        """获取 RSA 公钥的 (modulus, exponent)，均为 base64 字符串。"""
        resp = self._request("GET", self.PUBLIC_KEY)
        if resp.status_code != 200:
            raise JwglxtError(f"获取公钥失败，HTTP {resp.status_code}")
        text = resp.text.strip()
        try:
            # 标准返回：time 字段 + modulus + exponent
            data = json.loads(text)
        except json.JSONDecodeError:
            # 有的学校返回纯文本 "modulus\n...\nexponent\nAQAB"
            numbers = re.findall(r"[A-Za-z0-9+/=]{20,}", text)
            if len(numbers) >= 1:
                return numbers[0], (numbers[1] if len(numbers) > 1 else "AQAB")
            raise JwglxtError("无法解析公钥响应，请确认教务系统地址是否正确。")
        modulus = data.get("modulus") or data.get("publicKey") or ""
        exponent = data.get("exponent") or "AQAB"
        if not modulus:
            raise JwglxtError("公钥响应中没有 modulus 字段。")
        return str(modulus), str(exponent)

    def login(self) -> bool:
        """完整登录流程。成功返回 True，失败抛 LoginError。"""
        self._ensure_url()
        if not self.username or not self.password:
            raise LoginError("请先填写学号和密码。")

        with self._lock:
            # 1) 打开登录页，拿会话 cookie 与 csrf_token
            page = self._request("GET", self.LOGIN_PAGE)
            if page.status_code >= 400:
                raise LoginError(
                    f"打开登录页失败，HTTP {page.status_code}。请检查教务系统地址与网络。"
                )
            csrf = self._extract_csrf(page.text)
            if self.save_debug_html:
                self._dump_debug("login_page.html", page.text)

            self.session.headers.update({
                "Referer": self._url(self.LOGIN_PAGE),
                "Origin": self.base_url,
                "Content-Type": "application/x-www-form-urlencoded",
            })

            # 2) 公钥 + 加密密码
            modulus, exponent = self.fetch_public_key()
            try:
                mm = rsa_encrypt_password(self.password, modulus, exponent)
            except (ValueError, binascii.Error) as exc:
                raise LoginError(f"密码加密失败：{exc}") from exc

            payload = {
                "yhm": self.username,
                "mm": mm,
                "language": "zh_CN",
            }
            if csrf:
                payload["csrf_token"] = csrf
                payload["csrftoken"] = csrf  # 两种字段名都带上，兼容魔改

            # 3) 提交登录（禁止自动跳转，便于判断结果）
            resp = self._request(
                "POST", self.LOGIN_POST, data=payload, allow_redirects=False, retry=False
            )

            location = resp.headers.get("Location", "") or ""
            ok = resp.status_code in (301, 302, 303, 307, 308)
            if not ok:
                # 有些部署直接 200 + 主界面，或 200 + 错误提示
                if "index_initMenu" in resp.text or "index_gnmkdm" in resp.text:
                    ok = True
                else:
                    tip = self._login_error_message(resp.text)
                    raise LoginError(tip or f"登录失败，HTTP {resp.status_code}")

            if "login_slogin" in location and "error" in location.lower():
                raise LoginError("学号或密码错误（服务器要求重新登录）。")

            # 4) 落地到主页确认会话有效
            check = self._request("GET", self.INDEX, allow_redirects=True)
            final_url = str(check.url)
            if "login_slogin" in final_url:
                raise LoginError("登录未通过：被重定向回登录页，请检查学号/密码。")

            self.logged_in = True
            self.su = self.detect_su(check.text) or self.username
            self.log("info", f"登录成功，学号 {self.su}")
            return True

    @staticmethod
    def _login_error_message(html: str) -> Optional[str]:
        m = re.search(
            r'id="message"[^>]*>([^<]+)<|class="[^"]*alert[^"]*"[^>]*>\s*([^<]+)<',
            html, re.I,
        )
        if m:
            return (m.group(1) or m.group(2) or "").strip()
        if "验证码" in html:
            return "登录失败：系统要求输入验证码（连续输错密码会触发）。请用浏览器登录一次，或清空 cookie 后重试。"
        return None

    @staticmethod
    def detect_su(html: str) -> Optional[str]:
        """从页面里探测学号（su 参数）。"""
        patterns = [
            r"su\s*[=:]\s*['\"]?(\d{6,20})",
            r'name="su"\s+value="(\d{6,20})"',
            r"xh_id['\"]?\s*[:=]\s*['\"]?(\d{6,20})",
        ]
        for pat in patterns:
            m = re.search(pat, html)
            if m:
                return m.group(1)
        return None

    def ensure_login(self) -> None:
        if not self.logged_in:
            self.login()

    def _dump_debug(self, name: str, content: str) -> None:
        try:
            import os
            from config import app_dir
            path = os.path.join(app_dir(), "debug")
            os.makedirs(path, exist_ok=True)
            with open(os.path.join(path, name), "w", encoding="utf-8") as f:
                f.write(content)
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # 选课页探测
    # ------------------------------------------------------------------ #
    def _su_query(self) -> str:
        gnmkdm = self.cfg.get("gnmkdm") or "N253512"
        return f"?gnmkdm={gnmkdm}&su={self.su}"

    def fetch_xsxk_page(self) -> str:
        """打开选课首页，并解析 xkkz_id 映射。返回页面 HTML。"""
        self.ensure_login()
        gnmkdm = self.cfg.get("gnmkdm") or "N253512"
        path = f"{self.XSXK_PAGE}?gnmkdm={gnmkdm}&layout=default&su={self.su}"
        resp = self._request("GET", path, allow_redirects=True)
        html = resp.text
        if "login_slogin" in str(resp.url):
            self.logged_in = False
            raise SessionExpired("会话已失效，请重新登录。")
        if self.save_debug_html:
            self._dump_debug("xsxk_page.html", html)
        self._xsxk_html = html
        self._typed_keys = None
        self.xkkz_map = self.parse_xkkz_ids(html)
        self._page_params = self.collect_page_params()
        if not self.xkkz_map and not self._page_params.get("xkkz_id"):
            # 首页没有，去其它几个选课相关页面找（不同学校放的位置不一样）
            self._collect_xkkz_ids()
        if not self.xkkz_map and self._page_params.get("xkkz_id"):
            kk = str(self._page_params.get("first_kklxdm") or self.cfg.get("kklxdm") or "10")
            self.xkkz_map = {kk: str(self._page_params["xkkz_id"])}
        self._log_xkkz_map()
        self._log_page_params()
        return html

    def _log_page_params(self) -> None:
        keys = ("njdm_id", "zyh_id", "bh_id", "xkxnm", "xkxqm", "jg_id",
                "first_kklxdm", "first_kklxmc")
        shown = {k: self._page_params[k] for k in keys if self._page_params.get(k)}
        if shown:
            self.log("info", f"选课页下发的参数：{shown}")

    def _fetch_page_text(self, path: str) -> Optional[str]:
        """GET 一个页面并返回正文；302 到登录页或异常时返回 None。"""
        try:
            resp = self._request("GET", path, allow_redirects=True)
        except Exception as exc:  # noqa: BLE001
            self.log("warn", f"取页面失败 {path}：{exc}")
            return None
        if "login_slogin" in str(resp.url):
            return None
        return resp.text

    def _collect_xkkz_ids(self) -> Dict[str, str]:
        """在多个候选页面里找 xkkz_id，命中即并入 self.xkkz_map。

        实测：部分学校（如某高校）的选课首页并不下发轮次标识，
        它出现在「已选课程显示页」或菜单页里，所以这里逐个试。
        """
        gnmkdm = self.cfg.get("gnmkdm") or "N253512"
        candidates = [
            f"{self.XSXK_PAGE}?gnmkdm={gnmkdm}",
            self.CHOOSED_DISPLAY,
            f"{self.CHOOSED_DISPLAY}?gnmkdm={gnmkdm}&su={self.su}",
            f"{self.CHOOSED_PAGE}?gnmkdm={gnmkdm}&su={self.su}",
            f"{self.MENU_PAGE}?gnmkdm=N253512&su={self.su}",
            "/jwglxt/xtgl/index_initMenu.html",
        ]
        for path in candidates:
            html = self._fetch_page_text(path)
            if not html:
                continue
            mapping = self.parse_xkkz_ids(html)
            if not mapping:
                continue
            for k, v in mapping.items():
                self.xkkz_map.setdefault(k, v)
            self.log("info", f"在 {path.split('?')[0]} 里找到 xkkz_id。")
            break
        return self.xkkz_map

    def _log_xkkz_map(self) -> None:
        if self.xkkz_map:
            types = ", ".join(f"{k}={str(v)[:10]}…" for k, v in self.xkkz_map.items())
            self.log("info", f"探测到选课轮次 xkkz_id：{types}")
        else:
            self.log(
                "warn",
                "未能在任何选课页面上找到 xkkz_id（选课轮次标识）。"
                "如果选课界面此刻是打开的，请把浏览器地址栏里的 "
                "zzxkyzb_cxZzxkYzbPartDisplay.html 完整链接、或 F12 → Network 里"
                "名为 cxZzxkYzbPartDisplay 的请求表单，反馈给开发者；"
                "也可以在「系统配置」里直接填写 xkkz_id。",
            )

    @staticmethod
    def _extract_xkkz_candidates(html: str) -> List[Tuple[str, str]]:
        """按置信度从高到低抽取 (kklxdm, xkkz_id) 候选。

        不同学校把轮次标识放在完全不同的位置，这里把已知形态都覆盖一遍：
            1. queryCourse(this,'10','14564675131412412421','2020','2021')
            2. <input ... name="firstXkkzId" value="...">
            3. var xkkz_id = '...'  /  url: "...?xkkz_id=..."
            4. onclick="...('10','...')" 之类的成对出现
        返回去重后的有序列表，调用方按顺序取用。
        """
        found: List[Tuple[str, str]] = []

        def add(kklxdm: str, xkkz: str) -> None:
            kklxdm = (kklxdm or "").strip()
            xkkz = (xkkz or "").strip()
            if not xkkz or xkkz.lower() in ("null", "undefined", "default", "0"):
                return
            if len(xkkz) < 4:
                return
            if (kklxdm, xkkz) not in found:
                found.append((kklxdm, xkkz))

        # 1. queryCourse(this,'10','14564675131412412421','2020','2021')
        for m in re.finditer(
            r"queryCourse\s*\(\s*[^,)]*,\s*['\"](\d+)['\"]\s*,\s*['\"]([^'\"]{4,})['\"]",
            html,
        ):
            add(m.group(1), m.group(2))

        # 2. 隐藏域 / 任意 input：name 里带 kkz id 的都算
        for m in re.finditer(r"<input\b[^>]*>", html, re.I):
            tag = m.group(0)
            name_m = re.search(r'name=["\']([^"\']*)["\']', tag, re.I)
            val_m = re.search(r'value=["\']([^"\']*)["\']', tag, re.I)
            if not name_m or not val_m:
                continue
            name, value = name_m.group(1), val_m.group(1)
            if not re.search(r"kkz[_]?id", name, re.I):
                continue
            hint = re.sub(r"[^0-9]", "", name)
            add(hint, value)

        # 3. URL 查询串 / 相邻参数：kklxdm 在前、xkkz_id 在后，
        #    中间允许一个 & 分隔符（不允许出现第二个 = 或 &，避免跨参数误配）
        for m in re.finditer(
            r"kklxdm[^=&#]*=[^\d&#]*(\d+)[^=&#]*&?[^=&#]*[Xx]kkz[_]?[Ii]d"
            r"[^=&#]*=([0-9A-Za-z_\-]{4,})",
            html,
        ):
            add(m.group(1), m.group(2))

        # 4. py dict / json：'kklxdm': '10', 'xkkz_id': '...'
        for m in re.finditer(
            r"['\"]kklxdm['\"]\s*:\s*['\"](\d+)['\"][^{}]{0,120}?"
            r"['\"]xkkz_?id['\"]\s*:\s*['\"]([^'\"]{4,})['\"]",
            html, re.I,
        ):
            add(m.group(1), m.group(2))

        # 5. 变量赋值 / JSON：xkkz_id = '...'（没有 kklxdm 时记成通用值 ""）
        for m in re.finditer(
            r"[Xx]kkz[_]?[Ii]d\s*[=:]\s*['\"]([^'\"]{4,})['\"]", html
        ):
            # 排除 name="xkkz_id" value="..." 这种两段式（值里常混进 kklxdm）
            tail = html[m.end():m.end() + 40]
            if re.search(r"[Xx]kkz[_]?[Ii]d", m.group(1)) or "kklxdm" in tail:
                continue
            add("", m.group(1))

        # 6. 形如 ('10','14564675131412412421') 的成对参数
        for m in re.finditer(r"['\"](\d{2})['\"]\s*,\s*['\"]([0-9]{8,})['\"]", html):
            add(m.group(1), m.group(2))

        return found

    @staticmethod
    def parse_xkkz_ids(html: str) -> Dict[str, str]:
        """从选课页解析 课程类型代码 -> xkkz_id。

        兼容两种来源（协议分析：onClick 优先于隐藏域）：
            queryCourse(this,'10','14564675131412412421','2020','2021')
            <input type="hidden" name="firstXkkzId" id="firstXkkzId" value="...">
        另外吸纳变量赋值、URL 参数等形态（见 _extract_xkkz_candidates）。
        """
        result: Dict[str, str] = {}

        # 优先：onClick 回调参数（解析成对 课程类型->轮次）
        for m in re.finditer(
            r"queryCourse\s*\(\s*this\s*,\s*['\"](\d+)['\"]\s*,\s*['\"]([0-9a-fA-F]{8,})['\"]",
            html,
        ):
            result.setdefault(m.group(1), m.group(2))

        # 其余形态按置信度顺序补齐
        for kklxdm, xkkz in JwglxtClient._extract_xkkz_candidates(html):
            if kklxdm:
                result.setdefault(kklxdm, xkkz)
            else:
                result.setdefault("*", xkkz)
        return result

    def collect_page_params(self) -> Dict[str, Any]:
        """从选课页的隐藏域里取出服务端自己下发的真实参数。

        **这一步是必需的**：`zzxkyzb_cxZzxkYzbPartDisplay.html` 只带
        `xkkz_id` 而不带 `njdm_id`/`zyh_id`/`bh_id`/`xkxnm`/`xkxqm` 时，
        服务端会返回 `{"tmpList": [], "sfxsjc": "1"}` —— 看起来像「没有课程」，
        实际上是筛选维度缺失。这些值页面里就有，直接照抄即可：

            firstXkkzId / firstKklxdm / firstKklxmc
            firstNjdmId / firstZyhId
            njdm_id / zyh_id / bh_id / xkxnm / xkxqm / jg_id_1 / xh_id
        """
        html = self._xsxk_html or ""
        if not html:
            return {}

        def keep(name: str) -> bool:
            """过滤掉搜索框自己的字段和确定会报错的页面级字段。

            `PAGE_LEVEL_KEYS` 里的字段是页面渲染状态（分页、语言、窗口宽度…），
            实测把它们发给 `zzxkyzb_cxZzxkYzbPartDisplay.html`，服务端会直接抛
            异常并返回一个 HTML 错误页而不是 JSON。留着会被别处当参数用，
            所以这里就不要再收。
            """
            low = name.lower()
            if low.startswith("filter_list"):
                return False
            if low.startswith("searchbox") or low in ("kcmc", "jsxm", "jsgh"):
                return False
            if name in self._bad_keys or low in self._bad_keys:
                return False
            return True

        found: Dict[str, Any] = {}
        # 各种顺序的 name/value 组合都要能抓：name 在前、value 在前，属性之间可能有
        # 别的属性（id= 等），所以用 [^>] 限长度而不是紧邻匹配。
        patterns = (
            (r"""name=["']([^"']+)["'][^>]{0,120}?value=["']([^"']*)["']""", 1, 2),
            (r"""value=["']([^"']*)["'][^>]{0,120}?name=["']([^"']+)["']""", 2, 1),
            (r"""id=["']([^"']+)["'][^>]{0,120}?value=["']([^"']*)["']""", 1, 2),
        )
        for pattern, name_group, value_group in patterns:
            for m in re.finditer(pattern, html, re.I | re.S):
                raw_name = m.group(name_group).strip()
                value = m.group(value_group).strip()
                if not raw_name or not value or not keep(raw_name):
                    continue
                # firstXxxId / firstXxx 这类前缀去掉，等价于标准参数名
                key = raw_name
                if key.startswith("first") and len(key) > 5:
                    key = key[5:6].lower() + key[6:]
                # jg_id_1 与 jg_id 等价（页面里两个都在）
                key = re.sub(r"^jg_id_\d+$", "jg_id", key)
                if key not in found:
                    found[key] = value

        # 下拉框的选中值 / 第一个选项也一并收
        for m in re.finditer(r"<select\b([^>]*)>(.*?)</select>", html, re.I | re.S):
            attrs, body = m.group(1), m.group(2)
            nm = re.search(r"""name=["']([^"']+)["']""", attrs, re.I)
            if not nm:
                continue
            name = nm.group(1)
            if not keep(name) or name in found:
                continue
            sel = re.search(r"""<option[^>]*\bselected[^>]*value=["']([^"']*)""", body, re.I)
            if not sel:
                sel = re.search(r"""<option[^>]*value=["']([^"']*)""", body, re.I)
            if sel and sel.group(1).strip():
                found[name] = sel.group(1).strip()

        # <script> 里的 var X = 'Y'; 也一并收
        for m in re.finditer(
            r"""\b(xkkz_id|njdm_id|zyh_id|bh_id|xkxnm|xkxqm|jg_id|xklc)\b\s*=\s*['"]([^'"]+)['"]""",
            html,
        ):
            key, value = m.group(1), m.group(2).strip()
            if value and key not in found:
                found[key] = value
        return found

    def effective_param(self, name: str, default: Any = "") -> Any:
        """取参数的最终值：页面下发的真实值 > 配置里的值 > 默认值。

        优先级反着来是刻意的：页面上的值就是服务端此刻要的值（学期、专业、
        年级都会随选课轮次变），而配置文件可能是几个月前填的。
        """
        page = self._page_params.get(name)
        if page:
            return page
        cfg_value = self.cfg.get(name)
        if cfg_value not in (None, ""):
            return cfg_value
        return default

    def _typed_mask(self) -> Dict[str, str]:
        """从页面收集「用户手动输入过」的键（这些不该被配置文件覆盖）。"""
        if self._typed_keys is not None:
            return self._typed_keys
        mask: Dict[str, str] = {}
        html = self._xsxk_html or ""
        if html:
            for m in re.finditer(
                r"""<input\b[^>]*\bname=["']([^"']+)["'][^>]*>""", html, re.I
            ):
                tag = m.group(0)
                if re.search(r"""\btype=["']hidden["']""", tag, re.I):
                    continue
                if re.search(r"""\breadonly\b""", tag, re.I):
                    continue
                name = m.group(1)
                if name not in ("kklxdm", "kklxmc", "xkkz_id", "njdm_id", "zyh_id", "bh_id"):
                    continue
                value = re.search(r"""value=["']([^"']*)["']""", tag)
                mask[name] = (value.group(1) if value else "").strip()
        self._typed_keys = mask
        return mask

    def plan_params(self) -> Dict[str, Any]:
        """算出搜索要用的参数组合：先页面值、再用户手填。"""
        plan: Dict[str, Any] = {}
        for name in ("njdm_id", "zyh_id", "bh_id", "xkxnm", "xkxqm", "jg_id", "xqh_id"):
            value = self.effective_param(name)
            if value:
                plan[name] = value
        return plan

    def get_xkkz_id(self, kklxdm: str) -> str:
        """取某课程类型对应的 xkkz_id（配置可覆盖）。

        优先级：配置里的 xkkz_id_<课程类型> > 页面解析出的该类型 >
        解析出的 '*' 通用值 > 解析出的任意一个（很多学校各类型共用一个轮次 id）。
        """
        override = self.cfg.get(f"xkkz_id_{kklxdm}")
        if override:
            return str(override)
        if kklxdm in self.xkkz_map:
            return self.xkkz_map[kklxdm]
        if self.xkkz_map.get("*"):
            return self.xkkz_map["*"]
        if self.xkkz_map:
            # 有些学校只暴露一个轮次 id，课程类型不同但共用
            return next(iter(self.xkkz_map.values()))
        raise JwglxtError(
            "未取到 xkkz_id（选课轮次标识）。这个值只在选课开放期间由选课页下发："
            "请在浏览器里打开选课界面并点一次「查询」，然后看 F12 → Network 里"
            "名为 cxZzxkYzbPartDisplay 的请求，「表单」中 xkkz_id 的值；"
            "把它填进「系统配置」的「xkkz_id（选课轮次标识）」即可。"
        )

    def ensure_page_context(self, force: bool = False) -> Dict[str, Any]:
        """确保「选课页下发的参数」已经拿到（njdm_id / zyh_id / bh_id / xkxqm…）。

        这里踩过一个很隐蔽的坑：配置里存过 `xkkz_id_auto` 之后，
        `ensure_xkkz()` 会直接返回，选课页再也不会被打开，于是
        `_page_params` 一直是空的 —— 而搜索真正需要的不止轮次标识，
        缺了页面下发的身份/学期参数，服务端只会回
        `{"tmpList": [], "sfxsjc": "1"}`，看起来就像「一门课都搜不到」。
        """
        if force or not self._page_params:
            try:
                self.fetch_xsxk_page()
            except SessionExpired:
                raise
            except JwglxtError as exc:  # 拿不到也别炸，用现有参数继续试
                self.log("warn", f"取选课页失败（改用已有参数继续）：{exc}")
        return self._page_params

    def ensure_xkkz(self) -> Dict[str, str]:
        """确保已经拿到选课轮次标识；没有就去各选课页面找一遍。

        返回 `self.xkkz_map`。只有「既解析不出、配置里也没填」时才会抛
        JwglxtError —— 搜索流程会捕获它并降级成空 xkkz_id 继续尝试。
        """
        # 先补齐页面上下文：轮次标识可以来自配置，但身份/学期参数只能来自页面
        self.ensure_page_context()
        if self.xkkz_map:
            return self.xkkz_map
        auto = self.cfg.get("xkkz_id_auto") or ""
        if auto:
            self.xkkz_map = {str(self.cfg.get("kklxdm") or "10"): str(auto)}
            return self.xkkz_map
        for kk in (str(self.cfg.get("kklxdm") or "10"),
                   *(str(x) for x in (self.cfg.get("kklxdm_candidates") or []))):
            override = self.cfg.get(f"xkkz_id_{kk}")
            if override:
                self.xkkz_map[str(kk)] = str(override)
        if self.xkkz_map:
            return self.xkkz_map
        self.fetch_xsxk_page()
        return self.xkkz_map

    # ------------------------------------------------------------------ #
    # 搜索 / 取教学班 / 提交
    # ------------------------------------------------------------------ #
    def _computed_params(self, kklxdm: str, page: int = 1, xkkz_id: str = "",
                         page_size: int = 0) -> Dict[str, Any]:
        """本次请求计算的参数（优先级最高，压过页面与配置里的同名项）。

        `page` 是**第几页**（1 起）；服务端只认 `kspage`/`jspage` 这一对
        窗口边界，实测某高校每页 10 条：

            page=1 -> kspage=1,  jspage=10
            page=2 -> kspage=11, jspage=20

        以前把 `page` 直接当 `kspage` 发（kspage=2 配 jspage=10），窗口永远
        停在最前面，于是就出现了「只能搜到少量课程」。
        """
        try:
            step = int(page_size or self.effective_param("xkmcjzxskcs", "") or self.PAGE_SIZE)
        except (TypeError, ValueError):
            step = self.PAGE_SIZE
        if step <= 0:
            step = self.PAGE_SIZE
        page = max(1, int(page))
        kspage = (page - 1) * step + 1
        jspage = page * step
        return {
            "bh_id": self.effective_param("bh_id", ""),
            "bklx_id": self.effective_param("bklx_id", "0"),
            "ccdm": self.effective_param("ccdm", "3"),
            "jg_id": self.effective_param("jg_id", "0102"),
            "jspage": str(jspage),
            "kklxdm": kklxdm,
            "kspage": str(kspage),
            "njdm_id": self.effective_param("njdm_id", ""),
            "rwlx": self.effective_param("rwlx", "2"),
            "sfkcfx": self.effective_param("sfkcfx", "0"),
            "sfkgbcx": self.effective_param("sfkgbcx", "0"),
            "sfkknj": self.effective_param("sfkknj", "0"),
            "sfkkzy": self.effective_param("sfkkzy", "0"),
            "sfkxq": self.effective_param("sfkxq", "0"),
            "sfrxtgkcxd": self.effective_param("sfrxtgkcxd", "1"),
            "sfznkx": self.effective_param("sfznkx", "0"),
            "tykczgxdcs": self.effective_param("tykczgxdcs", "10"),
            "xbm": self.effective_param("xbm", "1"),
            "xh_id": self.effective_param("xh_id", self.su),
            "xkly": self.effective_param("xkly", "0"),
            "xkkz_id": xkkz_id,
            "xkxnm": self.effective_param("xkxnm", ""),
            "xkxqm": self.effective_param("xkxqm", ""),
            "xqh_id": self.effective_param("xqh_id", "1"),
            "xsbj": self.effective_param("xsbj", "0"),
            "xslbdm": self.effective_param("xslbdm", ""),
            "zdkxms": self.effective_param("zdkxms", "0"),
            "zyfx_id": self.effective_param("zyfx_id", "wfx"),
            "zyh_id": self.effective_param("zyh_id", ""),
        }

    def _search_body(self, keyword: str, kklxdm: str, kspage: int = 1,
                     xkkz_id: str = "", extra_keys: Optional[set] = None,
                     switch: bool = True) -> Dict[str, Any]:
        """构造搜索与取教学班接口共用的参数体。

        两个必守的字段：
        1. **xkkz_id** —— 请求里没有它时服务端只回空 JSON 对象 {}，
           表现就是「页面不点查询就没有课程」。
        2. **njdm_id / zyh_id / bh_id / xkxnm / xkxqm** —— 实测在某高校，
           只带 xkkz_id 而缺这几项时服务端回 `{"tmpList": [], "sfxsjc": "1"}`，
           同样看起来像「没有课程」，但其实是筛选维度不全。
           这几项的值由选课页的隐藏域下发，见 `collect_page_params()`。

        取值一律走 `effective_param()`：**页面下发的值 > 配置文件 > 内置默认**。
        页面上写着的是服务端此刻要的值（学期、专业、年级会随轮次变），
        配置文件可能是几个月前填的。
        """
        cfg = self.cfg
        # 第一层：选课页下发/内嵌的表单值（服务端此刻真实要的值）
        body: Dict[str, Any] = {
            k: v for k, v in self._page_params.items() if k not in self._bad_keys
        }
        # switch=False 时只发页面值与计算值 —— 自愈探测靠这个构造「最小可信请求」。
        if switch:
            # 第二层：配置文件里用户显式填的、且页面没给的
            for key, value in cfg.items():
                if key in body or value in (None, "", [], {}):
                    continue
                if key in ("username", "password", "base_url"):
                    continue
                # 本软件自己的配置项（轮询间隔、SMTP、通知开关…）不是协议字段，
                # 混进请求体只会让服务端看到一堆垃圾键，必须显式排除。
                if key in self.NON_PROTOCOL_KEYS:
                    continue
                if extra_keys is not None and key not in extra_keys:
                    continue
                if key in self._bad_keys:
                    continue
                body[key] = value
        # 第三层：本次请求计算的参数
        body.update(self._computed_params(kklxdm, kspage, xkkz_id))
        # 空关键字时干脆不发这个字段，避免个别服务端把空串当成「无匹配」
        if keyword:
            body["filter_list[0]"] = keyword
        else:
            body.pop("filter_list[0]", None)
        return body

    def _post_json(self, path: str, body: Dict[str, Any]) -> Any:
        """POST 表单并解析 JSON，自动处理会话失效。"""
        self.ensure_login()
        referer = self._url(self.XSXK_PAGE) + self._su_query()
        resp = self._request(
            "POST", path,
            data=body,
            headers={"Referer": referer, "Origin": self.base_url, "X-Requested-With": "XMLHttpRequest"},
            allow_redirects=False,
        )
        if resp.status_code in (301, 302, 303, 307, 308):
            self.logged_in = False
            raise SessionExpired("会话已过期（服务器重定向到登录页）。")
        text = resp.text.strip()
        self._last_status = resp.status_code
        if not text:
            return {}
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            if "login_slogin" in text or "j_username" in text:
                self.logged_in = False
                raise SessionExpired("会话已过期。")
            if self.save_debug_html:
                self._dump_debug("bad_response.html", text)
            raise JwglxtError(f"服务器返回了非 JSON 内容（前 120 字）：{text[:120]}")

    # 不同学校/版本用的搜索入口路径不同，按顺序尝试
    SEARCH_ENDPOINTS = (
        "/jwglxt/xsxk/zzxkyzb_cxZzxkYzbPartDisplay.html",
        "/jwglxt/xsxk/zzxkyzb_cxZzxkYzbPartDisplay.html?gnmkdm=N253512",
        "/jwglxt/xsxk/zzxkyzb_cxXkYzbPartDisplay.html",
        "/jwglxt/xsxk/zzxkyzb_cxZzxkYzb.html",
    )

    def _post_json_raw(self, path: str, body: Dict[str, Any]) -> Any:
        """同 _post_json，但把非 JSON 也尽量还原出来用于诊断。"""
        return self._post_json(path, body)

    def _describe_response(self, data: Any) -> str:
        """把响应形状描述成一行，用于说明「为什么没有课程」。"""
        if isinstance(data, list):
            return f"JSON 数组，{len(data)} 项"
        if isinstance(data, dict):
            if not data:
                return "空 JSON 对象 {}（通常是参数缺项被服务端拒绝）"
            keys = list(data.keys())[:6]
            return f"JSON 对象，键：{', '.join(keys)}"
        if data is None:
            return "无内容"
        return f"{type(data).__name__}: {str(data)[:80]}"

    def _remember_response_shape(self, data: Any) -> None:
        if isinstance(data, dict) and data:
            self.last_search_payload_keys = list(data.keys())[:20]
            for value in data.values():
                if isinstance(value, list) and value and isinstance(value[0], dict):
                    self.last_search_sample_keys = list(value[0].keys())[:30]
                    break
                if isinstance(value, dict):
                    for sub in value.values():
                        if isinstance(sub, list) and sub and isinstance(sub[0], dict):
                            self.last_search_sample_keys = list(sub[0].keys())[:30]
                            break
        elif isinstance(data, list) and data and isinstance(data[0], dict):
            self.last_search_sample_keys = list(data[0].keys())[:30]

    def search_courses(self, keyword: str, kklxdm: Optional[str] = None,
                       pages: int = 0) -> List[Dict[str, Any]]:
        """按关键字搜索可选课程，返回候选教学班列表。

        每个条目包含：kch（课程号）、jxb（教学班号）、kcmc、xf、teacher、
        xkkz_id、kklxdm、余量等已归一化字段。

        `pages=0`（默认）表示**一直翻到没有为止**，这样「查询」出来的才是
        全部课程；给正数则最多翻这么多页。

        很多学校的选课页必须**先点「查询」按钮**才会出课程，而且各校提交的
        参数、入口路径不完全一致。因此这里不是只发一次请求，而是分两轮尝试，
        命中即返回：

            第一轮（关键字优先，按候选矩阵）：
                入口路径 × 课程类型 × 指定关键字
            第二轮（空查询兜底，等价于「什么都不填直接点查询」）：
                入口路径 × 课程类型 × 空关键字

        第二轮只在第一轮一无所获时才会跑，这样既能让「必须先点查询」的学校
        列出课程，又不会掩盖「关键字确实没匹配到」这一事实 —— 命中兜底结果
        时会置 `self.keyword_fallback_used = True`，界面据此提示用户。

        每次尝试都会记录到 `self.search_trace`，搜不到时界面会显示这些记录，
        便于对照浏览器 Network 面板定位差异。
        """
        self.ensure_xkkz()
        self.search_trace = []
        self.last_search_payload_keys = []
        self.last_search_sample_keys = []
        self.keyword_fallback_used = False
        rounds = [[keyword]] if not keyword.strip() else [[keyword], [""]]
        types = [str(kklxdm)] if kklxdm else [
            str(t) for t in (self.cfg.get("kklxdm_candidates") or ["10"])
        ]

        last_error: Optional[JwglxtError] = None
        saw_meaningful_response = False

        for round_index, keywords in enumerate(rounds):
            if round_index:
                self.search_trace.append("———— 第一轮无结果，改用空关键字兜底查询 ————")
            for endpoint in self.SEARCH_ENDPOINTS:
                for kk in types:
                    # 某些版本不需要 xkkz_id 也能搜，拿不到就用空值继续试
                    try:
                        xkkz = self.get_xkkz_id(kk)
                    except JwglxtError as exc:
                        last_error = exc
                        xkkz = ""
                        self.search_trace.append(f"kklxdm={kk} 取 xkkz_id 失败：{exc}")

                    for kw in keywords:
                        self.search_trace.append(
                            f"尝试 {endpoint} kklxdm={kk} 关键字={kw or '(空)'}"
                        )
                        try:
                            data = self._search_payload_attempt(endpoint, kw, kk, xkkz)
                        except SessionExpired:
                            raise
                        except JwglxtError as exc:
                            last_error = exc
                            self.search_trace.append(f"  → 请求失败：{exc}")
                            # 服务端嫌参数里有它不认的字段时会回 HTML 错误页。
                            # 与其整轮失败，不如让它自己把坏字段找出来再试一次。
                            if self._looks_like_server_reject(exc):
                                try:
                                    self._probe_extra_keys(endpoint, kw, kk, xkkz)
                                    data = self._search_payload_attempt(endpoint, kw, kk, xkkz)
                                    self.search_trace.append("  → 已剔除导致服务端报错的页面字段后重试成功")
                                except (JwglxtError, SessionExpired):
                                    continue
                            else:
                                continue

                        rows = self._extract_tmp_list(data)
                        self._remember_response_shape(data)
                        if not rows:
                            self.search_trace.append(
                                f"  → 无课程：{self._describe_response(data)}"
                            )
                            # 服务端给了有内容的 JSON（如 {"tmpList": []}）说明参数没问题，
                            # 只是确实没有课程；此时不该把「缺 xkkz_id」当成原因抛出去。
                            if not isinstance(data, dict) or data:
                                saw_meaningful_response = True
                            continue

                        results: List[Dict[str, Any]] = []
                        seen_keys: set = set()
                        for row in rows:
                            item = self.normalize_row(row, kk, xkkz)
                            if not item:
                                continue
                            key = (item.get("kch_id") or item.get("kch"), item.get("jxb"))
                            if key in seen_keys:
                                continue
                            seen_keys.add(key)
                            results.append(item)

                        # 翻页：服务端每页只回一窗口（某高校 10 条），不翻页就会
                        # 出现「只能搜到少量课程」。窗口边界是 kspage/jspage，
                        # 相邻窗口会有重叠，所以按 (kch_id, jxb) 去重。
                        max_pages = self.MAX_PAGES if pages <= 0 else min(pages, self.MAX_PAGES)
                        sep = "&" if "?" in endpoint else "?"
                        page_url = endpoint + sep + self._su_query().lstrip("?")
                        page = 2
                        while page <= max_pages:
                            try:
                                more = self._extract_tmp_list(
                                    self._post_json(
                                        page_url,
                                        self._search_body(kw, kk, kspage=page, xkkz_id=xkkz),
                                    )
                                )
                            except (JwglxtError, SessionExpired) as exc:
                                if isinstance(exc, SessionExpired):
                                    raise
                                self.search_trace.append(f"  → 第 {page} 页失败：{exc}")
                                break
                            if not more:
                                self.search_trace.append(f"  → 第 {page} 页起没有更多课程，翻页结束")
                                break
                            added = 0
                            for row in more:
                                item = self.normalize_row(row, kk, xkkz)
                                if not item:
                                    continue
                                key = (item.get("kch_id") or item.get("kch"), item.get("jxb"))
                                if key in seen_keys:
                                    continue
                                seen_keys.add(key)
                                results.append(item)
                                added += 1
                            self.search_trace.append(
                                f"  → 第 {page} 页 {len(more)} 条，新增 {added} 条"
                            )
                            if added == 0:
                                # 整页都是已见过的课程，说明服务端没有真的往后翻
                                # （或已经绕回开头），继续翻只会空转。
                                self.search_trace.append("  → 本页无新增课程，翻页结束")
                                break
                            page += 1

                        if results:
                            self.keyword_fallback_used = bool(round_index)
                            self.search_trace.append(f"  → ✅ 命中 {len(results)} 条")
                            return results
                        self.search_trace.append("  → 有数据但字段无法识别，见原始字段名")

        if last_error and not kklxdm:
            # 所有候选类型都没能拿到 xkkz_id，而且服务端每次都回空对象 {}，
            # 说明确实是缺少轮次标识。把原因抛出去，界面会引导用户手工填写。
            if not saw_meaningful_response:
                raise last_error
        return []

    def _search_payload_attempt(self, endpoint: str, keyword: str, kklxdm: str,
                                xkkz_id: str = "") -> Any:
        """单次搜索请求。endpoint 里可能已带 query string，注意拼接顺序。"""
        body = self._search_body(keyword, kklxdm, xkkz_id=xkkz_id)
        sep = "&" if "?" in endpoint else "?"
        return self._post_json(endpoint + sep + self._su_query().lstrip("?"), body)

    def _looks_like_server_reject(self, exc: Exception) -> bool:
        """服务端用 HTML 错误页拒绝请求（而不是回 JSON 业务失败）时为真。"""
        text = str(exc)
        return "<!doctype html" in text.lower() or "<title>错误" in text

    def _probe_extra_keys(self, endpoint: str, keyword: str, kklxdm: str,
                          xkkz_id: str) -> None:
        """找出「发了就让服务端报错」的字段，写进 self._bad_keys。

        只在服务端已经用 HTML 错误页拒过一次时才调用，所以正常学校一次都不会跑。
        基线是**只发计算字段**的请求体 —— 实测这一份在某高校能正常返回课程，
        而把选课页那 113 个表单域原样带上，服务端会直接抛 HTML 错误页。
        以它做对照再二分，单个键出错就永久记下。最多十几次请求，一次性开销。

        注意候选必须同时包含**选课页下发的字段**与**配置里的字段**：
        踩过的坑就是只挑了配置字段，而真正致命的 `pageNumber` 来自选课页。
        """
        computed = self._computed_params(kklxdm, 1, xkkz_id)
        candidates: List[str] = []
        for key in list(self._page_params) + list(self.cfg):
            if key in computed or key in self._bad_keys or key in candidates:
                continue
            if key in ("username", "password", "base_url"):
                continue
            value = self._page_params.get(key, self.cfg.get(key))
            if value in (None, "", [], {}):
                continue
            candidates.append(key)
        if not candidates:
            return

        def build(keys: List[str]) -> Dict[str, Any]:
            """只发计算字段 + 指定的这几个候选字段。"""
            keep = set(keys)
            body = self._search_body(keyword, kklxdm, xkkz_id=xkkz_id)
            return {k: v for k, v in body.items() if k in computed or k in keep}

        def rejected(keys: List[str]) -> bool:
            try:
                self._post_json(endpoint + self._su_query(), build(keys))
                return False
            except SessionExpired:
                raise
            except JwglxtError as exc:
                return self._looks_like_server_reject(exc)

        # 先验证基线（只有计算字段）是通的，不通就没法二分
        if rejected([]):
            self.log("warn", "只发必要字段也被服务端拒绝，跳过坏字段自愈探测。")
            return

        found = 0
        queue: List[List[str]] = [candidates]
        while queue:
            chunk = queue.pop(0)
            if not chunk:
                continue
            if len(chunk) == 1:
                self._bad_keys.add(chunk[0])
                found += 1
                self.log("warn", f"请求体里的字段 {chunk[0]} 会让服务端报错，已忽略。")
                continue
            if rejected(chunk):
                mid = len(chunk) // 2
                queue.append(chunk[:mid])
                queue.append(chunk[mid:])
        if found:
            self.log("info", f"已忽略这些字段：{', '.join(sorted(self._bad_keys))}")
            self._remember_bad_keys()

    @property
    def bad_keys(self) -> List[str]:
        """本次会话判定为「会惹服务端报错」的请求字段（已排序）。"""
        return sorted(self._bad_keys)

    def _remember_bad_keys(self) -> None:
        """把学到的坏字段写回配置，下次启动直接跳过探测。

        有配置路径时**重新读一遍再合并**，避免把界面上尚未保存的其它改动一起写脏。
        """
        keys = sorted(self._bad_keys)
        self.cfg["bad_keys"] = keys
        if not self.config_path:
            return
        try:
            from config import load_config, save_config  # 延迟导入，避免循环依赖
            fresh = load_config(self.config_path)
            if sorted(fresh.get("bad_keys") or []) == keys:
                return
            fresh["bad_keys"] = keys
            save_config(fresh, self.config_path)
        except Exception as exc:  # noqa: BLE001
            self.log("warn", f"坏字段列表写回配置失败（不影响使用）：{exc}")

    @staticmethod
    def _extract_tmp_list(data: Any) -> List[Dict[str, Any]]:
        """不同版本字段名有差异：tmpList / tmpListXnxq / items / rows。"""
        if isinstance(data, list):
            return [d for d in data if isinstance(d, dict)]
        if not isinstance(data, dict):
            return []
        for key in ("tmpList", "tmpListXnxq", "items", "rows", "list"):
            value = data.get(key)
            if isinstance(value, list):
                return [d for d in value if isinstance(d, dict)]
            if isinstance(value, dict):
                for sub in value.values():
                    if isinstance(sub, list):
                        return [d for d in sub if isinstance(d, dict)]
        return []

    @staticmethod
    def normalize_row(row: Dict[str, Any], kklxdm: str, xkkz_id: str) -> Optional[Dict[str, Any]]:
        """把原始课程行归一化成统一结构。"""
        kch = row.get("kch") or row.get("kch_id") or row.get("courseCode")
        jxb = row.get("jxb") or row.get("jxb_id") or row.get("do_jxb_id")
        if not kch and not jxb:
            return None
        # 取教学班接口要的是 kch_id（那一长串十六进制），不是课程号 kch。
        # 搜索结果 raw 里就叫 kch_id；没有时退化为 kch，至少不会更差。
        kch_id = str(row.get("kch_id") or row.get("courseId") or kch or "")
        return {
            "kch": str(kch or ""),
            "kch_id": kch_id,
            "jxb": str(jxb or ""),
            "kcmc": str(row.get("kcmc") or row.get("courseName") or ""),
            "xf": str(row.get("xf") or row.get("credit") or ""),
            "teacher": JwglxtClient.clean_teacher(
                row.get("jsxm") or row.get("teacher") or row.get("jsgh")
                or row.get("jsxx") or ""
            ),
            # jxbrl = 教学班容量（实测某高校的教学班接口就是这个名字），
            # yxzrs = 已选人数，两个都出现时相减即余量。
            "capacity": JwglxtClient._int_of(
                row.get("jxbrl") or row.get("jxbrs") or row.get("zrs") or row.get("capacity")
            ),
            "enrolled": JwglxtClient._int_of(
                row.get("yxzrs") or row.get("yxrs") or row.get("selectedCount")
            ),
            "time_place": str(row.get("sksj") or row.get("jxdd") or row.get("timeAndPlace") or ""),
            # 提交选课要带的教学班总学时/总人数标记，页面 onclick 里叫 jxbzls
            "jxbzls": str(row.get("jxbzls") or ""),
            # 提交体里的 cxbj / xxkbj 在页面上是**每门课**的隐藏域
            # （`cxbj_<kch_id>`、`xxkbj_<kch_id>`），值直接来自搜索结果行。
            "cxbj": str(row.get("cxbj") if row.get("cxbj") is not None else "0"),
            "xxkbj": str(row.get("xxkbj") if row.get("xxkbj") is not None else "0"),
            "xkkz_id": xkkz_id,
            "kklxdm": kklxdm,
            "raw": row,
        }

    @staticmethod
    def _int_of(value: Any, default: int = -1) -> int:
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return default

    @staticmethod
    def clean_teacher(text: Any) -> str:
        """把教师字段洗成人名。

        实测某高校教学班接口的 `jsxx` 形如 `200001/张明/无` 或
        `200002/李华/副教授`（工号/姓名/职称），直接显示很难看；
        取中间那段。括号包裹的职称（`张明(教授)`）、纯姓名、`--` 等
        其它形态原样返回，不做过度加工。
        """
        raw = str(text or "").strip()
        if not raw or raw in ("--", "无"):
            return raw
        parts = [p.strip() for p in raw.split("/")]
        if len(parts) >= 2 and parts[1] and parts[1] not in ("无", "--"):
            return parts[1]
        # `张明(教授)` 这类形态：去掉括号后缀
        if "(" in raw and raw.endswith(")"):
            return raw.split("(")[0].strip()
        return raw

    def get_jxb_ids(self, kch: str, xkkz_id: str, keyword: str, kklxdm: str,
                    kch_id: str = "") -> List[Dict[str, Any]]:
        """取某课程号下的所有教学班（含 do_jxb_id 与余量）。

        xkkz_id 是**有时效性**的，每次抢课前都要现取，不可缓存。

        `kch_id` 是**必须**的：实测（某高校）这个接口不接受课程号 `kch`
        （`12070001A` 这种），传了就回空数组；它要的是搜索结果 raw 里的
        `kch_id`（`4DAD111B9D0A624DE0635D01020AB8DF` 这种十六进制串）。
        没传 `kch_id` 时退化为用 `kch` —— 对按课程号实现的学校仍然可用。
        """
        body = self._search_body(keyword, kklxdm)
        body.update({
            "kch_id": kch_id or kch,
            "xkkz_id": xkkz_id,
            "xkkz_id_": xkkz_id,
            "rlkz": self.effective_param("rlkz", "0"),
            "cdrlkz": self.effective_param("cdrlkz", "0"),
            "rlzlkz": self.effective_param("rlzlkz", "1"),
            "cxbj": self.effective_param("cxbj", "0"),
            "fxbj": self.effective_param("fxbj", "0"),
            "kzybkxy": self.effective_param("kzybkxy", "0"),
            "kzkcgs": self.effective_param("kzkcgs", "0"),
            "sfkkjyxdxnxq": "0",
            "sfxskssj": self.effective_param("sfxskssj", "0"),
        })
        # 某些学校把教学班接口换过路径，这里同样按候选顺序尝试（去重后）
        candidates = [
            self.JXB,
            "/jwglxt/xsxk/zzxkyzb_cxJxbWithKchZzxkYzb.html",
            "/jwglxt/xsxk/zzxkyzb_cxJxbWithKch.html",
        ]
        rows: List[Dict[str, Any]] = []
        last_error: Optional[JwglxtError] = None
        for path in dict.fromkeys(candidates):
            try:
                data = self._post_json(path + self._su_query(), body)
            except SessionExpired:
                raise
            except JwglxtError as exc:
                last_error = exc
                continue
            if isinstance(data, list):
                rows = [d for d in data if isinstance(d, dict)]
            elif isinstance(data, dict):
                rows = self._extract_tmp_list(data)
            if rows:
                break
        if not rows and last_error:
            self.log("warn", f"取教学班失败：{last_error}")
        out: List[Dict[str, Any]] = []
        for row in rows:
            item = self.normalize_row(row, kklxdm, xkkz_id)
            if not item:
                continue
            item["do_jxb_id"] = str(row.get("do_jxb_id") or row.get("jxb_id") or item["jxb"] or "")
            out.append(item)
        return out

    def enrich_rows(self, rows: List[Dict[str, Any]],
                    on_progress: Optional[Callable[[int, int, str], None]] = None,
                    should_stop: Optional[Callable[[], bool]] = None) -> List[Dict[str, Any]]:
        """为搜索结果补齐教师 / 容量 / 已选人数 / 上课时间，并按教学班展开成多行。

        **为什么需要它**：搜索接口（cxZzxkYzbPartDisplay）只回课程级字段
        （课程号、课程名、学分），**不含教师、容量、时间** —— 这些都在教学班
        接口里。所以「同一门课由不同老师开」在搜索结果里长得一模一样，用户根本
        分辨不出来，条数也对不上网站（网站是一行一门课，点开才选教学班）。

        本方法对每门课调一次教学班接口，把结果**展开成「一个教学班一行」**：
        同一门课有 2 个老师就产出 2 行，各自带自己的教师/容量/余量/时间。

        :param on_progress: 回调 `(已完成, 总数, 当前课程名)`，用于界面进度
        :param should_stop: 返回 True 时提前收工（用户点了取消）
        """
        # 按「课程 + 轮次 + 类型」去重，同一门课只请求一次教学班接口
        groups: "OrderedDict[Tuple[str, str, str], List[Dict[str, Any]]]" = OrderedDict()
        for row in rows:
            key = (
                str(row.get("kch_id") or row.get("kch") or ""),
                str(row.get("xkkz_id") or ""),
                str(row.get("kklxdm") or ""),
            )
            groups.setdefault(key, []).append(row)

        total = len(groups)
        out: List[Dict[str, Any]] = []
        done = 0
        for (kch_id, xkkz_id, kklxdm), group in groups.items():
            head = group[0]
            if should_stop and should_stop():
                out.extend(group)
                continue
            try:
                jxbs = self.get_jxb_ids(
                    head.get("kch", ""), xkkz_id, "", kklxdm, kch_id=kch_id
                )
            except SessionExpired:
                raise
            except JwglxtError as exc:
                self.log("warn", f"补齐「{head.get('kcmc')}」教学班信息失败：{exc}")
                jxbs = []
            done += 1
            if on_progress:
                on_progress(done, total, str(head.get("kcmc") or ""))
            if not jxbs:
                # 取不到就保留搜索行，宁可少信息也不要丢课程
                out.extend(group)
                continue
            for jxb in jxbs:
                out.append(self._merge_jxb_row(head, jxb))
        return out

    @staticmethod
    def _merge_jxb_row(row: Dict[str, Any], jxb: Dict[str, Any]) -> Dict[str, Any]:
        """把教学班信息合到搜索行上：课程级字段用搜索行的，教学班级字段用教学班的。"""
        merged = dict(row)
        for key in ("jxb", "do_jxb_id", "teacher", "capacity", "enrolled", "time_place"):
            val = jxb.get(key)
            if val not in (None, ""):
                merged[key] = val
        # jxbzls（父子教学班标记）教学班接口常回空串，此时保留搜索行的值
        if jxb.get("jxbzls"):
            merged["jxbzls"] = jxb["jxbzls"]
        merged["raw"] = {**row.get("raw", {}), **jxb.get("raw", {})}
        return merged

    @staticmethod
    def build_kcmc(kch: str, kcmc: str, xf: str) -> str:
        """构造选课接口需要的 kcmc：『(课程号)课程名 - 学分 学分』。"""
        name = kcmc or ""
        # 去掉可能重复的前缀
        name = re.sub(r"^[（(][^）)]{4,}[）)]\s*", "", name)
        if xf:
            return f"({kch}){name} - {xf} 学分"
        return f"({kch}){name}"

    def build_submit_body(self, kch_id: str, jxb_ids: str, xkkz_id: str, kcmc: str,
                          kklxdm: str = "10", cxbj: str = "0", xxkbj: str = "0",
                          qz: str = "0") -> Dict[str, Any]:
        """按浏览器 `saveCourse()` 的原样拼提交体（只有 `SUBMIT_FIELDS` 那 19 项）。

        逐项对应关系：
            jxb_ids  ← do_jxb_id（教学班内部 id，**有时效性**，每次都要现取）
            kch_id   ← 课程内部 id
            kcmc     ← `$("#kcmc_"+kch_id).text()`，形如 "(课程号)课程名 - X.X 学分"
            rwlx / rlkz / cdrlkz / rlzlkz / xklc / xkxnm / xkxqm / njdm_id /
            zyh_id / xkkz_id  ← 选课页隐藏域
            cxbj / xxkbj ← **每门课**的隐藏域 `cxbj_<kch_id>` / `xxkbj_<kch_id>`
            sxbj     ← `rlkz=="1" or cdrlkz=="1" or rlzlkz=="1"` ? "1" : "0"
            qz       ← 权重值（非权重选课恒为 "0"）
            jcxx_id  ← 教材 id 列表，没选教材时是空串
        """
        self.ensure_page_context()
        rlkz = str(self.effective_param("rlkz", "0") or "0")
        cdrlkz = str(self.effective_param("cdrlkz", "0") or "0")
        rlzlkz = str(self.effective_param("rlzlkz", "0") or "0")
        sxbj = "1" if "1" in (rlkz, cdrlkz, rlzlkz) else "0"
        return {
            "jxb_ids": jxb_ids,
            "kch_id": kch_id,
            "kcmc": kcmc,
            "rwlx": str(self.effective_param("rwlx", "2") or "2"),
            "rlkz": rlkz,
            "cdrlkz": cdrlkz,
            "rlzlkz": rlzlkz,
            "sxbj": sxbj,
            "xxkbj": str(xxkbj or "0"),
            "qz": str(qz or "0"),
            "cxbj": str(cxbj or "0"),
            "xkkz_id": xkkz_id,
            "njdm_id": str(self.effective_param("njdm_id", "") or ""),
            "zyh_id": str(self.effective_param("zyh_id", "") or ""),
            "kklxdm": kklxdm,
            "xklc": str(self.effective_param("xklc", "") or ""),
            "xkxnm": str(self.effective_param("xkxnm", "") or ""),
            "xkxqm": str(self.effective_param("xkxqm", "") or ""),
            "jcxx_id": "",
        }

    @staticmethod
    def _flag_text(flag: str) -> str:
        """flag 没有 msg 时补一句人话（语义取自 `saveCourse()` 的分支）。"""
        return {
            "1": "选课成功",
            "6": "选课成功",
            "3": "选课成功（免费学分已达上限）",
            "-1": "该教学班已无余量，不可选",
            "2": "上课时间冲突",
        }.get(flag, "服务端未说明原因")

    @staticmethod
    def _no_flag_result(data: Any, status: int, endpoint: str) -> Dict[str, Any]:
        """把「回包里没有 flag」如实翻译成一条可读的诊断信息。

        **实测某高校**：请求打到本模块下不存在的处理器时，服务端回的是
        HTTP 910 + 一个通用空信封（`jgpxzd`/`listnav`/`pageable`/`queryModel`/
        `rangeable`/`totalResult`/`userModel`），里面根本没有 flag。
        旧代码用 `data.get("flag", "0")` 把它伪装成「flag=0 且没有 msg」，
        让用户以为是业务规则拒了他 —— 必须如实报出来。
        """
        keys = ",".join(list(data)[:8]) if isinstance(data, dict) else type(data).__name__
        if status == 910:
            msg = (f"服务端没有进入选课流程：HTTP 910（{endpoint} 下没有这个处理器或"
                   f"被网关拦下），回包是通用空信封。")
        else:
            msg = f"服务端没有返回选课结果（HTTP {status}，回包字段：{keys}）"
        return {"flag": "0", "msg": msg, "success": False,
                "no_flag": True, "http_status": status, "endpoint": endpoint}

    def submit(self, kch_id: str, jxb_ids: str, xkkz_id: str, kcmc: str,
               kklxdm: str = "10", kch: str = "", jxbzls: str = "",
               extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """提交选课（自主选课 / 计划内选课）。

        返回 `{"flag", "msg", "success"}`。flag 语义**直接来自** `saveCourse()`
        的成功/失败分支：

        ==========  ==========================================
        "1" / "6"   选课成功
        "3"         选课成功，免费学分已达上限
        "-1"        容量超出（该教学班已无余量）
        "2"         上课时间冲突
        其它        服务端异常（带原文 msg）
        ==========  ==========================================

        端点优先用 `SUBMIT`（`zzxkyzbjk_xkBcZyZzxkYzb.html`，浏览器真实使用的
        那个）。只有在前一次**回包里没有 flag**（说明打错了处理器）时才换下一个
        候选，所以一次成功提交不会被重复发送。

        **实测（某高校）踩到的最后一个坑**：这个接口的权限网关认的是
        **查询串里的 `gnmkdm`**。同一份提交体，
        `POST .../zzxkyzbjk_xkBcZyZzxkYzb.html`（不带 gnmkdm）
        → `{"flag":"0","msg":"无操作权限！"}`，而且它**先判权限再判参数**：
        把 xkkz_id / jxb_ids / kch_id 全换成假值、换 Referer、去掉
        X-Requested-With 或 Origin，回的仍然是同一句话；
        加上 `?gnmkdm=N253512&su=<学号>` 后，同一份提交体 → `{"flag":"1"}`，
        该教学班的已选人数当场从 691 变成 692。
        所以**必须带查询串** —— 这就是「点抢课抢不上」的真正分水岭。
        """
        extra = extra or {}
        body = self.build_submit_body(
            kch_id, jxb_ids, xkkz_id, kcmc, kklxdm,
            cxbj=str(extra.get("cxbj", "0") or "0"),
            xxkbj=str(extra.get("xxkbj", "0") or "0"),
            qz=str(extra.get("qz", "0") or "0"),
        )
        # 首选＝带 gnmkdm/su 的查询串（实测必需），其次是不带查询串的裸路径，
        # 最后才是旧代码那条路径。顺序即优先级。
        plan = (
            (self.SUBMIT + self._su_query(), self.SUBMIT),
            (self.SUBMIT, self.SUBMIT + "#noquery"),
            (self.SUBMIT_LEGACY + self._su_query(), self.SUBMIT_LEGACY),
        )
        tried: List[str] = []
        last: Optional[Dict[str, Any]] = None
        for endpoint, path in plan:
            if path in tried:
                continue
            tried.append(path)
            try:
                data = self._post_json(endpoint, body)
            except JwglxtError as exc:
                last = {"flag": "0", "msg": f"请求 {path} 失败：{exc}",
                        "success": False, "endpoint": endpoint}
                continue
            status = getattr(self, "_last_status", 200)
            if isinstance(data, dict) and "flag" in data:
                flag = str(data.get("flag", "0"))
                msg = str(data.get("msg") or data.get("message") or "")
                result = {
                    "flag": flag,
                    "msg": msg or self._flag_text(flag),
                    "success": flag in self.SUBMIT_OK_FLAGS,
                    "endpoint": endpoint,
                }
                if self._submit_retryable(result):
                    last = result
                    continue
                return result
            last = self._no_flag_result(data, status, endpoint)
        if last:
            last["tried"] = ",".join(tried)
        return last or {"flag": "0", "msg": "服务端没有返回选课结果", "success": False}

    @staticmethod
    def _submit_retryable(result: Dict[str, Any]) -> bool:
        """这个回包是在说「请求根本没进业务逻辑」吗？只有这种情况才换端点重试。

        * `无操作权限！` —— 权限网关拦下（实测＝查询串缺 `gnmkdm`）；
        * `出现未知异常` —— 打到了同名但用途不同的处理器。

        两者都发生在写库之前，所以重试**不会**造成重复选课。
        （重复提交同一门课在新正方上是有退课风险的，这个判断必须保守。）
        """
        if result.get("success"):
            return False
        msg = str(result.get("msg") or "")
        return ("无操作权限" in msg) or ("出现未知异常" in msg)

    def check_stage(self) -> Dict[str, Any]:
        """判断「现在是否在选课时间」——用一个**永远不可能选上课**的探针。

        做法：拿**真实的 xkkz_id**（选课轮次）往提交接口发一份提交体，但把
        `jxb_ids` / `kch_id` 换成不存在的假值。假教学班 id 匹配不上任何教学班，
        所以无论服务端怎么处理，都不可能真的选上课程。

        实测（某高校，选课进行中，把真值替换成假值后）：
            {"msg": "出现未知异常，请与管理员联系！", "flag": "0"}
        —— 注意它**没有**说「当前时间不可选课」，也就是时间闸门放行了，
        失败只是因为我故意给了假参数。反过来，不在选课时间时服务端会先回
        「对不起，当前时间不可选课！」。据此判断：

        * 回包出现「不可选课 / 选课时间 / 未开始 / 已结束」等字样 → 未开放；
        * 回 `无操作权限！` 或没有 `flag`（HTTP 910）→ **问不出来**（None），
          绝不猜；
        * 其它任何业务回包（含 -1 人数已满、2 时间冲突、未知异常）→ 已开放。

        ⚠️ 这里**不能**用「一键选课」接口 `zzxkyzb_xkZzxkyzbQuickly.html` 来探：
        它对假 xkkz_id 一律回「当前时间不可选课！」（先判轮次合法性，与时间无关，
        会让人误以为选课关闭），而对真 xkkz_id 一旦某项功能真的开着，
        它会**把志愿里的课全选一遍**。已废弃，见 `STAGE_CHECK`。
        """
        xkkz_id = self.effective_param("xkkz_id", "") or \
            self.xkkz_map.get(str(self.cfg.get("kklxdm") or "10"), "")
        if not xkkz_id:
            try:
                self.ensure_page_context(force=True)
            except JwglxtError as exc:
                return {"open": None, "msg": f"无法判断选课阶段：取不到选课轮次（{exc}）"}
            xkkz_id = self.effective_param("xkkz_id", "") or \
                self.xkkz_map.get(str(self.cfg.get("kklxdm") or "10"), "")
        if not xkkz_id:
            return {"open": None, "msg": "无法判断选课阶段：页面上没有 xkkz_id"}

        body = self.build_submit_body(
            "ZFG_STAGE_PROBE_KCH", "ZFG_STAGE_PROBE_JXB", xkkz_id,
            "(ZFG_STAGE_PROBE)阶段探测 - 0.0 学分", str(self.cfg.get("kklxdm") or "10"),
        )
        try:
            data = self._post_json(self.SUBMIT + self._su_query(), body)
        except JwglxtError as exc:
            return {"open": None, "msg": f"无法判断选课阶段：{exc}"}
        status = getattr(self, "_last_status", 200)
        if not isinstance(data, dict) or "flag" not in data:
            return {"open": None,
                    "msg": f"无法判断选课阶段：服务端没有返回业务内容（HTTP {status}）"}
        msg = str(data.get("msg") or "")
        if "无操作权限" in msg:
            return {"open": None,
                    "msg": "无法判断选课阶段：请求被权限网关拦下（无操作权限）。"}
        closed_words = ("不可选课", "不属于选课阶段", "不在选课", "选课时间",
                        "未开始", "已结束", "选课未开放")
        if any(word in msg for word in closed_words):
            return {"open": False, "msg": msg}
        return {"open": True,
                "msg": f"选课已开放（阶段探针回：{msg or '无异常'}）"}

    def fetch_choosed(self) -> str:
        """获取已选课程页面 HTML（用于校验/展示）。"""
        gnmkdm = self.cfg.get("gnmkdm") or "N253512"
        path = f"{self.CHOOSED_PAGE}?gnmkdm={gnmkdm}&su={self.su}"
        resp = self._request("GET", path, allow_redirects=True)
        return resp.text

    # ------------------------------------------------------------------ #
    # 参数自动探测
    # ------------------------------------------------------------------ #
    def autodetect(self) -> Dict[str, Any]:
        """从选课页与搜索接口尽力推断 njdm_id / zyh_id / bh_id 等参数。"""
        self.ensure_login()
        html = self.fetch_xsxk_page()
        found: Dict[str, Any] = {}

        if self.xkkz_map:
            # 通用值（解析不出课程类型时）映射到当前课程类型，方便界面直接回填
            found["xkkz_map"] = {
                (k if k != "*" else str(self.cfg.get("kklxdm") or "10")): v
                for k, v in self.xkkz_map.items()
            }

        # 直接出现在页面里的字段
        for field in ("njdm_id", "zyh_id", "bh_id", "jg_id", "xqh_id", "xkxnm", "xkxqm"):
            m = re.search(
                rf'["\']?{field}["\']?\s*[:=]\s*["\']([A-Za-z0-9\-_]+)["\']', html
            )
            if m:
                found[field] = m.group(1)

        # 从学号推断年级与班级号：如 2023195103 -> 年级 2023，班级 23195103
        if self.su and re.fullmatch(r"\d{8,14}", self.su):
            if "njdm_id" not in found:
                found["njdm_id"] = self.su[:4]
            if "bh_id" not in found:
                found["bh_id"] = self.su[2:10] if len(self.su) >= 10 else self.su[2:]

        # 空搜索一次，看看能否从返回行里补出 zyh_id 等
        try:
            rows = self.search_courses("")
            if rows:
                raw = rows[0].get("raw") or {}
                for field in ("zyh_id", "njdm_id", "bh_id"):
                    if field not in found and raw.get(field):
                        found[field] = str(raw[field])
        except JwglxtError as exc:
            self.log("warn", f"自动探测搜索接口失败：{exc}")

        return found
