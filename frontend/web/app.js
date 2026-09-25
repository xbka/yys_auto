/* ============================================================
   阴阳师自动助手 — 前端逻辑 (v2 多实例版)
   ============================================================ */

// ============================================================
//  Tab 切换
// ============================================================
document.querySelectorAll(".tab").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach(b => b.classList.remove("active"));
    document.querySelectorAll(".tab-content").forEach(c => c.classList.remove("active"));
    btn.classList.add("active");
    const target = document.getElementById(btn.dataset.tab);
    if (target) target.classList.add("active");
  });
});

// ============================================================
//  全局变量
// ============================================================
let _currentScene = "";
let _captureType = "";   // 'begin' | 'end' for capture flow
let _instances = [];     // [{id, name, state, ...}, ...]
let _globalCfg = {};
let _pollTimer = null;
const POLL_INTERVAL = 800;  // ms

// ============================================================
//  初始化 & 轮询
// ============================================================
async function initApp() {
  try {
    const cfg = await eel.get_config()();
    _globalCfg = cfg;

    // 全局配置到表单
    loadGlobalConfigToForm(cfg);

    // 渲染实例卡片
    const instances = cfg.instances || [];
    _instances = instances.map(ic => ({
      id: ic.id,
      name: ic.name,
      window_title: ic.window_title || "",
      template_scene: ic.template_scene || "",
      limit: ic.limit ?? 200,
      state: "idle",
    }));
    renderInstanceCards(_instances);

    // 模板 Tab 初始化
    if (cfg._available_scenes) populateSceneSelector(cfg._available_scenes, cfg.instances?.[0]?.template_scene || "");

    // 开始轮询
    startPolling();
  } catch (e) {
    console.error("Init error:", e);
  }
}

function startPolling() {
  if (_pollTimer) clearInterval(_pollTimer);
  _pollTimer = setInterval(pollStatus, POLL_INTERVAL);
}

async function pollStatus() {
  try {
    const statuses = await eel.get_status()();
    for (const st of statuses) {
      const card = document.querySelector(`.inst-card[data-id="${st.id}"]`);
      if (!card) continue;

      // 更新状态标签
      const stateEl = card.querySelector(".inst-state");
      if (stateEl) {
        stateEl.textContent = stateLabel(st.state);
        stateEl.className = "inst-state " + st.state;
      }
      // 更新边框
      card.className = "inst-card " + st.state;

      // 更新统计
      const clickEl   = card.querySelector(".stat-clicks");
      const avgRoundEl= card.querySelector(".stat-avg-round");
      const avgBattleEl=card.querySelector(".stat-avg-battle");
      const elapsedEl = card.querySelector(".stat-elapsed");
      const etaEl     = card.querySelector(".stat-eta");
      if (clickEl)   clickEl.textContent   = st.total_clicks ?? "--";
      if (avgRoundEl) {
        const v = st.avg_duration;
        avgRoundEl.textContent = v ? Number(v).toFixed(1) + "s" : "--";
      }
      if (avgBattleEl) {
        const v = st.avg_battle_time;
        avgBattleEl.textContent = v ? Number(v).toFixed(1) + "s" : "--";
      }
      if (elapsedEl) {
        const e = st.elapsed;
        elapsedEl.textContent = e != null
          ? Math.floor(e / 60) + "m" + (e % 60).toString().padStart(2, "0") + "s"
          : "--";
      }
      if (etaEl) {
        const e = st.eta;
        etaEl.textContent = e != null && e > 0
          ? Math.floor(e / 60) + "m" + (e % 60).toString().padStart(2, "0") + "s"
          : "--";
      }

      // 更新点击分布图（数据未变化时跳过重绘）
      const chartEl = card.querySelector(".inst-chart");
      if (chartEl) {
        const curCnt = (st.click_positions && st.click_positions.length) || 0;
        const lastCnt = parseInt(chartEl.dataset.lastCnt) || 0;
        const needRedraw = curCnt !== lastCnt || Math.abs(chartEl.clientWidth * window.devicePixelRatio - (chartEl.width || 0)) > 2;
        if (needRedraw && st.click_positions && st.click_positions.length > 0) {
          drawClickChart(chartEl, st.click_positions, st.window_rect);
          chartEl.dataset.lastCnt = curCnt;
        } else if (needRedraw && (!st.click_positions || st.click_positions.length === 0)) {
          const ctx = chartEl.getContext("2d");
          const dpr = window.devicePixelRatio || 1;
          chartEl.width = chartEl.clientWidth * dpr;
          chartEl.height = chartEl.clientHeight * dpr;
          ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
          ctx.fillStyle = "#fff";
          ctx.fillRect(0, 0, chartEl.clientWidth, chartEl.clientHeight);
          ctx.fillStyle = "#aaa";
          ctx.font = "12px sans-serif";
          ctx.textAlign = "center";
          ctx.fillText("等待数据...", chartEl.clientWidth / 2, chartEl.clientHeight / 2);
          chartEl.dataset.lastCnt = 0;
        }
      }

      // 更新日志（在点击图下面）
      const logEl = card.querySelector(".inst-log");
      if (logEl) {
        const lines = st.log_lines || [];
        if (lines.length > 0) {
          // 只有用户没有手动上滚时，才自动滚到底部
          const wasAtBottom = (logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight) < 30;
          logEl.textContent = lines.slice(-50).join("\n");
          if (wasAtBottom) {
            logEl.scrollTop = logEl.scrollHeight;
          }
        } else {
          logEl.innerHTML = '<span class="inst-log-empty">暂无日志</span>';
        }
      }

      // 重绘按钮（根据新状态）
      refreshInstanceButtons(st.id, st.state);
    }
  } catch (e) { /* 忽略轮询错误 */ }
}

