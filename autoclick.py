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
import shutil
import threading
import time
import sys
from collections import defaultdict
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

class MouseController:
    def __init__(self, speed_min=2750, speed_max=3250, stop_event: Optional[threading.Event] = None):
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
        saved_pause = pyautogui.PAUSE
        pyautogui.PAUSE = 0
        n_points = len(path)

        # 每步权重：progress=0 → 0.3x（快）, progress=1 → 1.0x（最低基准慢）
        raw_weights = [0.3 + 0.7 * (i / (n_points - 1)) ** 1.5
                       for i in range(n_points)]
        sum_w = sum(raw_weights)

        for i, (px, py) in enumerate(path):
            if self._stop_event.is_set():
                pyautogui.PAUSE = saved_pause
                return
            jitter_x = random.gauss(0, 0.15)
            jitter_y = random.gauss(0, 0.15)
            step_time = duration * (raw_weights[i] / sum_w) * random.uniform(0.90, 1.10)
            pyautogui.moveTo(int(px + jitter_x), int(py + jitter_y), duration=0)
            time.sleep(step_time)

        pyautogui.PAUSE = saved_pause
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

# 首次进入某阶段、全扫学习可点击区域前先等待的时间（秒），让场景过渡动画走完。
# 否则会把动画中间帧的匹配结果一并学进区域，导致可点击范围不准。
# 两个阶段分开取值：进入战斗的过渡较短，战斗结算的演出更长。
REGION_LEARN_SETTLE_SEC = {"begin": 0.5, "end": 2.0}

# 是否在点击前复核阶段标志（仅针对「连续同阶段」的点击）。
# 坐标是识别阶段时算好的，若这期间界面已开始切换，点击会落到错误的区域。
# 首次进入某阶段时标志刚确认过，不做重复复核，避免白白多一次截图。
STAGE_RECHECK_ENABLED = True


