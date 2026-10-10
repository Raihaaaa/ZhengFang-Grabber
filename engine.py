# -*- coding: utf-8 -*-
"""抢课引擎：课程监控 + 余量检测 + 并发提交 + 定时开抢 + 多志愿优先级。

核心流程（严格复刻经实测验证的协议顺序）：
    每一轮对每门志愿课程：
      1. POST zzxkyzbjk_cxJxbWithKchZzxkYzb.html   取最新 do_jxb_id 与余量
          —— 教学班 id 是时效性的，必须每次重新获取，不能缓存！
      2. 若余量 > 0（或配置要求无余量也提交），
         POST zzxkyzbjk_xkBcZyZzxkYzb.html?gnmkdm=N253512&su=<学号> 提交
         —— **必须带 gnmkdm 查询串**，否则权限网关回「无操作权限！」；
         提交体只有 19 个字段，见 JwglxtClient.SUBMIT_FIELDS。
      3. flag ∈ {1,6} 抢到；3 = 抢到但免费学分到顶；-1 人数已满；
         2 上课时间冲突；0 且 msg=无操作权限 → 请求没进业务逻辑。
"""
from __future__ import annotations

import json
import os
import random
import threading
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from config import app_dir
from jwglxt import JwglxtClient, JwglxtError, LoginError, SessionExpired
from notifier import Notifier

WISHLIST_PATH = os.path.join(app_dir(), "wishlist.json")


def _is_success(result: Optional[Dict[str, Any]]) -> bool:
    """判断一次提交是否真的抢到了课。

    服务端的成功标志位不止一个：`saveCourse()` 把 `flag` 为 **1 / 6 / 3**
    都当成功（其中 3 = 「选课成功，免费学分已达上限」）。`JwglxtClient.submit()`
    已经把这三个归成 `success=True`，这里再兜一层，兼容测试里的假服务器
    与手工构造的返回值。
    """
    if not isinstance(result, dict) or result.get("success"):
        return bool(isinstance(result, dict) and result.get("success"))
    return str(result.get("flag") or "") in JwglxtClient.SUBMIT_OK_FLAGS


# --------------------------------------------------------------------------- #
# 志愿课程
# --------------------------------------------------------------------------- #
@dataclass
class Course:
    """一门志愿课程（对应一个教学班）。"""

    course_id: str                 # 唯一 id（kch + jxb）
    kch: str = ""                  # 课程号（12070001A 这种）
    # 课程 ID：取教学班接口 zzxkyzbjk_cxJxbWithKchZzxkYzb.html 要的是这个，
    # 实测传课程号会返回空数组。搜索结果的 raw 里叫 kch_id。
    kch_id: str = ""
    kcmc: str = ""                 # 课程名
    teacher: str = ""              # 教师
    xf: str = ""                   # 学分
    jxb: str = ""                  # 教学班号
    xkkz_id: str = ""              # 选课轮次标识
    kklxdm: str = "10"             # 课程类型代码
    enabled: bool = True
    priority: int = 1              # 越小越优先
    capacity: int = -1
    enrolled: int = -1
    # 持久状态：本课程是否已经抢到（写进 JSON，重启程序后依然有效）
    grabbed: bool = False
    # 运行期统计
    attempts: int = 0
    last_msg: str = ""

    @property
    def seats_left(self) -> int:
        if self.capacity < 0 or self.enrolled < 0:
            return -1
        return self.capacity - self.enrolled

    @property
    def title(self) -> str:
        bits = [self.kcmc or self.kch]
        if self.teacher:
            bits.append(f"[{self.teacher}]")
        return " ".join(bits)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Course":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in (data or {}).items() if k in known})

    @classmethod
    def from_search_item(cls, item: Dict[str, Any]) -> "Course":
        return cls(
            course_id=f"{item.get('kch','')}::{item.get('jxb','')}",
            kch=str(item.get("kch") or ""),
            kch_id=str(item.get("kch_id") or ""),
            kcmc=str(item.get("kcmc") or ""),
            teacher=str(item.get("teacher") or ""),
            xf=str(item.get("xf") or ""),
            jxb=str(item.get("jxb") or ""),
            xkkz_id=str(item.get("xkkz_id") or ""),
            kklxdm=str(item.get("kklxdm") or "10"),
            capacity=int(item.get("capacity", -1)),
            enrolled=int(item.get("enrolled", -1)),
        )


