"""
阴阳师自动点击脚本 v3.0
基于 OpenCV 模板匹配的自动化刷副本工具

优化内容:
  - 配置文件驱动 (config.json)，无需修改源码
  - logging 日志系统（文件持久化 + 控制台实时提示）
  - 空模板目录警告
  - 窗口未找到优雅降级
  - mouse_thread 超时保护
  - 连续失败自动保存调试截图
  - plt.savefig 非阻塞绘图
"""

import json
import logging
import math
import os
import queue
import random
import re
import shutil
import threading
import time
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from typing import Optional

from pathlib import Path

import cv2
import keyboard
import matplotlib.pyplot as plt
import numpy as np
import pyautogui

# ============================================================
#  全局状态 & 紧急停止
# ============================================================
STOP_EVENT = threading.Event()

# 为跨线程 unified call 设计的毒丸
POISON_PILL = ("__exit__", None, None, 0, 0, 0)

def emergency_stop():
    """F12 全局热键回调"""
    print("\n[紧急停止] F12 已按下，正在通知所有 Bot 退出...")
    STOP_EVENT.set()


# ============================================================
#  日志系统
# ============================================================
# 日志文件轮转：单文件上限 5 MB，最多保留 3 份历史（yys_auto.log.1 ~ .3）
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 3

# ---- 统一日志格式：文件 / 控制台 / 前端面板共用同一模板 ----
#   时间 | 来源 | 级别 | 消息
# 三处只有时间精度不同：文件带完整日期，控制台与前端面板只到时分秒。
LOG_FORMAT = "%(asctime)s | %(name)-16s | %(levelname)-7s | %(message)s"
LOG_DATE_FMT = "%Y-%m-%d %H:%M:%S"
LOG_TIME_FMT = "%H:%M:%S"


def make_log_formatter(with_date: bool = True) -> logging.Formatter:
    """按统一模板构造 formatter（文件用 with_date=True，其余用 False）"""
    return logging.Formatter(
        LOG_FORMAT,
        datefmt=LOG_DATE_FMT if with_date else LOG_TIME_FMT,
    )


def setup_root_file_logger(log_dir: str, level=logging.DEBUG):
    """设置根日志器，输出到文件（DEBUG 级别），按体积轮转，避免日志无限增长"""
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "yys_auto.log")

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # 避免重复添加 handler
    if not any(isinstance(h, logging.FileHandler) for h in root.handlers):
        fh = RotatingFileHandler(
            log_path,
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        fh.setLevel(level)
        fh.setFormatter(make_log_formatter(with_date=True))
        root.addHandler(fh)

    return log_path


class _LogBufferStream:
    """伪 stream，将日志写入内存列表供前端读取"""
    def __init__(self, buf: list):
        self.buf = buf

    def write(self, msg: str):
        msg = msg.strip()
        if msg:
            self.buf.append(msg)
            if len(self.buf) > 500:
                self.buf.pop(0)

    def flush(self):
        pass


def _console_stream():
    """
    取一个能安全输出 emoji 的控制台流。

    Windows 控制台默认 GBK，日志里的 🖱 / ✅ 会触发 UnicodeEncodeError，
    logging 直接吞掉这条消息并打一行「--- Logging error ---」。
    这里用 UTF-8 + errors="replace" 重包一次；拿不到 fd（GUI 无控制台、
    IDE 重定向输出）时退回原流。
    """
    stream = sys.stdout
    if stream is None:
        return None
    try:
        return open(stream.fileno(), mode="w", encoding="utf-8",
                    errors="replace", closefd=False, buffering=1)
    except (AttributeError, OSError, ValueError):
        return stream


def setup_bot_logger(name: str, level=logging.INFO):
    """为每个 Bot 创建独立 logger，带控制台输出"""
    logger = logging.getLogger(f"yys.{name}")
    logger.setLevel(logging.INFO)  # 确保 INFO 以上消息不被过滤

    # 清理旧的缓冲区 handler（bot 重启时残留）
    logger.handlers = [h for h in logger.handlers
                       if not (isinstance(h, logging.StreamHandler)
                               and isinstance(h.stream, _LogBufferStream))]

    # 控制台 handler（与文件日志同一模板，只是时间不带日期）
    if not any(isinstance(h, logging.StreamHandler) for h in logger.handlers):
        ch = logging.StreamHandler(_console_stream())
        ch.setLevel(level)
        ch.setFormatter(make_log_formatter(with_date=False))
        logger.addHandler(ch)

    return logger


# ============================================================
#  配置文件加载
# ============================================================
def load_config(config_path: str | None = None) -> dict:
    """加载 JSON 配置文件，若未指定则自动查找脚本同目录下的 config.json"""
    if config_path is None:
        config_path = Path(__file__).parent / "config.json"
    else:
        config_path = Path(config_path)

    if not config_path.exists():
        # 首次运行：从同目录的 config.example.json 生成一份
        # （config.json 是本机私有配置，不纳入版本管理）
        example = config_path.parent / "config.example.json"
        if example.exists() and example != config_path:
            shutil.copyfile(example, config_path)
        else:
            raise FileNotFoundError(
                f"配置文件不存在: {config_path}\n"
                f"请在脚本目录下创建 config.json（可直接复制 config.example.json），"
                f"或通过 --config 指定路径"
            )

    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)

    # 新版前端配置（instances / template_scene）→ 转为独立运行的内部结构
    config = _adapt_frontend_config(config)

    # 基础校验
    for key in ("bots", "global"):
        if key not in config:
            raise ValueError(f"配置文件缺少必要字段: {key}")

    if not isinstance(config["bots"], list) or len(config["bots"]) == 0:
        raise ValueError("配置文件中 bots 列表不能为空")

    return config


# 旧配置里的「连续未匹配次数」按常态帧间隔折算成秒（仅用于兼容老 config.json）
_LEGACY_MISS_FRAME_SEC = 0.3


def resolve_miss_timeout(cfg: dict, default: float = 8.0) -> float:
    """
    取出「未匹配超时（秒）」，兼容旧版 miss_threshold（次数）。

    旧版按次数判定，但扫描间隔在 0.25~1.0 秒之间浮动，同样的次数对应
    5~20 秒不等，所以这里按常态 0.3 秒/帧折算成秒，语义才一致。
    """
    raw = cfg.get("miss_timeout")
    if raw is None:
        legacy = cfg.get("miss_threshold")
        if legacy is None:
            return default
        try:
            raw = float(legacy) * _LEGACY_MISS_FRAME_SEC
        except (TypeError, ValueError):
            return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return max(2.0, min(120.0, value))


def resolve_battle_timeout(cfg: dict, default: float = 240.0) -> float:
    """取出「战斗超时（秒）」，即点完 begin 后最多等多久还没打到 end。"""
    try:
        value = float(cfg.get("battle_timeout", default))
    except (TypeError, ValueError):
        return default
    return max(60.0, min(900.0, value))


def _adapt_frontend_config(config: dict) -> dict:
    """
    将前端控制面板使用的配置格式（instances / template_scene）
    转换为独立运行入口期望的格式（bots / global）。
    已经是旧格式时原样返回。
    """
    if "bots" in config:
        return config

    instances = config.get("instances")
    if not instances:
        return config

    base_dir = Path(__file__).parent

    bots: list[dict] = []
    for idx, inst in enumerate(instances, 1):
        scene = inst.get("template_scene", "")
        task_dir = str(base_dir / "templates" / scene) if scene else ""

        bots.append({
            "name": inst.get("name") or inst.get("id") or f"bot-{idx}",
            "window_title": inst.get("window_title", config.get("window_title", "")),
            "task_dir": task_dir,
            "limit": inst.get("limit", config.get("limit", 200)),
            "threshold": inst.get("threshold", config.get("threshold", 0.75)),
        })

    global_cfg = {k: v for k, v in config.items()
                  if k not in ("instances", "screen", "_comment")}

    return {"bots": bots, "global": global_cfg}


# ============================================================
#  贝塞尔曲线
# ============================================================
def bezier_curve(points, n=100):
    """
    De Casteljau 贝塞尔曲线生成
    :param points: 控制点列表 [(x1,y1), (x2,y2), ...]
    :param n:      曲线上的采样点数
    :return:       路径点列表 [[x,y], [x,y], ...]
    """
    points = np.array(points)
    t_values = np.linspace(0, 1, n)
    path = []
    for t in t_values:
        temp = points.copy().astype(float)
        while len(temp) > 1:
            temp = (1 - t) * temp[:-1] + t * temp[1:]
        path.append(temp[0].tolist())
    return path


def arc_length_resample(path, step_px=4):
    """
    将贝塞尔路径按等弧长重新采样，使每步像素间距一致。

    :param path:    bezier_curve 返回的路径点列表
    :param step_px: 两两采样点之间的目标像素距离（默认 4 px）
    :return:        等间距采样后的路径点列表
    """
    if len(path) < 2:
        return path

    # 累积弧长
    pts = np.array(path)
    diffs = np.diff(pts, axis=0)
    seg_lens = np.sqrt((diffs ** 2).sum(axis=1))
    cum_len = np.concatenate(([0.0], np.cumsum(seg_lens)))
    total_len = cum_len[-1]

    if total_len < step_px:
        return path

    # 按 step_px 间距重采样
    n_steps = max(int(total_len / step_px), 2)
    sample_dists = np.linspace(0, total_len, n_steps)

    xs = np.interp(sample_dists, cum_len, pts[:, 0])
    ys = np.interp(sample_dists, cum_len, pts[:, 1])

    return [[float(x), float(y)] for x, y in zip(xs, ys)]


def human_delay(base_seconds, jitter_factor=0.3):
    """生成模拟人类操作的随机延迟"""
    jitter = base_seconds * jitter_factor * random.uniform(-1, 1)
    return base_seconds + jitter


# ============================================================
#  MouseController — 贝塞尔曲线鼠标移动 + 点击
# ============================================================
# ============================================================
#  点击方式配置（模拟器升级后点击不生效时可在此调整）
# ============================================================
# 按住时长（毫秒）：down → 按住 → up。
# pyautogui.click() 是瞬时 down/up，部分模拟器会判定为"掠过"而丢弃，
# 按住一小段时间后才被识别为一次有效触摸点击。
CLICK_HOLD_MS_RANGE = (60, 140)

# 抬起后的缓冲等待（秒）
CLICK_SETTLE_RANGE = (0.03, 0.08)

# 点击后把光标随机挪开：距离范围（像素，上限 500）。
# 目的是不让光标总停在同一处（固定落点），下次移动的起点也随之变化。
POST_CLICK_MOVE_RANGE = (80, 500)
# 挪开时的移动速度（像素/秒）与最短耗时：按距离算时长，
# 短距离也至少走 0.15 秒，否则几百像素一眨眼就到位，看着像瞬移
POST_CLICK_MOVE_SPEED = (600, 1100)
POST_CLICK_MOVE_MIN_SEC = 0.15

