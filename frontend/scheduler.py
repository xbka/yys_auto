"""
多实例 BOT 管理器
支持同时运行多个 AutoClickerBot 实例（各自独立窗口/场景）
"""
import os
import sys
import queue
import threading
import time
import json
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from autoclick import AutoClickerBot, MouseController, MouseWorker, POISON_PILL

logger = logging.getLogger("yys.system")


class BotInstance:
    """单个 Bot 实例：独立线程 + 独立事件 + 独立配置"""

    def __init__(self, instance_id: str, global_config: dict):
        self.instance_id = instance_id
        self._bot: AutoClickerBot | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._lock = threading.Lock()
        self._state = "idle"  # idle | running | paused | stopping
        self._global_config = global_config  # 全局默认参数
        self._instance_config: dict = {}      # 实例特有参数（窗口/场景）

    # ------------------------------------------------------------------
    #  状态
    # ------------------------------------------------------------------
    @property
    def state(self) -> str:
        return self._state

    def get_status(self) -> dict:
        """返回给前端轮询的状态快照"""
        with self._lock:
            base = {
                "id": self.instance_id,
                "state": self._state,
                "log_lines": [],
                "window_title": self._instance_config.get("window_title", ""),
                "template_scene": self._instance_config.get("template_scene", ""),
                "name": self._instance_config.get("name", self.instance_id),
            }
            # 停止后保留统计数据不清零，下次点击开始才清零
            if self._bot is not None:
                s = self._bot.status
                base.update({
                    "round": s["round"],
                    "total_clicks": s["total_clicks"],
                    "elapsed": s["elapsed"],
                    "avg_duration": round(s["avg_duration"], 1),
                    "avg_battle_time": round(s["avg_battle_time"], 1),
                    "remaining": s["remaining"],
                    "eta": s["eta"],
                    "click_positions": s["click_positions"],
                    "window_rect": s.get("window_rect"),
                })
                base["log_lines"] = list(self._bot._log_buf)
            return base

    # ------------------------------------------------------------------
    #  控制命令
    # ------------------------------------------------------------------
    def start(self, instance_cfg: dict):
        """创建 Bot 并启动线程"""
        with self._lock:
            if self._state == "running":
                return {"ok": False, "msg": f"[{self.instance_id}] 已在运行中"}

            self._stop_event.clear()
            self._pause_event.clear()
            self._instance_config = instance_cfg

            # 合并全局默认 + 实例配置
            g = self._global_config
            screen = instance_cfg.get("screen") or g.get("screen", {})
            screen_tuple = (
                screen.get("xmin", 0), screen.get("xmax", 1920),
                screen.get("ymin", 0), screen.get("ymax", 1080),
            )

            template_scene = instance_cfg.get("template_scene", "")
            task_dir = ""
            if template_scene:
                task_dir = os.path.join("templates", template_scene)

            bot_name = instance_cfg.get("name", self.instance_id)

            self._bot = AutoClickerBot(
                name=bot_name,
                window_title=instance_cfg.get("window_title", g.get("window_title", "")),
                task_dir=task_dir,
                limit=int(instance_cfg.get("limit", g.get("limit", 200))),
                screen=screen_tuple,
                threshold=instance_cfg.get("threshold", g.get("threshold", 0.75)),
                mouse_speed_min=instance_cfg.get("mouse_speed_min", g.get("mouse_speed_min", 2000)),
                mouse_speed_max=instance_cfg.get("mouse_speed_max", g.get("mouse_speed_max", 2500)),
                match_confirm_count=instance_cfg.get("match_confirm_count", g.get("match_confirm_count", 2)),
                detection_scale=instance_cfg.get("detection_scale", g.get("detection_scale", 0.5)),
                miss_threshold=instance_cfg.get("miss_threshold", g.get("miss_threshold", 20)),
                miss_retry_sleep=instance_cfg.get("miss_retry_sleep", g.get("miss_retry_sleep", 2.0)),
                rest_rounds=g.get("rest_rounds", 50),
                rest_rounds_var=g.get("rest_rounds_var", 10),
                rest_seconds=g.get("rest_seconds", 30),
                rest_seconds_var=g.get("rest_seconds_var", 5),
                stop_event=self._stop_event,
                pause_event=self._pause_event,
            )

            self._thread = threading.Thread(
                target=self._run_wrapper,
                name=f"Bot-{bot_name}",
                daemon=True,
            )
            self._state = "running"
            self._thread.start()
            logger.info(f"[{bot_name}] 实例已启动")
            return {"ok": True, "msg": f"[{bot_name}] 已启动"}

    def stop(self):
        """停止 Bot"""
        with self._lock:
            if self._state not in ("running", "paused"):
                return {"ok": False, "msg": f"[{self.instance_id}] 未在运行"}
            self._state = "stopping"
            self._stop_event.set()
            self._pause_event.clear()
            if self._bot:
                self._bot.stop_requested = True  # 防止 stop_event 清除后 status 误判为 running
            return {"ok": True, "msg": f"[{self.instance_id}] 正在停止..."}

    def pause(self):
        """暂停"""
        with self._lock:
            if self._state != "running":
                return {"ok": False, "msg": f"[{self.instance_id}] 未在运行"}
            self._pause_event.set()
            self._state = "paused"
            return {"ok": True, "msg": f"[{self.instance_id}] 已暂停"}

    def resume(self):
        """恢复"""
        with self._lock:
            if self._state != "paused":
                return {"ok": False, "msg": f"[{self.instance_id}] 未在暂停"}
            self._pause_event.clear()
            self._state = "running"
            return {"ok": True, "msg": f"[{self.instance_id}] 已恢复"}

    # ------------------------------------------------------------------
    #  内部
    # ------------------------------------------------------------------
    def _run_wrapper(self):
        """在线程中运行 Bot"""
        try:
            self._bot.run()
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            if self._bot is not None and hasattr(self._bot, '_log_buf'):
                self._bot._log_buf.append(tb)
            logger.error(f"[{self.instance_id}] Bot 异常退出: {e}")
        finally:
            with self._lock:
                self._state = "idle"
                self._stop_event.clear()
                self._pause_event.clear()

    def join(self, timeout: float = 3.0):
        """等待线程结束"""
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)