function stateLabel(s) {
  const map = { idle: "空闲", running: "运行中", paused: "已暂停", stopping: "停止中" };
  return map[s] || s;
}

// ============================================================
//  实例卡片渲染
// ============================================================
function renderInstanceCards(instances) {
  const container = document.getElementById("instancesContainer");
  if (!container) return;
  container.innerHTML = instances.map(inst => instanceCardHTML(inst)).join("");

  // 绑定每个卡片的事件
  for (const inst of instances) {
    bindCardEvents(inst.id);
  }
}

function instanceCardHTML(inst) {
  const maxBattles = inst.limit ?? 200;
  return `
<div class="inst-card idle" data-id="${escHtml(inst.id)}">
  <div class="inst-header">
    <span class="inst-name" contenteditable="true" data-inst-id="${escHtml(inst.id)}">${escHtml(inst.name)}</span>
    <span class="inst-state idle">空闲</span>
  </div>
  <div class="inst-config">
    <label>窗口:</label>
    <select class="cfg-input cfg-select inst-win-select" data-inst-id="${escHtml(inst.id)}">
      <option value="">-- 点击刷新 --</option>
    </select>
    <button class="btn btn-sm inst-refresh-wins" data-inst-id="${escHtml(inst.id)}">🔄</button>
    <label>场景:</label>
    <select class="cfg-input cfg-select inst-scene-select" data-inst-id="${escHtml(inst.id)}">
    </select>
    <label>战斗次数:</label>
    <input type="number" class="cfg-input cfg-num inst-limit" value="${maxBattles}" min="1" max="9999" step="1" data-inst-id="${escHtml(inst.id)}" style="width:64px;">
  </div>
  <div class="inst-stats">
    <div class="inst-stat"><div class="stat-label">回合</div><div class="stat-value stat-clicks">--</div></div>
    <div class="inst-stat"><div class="stat-label">均回合</div><div class="stat-value stat-avg-round">--</div></div>
    <div class="inst-stat"><div class="stat-label">战斗</div><div class="stat-value stat-avg-battle">--</div></div>
    <div class="inst-stat"><div class="stat-label">耗时</div><div class="stat-value stat-elapsed">--</div></div>
    <div class="inst-stat"><div class="stat-label">预计剩余</div><div class="stat-value stat-eta">--</div></div>
  </div>
  <canvas class="inst-chart" data-inst-id="${escHtml(inst.id)}"></canvas>
  <div class="inst-log"><span class="inst-log-empty">暂无日志</span></div>
  <div class="inst-btns" id="btns-${escHtml(inst.id)}">
    <button class="btn btn-start btn-sm inst-start" data-inst-id="${escHtml(inst.id)}">▶ 开始</button>
    <button class="btn btn-pause btn-sm inst-pause" data-inst-id="${escHtml(inst.id)}">⏸ 暂停</button>
    <button class="btn btn-stop btn-sm inst-stop" data-inst-id="${escHtml(inst.id)}">⏹ 停止</button>
    <button class="btn btn-sm inst-remove" data-inst-id="${escHtml(inst.id)}">✕</button>
  </div>
</div>`;
}

