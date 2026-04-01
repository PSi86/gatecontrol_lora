(function(){
  const $ = (sel, ctx=document) => ctx.querySelector(sel);
  const $$ = (sel, ctx=document) => Array.from(ctx.querySelectorAll(sel));

  const fmt = {
    num: v => (v===null || v===undefined || isNaN(v)) ? "" : String(v),
    hex2: v => ("0" + (Number(v) & 0xFF).toString(16).toUpperCase()).slice(-2),
  };

  function getDeviceTypeId(dev){
    const v = dev.dev_type ?? dev.caps ?? dev.type ?? 0;
    return Number(v || 0);
  }

  function hasWledCapability(dev){
    const caps = Array.isArray(dev.dev_type_caps) ? dev.dev_type_caps : [];
    return caps.includes("WLED");
  }

  function groupMatchesSelection(dev, groupId){
    const gid = Number(groupId);
    if(gid === 255){
      return hasWledCapability(dev);
    }
    return Number(dev.groupId) === gid;
  }

  // Flag bits (must match firmware; kept local for UI only)
  const GC_FLAG_POWER_ON    = 0x01;
  const GC_FLAG_ARM_ON_SYNC = 0x02;
  const GC_FLAG_HAS_BRI     = 0x04;

  const CONFIG_SETTINGS = [
    { bit: 0, label: "MAC filter" },
    { bit: 1, label: "MAC filter persist" },
    { bit: 2, label: "WLAN AP open" },
    { bit: 3, label: "Setting 3" },
    { bit: 4, label: "Setting 4" },
    { bit: 5, label: "Setting 5" },
    { bit: 6, label: "Setting 6" },
    { bit: 7, label: "Setting 7" },
  ];
  const DEFAULT_CONFIG_DISPLAY = [0, 1, 2];

  function loadConfigDisplay(){
    try{
      const raw = localStorage.getItem("gcConfigDisplay");
      if(!raw) return new Set(DEFAULT_CONFIG_DISPLAY);
      const arr = JSON.parse(raw);
      if(!Array.isArray(arr)) return new Set(DEFAULT_CONFIG_DISPLAY);
      const allowed = new Set(CONFIG_SETTINGS.map(s => s.bit));
      return new Set(arr.filter(v => allowed.has(v)));
    }catch{
      return new Set(DEFAULT_CONFIG_DISPLAY);
    }
  }

  function saveConfigDisplay(){
    try{
      localStorage.setItem("gcConfigDisplay", JSON.stringify(Array.from(state.configDisplay)));
    }catch{
      // ignore storage errors
    }
  }

  let state = {
    groups: [],
    devices: [],
    selGroupId: null,
    sortKey: null,
    sortDir: 1,
    selected: new Set(),
    busy: false,
    lastTask: null,
    lastMaster: null,
    fwUploads: { fwId: null, cfgId: null },
    configDisplay: loadConfigDisplay(),
    presets: { files: [], current: "" },
    specials: {},
    specialDevice: null,
    specialTab: null,
  };

  async function apiGet(url){
    const res = await fetch(url, {credentials:"same-origin"});
    const j = await res.json().catch(()=>({ok:false,error:"Bad JSON"}));
    j.__status = res.status;
    return j;
  }
  async function apiPost(url, body){
    const res = await fetch(url, {
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body: JSON.stringify(body||{}),
      credentials:"same-origin"
    });
    const j = await res.json().catch(()=>({ok:false,error:"Bad JSON"}));
    j.__status = res.status;
    return j;
  }
  async function apiUpload(url, formData){
    const res = await fetch(url, {
      method: "POST",
      body: formData,
      credentials: "same-origin"
    });
    const j = await res.json().catch(()=>({ok:false,error:"Bad JSON"}));
    j.__status = res.status;
    return j;
  }



  function setBusy(isBusy){
    state.busy = !!isBusy;
    const disable = state.busy;

    // header action buttons
    $$(".gc-actions button").forEach(b => b.disabled = disable);
    // group creation + bulk
    $("#btnNewGroup").disabled = disable;
    $("#btnBulkSetGroup").disabled = disable;
    const btnCfg = $("#btnNodeCfgSend");
    if(btnCfg) btnCfg.disabled = disable;

    // allow closing modal even when busy
    $("#btnDiscoverStart").disabled = disable;
    updateNodeCfgUi();
    updatePresetsDownloadUi();
    updateSpecialUi();
  }


function updateNodeCfgUi(){
  const btn = $("#btnNodeCfgSend");
  if(!btn) return;
  const n = state.selected.size;
  btn.disabled = state.busy || (n !== 1);
  const hint = $("#nodeCfgHint");
  if(hint){
    hint.textContent = (n === 1) ? "" : "Select exactly one device";
  }
}

function updatePresetsDownloadUi(){
  const btn = $("#btnPresetsDownload");
  if(!btn) return;
  const n = state.selected.size;
  btn.disabled = state.busy || (n !== 1);
  const hint = $("#presetsDownloadHint");
  if(hint){
    hint.textContent = (n === 1) ? "" : "Select exactly one device";
  }
}

function updateSpecialUi(){
  const panel = $("#specialPanel");
  if(!panel) return;
  panel.querySelectorAll("button").forEach(btn => {
    if(btn.classList.contains("special-save") || btn.classList.contains("special-refresh") || btn.classList.contains("special-action")){
      btn.disabled = state.busy || !state.specialDevice;
    }
  });
}

function getSpecialsForDevice(dev){
  const caps = Array.isArray(dev.dev_type_caps) ? dev.dev_type_caps : [];
  return caps
    .map(cap => ({ key: cap, info: state.specials[cap] }))
    .filter(entry => {
      if(!entry.info) return false;
      const opts = Array.isArray(entry.info.options) ? entry.info.options.length > 0 : false;
      const funcs = Array.isArray(entry.info.functions) ? entry.info.functions.length > 0 : false;
      return opts || funcs;
    });
}

function buildSpecialVarInput({varKey, varMeta, uiMeta, dev}){
  const currentVal = dev ? dev[varKey] : undefined;
  const defaultVal = (currentVal !== undefined && currentVal !== null)
    ? currentVal
    : (varMeta && varMeta.min !== undefined ? varMeta.min : 0);
  const options = uiMeta && Array.isArray(uiMeta.options) ? uiMeta.options : null;

  if(options){
    const select = document.createElement("select");
    if(!options.length){
      const opt = document.createElement("option");
      opt.value = "";
      opt.textContent = "No presets available";
      select.appendChild(opt);
      select.disabled = true;
      return { input: select, value: defaultVal };
    }
    options.forEach(optInfo => {
      const opt = document.createElement("option");
      opt.value = String(optInfo.value);
      opt.textContent = String(optInfo.label ?? optInfo.value);
      select.appendChild(opt);
    });
    const desiredNum = Number(defaultVal);
    const match = Number.isFinite(desiredNum)
      ? options.find(optInfo => Number(optInfo.value) === desiredNum)
      : null;
    if(match){
      select.value = String(match.value);
    }else{
      const desired = String(defaultVal ?? options[0].value ?? "");
      if(desired && Array.from(select.options).some(o => o.value === desired)){
        select.value = desired;
      }else{
        select.value = String(options[0].value ?? "");
      }
    }
    return { input: select, value: select.value };
  }

  const input = document.createElement("input");
  input.type = "number";
  if(varMeta && varMeta.min !== undefined) input.min = String(varMeta.min);
  if(varMeta && varMeta.max !== undefined) input.max = String(varMeta.max);
  if(defaultVal !== undefined && defaultVal !== null) input.value = String(defaultVal);
  return { input, value: defaultVal };
}

function renderSpecialTabs(){
  const tabs = $("#specialTabs");
  const panel = $("#specialPanel");
  if(!tabs || !panel) return;
  tabs.innerHTML = "";
  panel.innerHTML = "";
  const dev = state.specialDevice;
  if(!dev) return;
  const specials = getSpecialsForDevice(dev);
  if(specials.length === 0){
    panel.innerHTML = "<p class=\"muted\">No configurable options for this device.</p>";
    return;
  }
  if(!state.specialTab || !specials.some(s => s.key === state.specialTab)){
    state.specialTab = specials[0].key;
  }
  specials.forEach(({ key, info }) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = info.label || key;
    if(key === state.specialTab) btn.classList.add("active");
    btn.addEventListener("click", () => {
      state.specialTab = key;
      renderSpecialTabs();
    });
    tabs.appendChild(btn);
  });

  const active = specials.find(s => s.key === state.specialTab) || specials[0];
  const options = active.info.options || [];
  const functions = active.info.functions || [];
  const optionsByKey = {};
  options.forEach(opt => {
    if(opt && opt.key) optionsByKey[opt.key] = opt;
  });

  if(options.length === 0 && functions.length === 0){
    panel.innerHTML = "<p class=\"muted\">No configurable options for this device.</p>";
    return;
  }
  if(options.length){
    const section = document.createElement("h4");
    section.className = "gc-special-section";
    section.textContent = "Options";
    panel.appendChild(section);
  }

  options.forEach(opt => {
    const row = document.createElement("div");
    row.className = "gc-special-row";
    const label = document.createElement("label");
    label.textContent = opt.label || opt.key;
    const input = document.createElement("input");
    input.type = "number";
    if(opt.min !== undefined) input.min = String(opt.min);
    if(opt.max !== undefined) input.max = String(opt.max);
    const currentVal = dev[opt.key];
    if(currentVal !== undefined && currentVal !== null){
      input.value = String(currentVal);
    }
    const actions = document.createElement("div");
    actions.className = "gc-special-actions";
    const saveBtn = document.createElement("button");
    saveBtn.type = "button";
    saveBtn.className = "special-save";
    saveBtn.textContent = "Save";
    const refreshBtn = document.createElement("button");
    refreshBtn.type = "button";
    refreshBtn.className = "special-refresh";
    refreshBtn.textContent = "Refresh";
    actions.appendChild(saveBtn);
    actions.appendChild(refreshBtn);
    row.appendChild(label);
    row.appendChild(input);
    row.appendChild(actions);
    panel.appendChild(row);

    saveBtn.addEventListener("click", async () => {
      if(!state.specialDevice) return;
      const value = Number(input.value);
      if(!Number.isFinite(value)){
        $("#specialHint").textContent = "Enter a valid number.";
        return;
      }
      $("#specialHint").textContent = `Saving ${opt.label || opt.key}…`;
      const r = await apiPost("/gatecontrol/api/specials/config", {
        mac: state.specialDevice.addr,
        key: opt.key,
        value,
      });
      if(r && r.task){
        try{ updateTask(r.task); }catch{}
      }
      if(r.busy){
        $("#specialHint").textContent = `Busy: ${r.task?.name || "task"} is running`;
        return;
      }
      if(!r.ok){
        $("#specialHint").textContent = r.error || "Failed to save option.";
      }
    });

    refreshBtn.addEventListener("click", async () => {
      $("#specialHint").textContent = "Refreshing is not implemented yet.";
      await apiPost("/gatecontrol/api/specials/get", {
        mac: state.specialDevice?.addr,
        key: opt.key,
      }).catch(()=>{});
    });
  });

  if(functions.length){
    const section = document.createElement("h4");
    section.className = "gc-special-section";
    section.textContent = "Actions";
    panel.appendChild(section);
  }

  functions.forEach(fn => {
    const row = document.createElement("div");
    row.className = "gc-special-fn-row";
    const label = document.createElement("label");
    label.textContent = fn.label || fn.key || "Action";
    const inputsWrap = document.createElement("div");
    inputsWrap.className = "gc-special-inputs";
    const varsList = Array.isArray(fn.vars) ? fn.vars : [];
    const inputMeta = [];
    varsList.forEach(varKey => {
      const varMeta = optionsByKey[varKey] || {};
      const uiMeta = (fn.ui && fn.ui[varKey]) ? fn.ui[varKey] : {};
      const fieldWrap = document.createElement("div");
      fieldWrap.className = "gc-special-input";
      const fieldLabel = document.createElement("span");
      fieldLabel.className = "gc-special-input-label";
      fieldLabel.textContent = varMeta.label || varKey;
      const { input } = buildSpecialVarInput({varKey, varMeta, uiMeta, dev});
      fieldWrap.appendChild(fieldLabel);
      fieldWrap.appendChild(input);
      inputsWrap.appendChild(fieldWrap);
      inputMeta.push({ key: varKey, input, uiMeta });
    });
    const actions = document.createElement("div");
    actions.className = "gc-special-actions";
    const sendBtn = document.createElement("button");
    sendBtn.type = "button";
    sendBtn.className = "special-action";
    sendBtn.textContent = "Send";
    actions.appendChild(sendBtn);
    row.appendChild(label);
    row.appendChild(inputsWrap);
    row.appendChild(actions);
    panel.appendChild(row);

    sendBtn.addEventListener("click", async () => {
      if(!state.specialDevice) return;
      const params = {};
      for(const meta of inputMeta){
        const el = meta.input;
        let value = el.value;
        if(meta.uiMeta && Array.isArray(meta.uiMeta.options) && !value){
          $("#specialHint").textContent = "Select a preset.";
          return;
        }
        const numVal = Number(value);
        if(!Number.isFinite(numVal)){
          $("#specialHint").textContent = `Enter a valid number for ${meta.key}.`;
          return;
        }
        params[meta.key] = numVal;
      }
      $("#specialHint").textContent = `Sending ${fn.label || fn.key}…`;
      const r = await apiPost("/gatecontrol/api/specials/action", {
        mac: state.specialDevice.addr,
        function: fn.key,
        params,
      });
      if(r && r.task){
        try{ updateTask(r.task); }catch{}
      }
      if(r.busy){
        $("#specialHint").textContent = `Busy: ${r.task?.name || "task"} is running`;
        return;
      }
      if(!r.ok){
        $("#specialHint").textContent = r.error || "Failed to send action.";
        return;
      }
      if(!r.task){
        $("#specialHint").textContent = "Action sent.";
      }
    });
  });

  updateSpecialUi();
}

async function openSpecialsDialog(mac){
  const dev = state.devices.find(d => d.addr === mac);
  if(!dev) return;
  state.specialDevice = dev;
  $("#specialHint").textContent = "";
  renderSpecialTabs();
  $("#dlgSpecials").showModal();
}

  function renderConfigDisplayOptions(){
    const holder = $("#configDisplayOptions");
    if(!holder) return;
    holder.innerHTML = "";
    CONFIG_SETTINGS.forEach(setting => {
      const label = document.createElement("label");
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.checked = state.configDisplay.has(setting.bit);
      cb.addEventListener("change", () => {
        if(cb.checked){
          state.configDisplay.add(setting.bit);
        }else{
          state.configDisplay.delete(setting.bit);
        }
        saveConfigDisplay();
        renderTable();
      });
      label.appendChild(cb);
      label.appendChild(document.createTextNode(setting.label));
      holder.appendChild(label);
    });
  }

  function flagsLabel(flags){
    const f = Number(flags) & 0xFF;
    const parts = [];
    if(f & GC_FLAG_POWER_ON) parts.push("PWR");
    if(f & GC_FLAG_ARM_ON_SYNC) parts.push("ARM");
    if(f & GC_FLAG_HAS_BRI) parts.push("BRI");
    const p = parts.length ? parts.join("+") : "-";
    return `0x${fmt.hex2(f)} ${p}`;
  }

  function powerTag(flags){
    return (Number(flags) & GC_FLAG_POWER_ON)
      ? '<span class="tag ok">On</span>'
      : '<span class="tag off">Off</span>';
  }

  // Load initial data (and on refresh)
  async function loadGroups(){
    const g = await apiGet("/gatecontrol/api/groups");
    state.groups = (g.groups||[]);
    if(state.selGroupId===null && state.groups.length>0){ state.selGroupId = state.groups[0].id; }
    renderGroups();
    renderBulkGroup();
  }

  async function loadDevices(){
    const d = await apiGet("/gatecontrol/api/devices");
    state.devices = (d.devices||[]);
    renderTable();
  }

  async function loadSpecials(){
    const s = await apiGet("/gatecontrol/api/specials");
    state.specials = s.specials || {};
    renderTable();
  }

  async function loadAll(){
    await Promise.all([loadGroups(), loadDevices(), loadSpecials()]);
  }

  function renderGroups(){
    const ul = $("#gcGroups");
    ul.innerHTML = "";
    state.groups.forEach(gr => {
      const li = document.createElement("li");
      li.className = (gr.id===state.selGroupId) ? "active" : "";
      li.innerHTML = `<span>${gr.name}</span> <span class="count">${gr.device_count||0}</span>`;
      li.addEventListener("click", () => {
        state.selGroupId = gr.id;
        renderGroups();
        renderTable();
      });
      ul.appendChild(li);
    });
  }

  function renderBulkGroup(){
    const sel = $("#bulkGroup"), sel2 = $("#discoverGroup");
    sel.innerHTML = ""; sel2.innerHTML = "";
    const selectableGroups = state.groups.filter(gr => !gr.static && Number(gr.id) !== 255);
    selectableGroups.forEach(gr => {
      const o = document.createElement("option");
      o.value = gr.id; o.textContent = `${gr.id}: ${gr.name}`;
      sel.appendChild(o);
      const o2 = o.cloneNode(true);
      sel2.appendChild(o2);
    });
    if(state.selGroupId!==null){ sel.value = state.selGroupId; sel2.value = state.selGroupId; }
  }

  function renderTable(){
    const body = $("#gcBody");
    body.innerHTML = "";

    // Apply current filters first
    let rows = state.devices.slice();
    if(state.selGroupId!==null){
      rows = rows.filter(r => groupMatchesSelection(r, state.selGroupId));
    }

    // Prune selection to currently visible devices (within the current filter)
    const present = new Set(rows.map(d => d.addr));
    for (const m of Array.from(state.selected)) {
      if (!present.has(m)) state.selected.delete(m);
    }

    // Apply sorting
    if(state.sortKey){
      const key = state.sortKey, dir = state.sortDir;
      rows.sort((a,b)=>{
        const av = (a[key] ?? ""); const bv = (b[key] ?? "");
        if(av < bv) return -dir;
        if(av > bv) return dir;
        return 0;
      });
    }

    rows.forEach(r => {
      const tr = document.createElement("tr");
      const checked = state.selected.has(r.addr);
      const typeId = getDeviceTypeId(r);
      const typeLabel = r.dev_type_name || r.type_name || (isNaN(typeId) ? "" : String(typeId));
      const specials = getSpecialsForDevice(r);
      const typeCell = (specials.length && typeLabel)
        ? `<button class="gc-link-btn specials-link" data-mac="${r.addr ?? ""}">${typeLabel}</button>`
        : typeLabel;
      const configByte = Number(r.configByte ?? 0) & 0xFF;
      const selectedConfigs = [];
      const tooltipConfigs = [];
      CONFIG_SETTINGS.forEach(setting => {
        const enabled = !!(configByte & (1 << setting.bit));
        const label = `${setting.label}: ${enabled ? "On" : "Off"}`;
        if(state.configDisplay.has(setting.bit)){
          selectedConfigs.push(`<span class="tag ${enabled ? "ok" : "off"}" title="${label}">${setting.label}</span>`);
        }else{
          tooltipConfigs.push(label);
        }
      });
      const configCell = selectedConfigs.length ? `<div class="gc-config-tags">${selectedConfigs.join("")}</div>` : "-";
      const configTooltip = tooltipConfigs.length ? tooltipConfigs.join(" | ") : "";
      tr.innerHTML = `
        <td><input type="checkbox" ${checked?"checked":""} data-mac="${r.addr}"></td>
        <td>${r.name ?? ""}</td>
        <td class="mono">${r.addr ?? ""}</td>
        <td>${r.groupId}</td>
        <td>${powerTag(r.flags)} <span class="mono">${flagsLabel(r.flags)}</span></td>
        <td>${configCell}</td>
        <td>${fmt.num(r.presetId)}</td>
        <td>${fmt.num(r.brightness)}</td>
        <td>${fmt.num(r.voltage_mV)}</td>
        <td>${fmt.num(r.node_rssi)}</td>
        <td>${fmt.num(r.node_snr)}</td>
        <td>${fmt.num(r.host_rssi)}</td>
        <td>${fmt.num(r.host_snr)}</td>
        <td>${r.version ?? ""}</td>
        <td>${typeCell}</td>
        <td>${(r.online===true) ? '<span class="tag online">Online</span>' : (r.online===false) ? '<span class="tag off">Offline</span>' : ''}</td>
      `;
      if(configTooltip){
        tr.title = configTooltip;
      }
      body.appendChild(tr);
    });

    // Selection handlers
    $$("#gcBody input[type=checkbox]").forEach(cb => {
      cb.addEventListener("change", () => {
        const mac = cb.getAttribute("data-mac");
        if(cb.checked) state.selected.add(mac); else state.selected.delete(mac);
        updateNodeCfgUi();
        updatePresetsDownloadUi();
      });
    });

    $$(".specials-link").forEach(btn => {
      btn.addEventListener("click", (e) => {
        const mac = e.currentTarget.getAttribute("data-mac");
        if(mac) openSpecialsDialog(mac);
      });
    });

    // Keep the "Select all" checkbox in sync (for current view)
    const selAll = $("#selAll");
    if(selAll){
      const total = rows.length;
      const selectedNow = Array.from(state.selected).filter(mac => present.has(mac)).length;
      selAll.indeterminate = total > 0 && selectedNow > 0 && selectedNow < total;
      selAll.checked = total > 0 && selectedNow === total;
    }

    updateNodeCfgUi();
    updatePresetsDownloadUi();
}

  // Sorting
  $$("#gcTable thead th").forEach(th => {
    const key = th.getAttribute("data-key");
    if(!key) return;
    th.addEventListener("click", ()=>{
      if(state.sortKey===key) state.sortDir *= -1;
      else { state.sortKey = key; state.sortDir = 1; }
      renderTable();
    });
  });

  // Master/task UI
  function updateMaster(m){
    state.lastMaster = m;
    const pill = $("#masterPill");
    const detail = $("#masterDetail");

    const st = (m && m.state) ? String(m.state) : "IDLE";
    pill.textContent = st;
    pill.classList.remove("idle","tx","rx","err");
    if(st==="TX") pill.classList.add("tx");
    else if(st==="RX") pill.classList.add("rx");
    else if(st==="ERROR") pill.classList.add("err");
    else pill.classList.add("idle");

    const parts = [];
    if(m.tx_pending) parts.push("TX pending");
    if(m.rx_window_open) parts.push(`RX window ${m.rx_window_ms||0}ms`);
    if(m.last_rx_count_delta) parts.push(`ΔRX ${m.last_rx_count_delta}`);
    if(m.last_event) parts.push(`last: ${m.last_event}`);
    if(m.last_error) parts.push(`err: ${m.last_error}`);
    detail.textContent = parts.join(" · ");
    updateNodeCfgUi();
    updatePresetsDownloadUi();

  }

  function updateTask(t){
    state.lastTask = t;
    const el = $("#taskDetail");
    if(!t){
      el.textContent = "";
      setBusy(false);
      return;
    }
    const st = String(t.state||"");
    const name = String(t.name||"task");
    if(st === "running"){
      setBusy(true);
      const meta = t.meta || {};
      const mparts = [];
      if(meta.targetGroupId!==undefined && meta.targetGroupId!==null) mparts.push(`gid ${meta.targetGroupId}`);
      if(meta.selectionCount) mparts.push(`sel ${meta.selectionCount}`);
      if(meta.groupId!==undefined && meta.groupId!==null) mparts.push(`gid ${meta.groupId}`);

      if(name==="fwupdate"){
        if(meta.index!==undefined && meta.total!==undefined) mparts.push(`${meta.index}/${meta.total}`);
        if(meta.stage) mparts.push(String(meta.stage));
        if(meta.attempt && meta.retries) mparts.push(`try ${meta.attempt}/${meta.retries}`);
        if(meta.message) mparts.push(String(meta.message));
      }

      if(name==="presets_download"){
        if(meta.step !== undefined && meta.steps !== undefined) mparts.push(`Step ${meta.step} of ${meta.steps}`);
        if(meta.message) mparts.push(String(meta.message));
        const hintEl = $("#presetsHint");
        if(hintEl){
          const msg = [];
          if(meta.step !== undefined && meta.steps !== undefined){
            msg.push(`Step ${meta.step} of ${meta.steps}`);
          }
          if(meta.message) msg.push(String(meta.message));
          hintEl.textContent = msg.join(": ");
        }
      }

      const p = [
        `${name}…`,
        mparts.length ? `(${mparts.join(", ")})` : "",
        `replies ${t.rx_replies||0}`,
        `windows ${t.rx_window_events||0}`,
        `Δ ${t.rx_count_delta_total||0}`,
      ].filter(Boolean).join(" ");
      el.textContent = p;
    } else {
      setBusy(false);
      const dur = (t.started_ts && t.ended_ts) ? Math.max(0, (t.ended_ts - t.started_ts)) : null;
      const tail = (dur!==null) ? `(${dur.toFixed(1)}s)` : "";
      const res = t.result ? JSON.stringify(t.result) : "";
      const err = t.last_error ? `err: ${t.last_error}` : "";
      el.textContent = [ `${name} ${st}`, tail, err || res ].filter(Boolean).join(" · ");
    }

  // Discover modal helper
  if(name==="discover"){
    const r = t.result || {};
    if(r && typeof r === "object" && $("#discoverResult")){
      if(st==="done") $("#discoverResult").textContent = `Found: ${r.found ?? "?"}`;
      else if(st==="error") $("#discoverResult").textContent = `Error: ${t.last_error||"unknown"}`;
    }
  }

  // Firmware update modal helper
  if(name==="fwupdate"){
    const r = t.result || {};
    const hintEl = $("#fwHint");
    if(hintEl){
      if(st==="done"){
        const total = (r.devices && r.devices.length) ? r.devices.length : "";
        const errs = (r.errors && r.errors.length) ? r.errors.length : 0;
        hintEl.textContent = `Done. ${total ? `devices ${total}, ` : ""}errors ${errs}`;
      } else if(st==="error"){
        hintEl.textContent = `Error: ${t.last_error || "unknown"}`;
      }
    }
  }

  if(name==="presets_download"){
    const hintEl = $("#presetsHint");
    if(hintEl){
      if(st==="done"){
        const fname = t.result?.file?.name || "";
        hintEl.textContent = fname ? `Downloaded ${fname}` : "Preset download completed.";
        (async ()=>{
          try{
            await loadPresetsList($("#presetsSelect"));
            if(fname){
              const selectEl = $("#presetsSelect");
              if(selectEl && Array.from(selectEl.options).some(opt => opt.value === fname)){
                selectEl.value = fname;
              }
            }
            const currentLabel = state.presets.current ? `Current: ${state.presets.current}` : "Current: none";
            $("#presetsCurrentInfo").textContent = currentLabel;
          }catch(e){
            console.error("Failed to refresh presets list", e);
          }
        })();
      } else if(st==="error"){
        hintEl.textContent = `Error: ${t.last_error || "unknown"}`;
      } else if(st==="running"){
        // running updates handled above
      }
    }
  }

  if(name==="special_config"){
    const hintEl = $("#specialHint");
    if(hintEl){
      if(st==="done"){
        hintEl.textContent = "Option saved.";
      } else if(st==="error"){
        hintEl.textContent = `Error: ${t.last_error || "unknown"}`;
      } else if(st==="running"){
        const meta = t.meta || {};
        hintEl.textContent = meta.message ? String(meta.message) : "Saving option…";
      }
    }
  }

  if(name==="special_action"){
    const hintEl = $("#specialHint");
    if(hintEl){
      if(st==="done"){
        hintEl.textContent = "Action sent.";
      } else if(st==="error"){
        hintEl.textContent = `Error: ${t.last_error || "unknown"}`;
      } else if(st==="running"){
        const meta = t.meta || {};
        hintEl.textContent = meta.message ? String(meta.message) : "Sending action…";
      }
    }
  }

  }

  // Firmware Update modal
  function fwDialogCounts(){
    $("#fwSelCount").textContent = String(state.selected.size || 0);
    const filtered = (state.selGroupId === null || state.selGroupId === undefined)
      ? state.devices.length
      : state.devices.filter(d => groupMatchesSelection(d, state.selGroupId)).length;
    $("#fwFilterCount").textContent = String(filtered || 0);
    $("#fwAllCount").textContent = String(state.devices.length || 0);
  }

  function fwGetTargetValue(){
    const el = document.querySelector('input[name="fwTarget"]:checked');
    return el ? String(el.value) : "selected";
  }

  function fwMacsForTarget(){
    const t = fwGetTargetValue();
    if(t === "selected"){
      return Array.from(state.selected);
    }
    if(t === "filtered"){
      if(state.selGroupId === null || state.selGroupId === undefined){
        return state.devices.map(d => d.addr).filter(Boolean);
      }
      return state.devices.filter(d => groupMatchesSelection(d, state.selGroupId)).map(d => d.addr).filter(Boolean);
    }
    return state.devices.map(d => d.addr).filter(Boolean);
  }

  function fwResetUploadsUi(){
    state.fwUploads = { fwId: null, cfgId: null };
    $("#fwBinInfo").textContent = "";
    $("#fwCfgInfo").textContent = "";
    $("#fwHint").textContent = "";
    $("#fwBin").value = "";
    $("#fwCfg").value = "";
  }

  async function loadWifiIfaces(selectEl){
    const sel = selectEl;
    if(!sel) return;
    sel.innerHTML = "";
    let data = null;
    try{
      data = await apiGet("/gatecontrol/api/wifi/interfaces");
    }catch{
      data = null;
    }
    const ifaces = (data && data.ifaces && data.ifaces.length) ? data.ifaces : ["wlan0"];
    ifaces.forEach((name)=>{
      const opt = document.createElement("option");
      opt.value = name;
      opt.textContent = name;
      sel.appendChild(opt);
    });
    const preferred = ifaces.includes("wlan0") ? "wlan0" : (ifaces[0] || "");
    if(preferred) sel.value = preferred;
  }

  async function fwUpload(kind, fileInputEl, infoEl){
    const f = fileInputEl.files && fileInputEl.files[0];
    if(!f){
      alert("Please choose a file first.");
      return null;
    }
    const fd = new FormData();
    fd.append("file", f);
    fd.append("kind", kind);
    const r = await apiUpload("/gatecontrol/api/fw/upload", fd);
    if(!r.ok){
      alert(r.error || "Upload failed");
      return null;
    }
    const s = `${r.file.name} (${r.file.size} B) sha256 ${String(r.file.sha256||"").slice(0,8)}…`;
    infoEl.textContent = s;
    return r.file.id;
  }

  function formatPresetOption(preset){
    const ts = preset.saved_ts ? new Date(preset.saved_ts * 1000) : null;
    const tsLabel = ts ? ts.toLocaleString() : "";
    return tsLabel ? `${preset.name} (${tsLabel})` : preset.name;
  }

  function populatePresetsSelect(selectEl, files, current){
    selectEl.innerHTML = "";
    if(!files || !files.length){
      const opt = document.createElement("option");
      opt.value = "";
      opt.textContent = "No presets.json found";
      selectEl.appendChild(opt);
      return "";
    }
    files.forEach(preset => {
      const opt = document.createElement("option");
      opt.value = preset.name;
      opt.textContent = formatPresetOption(preset);
      selectEl.appendChild(opt);
    });
    const available = new Set(files.map(p => p.name));
    const desired = (current && available.has(current)) ? current : files[0].name;
    selectEl.value = desired;
    return desired;
  }

  async function loadPresetsList(selectEl){
    const data = await apiGet("/gatecontrol/api/presets/list");
    const files = data.files || [];
    state.presets.files = files;
    state.presets.current = data.current || "";
    return populatePresetsSelect(selectEl, files, state.presets.current);
  }

  $("#btnFwUpdate").addEventListener("click", async ()=>{
    try{
    fwDialogCounts();
    fwResetUploadsUi();
    loadWifiIfaces($("#fwWifiIface"));
    await loadPresetsList($("#fwPresetsSelect"));
    $("#fwPresetsHint").textContent = state.presets.current ? `Current: ${state.presets.current}` : "Current: none";

    // Enable/disable optional controls based on checkboxes
    $("#fwBin").disabled = !$("#fwDoFirmware").checked;
    $("#btnFwUpload").disabled = !$("#fwDoFirmware").checked;
    $("#fwPresetsSelect").disabled = !$("#fwDoPresets").checked;
    $("#fwCfg").disabled = !$("#fwDoCfg").checked;
    $("#btnFwUploadCfg").disabled = !$("#fwDoCfg").checked;

    $("#dlgFwUpdate").showModal();
    } catch(e){
      console.error(e);
      alert("Firmware dialog failed to open. Check console for details.");
    }
  });

  $("#fwDoFirmware").addEventListener("change", ()=>{
    const on = $("#fwDoFirmware").checked;
    $("#fwBin").disabled = !on;
    $("#btnFwUpload").disabled = !on;
    if(!on){
      state.fwUploads.fwId = null;
      $("#fwBinInfo").textContent = "";
      $("#fwBin").value = "";
    }
  });

  $("#fwDoPresets").addEventListener("change", ()=>{
    const on = $("#fwDoPresets").checked;
    $("#fwPresetsSelect").disabled = !on;
  });

  $("#fwDoCfg").addEventListener("change", ()=>{
    const on = $("#fwDoCfg").checked;
    $("#fwCfg").disabled = !on;
    $("#btnFwUploadCfg").disabled = !on;
    if(!on){
      state.fwUploads.cfgId = null;
      $("#fwCfgInfo").textContent = "";
      $("#fwCfg").value = "";
    }
  });

  $("#btnFwUpload").addEventListener("click", async ()=>{
    const id = await fwUpload("firmware", $("#fwBin"), $("#fwBinInfo"));
    if(id) state.fwUploads.fwId = id;
  });

  $("#btnFwUploadCfg").addEventListener("click", async ()=>{
    const id = await fwUpload("cfg", $("#fwCfg"), $("#fwCfgInfo"));
    if(id) state.fwUploads.cfgId = id;
  });

  $("#btnFwStart").addEventListener("click", async ()=>{
    const macs = fwMacsForTarget();
    if(!macs.length){
      alert("No target devices (selection/filter empty).");
      return;
    }
    const doFirmware = $("#fwDoFirmware").checked;
    const doPresets = $("#fwDoPresets").checked;
    const doCfg = $("#fwDoCfg").checked;
    if(!doFirmware && !doPresets && !doCfg){
      alert("Select at least one operation (firmware, presets, or cfg).");
      return;
    }

    const baseUrl = ($("#fwBaseUrl").value || "").trim() || "http://4.3.2.1";
    const retries = Number($("#fwRetries").value || 3) || 3;

    const wifiSsid = ($("#fwWifiSsid")?.value || "WLED-AP").trim();
    const wifiIface = ($("#fwWifiIface")?.value || "wlan0").trim();
    const wifiConnName = ($("#fwWifiConnName")?.value || "gatecontrol-wled-ap").trim();
    const wifiTimeoutS = Number($("#fwWifiTimeoutS")?.value || 35) || 35;

    const hostWifiEnable = !!($("#fwHostWifiEnable")?.checked);
    const hostWifiRestore = !!($("#fwHostWifiRestore")?.checked);

    const body = {
      macs,
      baseUrl,
      retries,
      hostWifiEnable,
      hostWifiRestore,
      doFirmware,
      doPresets,
      doCfg,
      wifi: { connName: wifiConnName, ssid: wifiSsid, iface: wifiIface, timeoutS: wifiTimeoutS }
    };

    if(doFirmware){
      if(!state.fwUploads.fwId){
        alert("Firmware enabled but firmware is not uploaded yet.");
        return;
      }
      body.fwId = state.fwUploads.fwId;
    }

    if(doPresets){
      const presetsName = ($("#fwPresetsSelect").value || "").trim();
      if(!presetsName){
        alert("Presets enabled but no presets.json is available.");
        return;
      }
      body.presetsName = presetsName;
    }
    if(doCfg){
      if(!state.fwUploads.cfgId){
        alert("cfg enabled but cfg.json is not uploaded yet.");
        return;
      }
      body.cfgId = state.fwUploads.cfgId;
    }

    const r = await apiPost("/gatecontrol/api/fw/start", body);
    if(r.busy){
      alert(`Busy: ${r.task?.name || "task"} is running`);
      return;
    }
    if(!r.ok){
      alert(r.error || "Failed to start firmware update.");
      return;
    }

    $("#dlgFwUpdate").close();
  });

  // SSE connection
  function connectEvents(){
    try{
      const es = new EventSource("/gatecontrol/api/events", {withCredentials:true});
      es.addEventListener("master", (e)=>{ try{ updateMaster(JSON.parse(e.data)); }catch{} });
      es.addEventListener("task", (e)=>{ try{ updateTask(JSON.parse(e.data)); }catch{} });
      es.addEventListener("refresh", async (e)=>{
        try{
          const p = JSON.parse(e.data);
          const what = (p && p.what) ? p.what : ["groups","devices"];
          if(what.includes("groups")) await loadGroups();
          if(what.includes("devices")) await loadDevices();
        }catch{
          await loadAll();
        }
      });
      es.onerror = () => {
        // If SSE fails, do a one-shot fetch so UI isn't empty
        apiGet("/gatecontrol/api/master").then(r=>{
          if(r.master) updateMaster(r.master);
          if(r.task) updateTask(r.task);
        }).catch(()=>{});
      };
    }catch(e){
      console.warn("SSE not available", e);
    }
  }

  // Buttons
  $("#btnSave").addEventListener("click", async ()=>{
    const r = await apiPost("/gatecontrol/api/save",{});
    if(r.busy) return;
  });

  $("#btnReload").addEventListener("click", async ()=>{
    const r = await apiPost("/gatecontrol/api/reload",{});
    if(!r.busy) await loadAll();
  });

  $("#btnForce").addEventListener("click", async ()=>{
    const r = await apiPost("/gatecontrol/api/groups/force",{});
    if(r.busy) return;
  });

  const dlgSpecials = $("#dlgSpecials");
  if(dlgSpecials){
    dlgSpecials.addEventListener("close", ()=>{
      state.specialDevice = null;
      state.specialTab = null;
      $("#specialHint").textContent = "";
      $("#specialTabs").innerHTML = "";
      $("#specialPanel").innerHTML = "";
    });
  }

  const dlgPresets = $("#dlgPresets");
  $("#btnWledPresets").addEventListener("click", async ()=>{
    $("#presetsHint").textContent = "";
    $("#presetsUploadInfo").textContent = "";
    $("#presetsDownloadHint").textContent = state.selected.size === 1 ? "" : "Select exactly one device";
    loadWifiIfaces($("#presetsWifiIface"));
    const selected = await loadPresetsList($("#presetsSelect"));
    const currentLabel = state.presets.current ? `Current: ${state.presets.current}` : "Current: none";
    $("#presetsCurrentInfo").textContent = currentLabel;
    dlgPresets.showModal();
  });

  $("#btnPresetsSave").addEventListener("click", async ()=>{
    const selected = ($("#presetsSelect").value || "").trim();
    if(!selected){
      $("#presetsHint").textContent = "No presets.json available to select.";
      return;
    }
    if(selected === state.presets.current){
      dlgPresets.close();
      return;
    }
    const r = await apiPost("/gatecontrol/api/presets/select", {name: selected});
    if(!r.ok){
      $("#presetsHint").textContent = r.error || "Failed to apply presets.";
      return;
    }
    state.presets.current = r.current || selected;
    $("#presetsCurrentInfo").textContent = `Current: ${state.presets.current}`;
    $("#presetsHint").textContent = `Applied ${state.presets.current}`;
    dlgPresets.close();
  });

  $("#btnPresetsUpload").addEventListener("click", async ()=>{
    const fileEl = $("#presetsUploadFile");
    const infoEl = $("#presetsUploadInfo");
    if(!fileEl || !infoEl) return;
    const file = fileEl.files && fileEl.files[0];
    if(!file){
      alert("Select a presets.json file first.");
      return;
    }
    const formData = new FormData();
    formData.append("file", file, file.name);
    const r = await apiUpload("/gatecontrol/api/presets/upload", formData);
    if(r.ok){
      infoEl.textContent = `Uploaded ${r.file?.name || file.name}`;
      if(r.files){
        state.presets.files = r.files;
        const selected = populatePresetsSelect($("#presetsSelect"), r.files, r.file?.name || "");
        $("#presetsCurrentInfo").textContent = state.presets.current ? `Current: ${state.presets.current}` : "Current: none";
      }
    } else {
      infoEl.textContent = r.error || "Upload failed.";
    }
  });

  $("#btnPresetsDownload").addEventListener("click", async ()=>{
    const macs = Array.from(state.selected);
    if(macs.length !== 1){
      alert("Select exactly one device to download presets.");
      return;
    }
    const baseUrl = ($("#presetsBaseUrl").value || "").trim() || "http://4.3.2.1";
    const wifiSsid = ($("#presetsWifiSsid")?.value || "WLED-AP").trim();
    const wifiIface = ($("#presetsWifiIface")?.value || "wlan0").trim();
    const wifiConnName = ($("#presetsWifiConnName")?.value || "gatecontrol-wled-ap").trim();
    const wifiTimeoutS = Number($("#presetsWifiTimeoutS")?.value || 35) || 35;
    const hostWifiEnable = !!($("#presetsHostWifiEnable")?.checked);
    const hostWifiRestore = !!($("#presetsHostWifiRestore")?.checked);

    $("#presetsHint").textContent = "Starting presets download…";
    const r = await apiPost("/gatecontrol/api/presets/download", {
      mac: macs[0],
      baseUrl,
      hostWifiEnable,
      hostWifiRestore,
      wifi: { connName: wifiConnName, ssid: wifiSsid, iface: wifiIface, timeoutS: wifiTimeoutS }
    });
    if(r.busy){
      alert(`Busy: ${r.task?.name || "task"} is running`);
      return;
    }
    if(!r.ok){
      $("#presetsHint").textContent = r.error || "Failed to start presets download.";
      return;
    }
  });

  $("#btnStatusSel").addEventListener("click", async ()=>{
    const macs = Array.from(state.selected);
    if(macs.length===0) return;
    const r = await apiPost("/gatecontrol/api/status", {selection: macs});
    if(r.busy){
      alert(`Busy: ${r.task?.name || "task"} is running`);
    }
  });

  $("#btnStatusAll").addEventListener("click", async ()=>{
    const r = await apiPost("/gatecontrol/api/status", {});
    if(r.busy){
      alert(`Busy: ${r.task?.name || "task"} is running`);
    }
  });

  $("#btnBulkSetGroup").addEventListener("click", async ()=>{
    const macs = Array.from(state.selected);
    const gid = Number($("#bulkGroup").value);
    if(macs.length===0) return;
    const r = await apiPost("/gatecontrol/api/devices/update-meta", {macs, groupId: gid});
    if(!r.busy) { /* refresh happens via SSE */ }
  });

  // Discover modal

// Node CONFIG (unicast only, requires exactly one selection)
$("#btnNodeCfgSend").addEventListener("click", async ()=>{
  const macs = Array.from(state.selected);
  if(macs.length !== 1){
    alert("Select exactly one device for CONFIG commands.");
    return;
  }
  const sel = ($("#nodeCfgCmd").value || "").trim();
  const parts = sel.split(":");
  const option = Number(parts[0] || 0);
  const data0 = Number(parts[1] || 0);
  const data1 = Number(parts[2] || 0);
  const data2 = Number(parts[3] || 0);
  const data3 = Number(parts[4] || 0);

  if(option === 0x80){
    if(!confirm("Forget learned Master MAC on the selected node?")) return;
  } else if(option === 0x81){
    if(!confirm("Reboot the selected node now?")) return;
  }

  const r = await apiPost("/gatecontrol/api/config", {mac: macs[0], option, data0, data1, data2, data3});
  if(r.busy){
    alert(`Busy: ${r.task?.name || "task"} is running`);
  }
});

  const dlg = $("#dlgDiscover");
  $("#btnDiscover").addEventListener("click", ()=>{
    $("#discoverResult").textContent = "";
    dlg.showModal();
  });

  $("#btnDiscoverStart").addEventListener("click", async (e)=>{
    e.preventDefault();
    const targetGroupId = Number($("#discoverGroup").value);
    const newGroupName = ($("#discoverNewGroup").value || "").trim() || null;

    $("#discoverResult").textContent = "Running…";
    const r = await apiPost("/gatecontrol/api/discover", {targetGroupId, newGroupName});

    // If the API uses "busy" both for "already busy" and "task started",
    // treat an incoming discover task as success and rely on SSE/task polling for progress.
    if(r && r.task){
      try{ updateTask(r.task); }catch{}
    }

    if(r && r.busy){
      const tn = r.task?.name || "";
      const ts = r.task?.state || "";
      if(tn && tn !== "discover"){
        $("#discoverResult").textContent = `Busy: ${tn} is running`;
      } else if(ts && ts !== "running"){
        $("#discoverResult").textContent = `Busy: ${tn || "task"} is ${ts}`;
      }
      // else: keep "Running…" (normal discover start)
    }

    // Best-effort immediate sync in case SSE isn't connected yet
    apiGet("/gatecontrol/api/master").then(m=>{
      if(m.master) updateMaster(m.master);
      if(m.task) updateTask(m.task);
    }).catch(()=>{});
  });

  // Select all
  $("#selAll").addEventListener("change", (e)=>{
    const c = e.target.checked;
    state.selected.clear();
    $$("#gcBody input[type=checkbox]").forEach(cb => {
      cb.checked = c;
      if(c) state.selected.add(cb.getAttribute("data-mac"));
    });
    updateNodeCfgUi();
    updatePresetsDownloadUi();
  });

  // New group
  $("#btnNewGroup").addEventListener("click", async ()=>{
    const name = prompt("New group name:");
    if(!name) return;
    const r = await apiPost("/gatecontrol/api/groups/create", {name});
    if(r.busy){
      alert(`Busy: ${r.task?.name || "task"} is running`);
    }
  });

  // Startup
  (async ()=>{
    // Connect SSE first so we still get feedback even if initial REST loads fail
    connectEvents();
    renderConfigDisplayOptions();

    try{
      await loadAll();
    }catch(e){
      console.error("Initial load failed", e);
    }

    // One-shot sync of master/task in case SSE is delayed or blocked by proxies
    try{
      const m = await apiGet("/gatecontrol/api/master");
      if(m.master) updateMaster(m.master);
      if(m.task) updateTask(m.task);
    }catch(e){
      console.error("Master sync failed", e);
    }
  })().catch(console.error);
})();
