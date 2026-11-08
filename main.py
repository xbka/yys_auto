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
import re

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

from autoclick import (resolve_miss_timeout, resolve_battle_timeout,
                       setup_root_file_logger, stage_key, parse_stage_stem,
                       is_self_window, find_window_handle, restore_and_activate,
                       grab_window_region,
                       STAGES_DIRNAME, STAGE_TAGS, DEFAULT_STAGES)
from frontend.scheduler import BotManager

# GUI 也要落盘日志：打包成 exe 后没有控制台，出问题只能靠 logs\yys_auto.log 定位
# 只为了副作用（挂上文件 handler），返回值用不到
setup_root_file_logger(os.path.join(BASE_DIR, "logs"))

# ============================================================
#  全局状态
# ============================================================
manager = BotManager()
_config: dict = {}
_config_path = os.path.join(BASE_DIR, "config.json")
_templates_root = os.path.join(BASE_DIR, "templates")

os.makedirs(_templates_root, exist_ok=True)


def _load_config():
    """加载配置文件；文件不存在时先自动生成一份"""
    global _config
    if not os.path.exists(_config_path):
        _init_config_file()
    try:
        with open(_config_path, "r", encoding="utf-8") as f:
            _config = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        _config = _default_config()

    # 兼容旧配置：把「连续未匹配次数」折算成「未匹配超时（秒）」，
    # 只改内存里的配置，用户在前端点一次保存后才会写回文件。
    if "miss_timeout" not in _config:
        legacy = _config.pop("miss_threshold", None)
        _config["miss_timeout"] = resolve_miss_timeout(
            {} if legacy is None else {"miss_threshold": legacy})
    if "battle_timeout" not in _config:
        _config["battle_timeout"] = resolve_battle_timeout({})

    # 同步到管理器
    manager.set_config(_config)


def _init_config_file():
    """
    首次运行：从 config.example.json 复制出 config.json。

    config.json 是本机私有配置（含窗口名等），不纳入版本管理，
    所以克隆仓库或解压发布包后第一次运行时需要现生成一份。
    样例文件缺失时退回内置默认配置。
    """
    example = os.path.join(BASE_DIR, "config.example.json")
    try:
        if os.path.exists(example):
            shutil.copyfile(example, _config_path)
        else:
            with open(_config_path, "w", encoding="utf-8") as f:
                json.dump(_default_config(), f, ensure_ascii=False, indent=2)
    except OSError:
        pass  # 写入失败则退回内存中的默认配置


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
        "mouse_speed_min": 1500,
        "mouse_speed_max": 2000,
        "match_confirm_count": 2,
        "detection_scale": 0.5,
        "miss_timeout": 8.0,
        "battle_timeout": 240,
        "miss_retry_sleep": 2.0,
        "same_stage_click_limit": 10,
        "instances": [
            {
                "id": "window-1",
                "name": "bot1",
                "window_title": "",
                "template_scene": "",
                "limit": 200,
            },
        ],
    }


# ============================================================
#  Eel 暴露接口 — 控制
# ============================================================
@eel.expose
def stop_bot(instance_id: str):
    """停止指定实例"""
    return manager.stop(instance_id)


@eel.expose
def toggle_bot(instance_id: str):
    """
    单按钮切换：空闲→启动，运行中→暂停，已暂停→继续。
    由后端按当前状态分派，前端无需判断状态。
    """
    _load_config()
    inst_cfg = _get_instance_config(instance_id)
    if inst_cfg is None:
        return {"ok": False, "msg": f"实例 [{instance_id}] 不存在"}
    return manager.toggle(instance_id, inst_cfg)


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
                "mouse_speed_min": _config.get("mouse_speed_min", 1500),
                "mouse_speed_max": _config.get("mouse_speed_max", 2000),
                "match_confirm_count": _config.get("match_confirm_count", 2),
                "detection_scale": _config.get("detection_scale", 0.5),
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
        "window_title": _config.get("instances", [{}])[0].get("window_title", "") if _config.get("instances") else "",
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
def _non_ascii_chars(text: str) -> str:
    """返回字符串里出现的非 ASCII 字符（去重保序），用来在提示里点名哪些字不能用"""
    seen = []
    for ch in text or "":
        if ord(ch) > 127 and ch not in seen:
            seen.append(ch)
    return "".join(seen)