class MouseController:
    def __init__(self, speed_min=1500, speed_max=2000, stop_event: Optional[threading.Event] = None):
        self.speed_min = speed_min
        self.speed_max = speed_max
        self._stop_event = stop_event if stop_event is not None else STOP_EVENT

    def click(self, hold_ms_range=CLICK_HOLD_MS_RANGE):
        """
        按压式点击：mouseDown → 随机按住 → mouseUp。
        替代 pyautogui.click() 的瞬时点击，避免被模拟器判定为"掠过"而丢弃。
        """
        hold = random.uniform(*hold_ms_range) / 1000.0
        saved_pause = pyautogui.PAUSE
        pyautogui.PAUSE = 0
        pyautogui.mouseDown()
        time.sleep(hold)
        pyautogui.mouseUp()
        pyautogui.PAUSE = saved_pause
        time.sleep(random.uniform(*CLICK_SETTLE_RANGE))
        self._move_after_click()

    def _move_after_click(self):
        """
        点击后把光标随机挪开（最多 POST_CLICK_MOVE_RANGE 上限的距离），
        避免总是停在同一个落点，也让下一次移动的起点有变化。
        移动点会夹在屏幕内并留边距，防止碰到 pyautogui 的角落 FAILSAFE。
        """
        if self._stop_event.is_set():
            return
        try:
            cur = pyautogui.position()
            scr_w, scr_h = pyautogui.size()
        except Exception:
            return
        dist = random.uniform(*POST_CLICK_MOVE_RANGE)
        angle = random.uniform(0, 2 * math.pi)
        margin = 10
        tx = int(cur[0] + dist * math.cos(angle))
        ty = int(cur[1] + dist * math.sin(angle))
        tx = max(margin, min(scr_w - margin, tx))
        ty = max(margin, min(scr_h - margin, ty))
        # 按距离算时长：500px 大约走 0.45~0.85 秒，肉眼能看出移动轨迹
        real_dist = math.hypot(tx - cur[0], ty - cur[1])
        duration = max(POST_CLICK_MOVE_MIN_SEC,
                       real_dist / random.uniform(*POST_CLICK_MOVE_SPEED))
        try:
            # 和点击前的接近移动走同一套贝塞尔曲线 + 密步推进，
            # 否则 pyautogui 自带插值掉帧，看起来一卡一卡的
            self.move_to(tx, ty, duration=duration)
        except Exception:
            pass

    def _escape_failsafe_corner(self):
        """
        光标正好压在屏幕四角时，pyautogui 的每个动作都会抛 FailSafeException，
        而移动动作本身也会被拦下 —— 光标永远离不开角落，等于自锁。
        这里先用 win32 的 SetCursorPos 把光标挪开几像素，再交给 pyautogui。
        """
        if not getattr(pyautogui, "FAILSAFE", False):
            return
        try:
            x, y = pyautogui.position()
            w, h = pyautogui.size()
        except Exception:
            return
        near_left, near_right = x <= 1, x >= w - 2
        near_top, near_bottom = y <= 1, y >= h - 2
        if (near_left or near_right) and (near_top or near_bottom):
            try:
                import ctypes
                nx = 20 if near_left else w - 21
                ny = 20 if near_top else h - 21
                ctypes.windll.user32.SetCursorPos(int(nx), int(ny))
                time.sleep(0.02)
            except Exception:
                pass

    def move_to(self, target_x, target_y, duration=None,
                start_offset_range=(-80, 80),
                control_offset_range=(-100, 100)):
        """
        从当前鼠标位置，沿一条随机贝塞尔曲线移动到目标点（不点击）。
        点击前的接近移动、点击后的随机挪开都走这一条路径，
        保证两处的轨迹形状与时间节奏一致。
        """
        self._escape_failsafe_corner()
        start_x, start_y = pyautogui.position()

        # 已经在目标附近 → 小幅移动过去即可
        if abs(start_x - target_x) < 10 and abs(start_y - target_y) < 10:
            pyautogui.moveTo(target_x, target_y, duration=random.uniform(0.08, 0.15))
            return

        distance = math.hypot(target_x - start_x, target_y - start_y)

        # ---- 根据距离动态缩放参数 ----
        # 控制点数量：每 400px 加一个中间弯折点（最少 1 个）
        num_midpoints = max(1, int(distance / 400))

        # 采样点密度：每 5px 至少一个原始采样点，min=120, max=800
        n_samples = max(120, min(800, int(distance / 3)))

        # 偏移范围缩放：距离越大，偏移范围适度放大
        scale = math.sqrt(distance / 400.0)  # 距离 400px→scale=1, 1600px→scale=2
        offset_lo = int(control_offset_range[0] * scale)
        offset_hi = int(control_offset_range[1] * scale)

        # 生成贝塞尔控制点：起点 + N个中间弯折点 + 终点偏差 + 终点
        control_points = [(start_x, start_y)]
        for i in range(1, num_midpoints + 1):
            t = i / (num_midpoints + 1)
            mx = start_x + (target_x - start_x) * t + random.randint(offset_lo, offset_hi)
            my = start_y + (target_y - start_y) * t + random.randint(offset_lo, offset_hi)
            control_points.append((mx, my))
        # 终点前加一个"冲过头"的偏差点
        dx = random.randint(*start_offset_range)
        dy = random.randint(*start_offset_range)
        control_points.append((target_x + dx, target_y + dy))
        control_points.append((target_x, target_y))

        raw_path = bezier_curve(control_points, n=n_samples)
        # 每 4px 一个落点：步数过密时每步只有零点几毫秒，time.sleep 的精度
        # 跟不上，光标会一顿一顿地往前跳，看起来像瞬移
        path = arc_length_resample(raw_path, step_px=4)

        # 根据速度计算总时长
        if duration is None:
            speed = random.uniform(self.speed_min, self.speed_max)
            duration = max(0.1, distance / speed)

        # 密集微步 + 变速移动（快启动慢结束，模仿人类）
        saved_pause = pyautogui.PAUSE
        pyautogui.PAUSE = 0
        n_points = len(path)

        # 每步权重：progress=0 → 0.3x（快）, progress=1 → 1.0x（最低基准慢）
        denom = max(1, n_points - 1)
        raw_weights = [0.3 + 0.7 * (i / denom) ** 1.5
                       for i in range(n_points)]
        sum_w = sum(raw_weights)

        # 按绝对时刻推进：每步的目标时间由前面累加得出，单次 sleep 的
        # 精度误差不会被放大成"跳一下"
        t_next = time.perf_counter()
        for i, (px, py) in enumerate(path):
            if self._stop_event.is_set():
                pyautogui.PAUSE = saved_pause
                return
            t_next += duration * (raw_weights[i] / sum_w) * random.uniform(0.90, 1.10)
            pyautogui.moveTo(int(px + random.gauss(0, 0.15)),
                             int(py + random.gauss(0, 0.15)), duration=0)
            remain = t_next - time.perf_counter()
            if remain > 0:
                time.sleep(remain)
            elif remain < -0.05:
                # 被系统调度打断得久了就别再追赶，否则后面会连发几步，
                # 反而造成肉眼可见的瞬移感
                t_next = time.perf_counter()

        pyautogui.PAUSE = saved_pause

    def move_and_click(self, target_x, target_y, duration=None,
                       start_offset_range=(-80, 80),
                       control_offset_range=(-100, 100)):
        """移动到目标点并点击（与点击后的挪开共用 move_to）"""
        self.move_to(target_x, target_y, duration=duration,
                     start_offset_range=start_offset_range,
                     control_offset_range=control_offset_range)
        if self._stop_event.is_set():
            return
        time.sleep(random.uniform(0.05, 0.12))
        self.click()


# ============================================================
#  TemplateManager — 模板管理 & 匹配
# ============================================================
# 阶段旗帜的占位图判定：短边小于该值（像素）的图片视为「未设置」
FLAG_PLACEHOLDER_MAX_PX = 20

# 可点击区域轮廓向内收缩（屏幕像素；避开框边缘的相邻 UI，防误触）。
# end 模板带背景，需要缩掉一圈，但也不宜太多，否则可点范围明显偏小；
# 内缩量还不得超过「最窄模板短边」的这个比例，否则窄条模板会被整个缩没。
#
# begin 不在此列：它改用「内切圆」采样（半径 = 中心到最近边的距离），
# 圆本身就不会碰到框边缘，因此不需要额外内缩。
REGION_SHRINK_PX_END = 15
REGION_SHRINK_RATIO_END = 0.20

# 相邻区域之间不超过该宽度的缝隙会被自动填补（屏幕像素）
REGION_FILL_GAP_PX = 6

# 是否在点击前复核阶段标志（仅针对「连续同阶段」的点击）。
# 坐标是识别阶段时算好的，若这期间界面已开始切换，点击会落到错误的区域。
# 首次进入某阶段时标志刚确认过，不做重复复核，避免白白多一次截图。
STAGE_RECHECK_ENABLED = True

# ------------------------------------------------------------
# 异常类退出保护（与「未匹配」判定彼此独立）
# ------------------------------------------------------------
# 过去这两个计数都塞进 error_score，再和 miss_threshold 共用同一个退出判断，
# 结果把「连续未匹配上限」调大后会被 error_score>=30 提前打断。
# 现在各管各的：未匹配走时长判定（miss_timeout），下面两类各自计数、各自退出。
CAPTURE_FAIL_LIMIT = 20    # 连续截图失败次数（0.5s/次，约 10 秒）
STEP_EXCEPTION_LIMIT = 10  # 连续识别异常帧数


def is_usable_image(img: np.ndarray) -> bool:
    """
    判断图片是否是可用的真实素材。
    新建场景时自动生成的占位图尺寸极小，用它来区分「未设置」。
    """
    h, w = img.shape[:2]
    return h >= FLAG_PLACEHOLDER_MAX_PX and w >= FLAG_PLACEHOLDER_MAX_PX


# ------------------------------------------------------------
#  阶段列表：<场景>/stages/<序号>_<标签>_<名字>.png
#  标签：start（战斗开始）/ mid（中间页，默认）/ end（一把结束）
# ------------------------------------------------------------
STAGES_DIRNAME = "stages"
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".bmp")
# 场景目录下记录被停用模板的文件名清单（前端「停用」按钮写入，运行时跳过这些模板）
DISABLED_FILE = ".disabled.json"
STAGE_NAME_RE = re.compile(r"^(\d+)_(start|end|mid)_([a-z0-9_]+)$")
STAGE_TAGS = ("start", "mid", "end")
# 新建场景默认生成的两个阶段
DEFAULT_STAGES = ((1, "start", "begin"), (2, "end", "result"))
# 战斗等待上限的硬顶：实际取值是「平均战斗时长 × 3」，但不会超过这个值
BATTLE_TIMEOUT_CAP = 240.0
# 同一阶段连续点击时的退避间隔（秒），按已点次数递增，越界取最后一档
# 首档 1.0：0.3 秒一帧的扫描节奏下，0.5 秒会让同一页在半秒内被点两下
STAGE_CLICK_BACKOFF = (1.0, 1.5, 2.0)
# start 阶段点击后的重试窗口：界面没变时允许再点一次（应对 begin 要点两下），
# 窗口结束后就不再点它，等价于旧版的「begin 长冷却」
START_RETRY_WINDOW = 3.0
# 首次进入某阶段、学习可点击区域前等待的秒数，让过渡动画走完
STAGE_SETTLE_SEC = {"start": 0.5, "mid": 1.5, "end": 3.0}
# 本程序自己的窗口标题特征：窗口列表要排除，否则很容易被误选成游戏窗口，
# 表现就是「点了开始，跳前台的是控制面板，模拟器一动没动」
# 「截取模板」是截图时弹出的框选窗，抓屏前要一并藏掉，否则会把它拍进截图里
SELF_WINDOW_MARKERS = ("痒痒鼠解放双手", "127.0.0.1", "截取模板")
# 中间页点完之后的稳定等待（秒）：切图 / 弹窗动画期间画面还在变，
# 这期间继续扫描容易误判成还在同一页，或者点到动画途中的位置
MID_CLICK_SETTLE_SEC = 1.0


def stage_key(index: int, tag: str, name: str) -> str:
    """阶段唯一键，同时也是磁盘上的文件名（不含扩展名）"""
    return f"{int(index):02d}_{tag}_{name}"


def is_self_window(title: str) -> bool:
    """窗口标题是否属于本程序自己（控制面板 / eel 打开的浏览器页）"""
    t = (title or "").lower()
    return any(m.lower() in t for m in SELF_WINDOW_MARKERS)


def find_window_handle(title: str):
    """
    按标题子串（忽略大小写）查找可见窗口，排除本程序自己的窗口。

    :return: (hwnd 或 None, 命中自身窗口的标题列表, 所有可见窗口标题)
    """
    import win32gui

    keyword = (title or "").lower().strip()
    if not keyword:
        return None, [], []

    target = None
    self_matched = []
    all_titles = []

    def _enum_cb(hwnd, _extra):
        nonlocal target
        if not win32gui.IsWindowVisible(hwnd):
            return
        wt = win32gui.GetWindowText(hwnd)
        if not wt or not wt.strip():
            return
        all_titles.append(wt.strip())
        if keyword not in wt.strip().lower():
            return
        # 标题命中的是本程序自己的控制面板 → 不能当游戏窗口，
        # 否则会「激活成功」但前置的是面板，模拟器一动不动
        if is_self_window(wt):
            self_matched.append(wt.strip())
            return
        target = hwnd

    win32gui.EnumWindows(_enum_cb, None)
    return target, self_matched, all_titles


