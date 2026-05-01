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
      targetKinds: Array.isArray(r.target_kinds)
        ? r.target_kinds : ["broadcast", "groups", "device"],
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

  // ---- C5: capability filtering for scene-action target dropdowns ---
  //
  // Different action kinds land on different device types: a
  // ``wled_preset`` packet is meaningless to a non-WLED node, a
  // ``startblock`` action only fires on starting-block hardware. Pre-
  // C5 the dropdowns showed every group / every device regardless of
  // the action kind, so picking a non-matching target produced a
  // green SSE pip with no actual effect — the worst kind of bug.
  //
  // ``requiredCapForKind`` maps each scene-action kind to the
  // capability string the wire packet needs (or ``null`` for kinds
  // that broadcast or don't touch hardware).
  // ``deviceHasCap`` reads the ``dev_type_caps`` array the device DTO
  // carries (see racelink/web/dto.py::serialize_device).
  // ``groupHasCap`` consults the ``caps_in_group`` map the
  // ``/api/groups`` endpoint now returns.
  function requiredCapForKind(kind){
    switch(kind){
      case "rl_preset":
      case "wled_preset":
      case "wled_control":
        return "WLED";
      case "startblock":
        return "STARTBLOCK";
      // sync / delay / offset_group don't carry a capability
      // requirement — sync broadcasts, delay is host-side, and
      // offset_group's children carry their own kind (which is
      // checked separately for the child target picker).
      default:
        return null;
    }
  }

  function deviceHasCap(device, cap){
    if(!cap || !device) return true;
    const caps = device.dev_type_caps;
    return Array.isArray(caps) && caps.indexOf(cap) >= 0;
  }

  function groupCapCount(group, cap){
    if(!cap) return undefined;  // no filter active
    const map = (group && group.caps_in_group) || {};
    return Number(map[cap] || 0);
  }

  // Sum the device count across an arbitrary set of group ids. Used by
  // the compact "Groups" target summary (operator's request: show
  // "N groups · M devices" inline in the action body so they don't
  // need to open the picker dialog to see the scope). When ``cap`` is
  // set, only devices that satisfy the action's capability count.
  function totalDevicesInGroups(groupIds, cap){
    if(!Array.isArray(groupIds) || !groupIds.length) return 0;
    const ids = new Set(groupIds);
    let total = 0;
    (state.devices || []).forEach(d => {
      if(!ids.has(d.groupId)) return;
      if(cap && !deviceHasCap(d, cap)) return;
      total += 1;
    });
    return total;
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

  // ---- Unified target shape (broadcast / groups / device) ----------
  //
  // See `docs/reference/broadcast-ruleset.md` and
  // `racelink/services/scenes_service.py::_canonical_target` for the
  // canonical definition. The JS side reads/writes this shape directly;
  // legacy on-disk shapes are migrated by the backend at load and save,
  // but in-memory drafts may still carry pre-migration shapes after a
  // partial WebUI render race — the helpers below absorb that.
  //
  // Shapes:
  //   { kind: "broadcast" }
  //   { kind: "groups",    value: [<int>, ...] }
  //   { kind: "device",    value: "<12-char MAC>" }

  function migrateLegacyTarget(target){
    // Translate the two superseded shapes to the unified one. Returns a
    // *new* object when a rewrite happened; passes the input through
    // otherwise. Caller assigns the result back to its container.
    if(!target || typeof target !== "object") return target;
    if(target.kind === "scope") return { kind: "broadcast" };
    if(target.kind === "group"){
      const v = Number(target.value);
      if(Number.isFinite(v)) return { kind: "groups", value: [v] };
    }
    return target;
  }

  function ensureContainerTarget(action){
    // Offset_group containers used to carry ``action.groups`` ("all" or
    // a list of ints). The unified shape uses ``action.target`` like
    // every other action. Migrate legacy state in place so every
    // downstream reader sees a single canonical field.
    if(action.target && typeof action.target === "object"){
      action.target = migrateLegacyTarget(action.target);
      delete action.groups;  // shouldn't coexist; defensive.
      return action.target;
    }
    if(action.groups === "all" || action.groups === 255){
      action.target = { kind: "broadcast" };
    }else if(Array.isArray(action.groups)){
      const ids = action.groups
        .map(g => (typeof g === "number") ? g : (g && g.id))
        .filter(n => Number.isFinite(n) && n >= 0 && n <= 254)
        .sort((a, b) => a - b);
      action.target = ids.length
        ? { kind: "groups", value: ids }
        : { kind: "broadcast" };
    }else{
      action.target = { kind: "broadcast" };
    }
    delete action.groups;
    return action.target;
  }

  function targetIsBroadcast(target){
    return !!target && target.kind === "broadcast";
  }

  function targetGroupIds(target, fallbackAllIds){
    // Resolve the unified target to the concrete group-id list it
    // implies. ``broadcast`` returns the fallback (every known group);
    // ``groups`` returns the explicit list; ``device`` returns [].
    if(!target) return [];
    if(target.kind === "broadcast") return fallbackAllIds.slice();
    if(target.kind === "groups" && Array.isArray(target.value)){
      return target.value.slice();
    }
    return [];
  }

  function ensureOffsetGroupShape(action){
    // Seed sensible defaults if a draft is missing ``target`` /
    // ``offset`` / ``actions`` so the panel can render without crashing.
    ensureContainerTarget(action);
    if(!action.offset || typeof action.offset !== "object"){
      action.offset = { ...OFFSET_FORMULA_DEFAULTS };
    }
    if(!Array.isArray(action.actions)){
      action.actions = [];
    }
    return action;
  }

  function getSelectedIdsFromHolder(holder){
    // ``holder`` is an offset_group container action. Returns
    // ``{ all: bool, ids: [int,...] }`` derived from the unified
    // ``holder.target`` shape (broadcast → every known group; groups →
    // explicit subset).
    const target = ensureContainerTarget(holder);
    if(targetIsBroadcast(target)){
      return { all: true, ids: knownGroupIds() };
    }
    if(target.kind === "groups" && Array.isArray(target.value)){
      const ids = target.value
        .filter(n => Number.isFinite(n) && n >= 0 && n <= 254)
        .slice()
        .sort((a, b) => a - b);
      return { all: false, ids };
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

  // Strip ``_ui`` and any ``__*`` render-side scratch fields from an
  // action tree before save; the server drops unknown keys but we send
  // a clean payload for cleaner debugging / API logs. Container actions
  // are recursed so nested children also get cleaned.
  //
  // The dunder-prefix strip is also load-bearing for the dirty-tracker:
  // ``_canonicalDraftJson`` runs this function so any render-time scratch
  // (e.g. a stale ``__cid`` from a pre-fix draft) is normalised away
  // before comparison. Stick to dunder-prefixed names for any future
  // render scratch so this stays automatic.
  function stripUiBeforeSave(actions){
    return (actions || []).map(a => {
      const out = {};
      for(const key of Object.keys(a)){
        if(key === "_ui") continue;
        if(key.startsWith("__")) continue;
        out[key] = a[key];
      }
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

  // 2026-04-28: cost badges optionally show *measured* wall-clock
  // duration alongside the estimated airtime. ``measuredMs`` is the
  // last-run duration_ms for that action (or the sum across all
  // actions for the total badge). When supplied, an "actual" fragment
  // is appended in a span with the ``rl-scene-cost-actual`` class so
  // CSS can style it distinctly from the estimate.
  //
  // Returns a DocumentFragment (not a string) when ``measuredMs`` is
  // present, so the styling-span can be inlined; falls back to a
  // plain string for the estimate-only case to preserve the existing
  // ``textContent`` callsites' simplicity.
  function formatCost(cost, measuredMs){
    if(!cost) return "≈ —";
    // Prefer wall_clock_ms (LoRa airtime + per-packet USB/gateway overhead)
    // so the badge prediction matches the runner's measured `actual:`. Fall
    // back to airtime_ms for back-compat with any older API response (e.g.
    // a stale RotorHazard build serving an older host plugin).
    const ms = Math.round(
      (cost.wall_clock_ms != null ? cost.wall_clock_ms : cost.airtime_ms) || 0
    );
    const estimate = `≈ ${cost.packets} pkts · ${cost.bytes} B · ${ms} ms`;
    if(measuredMs === undefined || measuredMs === null) return estimate;
    return { estimate, measuredMs: Math.round(Number(measuredMs) || 0) };
  }

  function setBadgeContent(el, formatted){
    // Accepts either a plain string (estimate only) or an object
    // ``{estimate, measuredMs}``. The object form renders the actual
    // fragment in a styled child span.
    if(typeof formatted === "string"){
      el.textContent = formatted;
      return;
    }
    el.textContent = formatted.estimate + " · ";
    const actual = document.createElement("span");
    actual.className = "rl-scene-cost-actual";
    actual.textContent = `actual: ${formatted.measuredMs} ms`;
    el.appendChild(actual);
  }

  function loraTooltip(measuredMs, predictedWallClockMs, estimatedAirtimeMs){
    const lora = state.scenes.schema && state.scenes.schema.lora;
    let base = "";
    if(lora){
      const bw = (lora.bw_hz / 1000).toFixed(0);
      const overhead = lora.wire_overhead_ms_per_packet;
      const overheadFrag = (overhead != null)
        ? ` · +${Math.round(overhead)} ms/pkt USB+gateway overhead`
        : "";
      base = `at SF${lora.sf}/${bw} kHz/CR4:${lora.cr}` +
             ` · bytes include Header7 + USB framing` +
             ` · airtime via Semtech AN1200.13${overheadFrag}`;
    }
    // Optional second line: per-action airtime breakdown so the operator
    // can see how much of the wall-clock prediction is pure radio vs.
    // host-side roundtrip cost. Both inputs are estimates from the API.
    const est = Math.round(Number(predictedWallClockMs) || 0);
    const air = Math.round(Number(estimatedAirtimeMs) || 0);
    const overheadMs = Math.max(0, est - air);
    const breakdown = (est > 0 && air > 0)
      ? `Predicted: ${est} ms (LoRa airtime ${air} ms + radio/USB overhead ${overheadMs} ms).`
      : "";
    if(measuredMs === undefined || measuredMs === null){
      return [base, breakdown].filter(Boolean).join("\n");
    }
    // Measured includes the same wall-clock overhead the prediction
    // already models. Any residual delta is calibration drift (e.g.
    // gateway latency_timer not actually at 1 ms, scene-runner step
    // overhead between actions) — small, but visible here.
    const meas = Math.round(Number(measuredMs) || 0);
    const delta = meas - est;
    const sign = delta >= 0 ? "+" : "";
    return [
      base,
      breakdown,
      `Last run: ${meas} ms wall-clock (predicted ${est} ms · ${sign}${delta} ms residual).`,
    ].filter(Boolean).join("\n");
  }

  function _measuredDurationsFromLastRun(){
    // Returns ``{perAction: Map<idx, ms>, total: ms | null}`` from the
    // last successful run. Operators see measured values *only* until
    // they start a new run or load a different scene; after that the
    // map is empty and badges fall back to estimate-only rendering.
    const lr = state.scenes && state.scenes.lastRunResult;
    if(!lr || !Array.isArray(lr.actions)) return { perAction: new Map(), total: null };
    const perAction = new Map();
    let total = 0;
    lr.actions.forEach(a => {
      if(typeof a.duration_ms === "number"){
        perAction.set(Number(a.index), a.duration_ms);
        total += a.duration_ms;
      }
    });
    return { perAction, total: perAction.size ? total : null };
  }

  function applyCostPayload(seq, payload){
    if(seq !== _costFetchSeq) return;        // stale response
    if(!payload || !payload.ok) return;
    const measured = _measuredDurationsFromLastRun();
    const tot = document.getElementById("sceneCostTotal");
    if(tot){
      const t = payload.total || {};
      // wall_clock_ms is the new (Batch B follow-up) prediction the cost
      // badge displays; airtime_ms remains for the tooltip's breakdown
      // line. Older API responses without wall_clock_ms fall back to
      // airtime_ms (no overhead modelled there).
      const totalWallClockMs = (t.wall_clock_ms != null ? t.wall_clock_ms : t.airtime_ms) || 0;
      const totalAirtimeMs   = t.airtime_ms || 0;
      const formatted = formatCost(t, measured.total);
      tot.textContent = "";
      tot.appendChild(document.createTextNode("Total "));
      setBadgeContent(tot, formatted);
      tot.title = loraTooltip(measured.total, totalWallClockMs, totalAirtimeMs);
    }
    const editor = document.getElementById("sceneEditor");
    if(!editor) return;
    (payload.per_action || []).forEach((cost, idx) => {
      const row = editor.querySelector(`[data-action-idx="${idx}"]`);
      if(!row) return;
      const badge = row.querySelector(".rl-scene-action-cost");
      if(!badge) return;
      const actionMeasured = measured.perAction.has(idx)
        ? measured.perAction.get(idx)
        : null;
      const formatted = formatCost(cost, actionMeasured);
      setBadgeContent(badge, formatted);
      const wallClock = cost && (cost.wall_clock_ms != null ? cost.wall_clock_ms : cost.airtime_ms);
      const airtime   = cost && cost.airtime_ms;
      badge.title = loraTooltip(actionMeasured, wallClock, airtime);
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
      // Default to "broadcast (all groups) + Linear, base=0, step=100" —
      // the most common operator intent and the cheapest wire path (one
      // broadcast OPC_OFFSET packet, see the Strategy A discussion in
      // docs/reference/broadcast-ruleset.md). Children list starts empty.
      action.target = { kind: "broadcast" };
      action.offset = { mode: "linear", base_ms: 0, step_ms: 100 };
      action.actions = [];
      return action;
    }
    // Top-level effect actions default to a single-group target — the
    // most common starting point. The unified shape keeps the value as
    // a length-1 list; the operator can switch to broadcast or device
    // via the picker.
    action.target = { kind: "groups", value: [1] };
    action.params = {};
    if(meta && meta.supports_flags_override){
      action.flags_override = {};
    }
    return action;
  }

  function defaultOffsetGroupChild(kind){
    // Children default to broadcast — the cheapest wire path and the
    // intent most often expressed by an offset_group container ("apply
    // this effect to every offset-configured device").
    const action = { kind, target: { kind: "broadcast" }, params: {} };
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

  // C11: dirty-tracking. ``pristineDraftJson`` is the canonical
  // serialised form of the draft at the moment it was loaded /
  // last saved. ``isDraftDirty`` recomputes the same canonical form
  // and compares; truthy means "the operator has unsaved edits".
  // Used by the beforeunload listener (browser-level navigation
  // away) and by selectScene / newSceneDraft (in-editor draft
  // swap) to confirm before discarding work.
  function _canonicalDraftJson(draft){
    if(!draft) return "";
    return JSON.stringify({
      label: String(draft.label || ""),
      actions: stripUiBeforeSave(draft.actions || []),
      // Include stop_on_error so toggling the editor checkbox marks
      // the draft dirty (would otherwise silently round-trip).
      stop_on_error: (draft.stop_on_error === undefined || draft.stop_on_error === null)
                     ? true
                     : !!draft.stop_on_error,
    });
  }
  function _markPristine(){
    state.scenes.pristineDraftJson = _canonicalDraftJson(state.scenes.draft);
  }
  function isDraftDirty(){
    const draft = state.scenes.draft;
    if(!draft) return false;
    const baseline = state.scenes.pristineDraftJson;
    if(baseline === undefined) return false;  // never seeded yet
    return _canonicalDraftJson(draft) !== baseline;
  }
  function _confirmDiscardIfDirty(){
    if(!isDraftDirty()) return true;
    return RL.confirmDestructive(
      "You have unsaved changes to this scene. Discard them and continue?"
    );
  }

  function selectScene(key){
    if(!_confirmDiscardIfDirty()) return;
    state.scenes.selectedKey = key;
    state.scenes.lastRunResult = null;
    const scene = state.scenes.items.find(s => s.key === key) || null;
    state.scenes.draft = scene ? cloneAction(scene) : null;
    _markPristine();
    renderSceneList();
    renderSceneEditor();
  }

  function newSceneDraft(){
    if(!_confirmDiscardIfDirty()) return;
    state.scenes.selectedKey = null;
    state.scenes.lastRunResult = null;
    state.scenes.draft = { id: null, key: null, label: "", actions: [] };
    _markPristine();
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

    // Stop-on-error toggle — Batch A (2026-04-28). Default true (the
    // safer behaviour: a half-failed sequence aborts the rest rather
    // than wasting air-time on packets that can't reach the intended
    // state). Stored on the scene root, persisted via the API.
    const stopOnErrorWrap = document.createElement("label");
    stopOnErrorWrap.className = "inline rl-scene-stop-on-error";
    stopOnErrorWrap.title = "Abort the run on the first action that fails. " +
                            "Off = play through every action regardless of errors.";
    const stopOnErrorCb = document.createElement("input");
    stopOnErrorCb.type = "checkbox";
    stopOnErrorCb.id = "sceneStopOnError";
    // Default true for missing/legacy field. Server's _coerce_bool
    // normalises both directions — frontend just round-trips the bool.
    stopOnErrorCb.checked = (draft.stop_on_error === undefined || draft.stop_on_error === null)
                            ? true
                            : !!draft.stop_on_error;
    stopOnErrorCb.addEventListener("change", () => {
      draft.stop_on_error = !!stopOnErrorCb.checked;
    });
    stopOnErrorWrap.appendChild(stopOnErrorCb);
    stopOnErrorWrap.appendChild(document.createTextNode(" Stop on error"));
    meta.appendChild(stopOnErrorWrap);

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
      // Batch A (2026-04-28): aborted runs surface a clear message
      // separate from the generic "failed" — the operator knows
      // exactly where the sequence stopped and which actions never
      // ran.
      if(r.aborted_at_index !== undefined && r.aborted_at_index !== null){
        const failedIdx = Number(r.aborted_at_index);
        const failed = (r.actions || []).find(a => a.index === failedIdx);
        const why = (failed && failed.error) ? failed.error : "failed";
        status.textContent =
          `Last run: aborted at action #${failedIdx + 1} (${why}). ` +
          `Remaining actions skipped — uncheck "Stop on error" to play through.`;
      } else {
        status.textContent = r.ok ? "Last run: OK" : `Last run: ${r.error || "failed"}`;
      }
      strip.appendChild(status);
      (r.actions || []).forEach(a => {
        const pip = document.createElement("span");
        // ``error: "skipped: aborted"`` is the placeholder the runner
        // emits for actions after the abort point. Render with a
        // distinct ``skipped`` class so CSS can mute them.
        const isSkipped = (a.error && a.error.startsWith("skipped"));
        let cls;
        if(isSkipped) cls = "skipped";
        else if(a.degraded) cls = "degraded";
        else if(a.ok) cls = "ok";
        else cls = "error";
        pip.className = "pip " + cls;
        // Display rebased to 1 to match the action-row labels (#1 #2 …).
        // The runner's ActionResult.index stays 0-based for log/structured output.
        const display = a.index + 1;
        pip.textContent = String(display);
        const tooltipBits = [`#${display}`, a.kind];
        if(a.error) tooltipBits.push(`— ${a.error}`);
        if(!isSkipped) tooltipBits.push(`(${a.duration_ms}ms)`);
        pip.title = tooltipBits.join(" ");
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

  // Build the permutation map for a single splice-style move: the action
  // at ``from`` ends up at ``to``; other actions shift through the gap. The
  // returned ``map`` is such that ``map[oldIdx] === newIdx`` for every
  // index in [0, n). Used to keep ``state.scenes.lastRunResult`` indices in
  // sync with the displayed action order so "actual" times follow their
  // action on a reorder rather than staying at the old slot.
  function _permutationFromMove(from, to, n){
    const map = new Array(n);
    for(let i = 0; i < n; i++) map[i] = i;
    if(from === to) return map;
    map[from] = to;
    if(from < to){
      for(let i = from + 1; i <= to; i++) map[i] = i - 1;
    } else {
      for(let i = to; i < from; i++) map[i] = i + 1;
    }
    return map;
  }

  // Apply the permutation to ``state.scenes.lastRunResult`` so post-run
  // indices follow the actions through the move. Top-level only — nested
  // child reorders inside an offset_group don't surface in lastRunResult
  // (the offset_group is treated as a single action by the runner).
  function _remapLastRunResultIndices(map){
    const lr = state.scenes && state.scenes.lastRunResult;
    if(!lr) return;
    if(Array.isArray(lr.actions)){
      lr.actions.forEach(a => {
        if(typeof a.index === "number" && map[a.index] !== undefined){
          a.index = map[a.index];
        }
      });
    }
    if(typeof lr.aborted_at_index === "number" && map[lr.aborted_at_index] !== undefined){
      lr.aborted_at_index = map[lr.aborted_at_index];
    }
  }

  function moveAction(draft, idx, delta){
    const j = idx + delta;
    if(j < 0 || j >= draft.actions.length) return;
    const [item] = draft.actions.splice(idx, 1);
    draft.actions.splice(j, 0, item);
    _remapLastRunResultIndices(_permutationFromMove(idx, j, draft.actions.length));
    renderSceneEditor();
  }

  function reorderActions(draft, oldIndex, newIndex){
    if(oldIndex === newIndex) return;
    if(oldIndex < 0 || oldIndex >= draft.actions.length) return;
    if(newIndex < 0 || newIndex >= draft.actions.length) return;
    const [item] = draft.actions.splice(oldIndex, 1);
    draft.actions.splice(newIndex, 0, item);
    _remapLastRunResultIndices(_permutationFromMove(oldIndex, newIndex, draft.actions.length));
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

  // ---- Unified target picker ----------------------------------------
  //
  // One implementation drives every target selection in the scene
  // editor: top-level effect actions, offset_group containers, and
  // offset_group children. Three radios — Broadcast / Groups / Device —
  // with the value picker (multi-select / single device) appearing
  // below when the active radio needs one.
  //
  // ``opts`` keys:
  //   scope        "top" | "container" | "child" — picks the radio
  //                set (container hides Device) and the wire-shape
  //                guidance shown in the inline hint.
  //   namePrefix   Unique-per-page DOM name prefix for the radio set.
  //   actionKind   The action's kind, passed through to
  //                ``requiredCapForKind`` for capability filtering.
  //   parentTarget (child scope only) the parent offset_group's
  //                target — restricts which group ids the child may
  //                pick when parent is a sparse `groups` list.
  //   onChange     Optional. Called after every target mutation.
  //                Defaults to ``renderSceneEditor`` so dependent UI
  //                (cost badge, child target dropdowns, …) re-renders.
  function buildUnifiedTargetPicker(action, opts){
    const scope = opts.scope || "top";
    const namePrefix = opts.namePrefix || `target-${scope}-${Date.now()}`;
    const actionKind = opts.actionKind || action.kind;
    const onChange = opts.onChange || (() => renderSceneEditor());
    const parentTarget = opts.parentTarget || null;

    // Migrate any legacy in-memory shape on first render so the rest of
    // the picker only deals with the canonical unified target.
    if(scope === "container"){
      ensureContainerTarget(action);
    }else{
      action.target = migrateLegacyTarget(action.target) || { kind: "broadcast" };
    }

    const wrap = document.createElement("div");
    wrap.className = "rl-scene-target";

    // ---- Radio row ---------------------------------------------------
    const radioRow = document.createElement("div");
    radioRow.className = "rl-scene-target-radios";

    // Container-scope picker hides "Device" (offset is per-group, so a
    // single device target is invalid at the container level — see
    // scenes_service._canonical_offset_group_container_target).
    const radioKinds = (scope === "container")
      ? [["broadcast", "Broadcast"], ["groups", "Groups"]]
      : [["broadcast", "Broadcast"], ["groups", "Groups"], ["device", "Device"]];

    const radios = {};
    radioKinds.forEach(([kind, lbl]) => {
      const r = document.createElement("input");
      r.type = "radio";
      r.name = namePrefix;
      r.value = kind;
      r.checked = (action.target.kind === kind);
      radios[kind] = r;
      const wl = document.createElement("label");
      wl.className = "inline";
      wl.appendChild(r);
      wl.appendChild(document.createTextNode(" " + lbl));
      radioRow.appendChild(wl);
    });
    wrap.appendChild(radioRow);

    // ---- Body holder (per-kind value picker) -------------------------
    const bodyHolder = document.createElement("div");
    bodyHolder.className = "rl-scene-target-body";
    wrap.appendChild(bodyHolder);

    function switchTo(newKind){
      const previous = action.target || {};
      if(newKind === "broadcast"){
        action.target = { kind: "broadcast" };
      }else if(newKind === "groups"){
        const allowed = allowedGroupIdsForChild();
        const seedFromPrev = (previous.kind === "groups" && Array.isArray(previous.value))
          ? previous.value.filter(id => allowed === null || allowed.includes(id))
          : [];
        const seed = seedFromPrev.length
          ? seedFromPrev
          : ((allowed && allowed.length) ? [allowed[0]]
             : ((knownGroupIds()[0] !== undefined) ? [knownGroupIds()[0]] : [1]));
        action.target = { kind: "groups", value: seed.slice().sort((a, b) => a - b) };
      }else if(newKind === "device"){
        const def = (previous.kind === "device" && typeof previous.value === "string")
          ? previous.value : ((state.devices || [])[0]?.addr || "AABBCCDDEEFF");
        action.target = { kind: "device", value: String(def).toUpperCase() };
      }
      onChange();
    }

    function allowedGroupIdsForChild(){
      // For offset_group children, the "Groups" multi-select is
      // restricted to the parent's participating groups. Null means
      // "no parent restriction" (top-level / container scopes, or
      // parent.target == broadcast).
      if(scope !== "child" || !parentTarget) return null;
      if(targetIsBroadcast(parentTarget)) return null;
      if(parentTarget.kind === "groups" && Array.isArray(parentTarget.value)){
        return parentTarget.value.slice();
      }
      return null;
    }

    Object.entries(radios).forEach(([kind, r]) => {
      r.addEventListener("change", () => { if(r.checked) switchTo(kind); });
    });

    // ---- Body content per radio kind --------------------------------
    function renderBody(){
      bodyHolder.innerHTML = "";
      const tk = action.target.kind;
      if(tk === "broadcast"){
        // No value picker; explain the wire effect inline so operators
        // (especially new ones) know what they just selected.
        const hint = document.createElement("span");
        hint.className = "muted";
        hint.textContent = scope === "child"
          ? "→ every offset-configured device (groupId=255)."
          : "→ every device (recv3=FFFFFF, groupId=255).";
        bodyHolder.appendChild(hint);
        return;
      }
      if(tk === "groups"){
        bodyHolder.appendChild(buildGroupsMultiSelect(action, {
          actionKind,
          allowed: allowedGroupIdsForChild(),
          onChange,
        }));
        return;
      }
      if(tk === "device"){
        bodyHolder.appendChild(buildDeviceSingleSelect(action, {
          actionKind,
          allowedGroupIds: allowedGroupIdsForChild(),
          onChange,
        }));
        return;
      }
    }
    renderBody();

    return wrap;
  }

  // ---- Groups target: compact summary chip + Edit dialog ------------
  //
  // Operators routinely have 10–50 groups in a fleet; the previous
  // inline checkbox list grew unbounded and crowded the action body.
  // The new design pushes the picker into a modal: the action shows
  // a compact summary (counts + small-text list of selected names);
  // clicking *Edit groups…* opens a dialog with a search field, a
  // filtered result list, and three batch operations that act on the
  // currently-visible *hits* (Select all hits / None / Invert).
  function buildGroupsMultiSelect(action, opts){
    const actionKind = opts.actionKind || action.kind;
    const allowed = opts.allowed;  // null → all known groups
    const onChange = opts.onChange || (() => renderSceneEditor());

    const wrap = document.createElement("div");
    wrap.className = "rl-scene-target-groups";

    const cap = requiredCapForKind(actionKind);
    const knownIds = knownGroupIds();
    // When a parent restricts the choice (offset_group child), the
    // available pool is the parent's groups; otherwise it's the full
    // known fleet. The picker further filters by capability so the
    // operator can't select groups with zero capable devices.
    const poolIds = (allowed === null) ? knownIds : allowed;
    const idsForPicker = poolIds.filter(id => {
      if(!cap) return true;
      const meta = (state.groups || []).find(g => ((typeof g.id === "number") ? g.id : g.groupId) === id);
      return (groupCapCount(meta, cap) || 0) > 0;
    });

    // Empty-state: no groups available (e.g. the operator hasn't
    // discovered or assigned any yet, or the parent's scope filtered
    // every group out for this action's cap).
    if(!idsForPicker.length){
      const empty = document.createElement("span");
      empty.className = "muted";
      empty.textContent = cap
        ? `(no groups with ${cap} devices — assign one first)`
        : "(no groups available)";
      wrap.appendChild(empty);
      return wrap;
    }

    // ``selected`` is the persisted state on disk. The dialog edits a
    // *copy*; only on Apply do we write back to ``action.target``.
    function readSelected(){
      const raw = Array.isArray(action.target.value) ? action.target.value : [];
      return new Set(raw.filter(id => idsForPicker.includes(id)));
    }

    function renderSummary(){
      wrap.innerHTML = "";
      const selected = readSelected();
      const selectedIds = Array.from(selected).sort((a, b) => a - b);

      // Counts row: "N groups · M devices" + Edit button.
      const head = document.createElement("div");
      head.className = "rl-scene-target-groups-summary-head";
      const counts = document.createElement("span");
      counts.className = "rl-scene-target-groups-summary-counts";
      const deviceCount = totalDevicesInGroups(selectedIds, cap);
      const groupWord = (selectedIds.length === 1) ? "group" : "groups";
      const deviceWord = (deviceCount === 1) ? "device" : "devices";
      const capLabel = cap ? ` ${cap}` : "";
      counts.textContent = `${selectedIds.length} ${groupWord} · ${deviceCount}${capLabel} ${deviceWord}`;
      head.appendChild(counts);
      const editBtn = document.createElement("button");
      editBtn.type = "button";
      editBtn.className = "btn rl-scene-target-groups-summary-edit";
      editBtn.textContent = "Edit groups…";
      editBtn.addEventListener("click", () => {
        openGroupsSelectionDialog({
          ids: idsForPicker,
          cap,
          initialSelected: readSelected(),
          knownIds,
          totalKnownCount: knownIds.length,
          allowed: allowed,
        }).then(result => {
          if(!result) return;  // operator cancelled — leave state alone
          action.target = {
            kind: "groups",
            value: Array.from(result).sort((a, b) => a - b),
          };
          onChange();
        });
      });
      head.appendChild(editBtn);
      wrap.appendChild(head);

      // Selected-names row: small-text comma-separated list, capped
      // at 8 entries with a trailing "+ K more" hint to keep the row
      // scannable. Empty selection shows a muted "(none selected)".
      const list = document.createElement("div");
      list.className = "rl-scene-target-groups-summary-list muted";
      if(!selectedIds.length){
        list.textContent = "(none selected — click Edit to choose)";
      }else{
        const labels = selectedIds.map(id => {
          const meta = (state.groups || []).find(g => ((typeof g.id === "number") ? g.id : g.groupId) === id);
          return `${meta?.name || ("Group " + id)} (${id})`;
        });
        const head = labels.slice(0, 8).join(", ");
        const tail = labels.length > 8 ? ` + ${labels.length - 8} more` : "";
        list.textContent = head + tail;
      }
      wrap.appendChild(list);

      // ---- "Select all → broadcast" hint ----
      // Mirrors scenes_service.collapse_actions_to_broadcast: when the
      // operator picks every currently-known group, the backend
      // rewrites the persisted target to {kind: "broadcast"} on save.
      // Surface the rewrite intent here so the cost badge's eventual
      // change isn't a surprise.
      if(allowed === null && selected.size === knownIds.length && knownIds.length){
        const hint = document.createElement("span");
        hint.className = "muted rl-scene-target-allgroups-hint";
        hint.textContent = "(All groups selected → will save as Broadcast.)";
        wrap.appendChild(hint);
      }
    }

    renderSummary();
    return wrap;
  }

  // ---- Groups selection dialog --------------------------------------
  //
  // Opens a modal with a search field, a filtered checkbox list, and
  // three batch buttons that act on the currently-visible *hits* (per
  // the operator's request — batch operations on the filtered
  // sub-list are far more useful than batch operations on the whole
  // pool when the fleet has many groups).
  //
  // Returns a Promise that resolves to:
  //   * a ``Set<number>`` of group ids when the operator clicks Apply
  //   * ``null`` when the operator cancels (or closes the dialog)
  //
  // The caller is responsible for writing the result back to
  // ``action.target`` and triggering the editor re-render.
  function openGroupsSelectionDialog(opts){
    const {ids, cap, initialSelected, knownIds, totalKnownCount, allowed} = opts;
    const groupMeta = id => (state.groups || []).find(
      g => ((typeof g.id === "number") ? g.id : g.groupId) === id,
    );

    const dlg = document.createElement("dialog");
    dlg.className = "rl-groups-dialog";

    const form = document.createElement("form");
    form.method = "dialog";
    dlg.appendChild(form);

    const title = document.createElement("h3");
    title.textContent = "Select groups";
    form.appendChild(title);

    // Search row.
    const searchRow = document.createElement("div");
    searchRow.className = "rl-groups-dialog-search";
    const search = document.createElement("input");
    search.type = "search";
    search.placeholder = "Filter by name or id…";
    search.autocomplete = "off";
    searchRow.appendChild(search);
    form.appendChild(searchRow);

    // Batch buttons — operate on currently-visible (filtered) hits.
    const batchRow = document.createElement("div");
    batchRow.className = "rl-groups-dialog-actions";
    function makeBatchBtn(label, fn){
      const b = document.createElement("button");
      b.type = "button";
      b.className = "btn";
      b.textContent = label;
      b.addEventListener("click", fn);
      return b;
    }
    const btnSelectHits = makeBatchBtn("Select all hits", () => {
      visibleIds().forEach(id => selected.add(id));
      renderList();
      renderFooter();
    });
    const btnDeselectHits = makeBatchBtn("Deselect all hits", () => {
      visibleIds().forEach(id => selected.delete(id));
      renderList();
      renderFooter();
    });
    const btnInvertHits = makeBatchBtn("Invert hits", () => {
      visibleIds().forEach(id => {
        if(selected.has(id)) selected.delete(id);
        else selected.add(id);
      });
      renderList();
      renderFooter();
    });
    batchRow.appendChild(btnSelectHits);
    batchRow.appendChild(btnDeselectHits);
    batchRow.appendChild(btnInvertHits);
    form.appendChild(batchRow);

    // Result list — scrollable container so the dialog stays a
    // sensible size on large fleets.
    const list = document.createElement("div");
    list.className = "rl-groups-dialog-list";
    form.appendChild(list);

    // Footer: counts + hint + Cancel / Apply.
    const footer = document.createElement("div");
    footer.className = "rl-groups-dialog-footer";
    const footerCounts = document.createElement("span");
    footerCounts.className = "rl-groups-dialog-footer-counts";
    const footerHint = document.createElement("span");
    footerHint.className = "muted rl-groups-dialog-footer-hint";
    footer.appendChild(footerCounts);
    footer.appendChild(footerHint);
    form.appendChild(footer);

    const buttons = document.createElement("menu");
    buttons.className = "rl-groups-dialog-buttons";
    const cancelBtn = document.createElement("button");
    cancelBtn.type = "button";
    cancelBtn.textContent = "Cancel";
    const applyBtn = document.createElement("button");
    applyBtn.type = "button";
    applyBtn.textContent = "Apply";
    applyBtn.className = "primary";
    buttons.appendChild(cancelBtn);
    buttons.appendChild(applyBtn);
    form.appendChild(buttons);

    document.body.appendChild(dlg);

    // Working copy of the selection — apply on confirm, drop on cancel.
    const selected = new Set(initialSelected);

    // Pre-built rows for each group id, indexed by id, so re-renders
    // on search input don't rebuild DOM nodes (just toggle hidden).
    const rowById = new Map();
    ids.forEach(id => {
      const meta = groupMeta(id);
      const row = document.createElement("label");
      row.className = "rl-toggle-wrap rl-groups-dialog-row";
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.addEventListener("change", () => {
        if(cb.checked) selected.add(id); else selected.delete(id);
        renderFooter();
      });
      const txt = document.createElement("span");
      const annot = cap
        ? ` — ${groupCapCount(meta, cap) || 0} ${cap}`
        : "";
      txt.textContent = `${meta?.name || ("Group " + id)} (${id})${annot}`;
      row.appendChild(cb);
      row.appendChild(txt);
      list.appendChild(row);
      rowById.set(id, {row, cb, name: String(meta?.name || ""), id});
    });

    function visibleIds(){
      const out = [];
      for(const [id, entry] of rowById){
        if(entry.row.style.display !== "none") out.push(id);
      }
      return out;
    }

    function renderList(){
      const q = search.value.trim().toLowerCase();
      for(const [id, entry] of rowById){
        let visible = true;
        if(q){
          const hay = `${entry.name.toLowerCase()} ${entry.id}`;
          visible = hay.includes(q);
        }
        entry.row.style.display = visible ? "" : "none";
        entry.cb.checked = selected.has(id);
      }
    }

    function renderFooter(){
      const selArr = Array.from(selected).sort((a, b) => a - b);
      const groupWord = (selArr.length === 1) ? "group" : "groups";
      const deviceCount = totalDevicesInGroups(selArr, cap);
      const deviceWord = (deviceCount === 1) ? "device" : "devices";
      const capLabel = cap ? ` ${cap}` : "";
      footerCounts.textContent =
        `Selected: ${selArr.length} ${groupWord} · ${deviceCount}${capLabel} ${deviceWord}`;
      // Same broadcast-collapse hint as the inline summary, only
      // shown when the operator is editing the top-level groups
      // pool (no parent restricts the choice) and has ticked every
      // known group.
      const willCollapse = (
        allowed === null &&
        totalKnownCount > 0 &&
        selected.size === totalKnownCount
      );
      footerHint.textContent = willCollapse
        ? "(All groups selected → will save as Broadcast.)"
        : "";
    }

    return new Promise(resolve => {
      function close(value){
        try { dlg.close(); } catch(_){}
        // Defer removal so any in-flight close handlers see the
        // dialog before it's gone.
        setTimeout(() => { try { dlg.remove(); } catch(_){} }, 0);
        resolve(value);
      }
      cancelBtn.addEventListener("click", () => close(null));
      applyBtn.addEventListener("click", () => close(new Set(selected)));
      dlg.addEventListener("cancel", e => { e.preventDefault(); close(null); });
      search.addEventListener("input", () => renderList());
      // Submit-on-Enter (default form behaviour) → Apply.
      form.addEventListener("submit", e => {
        e.preventDefault();
        close(new Set(selected));
      });

      renderList();
      renderFooter();
      dlg.showModal();
      // Focus the search field so power users can start typing
      // immediately.
      setTimeout(() => { try { search.focus(); } catch(_){} }, 0);
    });
  }

  // ---- "Device" single-select with cap + parent-scope filters -------
  function buildDeviceSingleSelect(action, opts){
    const actionKind = opts.actionKind || action.kind;
    const allowedGroupIds = opts.allowedGroupIds;  // null → no restriction
    const onChange = opts.onChange || (() => renderSceneEditor());

    const wrap = document.createElement("div");
    const cap = requiredCapForKind(actionKind);

    const sel = document.createElement("select");
    (state.devices || []).forEach(d => {
      if(!d.addr) return;
      const addr = String(d.addr).toUpperCase();
      if(addr.length !== 12) return;
      // Parent-scope filter (offset_group children only).
      if(allowedGroupIds !== null && !allowedGroupIds.includes(d.groupId)) return;
      // C5 capability filter.
      if(cap && !deviceHasCap(d, cap)) return;
      const opt = document.createElement("option");
      opt.value = addr;
      opt.textContent = `${d.name || addr} (${addr})`;
      if(action.target.kind === "device" &&
         String(action.target.value).toUpperCase() === addr){
        opt.selected = true;
      }
      sel.appendChild(opt);
    });
    if(!sel.options.length){
      const opt = document.createElement("option");
      opt.value = "AABBCCDDEEFF";
      opt.textContent = (cap || allowedGroupIds !== null)
        ? "(no matching devices — discover or assign one first)"
        : "(no devices yet — placeholder)";
      sel.appendChild(opt);
    }
    sel.addEventListener("change", () => {
      action.target = { kind: "device", value: String(sel.value || "").toUpperCase() };
      onChange();
    });
    // C5 sync: if the existing target was filtered out, snap to option[0].
    if(sel.selectedIndex < 0 && sel.options.length){
      sel.selectedIndex = 0;
      sel.dispatchEvent(new Event("change"));
    }
    wrap.appendChild(sel);
    return wrap;
  }

  function buildTargetPicker(action, idx, draft){
    return buildUnifiedTargetPicker(action, {
      scope: "top",
      namePrefix: `target-kind-${idx}`,
      actionKind: action.kind,
    });
  }

  function buildOffsetGroupConfigPanel(action, idx, draft){
    // Renders the target + offset configuration for an offset_group
    // container action. The target picker (Broadcast / Groups —
    // device is invalid at the container level) is the unified shared
    // component used by every other action; this function adds the
    // formula-mode selector, the per-mode parameter inputs, and the
    // live preview on top.
    ensureOffsetGroupShape(action);
    const wrap = document.createElement("div");
    wrap.className = "rl-groups-offset";

    const knownIds = knownGroupIds();
    const offset = action.offset;

    function repaintAll(){
      // Re-derive offset.values from the current target when in
      // Explicit mode, so toggling group selection in the picker keeps
      // the per-group offset_ms entries in sync with the participants.
      if(action.offset.mode === "explicit"){
        const prev = explicitValuesMapFromHolder(action);
        const sel = getSelectedIdsFromHolder(action);
        const ids = sel.all ? knownIds : sel.ids;
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

    // ---- Unified target picker (Broadcast / Groups; no Device) -----
    // Selecting Broadcast while in Explicit mode is invalid (no per-
    // group values to evaluate). Snap mode to Linear before the
    // editor re-renders so the operator sees a consistent panel.
    const targetWrap = document.createElement("div");
    targetWrap.className = "rl-groups-offset-target";
    const targetLbl = document.createElement("span");
    targetLbl.className = "muted rl-groups-offset-target-label";
    targetLbl.textContent = "Apply offset to:";
    targetWrap.appendChild(targetLbl);
    targetWrap.appendChild(buildUnifiedTargetPicker(action, {
      scope: "container",
      namePrefix: `og-target-${idx}`,
      actionKind: "offset_group",
      onChange: () => {
        if(targetIsBroadcast(action.target) && action.offset.mode === "explicit"){
          action.offset = { mode: "linear", base_ms: 0, step_ms: 100 };
        }
        repaintAll();
        renderSceneEditor();
      },
    }));
    wrap.appendChild(targetWrap);

    // ---- formula mode selector ----
    const modeRow = document.createElement("div");
    modeRow.className = "rl-groups-offset-mode";
    const modeLbl = document.createElement("span");
    modeLbl.className = "muted";
    modeLbl.textContent = "Offset mode:";
    modeRow.appendChild(modeLbl);
    OFFSET_FORMULA_MODES.forEach(m => {
      // Explicit needs a concrete groups list to evaluate against — it
      // is invalid when the container target is broadcast.
      if(m === "explicit" && targetIsBroadcast(action.target)) return;
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
          const sel = getSelectedIdsFromHolder(action);
          const ids = sel.all ? knownIds : sel.ids;
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
      const explicitSel = getSelectedIdsFromHolder(action);
      const ids = explicitSel.all ? knownIds : explicitSel.ids;
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
      const previewSel = getSelectedIdsFromHolder(action);
      const ids = previewSel.all ? knownIds : previewSel.ids;
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

    // Inline warning when target == broadcast AND prior offset_group
    // actions exist in the same scene — operators need to know that
    // broadcast formulas overwrite previously-configured groups.
    const priorOffsetGroups = (draft.actions || [])
      .slice(0, idx)
      .filter(a => a && a.kind === "offset_group");
    if(targetIsBroadcast(action.target) && priorOffsetGroups.length > 0){
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
        buildOffsetGroupChildRow(child, childIdx, action, draft, idx)
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

  function buildOffsetGroupChildRow(child, childIdx, parent, draft, parentIdx){
    // A child action row inside an offset_group container. Layout mirrors
    // a top-level row but with restricted kind dropdown + scope/group/device
    // target picker and a forced-on offset_mode flag (visually disabled).
    //
    // ``parentIdx`` is the parent action's index in ``draft.actions``. It's
    // threaded through so the radio-button group name can be derived
    // deterministically (``parentIdx-childIdx``) instead of from a random
    // id stored on the child action — the latter polluted the canonical
    // JSON used by the dirty-tracker, causing spurious "unsaved changes"
    // prompts on every render.
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
    bodyCol.appendChild(buildChildTargetPicker(child, parent, parentIdx, childIdx));
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

  function buildChildTargetPicker(child, parent, parentIdx, childIdx){
    // Thin wrapper around the unified picker. Children of an
    // offset_group container offer the same Broadcast / Groups /
    // Device choice as top-level actions; the multi-select and device
    // dropdown are filtered to the parent's participating groups.
    //
    // The DOM radio name is derived from ``parentIdx-childIdx``
    // (deterministic, stable across renders) so the dirty-tracker
    // doesn't flag every render as "unsaved changes".
    return buildUnifiedTargetPicker(child, {
      scope: "child",
      namePrefix: `child-target-${parentIdx}-${childIdx}`,
      actionKind: child.kind,
      parentTarget: ensureContainerTarget(parent),
      onChange: () => {
        scheduleCostEstimate();
        renderSceneEditor();
      },
    });
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
    // Batch A (2026-04-28): stop_on_error round-trips with the scene.
    // Default true if missing/null (defensive — checkbox seeds the
    // draft on render, but we don't want a stale draft to silently
    // disable the safer behaviour).
    const stopOnError = (draft.stop_on_error === undefined || draft.stop_on_error === null)
                        ? true
                        : !!draft.stop_on_error;
    const body = {
      label,
      actions: stripUiBeforeSave(draft.actions),
      stop_on_error: stopOnError,
    };
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
    // C11: a successful save makes the current draft pristine — the
    // beforeunload listener won't prompt until the next edit.
    _markPristine();
    renderSceneList();
    renderSceneEditor();
  }

  async function runScene(key){
    setScenesHint(`Running "${key}"…`);
    // C7: disable the Run / Save / Duplicate / Delete buttons in the
    // scene editor for the duration of the run. Without this the
    // operator can click Run repeatedly and queue up duplicate
    // requests, or Save/Delete mid-run and get a confused state.
    // ``setBusy`` is null-guarded for the Devices-page-only
    // selectors so it's safe to call from /scenes.
    if(typeof RL.setBusy === "function") RL.setBusy(true);
    // R7: arm live-status tracking. Each action row clears its border
    // colour and the SSE handler will paint blue (running) / green (ok) /
    // red (error/degraded) as transitions arrive.
    state.scenes.activeRunKey = key;
    state.scenes.actionStatus = [];
    state.scenes.lastRunResult = null;
    renderSceneEditor();

    // Run the *displayed* draft when it has unsaved edits, never the
    // last persisted version. The body short-circuits the runner's
    // storage lookup so the saved scene under ``key`` is left untouched
    // — only the explicit Save button persists changes. When the draft
    // is clean (or there is no draft for this key), we send no body and
    // the runner falls back to the persisted scene as before.
    const draft = state.scenes.draft;
    let body = {};
    if(draft && draft.key === key && isDraftDirty()){
      const stopOnError = (draft.stop_on_error === undefined || draft.stop_on_error === null)
                          ? true
                          : !!draft.stop_on_error;
      body = {
        label: (draft.label || "").trim() || "draft",
        actions: stripUiBeforeSave(draft.actions || []),
        stop_on_error: stopOnError,
      };
    }
    try{
      const r = await apiPost(`/racelink/api/scenes/${key}/run`, body);
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
    } finally {
      // C7: clear busy regardless of success/failure so a network
      // error doesn't leave the editor permanently disabled.
      if(typeof RL.setBusy === "function") RL.setBusy(false);
    }
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
            // C11: SSE refresh accepted — the on-disk version
            // matches the draft, so the new draft is pristine.
            _markPristine();
            renderSceneEditor();
          }
        }catch{
          // ignore
        }
      }else if(!fresh){
        state.scenes.selectedKey = null;
        state.scenes.draft = null;
        // C11: nothing to be dirty about now.
        _markPristine();
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
    // C11: pristine baseline so isDraftDirty() compares against
    // the just-loaded state, not against ``undefined``.
    _markPristine();
    renderSceneList();
    renderSceneEditor();
  }

  // C11: warn the operator about unsaved scene-editor changes when
  // they navigate away (refresh, close tab, click ← Devices). The
  // browser itself shows the prompt — we just signal "yes, there
  // is unsaved work" by setting ``returnValue`` on the event.
  // Modern browsers ignore custom messages and show their own
  // confirmation; the dialog is unconditional once returnValue is
  // set, so we gate by ``isDraftDirty`` to avoid prompting on
  // pristine navigations.
  window.addEventListener("beforeunload", (event) => {
    if(!isDraftDirty()) return;
    event.preventDefault();
    // returnValue text shown only on legacy browsers; modern
    // Chromium / Firefox use their canned "leave site?" dialog.
    event.returnValue = "You have unsaved changes to this scene.";
    return event.returnValue;
  });

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