def _sanitize_filename(name: str) -> str:
    """
    把文件名里的非 ASCII 字符清理掉，只留英文、数字、点、下划线、横线。

    Windows 下 OpenCV（cv2.imread）读不了中文路径，模板图存成中文名
    之后程序就再也读不到，所以导入时统一净化，别让用户踩这个坑。
    全是中文的文件名（如「魂王按钮.png」）会退回 imported.png。
    """
    base = os.path.basename((name or "").replace("\\", "/"))
    stem, ext = os.path.splitext(base)
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("_")
    if not stem:
        stem = "imported"
    if not re.fullmatch(r"\.[A-Za-z0-9]{1,5}", ext or ""):
        ext = ".png"
    return stem + ext


# ============================================================
#  阶段（<场景>/序号_标签_名字，直接放在场景目录下）
# ============================================================
def _stages_dir(scene: str) -> str:
    """阶段文件直接存在场景目录下，不再有 stages/ 子目录"""
    return os.path.join(_templates_root, scene)


def _stage_paths(scene: str, key: str):
    """返回 (标志图路径, 按钮模板目录)"""
    sdir = _stages_dir(scene)
    return os.path.join(sdir, f"{key}.png"), os.path.join(sdir, key)


def _migrate_legacy_structure(scene: str) -> bool:
    """
    把两种旧布局归拢到场景目录下（阶段现在直接存在 <场景>/ 里）：

    1. 早先版本用的 <场景>/stages/ 子目录 → 搬回 <场景>/，空的 stages/ 删掉
    2. 更早的 begin.png / begin/ → 01_start_begin.png / 01_start_begin/
       end.png / end/ → 02_end_result.png / 02_end_result/

    幂等：已有同名文件不覆盖；对应 tag 已经存在阶段时不再重复补。
    """
    scene_dir = os.path.join(_templates_root, scene)
    if not os.path.isdir(scene_dir):
        return False
    moved = False

    # 1) stages/ 子目录里的阶段搬回场景根
    old_dir = os.path.join(scene_dir, STAGES_DIRNAME)
    if os.path.isdir(old_dir):
        try:
            for name in os.listdir(old_dir):
                src = os.path.join(old_dir, name)
                dst = os.path.join(scene_dir, name)
                if not os.path.exists(dst):
                    os.replace(src, dst)
                    moved = True
            os.rmdir(old_dir)
        except OSError:
            pass

    # 2) 旧 begin/end 结构 → 新命名
    used_index, used_tag = [], set()
    for fname in os.listdir(scene_dir):
        parsed = parse_stage_stem(os.path.splitext(fname)[0])
        if parsed is not None:
            used_index.append(parsed[0])
            used_tag.add(parsed[1])
    for index, tag, name in DEFAULT_STAGES:
        if tag in used_tag:
            continue
        legacy = "begin" if tag == "start" else "end"
        flag_src = os.path.join(scene_dir, f"{legacy}.png")
        dir_src = os.path.join(scene_dir, legacy)
        if not os.path.isfile(flag_src) and not os.path.isdir(dir_src):
            continue
        i = index
        while i in used_index:
            i += 1
        used_index.append(i)
        key = stage_key(i, tag, name)
        flag_dst = os.path.join(scene_dir, f"{key}.png")
        dir_dst = os.path.join(scene_dir, key)
        try:
            if os.path.isfile(flag_src) and not os.path.exists(flag_dst):
                os.replace(flag_src, flag_dst)
            if os.path.isdir(dir_src) and not os.path.exists(dir_dst):
                os.replace(dir_src, dir_dst)
            # 标志图缺失时补占位图，否则阶段卡片不会出现在列表里
            if not os.path.isfile(flag_dst):
                _write_placeholder_png(flag_dst)
        except OSError:
            continue
        moved = True
    return moved