function bindCardEvents(instanceId) {
  // 开始按钮
  document.querySelector(`.inst-start[data-inst-id="${instanceId}"]`)?.addEventListener("click", () => {
    // 先保存该实例配置到全局
    saveSingleInstanceConfig(instanceId);
    // 延迟再启动（等配置保存完）
    setTimeout(() => startBotInstance(instanceId), 100);
  });

  // 暂停
  document.querySelector(`.inst-pause[data-inst-id="${instanceId}"]`)?.addEventListener("click", () => {
    eel.pause_bot(instanceId);
  });

  // 停止
  document.querySelector(`.inst-stop[data-inst-id="${instanceId}"]`)?.addEventListener("click", () => {
    eel.stop_bot(instanceId);
  });

    // 删除
  document.querySelector(`.inst-remove[data-inst-id="${instanceId}"]`)?.addEventListener("click", async () => {
    if (!confirm(`确定要删除实例「${instanceId}」吗？`)) return;
    const r = await eel.remove_instance(instanceId)();
    if (r.ok) {
      const cfg = await eel.get_config()();
      _instances = (cfg.instances || []).map(ic => ({
        id: ic.id,
        name: ic.name,
        window_title: ic.window_title || "",
        template_scene: ic.template_scene || "",
        limit: ic.limit ?? 200,
        state: "idle",
      }));
      renderInstanceCards(_instances);
      // 重新填充剩余卡片的窗口和场景下拉框
      for (const inst of _instances) {
        await populateInstanceWindowSelector(inst.id);
        await populateInstanceSceneSelector(inst.id, inst.template_scene);
      }
    } else {
      alert(r.msg);
    }
  });

  // 窗口选择器变化 → 自动保存
  document.querySelector(`.inst-win-select[data-inst-id="${instanceId}"]`)?.addEventListener("change", function() {
    eel.save_instance_config(instanceId, "window_title", this.value);
  });

  // 场景选择器变化 → 自动保存
  document.querySelector(`.inst-scene-select[data-inst-id="${instanceId}"]`)?.addEventListener("change", function() {
    eel.save_instance_config(instanceId, "template_scene", this.value);
  });

  // 战斗次数变化 → 自动保存
  document.querySelector(`.inst-limit[data-inst-id="${instanceId}"]`)?.addEventListener("change", function() {
    const v = parseInt(this.value) || 200;
    this.value = Math.max(1, Math.min(9999, v));
    eel.save_instance_config(instanceId, "limit", v);
  });

  // 名称编辑完成
  document.querySelector(`.inst-name[data-inst-id="${instanceId}"]`)?.addEventListener("blur", function() {
    const newName = this.textContent.trim();
    eel.save_instance_config(instanceId, "name", newName);
  });

  // 刷新窗口按钮
  document.querySelector(`.inst-refresh-wins[data-inst-id="${instanceId}"]`)?.addEventListener("click", async () => {
    await populateInstanceWindowSelector(instanceId);
  });
}