class BotManager:
    """多实例 Bot 调度器"""

    MAX_INSTANCES = 4

    def __init__(self):
        self._instances: dict[str, BotInstance] = {}
        self._global_config: dict = {}
        self._lock = threading.Lock()
        self._mouse_worker_init()

    # ------------------------------------------------------------------
    #  实例管理
    # ------------------------------------------------------------------
    def set_config(self, config: dict):
        """设置全局 + 实例配置"""
        with self._lock:
            self._global_config = {k: v for k, v in config.items()
                                   if k not in ("instances", "_available_scenes")}
            instance_configs = config.get("instances", [])

            # 创建 / 更新实例
            existing_ids = set(self._instances.keys())
            new_ids = set()

            for ic in instance_configs:
                iid = ic.get("id", "")
                if not iid:
                    continue
                new_ids.add(iid)
                if iid not in self._instances:
                    self._instances[iid] = BotInstance(iid, self._global_config)
                # 更新实例配置
                self._instances[iid]._instance_config = ic
                self._instances[iid]._global_config = self._global_config

            # 清理已移除的实例
            for iid in existing_ids - new_ids:
                inst = self._instances.pop(iid)
                inst.stop()
                inst.join()

            # 如果没有任何实例配置，确保至少有一个默认的
            if not self._instances:
                default_id = "window-1"
                self._instances[default_id] = BotInstance(default_id, self._global_config)

    def get_instance_ids(self) -> list:
        """返回所有实例 ID"""
        return sorted(self._instances.keys())

    def get_instance(self, instance_id: str) -> BotInstance | None:
        return self._instances.get(instance_id)

    # ------------------------------------------------------------------
    #  控制
    # ------------------------------------------------------------------
    def start(self, instance_id: str, config: dict):
        inst = self._instances.get(instance_id)
        if inst is None:
            return {"ok": False, "msg": f"实例 [{instance_id}] 不存在"}
        return inst.start(config)

    def stop(self, instance_id: str):
        inst = self._instances.get(instance_id)
        if inst is None:
            return {"ok": False, "msg": f"实例 [{instance_id}] 不存在"}
        return inst.stop()

    def stop_all(self):
        """停止所有运行中的实例"""
        results = {}
        for iid, inst in self._instances.items():
            results[iid] = inst.stop()
        return results

    def pause(self, instance_id: str):
        inst = self._instances.get(instance_id)
        if inst is None:
            return {"ok": False, "msg": f"实例 [{instance_id}] 不存在"}
        return inst.pause()

    def resume(self, instance_id: str):
        inst = self._instances.get(instance_id)
        if inst is None:
            return {"ok": False, "msg": f"实例 [{instance_id}] 不存在"}
        return inst.resume()

    # ------------------------------------------------------------------
    #  状态轮询
    # ------------------------------------------------------------------
    def get_all_status(self) -> list:
        """返回所有实例的状态列表"""
        with self._lock:
            return [inst.get_status() for inst in self._instances.values()]

    # ------------------------------------------------------------------
    #  共享鼠标工作线程（所有 Bot 共用一个队列，串行点击）
    # ------------------------------------------------------------------
    def _mouse_worker_init(self):
        """启动全局共享的鼠标点击线程"""
        self._mouse_queue = queue.Queue()
        self._mouse_ctrl = MouseController(stop_event=threading.Event())
        self._mouse_worker = MouseWorker(self._mouse_queue, self._mouse_ctrl,
                                         stop_event=threading.Event())
        MouseWorker.input_queue = self._mouse_queue
        self._mouse_worker.start()

    def _mouse_worker_shutdown(self):
        """停止共享鼠标工作线程"""
        MouseWorker.input_queue.put(POISON_PILL)
        if self._mouse_worker and self._mouse_worker.thread and self._mouse_worker.thread.is_alive():
            self._mouse_worker.thread.join(timeout=3)

    # ------------------------------------------------------------------
    #  生命周期
    # ------------------------------------------------------------------
    def shutdown(self):
        """安全退出所有实例 + 鼠标线程"""
        for iid in list(self._instances.keys()):
            self._instances[iid].stop()
        for iid in list(self._instances.keys()):
            self._instances[iid].join(timeout=5)
        self._mouse_worker_shutdown()
