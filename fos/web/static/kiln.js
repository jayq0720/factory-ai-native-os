// 窑炉监控：窑况、告警、窑次曲线、结束窑次、人工抄表、能耗。无构建步骤，原生 JS；只调用 /v1 接口，API Key 与其他页面共用。
"use strict";

const $ = (s) => document.querySelector(s);
const KEY = "fos.apiKey";
const state = { key: "", run: "", timer: null };

function store(op, v) {
  try {
    if (op === "get") return localStorage.getItem(KEY) || "";
    if (op === "set") localStorage.setItem(KEY, v);
    if (op === "del") localStorage.removeItem(KEY);
  } catch (_) {}
  return "";
}
async function api(path, opts = {}) {
  const headers = Object.assign({ Authorization: "Bearer " + state.key }, opts.headers || {});
  const r = await fetch(path, Object.assign({}, opts, { headers }));
  if (r.status === 401) { logout(); throw new Error("登录已失效，请重新输入 API Key"); }
  return r;
}
async function detail(r) {
  try { const j = await r.json(); return typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail); } catch (_) { return r.status + " " + r.statusText; }
}
function toast(msg, ms = 4000) { const t = $("#toast"); t.textContent = msg; t.hidden = false; clearTimeout(toast._t); toast._t = setTimeout(() => (t.hidden = true), ms); }
function el(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v;
    else if (v !== null && v !== undefined && v !== false) e.setAttribute(k, v);
  }
  for (const k of kids.flat()) if (k !== null && k !== undefined && k !== false) e.append(k.nodeType ? k : String(k));
  return e;
}
const fill = (n, ...k) => n.replaceChildren(...k.flat().filter((x) => x !== null && x !== undefined && x !== false));

async function login(key) {
  state.key = key;
  const r = await api("/v1/me");
  if (!r.ok) throw new Error(await detail(r));
  const me = await r.json();
  store("set", key);
  $("#login").hidden = true; $("#app").hidden = false; $("#logout").hidden = false;
  $("#who").textContent = `${me.tenant_name} · ${me.display_name}`;
  refresh();
}
function logout() { store("del"); state.key = ""; $("#app").hidden = true; $("#login").hidden = false; $("#logout").hidden = true; $("#who").textContent = ""; }


// ---------------- 通用 ----------------
const n = (x) => (x === null || x === undefined ? "—" : String(+(+x).toFixed(1)));
const t = (s) => (s ? new Date(s).toLocaleString("zh-CN", { hour12: false }) : "—");
async function getJ(path) { const r = await api(path); if (!r.ok) throw new Error(await detail(r)); return r.json(); }
async function post(path, body) {
  const r = await api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
  if (!r.ok) throw new Error(await detail(r));
  return r.json();
}
const table = (heads, rows) => el("table", {}, el("tr", {}, heads.map((x) => el("th", {}, x))), rows);
const wrap = (fn) => () => fn().catch((e) => toast(e.message, 8000));
const KIND = { high: "超上限", low: "低于下限", deviation: "偏离曲线", edge_offline: "网关断网", point_fault: "设备故障", edge_backlog: "网关积压", edge_rejected: "网关被拒批次" };
function refresh() { return Promise.all([loadBoard(), loadEnergy(), loadGateways()]).catch((e) => toast(e.message)); }