# --------------------------------------------------------------------------- #
# 志愿列表持久化
# --------------------------------------------------------------------------- #
class Wishlist:
    def __init__(self, path: Optional[str] = None):
        # 在调用时解析默认路径（写成默认参数会在 import 时定死，测试/预览无法替换）
        self.path = path or WISHLIST_PATH
        self.courses: List[Course] = []
        self.load()

    def load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.courses = [Course.from_dict(d) for d in data.get("courses", [])]
        except Exception:
            self.courses = []

    def save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(
                {"courses": [c.to_dict() for c in self.courses]},
                f, ensure_ascii=False, indent=2,
            )
        os.replace(tmp, self.path)

    def add(self, course: Course) -> bool:
        """按 course_id 去重加入。返回是否真的新增。"""
        if any(c.course_id == course.course_id for c in self.courses):
            return False
        course.priority = (max([c.priority for c in self.courses], default=0) + 1)
        self.courses.append(course)
        self.save()
        return True

    def remove(self, course_id: str) -> None:
        self.courses = [c for c in self.courses if c.course_id != course_id]
        self.save()

    def sorted_courses(self) -> List[Course]:
        return sorted(self.courses, key=lambda c: (c.priority, c.course_id))

    def move(self, index: int, delta: int) -> int:
        """调整优先级顺序，返回新的下标。"""
        ordered = self.sorted_courses()
        if not (0 <= index < len(ordered)):
            return index
        new_index = max(0, min(len(ordered) - 1, index + delta))
        ordered[index], ordered[new_index] = ordered[new_index], ordered[index]
        for i, course in enumerate(ordered):
            course.priority = i + 1
        self.courses = ordered
        self.save()
        return new_index

    def enabled_courses(self) -> List[Course]:
        return [c for c in self.sorted_courses() if c.enabled]

    def reset_runtime(self) -> None:
        """每次启动前清理运行期统计。

        注意：**不清除 `grabbed`**。`grabbed` 表示「这门课已经选上了」，
        是持久事实；清掉它会让引擎重复提交同一门课，而新正方在计划内
        选课中对已选课程重复提交有机会把课退掉。要重新抢请用
        `clear_grabbed()`（GUI 上的「清除已抢到标记」）。
        """
        for c in self.courses:
            c.attempts = 0
            c.last_msg = ""

    def clear_grabbed(self) -> None:
        """清除所有「已抢到」标记，让用户可以对同一批课重新开抢。"""
        for c in self.courses:
            c.grabbed = False
            c.attempts = 0
            c.last_msg = ""
        self.save()


