// 计划排窑：排窑建议（求解器）→ 核对 → 确认；交期评估。无构建步骤，原生 JS；只调用 /v1 接口，API Key 与其他页面共用。
"use strict";

const $ = (s) => document.querySelector(s);
const KEY = "fos.apiKey";
const state = { key: "", draft: null };

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
  loadCurrent();
}
function logout() { store("del"); state.key = ""; $("#app").hidden = true; $("#login").hidden = false; $("#logout").hidden = true; $("#who").textContent = ""; }

// ---------------- 排窑计划（建议 / 当前） ----------------
const fmt = (iso) => (iso ? iso.slice(5, 16).replace("T", " ") : "");
function gantt(p) {
  const t0 = Date.parse(p.horizon_start), t1 = Date.parse(p.horizon_end), span = t1 - t0;
  const lanes = p.kilns.map((k) => {
    const bars = p.runs.filter((r) => r.kiln === k.kiln).map((r) => {
      const a = Math.max(Date.parse(r.start_at), t0), b = Math.min(Date.parse(r.end_at), t1);
      const loads = r.loads.map((l) => `${l.order_no} ${l.qty}`).join("、");
      return el("div", {
        class: "bar" + (r.fill_pct !== null && r.fill_pct < 80 ? " part" : ""),
        style: `left:${((a - t0) / span) * 100}%;width:${Math.max(((b - a) / span) * 100, 0.5)}%`,
        title: `${r.run_no}\n${fmt(r.start_at)} → ${fmt(r.end_at)}\n装 ${r.qty}/${r.capacity} ${r.unit || ""}：${loads}`,
      }, loads);
    });
    return el("div", { class: "lane" },
      el("div", {}, k.name, el("div", { class: "muted small" }, `利用率 ${k.utilization_pct ?? "-"}%，平均装满 ${k.avg_fill_pct ?? "-"}%`)),
      el("div", { class: "track" }, bars));
  });
  return el("div", { class: "gantt" }, lanes, el("div", { class: "axis" }, el("span", {}, fmt(p.horizon_start)), el("span", {}, fmt(p.horizon_end))));
}
function render(p, editable) {
  if (!p) return fill($("#plan"), el("p", { class: "muted" }, "还没有确认的排窑计划。点“生成排窑建议”。"));
  $("#mock-banner").hidden = p.data_mode !== "mock";
  const head = p.plan
    ? `当前计划 ${p.plan.plan_no}（${p.plan.solver_status}）`
    : `排窑建议（求解 ${p.solver.status}，${p.solver.seconds} 秒）——还没有入账`;
  const kids = [
    el("p", { class: "ok" }, `${head}：${p.runs.length} 个窑次，${fmt(p.horizon_start)} → ${fmt(p.horizon_end)}`),
    p.warnings.length ? el("ul", { class: "muted small" }, p.warnings.map((w) => el("li", {}, w))) : null,
    gantt(p),
    el("h3", {}, "订单"),
    el("table", {},
      el("tr", {}, editable ? el("th", {}, "承诺交期") : null, el("th", {}, "订单"), el("th", {}, "客户"), el("th", {}, "产品"),
        el("th", {}, "待烧 / 已排"), el("th", {}, "交期"), el("th", {}, "预计可发货"), el("th", {}, "承诺"), el("th", {}, "结论")),
      p.orders.map((o) => el("tr", {},
        editable ? el("td", {}, el("input", { type: "checkbox", class: "promise", value: o.order, checked: o.ok ? "" : null, disabled: o.short > 0 ? "" : null })) : null,
        el("td", {}, o.order_no), el("td", {}, o.customer), el("td", {}, o.product_code),
        el("td", {}, `${o.to_fire} / ${o.planned} ${o.unit}`), el("td", {}, o.due_date || "-"),
        el("td", {}, o.ready_date || "-"), el("td", {}, o.promised_date || "-"),
        el("td", { class: o.ok ? "ok" : "bad" }, o.status_text)))),
    el("h3", {}, "原料需求"),
    el("p", { class: "muted small" }, p.materials.note),
    p.materials.materials.length
      ? el("table", {},
          el("tr", {}, el("th", {}, "原料"), el("th", {}, "需要"), el("th", {}, "现存"), el("th", {}, "在途"), el("th", {}, "缺口"), el("th", {}, "哪天起要用")),
          p.materials.materials.map((m) => el("tr", {},
            el("td", {}, m.name), el("td", {}, `${m.need} ${m.unit}`), el("td", {}, m.on_hand), el("td", {}, m.incoming + m.on_order),
            el("td", { class: m.shortage > 0 ? "bad" : "ok" }, m.shortage > 0 ? m.shortage : "够"), el("td", {}, m.short_from || "-"))))
      : el("p", { class: "muted small" }, "没有算出物料需求（产品没有配方用量，或计划为空）。"),
  ];
  fill($("#plan"), kids);
}
async function loadCurrent() {
  const r = await api("/v1/planning/plan?mode=" + $("#mode").value);
  if (!r.ok) return toast(await detail(r));
  state.draft = null; $("#b-confirm").disabled = true;
  render(await r.json(), false);
}
async function draft() {
  $("#b-draft").disabled = true;
  try {
    const r = await api("/v1/planning/draft?mode=" + $("#mode").value, { method: "POST" });
    if (!r.ok) return toast(await detail(r));
    state.draft = await r.json();
    render(state.draft, true);
    $("#b-confirm").disabled = !state.draft.runs.length;
  } finally { $("#b-draft").disabled = false; }
}
async function confirmPlan() {
  const d = state.draft;
  if (!d || !confirm(`确认这版排窑计划（${d.runs.length} 个窑次）？会以你的身份记账，并替代旧计划。`)) return;
  const body = {
    horizon_start: d.horizon_start, horizon_end: d.horizon_end, solver_status: d.solver.status, data_mode: d.data_mode,
    promise: [...document.querySelectorAll(".promise:checked")].map((x) => x.value),
    runs: d.runs.map((r) => ({ kiln: r.kiln, run_no: r.run_no, start_at: r.start_at, end_at: r.end_at,
      loads: r.loads.map((l) => ({ order: l.order, qty: l.qty })) })),
  };
  const r = await api("/v1/planning/plans", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!r.ok) return toast(await detail(r), 8000);
  const out = await r.json();
  toast(`已确认 ${out.plan_no}：承诺交期 ${out.promised.length} 个订单` + (out.shortages.length ? `，缺料 ${out.shortages.length} 种已通知采购` : ""), 6000);
  for (const w of out.warnings) toast(w, 6000);
  await loadCurrent();
}