def _scan_stages(scene: str) -> list:
    """扫描场景目录下的阶段标志图，按序号排序（老场景会先归一化旧布局）"""
    _migrate_legacy_structure(scene)
    sdir = _stages_dir(scene)
    if not os.path.isdir(sdir):
        return []
    items = []
    for fname in os.listdir(sdir):
        stem, ext = os.path.splitext(fname)
        if ext.lower() not in (".png", ".jpg", ".jpeg", ".bmp"):
            continue
        parsed = parse_stage_stem(stem)
        if parsed is None:
            continue
        index, tag, name = parsed
        items.append({
            "index": index, "tag": tag, "name": name,
            "key": stage_key(index, tag, name),
            "flag_path": os.path.join(sdir, fname),
        })
    items.sort(key=lambda x: x["index"])
    return items


def _sorted_by_role(order: list) -> list:
    """按角色重排：start 恒第一、end 恒最后，mid 保持原有相对顺序"""
    rank = {"start": 0, "mid": 1, "end": 2}
    return sorted(order, key=lambda s: rank.get(s["tag"], 1))


def _reindex_stages(scene: str, ordered: list):
    """
    按给定顺序重排阶段编号（01..NN 连续）。
    先全部改成临时名再改成终名，否则 02 与 03 互换时会互相覆盖。
    """
    sdir = _stages_dir(scene)
    staged = []
    for i, item in enumerate(ordered, start=1):
        old_key = item["key"]
        tmp_key = f"__tmp{i:03d}__"
        src = os.path.join(sdir, old_key + ".png")
        if os.path.exists(src):
            os.replace(src, os.path.join(sdir, tmp_key + ".png"))
        src_dir = os.path.join(sdir, old_key)
        if os.path.isdir(src_dir):
            os.replace(src_dir, os.path.join(sdir, tmp_key))
        staged.append((tmp_key, item["tag"], item["name"], i))

    for tmp_key, tag, name, i in staged:
        final_key = stage_key(i, tag, name)
        src = os.path.join(sdir, tmp_key + ".png")
        if os.path.exists(src):
            os.replace(src, os.path.join(sdir, final_key + ".png"))
        src_dir = os.path.join(sdir, tmp_key)
        if os.path.isdir(src_dir):
            os.replace(src_dir, os.path.join(sdir, final_key))


def _validate_stage_name(scene: str, name: str, tag: str, ignore_key: str = ""):
    """校验阶段名与标签，返回错误文案或 None"""
    bad = _non_ascii_chars(name)
    if bad:
        return f"阶段名不能含中文或其它非 ASCII 字符：{bad}"
    if not re.fullmatch(r"[a-z0-9_]{1,12}", name or ""):
        return "阶段名只能用 1~12 个小写字母、数字或下划线，例如 mvp、reward"
    if tag not in STAGE_TAGS:
        return "阶段标签只能是 start / mid / end"
    for st in _scan_stages(scene):
        if st["key"] == ignore_key:
            continue
        if st["name"] == name:
            return f"已有同名阶段「{name}」，换个名字"
        if tag in ("start", "end") and st["tag"] == tag:
            return f"已经有 {tag} 阶段了，开始 / 结束各只能有一个"
    return None


@eel.expose
def list_stages(scene: str):
    """列出场景的全部阶段（含标志缩略图与按钮模板缩略图）"""
    if not os.path.isdir(os.path.join(_templates_root, scene)):
        return {"ok": False, "msg": "场景不存在", "stages": []}
    disabled = _load_disabled_templates(scene)
    out = []
    for st in _scan_stages(scene):
        status = _image_status(st["flag_path"])
        thumb = _image_to_base64_thumb(st["flag_path"]) if status["exists"] else ""
        tdir = os.path.join(_stages_dir(scene), st["key"])
        templates = []
        if os.path.isdir(tdir):
            for fname in sorted(os.listdir(tdir)):
                if not fname.lower().endswith((".png", ".jpg", ".jpeg", ".bmp")):
                    continue
                templates.append({
                    "filename": fname,
                    "thumbnail": _image_to_base64_thumb(os.path.join(tdir, fname)),
                    "enabled": not _is_template_disabled(disabled, st["key"], fname),
                })
        out.append({
            "index": st["index"], "tag": st["tag"], "name": st["name"],
            "key": st["key"], "flag": status, "flag_thumb": thumb,
            "templates": templates,
        })
    return {"ok": True, "stages": out}