# --------------------------------------------------------------------------- #
# 引擎
# --------------------------------------------------------------------------- #
class GrabEngine:
    """后台抢课引擎。所有对外通知走回调，便于 GUI 更新。"""

    def __init__(
        self,
        cfg: Dict[str, Any],
        wishlist: Wishlist,
        log_cb: Optional[Callable[[str, str], None]] = None,
        status_cb: Optional[Callable[[str], None]] = None,
        event_cb: Optional[Callable[[str], None]] = None,
        config_path: Optional[str] = None,
    ):
        self.cfg = cfg
        self.wishlist = wishlist
        self.log_cb = log_cb or (lambda msg, level="info": None)
        self.status_cb = status_cb or (lambda status: None)
        self.event_cb = event_cb or (lambda kind: None)
        self.config_path = config_path

        self.client = JwglxtClient(cfg, logger=self.log, config_path=config_path)
        self.notifier = Notifier(cfg, logger=self.log)

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.RLock()

        self.stats: Dict[str, int] = {"rounds": 0, "submits": 0, "success": 0, "errors": 0}

    # ------------------------------------------------------------------ #
    # 对外控制
    # ------------------------------------------------------------------ #
    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def log(self, level: str, message: str) -> None:
        try:
            self.log_cb(message, level)
        except Exception:
            pass

    def start(self) -> None:
        """启动引擎（非阻塞）。"""
        with self._lock:
            if self.running:
                self.log("warn", "引擎已在运行中。")
                return
            if not self.wishlist.enabled_courses():
                raise ValueError("没有已启用的志愿课程，请先在「课程选择」里添加并勾选。")
            if not self.cfg.get("base_url") or not self.cfg.get("username"):
                raise ValueError("请先在「系统配置」里填写教务系统地址、学号和密码。")

            self._stop.clear()
            self.stats = {"rounds": 0, "submits": 0, "success": 0, "errors": 0}
            self.wishlist.reset_runtime()
            self._thread = threading.Thread(target=self._run, name="grab-engine", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.log("info", "已请求停止抢课。")

    def join(self, timeout: Optional[float] = None) -> None:
        if self._thread:
            self._thread.join(timeout=timeout)

    def set_credentials(self, base_url: str, username: str, password: str) -> None:
        """在校验/重连时刷新凭据，并强制下次重新登录。"""
        self.cfg["base_url"] = base_url.rstrip("/")
        self.cfg["username"] = username
        self.cfg["password"] = password
        self.client = JwglxtClient(self.cfg, logger=self.log, config_path=self.config_path)
        self.client.password = password

    # ------------------------------------------------------------------ #
    # 主循环
    # ------------------------------------------------------------------ #
    def _run(self) -> None:
        try:
            self._wait_for_start_time()
            if self._stop.is_set():
                self._finish("已取消")
                return

            self.status_cb("登录中…")
            self.log("info", f"正在登录 {self.cfg.get('base_url')} …")
            try:
                self.client.login()
            except LoginError as exc:
                self.log("error", f"登录失败：{exc}")
                self.notifier.send("抢课助手 · 登录失败", str(exc))
                self._finish("登录失败")
                return

            # 刷新选课页，解析 xkkz_id（并补齐可能缺失的 xkkz_id）
            try:
                self.client.fetch_xsxk_page()
            except JwglxtError as exc:
                self.log("warn", f"打开选课页失败：{exc}（将使用配置中的参数继续）")

            # 先问一次服务端「现在在不在选课时间」。不在也不会停 —— 定时开抢本来
            # 就是提前挂着的 —— 但要**明确告诉用户**，否则界面上只会看到一连串
            # 说不清原因的失败。
            stage = self.client.check_stage()
            if stage.get("open") is False:
                self.log("warn", f"服务端答复：{stage.get('msg')}"
                                 "（现在提交会被拒；到点后本程序会自动成功）")
            elif stage.get("open") is True:
                self.log("info", f"选课时间检查：{stage.get('msg')}")

            self._prepare_courses()

            self.status_cb("监控中")
            self.log("info", f"开始监控 {len(self.wishlist.enabled_courses())} 门课程。")
            self._main_loop()
            self._finish("已停止")
        except Exception as exc:  # noqa: BLE001
            self.log("error", f"引擎异常终止：{exc}")
            self._finish("异常终止")

    def _wait_for_start_time(self) -> None:
        raw = (self.cfg.get("start_time") or "").strip()
        if not raw:
            return
        try:
            target = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            self.log("warn", f"定时开抢时间格式不对（应为 YYYY-MM-DD HH:MM:SS）：{raw}，已忽略。")
            return

        now = datetime.now()
        if target <= now:
            self.log("info", "定时时间已过，立即开始。")
            return

        self.log("info", f"已设定时开抢：{target:%Y-%m-%d %H:%M:%S}，等待中…")
        self.status_cb(f"等待 {target:%H:%M:%S} 开抢")
        while not self._stop.is_set():
            remain = (target - datetime.now()).total_seconds()
            if remain <= 0:
                break
            # 临近 3 秒时细化到 20ms，避免系统调度造成秒级偏差
            if remain > 3:
                nap = min(1.0, remain - 3)
            elif remain > 0.5:
                nap = 0.05
            else:
                nap = 0.005
            self._stop.wait(nap)

        if not self._stop.is_set():
            self.log("info", "定时时间到，开始抢课！")
            self.notifier.send("抢课助手 · 开始", f"已到设定时间，开始抢 {len(self.wishlist.enabled_courses())} 门课程。")

    def _prepare_courses(self) -> None:
        """为每门课程补齐 xkkz_id 与课程类型。"""
        for course in self.wishlist.enabled_courses():
            if course.xkkz_id:
                continue
            candidates = [course.kklxdm] if course.kklxdm else []
            candidates += [c for c in (self.cfg.get("kklxdm_candidates") or []) if c not in candidates]
            for kk in candidates:
                try:
                    course.xkkz_id = self.client.get_xkkz_id(str(kk))
                    course.kklxdm = str(kk)
                    break
                except JwglxtError:
                    continue
            if not course.xkkz_id:
                self.log("warn", f"{course.title}：未取到 xkkz_id，尝试提交时会报错。")
        self.wishlist.save()

    def _main_loop(self) -> None:
        interval = float(self.cfg.get("poll_interval") or 1.2)
        interval = max(0.3, interval)
        monitor_interval = float(self.cfg.get("monitor_interval") or 0)
        last_refresh: Dict[str, float] = {}
        round_attempts: Dict[str, int] = {}
        max_attempts_per_round = int(self.cfg.get("max_attempts_per_round") or 20)

        while not self._stop.is_set():
            self.stats["rounds"] += 1
            courses = self.wishlist.enabled_courses()
            if not courses:
                self.log("info", "没有已启用的课程，退出。")
                return

            now = time.time()
            pending = [c for c in courses if not c.grabbed]
            if not pending:
                self.log("info", "所有志愿课程均已抢到，自动停止。")
                return

            for course in pending:
                if self._stop.is_set():
                    return

                # 监控间隔限制（避免高频请求被风控）；取到余量变化后立即继续
                if monitor_interval > 0:
                    since = now - last_refresh.get(course.course_id, 0)
                    if since < monitor_interval:
                        continue

                self._try_course(course, round_attempts, max_attempts_per_round)
                last_refresh[course.course_id] = time.time()

                if self.cfg.get("stop_when_all_done", True) and all(
                    c.grabbed for c in self.wishlist.enabled_courses()
                ):
                    self.log("info", "全部抢到，结束。")
                    return

            done = sum(1 for c in courses if c.grabbed)
            self.status_cb(f"监控中 {done}/{len(courses)}")
            self._stop.wait(interval + random.uniform(0, interval * 0.15))

    def _try_course(self, course: Course, round_attempts: Dict[str, int],
                    max_attempts: int) -> None:
        """对单门课程执行一轮：刷新教学班 -> 检查余量 -> 提交。"""
        try:
            if not course.xkkz_id:
                self._prepare_one(course)
            keyword = course.kcmc or course.kch
            classes = self.client.get_jxb_ids(
                course.kch, course.xkkz_id, keyword, course.kklxdm,
                kch_id=course.kch_id,
            )
        except SessionExpired:
            self._relogin()
            return
        except JwglxtError as exc:
            self.stats["errors"] += 1
            self.log("warn", f"{course.title}：刷新教学班失败：{exc}")
            return

        if not classes:
            self.log("debug", f"{course.title}：未查到教学班（可能不在本轮次）。")
            return

        # 优先选指定教学班，否则选第一个有余量的
        target = None
        if course.jxb:
            target = next((c for c in classes if c.get("jxb") == course.jxb
                           or c.get("do_jxb_id") == course.jxb), None)
        if target is None:
            target = next((c for c in classes if c.get("capacity", -1) - c.get("enrolled", -1) > 0), None)
        if target is None:
            target = classes[0]

        self._update_quota(course, target)

        seats = course.seats_left
        submit_even_full = bool(self.cfg.get("submit_even_if_full"))
        if seats <= 0 and not submit_even_full:
            self.log("debug", f"{course.title}：已满（{course.enrolled}/{course.capacity}）。")
            return

        attempts_this_round = round_attempts.get(course.course_id, 0)
        if attempts_this_round >= max_attempts:
            self.log("debug", f"{course.title}：本轮提交次数已达上限，等待下一轮。")
            return

        do_jxb_id = target.get("do_jxb_id") or target.get("jxb") or ""
        if not do_jxb_id:
            self.log("warn", f"{course.title}：未能取到 do_jxb_id，跳过。")
            return

        kcmc = self.client.build_kcmc(course.kch, course.kcmc, course.xf or target.get("xf", ""))
        jxbzls = str(target.get("jxbzls") or "")
        concurrency = max(1, int(self.cfg.get("concurrent_submit") or 1))

        if concurrency > 1:
            result = self._concurrent_submit(course, do_jxb_id, kcmc, concurrency, jxbzls, target)
        else:
            result = self._submit_once(course, do_jxb_id, kcmc, jxbzls, target)

        round_attempts[course.course_id] = attempts_this_round + 1
        self.log(
            "info" if _is_success(result) else "debug",
            f"{course.title}：提交结果 flag={result.get('flag')} {result.get('msg') or ''}".strip(),
        )

    def _submit_once(self, course: Course, do_jxb_id: str, kcmc: str,
                     jxbzls: str = "", row: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        course.attempts += 1
        self.stats["submits"] += 1
        # cxbj / xxkbj 在浏览器里是**每门课**的隐藏域（cxbj_<kch_id>、
        # xxkbj_<kch_id>），值来自搜索结果那一行；缺了服务端不一定报错，
        # 但既然客户端会发，我们就照发。
        row = row or {}
        extra = {
            "cxbj": str(row.get("cxbj", "0") or "0"),
            "xxkbj": str(row.get("xxkbj", "0") or "0"),
            "qz": "0",
        }
        try:
            result = self.client.submit(
                course.kch_id or course.kch, do_jxb_id, course.xkkz_id, kcmc,
                course.kklxdm, kch=course.kch, jxbzls=jxbzls, extra=extra,
            )
        except SessionExpired:
            self._relogin()
            return {"flag": "0", "msg": "会话过期，已重新登录"}
        except JwglxtError as exc:
            self.stats["errors"] += 1
            course.last_msg = str(exc)
            return {"flag": "0", "msg": f"请求失败：{exc}"}

        course.last_msg = result.get("msg", "")
        if _is_success(result):
            self._mark_grabbed(course)
        elif result.get("flag") == "-1":
            course.last_msg = result.get("msg") or "人数已满"
        return result

    def _concurrent_submit(self, course: Course, do_jxb_id: str, kcmc: str,
                           concurrency: int, jxbzls: str = "",
                           row: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """并发提交：多个线程同时抢同一教学班，谁成功算谁。"""
        results: List[Dict[str, Any]] = []
        lock = threading.Lock()

        def worker():
            r = self._submit_once(course, do_jxb_id, kcmc, jxbzls, row)
            with lock:
                results.append(r)

        threads = [threading.Thread(target=worker, daemon=True) for _ in range(concurrency)]
        for t in threads:
            t.start()
            time.sleep(random.uniform(0.01, 0.05))
        for t in threads:
            t.join(timeout=self.client.timeout + 10)

        for r in results:
            if _is_success(r):
                return r
        return results[0] if results else {"flag": "0", "msg": "无返回"}

    def _update_quota(self, course: Course, target: Dict[str, Any]) -> None:
        capacity = int(target.get("capacity", -1))
        enrolled = int(target.get("enrolled", -1))
        if capacity >= 0:
            course.capacity = capacity
        if enrolled >= 0:
            course.enrolled = enrolled
        if target.get("kcmc"):
            course.kcmc = course.kcmc or str(target["kcmc"])
        if target.get("teacher"):
            course.teacher = course.teacher or str(target["teacher"])
        if target.get("xf"):
            course.xf = course.xf or str(target["xf"])

    def _mark_grabbed(self, course: Course) -> None:
        if course.grabbed:
            return
        course.grabbed = True
        self.stats["success"] += 1
        self.wishlist.save()
        msg = f"已抢到：{course.title}"
        if course.teacher:
            msg += f"（{course.teacher}）"
        self.log("success", msg)
        try:
            self.event_cb("grabbed")
        except Exception:
            pass
        self.notifier.send("抢课成功 🎉", msg)

    def _prepare_one(self, course: Course) -> None:
        for kk in [course.kklxdm] + list(self.cfg.get("kklxdm_candidates") or []):
            try:
                course.xkkz_id = self.client.get_xkkz_id(str(kk))
                course.kklxdm = str(kk)
                return
            except JwglxtError:
                continue

    def _relogin(self) -> None:
        self.log("warn", "会话失效，正在重新登录…")
        try:
            self.client.session.cookies.clear()
            self.client.logged_in = False
            self.client.login()
            self.client.fetch_xsxk_page()
            self.log("info", "重新登录成功，继续监控。")
        except JwglxtError as exc:
            self.log("error", f"重新登录失败：{exc}，5 秒后重试。")
            self._stop.wait(5)

    def _finish(self, status: str) -> None:
        self.status_cb(status)
        summary = (
            f"轮询 {self.stats['rounds']} 轮，提交 {self.stats['submits']} 次，"
            f"成功 {self.stats['success']} 门，错误 {self.stats['errors']} 次。"
        )
        self.log("info", f"抢课结束（{status}）：{summary}")
        self._stop.set()

    # ------------------------------------------------------------------ #
    # 校验辅助（GUI 用）
    # ------------------------------------------------------------------ #
    def test_login(self) -> str:
        """仅测试登录（并顺带报一下当前是否在选课时间），返回提示文本。"""
        self.client.base_url = (self.cfg.get("base_url") or "").rstrip("/")
        self.client.username = self.cfg.get("username") or ""
        self.client.password = self.cfg.get("password") or ""
        self.client.login()
        text = f"登录成功，学号 {self.client.su}"
        stage = self.client.check_stage()
        if stage.get("open") is False:
            text += f"\n⚠ 当前不在选课时间：{stage.get('msg')}"
        elif stage.get("open") is True:
            text += f"\n✅ 选课时间看起来是开放的（{stage.get('msg')}）"
        else:
            text += f"\n（选课阶段未知：{stage.get('msg')}）"
        return text

    def check_stage(self) -> str:
        """给界面用：单独查一次「现在是否在选课时间」。"""
        self.client.base_url = (self.cfg.get("base_url") or "").rstrip("/")
        self.client.username = self.cfg.get("username") or ""
        self.client.password = self.cfg.get("password") or ""
        self.client.login()
        stage = self.client.check_stage()
        if stage.get("open") is False:
            return f"❌ 当前不在选课时间：{stage.get('msg')}"
        if stage.get("open") is True:
            return f"✅ 选课时间开放（{stage.get('msg')}）"
        return f"⚠ 无法判断：{stage.get('msg')}"

    def test_autodetect(self) -> Dict[str, Any]:
        """登录并探测选课参数，返回可写回配置的字典。"""
        self.client.base_url = (self.cfg.get("base_url") or "").rstrip("/")
        self.client.username = self.cfg.get("username") or ""
        self.client.password = self.cfg.get("password") or ""
        self.client.login()
        return self.client.autodetect()