function refreshInstanceButtons(instanceId, state) {
  const btns = document.getElementById("btns-" + instanceId);
  if (!btns) return;
  const start  = btns.querySelector(".inst-start");
  const pause  = btns.querySelector(".inst-pause");
  const stop   = btns.querySelector(".inst-stop");
  const remove = btns.querySelector(".inst-remove");

  if (state === "idle") {
    if (start) { start.style.display = "inline-block"; start.textContent = "▶ 开始"; }
    if (pause) pause.style.display = "none";
    if (stop) stop.style.display = "none";
    if (remove) remove.style.display = "inline-block";
  } else if (state === "running") {
    if (start) start.style.display = "none";
    if (pause) pause.style.display = "inline-block";
    if (stop) stop.style.display = "inline-block";
    if (remove) remove.style.display = "none";
  } else if (state === "paused") {
    if (start) { start.style.display = "inline-block"; start.textContent = "🔄 继续"; }
    if (pause) pause.style.display = "none";
    if (stop) stop.style.display = "inline-block";
    if (remove) remove.style.display = "none";
  } else {
    // stopping
    if (start) start.style.display = "none";
    if (pause) pause.style.display = "none";
    if (stop) stop.style.display = "inline-block";
    if (remove) remove.style.display = "none";
  }
}

async function startBotInstance(instanceId) {
  const r = await eel.start_bot(instanceId)();
  if (!r.ok) alert(r.msg);
}

// ============================================================
//  窗口列表填充
// ============================================================
async function populateInstanceWindowSelector(instanceId) {
  try {
    const wins = await eel.get_windows()();
    const sel = document.querySelector(`.inst-win-select[data-inst-id="${instanceId}"]`);
    if (!sel) return;
    const cur = sel.value || "";
    let html = '<option value="">-- 不选择 --</option>';
    let found = false;
    for (const w of wins) {
      const selected = (w.title === cur);
      if (selected) found = true;
      html += `<option value="${escHtml(w.title)}" ${selected ? "selected" : ""}>${escHtml(w.title)}</option>`;
    }
    if (!found && cur) {
      html += `<option value="${escHtml(cur)}" selected>${escHtml(cur)}</option>`;
    }
    sel.innerHTML = html;
  } catch (e) {
    console.error(e);
  }
}

async function populateInstanceSceneSelector(instanceId, currentScene) {
  try {
    const cfg = await eel.get_config()();
    const scenes = cfg._available_scenes || [];
    const sel = document.querySelector(`.inst-scene-select[data-inst-id="${instanceId}"]`);
    if (!sel) return;
    sel.innerHTML = ['<option value="">-- 无场景 --</option>']
      .concat(scenes.map(s => `<option value="${s}" ${s === currentScene ? "selected" : ""}>${s}</option>`))
      .join("");
  } catch (e) {
    console.error(e);
  }
}

// ============================================================
//  全局配置表单
// ============================================================
function loadGlobalConfigToForm(cfg) {
  setVal("cfgThreshold", cfg.threshold ?? 0.75);
  setVal("cfgDetectScale", cfg.detection_scale ?? 0.5);
  setVal("cfgMatchConfirm", cfg.match_confirm_count ?? 2);
  setVal("cfgMouseMin", cfg.mouse_speed_min ?? 2000);
  setVal("cfgMouseMax", cfg.mouse_speed_max ?? 2500);
  setVal("cfgRestRounds", cfg.rest_rounds ?? 50);
  setVal("cfgRestRoundsVar", cfg.rest_rounds_var ?? 10);
  setVal("cfgRestSeconds", cfg.rest_seconds ?? 30);
  setVal("cfgRestSecondsVar", cfg.rest_seconds_var ?? 5);
  setVal("cfgMissThreshold", cfg.miss_threshold ?? 20);
  setVal("cfgMissRetrySleep", cfg.miss_retry_sleep ?? 2.0);
  const s = cfg.screen || {};
  setVal("cfgScrXmin", s.xmin ?? 0);
  setVal("cfgScrXmax", s.xmax ?? 1920);
  setVal("cfgScrYmin", s.ymin ?? 0);
  setVal("cfgScrYmax", s.ymax ?? 1080);
}