// ---------------- 采集网关（W21） ----------------
async function loadGateways() {
  const r = await api("/v1/edge/gateways");
  if (r.status === 403) { fill($("#gateways"), el("p", { class: "muted small" }, "你没有查看采集网关的权限（production.edge:read）。")); return; }
  if (!r.ok) throw new Error(await detail(r));
  const g = await r.json();
  const head = g.signing
    ? el("p", { class: "muted small" }, "云端验签公钥（填进 edge.toml 的 command_public_key）：", el("code", {}, g.public_key))
    : el("p", { class: "bad small" }, "云端没有配置命令签名私钥（FOS_EDGE_COMMAND_KEY），不能下发命令；用 fos edge-keygen 生成。");
  if (!g.gateways.length) { fill($("#gateways"), head, el("p", { class: "muted" }, "还没有采集网关。")); return; }
  fill($("#gateways"), head, g.gateways.map((e) => {
    const s = e.status, h = e.health;
    const stat = s
      ? `v${s.version} · 积压 ${s.pending_batches} 批 / ${s.pending_readings} 条${h && h.backlog_minutes !== null ? `（最早 ${n(h.backlog_minutes)} 分钟）` : ""} · 被拒 ${s.rejected_batches} 批${h && h.failing_points.length ? " · 连续失败：" + h.failing_points.join("、") : ""}（${t(e.status_at)}）`
      : "还没报过状态";
    return el("div", { class: "kiln" },
      el("h4", {}, e.edge_id, el("span", { class: e.online ? "ok small" : "bad small" }, e.online ? "  在线" : "  离线"),
        el("span", { class: "muted small" }, `  最后上行 ${t(e.last_seen)} · ${e.points.length} 个测点${e.data_mode === "mock" ? " · 模拟" : ""}`)),
      el("p", { class: "small" }, stat),
      el("div", { class: "row" },
        cmdBtn(e.edge_id, "上报状态", { type: "report_status" }, g.signing),
        e.points.length ? cmdBtn(e.edge_id, "全部立即读数", { type: "read_now", points: e.points.slice(0, 50) }, g.signing) : null,
        resendBtn(e.edge_id, g.signing)),
      e.commands.length ? table(["命令", "状态", "结果", "谁要的", "时间"], e.commands.map((c) => el("tr", {},
        el("td", {}, c.text), el("td", { class: c.status === "failed" || c.status === "expired" ? "bad" : "" }, c.status_name),
        el("td", {}, c.result || "—"), el("td", {}, c.created_by_name), el("td", {}, t(c.created_at))))) : null);
  }));
}
function cmdBtn(edge, label, command, enabled) {
  const b = el("button", { class: "btn small", disabled: !enabled }, label);
  b.addEventListener("click", wrap(async () => { await post("/v1/edge/commands", { edge_id: edge, command }); toast("已排队，网关下次上行时带下去"); await loadGateways(); }));
  return b;
}
function resendBtn(edge, enabled) {
  const b = el("button", { class: "btn small", disabled: !enabled }, "重新补传…");
  b.addEventListener("click", wrap(async () => {
    const v = prompt("从哪个序号起重新补传？（网关上 fos-edge status 可查被拒批次的序号）");
    if (v === null || v.trim() === "") return;
    await post("/v1/edge/commands", { edge_id: edge, command: { type: "resend", from_seq: Number(v) } });
    toast("已排队"); await loadGateways();
  }));
  return b;
}