// ---------------- 交期评估 ----------------
async function evaluate() {
  const order = $("#e-order").value.trim();
  const body = { data_mode: $("#mode").value };
  if (order) body.order = order;
  else body.candidate = { product: $("#e-product").value.trim(), qty: Number($("#e-qty").value), due_date: $("#e-due").value || null };
  const r = await api("/v1/planning/evaluate", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!r.ok) return toast(await detail(r));
  const e = await r.json();
  fill($("#eval"),
    el("p", { class: e.ok ? "ok" : "bad" }, e.verdict),
    e.impacts.length ? el("table", {},
      el("tr", {}, el("th", {}, "受影响订单"), el("th", {}, "客户"), el("th", {}, "交期"), el("th", {}, "可发货（原 → 新）")),
      e.impacts.map((i) => el("tr", {}, el("td", {}, i.order_no), el("td", {}, i.customer), el("td", {}, i.due_date || "-"),
        el("td", {}, `${i.ready_before || "-"} → ${i.ready_after || "排不下"}`)))) : null,
    e.shortages.length ? el("ul", { class: "bad" }, e.shortages.map((s) => el("li", {}, `缺 ${s.name} ${s.shortage} ${s.unit}（${s.short_from} 起）`))) : null,
    e.warnings.length ? el("ul", { class: "muted small" }, e.warnings.map((w) => el("li", {}, w))) : null);
}

// ---------------- 事件 ----------------
$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try { await login($("#key").value.trim()); $("#login-err").hidden = true; }
  catch (err) { $("#login-err").textContent = err.message; $("#login-err").hidden = false; }
});
$("#logout").addEventListener("click", logout);
$("#b-draft").addEventListener("click", () => draft().catch((e) => toast(e.message)));
$("#b-current").addEventListener("click", () => loadCurrent().catch((e) => toast(e.message)));
$("#b-confirm").addEventListener("click", () => confirmPlan().catch((e) => toast(e.message)));
$("#b-eval").addEventListener("click", () => evaluate().catch((e) => toast(e.message)));
$("#mode").addEventListener("change", () => loadCurrent().catch((e) => toast(e.message)));

const saved = store("get");
if (saved) login(saved).catch(() => { $("#login").hidden = false; });
else $("#login").hidden = false;