// ============================================================
//  添加实例按钮
// ============================================================
document.getElementById("btnAddInstance").addEventListener("click", async () => {
  const r = await eel.add_instance()();
  if (r.ok) {
    const cfg = await eel.get_config()();
    _instances = (cfg.instances || []).map(ic => ({
      id: ic.id,
      name: ic.name,
      window_title: ic.window_title || "",
      template_scene: ic.template_scene || "",
      limit: ic.limit ?? 200,
      state: "idle",
    }));
    renderInstanceCards(_instances);
    // 填充新实例的窗口和场景列表
    for (const inst of _instances) {
      await populateInstanceWindowSelector(inst.id);
      await populateInstanceSceneSelector(inst.id, inst.template_scene);
    }
  } else {
    alert(r.msg);
  }
});

// ============================================================
//  保存单个实例配置
// ============================================================
async function saveSingleInstanceConfig(instanceId) {
  const winSel = document.querySelector(`.inst-win-select[data-inst-id="${instanceId}"]`);
  const scnSel = document.querySelector(`.inst-scene-select[data-inst-id="${instanceId}"]`);
  if (winSel) await eel.save_instance_config(instanceId, "window_title", winSel.value);
  if (scnSel) await eel.save_instance_config(instanceId, "template_scene", scnSel.value);
}

// ============================================================
//  保存全局配置
// ============================================================
document.getElementById("btnSaveCfg").addEventListener("click", async () => {
  const c = await eel.get_config()();
  c.threshold     = parseFloat(document.getElementById("cfgThreshold").value)     || 0.75;
  c.detection_scale = parseFloat(document.getElementById("cfgDetectScale").value) || 0.5;
  c.match_confirm_count = parseInt(document.getElementById("cfgMatchConfirm").value) || 2;
  c.mouse_speed_min  = parseInt(document.getElementById("cfgMouseMin").value)  || 2000;
  c.mouse_speed_max  = parseInt(document.getElementById("cfgMouseMax").value)  || 2500;
  c.rest_rounds      = parseInt(document.getElementById("cfgRestRounds").value)    || 50;
  c.rest_rounds_var  = parseInt(document.getElementById("cfgRestRoundsVar").value)  || 10;
  c.rest_seconds     = parseInt(document.getElementById("cfgRestSeconds").value)     || 30;
  c.rest_seconds_var = parseInt(document.getElementById("cfgRestSecondsVar").value)  || 5;
  c.miss_threshold   = parseInt(document.getElementById("cfgMissThreshold").value)   || 20;
  c.miss_retry_sleep = parseFloat(document.getElementById("cfgMissRetrySleep").value) || 2.0;
  c.screen = {
    xmin: parseInt(document.getElementById("cfgScrXmin").value) || 0,
    xmax: parseInt(document.getElementById("cfgScrXmax").value) || 1920,
    ymin: parseInt(document.getElementById("cfgScrYmin").value) || 0,
    ymax: parseInt(document.getElementById("cfgScrYmax").value) || 1080,
  };
  const r = await eel.save_config(c)();
  alert(r.msg);
});

document.getElementById("btnResetCfg").addEventListener("click", async () => {
  if (!confirm("确定恢复默认全局配置？")) return;
  const cfg = await eel.reset_config()();
  loadGlobalConfigToForm(cfg);
  alert("已恢复默认");
});

// ============================================================
//  紧急停止
// ============================================================
window.addEventListener("keydown", (e) => {
  if (e.key === "F12") {
    e.preventDefault();
    eel.emergency_stop();
    alert("⚡ F12 紧急停止已触发！（所有实例）");
  }
});

