"""
Eel 前端入口
启动桌面窗口，暴露所有 Python 接口给前端调用
支持多 Bot 实例并行运行
"""
import os
import sys
import json
import shutil
import time
import base64
import io
from pathlib import Path

import eel

# 项目根目录（本文件所在目录），加入 path 以便导入 frontend / autoclick
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

# PyInstaller 打包后的路径处理
if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)   # exe 所在目录（可写文件放这里）
    BUNDLE_DIR = sys._MEIPASS                     # 打包资源目录（只读：frontend/web）
else:
    BASE_DIR = PROJECT_ROOT
    BUNDLE_DIR = PROJECT_ROOT

os.chdir(BASE_DIR)

from frontend.scheduler import BotManager

# ============================================================
#  全局状态
# ============================================================
manager = BotManager()
_config: dict = {}
_config_path = os.path.join(BASE_DIR, "config.json")
_templates_root = os.path.join(BASE_DIR, "templates")

os.makedirs(_templates_root, exist_ok=True)


def _load_config():
    """加载配置文件"""
    global _config
    try:
        with open(_config_path, "r", encoding="utf-8") as f:
            _config = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        _config = _default_config()
    # 同步到管理器
    manager.set_config(_config)


def _save_config(cfg: dict):
    """保存配置到文件"""
    global _config
    _config = cfg
    manager.set_config(cfg)
    with open(_config_path, "w", encoding="utf-8") as f:
        json.dump(_config, f, ensure_ascii=False, indent=2)


def _default_config() -> dict:
    return {
        "threshold": 0.75,
        "rest_rounds": 50,
        "rest_rounds_var": 10,
        "rest_seconds": 30,
        "rest_seconds_var": 5,
        "mouse_speed_min": 2000,
        "mouse_speed_max": 2500,
        "match_confirm_count": 2,
        "detection_scale": 0.5,
        "miss_threshold": 20,
        "miss_retry_sleep": 2.0,
        "screen": {"xmin": 0, "xmax": 1920, "ymin": 0, "ymax": 1080},
        "instances": [
            {
                "id": "window-1",
                "name": "窗口1",
                "window_title": "阴阳师-网易游戏",
                "template_scene": "",
                "limit": 200,
            },
        ],
    }


# ============================================================
#  Eel 暴露接口 — 控制
# ============================================================
@eel.expose
def start_bot(instance_id: str):
    """启动指定实例的 Bot"""
    _load_config()
    # 找到该实例的配置
    inst_cfg = _get_instance_config(instance_id)
    if inst_cfg is None:
        return {"ok": False, "msg": f"实例 [{instance_id}] 不存在"}
    return manager.start(instance_id, inst_cfg)


@eel.expose
def stop_bot(instance_id: str):
    """停止指定实例"""
    return manager.stop(instance_id)


@eel.expose
def pause_bot(instance_id: str):
    """暂停指定实例"""
    return manager.pause(instance_id)


@eel.expose
def resume_bot(instance_id: str):
    """恢复指定实例"""
    return manager.resume(instance_id)


@eel.expose
def emergency_stop():
    """紧急停止所有实例"""
    return manager.stop_all()


# ============================================================
#  Eel 暴露接口 — 状态轮询
# ============================================================
@eel.expose
def get_status():
    """返回所有实例的运行状态"""
    return manager.get_all_status()


# ============================================================
#  Eel 暴露接口 — 配置
# ============================================================
@eel.expose
def get_config():
    """获取全部配置"""
    _load_config()
    cfg = dict(_config)
    cfg["_available_scenes"] = list_scenes()
    cfg["_instance_ids"] = list(manager.get_instance_ids())
    return cfg


def _get_instance_config(instance_id: str) -> dict | None:
    """从全局配置中提取单个实例的配置（合并全局默认值）"""
    for ic in _config.get("instances", []):
        if ic.get("id") == instance_id:
            # 合并：全局作为默认，实例配置覆盖
            merged = {
                "threshold": _config.get("threshold", 0.75),
                "mouse_speed_min": _config.get("mouse_speed_min", 2000),
                "mouse_speed_max": _config.get("mouse_speed_max", 2500),
                "match_confirm_count": _config.get("match_confirm_count", 2),
                "detection_scale": _config.get("detection_scale", 0.5),
                "screen": _config.get("screen", {"xmin": 0, "xmax": 1920, "ymin": 0, "ymax": 1080}),
                **ic,
            }
            # 每个 bot 单独设置战斗次数，不从全局读取，强制 int
            if "limit" not in merged:
                merged["limit"] = 200
            else:
                merged["limit"] = int(merged["limit"])
            return merged
    return None


@eel.expose
def save_config(cfg: dict):
    """保存配置"""
    frontend_keys = {"_available_scenes", "_instance_ids"}
    clean = {k: v for k, v in cfg.items() if k not in frontend_keys}
    _save_config(clean)
    return {"ok": True, "msg": "配置已保存"}