@eel.expose
def add_stage(scene: str, name: str, tag: str = "mid", after_key: str = ""):
    """
    新增阶段：只填名字，标签默认 mid，编号自动生成。

    顺序固定为「start 第一、end 最后」，新加的 mid 一律落在两者之间：
    选中某行就插在它后面（选中的是 end 则插在 end 之前），
    没选中就插在最后一个非 end 阶段之后。
    """
    if not os.path.isdir(os.path.join(_templates_root, scene)):
        return {"ok": False, "msg": "场景不存在"}
    name = (name or "").strip()
    err = _validate_stage_name(scene, name, tag)
    if err:
        return {"ok": False, "msg": err}

    stages = _scan_stages(scene)
    try:
        os.makedirs(_stages_dir(scene), exist_ok=True)
        # 先用临时编号建文件，随后统一重排到最终编号
        index = stages[-1]["index"] + 1 if stages else 1
        key = stage_key(index, tag, name)
        flag_path, tpl_dir = _stage_paths(scene, key)
        os.makedirs(tpl_dir, exist_ok=True)
        _write_placeholder_png(flag_path)

        order = [{"key": s["key"], "tag": s["tag"], "name": s["name"]} for s in stages]
        if tag == "start":
            pos = 0
        elif tag == "end":
            pos = len(order)
        else:
            anchor = None
            if after_key:
                anchor = next((i for i, s in enumerate(order) if s["key"] == after_key), None)
            if anchor is None:
                # 没选中（或选中的行已不存在）：插在 end 之前
                pos = next((i for i, s in enumerate(order) if s["tag"] == "end"), len(order))
            elif order[anchor]["tag"] == "end":
                pos = anchor          # mid 不能排到 end 后面
            else:
                pos = anchor + 1
        order.insert(pos, {"key": key, "tag": tag, "name": name})
        _reindex_stages(scene, order)
        return {"ok": True,
                "msg": f"已新增阶段「{name}」（{tag}），接着设置标志图和按钮模板"}
    except OSError as e:
        return {"ok": False, "msg": str(e)}


@eel.expose
def update_stage(scene: str, key: str, name: str, tag: str):
    """修改阶段名字或标签，标志图与目录一起重命名"""
    stages = _scan_stages(scene)
    if not any(s["key"] == key for s in stages):
        return {"ok": False, "msg": "阶段不存在"}
    name = (name or "").strip()
    err = _validate_stage_name(scene, name, tag, ignore_key=key)
    if err:
        return {"ok": False, "msg": err}
    try:
        order = [{"key": s["key"], "tag": s["tag"], "name": s["name"]} for s in stages]
        for item in order:
            if item["key"] == key:
                item["tag"], item["name"] = tag, name
        _reindex_stages(scene, _sorted_by_role(order))
        return {"ok": True, "msg": "阶段已更新"}
    except OSError as e:
        return {"ok": False, "msg": str(e)}


@eel.expose
def move_stage(scene: str, key: str, direction: str):
    """调整阶段顺序：direction = up / down，编号自动重排"""
    stages = _scan_stages(scene)
    keys = [s["key"] for s in stages]
    if key not in keys:
        return {"ok": False, "msg": "阶段不存在"}
    i = keys.index(key)
    if stages[i]["tag"] in ("start", "end"):
        return {"ok": False, "msg": "开始 / 结束阶段固定在首尾，不能移动"}
    j = i - 1 if direction == "up" else i + 1
    if j < 0 or j >= len(keys):
        return {"ok": False, "msg": "已经到头了"}
    if stages[j]["tag"] in ("start", "end"):
        return {"ok": False, "msg": "中间阶段只能在 begin 与 end 之间调整"}
    order = [{"key": s["key"], "tag": s["tag"], "name": s["name"]} for s in stages]
    order[i], order[j] = order[j], order[i]
    try:
        _reindex_stages(scene, order)
        return {"ok": True, "msg": "顺序已调整"}
    except OSError as e:
        return {"ok": False, "msg": str(e)}