// ============================================================
//  模板 Tab — 场景选择
// ============================================================
function populateSceneSelector(scenes, current) {
  _currentScene = current || (scenes.length > 0 ? scenes[0] : "");
  const sel = document.getElementById("selScene");
  if (!sel) return;
  sel.innerHTML = scenes.map(s =>
    `<option value="${s}" ${s === _currentScene ? "selected" : ""}>${s}</option>`
  ).join("");
  if (scenes.length === 0) {
    sel.innerHTML = '<option value="">-- 无场景 --</option>';
    _currentScene = "";
  }
  if (_currentScene) loadTemplates(_currentScene);
  else updateBaselineUI("");
}

document.getElementById("selScene")?.addEventListener("change", function() {
  _currentScene = this.value;
  if (_currentScene) loadTemplates(_currentScene);
});

document.getElementById("btnNewScene")?.addEventListener("click", async () => {
  const name = prompt("输入新场景名称（中文 OK）：");
  if (!name || !name.trim()) return;
  const r = await eel.create_scene(name)();
  if (r.ok) {
    const cfg = await eel.get_config()();
    populateSceneSelector(cfg._available_scenes || [], name.trim());
  } else {
    alert(r.msg);
  }
});

document.getElementById("btnDelScene")?.addEventListener("click", async () => {
  if (!_currentScene) return;
  if (!confirm(`确定删除场景「${_currentScene}」？\n其中的所有模板也会被删除。`)) return;
  const r = await eel.delete_scene(_currentScene)();
  if (r.ok) {
    const cfg = await eel.get_config()();
    populateSceneSelector(cfg._available_scenes || [], "");
  } else {
    alert(r.msg);
  }
});

// ============================================================
//  模板加载 & 交互
// ============================================================
async function loadTemplates(scene) {
  try {
    const tpls = await eel.get_templates(scene)();
    renderTemplateCards("tplBegin", tpls.begin, "begin", scene);
    renderTemplateCards("tplEnd", tpls.end, "end", scene);
  } catch (e) {
    console.error("loadTemplates error:", e);
  }
  updateBaselineUI(scene);
}

function renderTemplateCards(containerId, items, ttype, scene) {
  const el = document.getElementById(containerId);
  if (!el) return;
  if (!items || items.length === 0) {
    el.innerHTML = '<div class="empty-hint">暂无模板</div>';
    return;
  }
  el.innerHTML = items.map(item => `
    <div class="tpl-card ${item.enabled ? "" : "disabled"}">
      <button class="tmpl-del" title="删除" onclick="delTemplate('${escAttr(scene)}','${escAttr(ttype)}','${escAttr(item.filename)}')">&times;</button>
      <img src="data:image/png;base64,${item.thumbnail}" alt="${escHtml(item.filename)}">
      <div class="tmpl-name" title="${escHtml(item.filename)}">${escHtml(item.filename)}</div>
      <button class="tmpl-toggle" onclick="toggleTpl(this,'${escAttr(scene)}','${escAttr(ttype)}','${escAttr(item.filename)}')">
        ${item.enabled ? "✅ 启用" : "❌ 禁用"}
      </button>
    </div>
  `).join("");
}

async function toggleTpl(btn, scene, ttype, filename) {
  const card = btn.closest(".tpl-card");
  const isEnabled = !card.classList.contains("disabled");
  const r = await eel.toggle_template(scene, ttype, filename, !isEnabled)();
  if (r.ok) {
    card.classList.toggle("disabled");
    btn.textContent = isEnabled ? "❌ 禁用" : "✅ 启用";
  }
}

async function delTemplate(scene, ttype, filename) {
  if (!confirm(`确定删除模板「${filename}」？`)) return;
  const r = await eel.delete_template(scene, ttype, filename)();
  if (r.ok) loadTemplates(scene);
  else alert(r.msg);
}

// ============================================================
//  截屏工具（模板）
// ============================================================
window.startCapture = function(ttype) {
  if (!_currentScene) { alert("请先选择场景"); return; }
  _captureType = ttype;
  eel.minimize_self();
  const w = 1020, h = 680;
  const left = (screen.width - w) / 2;
  const top = (screen.height - h) / 2;
  setTimeout(() => {
    window.open("capture.html", "captureWindow",
      `width=${w},height=${h},left=${left},top=${top},resizable=yes`);
  }, 300);
};