def activate_window(hwnd, log=None) -> bool:
    """
    把目标窗口切到前台，逐级尝试并**校验结果**。

    背景：Windows 只允许「当前拥有前台权」的进程调用 SetForegroundWindow。
    从浏览器（控制面板）里点「开始」时，输入给了浏览器进程，本进程没有前台权，
    单次 SetForegroundWindow 会静默失败（返回 0），窗口一动不动 —— 这正是
    「点了开始但游戏没跳到前台」的原因。下面按成功率从高到低依次尝试。
    """
    try:
        import ctypes
        from ctypes import windll
        u = windll.user32
        k32 = windll.kernel32

        def _is_foreground() -> bool:
            return u.GetForegroundWindow() == hwnd

        # 1) AttachThreadInput 到当前前台线程，绕开前台锁
        fore = u.GetForegroundWindow()
        fore_tid = u.GetWindowThreadProcessId(fore, None)
        cur_tid = k32.GetCurrentThreadId()
        tgt_tid = u.GetWindowThreadProcessId(hwnd, None)
        if fore_tid and fore_tid != cur_tid:
            u.AttachThreadInput(fore_tid, cur_tid, True)
            u.AttachThreadInput(cur_tid, tgt_tid, True)
        try:
            u.SetForegroundWindow(hwnd)
            u.BringWindowToTop(hwnd)
        finally:
            if fore_tid and fore_tid != cur_tid:
                u.AttachThreadInput(cur_tid, tgt_tid, False)
                u.AttachThreadInput(fore_tid, cur_tid, False)
        if _is_foreground():
            return True

        # 2) 注入一次 Alt 键，解除系统对前台切换的封锁
        u.keybd_event(0x12, 0, 0, 0)
        u.keybd_event(0x12, 0, 2, 0)
        u.SetForegroundWindow(hwnd)
        if _is_foreground():
            return True

        # 3) 临时置顶再取消，把窗口抬到最上层
        #    HWND_TOPMOST/-NOTOPMOST 是 -1/-2，必须用指针类型传，
        #    否则 64 位下按 int 传会变成无效句柄
        u.SetWindowPos(hwnd, ctypes.c_void_p(-1), 0, 0, 0, 0, 0x0001 | 0x0002)
        u.SetWindowPos(hwnd, ctypes.c_void_p(-2), 0, 0, 0, 0, 0x0001 | 0x0002)
        u.SetForegroundWindow(hwnd)
        if _is_foreground():
            return True

        # 4) 未公开但常用的兜底
        try:
            u.SwitchToThisWindow(hwnd, True)
        except Exception:
            pass
        return _is_foreground()
    except Exception as e:
        if log is not None:
            log.warning(f"激活窗口异常: {type(e).__name__}: {e}")
        return False


def restore_and_activate(hwnd, log=None, activate=True) -> bool:
    """
    最小化的窗口先还原（最小化窗口截出来是空白）；
    activate=True 时再切到前台，返回是否成功。
    """
    import win32gui
    import win32con

    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    if not activate:
        return True
    return activate_window(hwnd, log)


def grab_window_region(hwnd, log=None):
    """
    截取窗口所占的屏幕区域。

    取景规则：GetWindowRect 的整窗口矩形（含标题栏与边框），超出屏幕的部分裁掉。
    前端截取模板 / 运行时识别必须共用这份逻辑，否则模板与运行时画面尺度不一致，
    匹配会整体偏移。

    :return: (PIL.Image, left, top, width, height) 或 None
    """
    import win32gui

    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    width, height = right - left, bottom - top
    if width <= 0 or height <= 0:
        if log is not None:
            log.warning(f"窗口尺寸异常: {width}x{height}")
        return None

    # 裁剪到实际屏幕范围：窗口可能有一部分在屏幕外，
    # 而 pyautogui 截取越界区域时会得到黑边甚至直接报错
    scr_w, scr_h = pyautogui.size()
    clip_left = max(left, 0)
    clip_top = max(top, 0)
    clip_right = min(right, scr_w)
    clip_bottom = min(bottom, scr_h)
    if clip_right <= clip_left or clip_bottom <= clip_top:
        if log is not None:
            log.warning("窗口完全在屏幕外")
        return None

    cw = clip_right - clip_left
    ch = clip_bottom - clip_top
    img = pyautogui.screenshot(region=(clip_left, clip_top, cw, ch))
    return img, clip_left, clip_top, cw, ch


def parse_stage_stem(stem: str):
    """解析阶段文件名（不含扩展名），不合规返回 None"""
    m = STAGE_NAME_RE.match(stem)
    if not m:
        return None
    return int(m.group(1)), m.group(2), m.group(3)


@dataclass
class Stage:
    """一个阶段：一张标志图（判断当前是不是这一页）+ 一组可点击按钮模板"""

    index: int
    tag: str
    name: str
    flag_file: str = ""
    flag: "np.ndarray | None" = None
    dir_name: str = ""          # 模板目录名，默认与 key 相同；旧结构会指向 begin / end
    templates: list = field(default_factory=list)   # [(文件名, 图像), ...]

    def __post_init__(self):
        if not self.dir_name:
            self.dir_name = self.key

    @property
    def key(self) -> str:
        return stage_key(self.index, self.tag, self.name)

    @property
    def is_start(self) -> bool:
        return self.tag == "start"

    @property
    def is_end(self) -> bool:
        return self.tag == "end"

    @property
    def has_flag(self) -> bool:
        return self.flag is not None

    @property
    def settle_sec(self) -> float:
        return STAGE_SETTLE_SEC.get(self.tag, 1.0)


def build_click_cells(boxes: list, shrink: int = 0, fill_gap: int = 0):
    """
    把若干识别框整理成「互不重叠、可直接采样」的矩形集合。

    1) 先把所有框合成一个整体（并集）——**不逐个框各自收缩**，
       否则原本紧贴的相邻框会被各自缩开、断成好几块
    2) 缝隙不超过 fill_gap 的部分自动填补
       （形态学闭运算：先膨胀把缝粘上、再腐蚀回原尺寸，
         所以只会填补细缝，不会把大的空心区域也填掉）
    3) 整体轮廓向内收缩 shrink 像素（形态学腐蚀），避开框边缘的相邻 UI
    4) 行程编码 + 纵向合并，输出互不重叠的矩形

    :param boxes: [(x, y, w, h), ...]
    :param shrink: 轮廓整体内缩的像素数
    :param fill_gap: 缝隙填补上限
    :return: (cells, total_area)，cells 为 [(x, y, w, h), ...]
    """
    boxes = [(x, y, w, h) for (x, y, w, h) in boxes if w > 0 and h > 0]
    if not boxes:
        return [], 0

    # ---- 掩码：四周留出余量，否则形状贴着边界时腐蚀会失效 ----
    ox = min(b[0] for b in boxes)
    oy = min(b[1] for b in boxes)
    ex = max(b[0] + b[2] for b in boxes)
    ey = max(b[1] + b[3] for b in boxes)
    pad = shrink + fill_gap + 2
    mask_h, mask_w = ey - oy + pad * 2, ex - ox + pad * 2

    mask = np.zeros((mask_h, mask_w), np.uint8)
    for (x, y, w, h) in boxes:
        mx, my = x - ox + pad, y - oy + pad
        mask[my:my + h, mx:mx + w] = 1

    # ---- 填补细缝 ----
    if fill_gap > 0:
        ksize = fill_gap if fill_gap % 2 == 1 else fill_gap + 1
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE,
                                np.ones((ksize, ksize), np.uint8))

    # ---- 整体轮廓内缩 ----
    # 必须显式给出 borderValue=0：OpenCV 腐蚀的默认边界值是「最大值」，
    # 不指定的话贴着边界的形状完全不会被腐蚀。
    if shrink > 0:
        ksize = shrink * 2 + 1
        mask = cv2.erode(mask, np.ones((ksize, ksize), np.uint8),
                         borderType=cv2.BORDER_CONSTANT, borderValue=0)

    if not mask.any():
        return [], 0

    # ---- 3) 行程编码 + 纵向合并，提取互不重叠的矩形 ----
    # 用「上一行的活跃段表」做纵向合并：同一个 x 区间在相邻行出现就向下延伸。
    # （不能只跟上一次添加的矩形比较，否则一行里有多个段时会断掉）
    cells: list[list[int]] = []
    active: dict[tuple[int, int], int] = {}   # (x0, width) -> cells 下标
    prev_row = -2

    for row in range(mask_h):
        cols = np.flatnonzero(mask[row])
        new_active: dict[tuple[int, int], int] = {}

        if cols.size:
            breaks = np.where(np.diff(cols) > 1)[0]
            starts = np.concatenate(([0], breaks + 1))
            ends = np.concatenate((breaks, [cols.size - 1]))
            contiguous = (row == prev_row + 1)

            for s, e in zip(starts, ends):
                x0 = ox - pad + int(cols[s])
                width = int(cols[e] - cols[s] + 1)
                key = (x0, width)

                idx = active.get(key) if contiguous else None
                if idx is not None:
                    cells[idx][3] += 1              # 同位置同宽 → 向下延伸一行
                else:
                    cells.append([x0, oy - pad + row, width, 1])
                    idx = len(cells) - 1
                new_active[key] = idx

        active = new_active
        prev_row = row

    total_area = sum(c[2] * c[3] for c in cells)
    return [(c[0], c[1], c[2], c[3]) for c in cells], total_area


