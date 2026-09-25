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
import threading
import time
import sys
from collections import defaultdict
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
def setup_root_file_logger(log_dir: str, level=logging.DEBUG):
    """设置根日志器，输出到文件（DEBUG 级别）"""
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "yys_auto.log")

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # 避免重复添加 handler
    if not any(isinstance(h, logging.FileHandler) for h in root.handlers):
        fh = logging.FileHandler(log_path, encoding="utf-8")
        fh.setLevel(level)
        fh.setFormatter(logging.Formatter(
            "%(asctime)s | %(name)-16s | %(levelname)-7s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        ))
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


def setup_bot_logger(name: str, level=logging.INFO):
    """为每个 Bot 创建独立 logger，带控制台输出"""
    logger = logging.getLogger(f"yys.{name}")
    logger.setLevel(logging.INFO)  # 确保 INFO 以上消息不被过滤

    # 清理旧的缓冲区 handler（bot 重启时残留）
    logger.handlers = [h for h in logger.handlers
                       if not (isinstance(h, logging.StreamHandler)
                               and isinstance(h.stream, _LogBufferStream))]

    # 控制台 handler（简洁格式，只显示消息内容）
    if not any(isinstance(h, logging.StreamHandler) for h in logger.handlers):
        ch = logging.StreamHandler(sys.stdout)
        ch.setLevel(level)
        ch.setFormatter(logging.Formatter(f"[{name}] %(message)s"))
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
        raise FileNotFoundError(
            f"配置文件不存在: {config_path}\n"
            f"请在脚本目录下创建 config.json，或通过 --config 指定路径"
        )

    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)

    # 新版前端配置（instances / template_scene）→ 转为独立运行的内部结构
    config = _adapt_frontend_config(config)

    # 基础校验
    for key in ("screens", "bots", "global"):
        if key not in config:
            raise ValueError(f"配置文件缺少必要字段: {key}")

    if not isinstance(config["bots"], list) or len(config["bots"]) == 0:
        raise ValueError("配置文件中 bots 列表不能为空")

    return config


def _adapt_frontend_config(config: dict) -> dict:
    """
    将前端控制面板使用的配置格式（instances / screen / template_scene）
    转换为独立运行入口期望的格式（screens / bots / global）。
    已经是旧格式时原样返回。
    """
    if "bots" in config and "screens" in config:
        return config

    instances = config.get("instances")
    if not instances:
        return config

    base_dir = Path(__file__).parent
    default_screen = config.get("screen", {})

    screens: dict[str, dict] = {}
    bots: list[dict] = []

    for idx, inst in enumerate(instances, 1):
        screen = inst.get("screen") or default_screen
        screen_name = f"screen-{idx}"
        screens[screen_name] = {
            "xmin": screen.get("xmin", 0),
            "xmax": screen.get("xmax", 1920),
            "ymin": screen.get("ymin", 0),
            "ymax": screen.get("ymax", 1080),
        }

        scene = inst.get("template_scene", "")
        task_dir = str(base_dir / "templates" / scene) if scene else ""

        bots.append({
            "name": inst.get("name") or inst.get("id") or f"bot-{idx}",
            "window_title": inst.get("window_title", config.get("window_title", "")),
            "task_dir": task_dir,
            "screen": screen_name,
            "limit": inst.get("limit", config.get("limit", 200)),
            "threshold": inst.get("threshold", config.get("threshold", 0.75)),
        })

    global_cfg = {k: v for k, v in config.items()
                  if k not in ("instances", "screen", "_comment")}

    return {"screens": screens, "bots": bots, "global": global_cfg}


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

class MouseController:
    def __init__(self, speed_min=2000, speed_max=2500, stop_event: Optional[threading.Event] = None):
        self.speed_min = speed_min
        self.speed_max = speed_max
        self._stop_event = stop_event if stop_event is not None else STOP_EVENT

    def click(self, hold_ms_range=CLICK_HOLD_MS_RANGE):
        """
        按压式点击：mouseDown → 随机按住 → mouseUp。
        替代 pyautogui.click() 的瞬时点击，避免被模拟器判定为"掠过"而丢弃。
        """
        hold = random.uniform(*hold_ms_range) / 1000.0
        _pause = pyautogui.PAUSE
        pyautogui.PAUSE = 0
        pyautogui.mouseDown()
        time.sleep(hold)
        pyautogui.mouseUp()
        pyautogui.PAUSE = _pause
        time.sleep(random.uniform(*CLICK_SETTLE_RANGE))

    def move_and_click(self, target_x, target_y, duration=None,
                       start_offset_range=(-80, 80),
                       control_offset_range=(-100, 100)):
        """
        从当前鼠标位置，经过一条随机贝塞尔曲线移动到目标点并点击。
        控制点数量和偏移范围随距离自适应缩放，远距离也能保证曲线自然流畅。
        """
        start_x, start_y = pyautogui.position()

        # 如果已经在目标附近则微移
        if abs(start_x - target_x) < 10 and abs(start_y - target_y) < 10:
            pyautogui.moveTo(target_x, target_y, duration=random.uniform(0.08, 0.15))
            self.click()
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
        path = arc_length_resample(raw_path, step_px=2)

        # 根据速度计算总时长
        if duration is None:
            distance = math.hypot(target_x - start_x, target_y - start_y)
            speed = random.uniform(self.speed_min, self.speed_max)
            duration = max(0.1, distance / speed)

        # 密集微步 + 变速移动（快启动慢结束，模仿人类）
        _pause = pyautogui.PAUSE
        pyautogui.PAUSE = 0
        n_points = len(path)

        # 每步权重：progress=0 → 0.3x（快）, progress=1 → 1.0x（最低基准慢）
        raw_weights = [0.3 + 0.7 * (i / (n_points - 1)) ** 1.5
                       for i in range(n_points)]
        sum_w = sum(raw_weights)

        for i, (px, py) in enumerate(path):
            if self._stop_event.is_set():
                pyautogui.PAUSE = _pause
                return
            jitter_x = random.gauss(0, 0.15)
            jitter_y = random.gauss(0, 0.15)
            step_time = duration * (raw_weights[i] / sum_w) * random.uniform(0.90, 1.10)
            pyautogui.moveTo(int(px + jitter_x), int(py + jitter_y), duration=0)
            time.sleep(step_time)

        pyautogui.PAUSE = _pause
        time.sleep(random.uniform(0.05, 0.12))
        self.click()


# ============================================================
#  TemplateManager — 模板管理 & 匹配
# ============================================================
class TemplateManager:
    def __init__(self, task_dir: str, threshold: float = 0.75):
        self.task_dir = Path(task_dir)
        self.threshold = threshold
        self.templates: dict[str, list[tuple[str, np.ndarray]]] = defaultdict(list)
        self.class_loaded: dict[str, bool] = {}
        self.baseline_w = None   # baseline.png 基准宽度
        self.baseline_h = None

    def load(self):
        """加载全部模板：begin / end + baseline.png 基准参照"""
        # 加载 baseline（基准窗口截图，用作尺寸参照）
        baseline_path = self.task_dir / "baseline.png"
        if baseline_path.exists():
            baseline_img = cv2.imread(str(baseline_path), cv2.IMREAD_COLOR)
            if baseline_img is not None:
                self.baseline_h, self.baseline_w = baseline_img.shape[:2]
                print(f"[baseline] 基准窗口 {self.baseline_w}x{self.baseline_h}")

        for class_name in ("begin", "end"):
            self._load_class(class_name)
        # 兼容旧版 mvp 目录：合并到 end
        mvp_dir = self.task_dir / "mvp"
        if mvp_dir.exists() and mvp_dir.is_dir():
            self._load_class("mvp")
            if "mvp" in self.templates:
                self.templates.setdefault("end", [])
                self.templates["end"].extend(self.templates.pop("mvp"))
                self.class_loaded["end"] = True

    def _load_class(self, class_name: str):
        folder = self.task_dir / class_name
        if not folder.exists() or not folder.is_dir():
            self.class_loaded[class_name] = False
            return

        loaded = 0
        for file_name in sorted(os.listdir(folder)):
            if file_name.lower().endswith((".png", ".jpg", ".jpeg", ".bmp")):
                img_path = str(folder / file_name)
                try:
                    template = cv2.imread(img_path, cv2.IMREAD_COLOR)
                    if template is not None:
                        self.templates[class_name].append((file_name, template))
                        loaded += 1
                except Exception:
                    pass

        self.class_loaded[class_name] = loaded > 0

    def get_load_report(self) -> list[str]:
        """返回模板加载报告，标注空模板提示"""
        report = []
        total = 0
        for cls in ("begin", "end"):
            count = len(self.templates.get(cls, []))
            total += count
            if self.class_loaded.get(cls):
                report.append(f"  {cls}: {count} 个模板")
            else:
                report.append(f"  {cls}: 0 个模板 ⚠ 目录为空或不存在，将永远无法匹配到 {cls} 按钮")
        report.insert(0, f"共加载 {total} 个模板:")
        return report

    @property
    def is_empty(self) -> bool:
        return all(len(v) == 0 for v in self.templates.values())

    def find_best_match(self, screenshot_cv: np.ndarray, class_name: str,
                        base_scale: float = 1.0):
        """
        扫描指定类别的所有模板，收集所有高于阈值的匹配，随机选一个返回。
        防止每次 end / begin 都命中同一个模板，增加点击位置随机性。
        base_scale: 模板缩放基准 = 当前窗口宽 / baseline宽（无baseline时为1.0）
        :return: (matched_class, (x, y, w, h), filename, score) 或 None
        """
        if not self.class_loaded.get(class_name, False):
            return None, -1

        candidates = []
        screenshot_h, screenshot_w = screenshot_cv.shape[:2]

        for template_name, template in self.templates[class_name]:
            t_h, t_w = template.shape[:2]
            new_w, new_h = int(t_w * base_scale), int(t_h * base_scale)
            if new_w < 10 or new_h < 10 or new_w > screenshot_w or new_h > screenshot_h:
                continue
            resized = cv2.resize(template, (new_w, new_h))
            result = cv2.matchTemplate(screenshot_cv, resized, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(result)

            if max_val >= self.threshold:
                candidates.append(
                    (class_name,
                     (max_loc[0], max_loc[1], new_w, new_h),
                     template_name, max_val))

        if candidates:
            chosen = random.choice(candidates)
            _, _, _, score = chosen
            return chosen, score
        return None, 0


# ============================================================
#  MouseWorker — 独立鼠标执行线程
# ============================================================
class MouseWorker:
    """
    生产者-消费者模型中的消费者线程。
    扫描线程通过 input_queue 发送点击指令，MouseWorker 串行执行鼠标操作。
    """

    def __init__(self, input_queue: queue.Queue, mouse: MouseController,
                 stop_event: Optional[threading.Event] = None):
        self.input_queue = input_queue
        self.mouse = mouse
        self.thread = None
        self._stop_event = stop_event if stop_event is not None else STOP_EVENT

    def start(self):
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

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

            class_name, click_x, click_y = item
            self.mouse.move_and_click(click_x, click_y)

            if class_name == "begin":
                time.sleep(human_delay(0.5, 0.5))
                self.mouse.click()


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
        screen: tuple = (0, 1920, 0, 1080),
        threshold: float = 0.75,
        mouse_speed_min: int = 2000,
        mouse_speed_max: int = 2500,
        # ---- 循环优化参数 ----
        match_confirm_count: int = 2,
        detection_scale: float = 0.5,
        # ---- 未匹配重试 ----
        miss_threshold: int = 20,
        miss_retry_sleep: float = 2.0,
        # ---- 休息机制 ----
        rest_rounds: int = 50,
        rest_rounds_var: int = 10,
        rest_seconds: int = 30,
        rest_seconds_var: int = 5,
        # ---- 启停控制 ----
        stop_event: Optional[threading.Event] = None,
        pause_event: Optional[threading.Event] = None,
        status_callback: Optional[callable] = None,
    ):
        self.name = name
        self.window_title = window_title
        self.limit = limit
        self.screen = screen
        self.threshold = threshold
        # ---- 循环优化参数 ----
        self.match_confirm_count = match_confirm_count
        self.detection_scale = detection_scale
        # ---- 未匹配重试 ----
        self.miss_threshold = miss_threshold
        self.miss_retry_sleep = miss_retry_sleep

        # ---- 计数 & 状态（需先于休息机制初始化）----
        self.click_count = 0

        # ---- 休息机制 ----
        self.rest_rounds = rest_rounds
        self.rest_rounds_var = rest_rounds_var
        self.rest_seconds = rest_seconds
        self.rest_seconds_var = rest_seconds_var
        self._rest_count = 0
        # 首次休息目标：运行 (rest_rounds ± var) 把后触发
        self._next_rest_at = self._calc_next_rest()

        # ---- 启停控制 ----
        self._stop_event = stop_event if stop_event is not None else STOP_EVENT
        self._pause_event = pause_event
        self._status_callback = status_callback

        # Logger
        self.log = setup_bot_logger(name)
        self._log_buf: list[str] = []  # 供前端读取的日志缓冲
        # 挂载缓冲 handler
        buf_handler = logging.StreamHandler(_LogBufferStream(self._log_buf))
        buf_handler.setLevel(logging.INFO)
        buf_handler.setFormatter(
            logging.Formatter("%(asctime)s %(message)s", datefmt="%H:%M:%S")
        )
        self.log.addHandler(buf_handler)
        self.log.info(f"Bot [{name}] 就绪")
        self.err = 0
        self._retry_rounds = 0  # 连续失败重试轮次
        self.total_time = 0.0
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
        self._begin_cooldown_until = 0.0      # begin 点击冷却截止时间（防止一轮多次点 begin）
        self._end_cooldown_until = 0.0        # end 点击冷却截止时间（防连点）

        # ---- 快速扫描模式（end 点击后开启，缩短 miss sleep 以快速捕获下一个 end）----
        # ---- 动态战斗时长预估 ----
        self.estimated_battle_time: float = 0.0  # 简单平均预估战斗时长（秒），初始 0
        self.battle_total_duration: float = 0.0  # 所有战斗总时长（累加）
        self.battle_count: int = 0               # 已完成战斗次数
        self.battle_start_time: float = 0.0      # 本次战斗开始时间戳

        # ---- 回合耗时统计（两次 begin 之间的时间）----
        self._last_begin_time: float = 0.0       # 上一次 begin 的时间戳
        self.round_total_duration: float = 0.0   # 已完成回合总耗时（累加）
        self.round_count: int = 0                # 已完成回合数

        # 模板管理
        self.template_manager = TemplateManager(task_dir, threshold)
        self.template_manager.load()

        # 鼠标
        self.mouse = MouseController(
            speed_min=mouse_speed_min, speed_max=mouse_speed_max,
            stop_event=self._stop_event,
        )

        # 窗口基准宽度（1080p 标准）
        self.baseline_width = 1920

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
    def capture_window(self, force_activate=True):
        """
        截取目标窗口（使用 win32gui 直接枚举，与前端 get_windows 一致）。
        :return: (PIL.Image, left, top, width, height) 或 None (窗口未找到)
        """
        try:
            import win32gui, win32con
            from ctypes import windll

            title_lower = (self.window_title or "").lower().strip()
            if not title_lower:
                self.log.warning("未设置目标窗口标题")
                return None

            target_hwnd = None
            all_titles = []

            def _enum_cb(hwnd, _extra):
                nonlocal target_hwnd
                if not win32gui.IsWindowVisible(hwnd):
                    return
                wt = win32gui.GetWindowText(hwnd)
                if not wt or not wt.strip():
                    return
                all_titles.append(wt.strip())
                if title_lower in wt.strip().lower():
                    target_hwnd = hwnd

            win32gui.EnumWindows(_enum_cb, None)

            if not target_hwnd:
                self.log.warning(
                    f"未找到包含 '{self.window_title}' 的窗口。"
                    f"当前可见窗口: {all_titles[:8]}")
                return None

            # ---- 激活 & 还原 ----
            if win32gui.IsIconic(target_hwnd):       # 最小化 → 还原
                win32gui.ShowWindow(target_hwnd, win32con.SW_RESTORE)
            if force_activate:
                # AttachThreadInput 绕过 Windows 前台锁定限制
                fore_hwnd = windll.user32.GetForegroundWindow()
                fore_tid = windll.user32.GetWindowThreadProcessId(fore_hwnd, None)
                cur_tid = windll.kernel32.GetCurrentThreadId()
                tgt_tid = windll.user32.GetWindowThreadProcessId(target_hwnd, None)

                if fore_tid != cur_tid:
                    windll.user32.AttachThreadInput(fore_tid, cur_tid, True)
                    windll.user32.AttachThreadInput(cur_tid, tgt_tid, True)
                windll.user32.SetForegroundWindow(target_hwnd)
                windll.user32.BringWindowToTop(target_hwnd)
                if fore_tid != cur_tid:
                    windll.user32.AttachThreadInput(cur_tid, tgt_tid, False)
                    windll.user32.AttachThreadInput(fore_tid, cur_tid, False)
                time.sleep(0.15)

            # ---- 获取位置尺寸 ----
            rect = win32gui.GetWindowRect(target_hwnd)
            left, top, right, bottom = rect
            width = right - left
            height = bottom - top

            if width <= 0 or height <= 0:
                self.log.warning(f"窗口尺寸异常: {width}x{height}")
                return None

            x1, x2 = self.screen[0], self.screen[1]
            y1, y2 = self.screen[2], self.screen[3]

            clip_left = max(left, x1)
            clip_top = max(top, y1)
            clip_right = min(left + width, x2)
            clip_bottom = min(top + height, y2)

            if clip_right <= clip_left or clip_bottom <= clip_top:
                self.log.warning("窗口完全在屏幕外")
                return None

            screenshot = pyautogui.screenshot(
                region=(clip_left, clip_top,
                        clip_right - clip_left, clip_bottom - clip_top)
            )
            cw = clip_right - clip_left
            ch = clip_bottom - clip_top
            self._window_rect = [clip_left, clip_top, cw, ch]
            return screenshot, clip_left, clip_top, cw, ch

        except Exception as e:
            self.log.warning(f"截图异常: {type(e).__name__}: {e}")
            return None

    # ----------------------------------------------------------
    #  处理匹配结果 -> 点击
    # ----------------------------------------------------------
    # ----------------------------------------------------------
    #  匹配结果处理（消抖 + 点击 + 状态切换）
    # ----------------------------------------------------------
    def _handle_detection(self, match_result, window_left, window_top):
        """消抖：要求同一目标连续匹配 N 次才触发点击"""
        class_name, (x, y, w, h), tname, score = match_result
        cx = x + w // 2
        cy = y + h // 2

        # 是否与上一次匹配为同一目标（类别相同 + 位置接近）
        if (self._last_matched_class == class_name
                and abs(cx - self._last_matched_x) < 25
                and abs(cy - self._last_matched_y) < 25):
            self._match_streak += 1
        else:
            self._match_streak = 1

        self._last_matched_class = class_name
        self._last_matched_x = cx
        self._last_matched_y = cy

        # end 按钮一帧确认即可（结算界面多个 end 按钮轮番出现，无需消抖）
        needed = 1 if class_name == "end" else self.match_confirm_count
        if self._match_streak >= needed:
            self._match_streak = 0
            self.err = 0
            self._retry_rounds = 0
            self._do_click(match_result, window_left, window_top)
        else:
            pass  # 攒确认次数中，不操作

    def _do_click(self, match_result, window_left, window_top):
        """执行点击并更新阶段追踪数据"""
        class_name, (x, y, w, h), tname, score = match_result

        now = time.time()

        # ---- 冷却检查 ----
        if class_name == "begin" and now < self._begin_cooldown_until:
            self._match_streak = 0
            return
        if class_name == "end" and now < self._end_cooldown_until:
            self._match_streak = 0
            return

        # ---- 随机选点 ----
        if class_name == "begin":
            # 以模板中心为圆心、半径为 w/2 的圆内随机选点
            cx = window_left + x + w // 2
            cy = window_top + y + h // 2
            radius = w / 2.0
            angle = random.uniform(0, 2 * math.pi)
            r = random.uniform(0, radius)
            click_x = int(cx + r * math.cos(angle))
            click_y = int(cy + r * math.sin(angle))
        else:
            # end: 矩形内随机，离边缘 20px
            margin = 20
            rx1 = max(window_left + x + margin, 0)
            ry1 = max(window_top + y + margin, 0)
            rx2 = max(window_left + x + w - margin, rx1 + 1)
            ry2 = max(window_top + y + h - margin, ry1 + 1)
            click_x = random.randint(rx1, rx2)
            click_y = random.randint(ry1, ry2)

        self.click_positions.append((class_name, click_x, click_y))

        # 投喂 MouseWorker（格式简化：直接传计算好的坐标）
        MouseWorker.input_queue.put(
            (class_name, click_x, click_y)
        )

        self.log.info(f"🖱 点击 {class_name}[{tname}] @ ({click_x}, {click_y}) 匹配度={score:.2f}")

        elapsed = now - self.start_time if self.start_time else 0

        if class_name == "begin":
            # 每轮只点一次 begin
            self.click_count += 1
            self.click_times.append(elapsed)
            self.class_counts[class_name] += 1

            # ---- 回合耗时统计（两次 begin 之间的时间）----
            round_now = time.time()
            if self._last_begin_time > 0:
                round_dur = round_now - self._last_begin_time
                self.round_total_duration += round_dur
                self.round_count += 1
            self._last_begin_time = round_now

            if self.click_count > 0:
                # 平均：基于已完成回合的 begin→begin 时间
                avg = (self.round_total_duration / self.round_count
                       if self.round_count > 0
                       else elapsed / self.click_count)
                remaining = (self.limit - self.click_count
                             if self.limit > 0 else float("inf"))
                if remaining != float("inf"):
                    eta_str = (f"{int(remaining * avg // 60)}m"
                               f"{int(remaining * avg % 60):02d}s")
                else:
                    eta_str = "---"

                self.log.info(
                    f"⚡ BEGIN #{self.click_count} | "
                    f"⏱ {int(elapsed // 60):d}m{int(elapsed % 60):02d}s | "
                    f"均 {avg:.1f}s/次 | "
                    f"剩余 {remaining if remaining != float('inf') else '∞'} 次 ≈ {eta_str} | "
                    f"匹配度 {score:.2f}"
                )

            # 进入战斗阶段
            self._in_battle = True
            self._battle_ended = False
            self.battle_start_time = time.time()
            half_time = self.estimated_battle_time / 2.0 if self.estimated_battle_time > 0 else 3.0
            self._begin_cooldown_until = time.time() + half_time  # 前 1/2 战斗时长不扫
            self._interruptible_sleep(0.1)

        elif class_name == "end":
            self.class_counts[class_name] += 1

            # 只记录本轮的第一次 end 点击作为战斗结束时间
            if self._in_battle and self.battle_start_time > 0 and not self._battle_ended:
                self._battle_ended = True
                actual_duration = time.time() - self.battle_start_time
                self.battle_total_duration += actual_duration
                self.battle_count += 1
                self.estimated_battle_time = (
                    self.battle_total_duration / self.battle_count)
                self.log.info(
                    f"  战斗时长: {actual_duration:.1f}s → "
                    f"平均: {self.estimated_battle_time:.1f}s "
                    f"(共 {self.battle_count} 次)"
                )

            # 结束阶段允许多次点击（结算/MVP等），短暂冷却防连点
            self._in_battle = False
            self._end_cooldown_until = time.time() + 1.8

    # ----------------------------------------------------------
    #  可中断睡眠
    # ----------------------------------------------------------
    def _interruptible_sleep(self, seconds: float):
        """切片化睡眠，每 0.05s 检查一次停止/暂停标志"""
        n = int(seconds * 20)
        for _ in range(n):
            if self._stop_event.is_set() or self.stop_requested:
                return
            if self._pause_event is not None and self._pause_event.is_set():
                if self._pause_begin == 0.0:
                    self._pause_begin = time.time()
                time.sleep(0.05)
                continue
            # 从暂停中恢复，累计暂停时长
            if self._pause_begin > 0:
                self._total_paused += time.time() - self._pause_begin
                self._pause_begin = 0.0
            time.sleep(0.05)

    # ----------------------------------------------------------
    #  休息机制
    # ----------------------------------------------------------
    def _calc_next_rest(self) -> int:
        """计算下一次休息的回合目标（每次随机偏移）"""
        if self.rest_rounds <= 0:
            return 999999999
        offset = random.randint(-self.rest_rounds_var, self.rest_rounds_var)
        return max(1, self.click_count + self.rest_rounds + offset)

    def _do_rest(self):
        """执行一次随机时长的休息"""
        duration = self.rest_seconds + random.randint(
            -self.rest_seconds_var, self.rest_seconds_var
        )
        duration = max(1, duration)
        self._rest_count += 1
        self.log.info(
            f"☕ 休息 #{self._rest_count} | "
            f"暂停 {duration}s，已打 {self.click_count} 把 ..."
        )
        self._interruptible_sleep(duration)
        self._next_rest_at = self._calc_next_rest()

    # ----------------------------------------------------------
    #  保存调试截图
    # ----------------------------------------------------------
    # ----------------------------------------------------------
    #  单步扫描（永远同时扫 begin + end，冷却避免连点）
    # ----------------------------------------------------------
    def run_step(self):
        """统一扫描：每帧同时检测 begin 和 end，匹配后消抖点击"""

        # end 点击后冷却期内完全跳过扫描和识别
        now = time.time()
        if now < self._end_cooldown_until:
            remaining = self._end_cooldown_until - now
            self._interruptible_sleep(max(remaining, 0.02))
            return

        try:
            capture_result = self.capture_window(
                force_activate=(self.err % 5 == 0))
            if capture_result is None:
                self.err += 20
                self.log.warning("窗口捕获失败，连续错误 +20")
                self._interruptible_sleep(0.5)
                return

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

            # 同时扫 begin + end，各取最高分
            begin_result, begin_score = self.template_manager.find_best_match(
                detect_img, "begin", base_scale=base_scale)
            end_result, end_score = self.template_manager.find_best_match(
                detect_img, "end", base_scale=base_scale)

            # ---- 识别结果日志 ----
            if begin_result:
                cls_name, (bx, by, bw, bh), tname, bscore = begin_result
                self.log.info(f"✅ 匹配 {cls_name}[{tname}] {bscore:.2f} @({bx+bw//2},{by+bh//2})")
            elif end_result:
                cls_name, (ex, ey, ew, eh), tname, escore = end_result
                self.log.info(f"✅ 匹配 {cls_name}[{tname}] {escore:.2f} @({ex+ew//2},{ey+eh//2})")
            else:
                self.log.info("❌ 未匹配")

            # begin 优先用于点击
            match_result = begin_result if begin_result else end_result

            # 坐标还原（detection_scale 缩放后的坐标映射回原始尺寸）
            if match_result:
                cls_name, (x, y, mw, mh), tname, score = match_result
                if 0 < self.detection_scale < 1.0:
                    s = 1.0 / self.detection_scale
                    match_result = (
                        cls_name,
                        (int(x * s), int(y * s),
                         int(mw * s), int(mh * s)),
                        tname, score)

            if match_result:
                self._handle_detection(match_result, left, top)
            else:
                self._match_streak = 0
                self.err += 1
                if self.err >= self.miss_threshold:
                    self.err = 0
                    self._retry_rounds += 1
                    if self._retry_rounds >= 3:
                        self.log.warning(
                            f"连续 3 轮每轮 {self.miss_threshold} 次未匹配到目标，停止运行\n"
                            f"  可能原因: 模板过时、窗口遮挡、游戏界面变化")
                        self.stop_requested = True
                        return
                    else:
                        self.log.warning(
                            f"连续 {self.miss_threshold} 次未匹配，延时重试 "
                            f"({self._retry_rounds}/3)")
                        self._interruptible_sleep(self.miss_retry_sleep)
                        return  # 跳过动态 sleep

                # 动态扫描间隔
                now = time.time()
                if now < self._begin_cooldown_until:
                    # begin 点击后 3s 不扫描，休眠到冷却结束
                    remaining = self._begin_cooldown_until - now
                    self._interruptible_sleep(max(remaining, 0.1))
                elif self._in_battle:
                    est = (self.estimated_battle_time
                           if self.estimated_battle_time > 0 else 4.0)
                    elapsed = time.time() - self.battle_start_time
                    remaining = max(0.0, est - elapsed)
                    if remaining <= est / 4.0:
                        self._interruptible_sleep(0.25)               # 最后1/4高频
                    else:
                        self._interruptible_sleep(1.0)              # 战斗中期低频
                else:
                    self._interruptible_sleep(0.3)              # 常速

        except Exception as e:
            self.log.error(
                f"run_step 异常: {type(e).__name__}: {e}", exc_info=True)
            self.err += 5

    # ----------------------------------------------------------
    #  主循环
    # ----------------------------------------------------------
    def run(self):
        """Bot 主线程入口：统一扫描 begin + end 的点击循环"""
        self.log.info("开始运行 ...")
        self.start_time = time.time()

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
                self._total_paused += time.time() - self._pause_begin
                self._pause_begin = 0.0

            if 0 < self.limit <= self.click_count:
                self.log.info(f"已达到次数上限 ({self.limit})，正常结束")
                self.stop_requested = True
                break

            self.run_step()

            # 休息检查：达到目标回合数后自动休息
            if self.rest_rounds > 0 and self.click_count >= self._next_rest_at:
                self._do_rest()

            # 防卡死：连续大错误量直接退出
            if self.err >= 30:
                self.log.warning("卡死风险 (err≥30)，线程退出")
                break

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
        }

    def reset(self):
        """重置统计，准备下一轮运行"""
        self.round_count = 0
        self.click_count = 0
        self.class_counts = {}
        self.click_times.clear()
        self.round_total_duration = 0.0
        self._last_begin_time = None
        self.start_time = None
        self.stop_requested = False
        self._pause_begin = 0.0
        self._total_paused = 0.0
        self._match_streak = 0
        self._last_matched_class = None
        self._end_cooldown_until = 0.0
        self._begin_cooldown_until = 0.0
        self._miss_count = 0
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
            begin_pts = [(px, py) for cls, px, py in self.click_positions if cls == "begin"]
            end_pts = [(px, py) for cls, px, py in self.click_positions if cls == "end"]

            if begin_pts:
                bx, by = zip(*begin_pts)
                ax3.scatter(bx, by, c="green", marker="o", alpha=0.6,
                            s=30, label=f"begin ({len(begin_pts)})")
            if end_pts:
                ex, ey = zip(*end_pts)
                ax3.scatter(ex, ey, c="blue", marker="x", alpha=0.6,
                            s=40, label=f"end ({len(end_pts)})")

            ax3.set_xlabel("屏幕 X")
            ax3.set_ylabel("屏幕 Y")
            ax3.set_title("点击位置分布")
            ax3.invert_yaxis()  # 屏幕坐标系 Y 轴朝下
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

    screens = config["screens"]
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
        screen_name = cfg.get("screen", "main")
        if screen_name not in screens:
            print(f"[错误] Bot '{cfg.get('name')}' 指定的屏幕 '{screen_name}' 未在配置中定义")
            sys.exit(1)
        screen_rect = screens[screen_name]
        screen_tuple = (screen_rect["xmin"], screen_rect["xmax"],
                        screen_rect["ymin"], screen_rect["ymax"])

        bot = AutoClickerBot(
            name=cfg["name"],
            window_title=cfg["window_title"],
            task_dir=cfg["task_dir"],
            limit=cfg.get("limit", 200),
            screen=screen_tuple,
            threshold=cfg.get("threshold", 0.75),
            mouse_speed_min=global_cfg.get("mouse_speed_min", 2000),
            mouse_speed_max=global_cfg.get("mouse_speed_max", 2500),
            # ---- 循环优化参数 ----
            match_confirm_count=global_cfg.get("match_confirm_count", 2),
            detection_scale=global_cfg.get("detection_scale", 0.5),
            miss_threshold=global_cfg.get("miss_threshold", 20),
            miss_retry_sleep=global_cfg.get("miss_retry_sleep", 2.0),
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
        speed_min=global_cfg.get("mouse_speed_min", 2000),
        speed_max=global_cfg.get("mouse_speed_max", 2500),
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