// 接收截屏结果
window.addEventListener("message", async (e) => {
  if (e.data?.type === "capture_result") {
    const { imageData, x1, y1, x2, y2 } = e.data;
    const r = await eel.capture_template(
      _currentScene, _captureType, imageData, x1, y1, x2, y2
    )();
    if (r.ok) {
      loadTemplates(_currentScene);
    } else {
      alert("截取失败：" + r.msg);
    }
  }
});

// ============================================================
//  导入本地图片
// ============================================================
let _importType = "";
window.importLocal = function(ttype) {
  if (!_currentScene) { alert("请先选择场景"); return; }
  _importType = ttype;
  document.getElementById("fileImport").click();
};

document.getElementById("fileImport").addEventListener("change", async function() {
  const file = this.files[0];
  if (!file || !_currentScene) return;
  const reader = new FileReader();
  reader.onload = async () => {
    const raw = reader.result;
    const b64 = raw.split(",")[1];
    const r = await eel.import_template(_currentScene, _importType, {
      name: file.name,
      data: b64,
    })();
    if (r.ok) loadTemplates(_currentScene);
    else alert("导入失败：" + r.msg);
  };
  reader.readAsDataURL(file);
  this.value = "";
});

// ============================================================
//  baseline 基准图
// ============================================================
async function updateBaselineUI(scene) {
  const card = document.getElementById("baselineCard");
  if (!card) return;
  if (!scene) {
    card.style.display = "none";
    return;
  }
  card.style.display = "";
  try {
    const r = await eel.check_baseline(scene)();
    const statusEl = document.getElementById("baselineStatus");
    const sizeEl = document.getElementById("baselineSize");
    if (!statusEl) return;
    if (r.exists) {
      statusEl.textContent = "已设置";
      statusEl.className = "baseline-status ready";
      sizeEl.textContent = `${r.width} × ${r.height}`;
    } else {
      statusEl.textContent = "未设置";
      statusEl.className = "baseline-status missing";
      sizeEl.textContent = "";
    }
  } catch (e) {
    console.error("checkBaseline error:", e);
  }
}

// 截取基准图（全屏截图，不需要裁剪）
async function captureBaseline() {
  if (!_currentScene) { alert("请先选择场景"); return; }
  await eel.minimize_self();
  await new Promise(r => setTimeout(r, 600));
  try {
    const r = await eel.screenshot_screen()();
    const result = await eel.save_baseline(_currentScene, r.data)();
    if (result.ok) {
      updateBaselineUI(_currentScene);
    } else {
      alert("保存失败：" + result.msg);
    }
  } catch (e) {
    alert("截取失败：" + e);
  }
}

document.getElementById("btnCaptureBaseline")?.addEventListener("click", captureBaseline);

document.getElementById("btnImportBaseline")?.addEventListener("click", () => {
  if (!_currentScene) { alert("请先选择场景"); return; }
  document.getElementById("baselineImportInput").click();
});

document.getElementById("baselineImportInput")?.addEventListener("change", async function() {
  const file = this.files[0];
  if (!file || !_currentScene) return;
  const reader = new FileReader();
  reader.onload = async () => {
    const raw = reader.result;
    const b64 = raw.split(",")[1];
    const r = await eel.save_baseline(_currentScene, b64)();
    if (r.ok) {
      updateBaselineUI(_currentScene);
    } else {
      alert("导入失败：" + r.msg);
    }
  };
  reader.readAsDataURL(file);
  this.value = "";
});

// ============================================================
//  工具函数
// ============================================================
function setVal(id, val) {
  const el = document.getElementById(id);
  if (el) el.value = val ?? "";
}

