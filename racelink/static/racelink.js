(function(){
  const $ = (sel, ctx=document) => ctx.querySelector(sel);
  const $$ = (sel, ctx=document) => Array.from(ctx.querySelectorAll(sel));
  const RL_BASE_PATH = (document.body?.dataset?.rlBasePath || "/racelink").replace(/\/$/, "");

  function withBasePath(path){
    if(!path) return RL_BASE_PATH || "/";
    if(/^https?:\/\//i.test(path)) return path;
    let normalized = String(path).trim();
    if(normalized === "/racelink"){
      normalized = "/";
    } else if(normalized.startsWith("/racelink/")){
      normalized = normalized.slice("/racelink".length);
    }
    if(!normalized.startsWith("/")){
      normalized = `/${normalized}`;
    }
    return RL_BASE_PATH ? `${RL_BASE_PATH}${normalized}` : normalized;
  }

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
  const RL_FLAG_POWER_ON    = 0x01;
  const RL_FLAG_ARM_ON_SYNC = 0x02;
  const RL_FLAG_HAS_BRI     = 0x04;

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
      const raw = localStorage.getItem("rlConfigDisplay");
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
      localStorage.setItem("rlConfigDisplay", JSON.stringify(Array.from(state.configDisplay)));
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
    const res = await fetch(withBasePath(url), {credentials:"same-origin"});
    const j = await res.json().catch(()=>({ok:false,error:"Bad JSON"}));
    j.__status = res.status;
    return j;
  }
  async function apiPost(url, body){
    const res = await fetch(withBasePath(url), {
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
    const res = await fetch(withBasePath(url), {
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
    $$(".rl-actions button").forEach(b => b.disabled = disable);
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
  const widget = uiMeta && typeof uiMeta.widget === "string" ? uiMeta.widget : null;
  const options = uiMeta && Array.isArray(uiMeta.options) ? uiMeta.options : null;

  // ---- slider (range + live number display) ----
  if(widget === "slider"){
    const min = (uiMeta && uiMeta.min !== undefined) ? Number(uiMeta.min) : 0;
    const max = (uiMeta && uiMeta.max !== undefined) ? Number(uiMeta.max) : 255;
    // A13: Default = 50% of the range (e.g. 128 for 0..255, 16 for 0..31) when
    // no device-cached value is available.
    const defaultVal = (currentVal !== undefined && currentVal !== null)
      ? Number(currentVal)
      : Math.round((min + max) / 2);
    const wrap = document.createElement("div");
    wrap.className = "rl-slider-wrap";
    const input = document.createElement("input");
    input.type = "range";
    input.min = String(min);
    input.max = String(max);
    input.step = "1";
    input.value = String(defaultVal);
    const readout = document.createElement("span");
    readout.className = "rl-slider-value";
    readout.textContent = String(defaultVal);
    input.addEventListener("input", () => { readout.textContent = input.value; });
    wrap.appendChild(input);
    wrap.appendChild(readout);
    // Expose the slider's element as the interactive node, but return the wrap
    // as the DOM to insert. Submit handler reads the widget type and queries
    // wrap.querySelector("input[type=range]") if needed; we stash the input on
    // the wrapper for convenience.
    wrap.rlInput = input;
    return { input: wrap, value: defaultVal, widget };
  }

  // ---- toggle (checkbox) ----
  if(widget === "toggle"){
    const defaultBool = Boolean(currentVal);
    const wrap = document.createElement("label");
    wrap.className = "rl-toggle-wrap";
    const input = document.createElement("input");
    input.type = "checkbox";
    input.checked = defaultBool;
    wrap.appendChild(input);
    wrap.rlInput = input;
    return { input: wrap, value: defaultBool, widget };
  }

  // ---- color (native color picker) ----
  if(widget === "color"){
    const input = document.createElement("input");
    input.type = "color";
    input.value = (typeof currentVal === "string" && /^#[0-9a-fA-F]{6}$/.test(currentVal))
      ? currentVal
      : "#000000";
    return { input, value: input.value, widget };
  }

  const defaultVal = (currentVal !== undefined && currentVal !== null)
    ? currentVal
    : (varMeta && varMeta.min !== undefined ? varMeta.min : 0);

  if(widget === "select" || options){
    const select = document.createElement("select");
    const opts = options || [];
    if(!opts.length){
      const opt = document.createElement("option");
      opt.value = "";
      opt.textContent = "No presets available";
      select.appendChild(opt);
      select.disabled = true;
      return { input: select, value: defaultVal, widget: "select" };
    }
    opts.forEach(optInfo => {
      const opt = document.createElement("option");
      opt.value = String(optInfo.value);
      opt.textContent = String(optInfo.label ?? optInfo.value);
      select.appendChild(opt);
    });
    const desiredNum = Number(defaultVal);
    const match = Number.isFinite(desiredNum)
      ? opts.find(optInfo => Number(optInfo.value) === desiredNum)
      : null;
    if(match){
      select.value = String(match.value);
    }else{
      const desired = String(defaultVal ?? opts[0].value ?? "");
      if(desired && Array.from(select.options).some(o => o.value === desired)){
        select.value = desired;
      }else{
        select.value = String(opts[0].value ?? "");
      }
    }
    return { input: select, value: select.value, widget: "select" };
  }

  // Default: plain number input
  const input = document.createElement("input");
  input.type = "number";
  if(varMeta && varMeta.min !== undefined) input.min = String(varMeta.min);
  if(varMeta && varMeta.max !== undefined) input.max = String(varMeta.max);
  if(defaultVal !== undefined && defaultVal !== null) input.value = String(defaultVal);
  return { input, value: defaultVal, widget: "number" };
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
    section.className = "rl-special-section";
    section.textContent = "Options";
    panel.appendChild(section);
  }

  options.forEach(opt => {
    const row = document.createElement("div");
    row.className = "rl-special-row";
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
    actions.className = "rl-special-actions";
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
      const r = await apiPost("/racelink/api/specials/config", {
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
      await apiPost("/racelink/api/specials/get", {
        mac: state.specialDevice?.addr,
        key: opt.key,
      }).catch(()=>{});
    });
  });

  if(functions.length){
    const section = document.createElement("h4");
    section.className = "rl-special-section";
    section.textContent = "Actions";
    panel.appendChild(section);
  }

  functions.forEach(fn => {
    const row = document.createElement("div");
    row.className = "rl-special-fn-row";
    const label = document.createElement("label");
    label.textContent = fn.label || fn.key || "Action";
    const inputsWrap = document.createElement("div");
    inputsWrap.className = "rl-special-inputs";
    const varsList = Array.isArray(fn.vars) ? fn.vars : [];
    const inputMeta = [];
    varsList.forEach(varKey => {
      const varMeta = optionsByKey[varKey] || {};
      const uiMeta = (fn.ui && fn.ui[varKey]) ? fn.ui[varKey] : {};
      const fieldWrap = document.createElement("div");
      fieldWrap.className = "rl-special-input";
      fieldWrap.dataset.field = varKey;
      const fieldLabel = document.createElement("span");
      fieldLabel.className = "rl-special-input-label";
      const defaultLabelText = varMeta.label || varKey;
      fieldLabel.textContent = defaultLabelText;
      fieldLabel.dataset.defaultLabel = defaultLabelText;
      const { input, widget } = buildSpecialVarInput({varKey, varMeta, uiMeta, dev});
      fieldWrap.appendChild(fieldLabel);
      fieldWrap.appendChild(input);
      inputsWrap.appendChild(fieldWrap);
      inputMeta.push({ key: varKey, input, uiMeta, widget, wrap: fieldWrap, labelEl: fieldLabel });
    });

    // A12: dynamische effect-spezifische UI für wled_control_advanced.
    // Wenn die Action ein "mode"-Select mit slots-Metadaten (pro Option) enthält,
    // passen Labels und Sichtbarkeit aller abhängigen Felder am Mode-Wechsel an.
    const modeMeta = inputMeta.find(m => m.key === "mode");
    const modeOptions = modeMeta && modeMeta.uiMeta && Array.isArray(modeMeta.uiMeta.options)
      ? modeMeta.uiMeta.options
      : null;
    const hasSlots = modeOptions && modeOptions.some(o => o && o.slots);
    if(modeMeta && hasSlots){
      const optionByValue = new Map(modeOptions.map(o => [String(o.value), o]));
      const applyEffectSlots = () => {
        const selected = optionByValue.get(String(modeMeta.input.value));
        const slots = selected && selected.slots ? selected.slots : null;
        for(const m of inputMeta){
          if(m.key === "mode") continue; // Mode-Select bleibt immer sichtbar
          // Slot fehlt / unbekannt -> konservativer Fallback: Feld anzeigen, generisches Label.
          const slot = slots ? slots[m.key] : null;
          const used = slot ? Boolean(slot.used) : true;
          m.wrap.style.display = used ? "" : "none";
          if(m.labelEl){
            const custom = slot && typeof slot.label === "string" && slot.label ? slot.label : null;
            m.labelEl.textContent = custom || m.labelEl.dataset.defaultLabel || m.key;
          }
        }
      };
      modeMeta.input.addEventListener("change", applyEffectSlots);
      applyEffectSlots();
    }
    const actions = document.createElement("div");
    actions.className = "rl-special-actions";
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
        // A12: ausgeblendete Felder (effect-spezifisch unused) nicht mitschicken.
        // Das Backend belässt sie dann aus fieldMask/extMask raus -> der WLED-Node
        // überschreibt nichts Irrelevantes.
        if(meta.wrap && meta.wrap.style.display === "none"){
          continue;
        }
        const widget = meta.widget;

        if(widget === "toggle"){
          const cb = meta.input.rlInput || meta.input;
          params[meta.key] = Boolean(cb.checked);
          continue;
        }

        if(widget === "slider"){
          const sl = meta.input.rlInput || meta.input;
          const n = Number(sl.value);
          if(!Number.isFinite(n)){
            $("#specialHint").textContent = `Enter a valid number for ${meta.key}.`;
            return;
          }
          params[meta.key] = n;
          continue;
        }

        if(widget === "color"){
          const hex = String(meta.input.value || "#000000").replace(/^#/, "");
          if(!/^[0-9a-fA-F]{6}$/.test(hex)){
            $("#specialHint").textContent = `Enter a valid color for ${meta.key}.`;
            return;
          }
          params[meta.key] = {
            r: parseInt(hex.slice(0, 2), 16),
            g: parseInt(hex.slice(2, 4), 16),
            b: parseInt(hex.slice(4, 6), 16),
          };
          continue;
        }

        // select / number / legacy fallback
        const el = meta.input;
        const value = el.value;
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
      const r = await apiPost("/racelink/api/specials/action", {
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
    if(f & RL_FLAG_POWER_ON) parts.push("PWR");
    if(f & RL_FLAG_ARM_ON_SYNC) parts.push("ARM");
    if(f & RL_FLAG_HAS_BRI) parts.push("BRI");
    const p = parts.length ? parts.join("+") : "-";
    return `0x${fmt.hex2(f)} ${p}`;
  }

  function powerTag(flags){
    return (Number(flags) & RL_FLAG_POWER_ON)
      ? '<span class="tag ok">On</span>'
      : '<span class="tag off">Off</span>';
  }

  // Load initial data (and on refresh)
  async function loadGroups(){
    const g = await apiGet("/racelink/api/groups");
    state.groups = (g.groups||[]);
    if(state.selGroupId===null && state.groups.length>0){ state.selGroupId = state.groups[0].id; }
    renderGroups();
    renderBulkGroup();
  }

  async function loadDevices(){
    const d = await apiGet("/racelink/api/devices");
    state.devices = (d.devices||[]);
    renderTable();
  }

  async function loadSpecials(){
    const s = await apiGet("/racelink/api/specials");
    state.specials = s.specials || {};
    renderTable();
  }

  async function loadAll(){
    await Promise.all([loadGroups(), loadDevices(), loadSpecials()]);
  }

  function renderGroups(){
    const ul = $("#rlGroups");
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
    const body = $("#rlBody");
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
        ? `<button class="rl-link-btn specials-link" data-mac="${r.addr ?? ""}">${typeLabel}</button>`
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
      const configCell = selectedConfigs.length ? `<div class="rl-config-tags">${selectedConfigs.join("")}</div>` : "-";
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
    $$("#rlBody input[type=checkbox]").forEach(cb => {
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
  $$("#rlTable thead th").forEach(th => {
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
      data = await apiGet("/racelink/api/wifi/interfaces");
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
    const r = await apiUpload("/racelink/api/fw/upload", fd);
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
    const data = await apiGet("/racelink/api/presets/list");
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
    const wifiConnName = ($("#fwWifiConnName")?.value || "racelink-wled-ap").trim();
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

    const r = await apiPost("/racelink/api/fw/start", body);
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

  // Transient banner (server unreachable / restarting) -----------------------
  function showTransientBanner(message){
    const banner = document.getElementById("transientBanner");
    const msgEl = document.getElementById("transientBannerMessage");
    if(!banner || !msgEl) return;
    msgEl.textContent = message;
    banner.classList.remove("hidden");
  }
  function hideTransientBanner(){
    const banner = document.getElementById("transientBanner");
    if(banner) banner.classList.add("hidden");
  }

  // Ephemeral toast -----------------------------------------------------------
  let _toastTimer = null;
  function showToast(message, durationMs){
    const toast = document.getElementById("rlToast");
    if(!toast) return;
    toast.textContent = message;
    toast.classList.remove("hidden");
    toast.classList.remove("rl-toast-fade");
    if(_toastTimer) clearTimeout(_toastTimer);
    _toastTimer = setTimeout(()=>{
      toast.classList.add("rl-toast-fade");
      setTimeout(()=>toast.classList.add("hidden"), 300);
    }, durationMs || 3000);
  }

  // Gateway banner render -----------------------------------------------------
  // Structured ``last_error.code`` drives both wording and retry-button
  // visibility: PORT_BUSY / LINK_LOST are auto-retried by the Host, so the
  // button is hidden while an auto-retry is scheduled (see ``next_retry_in_s``).
  let _retryCountdownTimer = null;
  function _stopRetryCountdown(){
    if(_retryCountdownTimer){ clearInterval(_retryCountdownTimer); _retryCountdownTimer = null; }
  }
  function _describeGatewayError(err){
    if(!err) return "RaceLink Gateway is not available.";
    const code = err.code || "";
    switch(code){
      case "PORT_BUSY":
        return "Gateway port busy (another process is using it).";
      case "NOT_FOUND":
        return "No RaceLink Gateway detected. Plug in the USB dongle.";
      case "LINK_LOST":
        return "Gateway link lost.";
      default:
        return err.reason ? `RaceLink Gateway unavailable: ${err.reason}` : "RaceLink Gateway is not available.";
    }
  }

  function updateGatewayStatus(status){
    const banner = document.getElementById("gatewayBanner");
    const msgEl = document.getElementById("gatewayBannerMessage");
    const retryBtn = document.getElementById("btnGatewayRetry");
    if(!banner || !msgEl) return;

    const ready = !!(status && status.ready);
    if(ready){
      _stopRetryCountdown();
      if(!banner.classList.contains("hidden")){
        showToast("RaceLink Gateway connected");
      }
      banner.classList.add("hidden");
      return;
    }

    const err = (status && status.last_error) || null;
    const base = _describeGatewayError(err);

    const autoRetry = err && err.next_retry_in_s != null;
    if(autoRetry){
      _stopRetryCountdown();
      // Run a local countdown so the user sees a live timer without waiting
      // for the next SSE update. Start from now + next_retry_in_s.
      let secondsLeft = Math.max(1, Math.round(Number(err.next_retry_in_s) || 1));
      const render = () => {
        msgEl.textContent = `${base} Next automatic retry in ${secondsLeft}s.`;
      };
      render();
      _retryCountdownTimer = setInterval(()=>{
        secondsLeft -= 1;
        if(secondsLeft <= 0){
          msgEl.textContent = `${base} Retrying now…`;
          _stopRetryCountdown();
          return;
        }
        render();
      }, 1000);
      if(retryBtn) retryBtn.classList.add("hidden");
    }else{
      _stopRetryCountdown();
      msgEl.textContent = base;
      if(retryBtn) retryBtn.classList.remove("hidden");
    }
    banner.classList.remove("hidden");
  }

  // SSE connection with auto-reconnect ----------------------------------------
  // Plan: on any error/close we enter a transient "reconnecting" state and
  // retry with exp-backoff 1s → 2s → 4s → 8s → 10s. Every reconnect also hits
  // /api/health for a cheap liveness probe, and once SSE is back up we
  // rehydrate state from /api/master so the banner is never stale.
  const _RECONNECT_DELAYS_MS = [1000, 2000, 4000, 8000, 10000];
  // Grace before showing the "reconnecting" banner -- keeps short network
  // blips (where the browser auto-reconnects within a second) silent.
  const _TRANSIENT_GRACE_MS = 2000;

  let _es = null;
  let _esReconnectAttempt = 0;
  let _esReconnectTimer = null;
  let _esKnownBad = false;
  let _esHadOpen = false;
  let _transientGraceTimer = null;

  function _armTransientBanner(message){
    // Only show the banner if we do not regain an OPEN connection within the
    // grace window. onopen cancels the timer.
    if(_transientGraceTimer) return;
    _transientGraceTimer = setTimeout(()=>{
      _transientGraceTimer = null;
      if(!_es || _es.readyState !== EventSource.OPEN){
        showTransientBanner(message);
      }
    }, _TRANSIENT_GRACE_MS);
  }
  function _clearTransientGrace(){
    if(_transientGraceTimer){ clearTimeout(_transientGraceTimer); _transientGraceTimer = null; }
  }

  function _scheduleReconnect(){
    if(_esReconnectTimer) return;
    const idx = Math.min(_esReconnectAttempt, _RECONNECT_DELAYS_MS.length - 1);
    const delay = _RECONNECT_DELAYS_MS[idx];
    _esReconnectAttempt += 1;
    _esReconnectTimer = setTimeout(()=>{
      _esReconnectTimer = null;
      _probeHealthAndConnect();
    }, delay);
  }

  function _probeHealthAndConnect(){
    // Fast liveness check so we can distinguish "server coming back up" from
    // "truly offline". Even if health fails we still attempt SSE; the
    // transient banner keeps the user informed either way.
    apiGet("/racelink/api/health").then((r)=>{
      if(r && r.ok){
        if(r.phase === "booting"){
          showTransientBanner("RotorHazard is starting…");
        }
      }
    }).catch(()=>{}).finally(()=>{
      connectEvents();
    });
  }

  function connectEvents(){
    try{
      if(_es){
        try{ _es.close(); }catch{}
        _es = null;
      }
      _esHadOpen = false;
      const es = new EventSource(withBasePath("/api/events"), {withCredentials:true});
      _es = es;

      es.onopen = () => {
        // Successful (re)connect -- reset backoff, hide transient banner and
        // the armed grace timer, rehydrate state from /api/master so the
        // user sees authoritative server state rather than a stale cache.
        _esReconnectAttempt = 0;
        _clearTransientGrace();
        hideTransientBanner();
        if(_esHadOpen && _esKnownBad){
          showToast("Connection restored");
        }else if(_esKnownBad){
          // First open after a page-load where the initial connection had
          // been failing -- same toast is appropriate.
          showToast("Connection restored");
        }
        _esHadOpen = true;
        _esKnownBad = false;
        apiGet("/racelink/api/master").then(r=>{
          if(r && r.master) updateMaster(r.master);
          if(r && r.task) updateTask(r.task);
          if(r && r.gateway) updateGatewayStatus(r.gateway);
        }).catch(()=>{});
      };
      es.addEventListener("master", (e)=>{ try{ updateMaster(JSON.parse(e.data)); }catch{} });
      es.addEventListener("task", (e)=>{ try{ updateTask(JSON.parse(e.data)); }catch{} });
      es.addEventListener("gateway", (e)=>{ try{ updateGatewayStatus(JSON.parse(e.data)); }catch{} });
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
        // Browsers fire onerror both when the stream permanently closes
        // (readyState == CLOSED, e.g. CORS / initial-open failure) and while
        // the built-in auto-reconnect is running (readyState == CONNECTING).
        // The typical RotorHazard restart case hits us in CONNECTING, so we
        // arm the banner on any non-OPEN state and cancel it in onopen.
        _esKnownBad = true;
        if(es.readyState === EventSource.CLOSED){
          _clearTransientGrace();
          showTransientBanner("RotorHazard not reachable — retrying…");
          _scheduleReconnect();
        }else{
          _armTransientBanner("RotorHazard not reachable — retrying…");
        }
      };
    }catch(e){
      console.warn("SSE not available", e);
      _esKnownBad = true;
      showTransientBanner("RotorHazard not reachable — retrying…");
      _scheduleReconnect();
    }
  }

  // Plan P1-1: wire the retry button + seed the banner from /api/gateway on load.
  (function initGatewayBanner(){
    const retryBtn = document.getElementById("btnGatewayRetry");
    if(retryBtn){
      retryBtn.addEventListener("click", async () => {
        retryBtn.disabled = true;
        try{
          const r = await apiPost("/racelink/api/gateway/retry", {});
          if(r && r.gateway) updateGatewayStatus(r.gateway);
        }catch(e){
          console.warn("Gateway retry failed", e);
        }finally{
          retryBtn.disabled = false;
        }
      });
    }
    apiGet("/racelink/api/gateway").then(r=>{
      if(r && r.gateway) updateGatewayStatus(r.gateway);
    }).catch(()=>{});
  })();

  // Buttons
  $("#btnSave").addEventListener("click", async ()=>{
    const r = await apiPost("/racelink/api/save",{});
    if(r.busy) return;
  });

  $("#btnReload").addEventListener("click", async ()=>{
    const r = await apiPost("/racelink/api/reload",{});
    if(!r.busy) await loadAll();
  });

  $("#btnForce").addEventListener("click", async ()=>{
    const r = await apiPost("/racelink/api/groups/force",{});
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
    const r = await apiPost("/racelink/api/presets/select", {name: selected});
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
    const r = await apiUpload("/racelink/api/presets/upload", formData);
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
    const wifiConnName = ($("#presetsWifiConnName")?.value || "racelink-wled-ap").trim();
    const wifiTimeoutS = Number($("#presetsWifiTimeoutS")?.value || 35) || 35;
    const hostWifiEnable = !!($("#presetsHostWifiEnable")?.checked);
    const hostWifiRestore = !!($("#presetsHostWifiRestore")?.checked);

    $("#presetsHint").textContent = "Starting presets download…";
    const r = await apiPost("/racelink/api/presets/download", {
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
    const r = await apiPost("/racelink/api/status", {selection: macs});
    if(r.busy){
      alert(`Busy: ${r.task?.name || "task"} is running`);
    }
  });

  $("#btnStatusAll").addEventListener("click", async ()=>{
    const r = await apiPost("/racelink/api/status", {});
    if(r.busy){
      alert(`Busy: ${r.task?.name || "task"} is running`);
    }
  });

  $("#btnBulkSetGroup").addEventListener("click", async ()=>{
    const macs = Array.from(state.selected);
    const gid = Number($("#bulkGroup").value);
    if(macs.length===0) return;
    const r = await apiPost("/racelink/api/devices/update-meta", {macs, groupId: gid});
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

  const r = await apiPost("/racelink/api/config", {mac: macs[0], option, data0, data1, data2, data3});
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
    const r = await apiPost("/racelink/api/discover", {targetGroupId, newGroupName});

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
    apiGet("/racelink/api/master").then(m=>{
      if(m.master) updateMaster(m.master);
      if(m.task) updateTask(m.task);
    }).catch(()=>{});
  });

  // Select all
  $("#selAll").addEventListener("change", (e)=>{
    const c = e.target.checked;
    state.selected.clear();
    $$("#rlBody input[type=checkbox]").forEach(cb => {
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
    const r = await apiPost("/racelink/api/groups/create", {name});
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
      const m = await apiGet("/racelink/api/master");
      if(m.master) updateMaster(m.master);
      if(m.task) updateTask(m.task);
    }catch(e){
      console.error("Master sync failed", e);
    }
  })().catch(console.error);
})();
