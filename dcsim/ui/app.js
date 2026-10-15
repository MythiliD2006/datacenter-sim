/* datacenter-sim control panel */
(() => {
  "use strict";
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const view = $("#view");
  const PHASES = ["idle", "ramp_up", "spike", "peak", "steady", "cooldown"];
  const PHASE_NAME = { idle: "Idle", ramp_up: "Ramp-up", spike: "Spike", peak: "Peak", steady: "Steady", cooldown: "Cooldown" };
  const S = {
    state: null, live: [], events: [], profiles: [], interfaces: null, charts: {}, route: "", selectedProfile: null,
    preview: null, edit: null, ifaceEdit: null, settings: null, compare: [], logTimer: null,
  };

  // ── utilities ──────────────────────────────────────────────────────
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const fmt = (v, d = 0) => (v == null || isNaN(v) ? "–" : Number(v).toLocaleString(undefined, { maximumFractionDigits: d, minimumFractionDigits: d }));
  const fmtTime = (s) => window.DCCharts.fmtTime(s);
  const fmtDate = (ts) => (ts ? new Date(ts * 1000).toLocaleString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "–");
  const clock = (ts) => new Date(ts * 1000).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" });

  async function api(path, opts = {}) {
    const o = { method: opts.method || "GET", headers: {} };
    if (opts.body !== undefined) { o.body = JSON.stringify(opts.body); o.headers["Content-Type"] = "application/json"; }
    const r = await fetch(path, o);
    const text = await r.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch { data = text; }
    if (!r.ok) throw new Error((data && data.detail) || `Request failed (${r.status})`);
    return data;
  }
  function toast(msg, kind = "") {
    const el = document.createElement("div");
    el.className = "toast " + kind;
    el.textContent = msg;
    $("#toasts").appendChild(el);
    setTimeout(() => el.remove(), kind === "error" ? 7000 : 3500);
  }
  async function act(fn, okMsg) {
    try { const r = await fn(); if (okMsg) toast(okMsg); return r; } catch (e) { toast(e.message, "error"); throw e; }
  }
  const kvText = (v) => (v && typeof v === "object" ? Object.entries(v).map(([k, x]) => `${k}=${x}`).join(", ") : v ?? "");
  const kvParse = (text, numeric) => {
    const out = {};
    for (const part of String(text).split(",")) {
      const [k, x] = part.split("=").map((y) => (y || "").trim());
      if (!k || x === undefined || x === "") continue;
      out[k] = numeric || /^\d+$/.test(x) ? Number(x) : x;
    }
    return out;
  };
  const IFACE_COLORS = ["--phase-ramp-solid", "--phase-peak-solid", "--phase-steady-solid", "--phase-cool-solid", "--phase-spike-solid", "--phase-idle-solid"];
  // stacked strip: users per interface in each stage (shows independent interface curves)
  function drawIfaceStrip(el, stages) {
    if (!el) return;
    if (!stages || !stages.length || !stages[0].targets) { el.innerHTML = ""; return; }
    const names = Object.keys(stages[0].targets);
    const W = Math.max(300, el.clientWidth || 800), H = 74, padL = 36, padB = 4;
    const total = stages[stages.length - 1].end;
    const maxU = Math.max(1, ...stages.map((s) => s.users));
    const x = (t) => padL + (t / total) * (W - padL - 4);
    let out = "";
    for (const st of stages) {
      let y0 = H - padB;
      names.forEach((n, i) => {
        const v = st.targets[n] || 0;
        if (!v) return;
        const h = (v / maxU) * (H - padB - 6);
        out += `<rect x="${x(st.start)}" y="${y0 - h}" width="${Math.max(0, x(st.end) - x(st.start) - 1)}" height="${h}" fill="var(${IFACE_COLORS[i % IFACE_COLORS.length]})"><title>${esc(st.label)}: ${esc(n)} ${v} users</title></rect>`;
        y0 -= h;
      });
    }
    el.setAttribute("viewBox", `0 0 ${W} ${H}`);
    el.innerHTML = out + `<text class="axis" x="${padL - 6}" y="12" text-anchor="end">${fmt(maxU)}</text>`;
    const leg = el.nextElementSibling;
    if (leg && leg.classList.contains("legend")) leg.innerHTML = names.map((n, i) => `<span><span class="phase-dot" style="background:var(${IFACE_COLORS[i % IFACE_COLORS.length]})"></span>${esc((S.interfaces && S.interfaces[n] && S.interfaces[n].label) || n)}</span>`).join("") + `<span class="faint">users per interface in each stage</span>`;
  }
  const planT = (s) => (s.run_time != null ? s.run_time : s.t);
  const running = () => !!(S.state && S.state.run && !S.state.run.finished);
  function destroyCharts() { Object.values(S.charts).forEach((c) => c.destroy()); S.charts = {}; }

  // ── theme ──────────────────────────────────────────────────────────
  function applyTheme(t) {
    if (t) document.documentElement.dataset.theme = t; else delete document.documentElement.dataset.theme;
  }
  try { applyTheme(localStorage.getItem("dcsim-theme")); } catch {}
  $("#themeToggle").addEventListener("click", () => {
    const dark = document.documentElement.dataset.theme
      ? document.documentElement.dataset.theme === "dark"
      : matchMedia("(prefers-color-scheme: dark)").matches;
    const next = dark ? "light" : "dark";
    applyTheme(next);
    try { localStorage.setItem("dcsim-theme", next); } catch {}
    route(true);
  });

  // ── live connection ────────────────────────────────────────────────
  function connect() {
    const ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws");
    ws.onmessage = (ev) => {
      const m = JSON.parse(ev.data);
      if (m.type === "sample") onSample(m.sample);
      else if (m.type === "state") { const wasRunning = running(); S.state = m.state; if (!wasRunning && running()) S.live = []; onState(); }
      else if (m.type === "event") { S.events.push(m.event); if (S.events.length > 200) S.events.shift(); if (S.route === "dashboard") renderEvents(); }
      else if (m.type === "run_finished") { toast("Test finished - results are in Results"); if (S.route === "runs") renderRuns(); }
    };
    ws.onclose = () => { setTimeout(connect, 1500); };
    ws.onopen = () => refreshState();
  }
  async function refreshState() {
    try {
      const s = await api("/api/state");
      S.state = s; S.live = s.live || []; S.events = s.events || [];
      $("#homePath").textContent = "Data: " + s.home;
      $("#version").textContent = "Version " + s.version;
      onState();
      if (S.route === "dashboard") { renderEvents(); updateCharts(); }
    } catch (e) { /* server restarting */ }
  }
  function onSample(s) {
    if (running() && s.idle) return;
    if (!running() && !s.idle) return;
    S.live.push(s);
    if (S.live.length > 900) S.live.shift();
    if (S.route === "dashboard") { updateKpis(); updateCharts(); updateTables(); updateSkyline(); updateRunBar(); }
  }
  function onState() {
    if (S.route === "dashboard") { updateTopbar(); updateRunBar(); updateSkyline(); }
  }

  // ── router ─────────────────────────────────────────────────────────
  window.addEventListener("hashchange", () => route());
  function route(force) {
    const h = (location.hash || "#dashboard").slice(1);
    const [name, ...rest] = h.split("/");
    if (S.logTimer) { clearInterval(S.logTimer); S.logTimer = null; }
    destroyCharts();
    S.route = name;
    $$(".nav a").forEach((a) => a.classList.toggle("active", a.dataset.nav === name || (a.dataset.nav === "profiles" && name === "profile") || (a.dataset.nav === "runs" && (name === "run" || name === "compare"))));
    const pages = { dashboard: renderDashboard, profiles: renderProfiles, profile: () => renderProfileEditor(rest.join("/")),
      interfaces: renderInterfaces, settings: renderSettings, runs: renderRuns, run: () => renderRun(rest[0]),
      compare: () => renderCompare(rest[0], rest[1]), logs: renderLogs };
    (pages[name] || renderDashboard)();
    if (!force) window.scrollTo(0, 0);
  }

  // ═══ LIVE TEST (dashboard) ══════════════════════════════════════════
  async function renderDashboard() {
    view.innerHTML = `
      <div class="topbar">
        <h1>Live test</h1>
        <div class="status-line" id="topStatus"></div>
      </div>
      <section class="card skyline-card" aria-label="Test plan">
        <div class="skyline-head">
          <div>
            <div class="skyline-title" id="skyTitle">–</div>
            <div class="skyline-meta" id="skyMeta"></div>
          </div>
          <div class="run-controls" id="runBar"></div>
        </div>
        <svg class="skyline" id="skyline" role="img" aria-label="Planned users over time, by phase"></svg>
        <div class="legend">${PHASES.map((p) => `<span><span class="phase-dot ${p}"></span>${PHASE_NAME[p]}</span>`).join("")}
          <span><svg width="18" height="8"><path d="M0 4h18" stroke="currentColor" stroke-width="2"/></svg> users actually running</span></div>
        <svg class="skyline strip" id="ifaceStrip" role="img" aria-label="Planned users per interface"></svg><div class="legend"></div>
      </section>
      <section class="kpis" id="kpis"></section>
      <section class="grid2">
        <div class="card"><h3>Users and throughput</h3><div class="chart-box"><canvas id="ch1"></canvas></div></div>
        <div class="card"><h3>Response time</h3><div class="chart-box"><canvas id="ch2"></canvas></div></div>
        <div class="card"><h3>Failed requests</h3><div class="chart-box"><canvas id="ch3"></canvas></div></div>
        <div class="card"><h3>This computer and the server under test</h3><div class="chart-box"><canvas id="ch4"></canvas></div></div>
      </section>
      <section class="split">
        <div class="card flush"><div class="card-head" style="padding:14px 16px 0"><h2>By interface</h2><span class="faint">last 5 seconds</span></div>
          <div class="table-wrap"><table id="ifaceTable"></table></div>
          <div class="card-head" style="padding:14px 16px 0"><h2>By endpoint</h2></div>
          <div class="table-wrap"><table id="epTable"></table></div></div>
        <div class="card"><div class="card-head"><h2>Activity</h2></div><div class="events" id="events"></div></div>
      </section>`;
    if (!S.profiles.length) S.profiles = await api("/api/profiles").catch(() => []);
    if (!S.selectedProfile) {
      try { S.selectedProfile = localStorage.getItem("dcsim-profile"); } catch {}
      if (!S.profiles.find((p) => p.id === S.selectedProfile)) S.selectedProfile = (S.profiles.find((p) => p.id === "normal_day") || S.profiles[0] || {}).id;
    }
    await loadPreview();
    buildCharts();
    updateTopbar(); updateRunBar(); updateSkyline(); updateKpis(); updateCharts(); updateTables(); renderEvents();
  }

  async function loadPreview() {
    S.preview = null;
    if (!S.selectedProfile) return;
    try {
      const p = await api("/api/profiles/" + S.selectedProfile);
      S.preview = await api("/api/preview", { method: "POST", body: p });
      S.preview.name = p.name; S.preview.description = p.description; S.preview.mix = p.mix;
    } catch (e) { S.preview = { error: e.message }; }
  }

  function updateTopbar() {
    const el = $("#topStatus");
    if (!el || !S.state) return;
    const t = S.state.target, c = S.state.capacity, sys = S.state.system;
    const label = { running: "running", starting: "starting", stopped: "stopped", crashed: "not responding", external: "external" }[t.state] || t.state;
    let btns = "";
    if (t.mode === "builtin") {
      btns = t.state === "running" || t.state === "crashed"
        ? `<button data-act="target-restart" ${running() ? "disabled" : ""}>Restart server</button><button data-act="target-stop" class="ghost" ${running() ? "disabled" : ""}>Stop server</button>`
        : `<button data-act="target-start" ${t.state === "starting" ? "disabled" : ""}>Start server</button>`;
    }
    el.innerHTML = `
      <span><span class="dot ${esc(t.state)}"></span>Server under test <strong>${esc(label)}</strong>
        <span class="faint">${t.url ? esc(t.url.replace(/^https?:\/\//, "")) : ""}${t.grpc ? " · gRPC " + esc(t.grpc.split(":").pop()) : ""}${t.mode === "builtin" && t.accounts ? " · " + fmt(t.accounts) + " accounts" : ""}</span></span>
      <span title="${esc(c.source)}">This computer <strong>${sys.logical_cores} cores, ${fmt(sys.memory_total_mb / 1024, 1)} GB</strong>
        ${(S.state.agents || []).length ? ` + <strong>${S.state.agents.length} worker computer${S.state.agents.length > 1 ? "s" : ""}</strong> <span class="faint">(${esc(S.state.agents.map((a) => a.hostname + " " + a.capacity).join(", "))})</span>` : ""}
        → capacity <strong>${fmt(c.users)} users</strong> <span class="faint">(${esc(c.mode === "measured" ? "measured" : c.mode === "fixed" ? "fixed" : "estimated")})</span></span>
      <span class="row">${btns}</span>`;
    el.querySelectorAll("[data-act]").forEach((b) => b.addEventListener("click", () => {
      const a = b.dataset.act.split("-")[1];
      b.disabled = true;
      act(() => api("/api/target/" + a, { method: "POST" }), { start: "Server started", stop: "Server stopped", restart: "Server restarted" }[a])
        .then((s) => { S.state = s; onState(); }).catch(() => {}).finally(() => { b.disabled = false; });
    }));
  }

  function updateRunBar() {
    const el = $("#runBar");
    if (!el || !S.state) return;
    const r = S.state.run;
    const title = $("#skyTitle"), meta = $("#skyMeta");
    if (running()) {
      const last = S.live[S.live.length - 1] || {};
      const st = r.stages[last.stage] || r.stages[0];
      title.textContent = r.profile_name;
      meta.innerHTML = `${esc(r.kind === "calibration" ? "Measuring capacity" : "Running")} · ${fmtTime(planT(last) || r.elapsed)} of ${fmtTime(r.total)} ·
        now <strong>${esc(st ? st.label : "")}</strong> ${st ? `(${esc(PHASE_NAME[st.phase] || st.phase)}, target ${fmt(last.target_users ?? st.users)} users)` : ""}
        · ${r.workers} load worker${r.workers > 1 ? "s" : ""}${r.remote_workers ? ` on ${r.remote_workers + 1} computers` : ""} · seed ${esc(r.seed)}${last.override != null ? ` · <strong>holding at ${fmt(last.override)} users</strong>` : ""}`;
      if (el.dataset.mode !== "running") {
        el.dataset.mode = "running";
        el.innerHTML = `
          <button data-act="next">Skip to next phase</button>
          <span class="hold"><label for="holdUsers" class="muted">Hold at</label><input id="holdUsers" class="narrow" type="number" min="0" placeholder="users">
          <button data-act="hold">Hold</button><button data-act="release" class="ghost">Follow plan</button></span>
          <button data-act="stop" class="danger">Stop test</button>`;
        el.querySelector('[data-act="next"]').onclick = () => act(() => api("/api/runs/command", { method: "POST", body: { cmd: "next_stage" } }), "Moving to the next phase");
        el.querySelector('[data-act="hold"]').onclick = () => {
          const v = parseInt($("#holdUsers").value, 10);
          if (isNaN(v) || v < 0) return toast("Enter how many users to hold at", "error");
          act(() => api("/api/runs/command", { method: "POST", body: { cmd: "set_users", users: v } }), `Holding at ${v} users`);
        };
        el.querySelector('[data-act="release"]').onclick = () => act(() => api("/api/runs/command", { method: "POST", body: { cmd: "set_users", users: null } }), "Following the profile again");
        el.querySelector('[data-act="stop"]').onclick = (e) => { e.target.disabled = true; act(() => api("/api/runs/stop", { method: "POST" }), "Stopping - users are logging out"); };
      }
      return;
    }
    const p = S.preview;
    title.textContent = p && p.name ? p.name : "Choose a workload profile";
    meta.innerHTML = p && !p.error
      ? `${fmtTime(p.duration)} · up to <strong>${fmt(p.peak)} users</strong> on this computer · ${p.stages.length} stages · ${p.workers} load worker${p.workers > 1 ? "s" : ""}
         <div class="faint" style="margin-top:2px">${esc(p.description || "")}</div>`
      : p && p.error ? `<span style="color:var(--bad)">${esc(p.error)}</span>` : "";
    if (el.dataset.mode !== "idle" || el.dataset.count !== String(S.profiles.length)) {
      el.dataset.mode = "idle"; el.dataset.count = String(S.profiles.length);
      el.innerHTML = `
        <label class="muted" for="profileSel">Profile</label>
        <select id="profileSel">${S.profiles.map((x) => `<option value="${esc(x.id)}" ${x.id === S.selectedProfile ? "selected" : ""}>${esc(x.name)}</option>`).join("")}</select>
        <a class="btn ghost" id="editSel" href="#profile/${esc(S.selectedProfile || "")}">Edit</a>
        <input id="seedIn" class="narrow" type="number" min="0" placeholder="seed" title="Optional: a seed from an earlier run repeats that run exactly">
        <button class="primary" data-act="start">Start test</button>
        <button data-act="calibrate" title="Steps users up until the SLA breaks and saves the result as this computer's capacity">Measure capacity</button>`;
      $("#profileSel").onchange = async (e) => {
        S.selectedProfile = e.target.value;
        try { localStorage.setItem("dcsim-profile", S.selectedProfile); } catch {}
        $("#editSel").href = "#profile/" + S.selectedProfile;
        await loadPreview(); updateRunBar(); updateSkyline();
      };
      el.querySelector('[data-act="start"]').onclick = (e) => {
        e.target.disabled = true;
        const sd = $("#seedIn").value.trim();
        act(() => api("/api/runs", { method: "POST", body: { profile_id: S.selectedProfile, seed: sd === "" ? null : Number(sd) } }))
          .then((s) => { S.state = s; S.live = []; buildCharts(); onState(); updateCharts(); })
          .catch(() => {}).finally(() => { e.target.disabled = false; });
      };
      el.querySelector('[data-act="calibrate"]').onclick = (e) => {
        if (!confirm("Measuring capacity runs a stepped test for a few minutes and saves the highest user count that met the SLA. Start it?")) return;
        e.target.disabled = true;
        act(() => api("/api/calibrate", { method: "POST" }))
          .then((s) => { S.state = s; S.live = []; buildCharts(); onState(); })
          .catch(() => {}).finally(() => { e.target.disabled = false; });
      };
    }
  }

  // the skyline: planned users per stage, ramps drawn at their real spawn speed,
  // with the users actually running traced on top
  function updateSkyline() {
    const svg = $("#skyline");
    if (!svg) return;
    const r = running() ? S.state.run : null;
    const stages = r ? r.stages : S.preview && S.preview.stages;
    if (!stages || !stages.length) { svg.innerHTML = ""; return; }
    const W = Math.max(300, svg.clientWidth || 800), H = 132, padL = 36, padB = 18, padT = 14;
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    const total = stages[stages.length - 1].end;
    const liveMax = r ? Math.max(0, ...S.live.map((s) => s.users || 0)) : 0;
    const maxU = Math.max(1, liveMax, ...stages.map((s) => s.users));
    const x = (t) => padL + (t / total) * (W - padL - 4);
    const y = (u) => H - padB - (u / maxU) * (H - padB - padT);
    const lastLive = S.live.filter((s) => !s.final && planT(s) != null).pop();
    const nowT = r ? (lastLive ? planT(lastLive) : 0) : null;
    let prev = 0, out = "";
    for (const s of stages) {
      const rampT = Math.min(s.duration, Math.abs(s.users - prev) / Math.max(s.spawn_rate, 0.001));
      const x0 = x(s.start), x1 = x(s.end), xr = x(s.start + rampT);
      const cls = `bar ${s.phase} ${r ? (nowT >= s.end ? "done" : nowT >= s.start ? "current" : "") : ""}`;
      out += `<path class="${cls}" d="M${x0},${y(0)} L${x0},${y(prev)} L${xr},${y(s.users)} L${x1},${y(s.users)} L${x1},${y(0)} Z"><title>${esc(s.label)} · ${esc(PHASE_NAME[s.phase])} · ${s.users} users · ${s.duration}s${s.only ? " · only " + s.only.join(", ") : ""}</title></path>`;
      prev = s.users;
    }
    out += `<line class="baseline" x1="${padL}" x2="${W - 4}" y1="${y(0)}" y2="${y(0)}"/>`;
    out += `<text class="axis" x="${padL - 6}" y="${y(maxU) + 4}" text-anchor="end">${fmt(maxU)}</text><text class="axis" x="${padL - 6}" y="${y(0)}" text-anchor="end">0</text>`;
    const ticks = 6;
    for (let i = 0; i <= ticks; i++) {
      const t = (total * i) / ticks;
      out += `<text class="axis" x="${x(t)}" y="${H - 4}" text-anchor="${i === 0 ? "start" : i === ticks ? "end" : "middle"}">${fmtTime(t)}</text>`;
    }
    if (r) {
      const pts = S.live.filter((s) => planT(s) != null && !s.final).map((s) => `${x(Math.min(planT(s), total)).toFixed(1)},${y(s.users || 0).toFixed(1)}`);
      if (pts.length) out += `<polyline class="live-line" points="${pts.join(" ")}"/>`;
      if (nowT != null) {
        const xc = x(Math.min(nowT, total));
        out += `<line class="cursor" x1="${xc}" x2="${xc}" y1="${padT - 6}" y2="${y(0)}"/><text class="cursor-label" x="${Math.min(xc + 4, W - 30)}" y="${padT - 2}">now</text>`;
      }
    }
    svg.innerHTML = out;
    drawIfaceStrip($("#ifaceStrip"), stages);
  }

  function windowStats(n = 5) {
    const w = S.live.slice(-n).filter((s) => !s.idle);
    const agg = (key) => {
      const out = {};
      for (const s of w) for (const [k, v] of Object.entries(s[key] || {})) {
        const o = (out[k] = out[k] || { count: 0, err: 0, p95s: [], rps: 0 });
        o.count += v.count; o.err += v.err; if (v.p95 != null) o.p95s.push(v.p95); o.rps += v.rps;
      }
      for (const o of Object.values(out)) { o.rps = o.rps / Math.max(1, w.length); o.p95 = o.p95s.length ? o.p95s.reduce((a, b) => a + b) / o.p95s.length : null; }
      return out;
    };
    return { iface: agg("iface"), endpoint: agg("endpoint"), last: w[w.length - 1] };
  }

  function updateKpis() {
    const el = $("#kpis");
    if (!el) return;
    const last = S.live[S.live.length - 1] || {};
    const w = S.live.slice(-5).filter((s) => !s.idle);
    const cnt = w.reduce((a, s) => a + (s.count || 0), 0), fails = w.reduce((a, s) => a + (s.fails || 0), 0);
    const errPct = cnt ? (100 * fails) / cnt : 0;
    const rps = w.length ? w.reduce((a, s) => a + (s.rps || 0), 0) / w.length : 0;
    const sys = last.sys || {}, tg = last.target || {}, ld = last.load || {};
    const sla = (S.settings && S.settings.config && S.settings.config.sla) || null;
    const k = (label, value, sub, bad) => `<div class="kpi ${bad ? "bad" : ""}"><div class="kpi-label">${label}</div><div class="kpi-value">${value}</div><div class="kpi-sub">${sub}</div></div>`;
    el.innerHTML =
      k("Users", fmt(last.users || 0), running() ? `target ${fmt(last.target_users)}` : "no test running") +
      k("Requests per second", fmt(rps, 1), running() ? `${fmt(cnt)} in the last ${w.length}s` : "–") +
      k("Response time p95", `${fmt(last.p95)}<small> ms</small>`, `p50 ${fmt(last.p50)} · p99 ${fmt(last.p99)} ms`, sla && last.p95 > sla.p95_ms) +
      k("Failed requests", `${fmt(errPct, 1)}<small> %</small>`, `${fmt(fails)} failed recently`, sla && errPct > sla.error_rate_pct) +
      k("CPU", `${fmt(sys.cpu_pct)}<small> %</small>`, `server ${fmt(tg.cpu_pct)}% · load gen ${fmt(ld.cpu_pct)}%`, sla && sys.cpu_pct > sla.cpu_pct) +
      k("Memory", `${fmt(sys.mem_pct)}<small> %</small>`, `server ${fmt(tg.rss_mb)} MB · ${fmt(tg.sessions)} sessions`, sla && sys.mem_pct > sla.memory_pct);
  }

  function buildCharts() {
    destroyCharts();
    if (!$("#ch1")) return;
    const stages = running() ? S.state.run.stages : [];
    const xmax = stages.length ? stages[stages.length - 1].end : undefined;
    const L = window.DCCharts.lineChart;
    const base = { stages, xmin: running() ? 0 : undefined, xmax, bandLabels: false };
    S.charts.c1 = L($("#ch1"), { ...base, yTitle: "users", y2: "req/s", series: [{ label: "Users", color: "--c-users" }, { label: "Requests/s", color: "--c-rps", axis: "y2" }] });
    S.charts.c2 = L($("#ch2"), { ...base, yTitle: "ms", series: [{ label: "p50", color: "--c-p50" }, { label: "p95", color: "--c-p95" }, { label: "p99", color: "--c-p99", dash: [4, 3] }] });
    S.charts.c3 = L($("#ch3"), { ...base, yTitle: "%", series: [{ label: "Failed %", color: "--c-err" }] });
    S.charts.c4 = L($("#ch4"), { ...base, yTitle: "%", suggestedMax: 100, series: [{ label: "Computer CPU %", color: "--c-cpu" }, { label: "Computer memory %", color: "--c-mem" }, { label: "Server CPU %", color: "--c-scpu", dash: [4, 3] }] });
    S.chartsMode = running() ? "run" : "idle";
  }

  function updateCharts() {
    if (!S.charts.c1) return;
    if ((running() ? "run" : "idle") !== S.chartsMode) buildCharts();
    const rows = S.live.filter((s) => (running() ? planT(s) != null && !s.final : true));
    const t0 = rows.length ? rows[0].ts : 0;
    const xv = (s) => (running() ? planT(s) : s.ts - t0);
    const pts = (f) => rows.map((s) => ({ x: xv(s), y: f(s) }));
    const set = (c, i, data) => { c.data.datasets[i].data = data; };
    set(S.charts.c1, 0, pts((s) => s.users)); set(S.charts.c1, 1, pts((s) => s.rps));
    set(S.charts.c2, 0, pts((s) => s.p50)); set(S.charts.c2, 1, pts((s) => s.p95)); set(S.charts.c2, 2, pts((s) => s.p99));
    set(S.charts.c3, 0, pts((s) => (s.count ? (100 * (s.fails || 0)) / s.count : 0)));
    set(S.charts.c4, 0, pts((s) => (s.sys || {}).cpu_pct)); set(S.charts.c4, 1, pts((s) => (s.sys || {}).mem_pct)); set(S.charts.c4, 2, pts((s) => (s.target || {}).cpu_pct));
    const cursor = running() && rows.length ? xv(rows[rows.length - 1]) : null;
    for (const c of Object.values(S.charts)) { c.options.plugins.phaseBands.cursor = cursor; c.update("none"); }
  }

  function updateTables() {
    const ti = $("#ifaceTable"), te = $("#epTable");
    if (!ti) return;
    const w = windowStats();
    const byIface = (w.last && w.last.users_by_iface) || {};
    const tgt = (w.last && w.last.targets_by_iface) || {};
    const byWorker = (w.last && w.last.users_by_worker) || {};
    const names = Array.from(new Set([...Object.keys(byIface), ...Object.keys(w.iface)]));
    const labels = S.interfaces || {};
    ti.innerHTML = `<thead><tr><th>Interface</th><th class="num">Users</th><th class="num">Target</th><th class="num">Req/s</th><th class="num">p95</th><th class="num">Failed</th></tr></thead><tbody>${
      names.length ? names.map((n) => { const v = w.iface[n] || {}; return `<tr><td>${esc((labels[n] && labels[n].label) || n)}</td><td class="num">${fmt(byIface[n])}</td><td class="num faint">${fmt(tgt[n])}</td><td class="num">${fmt(v.rps, 1)}</td><td class="num">${fmt(v.p95)} ms</td><td class="num">${v.count ? fmt((100 * v.err) / v.count, 1) + "%" : "–"}</td></tr>`; }).join("")
      : `<tr><td colspan="6" class="muted">Start a test to see each interface's traffic.</td></tr>`}</tbody>${
      Object.keys(byWorker).length ? `<tfoot><tr><td colspan="6" class="faint">Users per load worker: ${esc(Object.entries(byWorker).map(([k, v]) => k + " " + v).join(" · "))}</td></tr></tfoot>` : ""}`;
    const eps = Object.entries(w.endpoint).sort((a, b) => b[1].rps - a[1].rps);
    te.innerHTML = `<thead><tr><th>Endpoint</th><th class="num">Req/s</th><th class="num">p95</th><th class="num">Failed</th></tr></thead><tbody>${
      eps.length ? eps.map(([n, v]) => `<tr><td class="mono">${esc(n)}</td><td class="num">${fmt(v.rps, 1)}</td><td class="num">${fmt(v.p95)} ms</td><td class="num">${v.count ? fmt((100 * v.err) / v.count, 1) + "%" : "–"}</td></tr>`).join("")
      : `<tr><td colspan="4" class="muted">No requests yet.</td></tr>`}</tbody>`;
  }

  function renderEvents() {
    const el = $("#events");
    if (!el) return;
    const ev = S.events.slice(-80).reverse();
    el.innerHTML = ev.length ? ev.map((e) => `<div><time>${clock(e.ts)}</time><span class="${esc(e.kind)}">${esc(e.msg)}</span></div>`).join("")
      : `<p class="muted">Server starts, phase changes, commands and crashes show up here.</p>`;
  }

  // ═══ PROFILES ═══════════════════════════════════════════════════════
  async function renderProfiles() {
    view.innerHTML = `<div class="topbar"><h1>Workload profiles</h1><div class="row">
      <button data-act="restore">Restore built-in profiles</button><a class="btn primary" href="#profile/new">New profile</a></div></div>
      <p class="muted" style="max-width:70ch">A profile is the test plan: which interfaces take part and how many users there are in each phase.
      Users written as a percentage scale to the computer the test runs on.</p>
      <div class="card flush"><div class="table-wrap"><table id="profTable"></table></div></div>`;
    $('[data-act="restore"]').onclick = () => act(() => api("/api/profiles/restore-defaults", { method: "POST" }))
      .then((r) => { toast(r.restored.length ? `Restored ${r.restored.join(", ")}` : "All built-in profiles are already there"); renderProfiles(); }).catch(() => {});
    S.profiles = await api("/api/profiles");
    $("#profTable").innerHTML = `<thead><tr><th>Name</th><th>Kind</th><th class="num">Stages</th><th class="num">Length</th><th>Description</th></tr></thead><tbody>${
      S.profiles.map((p) => `<tr class="click" data-id="${esc(p.id)}"><td><strong>${esc(p.name)}</strong><div class="faint">${esc(p.id)}</div></td><td>${esc((p.archetype || "").replace("_", " "))}</td>
        <td class="num">${p.stages}</td><td class="num">${fmtTime(p.duration)}</td><td class="muted">${esc(p.description)}</td></tr>`).join("") ||
      `<tr><td colspan="5" class="empty">No profiles yet. Create one, or restore the built-in set.</td></tr>`}</tbody>`;
    $$("#profTable tr.click").forEach((tr) => (tr.onclick = () => (location.hash = "#profile/" + tr.dataset.id)));
  }

  async function renderProfileEditor(id) {
    const isNew = id === "new" || id.startsWith("new:");
    const [ifaces, prof] = await Promise.all([api("/api/interfaces"), isNew
      ? (id.startsWith("new:") ? api("/api/profiles/" + id.slice(4)) : Promise.resolve({ name: "New profile", description: "", archetype: "custom", mix: { web: 50, mobile: 35, api: 15 }, stages: [{ phase: "ramp_up", duration: 60, users: "50%", spawn_rate: "auto" }, { phase: "peak", duration: 120, users: "70%", spawn_rate: "auto" }, { phase: "cooldown", duration: 30, users: "5%", spawn_rate: "auto" }] }))
      : api("/api/profiles/" + id)]).catch((e) => { view.innerHTML = `<div class="empty">${esc(e.message)}</div>`; return []; });
    if (!ifaces) return;
    S.interfaces = ifaces;
    if (isNew && id.startsWith("new:")) prof.name = prof.name + " (copy)";
    Object.keys(prof.mix || {}).forEach((k) => { if (!ifaces[k]) delete prof.mix[k]; });
    S.edit = { id: isNew ? "" : id, isNew, data: JSON.parse(JSON.stringify(prof)) };
    view.innerHTML = `
      <div class="topbar"><div><a href="#profiles" class="faint">Profiles</a><h1 id="edTitle">${esc(prof.name)}</h1></div>
        <div class="row">${isNew ? "" : `<a class="btn" href="#profile/new:${esc(id)}">Duplicate</a><button class="danger" data-act="delete">Delete</button>`}
        <button data-act="save">Save</button><button class="primary" data-act="saverun">Save and start test</button></div></div>
      <section class="card skyline-card"><div class="skyline-head"><div><div class="skyline-title">Plan on this computer</div><div class="skyline-meta" id="pvMeta"></div></div></div>
        <svg class="skyline" id="skyline"></svg>
        <div class="legend">${PHASES.map((p) => `<span><span class="phase-dot ${p}"></span>${PHASE_NAME[p]}</span>`).join("")}</div>
        <svg class="skyline strip" id="ifaceStrip" role="img" aria-label="Planned users per interface"></svg><div class="legend"></div></section>
      <section class="card"><div class="form-grid">
        ${isNew ? `<label class="field">File name<input id="pfId" placeholder="e.g. black_friday" pattern="[a-z0-9_-]+"><span class="help">lowercase letters, digits, _ and -</span></label>` : ""}
        <label class="field">Name<input id="pfName" value="${esc(prof.name)}"></label>
        <label class="field">Kind<input id="pfArch" value="${esc(prof.archetype || "")}" placeholder="e.g. ecommerce"></label>
        <label class="field">Seed<input id="pfSeed" type="number" min="0" value="${esc(prof.seed ?? "")}" placeholder="new each run"><span class="help">fix it to repeat the same run every time</span></label>
        <label class="field" style="grid-column:1/-1">Description<input id="pfDesc" value="${esc(prof.description || "")}"></label>
      </div></section>
      <section class="card"><div class="card-head"><h2>Interface mix</h2><span class="faint">relative weights - who the users are</span></div>
        <div class="mix-bar" id="mixBar"></div><div class="table-wrap"><table id="mixTable"></table></div>
        <div class="row" style="margin-top:8px"><select id="mixAdd"></select><button data-act="mixadd">Add interface</button><a class="faint" href="#interfaces">Edit interfaces</a></div></section>
      <section class="card"><div class="card-head"><h2>Stages</h2><span class="faint">users: a number, a % of capacity, or per interface (web=30%, api=20) · spawn rate: auto or users/s · weights: this stage's split (web=70, api=30)</span></div>
        <div class="table-wrap"><table id="stageTable"></table></div>
        <div class="row" style="margin-top:8px"><button data-act="stageadd">Add stage</button></div></section>`;
    const bindTop = () => {
      $("#pfName").oninput = (e) => { S.edit.data.name = e.target.value; $("#edTitle").textContent = e.target.value; };
      $("#pfDesc").oninput = (e) => (S.edit.data.description = e.target.value);
      $("#pfArch").oninput = (e) => (S.edit.data.archetype = e.target.value);
      $("#pfSeed").oninput = (e) => { const v = e.target.value.trim(); if (v === "") delete S.edit.data.seed; else S.edit.data.seed = Number(v); };
    };
    bindTop(); drawMix(); drawStages(); schedulePreview();
    $('[data-act="mixadd"]').onclick = () => { const k = $("#mixAdd").value; if (k) { S.edit.data.mix[k] = 10; drawMix(); schedulePreview(); } };
    $('[data-act="stageadd"]').onclick = () => { const st = S.edit.data.stages; const last = st[st.length - 1] || {}; st.push({ phase: "steady", duration: 60, users: last.users ?? "50%", spawn_rate: "auto" }); drawStages(); schedulePreview(); };
    $('[data-act="save"]').onclick = () => saveProfile(false);
    $('[data-act="saverun"]').onclick = () => saveProfile(true);
    const del = $('[data-act="delete"]');
    if (del) del.onclick = () => { if (confirm(`Delete the profile "${S.edit.data.name}"?`)) act(() => api("/api/profiles/" + id, { method: "DELETE" }), "Profile deleted").then(() => (location.hash = "#profiles")).catch(() => {}); };
  }

  function drawMix() {
    const mix = S.edit.data.mix, ifaces = S.interfaces;
    const total = Object.values(mix).reduce((a, b) => a + Number(b || 0), 0) || 1;
    const colors = ["--phase-ramp-solid", "--phase-peak-solid", "--phase-steady-solid", "--phase-cool-solid", "--phase-spike-solid", "--phase-idle-solid"];
    $("#mixBar").innerHTML = Object.entries(mix).map(([k, v], i) => `<span title="${esc(k)}" style="width:${(100 * v) / total}%;background:var(${colors[i % colors.length]})"></span>`).join("");
    $("#mixTable").innerHTML = `<thead><tr><th>Interface</th><th style="width:110px">Weight</th><th class="num" style="width:80px">Share</th><th></th><th style="width:40px"></th></tr></thead><tbody>${
      Object.entries(mix).map(([k, v], i) => `<tr><td><span class="phase-dot" style="background:var(${colors[i % colors.length]})"></span><strong>${esc((ifaces[k] || {}).label || k)}</strong> <span class="faint">${esc(k)}</span></td>
        <td><input type="number" min="0" step="1" data-mix="${esc(k)}" value="${esc(v)}"></td><td class="num">${fmt((100 * v) / total)}%</td>
        <td class="muted">${esc((ifaces[k] || {}).description || "")}</td><td><button class="ghost" data-mixdel="${esc(k)}" aria-label="Remove ${esc(k)}">✕</button></td></tr>`).join("")}</tbody>`;
    $("#mixAdd").innerHTML = Object.keys(ifaces).filter((k) => !(k in mix)).map((k) => `<option value="${esc(k)}">${esc(ifaces[k].label || k)}</option>`).join("") || `<option value="">All interfaces added</option>`;
    $$("[data-mix]").forEach((inp) => (inp.onchange = () => { mix[inp.dataset.mix] = Math.max(0, Number(inp.value) || 0); drawMix(); schedulePreview(); }));
    $$("[data-mixdel]").forEach((b) => (b.onclick = () => {
      delete mix[b.dataset.mixdel];
      S.edit.data.stages.forEach((s) => { if (s.only) { s.only = s.only.filter((x) => x !== b.dataset.mixdel); if (!s.only.length) delete s.only; } });
      drawMix(); drawStages(); schedulePreview();
    }));
  }

  function drawStages() {
    const st = S.edit.data.stages;
    const mixKeys = Object.keys(S.edit.data.mix);
    $("#stageTable").innerHTML = `<thead><tr><th style="width:28px"></th><th style="width:120px">Phase</th><th>Label</th><th style="width:100px">Seconds</th><th style="width:210px">Users</th><th style="width:90px">Spawn rate</th><th>Interface weights</th><th>Only these interfaces</th><th style="width:96px"></th></tr></thead><tbody>${
      st.map((s, i) => `<tr data-i="${i}"><td class="faint">${i + 1}</td>
        <td><select data-f="phase">${PHASES.map((p) => `<option value="${p}" ${p === s.phase ? "selected" : ""}>${PHASE_NAME[p]}</option>`).join("")}</select></td>
        <td><input data-f="label" value="${esc(s.label || "")}" placeholder="${esc(PHASE_NAME[s.phase] || "")}"></td>
        <td><input data-f="duration" type="number" min="1" value="${esc(s.duration)}"></td>
        <td><input data-f="users" value="${esc(kvText(s.users))}"></td>
        <td><input data-f="spawn_rate" value="${esc(s.spawn_rate ?? "auto")}"></td>
        <td><input data-f="mix" value="${esc(kvText(s.mix))}" placeholder="profile mix"></td>
        <td><input data-f="only" value="${esc((s.only || []).join(", "))}" placeholder="all" list="mixList"></td>
        <td class="num"><button class="ghost" data-mv="-1" aria-label="Move up" ${i === 0 ? "disabled" : ""}>↑</button><button class="ghost" data-mv="1" aria-label="Move down" ${i === st.length - 1 ? "disabled" : ""}>↓</button><button class="ghost" data-del aria-label="Remove stage" ${st.length === 1 ? "disabled" : ""}>✕</button></td></tr>`).join("")}</tbody>
      <datalist id="mixList">${mixKeys.map((k) => `<option value="${esc(k)}">`).join("")}</datalist>`;
    $$("#stageTable tr[data-i]").forEach((tr) => {
      const s = st[Number(tr.dataset.i)];
      tr.querySelectorAll("[data-f]").forEach((inp) => (inp.oninput = () => {
        const f = inp.dataset.f, v = inp.value.trim();
        if (f === "duration") s.duration = parseInt(v, 10) || 0;
        else if (f === "users") s.users = v.includes("=") ? kvParse(v, false) : /^\d+$/.test(v) ? Number(v) : v;
        else if (f === "mix") { const m = kvParse(v, true); if (Object.keys(m).length) s.mix = m; else delete s.mix; }
        else if (f === "spawn_rate") s.spawn_rate = v === "" || v === "auto" ? "auto" : Number(v);
        else if (f === "only") { const l = v.split(",").map((x) => x.trim()).filter(Boolean); if (l.length) s.only = l; else delete s.only; }
        else if (f === "label") { if (v) s.label = v; else delete s.label; }
        else s[f] = v;
        schedulePreview();
      }));
      tr.querySelectorAll("[data-mv]").forEach((b) => (b.onclick = () => { const i = Number(tr.dataset.i), j = i + Number(b.dataset.mv); [st[i], st[j]] = [st[j], st[i]]; drawStages(); schedulePreview(); }));
      tr.querySelector("[data-del]").onclick = () => { st.splice(Number(tr.dataset.i), 1); drawStages(); schedulePreview(); };
    });
  }

  let pvTimer = null;
  function schedulePreview() {
    clearTimeout(pvTimer);
    pvTimer = setTimeout(async () => {
      try {
        const p = await api("/api/preview", { method: "POST", body: S.edit.data });
        S.preview = p;
        $("#pvMeta").innerHTML = `${fmtTime(p.duration)} long · up to <strong>${fmt(p.peak)} users</strong> on this computer (capacity ${fmt(p.capacity.users)}) · ${p.workers} load worker${p.workers > 1 ? "s" : ""}`;
        drawPreviewSky(p.stages);
      } catch (e) {
        $("#pvMeta").innerHTML = `<span style="color:var(--bad)">${esc(e.message)}</span>`;
      }
    }, 250);
  }
  function drawPreviewSky(stages) {
    const keep = S.state; const fake = { ...(S.state || {}), run: null };
    S.state = fake; const prevPreview = S.preview; S.preview = { stages };
    updateSkyline(); S.state = keep; S.preview = prevPreview;
  }

  async function saveProfile(andRun) {
    const e = S.edit;
    let id = e.id;
    if (e.isNew) {
      id = ($("#pfId").value || "").trim().toLowerCase();
      if (!/^[a-z0-9][a-z0-9_-]{0,63}$/.test(id)) return toast("Give the profile a file name using lowercase letters, digits, _ or -", "error");
      if (S.profiles.find((p) => p.id === id) && !confirm(`A profile called ${id} exists. Replace it?`)) return;
    }
    try {
      await act(() => api("/api/profiles/" + id, { method: "PUT", body: e.data }), "Profile saved");
      S.profiles = await api("/api/profiles");
      S.selectedProfile = id;
      try { localStorage.setItem("dcsim-profile", id); } catch {}
      if (andRun) {
        const s = await act(() => api("/api/runs", { method: "POST", body: { profile_id: id } }));
        S.state = s; S.live = [];
        location.hash = "#dashboard";
      } else if (e.isNew) location.hash = "#profile/" + id;
    } catch {}
  }

  // ═══ INTERFACES ═════════════════════════════════════════════════════
  async function renderInterfaces() {
    const data = await api("/api/interfaces");
    S.ifaceEdit = JSON.parse(JSON.stringify(data));
    view.innerHTML = `<div class="topbar"><h1>Interfaces</h1><div class="row"><button data-act="add">Add interface</button><button class="primary" data-act="save">Save interfaces</button></div></div>
      <p class="muted" style="max-width:72ch">Each interface describes one kind of client: how long it thinks between actions, how long its sessions last,
      and which requests it makes. Profiles mix interfaces by weight. Paths can point at the built-in server or at your own server.</p>
      <div id="ifaceList"></div>`;
    drawIfaces();
    $('[data-act="add"]').onclick = () => {
      const name = (prompt("Short id for the new interface (lowercase, e.g. partner_api)") || "").trim().toLowerCase();
      if (!name) return;
      if (!/^[a-z0-9][a-z0-9_-]{0,63}$/.test(name)) return toast("Use lowercase letters, digits, _ and -", "error");
      if (S.ifaceEdit[name]) return toast("That interface already exists", "error");
      S.ifaceEdit[name] = { label: name, description: "", login: true, wait_sec: [1, 3], session_lifetime_sec: null, heavy_user_fraction: 0, heavy_wait_multiplier: 1, tasks: [{ name: "health", method: "GET", path: "/health", weight: 1, auth: false }] };
      drawIfaces(name);
    };
    $('[data-act="save"]').onclick = () => act(() => api("/api/interfaces", { method: "PUT", body: S.ifaceEdit }), "Interfaces saved").then((d) => { S.interfaces = d; }).catch(() => {});
  }

  function drawIfaces(openName) {
    const list = $("#ifaceList");
    const open = new Set($$("details.iface-card[open]").map((d) => d.dataset.name));
    if (openName) open.add(openName);
    list.innerHTML = Object.entries(S.ifaceEdit).map(([k, f]) => `
      <details class="iface-card" data-name="${esc(k)}" ${open.has(k) ? "open" : ""}>
        <summary><strong>${esc(f.label || k)}</strong><span class="faint">${esc(k)}</span><span class="muted">${esc(f.description || "")}</span></summary>
        <div class="iface-body">
          <div class="form-grid">
            <label class="field">Label<input data-k="label" value="${esc(f.label || "")}"></label>
            <label class="field" style="grid-column:span 2">Description<input data-k="description" value="${esc(f.description || "")}"></label>
            <label class="field">Think-time between actions (s)<span class="row"><input class="narrow" data-k="wait_sec.0" type="number" step="0.1" min="0" value="${esc(f.wait_sec[0])}"> to <input class="narrow" data-k="wait_sec.1" type="number" step="0.1" min="0" value="${esc(f.wait_sec[1])}"></span></label>
            <label class="field">Session length before logging out and back in (s)<span class="row"><input class="narrow" data-k="session_lifetime_sec.0" type="number" min="0" placeholder="stay" value="${esc(f.session_lifetime_sec ? f.session_lifetime_sec[0] : "")}"> to <input class="narrow" data-k="session_lifetime_sec.1" type="number" min="0" placeholder="stay" value="${esc(f.session_lifetime_sec ? f.session_lifetime_sec[1] : "")}"></span><span class="help">leave empty to stay logged in</span></label>
            <label class="field">Share of heavy users (0-1)<input data-k="heavy_user_fraction" type="number" step="0.05" min="0" max="1" value="${esc(f.heavy_user_fraction ?? 0)}"></label>
            <label class="field">Heavy users' think-time ×<input data-k="heavy_wait_multiplier" type="number" step="0.1" min="0.01" value="${esc(f.heavy_wait_multiplier ?? 1)}"></label>
            <label class="field">Logs in<span class="row"><input type="checkbox" data-k="login" ${f.login !== false ? "checked" : ""}> sign in before working</span></label>
          </div>
          <div class="section-title">Actions</div>
          <div class="table-wrap"><table>
            <thead><tr><th>Name</th><th style="width:100px">Protocol</th><th style="width:90px">Method</th><th>Path</th><th style="width:80px">Weight</th><th style="width:60px">Auth</th><th>Query (JSON)</th><th>Body (JSON)</th><th style="width:90px">Upload KB</th><th>Page assets</th><th style="width:40px"></th></tr></thead>
            <tbody>${f.tasks.map((t, i) => `<tr data-t="${i}">
              <td><input data-tk="name" value="${esc(t.name || "")}"></td>
              <td><select data-tk="protocol"><option value="http" ${t.protocol !== "grpc" ? "selected" : ""}>HTTP</option><option value="grpc" ${t.protocol === "grpc" ? "selected" : ""}>gRPC</option></select></td>
              <td><select data-tk="method" ${t.protocol === "grpc" ? "disabled" : ""}>${["GET", "POST", "PUT", "PATCH", "DELETE", "CALL"].map((m) => `<option ${m === t.method ? "selected" : ""}>${m}</option>`).join("")}</select></td>
              <td><input data-tk="path" class="mono" value="${esc(t.path)}"></td>
              <td><input data-tk="weight" type="number" min="0.1" step="0.1" value="${esc(t.weight)}"></td>
              <td><input data-tk="auth" type="checkbox" ${t.auth !== false ? "checked" : ""}></td>
              <td><input data-tk="params" class="mono" value="${esc(t.params ? JSON.stringify(t.params) : "")}" placeholder="{}"></td>
              <td><input data-tk="json" class="mono" value="${esc(t.json ? JSON.stringify(t.json) : "")}" placeholder="{}"></td>
              <td><input data-tk="body_kb" type="number" min="0" value="${esc(t.body_kb || "")}"></td>
              <td><input data-tk="assets" class="mono" value="${esc((t.assets || []).join(", "))}" placeholder="none"></td>
              <td><button class="ghost" data-tdel aria-label="Remove action" ${f.tasks.length === 1 ? "disabled" : ""}>✕</button></td></tr>`).join("")}</tbody></table></div>
          <div class="row"><button data-tadd>Add action</button><span style="flex:1"></span><button class="danger" data-idel>Delete interface</button></div>
        </div></details>`).join("");
    $$("details.iface-card").forEach((card) => {
      const f = S.ifaceEdit[card.dataset.name];
      card.querySelectorAll("[data-k]").forEach((inp) => (inp.oninput = inp.onchange = () => {
        const [k, idx] = inp.dataset.k.split(".");
        if (inp.type === "checkbox") f[k] = inp.checked;
        else if (k === "session_lifetime_sec") {
          const a = card.querySelector('[data-k="session_lifetime_sec.0"]').value, b = card.querySelector('[data-k="session_lifetime_sec.1"]').value;
          f[k] = a === "" && b === "" ? null : [Number(a || b), Number(b || a)];
        } else if (idx !== undefined) { f[k][Number(idx)] = Number(inp.value); }
        else if (inp.type === "number") f[k] = Number(inp.value);
        else f[k] = inp.value;
      }));
      card.querySelectorAll("tr[data-t]").forEach((tr) => {
        const t = f.tasks[Number(tr.dataset.t)];
        tr.querySelectorAll("[data-tk]").forEach((inp) => (inp.onchange = () => {
          const k = inp.dataset.tk;
          if (k === "auth") t.auth = inp.checked;
          else if (k === "weight") t.weight = Number(inp.value) || 1;
          else if (k === "body_kb") { if (inp.value) t.body_kb = Number(inp.value); else delete t.body_kb; }
          else if (k === "protocol") { if (inp.value === "grpc") { t.protocol = "grpc"; t.method = "CALL"; } else { delete t.protocol; if (t.method === "CALL") t.method = "GET"; } drawIfaces(); }
          else if (k === "assets") { const l = inp.value.split(",").map((x) => x.trim()).filter(Boolean); if (l.length) t.assets = l; else delete t.assets; }
          else if (k === "params" || k === "json") {
            if (!inp.value.trim()) { delete t[k]; inp.style.borderColor = ""; return; }
            try { t[k] = JSON.parse(inp.value); inp.style.borderColor = ""; } catch { inp.style.borderColor = "var(--bad)"; toast(`${k} must be valid JSON, e.g. {"limit": 10}`, "error"); }
          } else t[k] = inp.value;
        }));
        tr.querySelector("[data-tdel]").onclick = () => { f.tasks.splice(Number(tr.dataset.t), 1); drawIfaces(); };
      });
      card.querySelector("[data-tadd]").onclick = () => { f.tasks.push({ name: "", method: "GET", path: "/", weight: 1, auth: true }); drawIfaces(); };
      card.querySelector("[data-idel]").onclick = () => { if (confirm(`Delete interface ${card.dataset.name}? Profiles that use it will need editing.`)) { delete S.ifaceEdit[card.dataset.name]; drawIfaces(); } };
    });
  }

  // ═══ SETTINGS ═══════════════════════════════════════════════════════
  const SECTION_NAMES = { control_panel: "Control panel", target: "Server under test", auth: "Login", server: "Built-in server behaviour",
    capacity: "User capacity of this computer", load: "Load generator", calibration: "Capacity measurement", sla: "Pass / fail thresholds (SLA)", analysis: "Defect detection rules" };
  const CHOICES = { "target.mode": ["builtin", "external"], "capacity.mode": ["auto", "measured", "fixed"], "server.rate_limit.key": ["username", "ip"] };

  async function renderSettings() {
    const d = await api("/api/config");
    S.settings = d;
    const field = (path, v, def) => {
      const help = d.help[path] || "";
      const name = path.split(".").pop().replace(/_/g, " ");
      let input;
      if (CHOICES[path]) input = `<select data-p="${path}" data-t="str">${CHOICES[path].map((c) => `<option ${c === v ? "selected" : ""}>${c}</option>`).join("")}</select>`;
      else if (typeof def === "boolean") input = `<span class="row"><input type="checkbox" data-p="${path}" data-t="bool" ${v ? "checked" : ""}> on</span>`;
      else if (Array.isArray(def)) input = `<span class="row">${def.map((_, i) => `<input class="narrow" type="number" step="any" data-p="${path}" data-t="arr" data-i="${i}" value="${esc(v[i])}">`).join(" to ")}</span>`;
      else if (typeof def === "number") input = `<input type="number" step="any" data-p="${path}" data-t="num" value="${esc(v)}">`;
      else input = `<input data-p="${path}" data-t="${path === "load.workers" ? "auto" : "str"}" value="${esc(v)}">`;
      return `<label class="field">${esc(name)}${input}${help ? `<span class="help">${esc(help)}</span>` : ""}</label>`;
    };
    const section = (key, obj, defs, prefix) => {
      const plain = [], nested = [];
      for (const [k, v] of Object.entries(defs)) {
        const p = prefix ? `${prefix}.${k}` : k;
        if (v && typeof v === "object" && !Array.isArray(v)) nested.push(section(k, obj[k] || {}, v, p));
        else plain.push(field(p, obj[k] ?? v, v));
      }
      const title = SECTION_NAMES[prefix] || prefix.split(".").pop().replace(/_/g, " ");
      return `<section class="card settings-section"><h2>${esc(title)}</h2>${d.help[prefix] ? `<p class="muted">${esc(d.help[prefix])}</p>` : ""}
        ${plain.length ? `<div class="form-grid">${plain.join("")}</div>` : ""}${nested.join("")}</section>`;
    };
    view.innerHTML = `<div class="topbar"><h1>Settings</h1><div class="row"><button data-act="reset">Reset to defaults</button><button class="primary" data-act="save">Save settings</button></div></div>
      <p class="muted">Saved to <span class="mono">${esc(d.file)}</span>. Every number the simulator uses lives here.</p>
      ${Object.keys(d.defaults).map((k) => section(k, d.config[k] || {}, d.defaults[k], k)).join("")}
      <div class="row end"><button class="primary" data-act="save">Save settings</button></div>`;
    $$('[data-act="save"]').forEach((b) => (b.onclick = saveSettings));
    $('[data-act="reset"]').onclick = () => { if (confirm("Put every setting back to its default?")) act(() => api("/api/config/reset", { method: "POST" }), "Settings reset").then(renderSettings).catch(() => {}); };
  }
  async function saveSettings() {
    const cfg = JSON.parse(JSON.stringify(S.settings.config));
    for (const inp of $$("[data-p]")) {
      const keys = inp.dataset.p.split(".");
      let node = cfg;
      keys.slice(0, -1).forEach((k) => (node = node[k] = node[k] || {}));
      const last = keys[keys.length - 1];
      const t = inp.dataset.t;
      if (t === "bool") node[last] = inp.checked;
      else if (t === "num") node[last] = inp.value === "" ? null : Number(inp.value);
      else if (t === "arr") { node[last] = Array.isArray(node[last]) ? node[last] : []; node[last][Number(inp.dataset.i)] = Number(inp.value); }
      else if (t === "auto") node[last] = /^\d+$/.test(inp.value.trim()) ? Number(inp.value) : inp.value.trim();
      else node[last] = inp.value;
    }
    try {
      const r = await act(() => api("/api/config", { method: "PUT", body: cfg }), "Settings saved");
      S.settings.config = r.config; S.state = r.state;
      (r.notes || []).forEach((n) => toast(n));
    } catch {}
  }

  // ═══ RESULTS ════════════════════════════════════════════════════════
  async function renderRuns() {
    view.innerHTML = `<div class="topbar"><h1>Results</h1><div class="row"><span class="muted" id="cmpHint">Tick two finished runs to compare them</span><button data-act="compare" disabled>Compare</button></div></div>
      <div class="card flush"><div class="table-wrap"><table id="runTable"></table></div></div>`;
    const runs = await api("/api/runs");
    S.compare = [];
    $("#runTable").innerHTML = `<thead><tr><th style="width:36px"></th><th>Started</th><th>Profile</th><th class="num">Peak users</th><th>Result</th><th class="num">p95</th><th class="num">Failed</th><th class="num">Req/s</th><th class="num">Findings</th><th class="num">Seed</th></tr></thead><tbody>${
      runs.map((r) => `<tr class="click" data-id="${esc(r.id)}"><td><input type="checkbox" data-cmp="${esc(r.id)}" aria-label="Compare" ${r.verdict && r.verdict !== "RUNNING" ? "" : "disabled"}></td>
        <td>${fmtDate(r.started)}<div class="faint">${esc(r.host || "")}</div></td><td><strong>${esc(r.profile_name)}</strong>${r.kind === "calibration" ? ' <span class="pill info">capacity</span>' : ""}${r.stopped_early ? ' <span class="faint">stopped early</span>' : ""}</td>
        <td class="num">${fmt(r.peak_users)}</td><td><span class="pill ${esc(r.verdict || "")}">${esc(r.verdict || "no data")}</span></td>
        <td class="num">${fmt(r.p95_ms)} ms</td><td class="num">${fmt(r.error_rate_pct, 2)}%</td><td class="num">${fmt(r.rps, 1)}</td><td class="num">${fmt(r.findings)}</td><td class="num faint">${esc(r.seed ?? "")}</td></tr>`).join("") ||
      `<tr><td colspan="10" class="empty">No tests yet. Start one from Live test.</td></tr>`}</tbody>`;
    $$("#runTable tr.click").forEach((tr) => (tr.onclick = (e) => { if (e.target.matches("input")) return; location.hash = "#run/" + tr.dataset.id; }));
    $$("[data-cmp]").forEach((c) => (c.onchange = () => {
      S.compare = $$("[data-cmp]:checked").map((x) => x.dataset.cmp);
      if (S.compare.length > 2) { c.checked = false; S.compare = $$("[data-cmp]:checked").map((x) => x.dataset.cmp); }
      $('[data-act="compare"]').disabled = S.compare.length !== 2;
    }));
    $('[data-act="compare"]').onclick = () => { const [b, a] = S.compare; location.hash = `#compare/${a}/${b}`; };
  }

  async function renderRun(id) {
    let d;
    try { d = await api("/api/runs/" + id); } catch (e) { view.innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
    const m = d.meta, s = d.summary;
    if (!s) { view.innerHTML = `<div class="topbar"><div><a href="#runs" class="faint">Results</a><h1>${esc(m.profile.name)}</h1></div></div><div class="card empty">This test is still running. Its results appear here when it finishes.</div>`; return; }
    const o = s.overall;
    view.innerHTML = `
      <div class="topbar"><div><a href="#runs" class="faint">Results</a><h1>${esc(m.profile.name)}</h1>
        <div class="muted">${fmtDate(m.started)} · ${fmtTime((m.ended || m.started) - m.started)} · ${esc(m.system.hostname)} (${m.system.logical_cores} cores) · capacity ${fmt(m.capacity.users)} users${m.stopped_early ? " · stopped early" : ""}</div></div>
        <div class="row"><span class="verdict ${s.verdict.toLowerCase()}">${s.verdict}</span></div></div>
      <div class="row no-print"><a class="btn" href="/api/runs/${esc(id)}/report" target="_blank" rel="noopener">Open full report</a><a class="btn" href="/api/runs/${esc(id)}/report?download=true">Download report</a>
        <a class="btn" href="/api/runs/${esc(id)}/samples.csv">Download data (CSV)</a>
        ${(s.charts || []).length ? `<a class="btn" href="/api/runs/${esc(id)}/files/phase_summary.csv">Phase summary (CSV)</a>` : ""}
        <button data-act="rerun" title="Runs the same profile with the same seed">Run again with seed ${esc(m.seed)}</button>
        <button class="danger" data-act="del">Delete</button></div>
      <section class="kpis">
        <div class="kpi"><div class="kpi-label">Peak users</div><div class="kpi-value">${fmt(o.users_max)}</div></div>
        <div class="kpi"><div class="kpi-label">Requests</div><div class="kpi-value">${fmt(o.requests)}</div><div class="kpi-sub">${fmt(o.rps, 1)} per second</div></div>
        <div class="kpi"><div class="kpi-label">Response time p95</div><div class="kpi-value">${fmt(o.p95_ms)}<small> ms</small></div><div class="kpi-sub">p50 ${fmt(o.p50_ms)} · p99 ${fmt(o.p99_ms)} ms</div></div>
        <div class="kpi"><div class="kpi-label">Failed requests</div><div class="kpi-value">${fmt(o.error_rate_pct, 2)}<small> %</small></div><div class="kpi-sub">${fmt(o.failures)} failed</div></div>
        <div class="kpi"><div class="kpi-label">CPU max</div><div class="kpi-value">${fmt(o.cpu_max)}<small> %</small></div><div class="kpi-sub">server ${fmt(o.server_cpu_max)}%</div></div>
        <div class="kpi"><div class="kpi-label">Memory max</div><div class="kpi-value">${fmt(o.mem_max)}<small> %</small></div><div class="kpi-sub">server ${fmt(o.server_rss_end)} MB at end</div></div>
      </section>
      <section class="split">
        <div class="card"><h2>What to look at</h2>${s.findings.length ? s.findings.map((f) => `<div class="finding ${f.severity}"><div class="finding-head"><span class="pill ${f.severity}">${esc(f.severity)}</span><strong>${esc(f.title)}</strong></div><p>${esc(f.detail)}</p></div>`).join("") : `<p class="muted">No likely defects were flagged.</p>`}
          ${s.calibration ? `<p>Measured capacity: <strong>${fmt(s.calibration.measured_users)} users</strong> within the SLA.</p>` : ""}</div>
        <div class="card"><h2>Pass / fail checks</h2><table><tbody>${s.checks.map((c) => `<tr><td>${esc(c.name)}</td><td class="num">${fmt(c.value, 1)} ${esc(c.unit)}</td><td class="num faint">limit ${fmt(c.limit)} ${esc(c.unit)}</td><td><span class="pill ${c.pass ? "ok" : c.pass === false ? "bad" : ""}">${c.pass ? "Pass" : c.pass === false ? "Fail" : "n/a"}</span></td></tr>`).join("")}</tbody></table></div>
      </section>
      ${coverageCard(s.coverage)}
      <section class="grid2">
        <div class="card"><h3>Users and throughput</h3><div class="chart-box"><canvas id="r1"></canvas></div></div>
        <div class="card"><h3>Response time</h3><div class="chart-box"><canvas id="r2"></canvas></div></div>
        <div class="card"><h3>Failed requests</h3><div class="chart-box"><canvas id="r3"></canvas></div></div>
        <div class="card"><h3>Computer and server</h3><div class="chart-box"><canvas id="r4"></canvas></div></div>
      </section>
      <section class="card flush"><div class="card-head" style="padding:14px 16px 0"><h2>By phase</h2></div><div class="table-wrap"><table>
        <thead><tr><th>Stage</th><th class="num">Planned</th><th class="num">Avg users</th><th class="num">Req/s</th><th class="num">p95</th><th class="num">p95 entering</th><th class="num">p95 settled</th><th class="num">Failed</th><th class="num">CPU max</th><th class="num">Server CPU</th><th class="num">Sessions at end</th></tr></thead>
        <tbody>${s.phases.map((p) => `<tr><td><span class="phase-dot ${esc(p.phase)}"></span>${esc(p.label)}${String(p.label).toLowerCase() === String(p.phase).replace("_", " ") ? "" : ` <span class="faint">${esc(PHASE_NAME[p.phase] || p.phase)}</span>`}</td><td class="num">${fmt(p.target_users)}</td><td class="num">${fmt(p.users_avg)}</td><td class="num">${fmt(p.rps, 1)}</td>
          <td class="num">${fmt(p.p95_ms)} ms</td><td class="num">${fmt(p.transition_p95_ms)} ms</td><td class="num">${fmt(p.settled_p95_ms)} ms</td><td class="num">${fmt(p.error_rate_pct, 2)}%</td><td class="num">${fmt(p.cpu_max)}%</td><td class="num">${fmt(p.server_cpu_max)}%</td><td class="num">${fmt(p.sessions_end)}</td></tr>`).join("")}</tbody></table></div></section>
      <section class="grid2">
        <div class="card flush"><div class="card-head" style="padding:14px 16px 0"><h2>By interface</h2></div><div class="table-wrap"><table>${sideTable(o.by_interface, false, S.interfaces)}</table></div></div>
        <div class="card flush"><div class="card-head" style="padding:14px 16px 0"><h2>By endpoint</h2></div><div class="table-wrap"><table>${sideTable(o.by_endpoint, true)}</table></div></div>
      </section>
      ${(s.charts || []).length ? `<section class="card"><h2>Static charts</h2><p class="muted">Drawn with Pandas and Matplotlib and saved in the run folder for slides and written reports.</p>
        <div class="grid2">${s.charts.map((c) => `<a href="/api/runs/${esc(id)}/files/${esc(c)}" target="_blank" rel="noopener"><img src="/api/runs/${esc(id)}/files/${esc(c)}" alt="${esc(c)}" loading="lazy" style="width:100%;border-radius:4px;border:1px solid var(--line)"></a>`).join("")}</div></section>` : ""}
      <section class="card"><h2>Most common errors</h2>${o.top_errors.length ? `<table><tbody>${o.top_errors.map((e) => `<tr><td class="mono">${esc(e.error)}</td><td class="num">${fmt(e.count)}</td></tr>`).join("")}</tbody></table>` : `<p class="muted">No failed requests.</p>`}</section>`;
    $('[data-act="rerun"]').onclick = () => act(() => api("/api/runs", { method: "POST", body: { profile_id: m.profile_id, seed: m.seed } }), `Started again with seed ${m.seed}`)
      .then((st) => { S.state = st; S.live = []; location.hash = "#dashboard"; }).catch(() => {});
    $('[data-act="del"]').onclick = () => { if (confirm("Delete this run and its report?")) act(() => api("/api/runs/" + id, { method: "DELETE" }), "Run deleted").then(() => (location.hash = "#runs")).catch(() => {}); };
    const rows = d.series.filter((x) => planT(x) != null && !x.final);
    const P = (f) => rows.map((x) => ({ x: planT(x), y: f(x) }));
    const L = window.DCCharts.lineChart, base = { stages: m.stages, xmin: 0, xmax: m.total_duration };
    S.charts.r1 = L($("#r1"), { ...base, yTitle: "users", y2: "req/s", series: [{ label: "Users", color: "--c-users", data: P((x) => x.users) }, { label: "Requests/s", color: "--c-rps", axis: "y2", data: P((x) => x.rps) }] });
    S.charts.r2 = L($("#r2"), { ...base, yTitle: "ms", series: [{ label: "p50", color: "--c-p50", data: P((x) => x.p50) }, { label: "p95", color: "--c-p95", data: P((x) => x.p95) }, { label: "p99", color: "--c-p99", dash: [4, 3], data: P((x) => x.p99) }] });
    S.charts.r3 = L($("#r3"), { ...base, yTitle: "%", series: [{ label: "Failed %", color: "--c-err", data: P((x) => (x.count ? (100 * x.fails) / x.count : 0)) }] });
    S.charts.r4 = L($("#r4"), { ...base, yTitle: "%", suggestedMax: 100, series: [{ label: "Computer CPU %", color: "--c-cpu", data: P((x) => (x.sys || {}).cpu_pct) }, { label: "Computer memory %", color: "--c-mem", data: P((x) => (x.sys || {}).mem_pct) }, { label: "Server CPU %", color: "--c-scpu", dash: [4, 3], data: P((x) => (x.target || {}).cpu_pct) }] });
  }
  function coverageCard(c) {
    if (!c) return "";
    const d = c.defects;
    const row = (name, part, extra) => `<tr><td>${esc(name)}</td><td class="num">${part.pct == null ? "–" : fmt(part.pct, 0) + "%"}</td><td class="muted">${extra}</td></tr>`;
    return `<section class="card"><h2>Coverage and defects</h2><div class="table-wrap"><table><tbody>
      ${row("Interfaces exercised", c.interfaces, esc(c.interfaces.exercised.join(", ")))}
      ${row("Endpoints exercised", c.endpoints, `${c.endpoints.exercised.length} of ${c.endpoints.planned.length}`)}
      ${row("Time windows with traffic", c.time_windows, `${c.time_windows.exercised} of ${c.time_windows.planned} stages`)}
      ${row("Phase types exercised", c.phases, esc(c.phases.exercised.map((p) => PHASE_NAME[p] || p).join(", ")))}
      <tr><td>Defects flagged</td><td class="num">${d.total}</td><td class="muted">${d.critical} critical, ${d.warning} warning${Object.keys(d.by_kind).length ? " · " + esc(Object.entries(d.by_kind).map(([k, v]) => k.replace("_", " ") + " " + v).join(", ")) : ""}</td></tr>
      <tr><td>Defects at phase transitions</td><td class="num">${d.transition}</td><td class="muted">latency jumps or error bursts as a phase starts</td></tr>
    </tbody></table></div></section>`;
  }
  function sideTable(d, mono, labels) {
    const rows = Object.entries(d || {}).sort((a, b) => b[1].requests - a[1].requests);
    return `<thead><tr><th>${mono ? "Endpoint" : "Interface"}</th><th class="num">Requests</th><th class="num">Req/s</th><th class="num">p50</th><th class="num">p95</th><th class="num">p99</th><th class="num">Failed</th></tr></thead><tbody>${
      rows.map(([k, v]) => `<tr><td class="${mono ? "mono" : ""}">${esc((labels && labels[k] && labels[k].label) || k)}</td><td class="num">${fmt(v.requests)}</td><td class="num">${fmt(v.rps, 1)}</td><td class="num">${fmt(v.p50_ms)}</td><td class="num">${fmt(v.p95_ms)}</td><td class="num">${fmt(v.p99_ms)}</td><td class="num">${fmt(v.error_rate_pct, 2)}%</td></tr>`).join("") ||
      `<tr><td colspan="7" class="muted">No requests.</td></tr>`}</tbody>`;
  }

  async function renderCompare(a, b) {
    let d;
    try { d = await api(`/api/compare?a=${encodeURIComponent(a)}&b=${encodeURIComponent(b)}`); } catch (e) { view.innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
    const names = { users_avg: "Avg users", rps: "Req/s", p95_ms: "p95 ms", error_rate_pct: "Failed %", cpu_max: "CPU max %", server_cpu_max: "Server CPU %", p99_ms: "p99 ms", requests: "Requests" };
    const better = { p95_ms: -1, p99_ms: -1, error_rate_pct: -1, cpu_max: -1, server_cpu_max: -1, rps: 1, requests: 1, users_avg: 0 };
    const cell = (k, v) => {
      const dl = v.delta; let color = "";
      if (dl && better[k]) color = dl * better[k] > 0 ? "var(--ok)" : "var(--bad)";
      return `<td class="num">${fmt(v.a, 1)}</td><td class="num">${fmt(v.b, 1)}</td><td class="num" style="color:${color}">${dl == null ? "–" : (dl > 0 ? "+" : "") + fmt(dl, 1)}</td>`;
    };
    view.innerHTML = `<div class="topbar"><div><a href="#runs" class="faint">Results</a><h1>Compare runs</h1></div></div>
      <section class="grid2"><div class="card"><div class="eyebrow">A · earlier</div><h2><a href="#run/${esc(d.a)}">${esc(d.names.a)}</a></h2><span class="pill ${d.verdicts.a}">${d.verdicts.a}</span> <span class="faint">${esc(d.a)}</span></div>
        <div class="card"><div class="eyebrow">B · later</div><h2><a href="#run/${esc(d.b)}">${esc(d.names.b)}</a></h2><span class="pill ${d.verdicts.b}">${d.verdicts.b}</span> <span class="faint">${esc(d.b)}</span></div></section>
      <section class="card flush"><div class="card-head" style="padding:14px 16px 0"><h2>Whole run</h2><span class="faint">green = B is better</span></div><div class="table-wrap"><table>
        <thead><tr><th>Measure</th><th class="num">A</th><th class="num">B</th><th class="num">Change</th></tr></thead>
        <tbody>${Object.entries(d.overall).map(([k, v]) => `<tr><td>${names[k] || k}</td>${cell(k, v)}</tr>`).join("")}</tbody></table></div></section>
      <section class="card flush"><div class="card-head" style="padding:14px 16px 0"><h2>Phase by phase</h2></div><div class="table-wrap"><table>
        <thead><tr><th>Phase</th>${["rps", "p95_ms", "error_rate_pct", "cpu_max"].map((k) => `<th class="num" colspan="3">${names[k]} (A / B / change)</th>`).join("")}</tr></thead>
        <tbody>${d.phases.map((r) => `<tr><td>${esc(r.phase)}</td>${["rps", "p95_ms", "error_rate_pct", "cpu_max"].map((k) => cell(k, r[k])).join("")}</tr>`).join("")}</tbody></table></div></section>`;
  }

  // ═══ LOGS ═══════════════════════════════════════════════════════════
  function renderLogs() {
    view.innerHTML = `<div class="topbar"><h1>Logs</h1><div class="row">
      <select id="logSrc"><option value="events">Activity</option><option value="server">Server log</option><option value="server_errors">Server errors</option><option value="load">Load generator (current test)</option><option value="control">Control panel</option></select>
      <label class="row muted"><input type="checkbox" id="logAuto" checked> keep refreshing</label><button id="logRefresh">Refresh</button></div></div>
      <div class="card"><div class="log-view mono" id="logView">Loading…</div></div>`;
    const load = async () => {
      try {
        const r = await api(`/api/logs?source=${$("#logSrc").value}&lines=400`);
        const el = $("#logView");
        const atBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 30;
        el.textContent = r.lines.length ? r.lines.join("\n") : "Nothing logged yet.";
        if (atBottom) el.scrollTop = el.scrollHeight;
      } catch (e) { $("#logView").textContent = e.message; }
    };
    $("#logSrc").onchange = load; $("#logRefresh").onclick = load;
    load();
    S.logTimer = setInterval(() => { if ($("#logAuto") && $("#logAuto").checked) load(); }, 3000);
  }

  // ── boot ───────────────────────────────────────────────────────────
  window.addEventListener("resize", () => { if (S.route === "dashboard") updateSkyline(); });
  (async () => {
    await refreshState();
    api("/api/interfaces").then((d) => (S.interfaces = d)).catch(() => {});
    api("/api/config").then((d) => (S.settings = d)).catch(() => {});
    route();
    connect();
  })();
})();