@eel.expose
def reset_config():
    """恢复默认配置"""
    _save_config(_default_config())
    return get_config()


# ============================================================
#  Eel 暴露接口 — 实例管理
# ============================================================
@eel.expose
def add_instance():
    """添加一个新实例"""
    _load_config()
    instances = _config.get("instances", [])
    if not instances:
        instances = []
    # 生成新 ID
    existing = {ic["id"] for ic in instances}
    idx = 1
    while f"window-{idx}" in existing:
        idx += 1
    new_inst = {
        "id": f"window-{idx}",
        "name": f"窗口{idx}",
        "window_title": _config.get("instances", [{}])[0].get("window_title", "阴阳师-网易游戏") if _config.get("instances") else "阴阳师-网易游戏",
        "template_scene": "",
        "limit": 200,
    }
    instances.append(new_inst)
    _config["instances"] = instances
    _save_config(_config)
    return {"ok": True, "instance": new_inst}


@eel.expose
def remove_instance(instance_id: str):
    """删除一个实例"""
    _load_config()
    instances = _config.get("instances", [])
    if len(instances) <= 1:
        return {"ok": False, "msg": "至少保留一个实例"}
    # 先停止该实例
    manager.stop(instance_id)
    instances = [ic for ic in instances if ic.get("id") != instance_id]
    _config["instances"] = instances
    _save_config(_config)
    return {"ok": True}


@eel.expose
def save_instance_config(instance_id: str, field: str, value):
    """保存单个实例的某个配置字段（不改全局）"""
    _load_config()
    instances = _config.get("instances", [])
    for ic in instances:
        if ic.get("id") == instance_id:
            ic[field] = value
            break
    _save_config(_config)
    return {"ok": True}


# ============================================================
#  Eel 暴露接口 — 模板管理
# ============================================================
@eel.expose
def list_scenes():
    """列出所有场景目录名"""
    if not os.path.isdir(_templates_root):
        return []
    scenes = []
    for name in os.listdir(_templates_root):
        path = os.path.join(_templates_root, name)
        if os.path.isdir(path) and not name.startswith("."):
            scenes.append(name)
    return sorted(scenes)


@eel.expose
def create_scene(name: str):
    """新建场景（含 begin/end 子目录）"""
    if not name or not name.strip():
        return {"ok": False, "msg": "场景名不能为空"}
    name = name.strip()
    scene_dir = os.path.join(_templates_root, name)
    try:
        os.makedirs(os.path.join(scene_dir, "begin"), exist_ok=True)
        os.makedirs(os.path.join(scene_dir, "end"), exist_ok=True)
        return {"ok": True, "msg": f"场景「{name}」已创建"}
    except OSError as e:
        return {"ok": False, "msg": str(e)}


@eel.expose
def delete_scene(name: str):
    """删除场景（含所有模板）"""
    scene_dir = os.path.join(_templates_root, name)
    if not os.path.isdir(scene_dir):
        return {"ok": False, "msg": "场景不存在"}
    try:
        shutil.rmtree(scene_dir)
        return {"ok": True, "msg": f"场景「{name}」已删除"}
    except OSError as e:
        return {"ok": False, "msg": str(e)}


@eel.expose
def get_templates(scene: str):
    """获取指定场景的模板列表（含缩略图 base64）"""
    scene_dir = os.path.join(_templates_root, scene)
    result = {"begin": [], "end": []}
    if not os.path.isdir(scene_dir):
        return result

    disabled = _load_disabled_templates(scene)

    for ttype in ("begin", "end"):
        tdir = os.path.join(scene_dir, ttype)
        if not os.path.isdir(tdir):
            continue
        for fname in sorted(os.listdir(tdir)):
            if fname.lower().endswith((".png", ".jpg", ".jpeg", ".bmp")):
                fpath = os.path.join(tdir, fname)
                thumb = _image_to_base64_thumb(fpath)
                result[ttype].append({
                    "filename": fname,
                    "thumbnail": thumb,
                    "enabled": fname not in disabled,
                })
    return result


@eel.expose
def toggle_template(scene: str, ttype: str, filename: str, enabled: bool):
    """启用 / 禁用模板"""
    disabled = _load_disabled_templates(scene)
    if not enabled:
        if filename not in disabled:
            disabled.append(filename)
    else:
        if filename in disabled:
            disabled.remove(filename)
    _save_disabled_templates(scene, disabled)
    return {"ok": True}


@eel.expose
def delete_template(scene: str, ttype: str, filename: str):
    """删除模板文件"""
    fpath = os.path.join(_templates_root, scene, ttype, filename)
    if os.path.isfile(fpath):
        os.remove(fpath)
        disabled = _load_disabled_templates(scene)
        if filename in disabled:
            disabled.remove(filename)
            _save_disabled_templates(scene, disabled)
        return {"ok": True, "msg": "模板已删除"}
    return {"ok": False, "msg": "文件不存在"}