// ---------------- 窑况 ----------------
async function loadBoard() {
  const b = await getJ("/v1/kiln/board");
  $("#mock-banner").hidden = b.data_mode !== "mock";
  const all = b.alarms.concat(...b.kilns.map((k) => k.alarms));
  fill($("#alarms"), all.length
    ? table(["类型", "内容", "开始", "处理"], all.map((a) => el("tr", {},
        el("td", { class: "bad" }, KIND[a.kind] || a.kind), el("td", {}, a.message), el("td", {}, t(a.started_at)),
        el("td", {}, a.acked_at ? `${a.acked_by_name} 已知晓` : ackBtn(a.id)))))
    : el("p", { class: "ok" }, "没有未关闭的告警。"));
  fill($("#board"), b.kilns.map((k) => el("div", { class: "kiln" },
    el("h4", {}, k.name, k.run ? el("span", { class: "muted small" }, `  在烧 ${k.run.run_no} · 第 ${n(k.run.hours)} 小时${k.run.curve ? " · " + k.run.curve : ""}`) : el("span", { class: "muted small" }, "  未在烧")),
    table(["测点", "最新", "目标", "偏差"], k.points.map((p) => el("tr", {},
      el("td", {}, p.name, el("div", { class: "muted small" }, `${p.source_name} · ${p.point}`)),
      el("td", {}, p.latest ? `${n(p.latest.value)} ${p.unit}` : "—", el("div", { class: "muted small" }, p.latest ? `${n(p.age_minutes)} 分钟前` : "")),
      el("td", {}, n(p.expected)),
      el("td", { class: p.deviation !== null && k.run && k.run.tolerance && Math.abs(p.deviation) > k.run.tolerance ? "bad" : "" }, n(p.deviation))))),
    k.run ? el("button", { class: "btn small", "data-run": k.run.id }, "看曲线") : null)));
  for (const btn of document.querySelectorAll("[data-run]")) btn.addEventListener("click", wrap(() => { $("#i-run").value = btn.dataset.run; return loadCurve(); }));
}
function ackBtn(id) {
  const b = el("button", { class: "btn small" }, "已知晓");
  b.addEventListener("click", wrap(async () => { await post(`/v1/kiln/alarms/${id}/ack`); toast("已确认，不再重复提醒"); await loadBoard(); }));
  return b;
}
async function loadDemo() {
  const r = await post("/v1/kiln/demo");
  toast(r.created ? `已写入演示窑炉和 ${r.readings} 条模拟读数` : "演示数据已经存在");
  $("#i-run").value = r.run;
  await Promise.all([loadBoard(), loadCurve(), loadEnergy()]);
}

// ---------------- 曲线（SVG，无第三方库） ----------------
const NS = "http://www.w3.org/2000/svg";
function svg(tag, attrs) { const e = document.createElementNS(NS, tag); for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v); return e; }
async function loadCurve() {
  const id = $("#i-run").value.trim();
  if (!id) throw new Error("请先填窑次 id");
  const c = await getJ(`/v1/kiln/runs/${encodeURIComponent(id)}/curve`);
  state.run = id;
  const W = 1000, H = 280, L = 48, B = 24;
  const t0 = Date.parse(c.started_at), t1 = Date.parse(c.ended_at || new Date().toISOString());
  const tEnd = Math.max(t1, ...c.curve.series.map((p) => Date.parse(p[0])));
  const vals = c.lines.flatMap((l) => l.readings.map((r) => r[1])).concat(c.curve.series.map((p) => p[1]));
  const vmax = Math.max(100, ...vals) * 1.05;
  const x = (ts) => L + ((Date.parse(ts) - t0) / Math.max(1, tEnd - t0)) * (W - L - 8);
  const y = (v) => H - B - (v / vmax) * (H - B - 8);
  const g = svg("svg", { viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: "none" });
  for (let i = 0; i <= 4; i++) {
    const v = (vmax / 4) * i;
    g.append(svg("line", { x1: L, x2: W, y1: y(v), y2: y(v), stroke: "#e5e7eb" }));
    const lab = svg("text", { x: 4, y: y(v) + 4, "font-size": 11, fill: "#6b7280" }); lab.textContent = Math.round(v); g.append(lab);
  }
  const path = (pts, attrs) => pts.length && g.append(svg("polyline", Object.assign({ points: pts.map((p) => `${x(p[0])},${y(p[1])}`).join(" "), fill: "none" }, attrs)));
  if (c.curve.tolerance) {
    path(c.curve.series.map((p) => [p[0], p[1] + c.curve.tolerance]), { stroke: "#d1d5db", "stroke-dasharray": "4 4" });
    path(c.curve.series.map((p) => [p[0], Math.max(0, p[1] - c.curve.tolerance)]), { stroke: "#d1d5db", "stroke-dasharray": "4 4" });
  }
  path(c.curve.series, { stroke: "#9ca3af", "stroke-width": 2 });
  const colors = ["#2563eb", "#059669", "#d97706", "#7c3aed"];
  c.lines.forEach((l, i) => {
    path(l.readings, { stroke: colors[i % colors.length], "stroke-width": 1.5 });
    for (const s of l.spans) g.append(svg("rect", { x: x(s.start), y: 0, width: Math.max(2, x(s.end) - x(s.start)), height: H - B, fill: "#fecaca", opacity: 0.5 }));
  });
  const legend = el("div", { class: "legend" },
    el("span", {}, `制度：${c.curve.code ? `${c.curve.code}（${c.curve.from}，±${n(c.curve.tolerance)}℃）` : "未指定（只看上下限）"}`),
    ...c.lines.map((l, i) => el("span", { style: `color:${colors[i % colors.length]}` }, `■ ${l.name} 最高 ${n(l.max_temp)}`)),
    el("span", { class: c.deviation ? "bad" : "ok" }, c.deviation ? `超差 ${n(c.deviation_minutes)} 分钟（红色区间）` : "未超差"));
  const energy = Object.keys(c.energy).length ? el("p", { class: "muted small" }, `能耗：${c.energy.electricity_kwh !== undefined ? n(c.energy.electricity_kwh) + " kWh " : ""}${c.energy.gas_m3 !== undefined ? n(c.energy.gas_m3) + " m³" : ""}`) : null;
  fill($("#curve"), el("p", {}, `${c.run_no} · ${t(c.started_at)} — ${c.ended_at ? t(c.ended_at) : "在烧"} · ${c.samples} 个读数${c.data_mode === "mock" ? " · 模拟数据" : ""}`), legend, g, energy);
}
async function finish() {
  const id = $("#i-run").value.trim();
  if (!id) throw new Error("请先填窑次 id");
  if (!confirm("结束这个窑次？最高温度、超差和能耗会按读数算出并入账，入账后不能撤销。")) return;
  const r = await post(`/v1/kiln/runs/${encodeURIComponent(id)}/finish`, {});
  toast(`已入账：最高 ${n(r.summary.max_temp)}℃，${r.summary.deviation ? "超差 " + n(r.summary.deviation_minutes) + " 分钟" : "未超差"}`, 8000);
  await Promise.all([loadBoard(), loadCurve()]);
}

