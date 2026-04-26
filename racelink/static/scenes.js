/**
 * Scene Manager — page-scoped script for /racelink/scenes (R5).
 *
 * This file replaces the modal-dialog scene editor that previously lived
 * inside racelink.js. Loads on the dedicated /scenes page only. Reuses
 * the shared helpers (apiGet, apiPost, apiPut, apiDelete, state) and the
 * RL-Preset modal opener (btnRlPresets) by reading them off ``window.RL``
 * exported by racelink.js.
 */
(function(){
  // racelink.js runs first and publishes window.RL with the shared helpers.
  // If it didn't (e.g. wrong page-load order), bail out loudly so the
  // failure is obvious instead of producing silently-broken UI.
  const RL = window.RL;
  if(!RL){
    console.error("[scenes] window.RL not available — racelink.js must load before scenes.js");
    return;
  }
  const { apiGet, apiPost, apiPut, apiDelete, state } = RL;
  const $ = (sel, ctx=document) => ctx.querySelector(sel);

  const SCENE_KIND_LABELS = {
    rl_preset: "Apply RL Preset",
    wled_preset: "Apply WLED Preset",
    wled_control: "Apply WLED Control",
    startblock: "Startblock Control",
    sync: "SYNC (fire armed)",
    delay: "Delay",
    offset_group: "Offset Group",
  };
  const SCENE_KINDS_ORDER = [
    "rl_preset", "wled_preset", "wled_control",
    "startblock", "sync", "delay", "offset_group",
  ];
  // Kinds allowed inside an offset_group container — must mirror
  // OFFSET_GROUP_CHILD_KINDS in racelink/services/scenes_service.py.
  const OFFSET_GROUP_CHILD_KINDS = ["rl_preset", "wled_preset", "wled_control"];
  const SCENE_MAX_ACTIONS = 20;

  function setScenesHint(text){
    const el = $("#scenesHint");
    if(el) el.textContent = text || "";
  }

  async function loadScenes(){
    const r = await apiGet("/racelink/api/scenes");
    state.scenes.items = (r && r.ok && r.scenes) ? r.scenes : [];
    return state.scenes.items;
  }

  async function ensureScenesSchema(){
    if(state.scenes.schema) return state.scenes.schema;
    const r = await apiGet("/racelink/api/scenes/editor-schema");
    if(!r || !r.ok) return null;
    state.scenes.schema = {
      kinds: Array.isArray(r.kinds) ? r.kinds : [],
      flagKeys: Array.isArray(r.flag_keys) ? r.flag_keys : [],
      targetKinds: Array.isArray(r.target_kinds) ? r.target_kinds : ["group", "device"],
      offsetGroup: r.offset_group || null,
      lora: r.lora || null,
    };
    return state.scenes.schema;
  }

  // ---- offset_group container helpers (formula-aware) -----------------
  // Persisted shape (mirrors racelink/services/scenes_service.py):
  //   {
  //     kind: "offset_group",
  //     groups: "all" | [<int>, ...],
  //     offset: { mode, ...mode-params },
  //     actions: [<rl_preset|wled_preset|wled_control child>, ...]
  //   }
  //
  // ``mode`` is one of: explicit | linear | vshape | modulo | none.
  // ``Linear/VShape/Modulo + groups: "all"`` is the broadcast-formula
  // wire path: one OPC_OFFSET configures every device. Legacy
  // ``target.kind == "groups_offset"`` actions auto-migrate to this
  // container shape on load (see _migrate_legacy_groups_offset_action
  // in scenes_service.py).

  const OFFSET_FORMULA_MODES = ["explicit", "linear", "vshape", "modulo", "none"];
  const FORMULA_MODE_LABELS = {
    explicit: "Explicit (per-group)",
    linear:   "Linear cascade",
    vshape:   "From-center (V-shape)",
    modulo:   "Repeating (modulo)",
    none:     "Clear / disable",
  };
  const OFFSET_FORMULA_DEFAULTS = { mode: "linear", base_ms: 0, step_ms: 100, center: 0, cycle: 4 };

  function clampOffsetMs(v){
    const bounds = (state.scenes.schema && state.scenes.schema.offsetGroup && state.scenes.schema.offsetGroup.offset_ms) || {min:0, max:65535};
    const n = Number(v);
    if(!Number.isFinite(n)) return bounds.min;
    return Math.max(bounds.min, Math.min(bounds.max, Math.round(n)));
  }

  function clampS16(v){
    const n = Math.round(Number(v) || 0);
    return Math.max(-32768, Math.min(32767, n));
  }

  // Group id 0 is the synthetic "Unconfigured" bucket — devices that the
  // gateway has seen but the operator hasn't assigned to a productive
  // group yet. It must NOT appear as a selectable scene target in any
  // editor dropdown (operators never want to fire effects at the
  // unassigned pool). Filtering happens here so every consumer of
  // ``knownGroupIds`` / ``selectableGroups`` is consistent.
  const UNCONFIGURED_GROUP_ID = 0;

  function isSelectableGroup(g){
    if(!g) return false;
    const id = (typeof g.id === "number") ? g.id : g.groupId;
    if(!Number.isFinite(id)) return false;
    if(id < 0 || id > 254) return false;
    if(id === UNCONFIGURED_GROUP_ID) return false;
    return true;
  }

  function selectableGroups(){
    return (state.groups || []).filter(isSelectableGroup);
  }

  function knownGroupIds(){
    return selectableGroups()
      .map(g => (typeof g.id === "number") ? g.id : g.groupId)
      .sort((a, b) => a - b);
  }

  // Mirrors racelink/domain/offset_formula.py — both sides must produce
  // byte-identical results. Used for the live preview AND for sparse
  // selections (the runner runs the same logic Python-side).
  function evaluateOffsetMs(spec, groupId){
    const gid = (Number(groupId) | 0) & 0xFF;
    const mode = (spec && spec.mode || "none").toLowerCase();
    const clamp = v => Math.max(0, Math.min(0xFFFF, v | 0));
    if(mode === "none") return 0;
    if(mode === "explicit") return clamp(Number(spec.offset_ms) | 0);
    const base = Number(spec.base_ms) | 0;
    const step = Number(spec.step_ms) | 0;
    if(mode === "linear") return clamp(base + gid * step);
    if(mode === "vshape"){
      const center = (Number(spec.center) | 0) & 0xFF;
      return clamp(base + Math.abs(gid - center) * step);
    }
    if(mode === "modulo"){
      const rawCycle = Number(spec.cycle) | 0;
      const cycle = rawCycle > 0 ? rawCycle : 1;
      return clamp(base + (gid % cycle) * step);
    }
    return 0;
  }

  function ensureOffsetGroupShape(action){
    // Container actions carry ``groups`` and ``offset`` directly (no
    // nested ``target``). Seed sensible defaults if a draft is missing
    // them so the panel can render without crashing.
    if(action.groups === undefined){
      action.groups = "all";
    }
    if(!action.offset || typeof action.offset !== "object"){
      action.offset = { ...OFFSET_FORMULA_DEFAULTS };
    }
    if(!Array.isArray(action.actions)){
      action.actions = [];
    }
    return action;
  }

  function getSelectedIdsFromHolder(holder){
    const groups = holder.groups;
    if(groups === "all" || groups === 255){
      return { all: true, ids: knownGroupIds() };
    }
    if(Array.isArray(groups)){
      const ids = groups.map(g => (typeof g === "number") ? g : (g && g.id))
        .filter(n => Number.isFinite(n) && n >= 0 && n <= 254);
      return { all: false, ids: ids.slice().sort((a, b) => a - b) };
    }
    return { all: false, ids: [] };
  }

  // Sparse mode-explicit values keyed by group id. Used by the Explicit
  // editor so the user's per-group entries survive selection toggles.
  function explicitValuesMapFromHolder(holder){
    const off = (holder && holder.offset) || {};
    const out = new Map();
    if(off.mode === "explicit" && Array.isArray(off.values)){
      for(const v of off.values){
        if(v && Number.isFinite(v.id)) out.set(Number(v.id), Number(v.offset_ms) | 0);
      }
    }
    return out;
  }

  // Strip ``_ui`` from an action tree before save; the server drops unknown
  // keys but we send a clean payload for cleaner debugging / API logs.
  // Container actions are recursed so nested children also get cleaned.
  function stripUiBeforeSave(actions){
    return (actions || []).map(a => {
      const out = { ...a };
      delete out._ui;
      if(out.kind === "offset_group" && Array.isArray(out.actions)){
        out.actions = stripUiBeforeSave(out.actions);
      }
      return out;
    });
  }

  // ---- cost estimator (debounced, server-backed) ---------------------
  // Server is the source of truth; the UI debounces to ~300ms after each
  // edit. Per user feedback point 3 the UI surfaces the active LoRa
  // parameters in a tooltip on the cost badges.

  const COST_BADGE_DEBOUNCE_MS = 300;
  let _costFetchTimer = null;
  let _costFetchSeq = 0;

  function formatCost(cost){
    if(!cost) return "≈ —";
    const ms = Math.round(cost.airtime_ms || 0);
    return `≈ ${cost.packets} pkts · ${cost.bytes} B · ${ms} ms`;
  }

  function loraTooltip(){
    const lora = state.scenes.schema && state.scenes.schema.lora;
    if(!lora) return "";
    const bw = (lora.bw_hz / 1000).toFixed(0);
    return `at SF${lora.sf}/${bw} kHz/CR4:${lora.cr}` +
           ` · bytes include Header7 + USB framing; airtime via Semtech AN1200.13`;
  }

  function applyCostPayload(seq, payload){
    if(seq !== _costFetchSeq) return;        // stale response
    if(!payload || !payload.ok) return;
    const tot = document.getElementById("sceneCostTotal");
    if(tot){
      tot.textContent = "Total " + formatCost(payload.total);
      tot.title = loraTooltip();
    }
    const editor = document.getElementById("sceneEditor");
    if(!editor) return;
    (payload.per_action || []).forEach((cost, idx) => {
      const row = editor.querySelector(`[data-action-idx="${idx}"]`);
      if(!row) return;
      const badge = row.querySelector(".rl-scene-action-cost");
      if(!badge) return;
      badge.textContent = formatCost(cost);
      badge.title = loraTooltip();
    });
  }

  function scheduleCostEstimate(){
    if(_costFetchTimer) clearTimeout(_costFetchTimer);
    _costFetchTimer = setTimeout(fetchCostEstimate, COST_BADGE_DEBOUNCE_MS);
  }

  async function fetchCostEstimate(){
    const draft = state.scenes.draft;
    if(!draft) return;
    const seq = ++_costFetchSeq;
    try{
      const body = {
        label: draft.label || "draft",
        actions: stripUiBeforeSave(draft.actions || []),
      };
      const r = await apiPost("/racelink/api/scenes/estimate", body);
      applyCostPayload(seq, r);
    }catch(e){
      // Estimator is observability — never block the editor. Log and
      // leave the badges showing the previous values.
      console.warn("[scenes] estimate fetch failed", e);
    }
  }

  async function loadGroupsAndDevicesForTargetPicker(){
    // The scenes page doesn't render the device table, but the action target
    // picker still needs current group/device lists. Fetch them once on
    // editor open; SSE refreshes update them when groups/devices change.
    try{
      const [g, d] = await Promise.all([
        apiGet("/racelink/api/groups"),
        apiGet("/racelink/api/devices"),
      ]);
      if(g && g.ok) state.groups = g.groups || [];
      if(d && d.ok) state.devices = d.devices || [];
    }catch(e){
      console.error("[scenes] failed to fetch groups/devices for target picker", e);
    }
  }

  function findKindMeta(kind){
    if(!state.scenes.schema) return null;
    return state.scenes.schema.kinds.find(k => k.kind === kind) || null;
  }

  function defaultActionForKind(kind){
    const meta = findKindMeta(kind);
    const action = { kind };
    if(kind === "delay"){
      action.duration_ms = 0;
      return action;
    }
    if(kind === "sync"){
      return action;
    }
    if(kind === "offset_group"){
      // Default to "all groups + Linear, base=0, step=100" — the most common
      // operator intent and the cheapest wire path (one broadcast packet).
      // Children list starts empty; the user adds wled_control / wled_preset
      // children via the in-container Add row.
      action.groups = "all";
      action.offset = { mode: "linear", base_ms: 0, step_ms: 100 };
      action.actions = [];
      return action;
    }
    action.target = { kind: "group", value: 1 };
    action.params = {};
    if(meta && meta.supports_flags_override){
      action.flags_override = {};
    }
    return action;
  }

  function defaultOffsetGroupChild(kind){
    const action = { kind, target: { kind: "scope" }, params: {} };
    const meta = findKindMeta(kind);
    if(meta && meta.supports_flags_override){
      action.flags_override = {};
    }
    return action;
  }

  function cloneAction(action){
    return JSON.parse(JSON.stringify(action || {}));
  }

  function renderSceneList(){
    const listEl = $("#sceneList");
    if(!listEl) return;
    listEl.innerHTML = "";
    if(!state.scenes.items.length){
      const empty = document.createElement("li");
      empty.className = "muted";
      empty.textContent = "(no scenes yet)";
      listEl.appendChild(empty);
      return;
    }
    state.scenes.items.forEach(s => {
      const li = document.createElement("li");
      li.textContent = `${s.label} (${(s.actions || []).length})`;
      li.dataset.key = s.key;
      if(s.key === state.scenes.selectedKey) li.classList.add("active");
      li.addEventListener("click", () => selectScene(s.key));
      listEl.appendChild(li);
    });
  }

  function selectScene(key){
    state.scenes.selectedKey = key;
    state.scenes.lastRunResult = null;
    const scene = state.scenes.items.find(s => s.key === key) || null;
    state.scenes.draft = scene ? cloneAction(scene) : null;
    renderSceneList();
    renderSceneEditor();
  }

  function newSceneDraft(){
    state.scenes.selectedKey = null;
    state.scenes.lastRunResult = null;
    state.scenes.draft = { id: null, key: null, label: "", actions: [] };
    renderSceneList();
    renderSceneEditor();
  }

  function renderSceneEditor(){
    const editor = $("#sceneEditor");
    if(!editor) return;
    editor.innerHTML = "";
    const draft = state.scenes.draft;
    if(!draft){
      const p = document.createElement("p");
      p.className = "muted";
      p.textContent = "Select a scene on the left, or create a new one.";
      editor.appendChild(p);
      return;
    }

    // --- Meta row -------------------------------------------------------
    const meta = document.createElement("div");
    meta.className = "rl-scene-meta";

    const labelLbl = document.createElement("label");
    labelLbl.textContent = "Label";
    const labelIn = document.createElement("input");
    labelIn.type = "text";
    labelIn.value = draft.label || "";
    labelIn.id = "sceneLabelInput";
    labelIn.style.minWidth = "240px";
    meta.appendChild(labelLbl);
    meta.appendChild(labelIn);

    if(draft.key){
      const keyInfo = document.createElement("span");
      keyInfo.className = "muted";
      keyInfo.textContent = `key: ${draft.key}`;
      meta.appendChild(keyInfo);
    }

    // Scene-level cost badge — populated asynchronously via the
    // ``/api/scenes/estimate`` endpoint after each edit. Tooltip surfaces
    // the active LoRa params per user feedback point 3.
    const costBadge = document.createElement("span");
    costBadge.className = "rl-scene-cost-total muted";
    costBadge.id = "sceneCostTotal";
    costBadge.textContent = "≈ —";
    meta.appendChild(costBadge);

    editor.appendChild(meta);

    // --- Run progress strip (shown after a run) -------------------------
    if(state.scenes.lastRunResult){
      const strip = document.createElement("div");
      strip.className = "rl-scene-progress";
      const status = document.createElement("span");
      const r = state.scenes.lastRunResult;
      status.textContent = r.ok ? "Last run: OK" : `Last run: ${r.error || "failed"}`;
      strip.appendChild(status);
      (r.actions || []).forEach(a => {
        const pip = document.createElement("span");
        pip.className = "pip " + (a.degraded ? "degraded" : (a.ok ? "ok" : "error"));
        // Display rebased to 1 to match the action-row labels (#1 #2 …).
        // The runner's ActionResult.index stays 0-based for log/structured output.
        const display = a.index + 1;
        pip.textContent = String(display);
        pip.title = `#${display} ${a.kind}${a.error ? " — " + a.error : ""} (${a.duration_ms}ms)`;
        strip.appendChild(pip);
      });
      editor.appendChild(strip);
    }

    // --- Action list ----------------------------------------------------
    const actionsContainer = document.createElement("div");
    actionsContainer.className = "rl-scenes-actions";
    (draft.actions || []).forEach((action, idx) => {
      actionsContainer.appendChild(buildSceneActionRow(action, idx, draft));
    });
    editor.appendChild(actionsContainer);
    // Hook up drag-reorder via SortableJS (vendored at static/vendor/).
    // ``handle: ".rl-scene-action-grip"`` confines drag to the dedicated grip
    // so click/keyboard interactions on the inner controls (selects, inputs,
    // buttons) keep their normal semantics. Falls back gracefully when the
    // vendor file is missing — the up/down buttons in each row remain.
    enableActionDragReorder(actionsContainer, draft);

    // --- Add-action row -------------------------------------------------
    const addRow = document.createElement("div");
    addRow.className = "rl-scene-add-row";
    const addLbl = document.createElement("span");
    addLbl.className = "muted";
    addLbl.textContent = `Add action (${(draft.actions || []).length}/${SCENE_MAX_ACTIONS}):`;
    const kindPicker = document.createElement("select");
    SCENE_KINDS_ORDER.forEach(k => {
      const opt = document.createElement("option");
      opt.value = k;
      opt.textContent = SCENE_KIND_LABELS[k] || k;
      kindPicker.appendChild(opt);
    });
    const addBtn = document.createElement("button");
    addBtn.type = "button";
    addBtn.textContent = "+ Add";
    addBtn.disabled = (draft.actions || []).length >= SCENE_MAX_ACTIONS;
    addBtn.addEventListener("click", () => {
      if((draft.actions || []).length >= SCENE_MAX_ACTIONS) return;
      draft.actions = draft.actions || [];
      draft.actions.push(defaultActionForKind(kindPicker.value));
      renderSceneEditor();
    });
    addRow.appendChild(addLbl);
    addRow.appendChild(kindPicker);
    addRow.appendChild(addBtn);
    editor.appendChild(addRow);

    // --- Action bar -----------------------------------------------------
    const actionBar = document.createElement("div");
    actionBar.className = "rl-special-actions";
    actionBar.style.marginTop = "12px";

    const saveBtn = document.createElement("button");
    saveBtn.type = "button";
    saveBtn.textContent = draft.key ? "Save" : "Create";
    saveBtn.addEventListener("click", saveSceneDraft);
    actionBar.appendChild(saveBtn);

    if(draft.key){
      const runBtn = document.createElement("button");
      runBtn.type = "button";
      runBtn.textContent = "Run";
      runBtn.addEventListener("click", () => runScene(draft.key));
      actionBar.appendChild(runBtn);

      const dupBtn = document.createElement("button");
      dupBtn.type = "button";
      dupBtn.textContent = "Duplicate";
      dupBtn.addEventListener("click", async () => {
        const newLabel = prompt("Label for duplicate?", `${draft.label} copy`);
        if(!newLabel) return;
        const r = await apiPost(`/racelink/api/scenes/${draft.key}/duplicate`, {label: newLabel});
        if(!r.ok){ setScenesHint(r.error || "Duplicate failed."); return; }
        await loadScenes();
        renderSceneList();
        selectScene(r.scene.key);
      });
      actionBar.appendChild(dupBtn);

      const delBtn = document.createElement("button");
      delBtn.type = "button";
      delBtn.textContent = "Delete";
      delBtn.addEventListener("click", async () => {
        if(!confirm(`Delete scene "${draft.label}"?`)) return;
        const r = await apiDelete(`/racelink/api/scenes/${draft.key}`);
        if(!r.ok){ setScenesHint(r.error || "Delete failed."); return; }
        await loadScenes();
        state.scenes.selectedKey = null;
        state.scenes.draft = null;
        state.scenes.lastRunResult = null;
        renderSceneList();
        renderSceneEditor();
      });
      actionBar.appendChild(delBtn);
    }

    editor.appendChild(actionBar);

    // Trigger a cost estimate after the editor finished rendering. Debounced
    // (~300ms) so successive renders during a typing burst coalesce into one
    // server call.
    scheduleCostEstimate();
  }

  function buildSceneActionRow(action, idx, draft){
    const row = document.createElement("div");
    row.className = "rl-scene-action-row";
    // R7: stamp the row with its position so __rlSceneProgress can target
    // it by querySelector without rebuilding the editor.
    row.dataset.actionIdx = String(idx);

    // R7: live status (set by __rlSceneProgress during a run) wins over the
    // post-run lastRunResult fallback. Both paths apply the same border
    // colour rules; the live path beats the post-hoc one only because it
    // arrives earlier on the wire.
    const liveStatus = state.scenes.actionStatus
      ? state.scenes.actionStatus[idx]
      : undefined;
    if(liveStatus){
      row.classList.add(liveStatus);
    }else{
      const runResult = state.scenes.lastRunResult;
      if(runResult && runResult.actions){
        const r = runResult.actions.find(x => x.index === idx);
        if(r){
          if(r.degraded) row.classList.add("degraded");
          else if(r.ok) row.classList.add("ok");
          else row.classList.add("error");
        }
      }
    }

    const indexCol = document.createElement("div");
    indexCol.className = "rl-scene-action-index";
    // Drag handle: SortableJS only initiates a drag from this element so
    // clicks/typing inside the row's controls keep their normal semantics.
    const grip = document.createElement("span");
    grip.className = "rl-scene-action-grip";
    grip.title = "Drag to reorder";
    grip.textContent = "⋮⋮";
    indexCol.appendChild(grip);
    const idxLabel = document.createElement("span");
    idxLabel.className = "rl-scene-action-index-label";
    idxLabel.textContent = `#${idx + 1}`;
    indexCol.appendChild(idxLabel);
    row.appendChild(indexCol);

    const kindCol = document.createElement("div");
    kindCol.className = "rl-scene-action-kind";
    const kindLabel = document.createElement("label");
    kindLabel.textContent = "Kind";
    const kindSelect = document.createElement("select");
    SCENE_KINDS_ORDER.forEach(k => {
      const opt = document.createElement("option");
      opt.value = k;
      opt.textContent = SCENE_KIND_LABELS[k] || k;
      if(k === action.kind) opt.selected = true;
      kindSelect.appendChild(opt);
    });
    kindSelect.addEventListener("change", () => {
      draft.actions[idx] = defaultActionForKind(kindSelect.value);
      renderSceneEditor();
    });
    kindCol.appendChild(kindLabel);
    kindCol.appendChild(kindSelect);
    row.appendChild(kindCol);

    const bodyCol = document.createElement("div");
    bodyCol.className = "rl-scene-action-body";
    bodyCol.dataset.kind = action.kind;
    buildActionBody(bodyCol, action, idx, draft);
    row.appendChild(bodyCol);

    // Per-action cost badge (populated by ``applyCostPayload`` after the
    // server round-trip). CSS ``grid-area: cost`` parks it on a dedicated
    // bottom row spanning all four columns and right-aligned, so it sits
    // at the bottom-right of the action card without consuming a column.
    const costBadge = document.createElement("div");
    costBadge.className = "rl-scene-action-cost muted";
    costBadge.textContent = "≈ —";
    row.appendChild(costBadge);

    const ctrl = document.createElement("div");
    ctrl.className = "rl-scene-action-controls";
    const upBtn = document.createElement("button");
    upBtn.type = "button";
    upBtn.textContent = "↑";
    upBtn.disabled = idx === 0;
    upBtn.addEventListener("click", () => moveAction(draft, idx, -1));
    const downBtn = document.createElement("button");
    downBtn.type = "button";
    downBtn.textContent = "↓";
    downBtn.disabled = idx === draft.actions.length - 1;
    downBtn.addEventListener("click", () => moveAction(draft, idx, +1));
    const rmBtn = document.createElement("button");
    rmBtn.type = "button";
    rmBtn.textContent = "Remove";
    rmBtn.addEventListener("click", () => {
      draft.actions.splice(idx, 1);
      renderSceneEditor();
    });
    ctrl.appendChild(upBtn);
    ctrl.appendChild(downBtn);
    ctrl.appendChild(rmBtn);
    row.appendChild(ctrl);

    return row;
  }

  function moveAction(draft, idx, delta){
    const j = idx + delta;
    if(j < 0 || j >= draft.actions.length) return;
    const [item] = draft.actions.splice(idx, 1);
    draft.actions.splice(j, 0, item);
    renderSceneEditor();
  }

  function reorderActions(draft, oldIndex, newIndex){
    if(oldIndex === newIndex) return;
    if(oldIndex < 0 || oldIndex >= draft.actions.length) return;
    if(newIndex < 0 || newIndex >= draft.actions.length) return;
    const [item] = draft.actions.splice(oldIndex, 1);
    draft.actions.splice(newIndex, 0, item);
    renderSceneEditor();
  }

  function enableActionDragReorder(container, draft){
    if(typeof window.Sortable !== "function"){
      // Vendor file missing or failed to load. Up/down buttons in each row
      // still work; we just skip the drag affordance silently.
      return;
    }
    window.Sortable.create(container, {
      handle: ".rl-scene-action-grip",
      animation: 150,
      ghostClass: "rl-scene-action-row-ghost",
      chosenClass: "rl-scene-action-row-chosen",
      dragClass:   "rl-scene-action-row-drag",
      onEnd: (evt) => {
        // ``oldIndex`` and ``newIndex`` are positions within the container.
        // The action array order matches the DOM order before the drag, so
        // these indices map 1:1 onto draft.actions.
        if(typeof evt.oldIndex !== "number" || typeof evt.newIndex !== "number") return;
        reorderActions(draft, evt.oldIndex, evt.newIndex);
      },
    });
  }

  function reorderChildren(parent, oldIndex, newIndex){
    if(oldIndex === newIndex) return;
    const arr = parent.actions || [];
    if(oldIndex < 0 || oldIndex >= arr.length) return;
    if(newIndex < 0 || newIndex >= arr.length) return;
    const [item] = arr.splice(oldIndex, 1);
    arr.splice(newIndex, 0, item);
    renderSceneEditor();
  }

  function enableChildDragReorder(container, parent){
    // Sortable instance scoped to one offset_group's children list. No
    // ``group:`` shared with the top-level list, so children stay inside
    // their parent (top-level allows kinds the children list does not).
    if(typeof window.Sortable !== "function") return;
    window.Sortable.create(container, {
      handle: ".rl-scene-action-grip",
      animation: 150,
      ghostClass: "rl-scene-action-row-ghost",
      chosenClass: "rl-scene-action-row-chosen",
      dragClass:   "rl-scene-action-row-drag",
      onEnd: (evt) => {
        if(typeof evt.oldIndex !== "number" || typeof evt.newIndex !== "number") return;
        reorderChildren(parent, evt.oldIndex, evt.newIndex);
      },
    });
  }

  function buildActionBody(container, action, idx, draft){
    container.innerHTML = "";
    const kindMeta = findKindMeta(action.kind);

    if(action.kind === "sync"){
      const note = document.createElement("span");
      note.className = "muted";
      note.textContent = "Broadcasts OPC_SYNC — fires every node currently in arm-on-sync state.";
      container.appendChild(note);
      return;
    }

    if(action.kind === "delay"){
      const wrap = document.createElement("div");
      wrap.className = "rl-slider-wrap";
      const lbl = document.createElement("span");
      lbl.className = "muted";
      lbl.textContent = "Duration (ms):";
      const inp = document.createElement("input");
      inp.type = "number";
      inp.min = 0;
      inp.max = 60000;
      inp.step = 50;
      inp.value = Number(action.duration_ms || 0);
      inp.style.width = "100px";
      inp.addEventListener("input", () => {
        const v = Math.max(0, Math.min(60000, Number(inp.value) || 0));
        draft.actions[idx].duration_ms = v;
        scheduleCostEstimate();
      });
      wrap.appendChild(lbl);
      wrap.appendChild(inp);
      container.appendChild(wrap);
      return;
    }

    if(action.kind === "offset_group"){
      container.appendChild(buildOffsetGroupContainer(action, idx, draft));
      return;
    }

    if(kindMeta && kindMeta.supports_target){
      container.appendChild(buildTargetPicker(action, idx, draft));
    }
    if(kindMeta && Array.isArray(kindMeta.vars) && kindMeta.vars.length){
      container.appendChild(buildVarsRow(action, idx, draft, kindMeta));
    }
    if(kindMeta && kindMeta.supports_flags_override){
      container.appendChild(buildFlagsOverrideRow(action, idx, draft));
    }
  }

  function buildTargetPicker(action, idx, draft){
    const wrap = document.createElement("div");
    wrap.className = "rl-scene-target";

    if(!action.target) action.target = { kind: "group", value: 1 };

    // Top-level targets: group or device. (Multi-group offset playback now
    // lives in the dedicated ``offset_group`` container action with its
    // own children list — pick that from the action-kind dropdown.)
    const radios = {};
    const radioRow = document.createElement("div");
    radioRow.className = "rl-scene-target-radios";
    [["group", "Group"], ["device", "Device"]].forEach(([kind, lbl]) => {
      const r = document.createElement("input");
      r.type = "radio";
      r.name = `target-kind-${idx}`;
      r.value = kind;
      r.checked = action.target.kind === kind;
      radios[kind] = r;
      const wl = document.createElement("label");
      wl.className = "inline";
      wl.appendChild(r);
      wl.appendChild(document.createTextNode(" " + lbl));
      radioRow.appendChild(wl);
    });
    wrap.appendChild(radioRow);

    const bodyHolder = document.createElement("div");
    bodyHolder.className = "rl-scene-target-body";
    bodyHolder.appendChild(buildSingleTargetSelect(action, idx, draft));
    wrap.appendChild(bodyHolder);

    function switchTo(newKind){
      const previous = action.target;
      if(newKind === "group"){
        const def = (previous.kind === "group" && Number.isFinite(Number(previous.value)))
          ? Number(previous.value) : (knownGroupIds()[0] ?? 1);
        action.target = { kind: "group", value: def };
      }else if(newKind === "device"){
        const def = (previous.kind === "device" && typeof previous.value === "string")
          ? previous.value : ((state.devices || [])[0]?.addr || "AABBCCDDEEFF");
        action.target = { kind: "device", value: String(def).toUpperCase() };
      }
      renderSceneEditor();
    }

    Object.entries(radios).forEach(([kind, r]) => {
      r.addEventListener("change", () => { if(r.checked) switchTo(kind); });
    });

    return wrap;
  }

  function buildSingleTargetSelect(action, idx, draft){
    const wrap = document.createElement("div");
    const target = action.target;
    const isGroup = target.kind === "group";
    const valueSelect = document.createElement("select");

    function fillValueOptions(){
      valueSelect.innerHTML = "";
      if(isGroup){
        // selectableGroups filters out the synthetic "Unconfigured"
        // bucket (id=0) — never a productive scene target.
        selectableGroups().forEach(g => {
          const id = (typeof g.id === "number") ? g.id : g.groupId;
          const opt = document.createElement("option");
          opt.value = String(id);
          opt.textContent = `${g.name || ("Group " + id)} (${id})`;
          if(Number(target.value) === id) opt.selected = true;
          valueSelect.appendChild(opt);
        });
        if(!valueSelect.options.length){
          const opt = document.createElement("option");
          opt.value = "1";
          opt.textContent = "(no groups — using id=1)";
          valueSelect.appendChild(opt);
        }
      }else{
        (state.devices || []).forEach(d => {
          if(!d.addr) return;
          const addr = String(d.addr).toUpperCase();
          if(addr.length !== 12) return;
          const opt = document.createElement("option");
          opt.value = addr;
          opt.textContent = `${d.name || addr} (${addr})`;
          if(String(target.value).toUpperCase() === addr) opt.selected = true;
          valueSelect.appendChild(opt);
        });
        if(!valueSelect.options.length){
          const opt = document.createElement("option");
          opt.value = "AABBCCDDEEFF";
          opt.textContent = "(no devices yet — placeholder)";
          valueSelect.appendChild(opt);
        }
      }
    }

    valueSelect.addEventListener("change", () => {
      if(isGroup){
        draft.actions[idx].target = { kind: "group", value: Number(valueSelect.value) || 1 };
      }else{
        draft.actions[idx].target = { kind: "device", value: String(valueSelect.value || "").toUpperCase() };
      }
    });

    fillValueOptions();
    wrap.appendChild(valueSelect);
    return wrap;
  }

  function buildOffsetGroupConfigPanel(action, idx, draft){
    // Renders the groups + offset configuration for an offset_group
    // container action. Reads/writes ``action.groups`` and
    // ``action.offset`` directly — no nested target indirection.
    ensureOffsetGroupShape(action);
    const wrap = document.createElement("div");
    wrap.className = "rl-groups-offset";

    const knownIds = knownGroupIds();
    const offset = action.offset;
    let selection = getSelectedIdsFromHolder(action);
    // ``selected`` is the editing scratchpad for non-"all" mode. We seed
    // it from the persisted state but let the user toggle freely; the
    // toggle becomes the persisted ``groups`` list when saved.
    const selected = new Set(selection.ids);

    function repaintAll(){
      // Recompute target.groups AND target.offset from the current UI
      // state, then re-render the live preview. Every input handler funnels
      // through here so the persisted shape is always in sync.
      if(allGroupsToggle && allGroupsToggle.checked){
        action.groups = "all";
      }else{
        action.groups = Array.from(selected).sort((a, b) => a - b);
      }
      // For Explicit mode, sync the values list to the current selection.
      if(action.offset.mode === "explicit"){
        const prev = explicitValuesMapFromHolder(action);
        const ids = action.groups === "all" ? knownIds : action.groups;
        action.offset = {
          mode: "explicit",
          values: ids.map(id => ({
            id,
            offset_ms: clampOffsetMs(prev.has(id) ? prev.get(id) : 0),
          })),
        };
      }
      renderPreview();
    }

    // ---- "all groups" toggle ----
    const allRow = document.createElement("div");
    allRow.className = "rl-groups-offset-all-row";
    const allLbl = document.createElement("label");
    allLbl.className = "rl-toggle-wrap";
    const allGroupsToggle = document.createElement("input");
    allGroupsToggle.type = "checkbox";
    allGroupsToggle.checked = (action.groups === "all");
    allLbl.appendChild(allGroupsToggle);
    const allTxt = document.createElement("span");
    allTxt.textContent = "All groups";
    allLbl.appendChild(allTxt);
    allRow.appendChild(allLbl);
    const allHint = document.createElement("span");
    allHint.className = "muted";
    allHint.textContent = "(broadcast formula in one packet — only available with Linear/From-center/Repeating/Clear)";
    allRow.appendChild(allHint);
    wrap.appendChild(allRow);

    // Selecting "all" is incompatible with mode=explicit (no per-group
    // values to evaluate against). Snap mode to linear in that case.
    allGroupsToggle.addEventListener("change", () => {
      if(allGroupsToggle.checked && offset.mode === "explicit"){
        action.offset = { mode: "linear", base_ms: 0, step_ms: 100 };
      }
      // Toggling re-seeds selected from the current "all" / list state so
      // unchecking "all" gives a sensible starting selection.
      if(!allGroupsToggle.checked){
        selected.clear();
        knownIds.forEach(id => selected.add(id));
      }
      rebuildCheckboxes();
      syncListEnabledState();
      repaintAll();
      // Mode might have changed → re-render the panel to refresh inputs.
      // Cheap; the editor pattern already does full repaints on shape
      // changes elsewhere.
      renderSceneEditor();
    });

    // ---- group multi-select (hidden when "all" is on) ----
    const listSection = document.createElement("div");
    listSection.className = "rl-groups-offset-list-section";

    const quick = document.createElement("div");
    quick.className = "rl-groups-offset-quick";
    function makeBtn(label, fn){
      const b = document.createElement("button");
      b.type = "button";
      b.className = "btn";
      b.textContent = label;
      // Full re-render after the selection changes so child target pickers
      // see the updated ``parent.groups`` list. Quick buttons are click-and-
      // done so losing focus is fine.
      b.addEventListener("click", () => {
        fn();
        repaintAll();
        renderSceneEditor();
      });
      return b;
    }
    quick.appendChild(makeBtn("All", () => { knownIds.forEach(id => selected.add(id)); }));
    quick.appendChild(makeBtn("None", () => { selected.clear(); }));
    quick.appendChild(makeBtn("Invert", () => {
      knownIds.forEach(id => { if(selected.has(id)) selected.delete(id); else selected.add(id); });
    }));
    quick.appendChild(makeBtn("Range…", () => {
      const lo = parseInt(prompt("From group id:", String(knownIds[0] ?? 0)) || "", 10);
      if(!Number.isFinite(lo)) return;
      const hi = parseInt(prompt("To group id (inclusive):", String(knownIds[knownIds.length-1] ?? lo)) || "", 10);
      if(!Number.isFinite(hi)) return;
      knownIds.forEach(id => { if(id >= Math.min(lo, hi) && id <= Math.max(lo, hi)) selected.add(id); });
    }));
    listSection.appendChild(quick);

    const groupsBox = document.createElement("div");
    groupsBox.className = "rl-groups-offset-checks";
    function rebuildCheckboxes(){
      groupsBox.innerHTML = "";
      if(!knownIds.length){
        const empty = document.createElement("span");
        empty.className = "muted";
        empty.textContent = "(no groups available)";
        groupsBox.appendChild(empty);
        return;
      }
      knownIds.forEach(id => {
        const lbl = document.createElement("label");
        lbl.className = "rl-toggle-wrap";
        const cb = document.createElement("input");
        cb.type = "checkbox";
        cb.checked = selected.has(id);
        cb.addEventListener("change", () => {
          if(cb.checked) selected.add(id); else selected.delete(id);
          repaintAll();
          // Re-render the whole editor so child target pickers reflect the
          // new parent.groups list. Without this their dropdown stays stale
          // until the user toggles the picker mode.
          renderSceneEditor();
        });
        const txt = document.createElement("span");
        const meta = (state.groups || []).find(g => ((typeof g.id === "number") ? g.id : g.groupId) === id);
        txt.textContent = `${meta?.name || ("Group " + id)} (${id})`;
        lbl.appendChild(cb);
        lbl.appendChild(txt);
        groupsBox.appendChild(lbl);
      });
    }
    listSection.appendChild(groupsBox);
    rebuildCheckboxes();
    wrap.appendChild(listSection);

    function syncListEnabledState(){
      listSection.style.display = allGroupsToggle.checked ? "none" : "";
    }
    syncListEnabledState();

    // ---- formula mode selector ----
    const modeRow = document.createElement("div");
    modeRow.className = "rl-groups-offset-mode";
    const modeLbl = document.createElement("span");
    modeLbl.className = "muted";
    modeLbl.textContent = "Offset mode:";
    modeRow.appendChild(modeLbl);
    OFFSET_FORMULA_MODES.forEach(m => {
      // Explicit not allowed when "all groups" is on (no list to evaluate).
      if(m === "explicit" && allGroupsToggle.checked) return;
      const r = document.createElement("input");
      r.type = "radio";
      r.name = `go-mode-${idx}`;
      r.value = m;
      r.checked = (offset.mode === m);
      r.addEventListener("change", () => {
        if(!r.checked) return;
        // Switching modes preserves shared params (base/step) when
        // moving between formula modes; only mode-specific extras reset.
        const prev = action.offset || {};
        if(m === "explicit"){
          const ids = allGroupsToggle.checked ? knownIds : Array.from(selected).sort((a,b)=>a-b);
          const prevValues = explicitValuesMapFromHolder(action);
          action.offset = {
            mode: "explicit",
            values: ids.map(id => ({
              id,
              offset_ms: clampOffsetMs(prevValues.has(id) ? prevValues.get(id) : 0),
            })),
          };
        }else if(m === "linear"){
          action.offset = {
            mode: "linear",
            base_ms: clampS16(prev.base_ms ?? OFFSET_FORMULA_DEFAULTS.base_ms),
            step_ms: clampS16(prev.step_ms ?? OFFSET_FORMULA_DEFAULTS.step_ms),
          };
        }else if(m === "vshape"){
          action.offset = {
            mode: "vshape",
            base_ms: clampS16(prev.base_ms ?? OFFSET_FORMULA_DEFAULTS.base_ms),
            step_ms: clampS16(prev.step_ms ?? OFFSET_FORMULA_DEFAULTS.step_ms),
            center: clampOffsetMs(prev.center ?? OFFSET_FORMULA_DEFAULTS.center),
          };
        }else if(m === "modulo"){
          action.offset = {
            mode: "modulo",
            base_ms: clampS16(prev.base_ms ?? OFFSET_FORMULA_DEFAULTS.base_ms),
            step_ms: clampS16(prev.step_ms ?? OFFSET_FORMULA_DEFAULTS.step_ms),
            cycle: Math.max(1, Math.min(255, Math.round(prev.cycle ?? OFFSET_FORMULA_DEFAULTS.cycle))),
          };
        }else{
          action.offset = { mode: "none" };
        }
        // Re-render the panel: mode-specific inputs change.
        renderSceneEditor();
      });
      const wl = document.createElement("label");
      wl.className = "inline";
      wl.appendChild(r);
      wl.appendChild(document.createTextNode(" " + (FORMULA_MODE_LABELS[m] || m)));
      modeRow.appendChild(wl);
    });
    wrap.appendChild(modeRow);

    // ---- mode-specific param inputs ----
    function makeMsInput(initial, onChange){
      const inp = document.createElement("input");
      inp.type = "number";
      const bounds = (state.scenes.schema && state.scenes.schema.offsetGroup && state.scenes.schema.offsetGroup.offset_ms) || {min:0, max:65535};
      inp.min = String(bounds.min);
      inp.max = String(bounds.max);
      inp.step = "1";
      inp.value = String(initial ?? 0);
      inp.style.width = "90px";
      inp.addEventListener("input", () => onChange(Number(inp.value) || 0));
      return inp;
    }
    function makeS16Input(initial, onChange){
      const inp = document.createElement("input");
      inp.type = "number";
      inp.min = "-32768";
      inp.max = "32767";
      inp.step = "1";
      inp.value = String(initial ?? 0);
      inp.style.width = "90px";
      inp.addEventListener("input", () => onChange(Number(inp.value) || 0));
      return inp;
    }
    function makeU8Input(initial, onChange, lo, hi){
      const inp = document.createElement("input");
      inp.type = "number";
      inp.min = String(lo);
      inp.max = String(hi);
      inp.step = "1";
      inp.value = String(initial ?? lo);
      inp.style.width = "70px";
      inp.addEventListener("input", () => onChange(Number(inp.value) || 0));
      return inp;
    }

    const paramsWrap = document.createElement("div");
    paramsWrap.className = "rl-groups-offset-params";
    const off = action.offset;
    if(["linear", "vshape", "modulo"].includes(off.mode)){
      const baseLbl = document.createElement("span"); baseLbl.textContent = "base_ms:";
      const baseInp = makeS16Input(off.base_ms ?? 0, v => { off.base_ms = clampS16(v); renderPreview(); });
      const stepLbl = document.createElement("span"); stepLbl.textContent = "step_ms:";
      const stepInp = makeS16Input(off.step_ms ?? 0, v => { off.step_ms = clampS16(v); renderPreview(); });
      paramsWrap.appendChild(baseLbl); paramsWrap.appendChild(baseInp);
      paramsWrap.appendChild(stepLbl); paramsWrap.appendChild(stepInp);
      if(off.mode === "vshape"){
        const cLbl = document.createElement("span"); cLbl.textContent = "center:";
        const cInp = makeU8Input(off.center ?? 0,
          v => { off.center = Math.max(0, Math.min(254, v|0)); renderPreview(); }, 0, 254);
        paramsWrap.appendChild(cLbl); paramsWrap.appendChild(cInp);
      }
      if(off.mode === "modulo"){
        const cLbl = document.createElement("span"); cLbl.textContent = "cycle:";
        const cInp = makeU8Input(off.cycle ?? 1,
          v => { off.cycle = Math.max(1, Math.min(255, v|0)); renderPreview(); }, 1, 255);
        paramsWrap.appendChild(cLbl); paramsWrap.appendChild(cInp);
      }
    }else if(off.mode === "explicit"){
      // One input per participating group.
      const ids = (action.groups === "all") ? knownIds : (action.groups || []);
      if(!ids.length){
        const m = document.createElement("span");
        m.className = "muted"; m.textContent = "(no groups selected)";
        paramsWrap.appendChild(m);
      }
      const byId = explicitValuesMapFromHolder(action);
      ids.forEach(id => {
        const row = document.createElement("div");
        row.className = "rl-groups-offset-explicit-row";
        const tag = document.createElement("span");
        tag.textContent = `G${id}:`;
        tag.style.minWidth = "48px";
        const inp = makeMsInput(byId.get(id) ?? 0, v => {
          // Update the values entry in place.
          let entry = (off.values || []).find(e => e.id === id);
          if(!entry){ entry = { id, offset_ms: 0 }; (off.values || (off.values = [])).push(entry); }
          entry.offset_ms = clampOffsetMs(v);
          renderPreview();
        });
        row.appendChild(tag);
        row.appendChild(inp);
        const ms = document.createElement("span");
        ms.className = "muted"; ms.textContent = "ms";
        row.appendChild(ms);
        paramsWrap.appendChild(row);
      });
    }else if(off.mode === "none"){
      const m = document.createElement("span");
      m.className = "muted";
      m.textContent = "Clear: configured devices return to NORMAL acceptance.";
      paramsWrap.appendChild(m);
    }
    wrap.appendChild(paramsWrap);

    function renderExplicitInputs(){
      // Selection toggle in Explicit mode → re-render the panel so the
      // per-group inputs match.
      if(off.mode === "explicit") renderSceneEditor();
    }

    // ---- live preview ----
    const previewWrap = document.createElement("div");
    previewWrap.className = "rl-groups-offset-preview";
    function renderPreview(){
      previewWrap.innerHTML = "";
      const head = document.createElement("span");
      head.className = "muted";
      head.textContent = "Preview:";
      previewWrap.appendChild(head);
      const ids = (action.groups === "all") ? knownIds : (action.groups || []);
      if(!ids.length){
        const m = document.createElement("span");
        m.className = "muted"; m.textContent = "(empty)";
        previewWrap.appendChild(m);
        return;
      }
      // Cap the preview at 32 entries to keep the row scannable; show
      // a trailing "+ N more" hint when truncated.
      const shown = ids.slice(0, 32);
      shown.forEach(id => {
        const ms = (off.mode === "explicit")
          ? (explicitValuesMapFromHolder(action).get(id) ?? 0)
          : evaluateOffsetMs(off, id);
        const tag = document.createElement("span");
        tag.className = "rl-groups-offset-preview-tag";
        tag.textContent = `G${id}→${ms}ms`;
        previewWrap.appendChild(tag);
      });
      if(ids.length > shown.length){
        const more = document.createElement("span");
        more.className = "muted";
        more.textContent = `+ ${ids.length - shown.length} more`;
        previewWrap.appendChild(more);
      }
    }
    wrap.appendChild(previewWrap);
    renderPreview();
    return wrap;
  }

  function buildOffsetGroupContainer(action, idx, draft){
    // Top-level renderer for an ``offset_group`` action. Contains the
    // groups + offset config (via buildOffsetGroupConfigPanel) plus a
    // nested list of child actions (wled_control / wled_preset / rl_preset).
    ensureOffsetGroupShape(action);

    const wrap = document.createElement("div");
    wrap.className = "rl-offset-group-container";

    // Inline warning when groups: "all" AND prior offset_group actions
    // exist in the same scene — operators need to know that broadcast
    // formulas overwrite previously-configured groups.
    const priorOffsetGroups = (draft.actions || [])
      .slice(0, idx)
      .filter(a => a && a.kind === "offset_group");
    if(action.groups === "all" && priorOffsetGroups.length > 0){
      const warn = document.createElement("div");
      warn.className = "rl-offset-group-warning";
      warn.textContent =
        "⚠ Affects all groups, including those configured by previous Offset Group actions.";
      wrap.appendChild(warn);
    }

    // Config panel: group selector + mode + params + preview.
    wrap.appendChild(buildOffsetGroupConfigPanel(action, idx, draft));

    // ---- children list -------------------------------------------------
    const childrenSection = document.createElement("div");
    childrenSection.className = "rl-offset-group-children";

    const childrenLbl = document.createElement("div");
    childrenLbl.className = "rl-offset-group-children-label muted";
    const numChildren = (action.actions || []).length;
    childrenLbl.textContent = `Effects (${numChildren}/${state.scenes.schema?.offsetGroup?.max_children ?? 16}):`;
    childrenSection.appendChild(childrenLbl);

    const childrenContainer = document.createElement("div");
    childrenContainer.className = "rl-offset-group-children-list";
    (action.actions || []).forEach((child, childIdx) => {
      childrenContainer.appendChild(
        buildOffsetGroupChildRow(child, childIdx, action, draft)
      );
    });
    if(!numChildren){
      const empty = document.createElement("div");
      empty.className = "muted rl-offset-group-children-empty";
      empty.textContent = "(no effects yet — add one below)";
      childrenContainer.appendChild(empty);
    }
    childrenSection.appendChild(childrenContainer);
    if(numChildren > 0){
      enableChildDragReorder(childrenContainer, action);
    }

    // Add-child row.
    const addRow = document.createElement("div");
    addRow.className = "rl-offset-group-add-child";
    const addLbl = document.createElement("span");
    addLbl.className = "muted";
    addLbl.textContent = "+ Add effect:";
    const kindPicker = document.createElement("select");
    OFFSET_GROUP_CHILD_KINDS.forEach(k => {
      const opt = document.createElement("option");
      opt.value = k;
      opt.textContent = SCENE_KIND_LABELS[k] || k;
      kindPicker.appendChild(opt);
    });
    const addBtn = document.createElement("button");
    addBtn.type = "button";
    addBtn.textContent = "Add";
    const maxChildren = state.scenes.schema?.offsetGroup?.max_children ?? 16;
    addBtn.disabled = numChildren >= maxChildren;
    addBtn.addEventListener("click", () => {
      if((action.actions || []).length >= maxChildren) return;
      action.actions = action.actions || [];
      action.actions.push(defaultOffsetGroupChild(kindPicker.value));
      renderSceneEditor();
    });
    addRow.appendChild(addLbl);
    addRow.appendChild(kindPicker);
    addRow.appendChild(addBtn);
    childrenSection.appendChild(addRow);

    wrap.appendChild(childrenSection);
    return wrap;
  }

  function buildOffsetGroupChildRow(child, childIdx, parent, draft){
    // A child action row inside an offset_group container. Layout mirrors
    // a top-level row but with restricted kind dropdown + scope/group/device
    // target picker and a forced-on offset_mode flag (visually disabled).
    const row = document.createElement("div");
    row.className = "rl-offset-group-child-row";

    // Index column with grip handle (for nested SortableJS).
    const indexCol = document.createElement("div");
    indexCol.className = "rl-scene-action-index";
    const grip = document.createElement("span");
    grip.className = "rl-scene-action-grip";
    grip.title = "Drag to reorder";
    grip.textContent = "⋮⋮";
    indexCol.appendChild(grip);
    const idxLbl = document.createElement("span");
    idxLbl.className = "rl-scene-action-index-label";
    idxLbl.textContent = `#${childIdx + 1}`;
    indexCol.appendChild(idxLbl);
    row.appendChild(indexCol);

    // Kind dropdown — restricted to OFFSET_GROUP_CHILD_KINDS.
    const kindCol = document.createElement("div");
    kindCol.className = "rl-scene-action-kind";
    const kindSelect = document.createElement("select");
    OFFSET_GROUP_CHILD_KINDS.forEach(k => {
      const opt = document.createElement("option");
      opt.value = k;
      opt.textContent = SCENE_KIND_LABELS[k] || k;
      if(k === child.kind) opt.selected = true;
      kindSelect.appendChild(opt);
    });
    kindSelect.addEventListener("change", () => {
      parent.actions[childIdx] = defaultOffsetGroupChild(kindSelect.value);
      renderSceneEditor();
    });
    kindCol.appendChild(kindSelect);
    row.appendChild(kindCol);

    // Body: child target picker + vars + flags row.
    const bodyCol = document.createElement("div");
    bodyCol.className = "rl-scene-action-body";
    bodyCol.dataset.kind = child.kind;
    bodyCol.appendChild(buildChildTargetPicker(child, parent));
    const meta = findKindMeta(child.kind);
    if(meta && Array.isArray(meta.vars) && meta.vars.length){
      bodyCol.appendChild(buildVarsRow(child, childIdx, draft, meta));
    }
    if(meta && meta.supports_flags_override){
      bodyCol.appendChild(buildFlagsOverrideRow(child, childIdx, draft, /*forceOffsetMode*/ true));
    }
    row.appendChild(bodyCol);

    // Remove button.
    const ctrl = document.createElement("div");
    ctrl.className = "rl-scene-action-controls";
    const rmBtn = document.createElement("button");
    rmBtn.type = "button";
    rmBtn.textContent = "Remove";
    rmBtn.addEventListener("click", () => {
      parent.actions.splice(childIdx, 1);
      renderSceneEditor();
    });
    ctrl.appendChild(rmBtn);
    row.appendChild(ctrl);

    return row;
  }

  function buildChildTargetPicker(child, parent){
    // Three options: scope (broadcast to all participants),
    // group (one of the parent's groups), device (any device whose
    // groupId is in the parent's scope). Filtered choices reflect the
    // parent's ``groups`` field.
    const wrap = document.createElement("div");
    wrap.className = "rl-scene-target";
    if(!child.target) child.target = { kind: "scope" };

    const radioRow = document.createElement("div");
    radioRow.className = "rl-scene-target-radios";

    const radios = {};
    [["scope", "Scope (broadcast)"], ["group", "Group"], ["device", "Device"]].forEach(([kind, lbl]) => {
      const r = document.createElement("input");
      r.type = "radio";
      r.name = `child-target-${child.__cid || (child.__cid = Math.random().toString(36).slice(2))}`;
      r.value = kind;
      r.checked = child.target.kind === kind;
      radios[kind] = r;
      const wl = document.createElement("label");
      wl.className = "inline";
      wl.appendChild(r);
      wl.appendChild(document.createTextNode(" " + lbl));
      radioRow.appendChild(wl);
    });
    wrap.appendChild(radioRow);

    const bodyHolder = document.createElement("div");
    bodyHolder.className = "rl-scene-target-body";
    wrap.appendChild(bodyHolder);

    function renderBody(){
      bodyHolder.innerHTML = "";
      const tk = child.target.kind;
      if(tk === "scope") return;  // no value picker
      const sel = document.createElement("select");
      if(tk === "group"){
        const allowedIds = (parent.groups === "all")
          ? knownGroupIds()
          : (parent.groups || []);
        allowedIds.forEach(id => {
          const opt = document.createElement("option");
          opt.value = String(id);
          const meta = (state.groups || []).find(g => ((typeof g.id === "number") ? g.id : g.groupId) === id);
          opt.textContent = `${meta?.name || ("Group " + id)} (${id})`;
          if(Number(child.target.value) === id) opt.selected = true;
          sel.appendChild(opt);
        });
        if(!sel.options.length){
          const opt = document.createElement("option");
          opt.value = "1";
          opt.textContent = "(no groups in parent scope)";
          sel.appendChild(opt);
        }
        sel.addEventListener("change", () => {
          child.target = { kind: "group", value: Number(sel.value) || 0 };
          scheduleCostEstimate();
        });
      }else{   // device
        const allowedIds = (parent.groups === "all") ? null : (parent.groups || []);
        (state.devices || []).forEach(d => {
          if(!d.addr) return;
          const addr = String(d.addr).toUpperCase();
          if(addr.length !== 12) return;
          // Filter to devices whose groupId is in the parent's scope.
          if(allowedIds !== null && !allowedIds.includes(d.groupId)) return;
          const opt = document.createElement("option");
          opt.value = addr;
          opt.textContent = `${d.name || addr} (${addr})`;
          if(child.target.kind === "device" && String(child.target.value).toUpperCase() === addr){
            opt.selected = true;
          }
          sel.appendChild(opt);
        });
        if(!sel.options.length){
          const opt = document.createElement("option");
          opt.value = "AABBCCDDEEFF";
          opt.textContent = "(no devices in parent scope)";
          sel.appendChild(opt);
        }
        sel.addEventListener("change", () => {
          child.target = { kind: "device", value: String(sel.value || "").toUpperCase() };
          scheduleCostEstimate();
        });
      }
      bodyHolder.appendChild(sel);
    }

    Object.entries(radios).forEach(([kind, r]) => {
      r.addEventListener("change", () => {
        if(!r.checked) return;
        if(kind === "scope"){
          child.target = { kind: "scope" };
        }else if(kind === "group"){
          const seedId = (parent.groups === "all")
            ? (knownGroupIds()[0] ?? 0)
            : ((parent.groups || [])[0] ?? 0);
          child.target = { kind: "group", value: seedId };
        }else{
          // Pick the first device whose groupId is in the parent scope.
          const allowedIds = (parent.groups === "all") ? null : (parent.groups || []);
          const dev = (state.devices || []).find(d =>
            d && d.addr && (allowedIds === null || allowedIds.includes(d.groupId))
          );
          child.target = {
            kind: "device",
            value: (dev?.addr || "AABBCCDDEEFF").toUpperCase(),
          };
        }
        renderBody();
        scheduleCostEstimate();
      });
    });

    renderBody();
    return wrap;
  }

  function buildVarsRow(action, idx, draft, kindMeta){
    const wrap = document.createElement("div");
    wrap.className = "rl-scene-vars";
    if(!action.params) action.params = {};
    const params = action.params;

    function coerceSelectValue(v){
      if(v === undefined || v === null) return v;
      const n = Number(v);
      return (Number.isFinite(n) && String(n) === String(v)) ? n : v;
    }

    kindMeta.vars.forEach(varKey => {
      const uiInfo = (kindMeta.ui && kindMeta.ui[varKey]) || {};
      const fieldWrap = document.createElement("div");
      fieldWrap.className = "rl-special-input";
      const lbl = document.createElement("span");
      lbl.className = "rl-special-input-label";
      lbl.textContent = varKey;
      fieldWrap.appendChild(lbl);

      let input;
      if(uiInfo.widget === "select"){
        input = document.createElement("select");
        (uiInfo.options || []).forEach(o => {
          const opt = document.createElement("option");
          opt.value = String(o.value);
          opt.textContent = o.label || String(o.value);
          if(String(params[varKey]) === String(o.value)) opt.selected = true;
          input.appendChild(opt);
        });
        if(!input.options.length){
          const opt = document.createElement("option");
          opt.value = "";
          opt.textContent = "(no options)";
          input.appendChild(opt);
        }
        input.addEventListener("change", () => {
          params[varKey] = coerceSelectValue(input.value);
        });
        // R3: commit the initial selection so a Save without user input
        // still posts a valid action. Without this, freshly-added rl_preset
        // / wled_preset / wled_control actions persisted with presetId
        // undefined and the runner returned ``missing_preset_id``.
        if(params[varKey] === undefined && input.options.length){
          params[varKey] = coerceSelectValue(input.value);
        }
      }else if(uiInfo.widget === "slider"){
        const sliderWrap = document.createElement("div");
        sliderWrap.className = "rl-slider-wrap";
        const range = document.createElement("input");
        range.type = "range";
        // R2: 50%-of-range default mirrors buildSpecialVarInput
        // (racelink.js A13 contract).
        const min = uiInfo.min !== undefined ? Number(uiInfo.min) : 0;
        const max = uiInfo.max !== undefined ? Number(uiInfo.max) : 255;
        const fallback = Math.round((min + max) / 2);
        const initial = (params[varKey] != null) ? Number(params[varKey]) : fallback;
        range.min = String(min);
        range.max = String(max);
        range.value = String(initial);
        const num = document.createElement("input");
        num.type = "number";
        num.min = String(min);
        num.max = String(max);
        num.value = String(initial);
        num.style.width = "70px";
        params[varKey] = initial;
        const sync = (src) => {
          const v = Number(src.value) || 0;
          range.value = v;
          num.value = v;
          params[varKey] = v;
        };
        range.addEventListener("input", () => sync(range));
        num.addEventListener("input", () => sync(num));
        sliderWrap.appendChild(range);
        sliderWrap.appendChild(num);
        fieldWrap.appendChild(sliderWrap);
        wrap.appendChild(fieldWrap);
        return;
      }else{
        input = document.createElement("input");
        input.type = "text";
        input.value = (params[varKey] != null) ? String(params[varKey]) : "";
        input.addEventListener("input", () => {
          params[varKey] = input.value;
        });
        if(params[varKey] === undefined && input.value !== ""){
          params[varKey] = input.value;
        }
      }
      fieldWrap.appendChild(input);
      wrap.appendChild(fieldWrap);
    });

    return wrap;
  }

  function buildFlagsOverrideRow(action, idx, draft, forceOffsetMode = false){
    const wrap = document.createElement("div");
    wrap.className = "rl-scene-flags";
    const lbl = document.createElement("span");
    lbl.className = "muted";
    lbl.textContent = "Flags override:";
    wrap.appendChild(lbl);

    if(!action.flags_override) action.flags_override = {};
    const flagKeys = (state.scenes.schema && state.scenes.schema.flagKeys) || [];
    // When the action is a child of an ``offset_group`` container, the
    // runner forces ``offset_mode`` on regardless of override; render
    // that checkbox as checked + disabled so the UI matches runtime.
    flagKeys.forEach(fk => {
      const tw = document.createElement("label");
      tw.className = "rl-toggle-wrap";
      const cb = document.createElement("input");
      cb.type = "checkbox";
      const forced = forceOffsetMode && fk === "offset_mode";
      cb.checked = forced ? true : Boolean(action.flags_override[fk]);
      cb.disabled = forced;
      if(forced){ tw.title = "Forced on by Offset Group container"; }
      cb.addEventListener("change", () => {
        if(forced) return;
        action.flags_override[fk] = cb.checked;
        scheduleCostEstimate();
      });
      const txt = document.createElement("span");
      txt.textContent = fk;
      tw.appendChild(cb);
      tw.appendChild(txt);
      wrap.appendChild(tw);
    });

    if(!flagKeys.length){
      const w = document.createElement("span");
      w.className = "muted";
      w.textContent = "(schema not loaded)";
      wrap.appendChild(w);
    }

    return wrap;
  }

  async function saveSceneDraft(){
    const draft = state.scenes.draft;
    if(!draft) return;
    const labelInput = $("#sceneLabelInput");
    const label = (labelInput && labelInput.value || "").trim();
    if(!label){ setScenesHint("Label is required."); return; }
    const body = { label, actions: stripUiBeforeSave(draft.actions) };
    let r;
    if(draft.key){
      r = await apiPut(`/racelink/api/scenes/${draft.key}`, body);
    }else{
      r = await apiPost(`/racelink/api/scenes`, body);
    }
    if(!r.ok){ setScenesHint(r.error || "Save failed."); return; }
    setScenesHint(`Saved "${r.scene.label}".`);
    await loadScenes();
    state.scenes.selectedKey = r.scene.key;
    state.scenes.draft = cloneAction(r.scene);
    renderSceneList();
    renderSceneEditor();
  }

  async function runScene(key){
    setScenesHint(`Running "${key}"…`);
    // R7: arm live-status tracking. Each action row clears its border
    // colour and the SSE handler will paint blue (running) / green (ok) /
    // red (error/degraded) as transitions arrive.
    state.scenes.activeRunKey = key;
    state.scenes.actionStatus = [];
    state.scenes.lastRunResult = null;
    renderSceneEditor();

    const r = await apiPost(`/racelink/api/scenes/${key}/run`, {});
    state.scenes.activeRunKey = null;
    if(r && r.result){
      state.scenes.lastRunResult = r.result;
      const summary = r.ok ? "OK" : (r.result.error || "failed");
      setScenesHint(`Run "${key}": ${summary}`);
    }else{
      setScenesHint(`Run "${key}" failed: ${(r && r.error) || "unknown error"}`);
    }
    // Drop the per-row live state on completion — lastRunResult drives the
    // borders from here on, identical to pre-R7 behaviour.
    state.scenes.actionStatus = [];
    renderSceneEditor();
  }

  // R7: live progress handler installed for racelink.js's SSE listener.
  // Filtered by activeRunKey so a parallel run from another tab doesn't
  // colour rows on this tab (the user there hasn't clicked Run).
  window.__rlSceneProgress = (payload) => {
    if(!payload || payload.scene_key !== state.scenes.activeRunKey) return;
    const idx = Number(payload.index);
    if(!Number.isFinite(idx)) return;
    if(!Array.isArray(state.scenes.actionStatus)){
      state.scenes.actionStatus = [];
    }
    state.scenes.actionStatus[idx] = payload.status;
    const editor = document.getElementById("sceneEditor");
    if(!editor) return;
    const row = editor.querySelector(`[data-action-idx="${idx}"]`);
    if(!row) return;
    row.classList.remove("running", "ok", "error", "degraded");
    if(payload.status){
      row.classList.add(String(payload.status));
    }
  };

  // Exposed for racelink.js's SSE refresh handler — fires when the SCENES
  // topic arrives (CRUD on another tab / RH plugin etc.).
  window.__rlScenesRefresh = async () => {
    await loadScenes();
    renderSceneList();
    if(state.scenes.selectedKey){
      const fresh = state.scenes.items.find(s => s.key === state.scenes.selectedKey);
      if(fresh && state.scenes.draft && state.scenes.draft.key === state.scenes.selectedKey){
        try{
          const sameAsDraft = JSON.stringify(fresh.actions) === JSON.stringify(state.scenes.draft.actions || [])
                            && fresh.label === state.scenes.draft.label;
          if(sameAsDraft){
            state.scenes.draft = cloneAction(fresh);
            renderSceneEditor();
          }
        }catch{
          // ignore
        }
      }else if(!fresh){
        state.scenes.selectedKey = null;
        state.scenes.draft = null;
        renderSceneEditor();
      }
    }
  };

  // ---- bootstrap (page-load) ------------------------------------------

  async function init(){
    setScenesHint("");
    await Promise.all([ensureScenesSchema(), loadScenes(), loadGroupsAndDevicesForTargetPicker()]);
    if(!state.scenes.selectedKey && state.scenes.items.length){
      state.scenes.selectedKey = state.scenes.items[0].key;
    }
    const sel = state.scenes.items.find(s => s.key === state.scenes.selectedKey) || null;
    state.scenes.draft = sel ? cloneAction(sel) : null;
    renderSceneList();
    renderSceneEditor();
  }

  document.addEventListener("DOMContentLoaded", () => {
    init().catch(e => {
      console.error("[scenes] init failed", e);
      setScenesHint("Initialisation failed — check the console.");
    });

    const newBtn = $("#btnSceneNew");
    if(newBtn){
      newBtn.addEventListener("click", () => {
        newSceneDraft();
        setScenesHint("New scene — enter label and add actions, then Create.");
      });
    }
    const rlPresetsLink = $("#btnSceneOpenRlPresets");
    if(rlPresetsLink){
      rlPresetsLink.addEventListener("click", () => {
        // The dlgRlPresets dialog is rendered on this page too (see scenes.html);
        // its open-handler lives in racelink.js and is bound to ``btnRlPresets``.
        const btnRl = $("#btnRlPresets");
        if(btnRl) btnRl.click();
      });
    }
  });

  // If DOM already parsed at script-eval time, fire init right away.
  if(document.readyState === "interactive" || document.readyState === "complete"){
    init().catch(e => console.error("[scenes] late init failed", e));
  }
})();
