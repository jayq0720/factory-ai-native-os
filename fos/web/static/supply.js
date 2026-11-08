// 采购仓储：实时库存、领料、请购建议 → 请购单 → 比价 → 采购单 → 到货、供应商评分。无构建步骤，原生 JS；只调用 /v1 接口，API Key 与其他页面共用。
"use strict";

const $ = (s) => document.querySelector(s);
const KEY = "fos.apiKey";
const state = { key: "", suggest: null };

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
const mode = () => $("#mode").value;
const q = (p) => p + (p.includes("?") ? "&" : "?") + "mode=" + mode();
const n = (x) => (x === null || x === undefined ? "" : String(+(+x).toFixed(3)));
const MANUAL = (note) => [{ kind: "manual", ref: "web:supply", note }];
async function getJ(path) { const r = await api(q(path)); if (!r.ok) throw new Error(await detail(r)); return r.json(); }
async function post(path, body) {
  const r = await api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!r.ok) throw new Error(await detail(r));
  return [r.status, await r.json()];
}
const table = (heads, rows) => el("table", {}, el("tr", {}, heads.map((x) => el("th", {}, x))), rows);
const wrap = (fn) => () => fn().catch((e) => toast(e.message, 8000));
function refresh() {
  $("#mock-banner").hidden = mode() !== "mock";
  return Promise.all([loadStock(), loadReqs(), loadPOs(), loadSup()]).catch((e) => toast(e.message));
}

// ---------------- 库存、领料 ----------------
async function loadStock() {
  const s = await getJ("/v1/stock");
  const rows = s.materials.map((m) => el("tr", {},
    el("td", {}, `${m.name}（${m.material}）`),
    el("td", { class: m.below_safety ? "bad" : "" }, `${n(m.usable)} ${m.unit}`),
    el("td", {}, n(m.incoming)), el("td", {}, n(m.blocked)), el("td", {}, n(m.safety_stock)),
    el("td", { class: "small" }, m.lots.map((l) => `${l.lot} ${l.status_text} ${n(l.on_hand)}${l.location ? " @" + l.location : ""}`).join("；"))));
  fill($("#stock"), table(["物料", "可用", "待检", "不可用", "安全库存", "批次（先进先出）"], rows),
    s.warnings.length ? el("ul", { class: "muted small" }, s.warnings.map((w) => el("li", {}, w))) : null);
}
async function issue() {
  const body = { qty: +$("#i-qty").value, data_mode: mode() };
  if ($("#i-lot").value.trim()) body.lot = $("#i-lot").value.trim(); else body.material = $("#i-material").value.trim();
  if ($("#i-batch").value.trim()) body.batch = $("#i-batch").value.trim();
  const [, out] = await post("/v1/stock/issues", body);
  toast(`已领料：${out.allocation.map((a) => `${a.lot} ${n(a.qty)}`).join("、")}` + (out.notified ? `；低于安全库存，已通知 ${out.notified} 人` : ""), 6000);
  loadStock();
}

// ---------------- 请购建议 → 请购单 ----------------
async function suggest() {
  const s = (state.suggest = await getJ("/v1/purchase/suggestions"));
  $("#b-raise").disabled = !s.suggestions.length;
  if (!s.suggestions.length) return fill($("#suggest"), el("p", { class: "ok" }, "现在没有要请购的。"));
  const rows = s.suggestions.map((x, i) => el("tr", {},
    el("td", {}, el("input", { type: "checkbox", "data-i": i, checked: "checked" })),
    el("td", {}, x.name), el("td", {}, el("input", { type: "number", step: "0.001", min: "0", value: x.qty, "data-q": i, style: "width:90px" }), " " + x.unit),
    el("td", {}, x.need_date || "尽快"), el("td", {}, x.suggested_supplier || "—"),
    el("td", { class: "small" }, x.reason, x.notes.length ? el("div", { class: "muted" }, x.notes.join("；")) : null)));
  fill($("#suggest"), table(["", "物料", "数量", "需要日期", "建议供应商", "依据"], rows), el("p", { class: "muted small" }, s.note));
}
async function raise() {
  const lines = [];
  for (const cb of document.querySelectorAll("#suggest input[type=checkbox]:checked")) {
    const x = state.suggest.suggestions[+cb.dataset.i];
    const qty = +document.querySelector(`#suggest input[data-q="${cb.dataset.i}"]`).value;
    lines.push({ material: x.material, qty, need_date: x.need_date, source: x.source, reason: x.reason, suggested_supplier: x.suggested_supplier });
  }
  if (!lines.length) return toast("没有勾选");
  const [, out] = await post("/v1/purchase/requisitions", { lines, data_mode: mode() });
  toast(`已生成 ${out.events.length} 张请购单`);
  suggest(); loadReqs();
}
async function loadReqs() {
  const s = await getJ("/v1/purchase/requisitions?open_only=true");
  const rows = s.requisitions.map((r) => el("tr", {},
    el("td", {}, r.req_no), el("td", {}, r.material), el("td", {}, `${n(r.qty)} ${r.unit}`), el("td", {}, r.need_date || ""),
    el("td", {}, r.stage_text),
    el("td", {}, r.stage === "open" ? el("button", { class: "btn small", "data-req": r.id }, "比价下单") : "")));
  fill($("#reqs"), rows.length ? table(["请购单", "物料", "数量", "需要日期", "状态", ""], rows) : el("p", { class: "muted" }, "没有待下单的请购单。"));
  for (const b of document.querySelectorAll("#reqs button[data-req]")) b.addEventListener("click", wrap(() => compare(s.requisitions.find((r) => r.id === b.dataset.req))));
}
async function compare(req) {
  const c = await getJ(`/v1/purchase/compare?material=${encodeURIComponent(req.material)}&qty=${req.qty}` + (req.need_date ? `&need_date=${req.need_date}` : ""));
  const rec = c.quotes.find((x) => x.quote === c.recommended);
  const rows = c.quotes.map((x) => el("tr", { class: x.quote === c.recommended ? "ok" : "" },
    el("td", {}, x.supplier_name), el("td", {}, `${n(x.unit_price)} 元/${x.unit}`), el("td", {}, n(x.amount)),
    el("td", {}, x.eta || "未写周期"), el("td", {}, x.meets_need_date === false ? "赶不上" : x.meets_need_date ? "来得及" : ""),
    el("td", {}, x.score === null ? "—" : n(x.score))));
  const sup = el("input", { id: "o-sup", placeholder: "供应商 id", value: (rec && rec.supplier) || req.suggested_supplier || "" });
  const price = el("input", { id: "o-price", type: "number", step: "0.01", min: "0", placeholder: "单价", value: rec ? rec.unit_price : "" });
  const qty = el("input", { id: "o-qty", type: "number", step: "0.001", min: "0", value: req.qty });
  const due = el("input", { id: "o-due", type: "date" });
  const go = el("button", { class: "btn primary small" }, "下采购单");
  go.addEventListener("click", wrap(async () => {
    const body = { supplier: sup.value.trim(), material: req.material, qty: +qty.value, unit_price: +price.value, requisitions: [req.id], data_mode: mode() };
    if (rec && rec.supplier === body.supplier) body.quote = rec.quote;
    if (due.value) body.due_date = due.value;
    const [st, out] = await post("/v1/purchase/orders", body);
    toast(st === 202 ? `${out.formula}，${out.message}` : `已下单 ${out.object_id}：${out.formula}`, 8000);
    for (const w of out.warnings || []) toast(w, 6000);
    fill($("#compare")); loadReqs(); loadPOs();
  }));
  fill($("#compare"), el("h3", {}, `比价：${c.name} ${n(req.qty)} ${c.unit}`),
    c.quotes.length ? table(["供应商", "单价", "金额", "预计到货", "需要日期", "评分"], rows) : null,
    el("p", { class: "muted small" }, [c.reason, ...c.notes].filter(Boolean).join("；")),
    el("div", { class: "row" }, sup, price, qty, due, go));
}

