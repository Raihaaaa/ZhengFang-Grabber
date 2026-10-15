# -*- coding: utf-8 -*-
"""配置管理：读写 config.json，提供默认值与账号/通知设置。

配置文件与程序同目录（打包成 exe 后为 exe 同目录），
首次运行自动生成默认配置。
"""
from __future__ import annotations

import json
import os
import sys
import threading
from typing import Any, Dict


def app_dir() -> str:
    """程序所在目录（兼容 PyInstaller 打包）。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


CONFIG_PATH = os.path.join(app_dir(), "config.json")

DEFAULT_CONFIG: Dict[str, Any] = {
    # ---------------- 账号 ----------------
    "base_url": "https://jwglxt.example.edu.cn",   # 教务系统根地址，末尾不要带 /
    # 教务系统挂在网站子路径时的前缀。取值语义：
    #   "auto"（默认） = 自动探测（探测结果会立刻写回配置，下次直接复用）
    #   ""            = 明确表示挂在根目录（没有 /jwglxt）
    #   "/xxx"        = 明确指定子路径（多数新正方是 "/jwglxt"）
    # 有的学校（如 https://jwxt.example.edu.cn/xtgl/login_slogin.html）就在根目录，
    # 探测到就是空串 —— 所以"留空"和"自动"必须是两个不同的值，不能混用。
    "app_prefix": "auto",
    # 记下 app_prefix 是给哪个域名探出来的；base_url 一变就自动作废、重新探测。
    "app_prefix_for": "",
    "username": "",                                  # 学号
    "password": "",                                  # 密码
    "remember_password": True,

    # ---------------- 选课参数（多数学校可留默认，登录后可自动探测） ----------------
    "gnmkdm": "N253512",       # 选课菜单号
    "xkxnm": "2025",           # 选课学年，如 2025
    "xkxqm": "12",             # 选课学期：12=第一学期 3=第二学期（5-8月为3，否则12）
    "njdm_id": "",             # 年级代码，如 2023（留空自动从学号推断）
    "zyh_id": "",              # 专业代号，如 1951（留空自动探测）
    "bh_id": "",               # 班级号，如 23195101（留空自动从学号推断）
    "jg_id": "0102",           # 学院/校区代码
    "xqh_id": "1",             # 校区号
    "kklxdm": "10",            # 课程类型代码
    "rwlx": "2",
    "xslbdm": "421",
    "ccdm": "3",
    "xbm": "1",
    "xsbj": "4294967296",
    "sfrxtgkcxd": "1",
    "tykczgxdcs": "10",
    "zyfx_id": "wfx",
    "kkbk": "0",
    "kkbkdj": "0",
    "xkly": "0",
    "bklx_id": "0",
    "sfkcfx": "0",
    "sfkgbcx": "0",
    "sfkknj": "0",
    "sfkkzy": "0",
    "sfkxq": "0",
    "sfznkx": "0",
    "zdkxms": "0",
    "xklc": "2",               # 选课轮次
    "rlzlkz": "1",
    "rlkz": "0",
    "cxbj": "0",
    "fxbj": "0",
    "kzybkxy": "0",
    "kklxdm_candidates": ["10", "16", "11", "12", "13"],  # 自动扫描的课程类型

    # ---------------- 抢课行为 ----------------
    "poll_interval": 1.2,      # 轮询间隔（秒），建议 >= 1.0 免得被风控
    "monitor_interval": 0,     # 同一门课两次刷新间隔（秒），0=每轮都刷
    "request_timeout": 8,      # 单次请求超时（秒）
    "max_retries": 3,          # 网络错误重试次数
    "max_attempts_per_round": 20,  # 每门课每轮最多提交次数
    "concurrent_submit": 1,    # 命中余量时的并发提交数
    "start_time": "",          # 定时开抢，格式 YYYY-MM-DD HH:MM:SS，留空=立即
    "stop_when_all_done": True,# 所有课程抢到后自动停止
    "submit_even_if_full": False,  # 余量为0时也尝试提交（部分学校允许排队/候补）
    # 搜索结果本身不含教师/容量/时间，要靠逐门课查教学班补全；
    # 打开后搜索完自动补全（约每门课一次请求，50 门课 ~10 秒）。
    "auto_enrich": True,

    # ---------------- 通知 ----------------
    "notify_desktop": True,
    "notify_email": False,
    "smtp_host": "smtp.qq.com",
    "smtp_port": 465,
    "smtp_user": "",
    "smtp_pass": "",           # QQ 邮箱为授权码，不是登录密码
    "mail_to": "",
    "notify_webhook": False,
    "webhook_url": "",         # Server酱 / PushPlus 等
    "webhook_body_key": "text",# 推送服务的正文字段名，Server酱为 text，PushPlus 为 content

    # ---------------- 调试 ----------------
    "verbose_log": True,
    "save_debug_html": False,

    # ---------------- 自动学习 ----------------
    # 程序自动记录「发到搜索接口会让服务端回 HTML 错误页」的字段，下次启动直接跳过。
    # 每次搜索都会先按这一份剔除；清空（[]）后下次遇到报错会重新二分探测。
    # localeKey / cdTsxx / pageNumber 是已知的页面渲染状态，默认就排除。
    "bad_keys": ["localeKey", "cdTsxx", "pageNumber"],
}

_lock = threading.RLock()


def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """把 override 合并进 base 的副本（仅一层足够，配置结构是扁平的）。"""
    out = dict(base)
    for k, v in (override or {}).items():
        out[k] = v
    return out


def load_config(path: str | None = None) -> Dict[str, Any]:
    path = path or CONFIG_PATH
    with _lock:
        if not os.path.exists(path):
            save_config(DEFAULT_CONFIG, path)
            return dict(DEFAULT_CONFIG)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("config.json 根节点必须是对象")
        except Exception:
            # 配置损坏时回退默认值，但备份原文件避免用户数据丢失
            try:
                os.replace(path, path + ".bak")
            except OSError:
                pass
            save_config(DEFAULT_CONFIG, path)
            return dict(DEFAULT_CONFIG)
        return deep_merge(DEFAULT_CONFIG, data)


def save_config(cfg: Dict[str, Any], path: str | None = None) -> None:
    path = path or CONFIG_PATH
    with _lock:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)


class Config:
    """配置对象：支持属性式访问与自动保存。"""

    def __init__(self, path: str | None = None):
        self.path = path or CONFIG_PATH
        self._data = load_config(self.path)

    def __getitem__(self, key: str) -> Any:
        return self._data.get(key, DEFAULT_CONFIG.get(key))

    def __setitem__(self, key: str, value: Any) -> None:
        self._data[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, DEFAULT_CONFIG.get(key, default))

    def update(self, mapping: Dict[str, Any]) -> None:
        self._data.update(mapping or {})

    def as_dict(self) -> Dict[str, Any]:
        return dict(self._data)

    def save(self) -> None:
        save_config(self._data, self.path)