@eel.expose
def delete_stage(scene: str, key: str):
    """删除阶段（标志图 + 按钮模板目录），剩余编号自动重排"""
    stages = _scan_stages(scene)
    target = next((s for s in stages if s["key"] == key), None)
    if target is None:
        return {"ok": False, "msg": "阶段不存在"}
    try:
        flag_path, tpl_dir = _stage_paths(scene, key)
        if os.path.isfile(flag_path):
            os.remove(flag_path)
        if os.path.isdir(tpl_dir):
            shutil.rmtree(tpl_dir)
        order = [{"key": s["key"], "tag": s["tag"], "name": s["name"]}
                 for s in stages if s["key"] != key]
        _reindex_stages(scene, order)
        return {"ok": True, "msg": f"阶段「{target['name']}」已删除"}
    except OSError as e:
        return {"ok": False, "msg": str(e)}


@eel.expose
def import_stage_flag(scene: str, key: str, file_data: dict):
    """导入 / 替换某阶段的标志图"""
    if not os.path.isdir(_stages_dir(scene)):
        return {"ok": False, "msg": "场景或阶段不存在"}
    flag_path, _ = _stage_paths(scene, key)
    result = _save_material(flag_path, file_data.get("data", ""), f"{key} 阶段标志")
    if result.get("ok"):
        result["msg"] = f"{key} 标志已保存"
        result["thumb"] = _image_to_base64_thumb(flag_path)
    return result


@eel.expose
def import_stage_template(scene: str, key: str, file_data: dict):
    """导入某阶段的按钮模板"""
    if not os.path.isdir(_stages_dir(scene)):
        return {"ok": False, "msg": "场景或阶段不存在"}
    _, tpl_dir = _stage_paths(scene, key)
    os.makedirs(tpl_dir, exist_ok=True)
    raw_name = file_data.get("name", "imported.png")
    name = _sanitize_filename(raw_name)
    raw = base64.b64decode(file_data.get("data", ""))
    fpath = os.path.join(tpl_dir, name)
    base, ext = os.path.splitext(name)
    counter = 1
    while os.path.exists(fpath):
        fpath = os.path.join(tpl_dir, f"{base}_{counter}{ext}")
        counter += 1
    with open(fpath, "wb") as f:
        f.write(raw)
    saved = os.path.basename(fpath)
    if saved != os.path.basename(raw_name):
        return {"ok": True, "msg": f"已导入，原文件名含中文已存为 {saved}"}
    return {"ok": True, "msg": "模板已导入"}


@eel.expose
def delete_stage_template(scene: str, key: str, filename: str):
    """删除某阶段的一张按钮模板（同时从停用清单里移除，避免同名新模板被误停用）"""
    _, tpl_dir = _stage_paths(scene, key)
    fpath = os.path.join(tpl_dir, filename)
    if os.path.isfile(fpath):
        os.remove(fpath)
        disabled = _load_disabled_templates(scene)
        if _drop_disabled_entry(disabled, key, filename):
            try:
                _save_disabled_templates(scene, disabled)
            except OSError:
                pass
        return {"ok": True, "msg": "模板已删除"}
    return {"ok": False, "msg": "文件不存在"}


@eel.expose
def set_template_enabled(scene: str, key: str, filename: str, enabled: bool):
    """启用 / 停用某阶段的一张按钮模板（停用的模板运行时跳过，不参与匹配）"""
    _, tpl_dir = _stage_paths(scene, key)
    if not os.path.isfile(os.path.join(tpl_dir, filename)):
        return {"ok": False, "msg": "文件不存在"}
    disabled = _load_disabled_templates(scene)
    if enabled:
        if not _drop_disabled_entry(disabled, key, filename):
            return {"ok": True, "msg": "模板已是启用状态"}
        msg = "模板已启用"
    else:
        if _is_template_disabled(disabled, key, filename):
            return {"ok": True, "msg": "模板已是停用状态"}
        disabled.append(f"{key}/{filename}")
        msg = "模板已停用"
    try:
        _save_disabled_templates(scene, disabled)
    except OSError as e:
        return {"ok": False, "msg": f"写入停用清单失败：{e}"}
    return {"ok": True, "msg": msg, "enabled": bool(enabled)}