// ---------------- 采购单、到货 ----------------
async function loadPOs() {
  const s = await getJ("/v1/purchase/orders?status=open");
  const rows = s.orders.map((o) => el("tr", {},
    el("td", {}, o.po_no), el("td", {}, o.supplier), el("td", {}, o.material), el("td", {}, `${n(o.qty)} ${o.unit}`),
    el("td", {}, n(o.amount)), el("td", { class: o.overdue ? "bad" : "" }, o.due_date || ""),
    el("td", {}, `${n(o.received)} / 未到 ${n(o.remaining)}`),
    el("td", {}, o.remaining > 0 ? el("button", { class: "btn small", "data-po": o.id }, "登记到货") : "")));
  fill($("#pos"), rows.length ? table(["采购单", "供应商", "物料", "数量", "金额", "交期", "到货", ""], rows) : el("p", { class: "muted" }, "没有未关闭的采购单。"));
  for (const b of document.querySelectorAll("#pos button[data-po]")) b.addEventListener("click", wrap(() => receive(s.orders.find((o) => o.id === b.dataset.po))));
}
async function receive(o) {
  const qty = prompt(`采购单 ${o.po_no} 本次到货数量（${o.unit}），未到 ${n(o.remaining)}`, n(o.remaining));
  if (!qty) return;
  const lot = prompt("供方批号（可空）") || undefined;
  const loc = prompt("库位（可空）") || undefined;
  const [, out] = await post(`/v1/purchase/orders/${encodeURIComponent(o.id)}/receipts`,
    { qty: +qty, supplier_lot_no: lot, location: loc, evidence: MANUAL("仓管网页确认到货（送货单另行拍照留存）") });
  toast(`已入库批次 ${out.object_id}（待检）。${out.notes.join("；")}`, 6000);
  loadPOs(); loadStock();
}
async function loadSup() {
  const s = await getJ("/v1/purchase/suppliers");
  const rows = s.suppliers.map((x) => el("tr", {},
    el("td", {}, x.name), el("td", {}, x.lots), el("td", {}, x.quality_rate === null ? "—" : x.quality_rate + "%"),
    el("td", {}, x.on_time_rate === null ? "—" : `${x.on_time_rate}%（${x.on_time}/${x.timed}）`), el("td", {}, x.score === null ? "—" : n(x.score))));
  fill($("#sup"), table(["供应商", `近 ${s.window_days} 天到货批次`, "来料合格率", "准时率", "总分"], rows), el("p", { class: "muted small" }, s.formula));
}

// ---------------- 事件 ----------------
$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try { await login($("#key").value.trim()); $("#login-err").hidden = true; }
  catch (err) { $("#login-err").textContent = err.message; $("#login-err").hidden = false; }
});
$("#logout").addEventListener("click", logout);
$("#mode").addEventListener("change", refresh);
$("#b-stock").addEventListener("click", wrap(loadStock));
$("#b-issue").addEventListener("click", wrap(issue));
$("#b-suggest").addEventListener("click", wrap(suggest));
$("#b-raise").addEventListener("click", wrap(raise));
$("#b-reqs").addEventListener("click", wrap(loadReqs));
$("#b-pos").addEventListener("click", wrap(loadPOs));
$("#b-sup").addEventListener("click", wrap(loadSup));

const saved = store("get");
if (saved) login(saved).catch(() => { $("#login").hidden = false; });
else $("#login").hidden = false;