function escHtml(str) {
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

function escAttr(str) {
  return String(str).replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/'/g, "&#39;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

// ============================================================
//  点击分布散点图（坐标相对窗口，比例与游戏窗口一致）
// ============================================================
function drawClickChart(canvas, positions, windowRect) {
  if (!positions || !positions.length) return;
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  const w = rect.width;
  const h = rect.height;
  if (canvas.width !== w * dpr || canvas.height !== h * dpr) {
    canvas.width  = w * dpr;
    canvas.height = h * dpr;
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

  // 背景
  ctx.fillStyle = "#fff";
  ctx.fillRect(0, 0, w, h);

  // 窗口尺寸 → 绘图区域保持与游戏窗口相同宽高比
  const winW = (windowRect && windowRect[2]) || 1920;
  const winH = (windowRect && windowRect[3]) || 1080;
  const winLeft = (windowRect && windowRect[0]) || 0;
  const winTop  = (windowRect && windowRect[1]) || 0;

  const pad = 12;
  const availW = w - pad * 2;
  const availH = h - pad * 2;
  const winRatio = winH > 0 ? winW / winH : 16/9;

  // 在可用空间内按窗口比例缩放，居中放置绘图区
  let plotW, plotH, offX, offY;
  if (availW / availH > winRatio) {
    plotH = availH;
    plotW = plotH * winRatio;
    offX = pad + (availW - plotW) / 2;
    offY = pad;
  } else {
    plotW = availW;
    plotH = plotW / winRatio;
    offX = pad;
    offY = pad + (availH - plotH) / 2;
  }

  // 浅色网格线
  ctx.strokeStyle = "#e0e0e0";
  ctx.lineWidth = 0.5;
  for (let i = 1; i < 4; i++) {
    const gx = offX + (plotW / 4) * i;
    const gy = offY + (plotH / 4) * i;
    ctx.beginPath();
    ctx.moveTo(gx, offY); ctx.lineTo(gx, offY + plotH);
    ctx.moveTo(offX, gy); ctx.lineTo(offX + plotW, gy);
    ctx.stroke();
  }

  // 先 end（蓝），后 begin（绿 → 上层醒目）
  const RADIUS = 2.5;
  const layers = [
    { cls: "end",   color: "rgba(33, 150, 243, 0.7)"  },
    { cls: "begin", color: "rgba(76, 175, 80, 0.85)"  },
  ];

  for (const layer of layers) {
    ctx.fillStyle = layer.color;
    for (const item of positions) {
      if (item[0] !== layer.cls) continue;
      // 转换为窗口内相对坐标
      const relX = item[1] - winLeft;
      const relY = item[2] - winTop;
      const px = offX + (relX / winW) * plotW;
      const py = offY + (relY / winH) * plotH;
      // 边界裁剪（窗口边缘可能超出截图区域）
      if (px < offX - 2 || px > offX + plotW + 2 || py < offY - 2 || py > offY + plotH + 2) continue;
      ctx.beginPath();
      ctx.arc(px, py, RADIUS, 0, Math.PI * 2);
      ctx.fill();
    }
  }

  // 图例
  const lx = offX + 4, ly = offY + plotH - 14;
  ctx.font = "10px sans-serif";
  ctx.fillStyle = "rgba(76, 175, 80, 0.95)";
  ctx.fillRect(lx, ly - 5, 8, 8);
  ctx.fillStyle = "#555";
  ctx.textAlign = "left";
  ctx.fillText("begin", lx + 12, ly + 3);
  ctx.fillStyle = "rgba(33, 150, 243, 0.95)";
  ctx.fillRect(lx + 54, ly - 5, 8, 8);
  ctx.fillText("end", lx + 66, ly + 3);
}

// ============================================================
//  启动
// ============================================================
window.addEventListener("load", async () => {
  await initApp();
  // 首次填充每个实例的窗口和场景下拉框
  if (_instances && _instances.length > 0) {
    for (const inst of _instances) {
      await populateInstanceWindowSelector(inst.id);
      await populateInstanceSceneSelector(inst.id, inst.template_scene);
    }
  }
});

// 窗口关闭前停止所有 Bot
window.addEventListener("beforeunload", () => {
  if (_pollTimer) clearInterval(_pollTimer);
  eel.emergency_stop();
});
