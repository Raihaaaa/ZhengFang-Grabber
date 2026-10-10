# -*- coding: utf-8 -*-
"""通知模块：桌面弹窗 / 邮件 / Webhook（Server酱、PushPlus 等）。

所有发送都是尽力而为，失败只记录日志，绝不打断抢课主流程。
"""
from __future__ import annotations

import json
import smtplib
import ssl
import threading
from email.header import Header
from email.mime.text import MIMEText
from typing import Any, Dict

try:
    import requests
    _HAS_REQUESTS = True
except ImportError:  # pragma: no cover
    _HAS_REQUESTS = False


class Notifier:
    def __init__(self, cfg: Dict[str, Any], logger=None):
        self.cfg = cfg
        self.log = logger or (lambda msg, level="info": None)
        self._desktop_warned = False

    # ------------------------------------------------------------------ #
    # 对外入口
    # ------------------------------------------------------------------ #
    def send(self, title: str, message: str, async_: bool = True) -> None:
        """按配置把通知并发发往所有已启用的渠道。"""
        jobs = []
        if self.cfg.get("notify_desktop"):
            jobs.append(self._desktop)
        if self.cfg.get("notify_email"):
            jobs.append(self._email)
        if self.cfg.get("notify_webhook"):
            jobs.append(self._webhook)

        def run():
            threads = []
            for job in jobs:
                t = threading.Thread(target=self._safe, args=(job, title, message), daemon=True)
                t.start()
                threads.append(t)
            for t in threads:
                t.join(timeout=20)

        if async_:
            threading.Thread(target=run, daemon=True).start()
        else:
            run()

    def _safe(self, func, title: str, message: str) -> None:
        try:
            func(title, message)
        except Exception as exc:  # noqa: BLE001
            self.log("warn", f"通知发送失败（{func.__name__}）：{exc}")

    # ------------------------------------------------------------------ #
    # 桌面通知
    # ------------------------------------------------------------------ #
    def _desktop(self, title: str, message: str) -> None:
        try:
            from plyer import notification  # type: ignore
            notification.notify(
                title=title,
                message=message[:250],
                app_name="正方抢课助手",
                timeout=10,
            )
            return
        except Exception:
            pass

        # 退路 1：Windows 原生弹窗（ctypes，无需第三方库）
        if self._win_toast(title, message):
            return

        if not self._desktop_warned:
            self._desktop_warned = True
            self.log("warn", "桌面通知不可用：可执行 pip install plyer 启用弹窗。")

    @staticmethod
    def _win_toast(title: str, message: str) -> bool:
        try:
            import ctypes
            # MB_ICONINFORMATION | MB_SETFOREGROUND | MB_TOPMOST
            flags = 0x40 | 0x10000 | 0x40000
            ctypes.windll.user32.MessageBoxW(0, message[:500], title[:100], flags)
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------ #
    # 邮件
    # ------------------------------------------------------------------ #
    def _email(self, title: str, message: str) -> None:
        host = self.cfg.get("smtp_host")
        port = int(self.cfg.get("smtp_port") or 465)
        user = self.cfg.get("smtp_user")
        password = self.cfg.get("smtp_pass")
        to_addr = self.cfg.get("mail_to") or user
        if not (host and user and password and to_addr):
            raise ValueError("邮件通知配置不完整（需要 smtp_user / smtp_pass / mail_to）")

        msg = MIMEText(message, "plain", "utf-8")
        msg["Subject"] = Header(title, "utf-8")
        msg["From"] = user
        msg["To"] = to_addr

        if port == 465:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(host, port, timeout=20, context=context) as server:
                server.login(user, password)
                server.sendmail(user, [a.strip() for a in to_addr.split(",")], msg.as_string())
        else:
            with smtplib.SMTP(host, port, timeout=20) as server:
                server.ehlo()
                server.starttls(context=ssl.create_default_context())
                server.login(user, password)
                server.sendmail(user, [a.strip() for a in to_addr.split(",")], msg.as_string())
        self.log("info", f"邮件通知已发送到 {to_addr}")

    # ------------------------------------------------------------------ #
    # Webhook（Server酱 / PushPlus / 企业微信机器人 / 自定义）
    # ------------------------------------------------------------------ #
    def _webhook(self, title: str, message: str) -> None:
        if not _HAS_REQUESTS:
            raise RuntimeError("缺少 requests，无法发送 webhook")
        url = self.cfg.get("webhook_url")
        if not url:
            raise ValueError("webhook_url 未配置")

        key = self.cfg.get("webhook_body_key") or "text"
        payload: Dict[str, Any] = {}
        payload[key] = f"{title}\n{message}"

        lower = url.lower()
        if "sctapi.ftqq.com" in lower or "server" in lower:
            payload = {"title": title, "desp": message}
        elif "pushplus" in lower:
            payload = {"title": title, "content": message, "template": "txt"}
        elif "qyapi.weixin.qq.com" in lower:  # 企业微信机器人
            payload = {"msgtype": "text", "text": {"content": f"{title}\n{message}"}}
        elif "oapi.dingtalk.com" in lower:    # 钉钉机器人
            payload = {"msgtype": "text", "text": {"content": f"{title}\n{message}"}}

        resp = requests.post(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
            timeout=15,
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:120]}")
        self.log("info", "Webhook 通知已发送")


def build_notifier(cfg: Dict[str, Any], logger=None) -> Notifier:
    return Notifier(cfg, logger)