@eel.expose
def import_template(scene: str, ttype: str, file_data: dict):
    """前端通过文件选择器传入文件数据"""
    tdir = os.path.join(_templates_root, scene, ttype)
    os.makedirs(tdir, exist_ok=True)
    name = file_data.get("name", "imported.png")
    raw = base64.b64decode(file_data.get("data", ""))
    fpath = os.path.join(tdir, name)
    base, ext = os.path.splitext(name)
    counter = 1
    while os.path.exists(fpath):
        fpath = os.path.join(tdir, f"{base}_{counter}{ext}")
        counter += 1
    with open(fpath, "wb") as f:
        f.write(raw)
    return {"ok": True, "msg": "模板已导入"}


@eel.expose
def capture_template(scene: str, ttype: str, image_data: str,
                     x1: int, y1: int, x2: int, y2: int):
    """保存截取的模板图片"""
    from PIL import Image
    tdir = os.path.join(_templates_root, scene, ttype)
    os.makedirs(tdir, exist_ok=True)

    raw = base64.b64decode(image_data)
    img = Image.open(io.BytesIO(raw))

    left, top = min(x1, x2), min(y1, y2)
    right, bottom = max(x1, x2), max(y1, y2)
    cropped = img.crop((left, top, right, bottom))

    ts = time.strftime("%Y%m%d_%H%M%S")
    fpath = os.path.join(tdir, f"capture_{ts}.png")
    cropped.save(fpath, "PNG")
    return {"ok": True, "msg": "模板已保存", "filename": os.path.basename(fpath)}


@eel.expose
def screenshot_screen():
    """截取当前屏幕，返回 base64"""
    import pyautogui
    from PIL import Image

    img: Image.Image = pyautogui.screenshot()
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    data = base64.b64encode(buf.getvalue()).decode()
    return {"width": img.width, "height": img.height, "data": data}


@eel.expose
def check_baseline(scene: str):
    """检查场景是否有 baseline.png，返回是否存在及其分辨率"""
    path = os.path.join(_templates_root, scene, "baseline.png")
    if os.path.isfile(path):
        from PIL import Image
        try:
            img = Image.open(path)
            w, h = img.size
            return {"exists": True, "width": w, "height": h}
        except Exception:
            return {"exists": True, "width": 0, "height": 0}
    return {"exists": False, "width": 0, "height": 0}


@eel.expose
def save_baseline(scene: str, image_data: str):
    """保存整张截图为 baseline.png"""
    from PIL import Image
    path = os.path.join(_templates_root, scene, "baseline.png")
    raw = base64.b64decode(image_data)
    img = Image.open(io.BytesIO(raw))
    img.save(path, "PNG")
    return {"ok": True, "msg": "基准图已保存", "width": img.width, "height": img.height}


# ============================================================
#  Eel 暴露接口 — 窗口检测
# ============================================================
@eel.expose
def minimize_self():
    """截取模板时最小化前端窗口"""
    import win32gui
    import win32con

    def _cb(hwnd, _extra):
        title = win32gui.GetWindowText(hwnd)
        if "127.0.0.1" in title or "eel" in title.lower():
            win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
        return True

    win32gui.EnumWindows(_cb, None)


@eel.expose
def get_windows():
    """获取当前所有可见窗口标题列表"""
    import win32gui

    windows = []

    def _enum_cb(hwnd, _extra):
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd)
        if title and title.strip() and len(title.strip()) > 1:
            windows.append({"hwnd": hwnd, "title": title.strip()})

    win32gui.EnumWindows(_enum_cb, None)
    windows.sort(key=lambda w: (
        w["title"].encode("utf-8").isascii(),
        w["title"]
    ))
    return windows


# ============================================================
#  辅助函数
# ============================================================
def _disabled_path(scene: str) -> str:
    return os.path.join(_templates_root, scene, ".disabled.json")


def _load_disabled_templates(scene: str) -> list:
    path = _disabled_path(scene)
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError:
            pass
    return []


def _save_disabled_templates(scene: str, disabled: list):
    path = _disabled_path(scene)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(disabled, f, ensure_ascii=False)


def _image_to_base64_thumb(fpath: str, max_size: int = 120) -> str:
    """将图片转为 base64 缩略图字符串"""
    from PIL import Image
    try:
        img = Image.open(fpath)
        img.thumbnail((max_size, max_size), Image.LANCZOS if hasattr(Image, "LANCZOS") else Image.BICUBIC)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return ""


# ============================================================
#  启动入口
# ============================================================
def run():
    """启动 Eel 桌面应用"""
    _load_config()
    web_dir = os.path.join(BUNDLE_DIR, "frontend", "web")
    eel.init(web_dir)

    eel_kwargs = {
        "mode": "default",
        "port": 0,
        "size": (1050, 720),
    }
    try:
        eel.start("index.html", **eel_kwargs)
    except EnvironmentError:
        eel_kwargs["mode"] = "default"
        eel.start("index.html", **eel_kwargs)
    finally:
        manager.shutdown()


if __name__ == "__main__":
    run()