@eel.expose
def capture_stage(scene: str, key: str, kind: str, image_data: str,
                  x1: int, y1: int, x2: int, y2: int):
    """保存截取的阶段标志或按钮模板，kind = flag / template"""
    from PIL import Image
    raw = base64.b64decode(image_data)
    img = Image.open(io.BytesIO(raw))
    left, top = min(x1, x2), min(y1, y2)
    right, bottom = max(x1, x2), max(y1, y2)
    cropped = img.crop((left, top, right, bottom))

    flag_path, tpl_dir = _stage_paths(scene, key)
    try:
        if kind == "flag":
            # 与导入走同一套阈值：太小的图会被当成「未设置」，
            # 存下来只会让人以为设好了，运行时却认不出这一页
            if cropped.width < _PLACEHOLDER_MAX_PX or cropped.height < _PLACEHOLDER_MAX_PX:
                return {"ok": False,
                        "msg": f"框选范围太小（{cropped.width}x{cropped.height}），"
                               f"标志图短边至少 {_PLACEHOLDER_MAX_PX}px，重新框一次"}
            cropped.convert("RGB").save(flag_path, "PNG")
            return {"ok": True, "msg": f"{key} 标志已保存"}
        os.makedirs(tpl_dir, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        fpath = os.path.join(tpl_dir, f"capture_{ts}.png")
        cropped.save(fpath, "PNG")
        return {"ok": True, "msg": "模板已保存", "filename": os.path.basename(fpath)}
    except OSError as e:
        return {"ok": False, "msg": str(e)}


def list_scenes():
    """列出所有场景目录名（内部助手，经 get_config 的 _available_scenes 提供给前端）"""
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
    """新建场景：一张 baseline 占位图 + 两个默认阶段（01_start_begin、02_end_result）"""
    if not name or not name.strip():
        return {"ok": False, "msg": "场景名不能为空"}
    name = name.strip()
    bad = _non_ascii_chars(name)
    if bad:
        return {"ok": False,
                "msg": f"场景名不能含中文或其它非 ASCII 字符：{bad}\n"
                       f"Windows 下 OpenCV 读不了中文路径，含中文的场景会读不到模板。\n"
                       f"请改用英文、数字或下划线，例如 chi、hunwang、999"}
    scene_dir = os.path.join(_templates_root, name)
    try:
        os.makedirs(scene_dir, exist_ok=True)
        baseline_path = os.path.join(scene_dir, "baseline.png")
        if not os.path.isfile(baseline_path):
            _write_placeholder_png(baseline_path)
        # 默认只建第一个和最后一个阶段：绝大多数副本就这两个
        for index, tag, stage_name in DEFAULT_STAGES:
            key = stage_key(index, tag, stage_name)
            flag_path, tpl_dir = _stage_paths(name, key)
            os.makedirs(tpl_dir, exist_ok=True)
            if not os.path.isfile(flag_path):
                _write_placeholder_png(flag_path)
        return {"ok": True,
                "msg": f"场景「{name}」已创建（默认生成 01_start_begin 与 02_end_result 两个阶段）"}
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
def screenshot_screen(window_title: str = ""):
    """
    截取屏幕，返回 base64。

    window_title 非空时只截这个窗口，取景规则与运行时 Bot.capture_window 完全一致
    （整窗口矩形、裁剪到屏幕内），这样模板/baseline 与运行时画面同尺度；为空则截整屏。

    抓屏瞬间先把本程序自己的窗口（截屏窗、控制面板）临时藏起来，
    否则弹出的截屏窗自己会被拍进图里。
    """
    import pyautogui
    from PIL import Image

    title = (window_title or "").strip()
    hwnd = None
    if title:
        # 先找窗口再藏自己的窗口：自身窗口被藏起来就查不到，
        # 也就报不出「你把控制面板选成游戏窗口了」这个真正的原因
        hwnd, self_matched, _others = find_window_handle(title)
        if not hwnd:
            msg = (f"「{title}」指向的是本程序自己的窗口，不能用来截取模板"
                   if self_matched else
                   f"没找到标题含「{title}」的窗口，它可能已经关闭了")
            return {"ok": False, "msg": msg, "width": 0, "height": 0, "data": ""}

    hidden = _hide_self_windows()
    try:
        if hwnd:
            restore_and_activate(hwnd)      # 最小化的先还原，再切到前台
            time.sleep(0.15)
            grabbed = grab_window_region(hwnd)
            if grabbed is None:
                return {"ok": False, "msg": "目标窗口不可见或完全在屏幕外",
                        "width": 0, "height": 0, "data": ""}
            img, left, top, width, height = grabbed
        else:
            img: Image.Image = pyautogui.screenshot()
            left, top, width, height = 0, 0, img.width, img.height
    finally:
        _restore_windows(hidden)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    data = base64.b64encode(buf.getvalue()).decode()
    return {"ok": True, "width": width, "height": height, "data": data,
            "window": title, "left": left, "top": top}


@eel.expose
def check_baseline(scene: str):
    """检查场景的 baseline.png 是否已设置及其分辨率"""
    return _image_status(os.path.join(_templates_root, scene, "baseline.png"))


@eel.expose
def save_baseline(scene: str, image_data: str):
    """保存整张截图为 baseline.png"""
    return _save_material(os.path.join(_templates_root, scene, "baseline.png"),
                          image_data, "基准图")


@eel.expose
def make_baseline_placeholder(scene: str):
    """
    为场景生成 1×1 的 baseline 占位图，用户直接用截图覆盖同名文件即可。
    （阶段标志不再需要占位图，阶段在模板管理里新建时已自带。）
    """
    scene_dir = os.path.join(_templates_root, scene)
    if not os.path.isdir(scene_dir):
        return {"ok": False, "msg": "场景不存在"}
    try:
        _write_placeholder_png(os.path.join(scene_dir, "baseline.png"))
        return {"ok": True, "msg": "已生成 baseline.png 占位图（可直接用截图覆盖）"}
    except OSError as e:
        return {"ok": False, "msg": str(e)}


# ============================================================
#  Eel 暴露接口 — 窗口检测
# ============================================================
# dev 模式下 eel 用浏览器打开页面，窗口标题可能只有 "Eel"，整词匹配即可
_EEL_TITLE_RE = re.compile(r"\beel\b", re.IGNORECASE)


def _iter_self_windows():
    """枚举本程序自己的顶层可见窗口，返回 [hwnd, ...]"""
    import win32gui

    found = []

    def _cb(hwnd, _extra):
        if win32gui.IsWindowVisible(hwnd):
            title = win32gui.GetWindowText(hwnd)
            # app 模式下 eel 窗口的标题就是网页 title，一并按自身窗口处理。
            # 这里用整词匹配，避免把 SteelSeries 之类含 eel 的无关窗口也算进来
            if is_self_window(title) or _EEL_TITLE_RE.search(title):
                found.append(hwnd)
        return True

    win32gui.EnumWindows(_cb, None)
    return found


def _hide_self_windows():
    """
    抓屏前临时藏起本程序自己的窗口，返回被藏的句柄列表。
    已最小化的窗口跳过：它们本来就不在画面上，且恢复时会被弹回前台。
    """
    import win32gui
    import win32con

    hidden = []
    for hwnd in _iter_self_windows():
        if win32gui.IsIconic(hwnd):
            continue
        win32gui.ShowWindow(hwnd, win32con.SW_HIDE)
        hidden.append(hwnd)
    if hidden:
        # 等桌面把窗口原来占的区域重绘完，否则可能抓到残影
        time.sleep(0.2)
    return hidden


def _restore_windows(handles):
    """把 _hide_self_windows 藏起来的窗口还原并置前"""
    import win32gui
    import win32con

    for hwnd in handles:
        if not win32gui.IsWindow(hwnd):
            continue
        win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
        try:
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            pass


@eel.expose
def minimize_self():
    """截取模板时最小化前端窗口"""
    import win32gui
    import win32con

    for hwnd in _iter_self_windows():
        win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)