// ---------------- 抄表、能耗 ----------------
async function meter() {
  const point = $("#i-point").value.trim(), value = parseFloat($("#i-value").value);
  if (!point || Number.isNaN(value)) throw new Error("请填测点和数值");
  const r = await post("/v1/kiln/readings", { point, value });
  toast(`已记录 ${r.name}：${n(r.value)} ${r.unit}`);
  $("#i-value").value = "";
  await loadBoard();
}
async function loadEnergy() {
  const e = await getJ("/v1/kiln/energy");
  fill($("#energy"), e.rows.length
    ? table(["窑炉", "耗电 kWh", "耗气 m³"], e.rows.map((r) => el("tr", {}, el("td", {}, r.name), el("td", {}, n(r.electricity_kwh)), el("td", {}, n(r.gas_m3)))))
    : el("p", { class: "muted" }, "还没有登记电表 / 燃气表测点。"));
}

// ---------------- 事件 ----------------
$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try { await login($("#key").value.trim()); $("#login-err").hidden = true; }
  catch (err) { $("#login-err").textContent = err.message; $("#login-err").hidden = false; }
});
$("#logout").addEventListener("click", logout);
$("#b-board").addEventListener("click", wrap(loadBoard));
$("#b-demo").addEventListener("click", wrap(loadDemo));
$("#b-curve").addEventListener("click", wrap(loadCurve));
$("#b-finish").addEventListener("click", wrap(finish));
$("#b-meter").addEventListener("click", wrap(meter));
$("#b-energy").addEventListener("click", wrap(loadEnergy));
$("#b-gw").addEventListener("click", wrap(loadGateways));
state.timer = setInterval(() => { if (state.key && $("#auto").checked) loadBoard().catch(() => {}); }, 30000);

const saved = store("get");
if (saved) login(saved).catch(() => { $("#login").hidden = false; });
else $("#login").hidden = false;
