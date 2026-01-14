(function(){
  const $ = (sel, ctx=document) => ctx.querySelector(sel);
  const $$ = (sel, ctx=document) => Array.from(ctx.querySelectorAll(sel));

  const fmt = {
    num: v => (v===null || v===undefined || isNaN(v)) ? "" : String(v),
    hex2: v => ("0" + (Number(v) & 0xFF).toString(16).toUpperCase()).slice(-2),
  };

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
    fwUploads: { fwId: null, presetsId: null, cfgId: null },
    configDisplay: loadConfigDisplay(),
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

  async function loadAll(){
    await Promise.all([loadGroups(), loadDevices()]);
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
    state.groups.forEach(gr => {
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
      rows = rows.filter(r => Number(r.groupId)===Number(state.selGroupId));
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
      const configByte = Number(r.configByte ?? 0) & 0xFF;
      const selectedConfigs = [];
      const tooltipConfigs = [];
      CONFIG_SETTINGS.forEach(setting => {
        const enabled = !!(configByte & (1 << setting.bit));
        const label = `${setting.label}: ${enabled ? "On" : "Off"}`;
        if(state.configDisplay.has(setting.bit)){
          selectedConfigs.push(`<span class="tag ${enabled ? "ok" : "off"}">${label}</span>`);
        }else{
          tooltipConfigs.push(label);
        }
      });
      const configCell = selectedConfigs.length ? selectedConfigs.join(" ") : "-";
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
        <td>${r.caps ?? ""}</td>
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

      const p = [
        `${name}…`,
        mparts.length ? `(${mparts.join(", ")})` : "",
        `replies ${t.rx_replies||0}`,
        `windows ${t.rx_windows||0}`,
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

  }

  // Firmware Update modal
  function fwDialogCounts(){
    $("#fwSelCount").textContent = String(state.selected.size || 0);
    const filtered = (state.selGroupId === null || state.selGroupId === undefined)
      ? state.devices.length
      : state.devices.filter(d => Number(d.groupId) === Number(state.selGroupId)).length;
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
      return state.devices.filter(d => Number(d.groupId) === Number(state.selGroupId)).map(d => d.addr).filter(Boolean);
    }
    return state.devices.map(d => d.addr).filter(Boolean);
  }

  function fwResetUploadsUi(){
    state.fwUploads = { fwId: null, presetsId: null, cfgId: null };
    $("#fwBinInfo").textContent = "";
    $("#fwPresetsInfo").textContent = "";
    $("#fwCfgInfo").textContent = "";
    $("#fwHint").textContent = "";
    $("#fwBin").value = "";
    $("#fwPresets").value = "";
    $("#fwCfg").value = "";
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

  $("#btnFwUpdate").addEventListener("click", ()=>{
    try{
    fwDialogCounts();
    fwResetUploadsUi();

    // Enable/disable optional controls based on checkboxes
    $("#fwPresets").disabled = !$("#fwDoPresets").checked;
    $("#btnFwUploadPresets").disabled = !$("#fwDoPresets").checked;
    $("#fwCfg").disabled = !$("#fwDoCfg").checked;
    $("#btnFwUploadCfg").disabled = !$("#fwDoCfg").checked;

    $("#dlgFwUpdate").showModal();
    } catch(e){
      console.error(e);
      alert("Firmware dialog failed to open. Check console for details.");
    }
  });

  $("#fwDoPresets").addEventListener("change", ()=>{
    const on = $("#fwDoPresets").checked;
    $("#fwPresets").disabled = !on;
    $("#btnFwUploadPresets").disabled = !on;
    if(!on){
      state.fwUploads.presetsId = null;
      $("#fwPresetsInfo").textContent = "";
      $("#fwPresets").value = "";
    }
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

  $("#btnFwUploadPresets").addEventListener("click", async ()=>{
    const id = await fwUpload("presets", $("#fwPresets"), $("#fwPresetsInfo"));
    if(id) state.fwUploads.presetsId = id;
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
    if(!state.fwUploads.fwId){
      alert("Please upload the firmware file to the host first.");
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

    const body = { macs, fwId: state.fwUploads.fwId, baseUrl, retries, hostWifiEnable, hostWifiRestore,
      wifi: { connName: wifiConnName, ssid: wifiSsid, iface: wifiIface, timeoutS: wifiTimeoutS }
    };

    if($("#fwDoPresets").checked){
      if(!state.fwUploads.presetsId){
        alert("Presets enabled but presets.json is not uploaded yet.");
        return;
      }
      body.presetsId = state.fwUploads.presetsId;
    }
    if($("#fwDoCfg").checked){
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