@eel.expose
def get_windows():
    """获取当前所有可见窗口标题列表"""
    import win32gui

    windows = []

    def _enum_cb(hwnd, _extra):
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd)
        # 排除本程序自己的控制面板，避免它被当成游戏窗口选中
        if is_self_window(title):
            return
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
    """.disabled.json 里记录被停用的模板文件名，由前端「停用」按钮写入"""
    path = _disabled_path(scene)
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return []
        if isinstance(data, list):
            return [str(x) for x in data]
    return []


def _save_disabled_templates(scene: str, disabled: list):
    path = _disabled_path(scene)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(disabled, f, ensure_ascii=False, indent=2)


def _is_template_disabled(disabled: list, key: str, filename: str) -> bool:
    """清单按「阶段 key/文件名」记录；同时兼容手工写的纯文件名条目"""
    return f"{key}/{filename}" in disabled or filename in disabled


def _drop_disabled_entry(disabled: list, key: str, filename: str) -> bool:
    """清掉该模板的停用记录（新旧两种写法都清），返回清单是否被改动"""
    changed = False
    for entry in (f"{key}/{filename}", filename):
        if entry in disabled:
            disabled.remove(entry)
            changed = True
    return changed


# 占位素材判定阈值：短边小于该值（像素）的图片视为「未设置」
# 需与 autoclick.py 里的 FLAG_PLACEHOLDER_MAX_PX 保持一致
_PLACEHOLDER_MAX_PX = 20