def is_usable_image(img: np.ndarray) -> bool:
    """
    判断图片是否是可用的真实素材。
    新建场景时自动生成的占位图尺寸极小，用它来区分「未设置」。
    """
    h, w = img.shape[:2]
    return h >= FLAG_PLACEHOLDER_MAX_PX and w >= FLAG_PLACEHOLDER_MAX_PX


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
        self.templates: dict[str, list[tuple[str, np.ndarray]]] = defaultdict(list)
        self.class_loaded: dict[str, bool] = {}
        self.baseline_w = None   # baseline.png 基准宽度
        self.baseline_h = None
        # 阶段旗帜：{"begin": (文件名, 图像), "end": (文件名, 图像)}
        self.flags: dict[str, tuple[str, np.ndarray]] = {}

    def load(self):
        """加载全部模板：begin / end + baseline.png 基准参照 + 阶段旗帜"""
        # 加载 baseline（基准窗口截图，用作尺寸参照）
        baseline_path = self.task_dir / "baseline.png"
        if baseline_path.exists():
            baseline_img = cv2.imread(str(baseline_path), cv2.IMREAD_COLOR)
            if baseline_img is not None and is_usable_image(baseline_img):
                self.baseline_h, self.baseline_w = baseline_img.shape[:2]
                print(f"[baseline] 基准窗口 {self.baseline_w}x{self.baseline_h}")

        for class_name in ("begin", "end"):
            self._load_class(class_name)
            self._load_flag(class_name)
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

    def _load_flag(self, class_name: str):
        """
        加载阶段旗帜：<场景>/<class_name>.png（与 baseline.png 同级）。
        占位图（尺寸极小）视为未设置。
        """
        flag_path = self.task_dir / f"{class_name}.png"
        if not flag_path.exists():
            return
        img = cv2.imread(str(flag_path), cv2.IMREAD_COLOR)
        if img is None or not is_usable_image(img):
            return
        self.flags[class_name] = (flag_path.name, img)

    @property
    def has_flags(self) -> bool:
        """begin / end 两张阶段旗帜是否都已设置（缺一不可，否则无法区分阶段）"""
        return "begin" in self.flags and "end" in self.flags

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

        if self.has_flags:
            report.append("  ✔ 阶段旗帜已设置（begin.png / end.png），启用旗帜快速识别")
        elif self.flags:
            missing = [c for c in ("begin", "end") if c not in self.flags]
            report.append(f"  ⚠ 阶段旗帜不完整，缺少 {' / '.join(missing)}.png，退回全图扫描模式")
        else:
            report.append("  · 未设置阶段旗帜，使用全图扫描模式")

        report.insert(0, f"共加载 {total} 个模板:")
        return report

    @property
    def is_empty(self) -> bool:
        return all(len(v) == 0 for v in self.templates.values())

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

    def find_all_matches(self, screenshot_cv: np.ndarray, class_name: str,
                         base_scale: float = 1.0) -> list:
        """
        扫描该类别全部模板，返回【所有】高于阈值的匹配（不做随机挑选）。
        用于学习某阶段的可点击区域。
        :return: [(class_name, (x, y, w, h), filename, score), ...]
        """
        if not self.class_loaded.get(class_name, False):
            return []

        results = []
        for template_name, template in self.templates[class_name]:
            hit = self._match_one(screenshot_cv, template, base_scale)
            if hit is not None:
                pos, score = hit
                results.append((class_name, pos, template_name, score))
        return results

    def match_flag(self, screenshot_cv: np.ndarray, class_name: str,
                   base_scale: float = 1.0):
        """
        匹配阶段旗帜（每阶段只有一张，开销恒定，与模板总数无关）。
        :return: ((class_name, (x, y, w, h), filename, score), score) 或 (None, 0)
        """
        entry = self.flags.get(class_name)
        if entry is None:
            return None, 0

        file_name, template = entry
        hit = self._match_one(screenshot_cv, template, base_scale)
        if hit is None:
            return None, 0

        pos, score = hit
        return (class_name, pos, file_name, score), score

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

        candidates = self.find_all_matches(screenshot_cv, class_name, base_scale)
        if not candidates:
            return None, 0

        chosen = random.choice(candidates)
        return chosen, chosen[3]


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
        threshold: float = 0.75,
        mouse_speed_min: int = 2750,
        mouse_speed_max: int = 3250,
        # ---- 循环优化参数 ----
        match_confirm_count: int = 2,
        detection_scale: float = 0.5,
        # ---- 未匹配重试 ----
        miss_threshold: int = 20,
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
        status_callback: Optional[callable] = None,
    ):
        self.name = name
        self.window_title = window_title
        self.limit = limit
        self.threshold = threshold
        # ---- 循环优化参数 ----
        self.match_confirm_count = match_confirm_count
        self.detection_scale = detection_scale
        # ---- 未匹配重试 ----
        self.miss_threshold = miss_threshold
        self.miss_retry_sleep = miss_retry_sleep
        # ---- 同阶段反复点击保护 ----
        self.same_stage_click_limit = same_stage_click_limit

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
        self.error_score = 0
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

        # ---- 阶段识别与可点击区域（旗帜模式）----
        # 首次进入某阶段时全扫学习一次，之后直接从区域中采样点击
        # 每项形如 {"cells": [(x, y, w, h), ...], "total_area": int}
        self._regions: dict[str, dict | None] = {"begin": None, "end": None}
        self._region_outline: dict[str, list] = {"begin": [], "end": []}  # 外轮廓（只画边框）
        self._region_settled: set[str] = set()         # 已等过过渡动画的阶段
        self._stage: str | None = None                 # 当前识别到的阶段
        self._last_clicked_stage: str | None = None    # 上一次点击的阶段
        self._same_stage_clicks = 0                    # 连续点击同一阶段的次数

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

            # 裁剪到实际屏幕范围：窗口可能有一部分在屏幕外，
            # 而 pyautogui 截取越界区域时会得到黑边甚至直接报错
            scr_w, scr_h = pyautogui.size()
            clip_left = max(left, 0)
            clip_top = max(top, 0)
            clip_right = min(left + width, scr_w)
            clip_bottom = min(top + height, scr_h)

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
    def _confirm_match(self, match_result, window_left, window_top):
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
            self.error_score = 0
            self._retry_rounds = 0
            # 连续同阶段才复核标志：首次进入该阶段时标志刚确认过，不必重复查
            if (STAGE_RECHECK_ENABLED and self._same_stage_clicks >= 1
                    and not self._recheck_stage(class_name)):
                self.log.info(
                    f"⏭ {class_name} 阶段标志复核未通过（界面已开始切换），丢弃本次点击")
                return
            self._click_target(match_result, window_left, window_top)
        else:
            pass  # 攒确认次数中，不操作

    def _click_target(self, match_result, window_left, window_top):
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

        # ---- 计算点击坐标 ----
        if w <= 0 or h <= 0:
            # w/h 为 0 表示坐标在上游已经选好（旗帜模式的面积加权采样点，不再二次随机）
            click_x = window_left + x
            click_y = window_top + y
        elif class_name == "begin":
            # 以模板中心为圆心、「中心到最近边的距离」为半径的圆内随机选点
            cx = window_left + x + w // 2
            cy = window_top + y + h // 2
            radius = min(w, h) / 2.0
            angle = random.uniform(0, 2 * math.pi)
            r = math.sqrt(random.random()) * radius
            click_x = int(cx + r * math.cos(angle))
            click_y = int(cy + r * math.sin(angle))
        else:
            # end 全扫模式：矩形内随机，离边缘 20px
            margin = 20
            rx1 = max(window_left + x + margin, 0)
            ry1 = max(window_top + y + margin, 0)
            rx2 = max(window_left + x + w - margin, rx1 + 1)
            ry2 = max(window_top + y + h - margin, ry1 + 1)
            click_x = random.randint(rx1, rx2)
            click_y = random.randint(ry1, ry2)

        # 记录窗口内相对坐标（与窗口在屏幕上的位置无关）：
        # 这样运行中移动窗口后，前端点击分布图不会整体错位。
        self.click_positions.append(
            (class_name, click_x - window_left, click_y - window_top)
        )

        # 投喂 MouseWorker（格式简化：直接传计算好的坐标）
        MouseWorker.input_queue.put(
            (class_name, click_x, click_y)
        )

        score_text = f" 匹配度={score:.2f}" if score >= 0 else ""
        self.log.info(f"🖱 点击 {class_name}[{tname}] @ ({click_x}, {click_y}){score_text}")

        # ---- 同阶段反复点击保护 ----
        # 点击落空时阶段不会变化、err 也不会增长，所以单独用
        # 「连续点击同一阶段」的次数来判断界面被遮挡 / 点击失效。
        if class_name == self._last_clicked_stage:
            self._same_stage_clicks += 1
        else:
            self._last_clicked_stage = class_name
            self._same_stage_clicks = 1

        if (self.same_stage_click_limit > 0
                and self._same_stage_clicks >= self.same_stage_click_limit):
            self.log.warning(
                f"连续 {self._same_stage_clicks} 次点击 {class_name} 阶段后界面无变化，"
                f"判定为点击失效或被遮挡，停止运行\n"
                f"  可能原因: 弹窗遮挡按钮、阶段旗帜/模板过期、游戏界面变化")
            self.stop_requested = True
            return

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
    #  识别：全图扫描 / 旗帜判阶段
    # ----------------------------------------------------------
    def _detect_by_templates(self, detect_img, base_scale):
        """
        全图扫描模式（未配置阶段旗帜时使用）：
        同时扫 begin + end 的全部模板，begin 优先。
        """
        begin_result, begin_score = self.template_manager.find_best_match(
            detect_img, "begin", base_scale=base_scale)
        end_result, end_score = self.template_manager.find_best_match(
            detect_img, "end", base_scale=base_scale)

        if begin_result:
            cls_name, (bx, by, bw, bh), tname, bscore = begin_result
            self.log.info(f"✅ 匹配 {cls_name}[{tname}] {bscore:.2f} @({bx+bw//2},{by+bh//2})")
        elif end_result:
            cls_name, (ex, ey, ew, eh), tname, escore = end_result
            self.log.info(f"✅ 匹配 {cls_name}[{tname}] {escore:.2f} @({ex+ew//2},{ey+eh//2})")
        else:
            self.log.info("❌ 未匹配")

        return begin_result if begin_result else end_result

    def _judge_stage(self, detect_img, base_scale) -> tuple[str | None, float]:
        """
        匹配两张阶段旗帜，判定当前处于哪个阶段（两张都命中时取分数高的）。
        :return: (阶段名 或 None, 最高分)
        """
        tm = self.template_manager
        begin_hit, begin_score = tm.match_flag(detect_img, "begin", base_scale)
        end_hit, end_score = tm.match_flag(detect_img, "end", base_scale)

        if begin_hit and end_hit:
            return ("begin" if begin_score >= end_score else "end"), max(begin_score, end_score)
        if begin_hit:
            return "begin", begin_score
        if end_hit:
            return "end", end_score
        return None, 0.0

    def _recheck_stage(self, stage: str) -> bool:
        """
        点击前复核阶段标志是否依然成立（只用于「连续同阶段」的点击）。

        重新截一帧并匹配阶段旗帜，确认当前仍处于 stage 阶段。
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
        return now_stage == stage

    def _detect_by_flags(self, detect_img, base_scale):
        """
        旗帜模式：每帧只扫 begin / end 两张阶段旗帜来判定当前阶段，
        再从该阶段学习到的「可点击区域」里随机取一个作为点击目标。
        扫描开销恒定（2 次匹配），与模板总数无关。
        """
        stage, _ = self._judge_stage(detect_img, base_scale)

        # 阶段发生变化 → 重置「同阶段连续点击」计数
        if stage != self._stage:
            self._stage = stage
            self._same_stage_clicks = 0
            if stage is not None:
                self.log.info(f"🔄 进入 {stage} 阶段")

        if stage is None:
            self.log.info("❌ 未匹配（阶段旗帜未命中）")
            return None

        # 本轮首次进入该阶段 → 全扫一次，学习可点击区域。
        # 先等一小段让过渡动画走完：动画中间帧的匹配结果会被一并学进区域，
        # 导致范围不准。等待结束本轮直接返回，下一帧用稳定后的画面重新学习。
        if not self._regions[stage]:
            if stage not in self._region_settled:
                self._region_settled.add(stage)
                settle = REGION_LEARN_SETTLE_SEC.get(stage, 1.0)
                self.log.info(
                    f"⏳ 首次进入 {stage} 阶段，等待 {settle:.1f}s "
                    f"待画面稳定后学习可点击区域")
                self._interruptible_sleep(settle)
                return None
            self._learn_regions(detect_img, stage, base_scale)

        region = self._regions[stage]
        if not region:
            self.log.warning(f"⚠ {stage} 阶段没有可点击区域，本轮跳过点击")
            return None

        # 采样：begin 在「内切圆」内均匀取点，end 在区域内按面积加权取点
        if stage == "begin":
            point = sample_point_in_cells_circle(region["cells"])
        else:
            point = sample_point_in_cells(region["cells"], region["total_area"])
        if point is None:
            self.log.warning(f"⚠ {stage} 阶段可点击区域为空，本轮跳过点击")
            return None

        self.log.info(
            f"✅ 阶段 {stage} | 采样点 @({point[0]},{point[1]}) "
            f"（候选像素 {region['total_area']}）")
        # w/h 置 0 表示坐标已经选好，_click_target 不再做二次随机
        return (stage, (point[0], point[1], 0, 0), "region", -1)

    def _learn_regions(self, detect_img, stage, base_scale):
        """
        全扫该阶段的所有模板，把命中的框收缩后合并成「可点击区域」。
        每次运行只在首次进入该阶段时执行一次。
        """
        matches = self.template_manager.find_all_matches(
            detect_img, stage, base_scale)
        if not matches:
            self.log.warning(
                f"⚠ {stage} 阶段学习失败：没有模板命中，暂无可点击区域")
            return

        # 收缩量 / 填缝量按 detection_scale 换算到当前识别图坐标系
        scale = max(self.detection_scale, 0.1)
        fill_gap = max(0, round(REGION_FILL_GAP_PX * scale))

        boxes = [pos for _, pos, _, _ in matches]

        # 内缩：begin 用内切圆采样，圆本身不会碰到框边缘，不额外内缩；
        # end 缩掉一圈以避开背景里的相邻 UI，但受绝对上限与「最窄短边比例」约束
        # （jiu_xiao 场景存在短边仅 39px 的模板，缩多了会被整个缩没）。
        thinnest = min(min(w, h) for (_, _, w, h) in boxes)
        if stage == "begin":
            shrink = 0
        else:
            cap = max(1, round(REGION_SHRINK_PX_END * scale))
            shrink = max(1, min(cap, int(thinnest * REGION_SHRINK_RATIO_END)))

        cells, total_area = build_click_cells(
            boxes, shrink=shrink, fill_gap=fill_gap)
        if not cells:
            self.log.warning(
                f"⚠ {stage} 阶段学习失败：向内收缩 {shrink}px 后没有剩余范围，"
                f"模板框可能太小，建议重新截取")
            return

        self._regions[stage] = {"cells": cells, "total_area": total_area}
        # 外轮廓在此算一次即可（供前端只绘制区域边框）
        # begin 按内切圆绘制，与它的圆形采样保持一致
        self._region_outline[stage] = cells_to_outline(
            cells, as_circle=(stage == "begin"))
        self.log.info(
            f"📚 学习 {stage} 阶段：{len(matches)} 个模板命中 → "
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
            remaining = self._end_cooldown_until - now
            self._interruptible_sleep(max(remaining, 0.02))
            return

        try:
            capture_result = self.capture_window(
                force_activate=(self.error_score % 5 == 0))
            if capture_result is None:
                self.error_score += 20
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

            # ---- 识别：配置了阶段旗帜就走「旗帜判阶段 + 区域取点」，否则退回全图扫描 ----
            if self.template_manager.has_flags:
                match_result = self._detect_by_flags(detect_img, base_scale)
            else:
                match_result = self._detect_by_templates(detect_img, base_scale)

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
                self._confirm_match(match_result, left, top)
            else:
                self._match_streak = 0
                self.error_score += 1
                if self.error_score >= self.miss_threshold:
                    self.error_score = 0
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
            if self.error_score >= 30:
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
        # 阶段识别缓存：清空后下次进入阶段会重新学习
        self._regions = {"begin": None, "end": None}
        self._region_outline = {"begin": [], "end": []}
        self._region_settled = set()
        self._stage = None
        self._last_clicked_stage = None
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
            mouse_speed_min=global_cfg.get("mouse_speed_min", 2750),
            mouse_speed_max=global_cfg.get("mouse_speed_max", 3250),
            # ---- 循环优化参数 ----
            match_confirm_count=global_cfg.get("match_confirm_count", 2),
            detection_scale=global_cfg.get("detection_scale", 0.5),
            miss_threshold=global_cfg.get("miss_threshold", 20),
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
        speed_min=global_cfg.get("mouse_speed_min", 2750),
        speed_max=global_cfg.get("mouse_speed_max", 3250),
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
