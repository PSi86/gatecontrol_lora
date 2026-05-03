(function(){
  // R5c: page-mode detection. The Devices page (/) renders racelink.html with
  // ``data-rl-page="devices"`` (the default when the attribute is absent for
  // back-compat). The Scenes page (/scenes) renders scenes.html with
  // ``data-rl-page="scenes"`` and a much smaller DOM — the device table,
  // group sidebar, FW dialog, etc. don't exist there. To keep this JS file
  // working on both pages without splitting the monolith, the selector
  // helper below returns a no-op stub for missing elements ON SCENES PAGES
  // so the top-level ``$("#btnX").addEventListener(...)`` calls degrade
  // silently. On the Devices page a missing element is still a real bug and
  // returns ``null`` (current throw-on-null behaviour preserved).
  const RL_PAGE = (document.body && document.body.dataset && document.body.dataset.rlPage) || "devices";
  const _NOOP = () => {};
  const _STUB_CLASSLIST = new Proxy({}, { get: () => _NOOP });
  const _STUB_STYLE = new Proxy({}, {
    get: () => "",
    set: () => true,
  });
  const _STUB_DATASET = new Proxy({}, {
    get: () => undefined,
    set: () => true,
  });
  const _STUB_EL = new Proxy({}, {
    get(_, prop){
      if(prop === "addEventListener" || prop === "removeEventListener"
         || prop === "appendChild" || prop === "removeChild"
         || prop === "setAttribute" || prop === "removeAttribute"
         || prop === "focus" || prop === "blur" || prop === "click"
         || prop === "showModal" || prop === "close" || prop === "open"
         || prop === "scrollIntoView") return _NOOP;
      if(prop === "classList") return _STUB_CLASSLIST;
      if(prop === "style") return _STUB_STYLE;
      if(prop === "dataset") return _STUB_DATASET;
      if(prop === "children" || prop === "options" || prop === "files") return [];
      if(prop === "value" || prop === "textContent" || prop === "innerHTML"
         || prop === "innerText" || prop === "name") return "";
      if(prop === "checked" || prop === "disabled" || prop === "hidden") return false;
      if(prop === "querySelector") return () => _STUB_EL;
      if(prop === "querySelectorAll") return () => [];
      // Fallback — undefined is the safest default for anything else.
      return undefined;
    },
    set: () => true,
  });

  const $ = (sel, ctx=document) => {
    const el = ctx.querySelector(sel);
    if(el) return el;
    return RL_PAGE === "devices" ? null : _STUB_EL;
  };
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

  // Flag bits (must match firmware; kept local for UI only).
  // Single source of truth: racelink/domain/flags.py.
  const RL_FLAG_POWER_ON       = 0x01;
  const RL_FLAG_ARM_ON_SYNC    = 0x02;
  const RL_FLAG_HAS_BRI        = 0x04;
  const RL_FLAG_FORCE_TT0      = 0x08;
  const RL_FLAG_FORCE_REAPPLY  = 0x10;
  const RL_FLAG_OFFSET_MODE    = 0x20;

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

  // Persisted selection: which group is active in the sidebar. Read at
  // bootstrap and validated against the freshly-fetched group list — if
  // the stored id no longer exists (group deleted, e.g. on another tab),
  // the caller falls back to whatever default it had before.
  function loadStoredSelGroupId(){
    try{
      const raw = localStorage.getItem("rlSelGroupId");
      if(raw === null || raw === "") return null;
      const n = Number(raw);
      return Number.isFinite(n) ? n : null;
    }catch{
      return null;
    }
  }

  function storeSelGroupId(id){
    try{
      if(id === null || id === undefined){
        localStorage.removeItem("rlSelGroupId");
      }else{
        localStorage.setItem("rlSelGroupId", String(id));
      }
    }catch{
      // ignore storage errors (private mode etc.)
    }
  }

  let state = {
    groups: [],
    devices: [],
    // Seed the active-group filter synchronously from localStorage so
    // ``renderTable`` already filters correctly when /api/devices wins
    // the parallel-load race against /api/groups. ``loadGroups`` later
    // validates this id against the fresh group list and falls back if
    // the stored group has since been deleted.
    selGroupId: loadStoredSelGroupId(),
    sortKey: null,
    sortDir: 1,
    selected: new Set(),
    busy: false,
    lastTask: null,
    lastMaster: null,
    fwUploads: { fwId: null, cfgId: null },
    configDisplay: loadConfigDisplay(),
    presets: { files: [], current: "" },
    rlPresets: { items: [], selectedKey: null, dirty: false },
    scenes: {
      items: [], selectedKey: null, schema: null, lastRunResult: null, draft: null,
      // R7: live per-action progress during a run.
      activeRunKey: null,
      actionStatus: [],
    },
    specials: {},
    specialDevice: null,
    specialTab: null,
  };

  // ---- generic resource-refresh registry --------------------------------
  // One rule across the WebUI: when a first-class element is created,
  // deleted, or renamed (regardless of resource type), every view that
  // consumes that data is re-fetched and re-rendered. The registry below
  // is the single entry point for both same-tab mutations and SSE-driven
  // cross-tab updates — they go through ``refreshResource`` so subscribers
  // never need to care which side triggered the change.
  //
  // Loaders are registered later in this file (and from scenes.js for the
  // scenes resource). Each subscriber is a re-render hook owned by the
  // view it updates; views null-guard themselves so a hook registered on
  // /racelink/ is harmless when scenes.js isn't loaded and vice versa.
  const _refreshSubs = {
    groups: [],
    devices: [],
    scenes: [],
    rl_presets: [],
    specials: [],
  };
  const _resourceLoaders = {};

  function registerLoader(resource, fn){
    _resourceLoaders[resource] = fn;
  }

  function subscribeRefresh(resource, fn){
    if(!_refreshSubs[resource]) _refreshSubs[resource] = [];
    _refreshSubs[resource].push(fn);
  }

  async function refreshResource(resource){
    const loader = _resourceLoaders[resource];
    if(typeof loader === "function"){
      try{ await loader(); }
      catch(e){ console.warn(`[refresh] loader for ${resource} failed`, e); }
    }
    const subs = _refreshSubs[resource] || [];
    for(const fn of subs){
      try{ await fn(); }
      catch(e){ console.warn(`[refresh] subscriber for ${resource} failed`, e); }
    }
  }

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
  async function apiJson(url, method, body){
    const res = await fetch(withBasePath(url), {
      method,
      headers: {"Content-Type":"application/json"},
      body: body === undefined ? undefined : JSON.stringify(body),
      credentials: "same-origin"
    });
    const j = await res.json().catch(()=>({ok:false,error:"Bad JSON"}));
    j.__status = res.status;
    return j;
  }
  const apiPut = (url, body) => apiJson(url, "PUT", body);
  const apiDelete = (url) => apiJson(url, "DELETE");



  function setBusy(isBusy){
    state.busy = !!isBusy;
    const disable = state.busy;

    // C7: setBusy is now called from both pages (Devices + Scenes).
    // Devices-page-only selectors are null-guarded so the function
    // doesn't crash on /racelink/scenes where those elements don't
    // exist. ``$$(".rl-actions button")`` already iterates safely
    // over an empty NodeList; the ``$()`` calls below need explicit
    // checks because they previously accessed ``.disabled`` on null.

    // Header action buttons (present on every page).
    $$(".rl-actions button").forEach(b => b.disabled = disable);

    // Devices-page-only controls.
    const newGroup = $("#btnNewGroup");          if(newGroup) newGroup.disabled = disable;
    const bulkBtn  = $("#btnBulkSetGroup");      if(bulkBtn)  bulkBtn.disabled  = disable;
    const cfgBtn   = $("#btnNodeCfgSend");       if(cfgBtn)   cfgBtn.disabled   = disable;
    const discBtn  = $("#btnDiscoverStart");     if(discBtn)  discBtn.disabled  = disable;

    // Scenes-page-only controls (the editor's Run/Save/Duplicate/
    // Delete row). The buttons live inside ``.rl-special-actions``
    // built per-render by scenes.js; an attribute selector finds
    // them without scenes.js needing to import a helper.
    $$("#sceneEditor .rl-special-actions button").forEach(b => b.disabled = disable);

    // Devices-page extra UI updaters — null-guarded so a missing
    // helper (e.g. on the scenes page) doesn't throw.
    if(typeof updateNodeCfgUi === "function") updateNodeCfgUi();
    if(typeof updatePresetsDownloadUi === "function") updatePresetsDownloadUi();
    if(typeof updateSpecialUi === "function") updateSpecialUi();
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
      // WLED effect-mode entries carry ``deterministic: true`` for the
      // 19 effects audited as cross-node sync-safe (see
      // racelink/domain/wled_deterministic.py + analysis doc). Mark them
      // with a leading "* " so the operator can pick offset-mode-safe
      // effects at a glance. The backend already sorts these to the top
      // of the list, so the marker is what the operator sees first.
      const baseLabel = String(optInfo.label ?? optInfo.value);
      opt.textContent = optInfo.deterministic ? `* ${baseLabel}` : baseLabel;
      if(optInfo.deterministic) opt.dataset.deterministic = "1";
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
    // B7: the "Refresh" button was a stub — its handler logged
    // "Refreshing is not implemented yet" and called an endpoint
    // (``/api/specials/get``) that returns 501. Hidden until a real
    // implementation lands; restoring the button is one Edit away.
    actions.appendChild(saveBtn);
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

    // A12: dynamic effect-specific UI (slot filtering + labels) — still
    // active in the preset editor (dlgRlPresets); after Phase D the
    // Specials dialog no longer uses it (wled_control carries only
    // {presetId, brightness}). The conditional block below therefore
    // only runs when the function actually has a ``mode`` var carrying
    // slot metadata.
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
          if(m.key === "mode") continue; // mode select stays visible always
          // Slot missing / unknown -> conservative fallback: show field, generic label.
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
        // A12: don't submit hidden fields (effect-specific unused). The
        // backend leaves them out of fieldMask/extMask -> the WLED node
        // doesn't overwrite anything irrelevant.
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

    // Phase D removed the Save-as-preset shortcut: the Specials-dialog no
    // longer holds a 14-field parameter editor (that moved to dlgRlPresets).
    // The only way to create a new RL preset is the editor dialog.
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
    if(f & RL_FLAG_POWER_ON)       parts.push("PWR");
    if(f & RL_FLAG_ARM_ON_SYNC)    parts.push("ARM");
    if(f & RL_FLAG_HAS_BRI)        parts.push("BRI");
    if(f & RL_FLAG_FORCE_TT0)      parts.push("TT0");
    if(f & RL_FLAG_FORCE_REAPPLY)  parts.push("REAP");
    if(f & RL_FLAG_OFFSET_MODE)    parts.push("OFS");
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
    // Reconcile ``state.selGroupId`` with the freshly-loaded list. Three
    // cases to handle:
    //   1. selGroupId was seeded synchronously from localStorage at module
    //      init, but the stored id no longer exists in the fleet → reset.
    //   2. nothing was stored / selGroupId is still null → pick the first
    //      group as the historical default.
    //   3. the stored id is still valid → keep it.
    // Doing this here (rather than only when ``selGroupId === null``)
    // avoids a stale highlight when a group is deleted on another tab.
    const idValid = (id) => state.groups.some(gr => gr.id === id);
    if(state.selGroupId !== null && !idValid(state.selGroupId)){
      state.selGroupId = null;
      storeSelGroupId(null);
    }
    if(state.selGroupId === null && state.groups.length > 0){
      state.selGroupId = state.groups[0].id;
    }
    renderGroups();
    renderBulkGroup();
    // ``renderTable`` filters by ``selGroupId`` — re-render it here so the
    // device list matches the sidebar selection regardless of whether
    // loadGroups or loadDevices won the parallel-load race. Pre-2026-05-02
    // only loadDevices called renderTable, which meant a fast /api/devices
    // response paired with a slow /api/groups left the table unfiltered
    // ("all devices" while the sidebar showed Unconfigured).
    renderTable();
  }

  async function loadDevices(){
    const d = await apiGet("/racelink/api/devices");
    state.devices = (d.devices||[]);
    // The sidebar's group rows show online/total counts and flash
    // when their devices reply, so they need a re-render whenever
    // the device list refreshes — driven by SSE on STATUS_REPLY /
    // IDENTIFY_REPLY events. ``renderGroups`` reads state.devices.
    renderGroups();
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
    // ``renderGroups`` is shared between loadGroups (sidebar) and the SSE
    // refresh path. The Scenes page doesn't have ``#rlGroups`` in its
    // template, so a refresh fired there would throw — null-guard it so
    // the loader stays cross-page-safe.
    if(!ul) return;
    ul.innerHTML = "";

    // Snapshot the freshest ``last_seen_ts`` per group from the device
    // list. Used below to (a) flash the group row when any of its
    // devices receives new data — mirroring the device-table flash —
    // and (b) compare against the previous snapshot to detect "fresh
    // since the last render". Same first-render behaviour as the
    // device table: an empty prev snapshot means nothing flashes on
    // initial load (otherwise every row would).
    const prevByGroup = state._lastSeenSnapshotByGroup || {};
    const nextByGroup = {};

    // Per-group device aggregation in a single pass over state.devices
    // (cheaper than re-filtering once per group when the fleet has
    // many devices).
    const byGroup = new Map();   // id → { total, online, maxSeen }
    (state.devices || []).forEach(d => {
      const gid = (typeof d.groupId === "number") ? d.groupId : -1;
      if(gid < 0) return;
      let entry = byGroup.get(gid);
      if(!entry){
        entry = { total: 0, online: 0, maxSeen: 0 };
        byGroup.set(gid, entry);
      }
      entry.total += 1;
      if(d.online === true) entry.online += 1;
      const seen = Number(d.last_seen_ts || 0);
      if(seen > entry.maxSeen) entry.maxSeen = seen;
    });

    state.groups.forEach(gr => {
      const li = document.createElement("li");
      li.className = (gr.id===state.selGroupId) ? "active" : "";
      // Build the row in DOM nodes (was innerHTML) so the per-group
      // delete button can be wired up safely without HTML-escaping
      // dance on the group name.
      const nameSpan = document.createElement("span");
      nameSpan.textContent = gr.name || `Group ${gr.id}`;
      // ``M / N`` — online of total. Falls back to the server-side
      // ``device_count`` when the device list hasn't loaded yet (the
      // groups list often renders before /api/devices completes on
      // page load).
      const agg = byGroup.get(gr.id) || { total: gr.device_count || 0, online: 0, maxSeen: 0 };
      const countSpan = document.createElement("span");
      countSpan.className = "count";
      countSpan.textContent = `${agg.online} / ${agg.total}`;
      countSpan.title =
        `${agg.online} of ${agg.total} device${agg.total === 1 ? "" : "s"} ` +
        `in this group ${agg.online === 1 ? "is" : "are"} currently online ` +
        `(replied to the last status query or sent an unsolicited ` +
        `IDENTIFY_REPLY recently).`;
      li.appendChild(nameSpan);
      li.appendChild(countSpan);
      // Per-group delete button. Hidden by default, revealed on
      // hover via CSS. Static groups (e.g. "All WLED Nodes") and the
      // synthetic Unconfigured (id=0) cannot be deleted; for those
      // rows we still emit the button as a placeholder so the count
      // column lines up with deletable rows — the placeholder class
      // keeps it permanently invisible and non-interactive.
      const deletable = !gr.static && Number(gr.id) !== 0;
      const delBtn = document.createElement("button");
      delBtn.type = "button";
      delBtn.className = "rl-group-delete-btn";
      delBtn.textContent = "✕";
      if(deletable){
        delBtn.title = `Delete group "${gr.name}"`;
        delBtn.addEventListener("click", async (e) => {
          // Stop the click from also selecting the group (the row's
          // own click handler runs in the bubble phase otherwise).
          e.stopPropagation();
          await handleGroupDelete(gr);
        });
      }else{
        delBtn.classList.add("rl-group-delete-btn--placeholder");
        delBtn.tabIndex = -1;
        delBtn.setAttribute("aria-hidden", "true");
        delBtn.disabled = true;
      }
      li.appendChild(delBtn);
      li.addEventListener("click", () => {
        state.selGroupId = gr.id;
        storeSelGroupId(gr.id);
        renderGroups();
        renderTable();
      });

      // Flash on incoming device data. ``maxSeen`` is the freshest
      // ``last_seen_ts`` across the group's devices; an advance over
      // the previous snapshot means at least one device replied
      // since the last render. The auto-strip mirrors the device
      // table's pattern so a row that doesn't refresh next time
      // doesn't keep a stale class.
      nextByGroup[gr.id] = agg.maxSeen;
      const prevSeen = prevByGroup[gr.id];
      if(prevSeen !== undefined && agg.maxSeen > prevSeen){
        li.classList.add("rl-row-flash");
        setTimeout(() => { li.classList.remove("rl-row-flash"); }, 1100);
      }
      ul.appendChild(li);
    });

    state._lastSeenSnapshotByGroup = nextByGroup;
  }

  async function handleGroupDelete(group){
    // C2-style destructive confirm: name the group, count the
    // affected devices, and warn about scene renumbering. The
    // device count is already in the group row's payload.
    const devCount = Number(group.device_count || 0);
    const consequences = [];
    if(devCount > 0){
      consequences.push(
        `${devCount} device${devCount === 1 ? "" : "s"} will move to "Unconfigured" (group 0)`
      );
    }
    consequences.push(
      "scene actions targeting this group will collapse to Unconfigured, "
      + "and scene actions targeting higher-numbered groups will renumber"
    );
    const msg = `Delete group "${group.name}"?\n\n` + consequences.join(". ") + ".";
    if(!confirmDestructive(msg)) return;

    const r = await apiPost("/racelink/api/groups/delete", { id: group.id });
    if(!r || !r.ok){
      showToastError(r?.error || "Delete failed.");
      return;
    }
    // Build a friendly summary toast from the response counts.
    const parts = [];
    if(r.moved_devices) parts.push(`${r.moved_devices} → Unconfigured`);
    if(r.renumbered_devices) parts.push(`${r.renumbered_devices} renumbered`);
    if(r.renumbered_scenes) parts.push(`${r.renumbered_scenes} scene${r.renumbered_scenes === 1 ? "" : "s"} updated`);
    showToast(
      parts.length
        ? `Deleted "${group.name}" — ${parts.join(", ")}.`
        : `Deleted "${group.name}".`
    );
    // If the deleted group was the active filter, drop the filter
    // so the table doesn't show an empty view.
    if(state.selGroupId === group.id){
      state.selGroupId = null;
      storeSelGroupId(null);
    }
    // SSE refresh will fire from the server too; this just makes
    // the response feel instant. Routing through ``refreshResource``
    // also notifies any subscriber registered on /scenes (target
    // picker etc.) without scenes.js having to listen separately.
    await refreshResource("groups");
    await refreshResource("devices");
  }

  function renderBulkGroup(){
    const sel = $("#bulkGroup"), sel2 = $("#discoverGroup");
    // Devices-page-only widgets; null-guard so cross-page refreshes
    // don't throw on /scenes.
    if(!sel || !sel2) return;
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

    // The "Discover in" selector lets the operator pick which groupId
    // the OPC_DEVICES filter targets. Default is 0 (Unconfigured =
    // newly-booted devices, the historical discovery flow). A specific
    // group id re-polls a known group; "all" sweeps every known group
    // sequentially. See docs/reference/broadcast-ruleset.md
    // §"Designed-in special cases" and the Discovery group selector
    // section of webui-guide.md.
    const inSel = $("#discoverInGroup");
    if(inSel){
      const previousValue = inSel.value;
      inSel.innerHTML = "";
      const optDefault = document.createElement("option");
      optDefault.value = "0";
      optDefault.textContent = "Unconfigured (group 0) — default";
      inSel.appendChild(optDefault);
      selectableGroups.forEach(gr => {
        const o = document.createElement("option");
        o.value = String(gr.id);
        o.textContent = `Group ${gr.id}: ${gr.name}`;
        inSel.appendChild(o);
      });
      const optAll = document.createElement("option");
      optAll.value = "all";
      optAll.textContent = "All groups (sweep — N packets)";
      inSel.appendChild(optAll);
      // Preserve the operator's last choice across re-renders (groups
      // refresh re-renders this dropdown via the GROUPS scope event).
      if(previousValue && [...inSel.options].some(o => o.value === previousValue)){
        inSel.value = previousValue;
      }
    }
  }

  function renderTable(){
    const body = $("#rlBody");
    // Devices-page-only widget; null-guard so a refresh on /scenes
    // (which has no device table) is a safe no-op.
    if(!body) return;
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

    // Flash animation on rows whose last_seen_ts advanced since the
    // previous render. The first render after page-load has an empty
    // snapshot so nothing flashes (otherwise every row would on
    // initial load); subsequent renders flash only the rows that
    // received fresh data — typically a STATUS_REPLY or
    // IDENTIFY_REPLY arrived over SSE and triggered a re-render.
    const prevSeenSnapshot = state._lastSeenSnapshot || {};
    const nextSeenSnapshot = {};

    rows.forEach(r => {
      const tr = document.createElement("tr");
      const checked = state.selected.has(r.addr);
      const typeId = getDeviceTypeId(r);
      const typeLabel = r.dev_type_name || r.type_name || (isNaN(typeId) ? "" : String(typeId));
      const specials = getSpecialsForDevice(r);
      const typeCell = (specials.length && typeLabel)
        ? `<button class="rl-link-btn specials-link" data-mac="${r.addr ?? ""}">${typeLabel}</button>`
        : typeLabel;
      // Track the row's identity + freshness for the flash detector
      // below. Use the MAC as the dataset key; the table is rebuilt
      // each render so we can't keep a per-tr handle across renders.
      tr.dataset.mac = String(r.addr || "");
      const seen = Number(r.last_seen_ts || 0);
      nextSeenSnapshot[r.addr] = seen;
      if(prevSeenSnapshot[r.addr] !== undefined && seen > prevSeenSnapshot[r.addr]){
        tr.classList.add("rl-row-flash");
        // Auto-strip the class after the animation duration so a row
        // that doesn't refresh next time doesn't leave a stale class.
        setTimeout(() => { tr.classList.remove("rl-row-flash"); }, 1100);
      }
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

    // Persist the freshness snapshot for the next render's flash
    // comparison. Only includes the macs we just rendered — devices
    // filtered out (by the group-selection filter) won't flash on
    // re-appearance, which is the right call (the filter change is
    // the noise source, not fresh radio data).
    state._lastSeenSnapshot = nextSeenSnapshot;

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
  // C8 / Batch B: human-readable explanation of each master-state for the
  // pill tooltip. Pre-Batch-B the host inferred state by combining
  // EV_RX_WINDOW_OPEN/CLOSED + EV_TX_DONE; v4 mirrors the gateway's
  // single state byte verbatim, so the help dictionary now matches the
  // GatewayState enum (IDLE / TX / RX_WINDOW / RX / ERROR / UNKNOWN).
  const MASTER_STATE_HELP = {
    UNKNOWN:   "Unknown — host hasn't received a STATE_REPORT yet (USB just connected, or the gateway never replied). Click ↻ to refresh.",
    IDLE:      "Idle — gateway is in continuous RX, ready for the next host send. No traffic in flight.",
    TX:        "Transmitting — gateway is sending an RF packet to the fleet. Auto-clears when the radio finishes (LBT backoff ~50–300 ms + airtime).",
    RX_WINDOW: "RX window — gateway has a bounded receive window open after a unicast/stream send and is waiting for a node reply. Auto-closes at the window's deadline.",
    RX:        "Receiving — gateway is in active receive (setDefaultRxNone mode only; not used by the current default firmware).",
    ERROR:     "Error — the gateway reported a fault. May be transient (USB hiccup) or persistent (link lost); check ``last_error`` for the cause and the gateway banner for retry status.",
  };

  function updateMaster(m){
    state.lastMaster = m;
    const pill = $("#masterPill");
    const detail = $("#masterDetail");

    const st = (m && m.state) ? String(m.state) : "UNKNOWN";
    pill.textContent = st === "RX_WINDOW" ? "RX-WIN" : st;
    pill.classList.remove("idle","tx","rx","err","unknown");
    if(st==="TX") pill.classList.add("tx");
    else if(st==="RX_WINDOW" || st==="RX") pill.classList.add("rx");
    else if(st==="ERROR") pill.classList.add("err");
    else if(st==="UNKNOWN") pill.classList.add("unknown");
    else pill.classList.add("idle");

    const parts = [];
    if(st==="RX_WINDOW" && m.state_metadata_ms) parts.push(`min_ms ${m.state_metadata_ms}`);
    if(m.last_event) parts.push(`last: ${m.last_event}`);
    if(m.last_error) parts.push(`err: ${m.last_error}`);
    detail.textContent = parts.join(" · ");

    // C8: build the pill's hover-tooltip from the state explanation
    // plus the same sub-state parts the masterDetail span shows. The
    // detail span truncates on narrow screens; the tooltip is the
    // accessibility-friendly alternative.
    const help = MASTER_STATE_HELP[st] || `Master state: ${st}`;
    pill.title = parts.length ? `${help}\n\n${parts.join("\n")}` : help;

    updateNodeCfgUi();
    updatePresetsDownloadUi();

  }

  // Batch B: ↻ refresh button on the master pill. Sends a synchronous
  // STATE_REQUEST to the gateway and merges the reply into the local
  // master snapshot. Useful at startup, after a USB drop, or when the
  // operator wants to confirm the gateway is alive.
  function bindMasterRefresh(){
    const btn = $("#masterRefresh");
    if(!btn || btn.dataset.bound) return;
    btn.dataset.bound = "1";
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      try {
        const r = await fetch("/racelink/api/gateway/query-state", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
        });
        if(r.ok) {
          const j = await r.json();
          if(j) updateMaster(Object.assign({}, state.lastMaster || {}, j));
        }
      } catch(_) {
        // swallow-ok: SSE will deliver the next gateway-driven update
        // and the operator can click again. No banner needed.
      } finally {
        btn.disabled = false;
      }
    });
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
        // C4: mirror the running task into the OTA progress dialog
        // panel if it's currently visible. Cheap to call repeatedly;
        // the function is a no-op when the panel is hidden.
        try{ fwUpdateProgressFromTask(t); }catch(e){ console.error(e); }
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

      if(name==="bulk_set_group"){
        // 2026-04-29: bulk-move "Move selected to group" runs as a
        // task so the operator gets per-device progress instead of
        // staring at a frozen UI for 8 s per offline device. Same
        // ``index/total`` + ``stage`` shape the fwupdate path uses.
        if(meta.index!==undefined && meta.total!==undefined) mparts.push(`${meta.index}/${meta.total}`);
        if(meta.stage) mparts.push(String(meta.stage));
        if(meta.addr) mparts.push(String(meta.addr));
        if(meta.message) mparts.push(String(meta.message));
      }

      if(name==="force_groups"){
        // 2026-04-29 (rf-timing batch): "Re-sync group config"
        // mirrors the bulk_set_group masterbar layout. The route
        // skips offline devices entirely, so the index/total ratio
        // counts every known device but the stage flips quickly
        // through the offline ones.
        if(meta.index!==undefined && meta.total!==undefined) mparts.push(`${meta.index}/${meta.total}`);
        if(meta.stage) mparts.push(String(meta.stage));
        if(meta.addr) mparts.push(String(meta.addr));
        if(meta.message) mparts.push(String(meta.message));
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
    // C4: also finalise the progress panel if it was shown.
    if(st==="done" || st==="error"){
      try{ fwFinaliseProgressFromTask(t); }catch(e){ console.error(e); }
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

  if(name==="bulk_set_group"){
    // 2026-04-29 — final summary toast on completion. Pre-fix the
    // route was synchronous and produced no toast at all; now the
    // operator gets a per-outcome breakdown ("3 acked, 2 offline,
    // 1 timed out") that matches what the masterbar showed during
    // the run.
    if(st==="done" && state._bulkSetGroupActive){
      state._bulkSetGroupActive = false;
      const r = t.result || {};
      const parts = [];
      if(r.changed) parts.push(`${r.changed} ACKed`);
      if(r.skipped_offline) parts.push(`${r.skipped_offline} offline (queued for auto-restore)`);
      if(r.timed_out) parts.push(`${r.timed_out} timed out`);
      const summary = parts.length ? parts.join(", ") : "no devices changed";
      // Use the error-flavoured toast when something went wrong
      // (timed_out or transport problems); otherwise green.
      if(r.timed_out){
        showToastError(`Move finished with timeouts — ${summary}.`);
      } else {
        showToast(`Move complete — ${summary}.`);
      }
    } else if(st==="error" && state._bulkSetGroupActive){
      state._bulkSetGroupActive = false;
      showToastError(`Move failed: ${t.last_error || "unknown error"}`);
    }
  }

  if(name==="force_groups"){
    // 2026-04-29 (rf-timing batch): completion toast for "Re-sync
    // group config". Same shape as bulk_set_group's summary, gated
    // by ``_forceGroupsActive`` so the toast only fires for the
    // run the operator started (not for any historical task that
    // SSE replays into ``updateTask``).
    if(st==="done" && state._forceGroupsActive){
      state._forceGroupsActive = false;
      const r = t.result || {};
      const parts = [];
      if(r.changed) parts.push(`${r.changed} ACKed`);
      if(r.skipped_offline) parts.push(`${r.skipped_offline} offline (queued for auto-restore)`);
      if(r.timed_out) parts.push(`${r.timed_out} timed out`);
      const summary = parts.length ? parts.join(", ") : "no devices changed";
      if(r.timed_out){
        showToastError(`Re-sync finished with timeouts — ${summary}.`);
      } else {
        showToast(`Re-sync complete — ${summary}.`);
      }
    } else if(st==="error" && state._forceGroupsActive){
      state._forceGroupsActive = false;
      showToastError(`Re-sync failed: ${t.last_error || "unknown error"}`);
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
      showToastError("Please choose a file first.");
      return null;
    }
    const fd = new FormData();
    fd.append("file", f);
    fd.append("kind", kind);
    const r = await apiUpload("/racelink/api/fw/upload", fd);
    if(!r.ok){
      showToastError(r.error || "Upload failed");
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
      showToastError("Firmware dialog failed to open. Check console for details.");
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
      showToastError("No target devices (selection / filter empty).");
      return;
    }
    const doFirmware = $("#fwDoFirmware").checked;
    const doPresets = $("#fwDoPresets").checked;
    const doCfg = $("#fwDoCfg").checked;
    if(!doFirmware && !doPresets && !doCfg){
      showToastError("Select at least one operation (firmware, presets, or cfg).");
      return;
    }

    const baseUrl = ($("#fwBaseUrl").value || "").trim() || "http://4.3.2.1";
    const retries = Number($("#fwRetries").value || 3) || 3;

    // Parse the comma-separated SSID list. Empty entries are stripped on
    // the server too; sending an empty list triggers a 400 — the field's
    // placeholder + default carry the canonical SSIDs so this only fires
    // if the operator wiped the field intentionally.
    const wifiSsids = ($("#fwWifiSsid")?.value || "WLED_RaceLink_AP, WLED-AP")
                       .split(",").map(s => s.trim()).filter(Boolean);
    const wifiIface = ($("#fwWifiIface")?.value || "wlan0").trim();
    const wifiPassword = ($("#fwWifiPassword")?.value || "wled1234");
    const wifiOtaPassword = ($("#fwWifiOtaPassword")?.value || "wledota");
    const wifiTimeoutS = Number($("#fwWifiTimeoutS")?.value || 20) || 20;

    const hostWifiEnable = !!($("#fwHostWifiEnable")?.checked);
    const hostWifiRestore = !!($("#fwHostWifiRestore")?.checked);
    const skipValidation = !!($("#fwSkipValidation")?.checked);

    const body = {
      macs,
      baseUrl,
      retries,
      hostWifiEnable,
      hostWifiRestore,
      doFirmware,
      doPresets,
      doCfg,
      skipValidation,
      wifi: {
        ssids: wifiSsids,
        password: wifiPassword,
        otaPassword: wifiOtaPassword,
        iface: wifiIface,
        timeoutS: wifiTimeoutS,
      }
    };

    if(doFirmware){
      if(!state.fwUploads.fwId){
        showToastError("Firmware enabled but firmware is not uploaded yet.");
        return;
      }
      body.fwId = state.fwUploads.fwId;
    }

    if(doPresets){
      const presetsName = ($("#fwPresetsSelect").value || "").trim();
      if(!presetsName){
        showToastError("Presets enabled but no presets.json is available.");
        return;
      }
      body.presetsName = presetsName;
    }
    if(doCfg){
      if(!state.fwUploads.cfgId){
        showToastError("cfg enabled but cfg.json is not uploaded yet.");
        return;
      }
      body.cfgId = state.fwUploads.cfgId;
    }

    const r = await apiPost("/racelink/api/fw/start", body);
    if(r.busy){
      showToast(`Busy: ${r.task?.name || "task"} is running`);
      return;
    }
    if(!r.ok){
      showToastError(r.error || "Failed to start firmware update.");
      return;
    }

    // C4: keep the dialog open and switch to the progress panel.
    // ``updateTask`` populates the panel's stage / index / message
    // fields as the SSE ``task`` events arrive. Operators no longer
    // see a closed dialog with no feedback during a multi-minute
    // multi-device firmware roll-out.
    fwShowProgressPanel(macs);
  });

  // ---- C4: OTA progress panel ---------------------------------------------
  // Two-state dialog (#fwConfig vs #fwProgress); switching is purely a
  // visibility toggle. ``state.fwUI`` keeps the macs we expect to see
  // in the per-device summary so a malformed task-meta payload can't
  // wipe the row list.
  state.fwUI = state.fwUI || { macs: [], rows: new Map() };

  function fwShowProgressPanel(macs){
    const cfg = document.getElementById("fwConfig");
    const prog = document.getElementById("fwProgress");
    if(!cfg || !prog) return;
    cfg.classList.add("hidden");
    prog.classList.remove("hidden");
    state.fwUI.macs = (macs || []).map(m => String(m).toUpperCase());
    state.fwUI.rows = new Map();

    // Seed the per-device summary so the operator can see the planned
    // macs immediately rather than empty space until the first event.
    const summary = document.getElementById("fwProgressSummary");
    if(summary){
      summary.innerHTML = "";
      state.fwUI.macs.forEach(mac => {
        const row = document.createElement("div");
        row.className = "rl-fw-progress-summary-row";
        row.dataset.mac = mac;
        const macSpan = document.createElement("span");
        macSpan.className = "mac";
        macSpan.textContent = mac;
        const status = document.createElement("span");
        status.className = "status";
        status.textContent = "queued";
        row.appendChild(macSpan);
        row.appendChild(status);
        summary.appendChild(row);
        state.fwUI.rows.set(mac, { row, status });
      });
    }

    // Initial stage label until the first task event lands.
    const stage = document.getElementById("fwProgressStage");
    if(stage) stage.textContent = `Starting (${macs.length} device${macs.length === 1 ? "" : "s"})…`;
    const msg = document.getElementById("fwProgressMessage");
    if(msg) msg.textContent = "Waiting for the first stage event…";
    const bar = document.getElementById("fwProgressBar");
    if(bar){ bar.style.width = "0%"; bar.classList.remove("done", "error"); }
  }

  function fwResetDialogToConfig(){
    const cfg = document.getElementById("fwConfig");
    const prog = document.getElementById("fwProgress");
    if(cfg) cfg.classList.remove("hidden");
    if(prog) prog.classList.add("hidden");
  }

  // ``btnFwClose`` is a normal cancel-button (form method=dialog), but
  // we also want the *next* time the operator opens the dialog to
  // start fresh on the config form. Attach a close handler.
  const _dlgFw = document.getElementById("dlgFwUpdate");
  if(_dlgFw){
    _dlgFw.addEventListener("close", fwResetDialogToConfig);
  }

  function fwUpdateProgressFromTask(t){
    const prog = document.getElementById("fwProgress");
    if(!prog || prog.classList.contains("hidden")) return;

    const meta = t.meta || {};
    const stageEl = document.getElementById("fwProgressStage");
    const msgEl = document.getElementById("fwProgressMessage");
    const bar = document.getElementById("fwProgressBar");

    const idx = Number(meta.index || 0);
    const total = Number(meta.total || state.fwUI.macs.length || 0);
    const stage = String(meta.stage || "");
    const addr = String(meta.addr || "").toUpperCase();
    const attempt = meta.attempt;
    const retries = meta.retries;
    const message = String(meta.message || "");

    if(stageEl){
      const parts = [];
      if(total) parts.push(`Device ${idx} of ${total}`);
      if(stage) parts.push(stage);
      if(attempt && retries) parts.push(`try ${attempt}/${retries}`);
      stageEl.textContent = parts.join(" · ") || "Running…";
    }
    if(msgEl){
      msgEl.textContent = message || (addr ? `device ${addr}` : "");
    }
    if(bar && total){
      const pct = Math.max(0, Math.min(100, Math.round((idx / total) * 100)));
      bar.style.width = `${pct}%`;
    }

    // Per-device row updates: mark the current device as running, the
    // ones before it as ok (best-effort — the task only signals
    // forward progress; per-device errors from results.errors are
    // applied on task done/error below).
    if(addr && state.fwUI.rows.has(addr)){
      const cur = state.fwUI.rows.get(addr);
      if(!cur.row.classList.contains("error") && !cur.row.classList.contains("ok")){
        cur.row.classList.remove("running");
        cur.row.classList.add("running");
        cur.status.textContent = stage || "running";
      }
    }
    // Mark every mac that appears before the current one as "ok"
    // (best-effort, see comment above).
    if(addr && state.fwUI.macs.length){
      const seen = state.fwUI.macs.indexOf(addr);
      if(seen > 0){
        for(let i = 0; i < seen; i++){
          const m = state.fwUI.macs[i];
          const r = state.fwUI.rows.get(m);
          if(r && !r.row.classList.contains("error") && !r.row.classList.contains("ok")){
            r.row.classList.remove("running");
            r.row.classList.add("ok");
            r.status.textContent = "ok";
          }
        }
      }
    }
  }

  function fwFinaliseProgressFromTask(t){
    const prog = document.getElementById("fwProgress");
    if(!prog || prog.classList.contains("hidden")) return;
    const stageEl = document.getElementById("fwProgressStage");
    const msgEl = document.getElementById("fwProgressMessage");
    const bar = document.getElementById("fwProgressBar");

    const result = t.result || {};
    const errs = (result.errors && result.errors.length) ? result.errors.length : 0;
    const errored = t.state === "error" || errs > 0;

    if(stageEl){
      stageEl.textContent = errored
        ? "Update finished with errors"
        : "Update complete";
    }
    if(msgEl){
      if(errored){
        const lastErr = t.last_error || (result.errors && result.errors[0]) || "see device list below";
        msgEl.textContent = `Errors: ${errs}. ${lastErr}`;
      } else {
        msgEl.textContent = "All devices updated successfully. You can close this dialog.";
      }
    }
    if(bar){
      bar.style.width = "100%";
      bar.classList.remove("done", "error");
      bar.classList.add(errored ? "error" : "done");
    }

    // Apply per-device results when available. The OTA workflow
    // populates ``result.devices`` with per-mac entries; map them
    // onto the seeded rows.
    const devices = result.devices || [];
    devices.forEach(d => {
      const mac = String(d.addr || d.mac || "").toUpperCase();
      const row = state.fwUI.rows.get(mac);
      if(!row) return;
      row.row.classList.remove("running", "ok", "error");
      if(d.ok){
        row.row.classList.add("ok");
        row.status.textContent = "ok";
      } else {
        row.row.classList.add("error");
        row.status.textContent = d.error || "failed";
      }
    });
    // Any rows still in "running" state when the task ended -
    // attribute them to the operator-visible aggregate result.
    state.fwUI.rows.forEach(({row, status}) => {
      if(row.classList.contains("running") && !row.classList.contains("error") && !row.classList.contains("ok")){
        row.classList.remove("running");
        if(errored){
          row.classList.add("error");
          status.textContent = "no result";
        } else {
          row.classList.add("ok");
          status.textContent = "ok";
        }
      }
    });
  }

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
  // Two flavours, sharing one DOM element:
  //   * ``showToast(msg)``       success / info (green, 3 s default)
  //   * ``showToastError(msg)``  error / validation (red, 5 s default)
  // Both are non-blocking; native ``alert()`` is no longer used in the
  // operator-facing UI. Error toasts deliberately get a longer default
  // duration so a fast scroll doesn't lose them.
  let _toastTimer = null;
  function _renderToast(message, durationMs){
    const toast = document.getElementById("rlToast");
    if(!toast) return;
    toast.textContent = message;
    toast.classList.remove("hidden");
    toast.classList.remove("rl-toast-fade");
    if(_toastTimer) clearTimeout(_toastTimer);
    _toastTimer = setTimeout(()=>{
      toast.classList.add("rl-toast-fade");
      setTimeout(()=>toast.classList.add("hidden"), 300);
    }, durationMs);
  }
  function showToast(message, durationMs){
    const toast = document.getElementById("rlToast");
    if(!toast) return;
    toast.classList.remove("rl-toast-error");
    _renderToast(message, durationMs || 3000);
  }
  function showToastError(message, durationMs){
    const toast = document.getElementById("rlToast");
    if(!toast) return;
    toast.classList.add("rl-toast-error");
    _renderToast(message, durationMs || 5000);
  }

  // C2: standardised confirmation for destructive ops. Uses native
  // ``confirm()`` because it's blocking, accessible, and keyboard-
  // friendly out of the box; building a custom modal here would be
  // bigger scope than the audit asked for. Keep callsites consistent
  // by routing through this wrapper so a future swap to a custom
  // modal is one-line.
  function confirmDestructive(message){
    return Boolean(window.confirm(message));
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

  // Close the EventSource synchronously when the page is being unloaded.
  // Without this, Chrome lets a navigation-triggered SSE close drift through
  // its "graceful FIN" path and parks the underlying TCP socket in a
  // half-finished state inside its HTTP/1.1 connection pool. After ~5
  // page-switches between /racelink/ and /racelink/scenes that pool fills up
  // (limit 6 per origin) and the next set of API requests hangs for tens of
  // seconds. ``pagehide`` runs *before* unload while JS is still alive and
  // forces an explicit close, which makes the browser release the socket
  // slot deterministically. Persisted-page-cache-bound tabs (event.persisted)
  // would survive a reconnect anyway — close them too because the new page
  // load opens its own fresh EventSource.
  window.addEventListener("pagehide", () => {
    if(_esReconnectTimer){
      clearTimeout(_esReconnectTimer);
      _esReconnectTimer = null;
    }
    if(_es){
      try{ _es.close(); }catch{}
      _es = null;
    }
  });

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
          // Route every server-broadcast refresh through the registry so
          // SSE-driven and local mutations share the exact same code path
          // (loader + every subscriber). Topics without a loader and no
          // subscribers are no-ops, which keeps forward-compat with new
          // server-side topics that the client doesn't know about yet.
          for(const topic of what){
            await refreshResource(topic);
          }
        }catch{
          await loadAll();
        }
      });
      // R7: per-action live progress for the Scene Manager. Runs on both
      // pages — on / the handler is not installed and the event is a no-op.
      es.addEventListener("scene_progress", (e)=>{
        try{
          const p = JSON.parse(e.data);
          if(typeof window.__rlSceneProgress === "function"){
            window.__rlSceneProgress(p);
          }
        }catch{
          // ignore malformed payloads — runner-side broadcasts are
          // structured but a corrupt SSE chunk shouldn't break the page.
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

  // 2026-04-29 (rf-timing batch + skip-offline toggle): the route is
  // TaskManager-wrapped (mirrors bulk_set_group). The dialog exposes
  // the ``skipOffline`` toggle — default OFF so re-sync's operator
  // semantic ("push to ALL") is the no-click path. Operators with
  // large fleets can opt into the fast skip-offline path.
  const dlgResync = $("#dlgResyncGroups");
  $("#btnForce").addEventListener("click", ()=>{
    // Reset the checkbox each time the dialog opens so the previous
    // run's choice doesn't silently persist into the next.
    const cb = $("#resyncSkipOffline");
    if(cb) cb.checked = false;
    if(dlgResync) dlgResync.showModal();
  });

  $("#btnResyncStart").addEventListener("click", async (e)=>{
    e.preventDefault();
    const skipOffline = !!$("#resyncSkipOffline")?.checked;
    if(dlgResync) dlgResync.close();
    const r = await apiPost("/racelink/api/groups/force", {skipOffline});
    if(r && r.task){
      state._forceGroupsActive = true;
      const toastMsg = skipOffline
        ? "Re-syncing group config (skipping offline)…"
        : "Re-syncing group config…";
      showToast(toastMsg);
      try{ updateTask(r.task); }catch(err){ console.error(err); }
    } else if(r && r.busy){
      // covered by the universal busy toast in apiPost
    } else if(r && !r.ok){
      showToastError(`Re-sync failed: ${r.error || "unknown error"}`);
    }
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
      showToastError("Select a presets.json file first.");
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
      showToastError("Select exactly one device to download presets.");
      return;
    }
    const baseUrl = ($("#presetsBaseUrl").value || "").trim() || "http://4.3.2.1";
    const wifiSsids = ($("#presetsWifiSsid")?.value || "WLED_RaceLink_AP, WLED-AP")
                       .split(",").map(s => s.trim()).filter(Boolean);
    const wifiIface = ($("#presetsWifiIface")?.value || "wlan0").trim();
    const wifiPassword = ($("#presetsWifiPassword")?.value || "wled1234");
    const wifiOtaPassword = ($("#presetsWifiOtaPassword")?.value || "wledota");
    const wifiTimeoutS = Number($("#presetsWifiTimeoutS")?.value || 20) || 20;
    const hostWifiEnable = !!($("#presetsHostWifiEnable")?.checked);
    const hostWifiRestore = !!($("#presetsHostWifiRestore")?.checked);

    $("#presetsHint").textContent = "Starting presets download…";
    const r = await apiPost("/racelink/api/presets/download", {
      mac: macs[0],
      baseUrl,
      hostWifiEnable,
      hostWifiRestore,
      wifi: {
        ssids: wifiSsids,
        password: wifiPassword,
        otaPassword: wifiOtaPassword,
        iface: wifiIface,
        timeoutS: wifiTimeoutS,
      }
    });
    if(r.busy){
      showToast(`Busy: ${r.task?.name || "task"} is running`);
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
      showToast(`Busy: ${r.task?.name || "task"} is running`);
    }
  });

  $("#btnStatusAll").addEventListener("click", async ()=>{
    const r = await apiPost("/racelink/api/status", {});
    if(r.busy){
      showToast(`Busy: ${r.task?.name || "task"} is running`);
    }
  });

  $("#btnBulkSetGroup").addEventListener("click", async ()=>{
    const macs = Array.from(state.selected);
    const gid = Number($("#bulkGroup").value);
    if(macs.length===0){
      showToastError("Select one or more devices first.");
      return;
    }
    // C2: changing group membership for many devices at once is
    // destructive in the "operator clicked the wrong dropdown"
    // sense — confirm with the count + the target group.
    const groupSel = $("#bulkGroup");
    const groupLabel = groupSel.options[groupSel.selectedIndex]?.textContent || `Group ${gid}`;
    if(!confirmDestructive(
      `Move ${macs.length} device${macs.length === 1 ? "" : "s"} to "${groupLabel}"? `
      + "This sends a SET_GROUP packet to each one."
    )) return;
    const r = await apiPost("/racelink/api/devices/update-meta", {macs, groupId: gid});
    if(r && r.task){
      // 2026-04-29: bulk-move now runs as a TaskManager job. Mark
      // the click as "in flight" so updateTask's done/error branch
      // fires the summary toast for THIS click (not for any older
      // bulk that finished while we were idle).
      state._bulkSetGroupActive = true;
      showToast(`Moving ${macs.length} device${macs.length === 1 ? "" : "s"} → ${groupLabel}…`);
      try{ updateTask(r.task); }catch(e){ console.error(e); }
    } else if(r && r.busy){
      showToast(`Busy: ${r.task?.name || "task"} is running`);
    } else if(r && !r.ok){
      showToastError(r.error || "Bulk move failed.");
    }
  });

  // Discover modal

// Node CONFIG (unicast only, requires exactly one selection)
$("#btnNodeCfgSend").addEventListener("click", async ()=>{
  const macs = Array.from(state.selected);
  if(macs.length !== 1){
    showToastError("Select exactly one device for CONFIG commands.");
    return;
  }
  const sel = ($("#nodeCfgCmd").value || "").trim();
  const parts = sel.split(":");
  const option = Number(parts[0] || 0);
  const data0 = Number(parts[1] || 0);
  const data1 = Number(parts[2] || 0);
  const data2 = Number(parts[3] || 0);
  const data3 = Number(parts[4] || 0);

  // C2: confirms for the two destructive node-config ops are routed
  // through ``confirmDestructive`` for consistency. The wording stays
  // close to the pre-fix prompts so muscle memory carries over.
  if(option === 0x80){
    if(!confirmDestructive("Forget the learned Master MAC on the selected node? "
      + "The node will need to re-pair on its next discovery.")) return;
  } else if(option === 0x81){
    if(!confirmDestructive("Reboot the selected node now? "
      + "It will be unreachable for a few seconds.")) return;
  }

  const r = await apiPost("/racelink/api/config", {mac: macs[0], option, data0, data1, data2, data3});
  if(r.busy){
    showToast(`Busy: ${r.task?.name || "task"} is running`);
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
    // ``discoveryGroup`` is the OPC_DEVICES filter (Stage-2 groupMatch
    // on the device side). Pass through as-is — the API accepts an
    // int 0..254 or the literal string "all" (sweep). Default 0.
    const rawIn = ($("#discoverInGroup")?.value ?? "0").trim();
    const discoveryGroup = (rawIn === "all") ? "all" : (Number(rawIn) || 0);

    $("#discoverResult").textContent = "Running…";
    const r = await apiPost("/racelink/api/discover", {
      targetGroupId, newGroupName, discoveryGroup,
    });

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
      showToast(`Busy: ${r.task?.name || "task"} is running`);
    }
  });

  // Startup
  (async ()=>{
    // Connect SSE first so we still get feedback even if initial REST loads
    // fail. SSE runs on both pages so the Scenes page also gets refresh
    // events for the SCENES topic and master/task pill updates.
    connectEvents();

    // R5c: Devices-only initial load. The Scenes page has no device table /
    // group sidebar / config-display options to populate, so skip the heavy
    // loadAll() roundtrip there.
    if(RL_PAGE !== "devices"){
      return;
    }

    renderConfigDisplayOptions();

    // Batch B: wire the ↻ refresh affordance once per page load. Has to
    // happen after the DOM is ready (the button lives in the masterbar
    // header rendered by Flask).
    try{ bindMasterRefresh(); }catch(_){}

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

  // =====================================================================
  // Phase B: RL-Presets editor (dlgRlPresets)
  // =====================================================================

  const RL_PRESET_VARS = [
    "mode", "speed", "intensity",
    "custom1", "custom2", "custom3",
    "check1", "check2", "check3",
    "palette",
    "color1", "color2", "color3",
    "brightness",
  ];
  const RL_PRESET_FLAGS = ["arm_on_sync", "force_tt0", "force_reapply", "offset_mode"];

  let rlPresetUiSchema = null; // cached from /api/rl-presets/schema

  async function ensureRlPresetUiSchema(){
    if(rlPresetUiSchema) return rlPresetUiSchema;
    // Phase D: the editor schema lives on its own endpoint now. The
    // Specials ``wled_control`` action became a preset picker and no
    // longer carries the 14 field defs this form needs.
    const r = await apiGet("/racelink/api/rl-presets/schema");
    if(!r || !r.ok || !r.schema) return null;
    rlPresetUiSchema = {
      vars: r.schema.vars || RL_PRESET_VARS,
      ui: r.schema.ui || {},
      flags: Array.isArray(r.schema.flags) ? r.schema.flags : null,
      paletteColorRules: r.schema.palette_color_rules || null,
    };
    return rlPresetUiSchema;
  }

  async function loadRlPresets(){
    const r = await apiGet("/racelink/api/rl-presets");
    state.rlPresets.items = (r && r.ok && r.presets) ? r.presets : [];
    return state.rlPresets.items;
  }

  function rgbTupleToHex(rgb){
    if(!Array.isArray(rgb) || rgb.length !== 3) return "#000000";
    const h = v => Number(v & 0xFF).toString(16).padStart(2, "0");
    return `#${h(rgb[0])}${h(rgb[1])}${h(rgb[2])}`;
  }

  function valueForWidget(varKey, schemaUi, presetParams){
    // Convert the stored (canonical) preset value into a UI-input-friendly form.
    const v = presetParams ? presetParams[varKey] : undefined;
    if(v === undefined || v === null){
      return null;
    }
    const widget = schemaUi && schemaUi[varKey] && schemaUi[varKey].widget;
    if(widget === "color"){
      return rgbTupleToHex(v);
    }
    return v;
  }

  function buildRlPresetForm(container, schema, preset){
    container.innerHTML = "";
    const params = (preset && preset.params) || {};
    const flags = (preset && preset.flags) || {};

    // --- Label row ------------------------------------------------------
    const labelRow = document.createElement("div");
    labelRow.className = "rl-special-fn-row";
    const labelLabel = document.createElement("label");
    labelLabel.textContent = "Label";
    const labelInputWrap = document.createElement("div");
    labelInputWrap.className = "rl-special-inputs";
    const labelInput = document.createElement("input");
    labelInput.type = "text";
    labelInput.id = "rlPresetLabelInput";
    labelInput.value = (preset && preset.label) || "";
    labelInput.style.width = "100%";
    labelInputWrap.appendChild(labelInput);
    labelRow.appendChild(labelLabel);
    labelRow.appendChild(labelInputWrap);
    labelRow.appendChild(document.createElement("div"));
    container.appendChild(labelRow);

    // --- Parameter row (reuse buildSpecialVarInput) ---------------------
    const row = document.createElement("div");
    row.className = "rl-special-fn-row";
    const rowLabel = document.createElement("label");
    rowLabel.textContent = "Parameters";
    const inputsWrap = document.createElement("div");
    inputsWrap.className = "rl-special-inputs";
    const inputMeta = [];
    const vars = (schema && schema.vars) || RL_PRESET_VARS;
    const ui = (schema && schema.ui) || {};

    // Dummy "dev" with preset params so buildSpecialVarInput picks them up.
    const dev = {};
    for(const k of vars){
      const uiMeta = ui[k] || {};
      const val = valueForWidget(k, ui, params);
      if(val !== null){ dev[k] = val; }
    }

    vars.forEach(varKey => {
      const uiMeta = ui[varKey] || {};
      const fieldWrap = document.createElement("div");
      fieldWrap.className = "rl-special-input";
      fieldWrap.dataset.field = varKey;
      const fieldLabel = document.createElement("span");
      fieldLabel.className = "rl-special-input-label";
      fieldLabel.textContent = varKey;
      fieldLabel.dataset.defaultLabel = varKey;
      const { input, widget } = buildSpecialVarInput({
        varKey, varMeta: {}, uiMeta, dev,
      });
      fieldWrap.appendChild(fieldLabel);
      fieldWrap.appendChild(input);
      inputsWrap.appendChild(fieldWrap);
      inputMeta.push({ key: varKey, input, widget, uiMeta, wrap: fieldWrap, labelEl: fieldLabel });
    });

    row.appendChild(rowLabel);
    row.appendChild(inputsWrap);
    row.appendChild(document.createElement("div"));
    container.appendChild(row);

    // A12 re-wire: mode-select change -> toggle visibility + labels.
    // Some built-in palettes ("* Color 1", "* Colors 1&2", "* Color Gradient",
    // "* Colors Only") force-show extra color slots regardless of the
    // effect's static metadata — mirrors WLED's updateSelectedPalette()
    // in wled00/data/index.js. The exact thresholds come from the schema
    // (auto-extracted by gen_wled_metadata.py); the literals below are
    // a safety fallback for older backends that don't ship the rule yet.
    const modeMeta = inputMeta.find(m => m.key === "mode");
    const paletteMeta = inputMeta.find(m => m.key === "palette");
    const COLOR_KEY_TO_SLOT = { color1: 0, color2: 1, color3: 2 };
    const DEFAULT_COLOR_LABEL = ["Fx", "Bg", "Cs"];
    const paletteRules = (schema && schema.paletteColorRules) || {
      force_slot_min_palette: [2, 3, 4],
      max_palette_id: 5,
    };
    const paletteForcesSlot = (paletteId, slotIndex) => {
      const p = Number(paletteId);
      if(!Number.isFinite(p)) return false;
      if(p > paletteRules.max_palette_id) return false;
      const min = paletteRules.force_slot_min_palette[slotIndex];
      return min !== undefined && p >= min;
    };
    if(modeMeta && modeMeta.uiMeta && Array.isArray(modeMeta.uiMeta.options)){
      const optionByValue = new Map(modeMeta.uiMeta.options.map(o => [String(o.value), o]));
      const apply = () => {
        const selected = optionByValue.get(String(modeMeta.input.value));
        const slots = selected && selected.slots ? selected.slots : null;
        const paletteId = paletteMeta ? paletteMeta.input.value : null;
        for(const m of inputMeta){
          if(m.key === "mode") continue;
          const slot = slots ? slots[m.key] : null;
          const effectUses = slot ? Boolean(slot.used) : true;
          const slotIndex = COLOR_KEY_TO_SLOT[m.key];
          const paletteForces = slotIndex !== undefined && paletteForcesSlot(paletteId, slotIndex);
          const used = effectUses || paletteForces;
          m.wrap.style.display = used ? "" : "none";
          if(m.labelEl){
            let label;
            if(slotIndex !== undefined){
              if(effectUses){
                const custom = slot && typeof slot.label === "string" && slot.label ? slot.label : null;
                label = custom || DEFAULT_COLOR_LABEL[slotIndex];
              }else if(paletteForces){
                label = String(slotIndex + 1);
              }
            }
            if(!label){
              const custom = slot && typeof slot.label === "string" && slot.label ? slot.label : null;
              label = custom || m.labelEl.dataset.defaultLabel || m.key;
            }
            m.labelEl.textContent = label;
          }
        }
      };
      modeMeta.input.addEventListener("change", apply);
      if(paletteMeta) paletteMeta.input.addEventListener("change", apply);
      apply();
    }

    // --- Flags row ------------------------------------------------------
    const flagRow = document.createElement("div");
    flagRow.className = "rl-special-fn-row";
    const flagLabel = document.createElement("label");
    flagLabel.textContent = "Flags";
    const flagInputs = document.createElement("div");
    flagInputs.className = "rl-special-inputs";
    const flagMeta = [];
    // Prefer labels from the serialized schema when available -- falls back
    // to the bare key for older backends that don't yet publish `flags`.
    const flagsFromSchema = (schema && Array.isArray(schema.flags)) ? schema.flags : null;
    const flagEntries = flagsFromSchema
      ? flagsFromSchema.map(f => ({ key: f.key, label: f.label || f.key }))
      : RL_PRESET_FLAGS.map(fk => ({ key: fk, label: fk }));
    flagEntries.forEach(({ key: fk, label: flabel }) => {
      const wrap = document.createElement("label");
      wrap.className = "rl-toggle-wrap";
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.checked = Boolean(flags[fk]);
      const labelNode = document.createElement("span");
      labelNode.textContent = flabel;
      wrap.appendChild(cb);
      wrap.appendChild(labelNode);
      flagInputs.appendChild(wrap);
      flagMeta.push({ key: fk, input: cb });
    });
    flagRow.appendChild(flagLabel);
    flagRow.appendChild(flagInputs);
    flagRow.appendChild(document.createElement("div"));
    container.appendChild(flagRow);

    // --- Action bar -----------------------------------------------------
    const actions = document.createElement("div");
    actions.className = "rl-special-actions";
    actions.style.marginTop = "12px";
    const saveBtn = document.createElement("button");
    saveBtn.type = "button";
    saveBtn.id = "rlPresetSaveBtn";
    saveBtn.textContent = preset && preset.key ? "Save" : "Create";
    actions.appendChild(saveBtn);
    if(preset && preset.key){
      const dupBtn = document.createElement("button");
      dupBtn.type = "button";
      dupBtn.textContent = "Duplicate";
      dupBtn.addEventListener("click", async () => {
        const newLabel = prompt("Label for duplicate?", `${preset.label} copy`);
        if(!newLabel) return;
        const r = await apiPost(`/racelink/api/rl-presets/${preset.key}/duplicate`, {label: newLabel});
        if(!r.ok){
          $("#rlPresetsHint").textContent = r.error || "Duplicate failed.";
          return;
        }
        await refreshResource("rl_presets");
        selectRlPreset(r.preset.key);
      });
      actions.appendChild(dupBtn);
      const delBtn = document.createElement("button");
      delBtn.type = "button";
      delBtn.textContent = "Delete";
      delBtn.addEventListener("click", async () => {
        if(!confirm(`Delete preset "${preset.label}"?`)) return;
        const r = await apiDelete(`/racelink/api/rl-presets/${preset.key}`);
        if(!r.ok){
          $("#rlPresetsHint").textContent = r.error || "Delete failed.";
          return;
        }
        state.rlPresets.selectedKey = null;
        await refreshResource("rl_presets");
        renderRlPresetEditor(null);
      });
      actions.appendChild(delBtn);
    }
    container.appendChild(actions);

    saveBtn.addEventListener("click", async () => {
      // Collect parameter values (inputs visible or hidden are all saved; the
      // "hidden" semantics only applies at send-time to pick fieldMask bits).
      const outParams = {};
      for(const m of inputMeta){
        if(m.widget === "toggle"){
          const cb = m.input.rlInput || m.input;
          outParams[m.key] = Boolean(cb.checked);
        }else if(m.widget === "slider"){
          const sl = m.input.rlInput || m.input;
          const n = Number(sl.value);
          outParams[m.key] = Number.isFinite(n) ? n : null;
        }else if(m.widget === "color"){
          const hex = String(m.input.value || "#000000").replace(/^#/, "");
          if(/^[0-9a-fA-F]{6}$/.test(hex)){
            outParams[m.key] = [
              parseInt(hex.slice(0,2), 16),
              parseInt(hex.slice(2,4), 16),
              parseInt(hex.slice(4,6), 16),
            ];
          }else{
            outParams[m.key] = null;
          }
        }else{
          // select / number
          const v = m.input.value;
          const n = Number(v);
          outParams[m.key] = Number.isFinite(n) ? n : v;
        }
      }
      const outFlags = {};
      for(const f of flagMeta){ outFlags[f.key] = Boolean(f.input.checked); }
      const label = ($("#rlPresetLabelInput").value || "").trim();
      if(!label){
        $("#rlPresetsHint").textContent = "Label is required.";
        return;
      }
      let r;
      if(preset && preset.key){
        r = await apiPut(`/racelink/api/rl-presets/${preset.key}`, {label, params: outParams, flags: outFlags});
      }else{
        r = await apiPost(`/racelink/api/rl-presets`, {label, params: outParams, flags: outFlags});
      }
      if(!r.ok){
        $("#rlPresetsHint").textContent = r.error || "Save failed.";
        return;
      }
      $("#rlPresetsHint").textContent = `Saved "${r.preset.label}"`;
      state.rlPresets.selectedKey = r.preset.key;
      await refreshResource("rl_presets");
      renderRlPresetEditor(r.preset);
    });
  }

  function renderRlPresetList(){
    const listEl = $("#rlPresetList");
    if(!listEl) return;
    listEl.innerHTML = "";
    if(!state.rlPresets.items.length){
      const empty = document.createElement("li");
      empty.className = "muted";
      empty.textContent = "(no presets yet)";
      listEl.appendChild(empty);
      return;
    }
    state.rlPresets.items.forEach(p => {
      const li = document.createElement("li");
      li.textContent = p.label || p.key;
      li.dataset.key = p.key;
      if(p.key === state.rlPresets.selectedKey){
        li.classList.add("active");
      }
      li.addEventListener("click", () => selectRlPreset(p.key));
      listEl.appendChild(li);
    });
  }

  async function renderRlPresetEditor(preset){
    const editor = $("#rlPresetEditor");
    if(!editor) return;
    const schema = await ensureRlPresetUiSchema();
    if(!schema){
      editor.innerHTML = "<p class=\"muted\">Failed to load form schema.</p>";
      return;
    }
    buildRlPresetForm(editor, schema, preset);
  }

  function selectRlPreset(key){
    state.rlPresets.selectedKey = key;
    const preset = state.rlPresets.items.find(p => p.key === key) || null;
    renderRlPresetList();
    renderRlPresetEditor(preset);
  }

  const dlgRlPresets = $("#dlgRlPresets");
  $("#btnRlPresets").addEventListener("click", async () => {
    $("#rlPresetsHint").textContent = "";
    await ensureRlPresetUiSchema();
    await loadRlPresets();
    if(!state.rlPresets.selectedKey && state.rlPresets.items.length){
      state.rlPresets.selectedKey = state.rlPresets.items[0].key;
    }
    renderRlPresetList();
    const selected = state.rlPresets.items.find(p => p.key === state.rlPresets.selectedKey) || null;
    await renderRlPresetEditor(selected);
    dlgRlPresets.showModal();
  });

  $("#btnRlPresetNew").addEventListener("click", async () => {
    state.rlPresets.selectedKey = null;
    renderRlPresetList();
    await renderRlPresetEditor(null);
    $("#rlPresetsHint").textContent = "New preset — enter label and Create.";
  });

  // ---- resource refresh: register loaders + subscribers -----------------
  // Each loader fetches a resource from the server. Each subscriber is a
  // re-render hook that depends on that resource. Subscribers run after
  // the loader has refreshed ``state``. Cross-resource cascades (e.g.
  // RL-preset changes invalidate the device-options dropdown which is
  // a derived view inside ``state.specials``) are explicit
  // ``refreshResource`` calls inside subscribers.
  registerLoader("groups",     loadGroups);
  registerLoader("devices",    loadDevices);
  registerLoader("specials",   loadSpecials);
  registerLoader("rl_presets", loadRlPresets);

  // Devices-page renderers are already invoked from within the load
  // helpers, but registering them as subscribers means SSE-driven and
  // local mutations follow the exact same path no matter who fired it.
  // The renderers null-guard their DOM lookups so calls on the Scenes
  // page (no device table, no group sidebar) are no-ops.
  subscribeRefresh("rl_presets", async () => {
    // RL-preset list changes invalidate the WLED Control dropdown in the
    // Device Options dialog, whose options are server-resolved into
    // ``state.specials``. Re-fetch and re-render that dialog too.
    await refreshResource("specials");
    if(document.getElementById("rlPresetList")){
      renderRlPresetList();
    }
  });
  subscribeRefresh("specials", () => {
    if(state.specialDevice){
      renderSpecialTabs();
    }
  });

  // R5c: expose the shared helpers to scenes.js (loaded on /racelink/scenes).
  // Scenes pages reuse apiGet/apiPost/apiPut/apiDelete and read from the
  // same ``state`` object. The Devices page uses these directly via the IIFE
  // closure — exposing them is harmless there.
  window.RL = {
    apiGet, apiPost, apiPut, apiDelete,
    withBasePath,
    state,
    // C2 + C3: scenes.js can route through the same toast / confirm
    // helpers so message styling and button-confirm wording stay
    // consistent across the two pages.
    showToast, showToastError, confirmDestructive,
    // C7: shared busy indicator. setBusy is null-guarded so calls
    // from scenes.js (where the Devices controls don't exist) are
    // safe — only the elements that exist on the current page get
    // disabled.
    setBusy,
    // Resource-refresh registry: scenes.js registers its own loader
    // (``scenes``) and subscribers (target picker, schema cache).
    registerLoader, subscribeRefresh, refreshResource,
  };

  // =====================================================================
  // Scene Manager — moved to scenes.js (R5c). The dlgScenes modal in
  // racelink.html was deleted in R5f; the editor now lives at its own
  // /racelink/scenes URL. The SSE refresh handler above already calls
  // ``window.__rlScenesRefresh()`` which scenes.js installs on page load.
  // =====================================================================

})();