def _write_placeholder_png(path: str):
    """写入一张极小尺寸的占位图，用于标记「该素材尚未设置」"""
    from PIL import Image
    Image.new("RGB", (1, 1), (200, 200, 200)).save(path, "PNG")


def _image_status(path: str) -> dict:
    """
    检查素材是否已设置。
    新建场景时生成的占位图尺寸极小，据此区分「未设置」和用户导入的真实截图。
    """
    if not os.path.isfile(path):
        return {"exists": False, "width": 0, "height": 0}

    from PIL import Image
    try:
        with Image.open(path) as img:
            w, h = img.size
    except Exception:
        return {"exists": False, "width": 0, "height": 0}

    if w < _PLACEHOLDER_MAX_PX or h < _PLACEHOLDER_MAX_PX:
        return {"exists": False, "width": w, "height": h}
    return {"exists": True, "width": w, "height": h}


def _save_material(path: str, image_data: str, label: str = "素材") -> dict:
    """把前端传来的 base64 图片保存到指定路径（baseline / 阶段旗帜共用）"""
    from PIL import Image
    try:
        raw = base64.b64decode(image_data)
        img = Image.open(io.BytesIO(raw))
        if img.width < _PLACEHOLDER_MAX_PX or img.height < _PLACEHOLDER_MAX_PX:
            return {"ok": False,
                    "msg": f"图片尺寸过小（{img.width}x{img.height}），"
                           f"{label}需要短边至少 {_PLACEHOLDER_MAX_PX}px"}
        img.convert("RGB").save(path, "PNG")
        return {"ok": True, "msg": f"{label}已保存",
                "width": img.width, "height": img.height}
    except Exception as e:
        return {"ok": False, "msg": f"{label}保存失败: {e}"}


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


def _ensure_admin() -> bool:
    """
    游戏窗口若以管理员身份运行，本程序不提权时 Windows 会直接丢弃所有
    鼠标输入，表现就是「日志有点击、鼠标不动、游戏没反应」。

    :return True 表示当前已是管理员（或无法提权但不该阻塞启动）；
            False 表示已经用管理员身份重新拉起，当前进程应当退出
    """
    try:
        import ctypes
        if ctypes.windll.shell32.IsUserAnAdmin():
            return True
    except Exception:
        return True
    # 提权后被 UAC 拒绝过就别再循环弹窗
    if os.environ.get("YYS_ELEVATE_TRIED") == "1":
        return True
    os.environ["YYS_ELEVATE_TRIED"] = "1"
    try:
        import ctypes
        import subprocess
        if getattr(sys, "frozen", False):
            exe, args = sys.executable, ""
        else:
            exe = sys.executable
            args = subprocess.list2cmdline([os.path.abspath(sys.argv[0])] + sys.argv[1:])
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, args, BASE_DIR, 1)
        if rc > 32:
            return False
    except Exception:
        pass
    return True


if __name__ == "__main__":
    if not _ensure_admin():
        sys.exit(0)
    run()