def cells_to_outline(cells: list, epsilon: float = 2.0,
                     as_circle: bool = False) -> list:
    """
    把矩形集合转成「外轮廓多边形」，供前端只绘制区域边框。

    相邻矩形共享的边、以及重叠部分都会被合并掉：先把矩形画进一张掩膜，
    再用 findContours(RETR_CCOMP) 取「外轮廓 + 内部空洞」两层轮廓，
    最后 approxPolyDP 简化顶点（结果通常只有几十个点）。

    外轮廓与空洞的环绕方向相反，前端把两者放进同一条路径、用 evenodd 规则
    填充即可自动挖出中间的洞；描边则会把外框和空洞框一起画出来。
    :param as_circle: True 时按「内切圆」绘制（与 begin 的圆形采样保持一致）
    :return: [[[x, y], ...], ...]，每个元素是一个轮廓多边形的顶点序列
    """
    if not cells:
        return []
    max_x = max(x + w for (x, y, w, h) in cells)
    max_y = max(y + h for (x, y, w, h) in cells)
    mask = np.zeros((int(max_y) + 2, int(max_x) + 2), dtype=np.uint8)
    for (x, y, w, h) in cells:
        if as_circle:
            cv2.circle(mask, (int(x + w / 2), int(y + h / 2)),
                       int(min(w, h) / 2), 255, -1)
        else:
            cv2.rectangle(mask, (int(x), int(y)), (int(x + w), int(y + h)), 255, -1)

    # RETR_CCOMP：同时取「外轮廓」与「内部空洞」两层轮廓
    contours, _ = cv2.findContours(mask, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    outlines = []
    for cnt in contours:
        approx = cv2.approxPolyDP(cnt, epsilon, True)
        if len(approx) >= 3:
            outlines.append([[int(p[0][0]), int(p[0][1])] for p in approx])
    return outlines


def sample_point_in_cells_circle(cells: list):
    """
    在矩形集合的「内切圆」内随机取点（begin 阶段专用）。

    先随机挑一个矩形，再以其中心为圆心、以「中心到最近边的距离」
    （短边的一半）为半径，在圆内按面积均匀取一点。
    圆天然碰不到框边缘，所以 begin 不需要再做轮廓内缩。
    :return: (x, y) 或 None
    """
    if not cells:
        return None
    x, y, w, h = random.choice(cells)
    radius = min(w, h) / 2.0
    if radius <= 0:
        return None
    cx = x + w / 2.0
    cy = y + h / 2.0
    angle = random.uniform(0, 2 * math.pi)
    r = math.sqrt(random.random()) * radius   # sqrt 才保证圆内「面积」均匀
    return int(cx + r * math.cos(angle)), int(cy + r * math.sin(angle))


def sample_point_in_cells(cells: list, total_area: int):
    """
    在区域并集内按面积加权采样，保证【每个像素被选中的概率完全相同】。

    做法：把并集里所有像素按顺序编号成 0 ~ total_area-1，
          等概率抽一个编号，再还原成二维坐标。
    :return: (x, y) 或 None
    """
    if total_area <= 0 or not cells:
        return None

    target = random.randrange(total_area)
    for (x, y, w, h) in cells:
        area = w * h
        if target < area:
            return x + target % w, y + target // w
        target -= area
    return None


class TemplateManager:
    def __init__(self, task_dir: str, threshold: float = 0.75):
        self.task_dir = Path(task_dir)
        self.threshold = threshold
        self.stages: list[Stage] = []            # 按序号排好序的阶段列表
        self.stage_map: dict[str, Stage] = {}    # key -> Stage
        self.baseline_w = None   # baseline.png 基准宽度
        self.baseline_h = None
        self.disabled_templates: set[str] = set()   # 被停用的模板文件名，加载时跳过

    # ---------------- 加载 ----------------
    def load(self):
        """加载 baseline、各阶段标志与按钮模板"""
        self.disabled_templates = self._read_disabled_templates()
        baseline_path = self.task_dir / "baseline.png"
        if baseline_path.exists():
            baseline_img = cv2.imread(str(baseline_path), cv2.IMREAD_COLOR)
            if baseline_img is not None and is_usable_image(baseline_img):
                self.baseline_h, self.baseline_w = baseline_img.shape[:2]
                logging.getLogger("yys.templates").info(
                    f"基准窗口 {self.baseline_w}x{self.baseline_h}（模板缩放基准）")

        stages = self._scan_stage_dir()
        if not stages:
            stages = self._legacy_stages()
        for st in stages:
            self._load_stage_templates(st)
        self.stages = sorted(stages, key=lambda s: s.index)
        self.stage_map = {st.key: st for st in self.stages}

    def _scan_stage_dir(self) -> list[Stage]:
        """扫描场景目录下的阶段标志图，按序号排序（同序号只留第一个）"""
        log = logging.getLogger("yys.templates")
        bases = [self.task_dir]
        # 兼容早期把阶段放在 stages/ 子目录里的布局（GUI 打开时会自动迁出）
        legacy_sub = self.task_dir / STAGES_DIRNAME
        if legacy_sub.is_dir():
            bases.append(legacy_sub)
        found: dict[int, Stage] = {}
        for base in bases:
            for path in sorted(base.iterdir()):
                if not path.is_file() or path.suffix.lower() not in IMAGE_EXTS:
                    continue
                parsed = parse_stage_stem(path.stem)
                if parsed is None:
                    # 场景根下的 baseline.png / begin.png 等普通素材，不算阶段；
                    # 只有以数字开头才说明是写错格式的阶段文件名
                    if path.stem[:1].isdigit():
                        log.warning(
                            f"⚠ 阶段文件命名不合规，已忽略: {path.name}"
                            f"（应形如 01_start_begin.png）")
                    continue
                index, tag, name = parsed
                if index in found:
                    log.warning(f"⚠ 阶段序号 {index:02d} 重复，已忽略: {path.name}")
                    continue
                img = cv2.imread(str(path), cv2.IMREAD_COLOR)
                if img is None:
                    log.warning(f"⚠ 阶段标志读不出来，已忽略: {path.name}")
                    continue
                if not is_usable_image(img):
                    img = None      # 占位图（1x1）视为「还没设置」
                found[index] = Stage(index, tag, name, path.name, img)
        return [found[i] for i in sorted(found)]

    def _legacy_stages(self) -> list[Stage]:
        """旧结构兼容：begin/ + begin.png、end/ + end.png 映射成两个阶段"""
        stages = []
        for index, tag, name in DEFAULT_STAGES:
            legacy = "begin" if tag == "start" else "end"
            folder = self.task_dir / legacy
            flag_path = self.task_dir / f"{legacy}.png"
            if not folder.is_dir() and not flag_path.exists():
                continue
            flag_img = None
            if flag_path.exists():
                img = cv2.imread(str(flag_path), cv2.IMREAD_COLOR)
                if img is not None and is_usable_image(img):
                    flag_img = img
            stages.append(Stage(index, tag, name, f"{legacy}.png", flag_img,
                                dir_name=legacy))
        if stages:
            logging.getLogger("yys.templates").info(
                "检测到旧版 begin/end 结构，已自动映射为 01_start_begin 与 02_end_result")
        return stages

    def _read_disabled_templates(self) -> set[str]:
        """读取场景目录下的 .disabled.json（前端停用的模板文件名清单）"""
        path = self.task_dir / DISABLED_FILE
        if not path.is_file():
            return set()
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            logging.getLogger("yys.templates").warning(
                f"停用清单无法解析，已忽略: {path}")
            return set()
        if not isinstance(data, list):
            return set()
        return {str(x) for x in data}

    def _is_template_disabled(self, dir_name: str, file_name: str) -> bool:
        """清单里按「阶段目录/文件名」记录；纯文件名条目视为对所有阶段生效"""
        return (f"{dir_name}/{file_name}" in self.disabled_templates
                or file_name in self.disabled_templates)

    def _load_stage_templates(self, stage: Stage):
        """加载某阶段的按钮模板：场景根下的 <key>/，回退到旧结构 begin / end"""
        folder = self.task_dir / stage.dir_name
        if not folder.is_dir():
            folder = self.task_dir / STAGES_DIRNAME / stage.dir_name
        if not folder.is_dir():
            return
        skipped = 0
        for file_name in sorted(os.listdir(folder)):
            if not file_name.lower().endswith(IMAGE_EXTS):
                continue
            if self._is_template_disabled(stage.dir_name, file_name):
                skipped += 1
                continue
            try:
                template = cv2.imread(str(folder / file_name), cv2.IMREAD_COLOR)
            except Exception:
                continue
            if template is not None:
                stage.templates.append((file_name, template))
        if skipped:
            logging.getLogger("yys.templates").info(
                f"{stage.key}: 已跳过 {skipped} 个停用的模板")

    @property
    def has_stages(self) -> bool:
        """是否存在可用的阶段标志（至少一个阶段设置了标志图）"""
        return any(st.has_flag for st in self.stages)

    def get_load_report(self) -> list[str]:
        """返回模板加载报告，标注缺标志 / 空模板提示"""
        report = []
        total = 0
        for st in self.stages:
            count = len(st.templates)
            total += count
            if not st.has_flag:
                report.append(
                    f"  {st.key}: {count} 个模板 ⚠ 阶段标志未设置，运行时认不出这一页")
            elif count == 0:
                report.append(f"  {st.key}: 0 个模板 ⚠ 没有按钮模板，永远点不到")
            else:
                report.append(f"  {st.key}: {count} 个模板")

        if not self.stages:
            report.append("  ⚠ 没有任何阶段，请在「模板管理」里先创建阶段")
        elif not self.has_stages:
            report.append("  ⚠ 所有阶段标志都未设置，无法判断当前界面")

        report.insert(0, f"共 {len(self.stages)} 个阶段、{total} 个模板:")
        return report

    @property
    def is_empty(self) -> bool:
        return all(len(st.templates) == 0 for st in self.stages)

    def _match_one(self, screenshot_cv: np.ndarray, template: np.ndarray,
                   base_scale: float):
        """
        在截图上匹配单个模板（含缩放）。
        :return: ((x, y, w, h), score)；尺寸不适配或分数低于阈值时返回 None
        """
        screenshot_h, screenshot_w = screenshot_cv.shape[:2]
        t_h, t_w = template.shape[:2]
        new_w, new_h = int(t_w * base_scale), int(t_h * base_scale)
        if new_w < 10 or new_h < 10 or new_w > screenshot_w or new_h > screenshot_h:
            return None

        resized = cv2.resize(template, (new_w, new_h))
        result = cv2.matchTemplate(screenshot_cv, resized, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(result)
        if max_val < self.threshold:
            return None
        return (max_loc[0], max_loc[1], new_w, new_h), max_val

    def find_all_matches(self, screenshot_cv: np.ndarray, stage: Stage,
                         base_scale: float = 1.0) -> list:
        """
        扫描某阶段的全部按钮模板，返回所有高于阈值的匹配。
        用于学习该阶段的可点击区域。
        :return: [(阶段 key, (x, y, w, h), 模板文件名, 分数), ...]
        """
        results = []
        for template_name, template in stage.templates:
            hit = self._match_one(screenshot_cv, template, base_scale)
            if hit is not None:
                pos, score = hit
                results.append((stage.key, pos, template_name, score))
        return results

    def match_stage(self, screenshot_cv: np.ndarray, base_scale: float = 1.0):
        """
        扫描全部阶段标志，取分数最高的一个。
        阶段数一般只有 3 个，全扫的开销与模板总数无关。
        :return: (Stage 或 None, (x, y, w, h) 或 None, 分数)
        """
        best_stage, best_pos, best_score = None, None, 0.0
        for st in self.stages:
            if st.flag is None:
                continue
            hit = self._match_one(screenshot_cv, st.flag, base_scale)
            if hit is None:
                continue
            pos, score = hit
            if score > best_score:
                best_stage, best_pos, best_score = st, pos, score
        return best_stage, best_pos, best_score


# ============================================================
#  MouseWorker — 独立鼠标执行线程
# ============================================================
class MouseWorker:
    """
    生产者-消费者模型中的消费者线程。
    扫描线程通过 input_queue 发送点击指令，MouseWorker 串行执行鼠标操作。
    """

    input_queue: Optional[queue.Queue] = None   # 由入口（BotManager / 命令行）注入
    instance: Optional["MouseWorker"] = None    # 最近启动的实例，供存活检查用

    def __init__(self, input_queue: queue.Queue, mouse: MouseController,
                 stop_event: Optional[threading.Event] = None):
        self.input_queue = input_queue
        self.mouse = mouse
        self.thread = None
        self._stop_event = stop_event if stop_event is not None else STOP_EVENT

    def start(self):
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        MouseWorker.instance = self

    @classmethod
    def is_alive(cls) -> bool:
        """共享鼠标线程是否还在运行"""
        inst = cls.instance
        return bool(inst and inst.thread and inst.thread.is_alive())

    @classmethod
    def revive(cls) -> bool:
        """
        鼠标线程意外退出后重新拉起（队列不变，积压的点击会继续执行）。

        过去这里的 _run 没有异常保护：pyautogui 抛一次异常（例如鼠标停在
        屏幕角落触发 FAILSAFE）线程就静默死掉，之后所有点击只进不出，
        而扫描线程仍照常打印「🖱 点击 ...」，看起来像点成功了实际没动。
        """
        inst = cls.instance
        if inst is None:
            return False
        if cls.is_alive():
            return True
        if inst._stop_event.is_set():
            return False

        logging.getLogger("yys.MouseWorker").warning(
            "鼠标执行线程此前已退出，正在重启")
        inst.thread = threading.Thread(
            target=inst._run, daemon=True, name="MouseWorker-restart")
        inst.thread.start()
        return True

    @classmethod
    def drain(cls, owner: "str | None" = None) -> int:
        """
        丢弃队列里尚未执行的点击，返回丢弃条数。

        停止实例时用：队列中的点击对应的是「按下停止之前」的界面，
        界面早就变了，继续执行只会让鼠标在用户喊停之后还在动。

        :param owner: 只丢弃该 Bot 投递的点击；None 表示全部丢弃
        """
        q = cls.input_queue
        if q is None:
            return 0
        kept = []
        dropped = 0
        while True:
            try:
                item = q.get_nowait()
            except queue.Empty:
                break
            if item == POISON_PILL:
                kept.append(item)      # 退出信号必须保留
                continue
            item_owner = item[3] if len(item) > 3 else None
            if owner is None or item_owner is None or item_owner == owner:
                dropped += 1
            else:
                kept.append(item)
        for item in kept:
            q.put(item)
        return dropped

    def _run(self):
        logger = logging.getLogger("yys.MouseWorker")
        while not self._stop_event.is_set():
            try:
                item = self.input_queue.get(timeout=0.1)
            except queue.Empty:
                continue

            if item == POISON_PILL:
                logger.info("收到退出指令，MouseWorker 结束")
                break

            # 队列项格式：前 3 位是点击内容，第 4 位是投递方 Bot 名（可选，
            # 供停止时精确丢弃该 Bot 尚未执行的点击）
            class_name, click_x, click_y = item[0], item[1], item[2]
            # 单次点击失败不能拖垮整个线程，否则后续点击会全部静默丢失
            try:
                self.mouse.move_and_click(click_x, click_y)
                logger.debug(f"点击已执行 @({click_x},{click_y})")
            except Exception as e:
                logger.error(
                    f"鼠标点击执行失败 @({click_x},{click_y}): "
                    f"{type(e).__name__}: {e}")
                continue

            # class_name 是完整阶段键（"01_start_begin"），也可能是裸 tag，
            # 用包含匹配判断「起始阶段」，避免键格式变化后追点静默失效
            _cls = str(class_name).lower()
            if "start" in _cls or "begin" in _cls:
                try:
                    time.sleep(human_delay(0.5, 0.5))
                    # 第一下点完光标已被随机挪开，补点必须重新移回原位再点，
                    # 否则会在几百像素外的地方空点一下
                    self.mouse.move_and_click(click_x, click_y)
                except Exception as e:
                    logger.error(f"begin 追点失败: {type(e).__name__}: {e}")


# ============================================================
#  AutoClickerBot — 核心 Bot 逻辑
# ============================================================
class AutoClickerBot:
    """
    单个阴阳师窗口的自动点击 Bot。
    截图 -> 模板匹配 -> 投喂点击指令给 MouseWorker。
    """

    def __init__(
        self,
        name: str,
        window_title: str,
        task_dir: str,
        limit: int = 200,
        threshold: float = 0.75,
        # ---- 循环优化参数 ----
        match_confirm_count: int = 2,
        detection_scale: float = 0.5,
        # ---- 未匹配重试 ----
        # miss_timeout: 非战斗状态下，连续多少「秒」找不到目标算一轮失败
        #               （旧版是数次数 miss_threshold，因扫描间隔动态变化而失真）
        # battle_timeout: 点完 begin 后最多等多少「秒」还没打到 end，判定战斗等待超时
        miss_timeout: float = 8.0,
        battle_timeout: float = 240.0,
        miss_retry_sleep: float = 2.0,
        # ---- 同阶段反复点击保护 ----
        same_stage_click_limit: int = 10,
        # ---- 休息机制 ----
        rest_rounds: int = 50,
        rest_rounds_var: int = 10,
        rest_seconds: int = 30,
        rest_seconds_var: int = 5,
        # ---- 启停控制 ----
        stop_event: Optional[threading.Event] = None,
        pause_event: Optional[threading.Event] = None,
    ):
        self.name = name
        self.window_title = window_title
        self.limit = limit
        self.threshold = threshold
        # ---- 循环优化参数 ----
        self.match_confirm_count = match_confirm_count
        self.detection_scale = detection_scale
        # ---- 未匹配重试 ----
        self.miss_timeout = float(miss_timeout)
        self.battle_timeout = float(battle_timeout)
        self.miss_retry_sleep = miss_retry_sleep
        # ---- 同阶段反复点击保护 ----
        self.same_stage_click_limit = same_stage_click_limit

        # ---- 计数 & 状态（需先于休息机制初始化）----
        self.click_count = 0
        self._limit_wait_logged = False   # 达到次数上限后是否已提示「等这把打完」

        # ---- 休息机制 ----
        self.rest_rounds = rest_rounds
        self.rest_rounds_var = rest_rounds_var
        self.rest_seconds = rest_seconds
        self.rest_seconds_var = rest_seconds_var
        self._rest_count = 0
        # 首次休息目标（按「把」计）在 round_count 初始化后再算，见下方回合统计

        # ---- 启停控制 ----
        self._stop_event = stop_event if stop_event is not None else STOP_EVENT
        self._pause_event = pause_event

        # Logger
        self.log = setup_bot_logger(name)
        self._log_buf: list[str] = []  # 供前端读取的日志缓冲
        # 挂载缓冲 handler
        buf_handler = logging.StreamHandler(_LogBufferStream(self._log_buf))
        buf_handler.setLevel(logging.INFO)
        buf_handler.setFormatter(make_log_formatter(with_date=False))
        self.log.addHandler(buf_handler)
        self.log.info(f"Bot [{name}] 就绪")
        self.log.info(
            f"超时设置: 未匹配 {self.miss_timeout:.1f} 秒 / "
            f"战斗 ≤ {min(self.battle_timeout, BATTLE_TIMEOUT_CAP):.0f} 秒"
            f"（均值×3 与上限取小）/ "
            f"重试延时 {self.miss_retry_sleep:.1f} 秒")
        self.error_score = 0      # 仅用于日志与窗口激活节奏，不参与退出判定
        self._retry_rounds = 0    # 连续失败重试轮次
        self._capture_fail_streak = 0    # 连续截图失败次数（成功一帧即清零）
        self._step_exception_streak = 0  # 连续识别异常帧数（成功一帧即清零）
        self._last_activate_warn = 0.0   # 上次「激活窗口失败」告警的时间，用于限频
        self._privilege_checked = False  # 是否已检查过游戏窗口的权限级别
        # 未匹配计时：本轮「确实在扫、且预期有目标、却没找到」的起点（0 = 不计时）
        self._unmatched_since = 0.0
        # 战斗等待截止时间：点 begin 时按 battle_timeout 算好，超过即判定战斗超时
        self._battle_wait_until = 0.0
        self._battle_wait_limit = 0.0   # 本把实际使用的等待上限（动态值，用于日志）
        self.click_times: list[float] = []
        self.click_positions: list[tuple[str, int, int]] = []  # (class_name, x, y)
        self._window_rect = None  # [left, top, width, height] 最近一次截图窗口位置
        self.start_time = None
        self.stop_requested = False
        self._pause_begin = 0.0       # 本次暂停起始时间（0 表示未暂停）
        self._total_paused = 0.0      # 累计暂停时长（秒）
        self._final_elapsed = 0       # 停止时冻结的耗时

        # 缓存按类别统计
        self.class_counts: dict[str, int] = defaultdict(int)

        # ---- 阶段追踪（仅用于战斗时长统计，不参与模板扫描决策） ----
        self._in_battle = False               # 当前是否在战斗中（begin 点击后 → 第一次 end 点击前）
        self._battle_ended = True             # 本轮是否已记录战斗时长

        # ---- 消抖 ----
        self._match_streak = 0                # 连续匹配计数
        self._last_matched_class = ""         # 上一次匹配的目标类别
        self._last_matched_x = 0              # 上一次匹配的中心 x
        self._last_matched_y = 0              # 上一次匹配的中心 y

        # ---- 点击冷却 ----
        self._end_cooldown_until = 0.0        # end 阶段点击后的冷却截止时间（防连点）

        # ---- 快速扫描模式（end 点击后开启，缩短 miss sleep 以快速捕获下一个 end）----
        # ---- 动态战斗时长预估 ----
        self.estimated_battle_time: float = 0.0  # 简单平均预估战斗时长（秒），初始 0
        self.battle_total_duration: float = 0.0  # 所有战斗总时长（累加）
        self.battle_count: int = 0               # 已完成战斗次数
        self.battle_start_time: float = 0.0      # 本次战斗开始时间戳

        # ---- 回合耗时统计（一把 = start 阶段 → end 阶段）----
        self.round_total_duration: float = 0.0   # 已完成回合总耗时（累加）
        self.round_count: int = 0                # 已完成回合数（每把 = begin→下一次 begin）
        # 首次休息目标：打完 (rest_rounds ± var) 把后触发（按把数，不按点击数）
        self._next_rest_at = self._calc_next_rest()

        # 模板管理
        self.template_manager = TemplateManager(task_dir, threshold)
        self.template_manager.load()

        # ---- 阶段识别与可点击区域 ----
        # 首次进入某阶段时全扫学习一次，之后直接从区域中采样点击
        # 每项形如 {"cells": [(x, y, w, h), ...], "total_area": int}，键为阶段 key
        self._regions: dict[str, dict | None] = {}
        self._region_outline: dict[str, list] = {}   # 外轮廓（只画边框）
        self._region_settled: set[str] = set()       # 已等过过渡动画的阶段
        self._stage: "Stage | None" = None           # 当前识别到的阶段
        self._stage_cursor = 0                       # 本把已到达的最大阶段序号（单调前进）
        self._stage_clicks: dict[str, int] = defaultdict(int)   # 本把各阶段点击次数
        self._stage_last_click: dict[str, float] = {}           # 各阶段上次点击时间（退避用）
        self._start_retry_until = 0.0                # start 阶段的重试窗口截止时间
        self._same_stage_clicks = 0                  # 连续点击同一阶段的次数

        # 鼠标：点击统一交给 BotManager 的共享队列（MouseWorker 串行执行），
        # 这里不再各自持有一个 MouseController，否则速度配置会有两套来源
        #
        # 基准缩放因子：首次截图后计算一次，后续复用
        self._base_scale = None

    # ----------------------------------------------------------
    #  模板加载报告
    # ----------------------------------------------------------
    def print_template_report(self):
        """启动前打印模板加载情况"""
        self.log.info("=" * 50)
        self.log.info(f"窗口: {self.window_title}")
        self.log.info(f"副本: {self.template_manager.task_dir}")
        self.log.info(f"次数上限: {'无限制' if self.limit == 0 else self.limit}")
        self.log.info(f"阈值: {self.threshold}")
        for line in self.template_manager.get_load_report():
            self.log.info(line)
        if self.template_manager.is_empty:
            self.log.warning("⚠ 所有模板目录均为空，Bot 将无法匹配任何按钮！")
        self.log.info("=" * 50)

    # ----------------------------------------------------------
    #  截图窗口
    # ----------------------------------------------------------
    @staticmethod
    def _is_elevated_pid(pid) -> Optional[bool]:
        """
        判断指定进程是否以管理员权限运行。
        :return True/False；None 表示读不到（多数情况是对方权限更高）
        """
        try:
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.windll.kernel32
            adv = ctypes.windll.advapi32
            h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return None
            try:
                tok = wintypes.HANDLE()
                if not adv.OpenProcessToken(h, 0x0008, ctypes.byref(tok)):  # TOKEN_QUERY
                    return None
                try:
                    buf = ctypes.create_string_buffer(4)
                    ret = wintypes.DWORD()
                    # TokenElevation = 20
                    adv.GetTokenInformation(tok, 20, buf, 4, ctypes.byref(ret))
                    return bool(
                        ctypes.cast(buf, ctypes.POINTER(wintypes.DWORD)).contents.value)
                finally:
                    k32.CloseHandle(tok)
            finally:
                k32.CloseHandle(h)
        except Exception:
            return None

    def _check_target_privilege(self, hwnd):
        """
        检查「游戏进程」与「本程序」的权限级别是否一致。

        Windows 的 UIPI（用户界面特权隔离）规定：低权限进程不能把鼠标/键盘
        输入发送给高权限窗口，这些点击会被系统直接丢弃。表现就是日志里明明
        有「🖱 点击」，游戏却毫无反应，而且也无法把该窗口置顶（SetWindowPos
        返回「拒绝访问」）。每次运行只检查一次。
        """
        if self._privilege_checked:
            return
        self._privilege_checked = True
        try:
            import win32process
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
        except Exception:
            return

        mine = self._is_elevated_pid(os.getpid())
        theirs = self._is_elevated_pid(pid)

        if theirs is None:
            self.log.warning(
                "⚠ 读不到游戏进程权限（通常说明它以管理员身份运行）。"
                "如果日志有点击但游戏没反应，请右键「以管理员身份运行」本程序")
            return
        if theirs and not mine:
            self.log.warning(
                "⚠ 权限不匹配：游戏以管理员身份运行，本程序未提权\n"
                "  Windows 会直接丢弃发往高权限窗口的鼠标点击（日志有点击、游戏无反应），"
                "窗口也无法置顶。请以管理员身份重新运行本程序")
        elif theirs and mine:
            self.log.info("权限检查通过：游戏与本程序均为管理员权限")

    def _activate_window(self, hwnd) -> bool:
        """把目标窗口切到前台（实现见模块级 activate_window，前端截图也用它）"""
        return activate_window(hwnd, self.log)

    def capture_window(self, force_activate=True):
        """
        截取目标窗口。窗口查找与取景都走模块级 find_window_handle / grab_window_region，
        与前端「截取模板」的截图完全同一套逻辑，保证两边画面同尺度。
        :return: (PIL.Image, left, top, width, height) 或 None (窗口未找到)
        """
        try:
            title = (self.window_title or "").strip()
            if not title:
                self.log.warning("未设置目标窗口标题")
                return None

            target_hwnd, self_matched, all_titles = find_window_handle(title)

            if not target_hwnd:
                if self_matched:
                    self.log.warning(
                        f"目标窗口「{title}」指向的是本程序自己的控制面板窗口，"
                        f"不是模拟器。请在实例卡片里重新选择模拟器窗口")
                else:
                    self.log.warning(
                        f"未找到包含 '{title}' 的窗口。"
                        f"当前可见窗口: {all_titles[:8]}")
                return None

            # 首次找到窗口时检查一次权限级别（UIPI 会让点击被静默丢弃）
            self._check_target_privilege(target_hwnd)

            # ---- 还原 & 激活 ----
            if not restore_and_activate(target_hwnd, self.log, activate=force_activate):
                # 每帧都会尝试激活，失败时限制告警频率，避免刷屏
                now_w = time.time()
                if now_w - self._last_activate_warn > 10.0:
                    self._last_activate_warn = now_w
                    self.log.warning(
                        "⚠ 无法把游戏窗口切到前台（Windows 前台锁定）。"
                        "截图是抓屏幕区域，窗口被别的窗口挡住时识别会失效，"
                        "请手动点一下游戏窗口或暂停控制面板的遮挡")
            if force_activate:
                time.sleep(0.15)

            # ---- 取景截图（与前端截取模板共用同一份逻辑）----
            grabbed = grab_window_region(target_hwnd, self.log)
            if grabbed is None:
                return None
            screenshot, left, top, cw, ch = grabbed
            self._window_rect = [left, top, cw, ch]
            return screenshot, left, top, cw, ch

        except Exception as e:
            self.log.warning(f"截图异常: {type(e).__name__}: {e}")
            return None

    # ----------------------------------------------------------
    #  处理匹配结果 -> 点击
    # ----------------------------------------------------------
    # ----------------------------------------------------------
    #  匹配结果处理（消抖 + 点击 + 状态切换）
    # ----------------------------------------------------------
    def _confirm_match(self, match_result, window_left, window_top):
        """消抖：要求同一目标连续匹配 N 次才触发点击"""
        key, (x, y, w, h), tname, score = match_result
        stage = self.template_manager.stage_map.get(key)
        cx = x + w // 2
        cy = y + h // 2

        # 是否与上一次匹配为同一目标（阶段相同 + 位置接近）
        # 注意：区域采样模式下坐标是「可点击区域内的随机点」（w/h 已置 0），
        # 同一阶段每帧取的都不是同一个像素，位置比较会让确认计数几乎攒不满，
        # 表现为「一直看到某阶段却半天才点」。这类点只按阶段计数。
        same_target = self._last_matched_class == key
        if same_target and (w > 0 or h > 0):
            same_target = (abs(cx - self._last_matched_x) < 25
                           and abs(cy - self._last_matched_y) < 25)
        if same_target:
            self._match_streak += 1
        else:
            self._match_streak = 1

        self._last_matched_class = key
        self._last_matched_x = cx
        self._last_matched_y = cy

        # end 阶段一帧确认即可（结算界面多个按钮轮番出现，无需消抖）
        needed = 1 if (stage is not None and stage.is_end) else self.match_confirm_count
        if self._match_streak >= needed:
            self._match_streak = 0
            self.error_score = 0
            self._retry_rounds = 0
            # 连续同阶段才复核标志：首次进入该阶段时标志刚确认过，不必重复查
            if (STAGE_RECHECK_ENABLED and stage is not None
                    and self._same_stage_clicks >= 1
                    and not self._recheck_stage(stage)):
                self.log.info(
                    f"⏭ {key} 阶段标志复核未通过（界面已开始切换），丢弃本次点击")
                return
            self._click_target(match_result, window_left, window_top)
        else:
            pass  # 攒确认次数中，不操作

    def _click_target(self, match_result, window_left, window_top):
        """执行点击并更新阶段进度 / 计时"""
        key, (x, y, w, h), tname, score = match_result
        stage = self.template_manager.stage_map.get(key)
        if stage is None:
            return

        now = time.time()
        clicks = self._stage_clicks.get(key, 0)

        # ---- 退避与冷却 ----
        # 同一阶段连续点击时按次数拉长间隔（0.5 → 1 → 1.5 秒）：界面要点两三次
        # （begin、结算页）时既不会狂点，也不会像旧版那样被 3 秒硬冷却直接挡掉。
        if clicks > 0:
            wait = STAGE_CLICK_BACKOFF[min(clicks - 1, len(STAGE_CLICK_BACKOFF) - 1)]
            if now - self._stage_last_click.get(key, 0.0) < wait:
                self._match_streak = 0
                return
        if stage.is_end and now < self._end_cooldown_until:
            self._match_streak = 0
            return
        # start 只在「重试窗口」内允许重复点击：窗口结束后不再点它，
        # 等价于旧版的 begin 长冷却，避免开战后误点开始按钮
        if stage.is_start and clicks > 0 and now > self._start_retry_until:
            self._match_streak = 0
            return

        # ---- 计算点击坐标 ----
        if w <= 0 or h <= 0:
            # w/h 为 0 表示坐标在上游已经选好（区域采样点，不再二次随机）
            click_x = window_left + x
            click_y = window_top + y
        elif stage.is_start:
            # 以模板中心为圆心、「中心到最近边的距离」为半径的圆内随机选点
            cx = window_left + x + w // 2
            cy = window_top + y + h // 2
            radius = min(w, h) / 2.0
            angle = random.uniform(0, 2 * math.pi)
            r = math.sqrt(random.random()) * radius
            click_x = int(cx + r * math.cos(angle))
            click_y = int(cy + r * math.sin(angle))
        else:
            # 固定模板模式：矩形内随机，离边缘 20px
            margin = 20
            rx1 = max(window_left + x + margin, 0)
            ry1 = max(window_top + y + margin, 0)
            rx2 = max(window_left + x + w - margin, rx1 + 1)
            ry2 = max(window_top + y + h - margin, ry1 + 1)
            click_x = random.randint(rx1, rx2)
            click_y = random.randint(ry1, ry2)

        # 记录窗口内相对坐标（与窗口在屏幕上的位置无关）：
        # 这样运行中移动窗口后，前端点击分布图不会整体错位。
        # 第一项存完整阶段键（如 02_mid_mvp），前端按阶段分别着不同颜色
        self.click_positions.append(
            (key, click_x - window_left, click_y - window_top)
        )

        # 投喂 MouseWorker：先确认鼠标线程还活着，否则点击只会堆在队列里，
        # 日志仍打印「点击」，看起来成功其实鼠标没动。
        if not MouseWorker.is_alive():
            if MouseWorker.revive():
                self.log.warning("⚠ 鼠标执行线程已退出，已自动重启")
            else:
                self.log.error("⚠ 鼠标执行线程未运行，本次点击可能不会执行")

        # 第 4 位带上本 Bot 名字：停止时可以只丢弃自己的积压点击
        MouseWorker.input_queue.put((key, click_x, click_y, self.name))

        score_text = f" 匹配度={score:.2f}" if score >= 0 else ""
        self.log.info(f"🖱 点击 {key}[{tname}] @ ({click_x}, {click_y}){score_text}")

        backlog = MouseWorker.input_queue.qsize()
        if backlog > 3:
            self.log.warning(
                f"⚠ 点击队列积压 {backlog} 条，鼠标执行线程可能已卡住或退出")

        # ---- 进度与计数 ----
        self._stage_clicks[key] = clicks + 1
        self._stage_last_click[key] = now
        self._same_stage_clicks = self._stage_clicks[key]
        self._stage_cursor = max(self._stage_cursor, stage.index)
        self.class_counts[key] += 1

        # 同阶段反复点击保护：点了上限次还停在同一页 → 判定点击失效
        if (self.same_stage_click_limit > 0
                and self._stage_clicks[key] >= self.same_stage_click_limit):
            self.log.warning(
                f"连续 {self._stage_clicks[key]} 次点击 {key} 阶段后界面无变化，"
                f"判定为点击失效或被遮挡，停止运行\n"
                f"  可能原因: 弹窗遮挡按钮、阶段标志/模板过期、游戏界面变化")
            self.stop_requested = True
            return

        elapsed = now - self.start_time if self.start_time else 0

        if stage.is_start:
            # 一把只计一次（重复补点 start 不重复计数）
            if not self._in_battle:
                self.click_count += 1
                self.click_times.append(elapsed)
                self._in_battle = True
                self._battle_ended = False
                self.battle_start_time = now
                # 战斗中改用「战斗超时」，按历史均值动态收紧，未匹配计时暂停
                self._battle_wait_limit = self._current_battle_timeout()
                self._battle_wait_until = now + self._battle_wait_limit
                self._unmatched_since = 0.0
                avg = (self.round_total_duration / self.round_count
                       if self.round_count > 0 else 0.0)
                remaining = (self.limit - self.click_count
                             if self.limit > 0 else float("inf"))
                if remaining != float("inf") and avg > 0:
                    eta_str = (f"{int(remaining * avg // 60)}m"
                               f"{int(remaining * avg % 60):02d}s")
                else:
                    eta_str = "---"
                self.log.info(
                    f"⚡ START #{self.click_count} | "
                    f"⏱ {int(elapsed // 60):d}m{int(elapsed % 60):02d}s | "
                    f"均 {avg:.1f}s/把 | "
                    f"本把超时 {self._battle_wait_limit:.0f}s | "
                    f"剩余 {remaining if remaining != float('inf') else '∞'} 次 ≈ {eta_str} | "
                    f"匹配度 {score:.2f}"
                )
            else:
                # 界面没变，需要再点一次（典型：begin 要点两下）
                self.log.info(f"↻ {key} 仍在，补点第 {self._stage_clicks[key]} 次")
            # 只在本轮第一次点击时开窗，补点不再续期：
            # 否则界面卡住（或点击被系统丢弃）时会一直补点，直到触发失效保护停机
            if clicks == 0:
                self._start_retry_until = now + START_RETRY_WINDOW

        elif stage.is_end:
            # 只记录本把第一次点中 end 作为战斗结束时间
            if self._in_battle and self.battle_start_time > 0 and not self._battle_ended:
                self._battle_ended = True
                duration = now - self.battle_start_time
                self.battle_total_duration += duration
                self.battle_count += 1
                self.estimated_battle_time = (
                    self.battle_total_duration / self.battle_count)
                self.round_total_duration += duration
                self.round_count += 1
                self.log.info(
                    f"  战斗时长: {duration:.1f}s → "
                    f"平均: {self.estimated_battle_time:.1f}s "
                    f"(共 {self.battle_count} 次)"
                )
            self._in_battle = False
            self._unmatched_since = 0.0   # 退出战斗，切回「未匹配超时」时钟
            self._end_cooldown_until = now + 1.8
            # 一把结束：清空阶段进度，等下一次 start 重新走一遍队列
            self._reset_round_progress()

        elif stage.tag == "mid":
            # 中间页点完通常还要切图 / 走弹窗动画，等 1 秒再继续扫描，
            # 否则这一秒内的画面还在变，容易当成「还停在同一页」而重复点
            self.log.info(
                f"⏳ 中间页点击后等待 {MID_CLICK_SETTLE_SEC:.1f}s 待画面稳定")
            self._interruptible_sleep(MID_CLICK_SETTLE_SEC)

    def _reset_round_progress(self):
        """一把结束后清空阶段进度（下次 start 重新开始）"""
        self._stage = None
        self._stage_cursor = 0
        self._stage_clicks.clear()
        self._stage_last_click.clear()
        self._start_retry_until = 0.0
        self._match_streak = 0
        self._last_matched_class = ""

    # ----------------------------------------------------------
    #  可中断睡眠
    # ----------------------------------------------------------
    def _interruptible_sleep(self, seconds: float):
        """切片化睡眠，每 0.05s 检查一次停止/暂停标志"""
        tick_count = int(seconds * 20)
        for _ in range(tick_count):
            if self._stop_event.is_set() or self.stop_requested:
                return
            if self._pause_event is not None and self._pause_event.is_set():
                if self._pause_begin == 0.0:
                    self._pause_begin = time.time()
                time.sleep(0.05)
                continue
            # 从暂停中恢复，累计暂停时长
            if self._pause_begin > 0:
                paused = time.time() - self._pause_begin
                self._total_paused += paused
                self._pause_begin = 0.0
                self._shift_battle_clock(paused)
            time.sleep(0.05)

    def _current_battle_timeout(self) -> float:
        """
        这次战斗最多等多久：取「平均战斗时长 × 3」与 240 秒中较小的那个。

        还没打过（没有平均值）时直接用 240 秒，否则第一把就会被自己的
        均值判超时。用户配置的战斗超时若小于 240，则作为更紧的上限生效。
        """
        cap = min(float(self.battle_timeout), BATTLE_TIMEOUT_CAP)
        if self.estimated_battle_time > 0:
            return min(cap, self.estimated_battle_time * 3.0)
        return cap

    def _shift_battle_clock(self, seconds: float):
        """
        把战斗计时整体后移。

        休息/暂停期间没有在识别，这段时间既不该算进「战斗时长」统计，
        也不该消耗「战斗超时」的额度，否则休息 30 秒就会从 240 秒里扣掉 30 秒。
        """
        if self._in_battle and seconds > 0:
            self.battle_start_time += seconds
            self._battle_wait_until += seconds

    # ----------------------------------------------------------
    #  休息机制
    # ----------------------------------------------------------
    def _calc_next_rest(self) -> int:
        """计算下一次休息的回合目标（每次随机偏移）"""
        if self.rest_rounds <= 0:
            return 999999999
        offset = random.randint(-self.rest_rounds_var, self.rest_rounds_var)
        return max(1, self.round_count + self.rest_rounds + offset)

    def _do_rest(self):
        """执行一次随机时长的休息"""
        duration = self.rest_seconds + random.randint(
            -self.rest_seconds_var, self.rest_seconds_var
        )
        duration = max(1, duration)
        self._rest_count += 1
        self.log.info(
            f"☕ 休息 #{self._rest_count} | "
            f"暂停 {duration}s，已打 {self.round_count} 把 ..."
        )
        # 休息期间没在扫描，未匹配时长不计入
        self._unmatched_since = 0.0
        rest_start = time.time()
        self._interruptible_sleep(duration)
        self._unmatched_since = 0.0
        # 若休息恰好发生在战斗中途（休息按点击数触发，可能刚点完 begin），
        # 把战斗计时后移，避免休息时间被算进战斗时长/战斗超时
        self._shift_battle_clock(time.time() - rest_start)
        self._next_rest_at = self._calc_next_rest()

    # ----------------------------------------------------------
    #  保存调试截图
    # ----------------------------------------------------------
    # ----------------------------------------------------------
    #  识别：全图扫描 / 旗帜判阶段
    # ----------------------------------------------------------
    def _judge_stage(self, detect_img, base_scale):
        """
        全扫所有阶段标志，取分数最高的一个作为当前阶段。
        用序号做单调约束：序号小于「本把已到达进度」的命中一律忽略，
        避免上一阶段残留在画面上的元素把进度拉回去、来回点。
        :return: (Stage 或 None, 分数)
        """
        stage, _, score = self.template_manager.match_stage(detect_img, base_scale)
        if stage is None:
            return None, 0.0
        if stage.index < self._stage_cursor:
            return None, score
        return stage, score

    def _recheck_stage(self, stage: "Stage") -> bool:
        """
        点击前复核阶段标志是否依然成立（只用于「连续同阶段」的点击）。

        重新截一帧并匹配全部阶段标志，确认当前仍是同一阶段。
        复核不通过说明界面已经开始切换，这次点击会落到错误的区域，应当丢弃。
        """
        capture_result = self.capture_window(force_activate=False)
        if capture_result is None:
            return False
        screenshot = capture_result[0]
        frame = cv2.cvtColor(np.array(screenshot), cv2.COLOR_RGB2BGR)
        if 0 < self.detection_scale < 1.0:
            fh, fw = frame.shape[:2]
            frame = cv2.resize(
                frame,
                (int(fw * self.detection_scale), int(fh * self.detection_scale)))
        base_scale = (self._base_scale or 1.0) * max(self.detection_scale, 0.1)
        now_stage, _ = self._judge_stage(frame, base_scale)
        return now_stage is not None and now_stage.key == stage.key

    def _detect_by_flags(self, detect_img, base_scale):
        """
        每帧扫描全部阶段标志判定当前阶段，再从该阶段学到的可点击区域里取点。
        阶段数一般只有 3 个，全扫的开销与模板总数无关。
        """
        stage, _ = self._judge_stage(detect_img, base_scale)

        if stage is None:
            self._stage = None
            self.log.info("❌ 未匹配（没有阶段标志命中）")
            return None

        key = stage.key
        if self._stage is None or self._stage.key != key:
            self._stage = stage
            self._same_stage_clicks = self._stage_clicks.get(key, 0)
            self.log.info(f"🔄 进入 {key} 阶段（tag={stage.tag}）")

        # 本把内已经点了太多次仍停在这一页 → 交给连击保护处理
        if (self.same_stage_click_limit > 0
                and self._stage_clicks.get(key, 0) >= self.same_stage_click_limit):
            self.log.warning(
                f"连续 {self._stage_clicks[key]} 次点击 {key} 阶段后界面无变化，"
                f"判定为点击失效或被遮挡，停止运行\n"
                f"  可能原因: 弹窗遮挡按钮、阶段标志/模板过期、游戏界面变化")
            self.stop_requested = True
            return None

        # 首次进入该阶段 → 等过渡动画走完，再全扫一次学习可点击区域
        if not self._regions.get(key):
            if key not in self._region_settled:
                self._region_settled.add(key)
                settle = stage.settle_sec
                self.log.info(
                    f"⏳ 首次进入 {key} 阶段，等待 {settle:.1f}s "
                    f"待画面稳定后学习可点击区域")
                self._interruptible_sleep(settle)
                return None
            self._learn_regions(detect_img, stage, base_scale)

        region = self._regions.get(key)
        if not region:
            self.log.warning(f"⚠ {key} 阶段没有可点击区域，本轮跳过点击")
            return None

        # 采样：start 在「内切圆」内均匀取点，其余在区域内按面积加权取点
        if stage.is_start:
            point = sample_point_in_cells_circle(region["cells"])
        else:
            point = sample_point_in_cells(region["cells"], region["total_area"])
        if point is None:
            self.log.warning(f"⚠ {key} 阶段可点击区域为空，本轮跳过点击")
            return None

        self.log.info(
            f"✅ 阶段 {key} | 采样点 @({point[0]},{point[1]}) "
            f"（候选像素 {region['total_area']}）")
        # w/h 置 0 表示坐标已经选好，_click_target 不再做二次随机
        return (key, (point[0], point[1], 0, 0), "region", -1)

    def _learn_regions(self, detect_img, stage: "Stage", base_scale):
        """
        全扫该阶段的所有按钮模板，把命中的框收缩后合并成「可点击区域」。
        每次运行只在首次进入该阶段时执行一次。
        """
        key = stage.key
        matches = self.template_manager.find_all_matches(detect_img, stage, base_scale)
        if not matches:
            self.log.warning(
                f"⚠ {key} 阶段学习失败：没有模板命中，暂无可点击区域")
            return

        # 收缩量 / 填缝量按 detection_scale 换算到当前识别图坐标系
        scale = max(self.detection_scale, 0.1)
        fill_gap = max(0, round(REGION_FILL_GAP_PX * scale))

        boxes = [pos for _, pos, _, _ in matches]

        # 内缩：start 用内切圆采样，圆本身不会碰到框边缘，不额外内缩；
        # 其余阶段缩掉一圈以避开背景里的相邻 UI，但受绝对上限与「最窄短边比例」
        # 约束（存在短边仅 39px 的模板，缩多了会被整个缩没）。
        thinnest = min(min(w, h) for (_, _, w, h) in boxes)
        if stage.is_start:
            shrink = 0
        else:
            cap = max(1, round(REGION_SHRINK_PX_END * scale))
            shrink = max(1, min(cap, int(thinnest * REGION_SHRINK_RATIO_END)))

        cells, total_area = build_click_cells(
            boxes, shrink=shrink, fill_gap=fill_gap)
        if not cells:
            self.log.warning(
                f"⚠ {key} 阶段学习失败：向内收缩 {shrink}px 后没有剩余范围，"
                f"模板框可能太小，建议重新截取")
            return

        self._regions[key] = {"cells": cells, "total_area": total_area}
        # 外轮廓在此算一次即可（供前端只绘制区域边框）
        # start 按内切圆绘制，与它的圆形采样保持一致
        self._region_outline[key] = cells_to_outline(
            cells, as_circle=stage.is_start)
        self.log.info(
            f"📚 学习 {key} 阶段：{len(matches)} 个模板命中 → "
            f"轮廓内缩 {shrink}px（最窄短边 {thinnest}px）、填缝 {fill_gap}px 后 "
            f"合并为 {len(cells)} 个矩形，共 {total_area} 个可点击像素")

    # ----------------------------------------------------------
    #  单步扫描（冷却避免连点）
    # ----------------------------------------------------------
    def run_step(self):
        """每帧识别当前阶段并点击对应目标"""

        # end 点击后冷却期内完全跳过扫描和识别
        now = time.time()
        if now < self._end_cooldown_until:
            # 这期间根本没在扫，不能算作「未匹配」，秒表清零
            self._unmatched_since = 0.0
            remaining = self._end_cooldown_until - now
            self._interruptible_sleep(max(remaining, 0.02))
            return

        try:
            capture_result = self.capture_window(
                force_activate=(self.error_score % 5 == 0))
            if capture_result is None:
                self.error_score += 20
                self._capture_fail_streak += 1
                self.log.warning(
                    f"窗口捕获失败（连续 {self._capture_fail_streak}/"
                    f"{CAPTURE_FAIL_LIMIT}），连续错误 +20")
                if self._capture_fail_streak >= CAPTURE_FAIL_LIMIT:
                    self.log.warning(
                        f"连续 {CAPTURE_FAIL_LIMIT} 次窗口捕获失败，停止运行\n"
                        f"  可能原因: 模拟器已关闭、窗口被最小化、游戏被完全遮挡")
                    self.stop_requested = True
                    return
                self._interruptible_sleep(0.5)
                return

            # 这一帧走到了识别环节：两个异常计数器清零（只统计「连续」）
            self._capture_fail_streak = 0
            self._step_exception_streak = 0

            screenshot, left, top, width, height = capture_result
            screenshot_cv = cv2.cvtColor(
                np.array(screenshot), cv2.COLOR_RGB2BGR)

            # 缩放加速检测
            if 0 < self.detection_scale < 1.0:
                h, w = screenshot_cv.shape[:2]
                detect_img = cv2.resize(
                    screenshot_cv,
                    (int(w * self.detection_scale),
                     int(h * self.detection_scale)))
            else:
                detect_img = screenshot_cv

            # 基准缩放因子：窗口宽 ÷ baseline宽 → 乘 detection_scale（截帧缩小因子）
            if self._base_scale is None:
                if self.template_manager.baseline_w:
                    self._base_scale = width / self.template_manager.baseline_w
                else:
                    self._base_scale = 1.0
                self.log.info(f"窗口宽={width}, baseline宽={self.template_manager.baseline_w or 'N/A'}, "
                              f"detection_scale={self.detection_scale}, "
                              f"base_scale={self._base_scale:.3f}")
            # 模板缩放 = 窗口缩放比 × 截帧缩放比（因为匹配在缩小后的 detect_img 上进行）
            base_scale = self._base_scale * max(self.detection_scale, 0.1)

            # ---- 识别：全扫阶段标志，命中哪个阶段就从它的可点击区域取点 ----
            match_result = self._detect_by_flags(detect_img, base_scale)

            # 坐标还原（detection_scale 缩放后的坐标映射回原始尺寸）
            if match_result:
                cls_name, (x, y, mw, mh), tname, score = match_result
                if 0 < self.detection_scale < 1.0:
                    scale_back = 1.0 / self.detection_scale
                    match_result = (
                        cls_name,
                        (int(x * scale_back), int(y * scale_back),
                         int(mw * scale_back), int(mh * scale_back)),
                        tname, score)

            if match_result:
                # 只要这一帧匹配到目标（哪怕还要消抖确认），秒表就清零
                self._unmatched_since = 0.0
                self._confirm_match(match_result, left, top)
            else:
                self._match_streak = 0
                self.error_score += 1
                now = time.time()

                # ============ 两套时钟，互斥 ============
                if self._in_battle:
                    # 时钟 A：战斗等待（点完 begin → 首次点中 end）
                    # 这段本来就没有可点目标，不能按「未匹配」计，只受 battle_timeout 约束
                    self._unmatched_since = 0.0
                    if now >= self._battle_wait_until:
                        waited = now - self.battle_start_time
                        avg_txt = (f"平均 {self.estimated_battle_time:.0f}s × 3"
                                   if self.estimated_battle_time > 0
                                   else "暂无历史均值，用保底上限")
                        self.log.warning(
                            f"战斗等待超时（已 {waited:.0f} 秒没匹配到 end，"
                            f"上限 {self._battle_wait_limit:.0f} 秒 = {avg_txt}），"
                            f"停止运行\n"
                            f"  可能原因: 这把打得比平时久很多、end 模板过时、"
                            f"游戏卡在演出里")
                        self.stop_requested = True
                        return
                else:
                    # 时钟 B：非战斗（开始界面 / 结算界面 / 加载切图）
                    if self._unmatched_since == 0.0:
                        self._unmatched_since = now          # 起表
                    elif now - self._unmatched_since >= self.miss_timeout:
                        self._unmatched_since = 0.0
                        self._retry_rounds += 1
                        if self._retry_rounds >= 3:
                            self.log.warning(
                                f"连续 3 轮每轮 {self.miss_timeout:.1f} 秒未匹配到目标，停止运行\n"
                                f"  可能原因: 模板过时、窗口遮挡、游戏界面变化")
                            self.stop_requested = True
                            return
                        self.log.warning(
                            f"连续 {self.miss_timeout:.1f} 秒未匹配，延时重试 "
                            f"({self._retry_rounds}/3)")
                        self._interruptible_sleep(self.miss_retry_sleep)
                        return  # 跳过动态 sleep

                # 动态扫描间隔
                if self._in_battle:
                    estimated_seconds = (
                        self.estimated_battle_time
                        if self.estimated_battle_time > 0 else 4.0)
                    elapsed = time.time() - self.battle_start_time
                    remaining = max(0.0, estimated_seconds - elapsed)
                    if remaining <= estimated_seconds / 4.0:
                        self._interruptible_sleep(0.25)               # 最后1/4高频
                    else:
                        self._interruptible_sleep(1.0)              # 战斗中期低频
                else:
                    self._interruptible_sleep(0.3)              # 常速

        except Exception as e:
            self.log.error(
                f"run_step 异常: {type(e).__name__}: {e}", exc_info=True)
            self.error_score += 5
            self._step_exception_streak += 1
            if self._step_exception_streak >= STEP_EXCEPTION_LIMIT:
                self.log.error(
                    f"连续 {STEP_EXCEPTION_LIMIT} 帧识别异常，停止运行")
                self.stop_requested = True

    # ----------------------------------------------------------
    #  主循环
    # ----------------------------------------------------------
    def run(self):
        """Bot 主线程入口：按阶段列表识别 + 点击的循环"""
        self.log.info("开始运行 ...")
        self.start_time = time.time()

        if not self.template_manager.has_stages:
            self.log.error(
                "没有任何可用的阶段标志，无法判断当前界面。"
                "请先在「模板管理」里为每个阶段设置标志图，再启动运行")
            self.stop_requested = True
            return

        # ---- 启动时主动激活一次目标窗口 ----
        self.log.info(f"正在定位窗口: {self.window_title}")
        result = self.capture_window(force_activate=True)
        if result is None:
            self.log.warning("首次激活窗口失败，将在后续循环中重试")
        else:
            self.log.info("窗口已激活到前台")

        while not self._stop_event.is_set() and not self.stop_requested:
            # ---- 暂停追踪（run_step 外也会被暂停，需计入）----
            if self._pause_event is not None and self._pause_event.is_set():
                if self._pause_begin == 0.0:
                    self._pause_begin = time.time()
                time.sleep(0.1)
                continue
            if self._pause_begin > 0:
                paused = time.time() - self._pause_begin
                self._total_paused += paused
                self._pause_begin = 0.0
                self._unmatched_since = 0.0   # 暂停期间不算未匹配，恢复后重新起表
                self._shift_battle_clock(paused)

            if 0 < self.limit <= self.click_count:
                # 达到上限时如果最后一把已经开打，就等它打完再停：
                # 否则「打 10 次」会在第 10 把刚点 begin 时就被切断，
                # 完成数只有 9 把（补点不计数，但最后一把没算完）
                if self._in_battle:
                    if not self._limit_wait_logged:
                        self.log.info(
                            f"已达到次数上限 ({self.limit})，等当前这把打完再结束")
                        self._limit_wait_logged = True
                else:
                    self.log.info(f"已达到次数上限 ({self.limit})，正常结束")
                    self.stop_requested = True
                    break

            self.run_step()

            # 休息检查：按「已完成的把数」判断。
            # round_count 在点下新一把 begin 时递增，此时人在战斗里，所以等
            # 本把第一次点中 end（_in_battle 转 False，回到结算界面）后再休息。
            if (self.rest_rounds > 0
                    and self.round_count >= self._next_rest_at
                    and not self._in_battle):
                self._do_rest()

            # 退出判定统一由 run_step 内部负责：
            #   未匹配 → miss_timeout（时长，见 run_step）
            #   截图失败 / 识别异常 → CAPTURE_FAIL_LIMIT / STEP_EXCEPTION_LIMIT
            # 此处不再用 error_score 硬编码阈值抢先 break，否则会把
            # 「连续未匹配上限」调大的设置直接废掉（旧 bug）。

        elapsed = time.time() - self.start_time
        self._final_elapsed = max(0, int(elapsed - self._total_paused))
        self.log.info(
            f"运行结束 | 总点击: {self.click_count} | "
            f"耗时: {int(elapsed // 60)}m{int(elapsed % 60):02d}s | "
            f"各类点击: {dict(self.class_counts)}"
        )

    # ----------------------------------------------------------
    #  状态查询 / 重置
    # ----------------------------------------------------------
    @property
    def status(self) -> dict:
        """返回当前运行状态字典，供前端轮询"""
        is_running = not self.stop_requested and not self._stop_event.is_set()
        if not self.start_time:
            elapsed = 0
        elif not is_running:
            elapsed = self._final_elapsed
        else:
            now = time.time()
            elapsed = now - self.start_time - self._total_paused
            if self._pause_begin > 0:
                elapsed -= (now - self._pause_begin)
            elapsed = max(0, int(elapsed))
        avg_round = (self.round_total_duration / self.round_count
                     if self.round_count > 0 else 0)
        avg_battle = (self.battle_total_duration / self.battle_count
                      if self.battle_count > 0 else 0)
        remaining = max(0, self.limit - self.round_count)
        eta = avg_round * remaining if avg_round > 0 else 0
        click_positions = self.click_positions[-500:]

        # 可点击区域外轮廓：轮廓是在缩小后的识别图上算的，这里换算回窗口原始坐标，
        # 供前端在点击分布图上叠加显示（只画外边框）
        scale_back = 1.0 / max(self.detection_scale, 0.1)
        region_outline: dict[str, list] = {}
        for _st, _polys in (self._region_outline or {}).items():
            if _polys:
                region_outline[_st] = [
                    [[int(px * scale_back), int(py * scale_back)] for (px, py) in poly]
                    for poly in _polys
                ]

        return {
            "running": is_running,
            "round": self.round_count,
            "battle_count": self.battle_count,
            "total_clicks": self.click_count,
            "elapsed": int(elapsed),
            "avg_duration": avg_round,
            "avg_battle_time": avg_battle,
            "remaining": remaining,
            "eta": int(eta),
            "rest_count": self._rest_count,
            "last_log": self._log_buf[-1] if self._log_buf else "",
            "click_positions": click_positions,
            "window_rect": self._window_rect,
            "region_outline": region_outline,
        }

    def reset(self):
        """重置统计，准备下一轮运行"""
        self.round_count = 0
        self.click_count = 0
        self._limit_wait_logged = False
        self.class_counts = {}
        self.click_times.clear()
        self.round_total_duration = 0.0
        self.start_time = None
        self.stop_requested = False
        self._pause_begin = 0.0
        self._total_paused = 0.0
        self._match_streak = 0
        self._last_matched_class = None
        self._end_cooldown_until = 0.0
        self.error_score = 0
        self._retry_rounds = 0
        self._capture_fail_streak = 0
        self._step_exception_streak = 0
        self._unmatched_since = 0.0
        self._battle_wait_until = 0.0
        self._battle_wait_limit = 0.0
        self._in_battle = False
        self._battle_ended = True
        # 阶段识别缓存：清空后下次进入阶段会重新学习
        self._regions = {}
        self._region_outline = {}
        self._region_settled = set()
        self._stage = None
        self._stage_cursor = 0
        self._stage_clicks.clear()
        self._stage_last_click.clear()
        self._start_retry_until = 0.0
        self._same_stage_clicks = 0
        self._rest_count = 0
        self._next_rest_at = self._calc_next_rest()

    # ----------------------------------------------------------
    #  效率曲线图 (非阻塞)
    # ----------------------------------------------------------
    def plot_history(self, save_path: str = None):
        """保存点击效率曲线到文件"""
        if len(self.click_times) < 2:
            self.log.info("数据太少，无法绘制统计图")
            return

        intervals = [self.click_times[0]]
        for i in range(1, len(self.click_times)):
            intervals.append(self.click_times[i] - self.click_times[i - 1])

        fig, (ax1, ax2, ax3) = plt.subplots(1, 3, figsize=(18, 4))
        fig.suptitle(f"{self.name} 点击效率统计")

        # 左图：累积时间 vs 次数
        ax1.plot(range(1, len(self.click_times) + 1), self.click_times, marker=".")
        ax1.set_xlabel("点击次数")
        ax1.set_ylabel("累计时间 (s)")
        ax1.set_title("累计耗时")
        ax1.grid(True, alpha=0.3)

        # 中图：每次间隔分布
        ax2.plot(range(1, len(intervals) + 1), intervals, marker=".", color="orange")
        ax2.set_xlabel("间隔序号")
        ax2.set_ylabel("间隔 (s)")
        ax2.set_title("每次点击间隔")
        ax2.grid(True, alpha=0.3)

        mean_interval = np.mean(intervals)
        ax2.axhline(y=mean_interval, color="red", linestyle="--",
                     label=f"平均间隔 {mean_interval:.1f}s")
        ax2.legend()

        # 右图：点击位置散点图
        if self.click_positions:
            def _grp(cls):
                s = str(cls).lower()
                return "begin" if ("start" in s or "begin" in s) else "end"

            begin_pts = [(px, py) for cls, px, py in self.click_positions
                         if _grp(cls) == "begin"]
            end_pts = [(px, py) for cls, px, py in self.click_positions
                       if _grp(cls) == "end"]

            if begin_pts:
                bx, by = zip(*begin_pts)
                ax3.scatter(bx, by, c="green", marker="o", alpha=0.6,
                            s=30, label=f"begin ({len(begin_pts)})")
            if end_pts:
                ex, ey = zip(*end_pts)
                ax3.scatter(ex, ey, c="blue", marker="x", alpha=0.6,
                            s=40, label=f"end ({len(end_pts)})")

            ax3.set_xlabel("窗口内 X")
            ax3.set_ylabel("窗口内 Y")
            ax3.set_title("点击位置分布")
            ax3.invert_yaxis()  # Y 轴朝下（与窗口内坐标一致）
            ax3.legend()
            ax3.grid(True, alpha=0.3)
        else:
            ax3.text(0.5, 0.5, "无位置数据", transform=ax3.transAxes,
                     ha="center", va="center", color="gray")
            ax3.set_title("点击位置分布")

        plt.tight_layout()

        if save_path is None:
            save_path = f"{self.name}_stats.png"

        plt.savefig(save_path, dpi=150)
        plt.close(fig)
        self.log.info(f"统计图已保存: {save_path}")


# ============================================================
#  __main__ — 入口
# ============================================================
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="阴阳师自动点击脚本 v3.0")
    parser.add_argument("-c", "--config", default=None,
                        help="配置文件路径 (默认: 脚本同目录下的 config.json)")
    parser.add_argument("--dry-run", action="store_true",
                        help="仅加载配置并打印信息，不实际运行")
    args = parser.parse_args()

    # ----------------------------------------------------------
    #  Step 1: 加载配置
    # ----------------------------------------------------------
    try:
        config = load_config(args.config)
    except (FileNotFoundError, ValueError) as e:
        print(f"[错误] 配置文件加载失败: {e}")
        sys.exit(1)

    bots_cfg = config["bots"]
    global_cfg = config.get("global", {})

    # ----------------------------------------------------------
    #  Step 2: 初始化日志系统
    # ----------------------------------------------------------
    log_dir = global_cfg.get("log_dir", "logs")
    log_path = setup_root_file_logger(log_dir)
    print(f"[系统] 日志文件: {os.path.abspath(log_path)}")

    sys_log = logging.getLogger("yys.system")
    sys_log.info(f"配置文件加载成功，共 {len(bots_cfg)} 个 Bot")

    # ----------------------------------------------------------
    #  Step 3: 注册 F12 紧急停止热键
    # ----------------------------------------------------------
    keyboard.add_hotkey("F12", emergency_stop)
    sys_log.info("已注册 F12 紧急停止热键")

    # 脚本被 Ctrl+C 终止时的回调
    def on_terminate():
        if not STOP_EVENT.is_set():
            print("\n[系统] 收到终止信号，正在停止所有 Bot...")
            STOP_EVENT.set()

    import signal
    signal.signal(signal.SIGINT, lambda s, f: on_terminate())

    # ----------------------------------------------------------
    #  Step 4: 创建 Bot 实例
    # ----------------------------------------------------------
    bots = []
    for cfg in bots_cfg:
        bot = AutoClickerBot(
            name=cfg["name"],
            window_title=cfg["window_title"],
            task_dir=cfg["task_dir"],
            limit=cfg.get("limit", 200),
            threshold=cfg.get("threshold", 0.75),
            # ---- 循环优化参数 ----
            match_confirm_count=global_cfg.get("match_confirm_count", 2),
            detection_scale=global_cfg.get("detection_scale", 0.5),
            miss_timeout=resolve_miss_timeout(global_cfg),
            battle_timeout=resolve_battle_timeout(global_cfg),
            miss_retry_sleep=global_cfg.get("miss_retry_sleep", 2.0),
            same_stage_click_limit=global_cfg.get("same_stage_click_limit", 10),
        )
        bots.append(bot)

    # 打印模板加载报告
    for bot in bots:
        bot.print_template_report()

    if args.dry_run:
        print("\n[系统] --dry-run 模式，仅检查配置，不启动 Bot。")
        sys.exit(0)

    # ----------------------------------------------------------
    #  Step 5: 启动 MouseWorker 共享线程
    # ----------------------------------------------------------
    shared_queue = queue.Queue()  # 所有 Bot 共享的点击指令队列（线程安全）
    MouseWorker.input_queue = shared_queue
    mouse = MouseController(
        speed_min=global_cfg.get("mouse_speed_min", 1500),
        speed_max=global_cfg.get("mouse_speed_max", 2000),
    )
    worker = MouseWorker(shared_queue, mouse)
    worker.start()

    # ----------------------------------------------------------
    #  Step 6: 启动所有 Bot 线程
    # ----------------------------------------------------------
    threads = []
    for bot in bots:
        sys_log.info(f"启动 Bot: {bot.name}")
        t = threading.Thread(target=bot.run, name=f"Bot-{bot.name}", daemon=True)
        t.start()
        threads.append(t)

    print(f"\n{'=' * 50}")
    print(f"  阴阳师自动脚本 v3.0 运行中")
    print(f"  已启动 {len(bots)} 个 Bot")
    print(f"  按 F12 紧急停止")
    print(f"{'=' * 50}\n")

    # ----------------------------------------------------------
    #  Step 7: 等待所有 Bot 完成
    # ----------------------------------------------------------
    for t in threads:
        t.join()

    # 通知 MouseWorker 退出
    shared_queue.put(POISON_PILL)
    if worker.thread is not None:
        worker.thread.join(timeout=10)
        if worker.thread.is_alive():
            sys_log.warning("MouseWorker 线程未能正常退出（已超时）")
    else:
        sys_log.warning("MouseWorker 线程未启动")

    # ----------------------------------------------------------
    #  Step 8: 生成统计图
    # ----------------------------------------------------------
    print()  # 空行
    for bot in bots:
        bot.plot_history()

    sys_log.info("所有任务完成，脚本退出")
    print("[系统] Done.") 
