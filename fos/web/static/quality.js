// 质量体系：待检任务、NCR / 8D、质保书、计量器具校准、ISO 9001 审核证据包。原生 JS，无构建步骤。
"use strict";

const $ = (s) => document.querySelector(s);
const KEY = "fos.apiKey";
const state = { key: "" };

function store(op, v) {
  try {
    if (op === "get") return localStorage.getItem(KEY) || "";
    if (op === "set") localStorage.setItem(KEY, v);
    if (op === "del") localStorage.removeItem(KEY);
  } catch (_) {}
  return "";
}
async function api(path, opts = {}) {
  const headers = Object.assign(state.key ? { Authorization: "Bearer " + state.key } : {}, opts.headers || {});
  const r = await fetch(path, Object.assign({ credentials: "same-origin" }, opts, { headers }));
  if (r.status === 401) { logout(); throw new Error("登录已失效，请重新输入 API Key"); }
  return r;
}
async function detail(r) {
  try { const j = await r.json(); return typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail); } catch (_) { return r.status + " " + r.statusText; }
}
function toast(msg, ms = 4000) {
  const t = $("#toast"); t.textContent = msg; t.hidden = false;
  clearTimeout(toast._t); toast._t = setTimeout(() => (t.hidden = true), ms);
}
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
const table = (heads, rows) => el("table", {}, el("tr", {}, heads.map((x) => el("th", {}, x))), rows);
const mode = () => $("#mode").value;
const q = (p) => p + (p.includes("?") ? "&" : "?") + "mode=" + encodeURIComponent(mode());
async function getJ(path) {
  const r = await api(q(path));
  if (!r.ok) { const e = new Error(await detail(r)); e.status = r.status; throw e; }
  return r.json();
}
function guardPanel(fn, boxSel) {
  return fn().catch((e) => {
    if (e.status === 403) fill($(boxSel), el("p", { class: "muted small" }, "当前账号无此模块查看权限。"));
    else toast(e.message, 6000);
  });
}

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
function logout() {
  store("del"); state.key = "";
  $("#app").hidden = true; $("#login").hidden = false; $("#logout").hidden = true; $("#who").textContent = "";
}
function refresh() {
  $("#mock-banner").hidden = mode() !== "mock";
  return Promise.all([
    guardPanel(loadPending, "#pending"),
    guardPanel(loadNcrs, "#ncrs"),
    guardPanel(loadInstruments, "#insts"),
    guardPanel(loadIso, "#iso"),
  ]);
}

async function loadPending() {
  const d = await getJ("/v1/quality/pending");
  const rows = [
    ...d.incoming.map((x) => el("tr", {}, el("td", {}, "来料"), el("td", {}, x.object_id), el("td", {}, x.name), el("td", {}, `${x.qty ?? ""} ${x.unit || ""}`))),
    ...d.final.map((x) => el("tr", {}, el("td", {}, "出厂"), el("td", {}, x.object_id), el("td", {}, x.name), el("td", {}, `${x.qty ?? ""} ${x.unit || ""}`))),
  ];
  fill($("#pending"), rows.length ? table(["类型", "编号", "物料/产品", "数量"], rows) : el("p", { class: "ok" }, "当前没有待检任务。"));
}

async function loadNcrs() {
  const d = await getJ("/v1/quality/ncrs");
  const rows = d.items.map((n) => {
    const btn = el("button", { class: "btn small" }, "8D 追溯草稿");
    btn.addEventListener("click", () => loadDraft8d(n.id).catch((e) => toast(e.message, 6000)));
    return el("tr", {},
      el("td", {}, n.ncr_no), el("td", {}, n.source_text),
      el("td", { class: n.severity === "critical" ? "bad" : n.severity === "major" ? "warn" : "" }, n.severity_text),
      el("td", {}, n.status_text), el("td", {}, n.disposition_text), el("td", {}, n.d8_text),
      el("td", { class: "small" }, n.summary || ""), el("td", {}, btn));
  });
  fill($("#ncrs"), rows.length ? table(["NCR编号", "来源", "严重度", "状态", "处置", "8D", "描述", ""], rows) : el("p", { class: "muted" }, "暂无不合格品报告。"));
}

async function loadDraft8d(id) {
  const r = await api(`/v1/quality/ncrs/${encodeURIComponent(id)}/8d-draft`);
  if (!r.ok) throw new Error(await detail(r));
  const d = await r.json();
  fill($("#d8-box"),
    el("h3", {}, `8D 追溯草稿：${d.ncr_no}`),
    el("p", {}, `D2 问题：${d.d2_problem}`),
    el("p", {}, `D3 建议围堵：${d.d3_containment}`),
    el("p", {}, `D4 追溯线索：${d.d4_suggested}`));
}

async function loadInstruments() {
  const d = await getJ("/v1/quality/instruments");
  const rows = d.items.map((x) => el("tr", {},
    el("td", {}, x.code), el("td", {}, x.name), el("td", {}, x.category_text),
    el("td", {}, x.location || "—"), el("td", {}, x.next_due_date || "—"),
    el("td", { class: x.cal_state === "overdue" ? "bad" : x.cal_state === "due_soon" ? "warn" : "ok" }, x.cal_text)));
  fill($("#insts"), rows.length ? table(["编号", "名称", "类别", "位置", "下次到期", "状态"], rows) : el("p", { class: "muted" }, "暂无计量器具记录。"));
}

async function loadIso() {
  const d = await getJ("/v1/quality/iso-audit");
  const k = d.kpis;
  fill($("#iso"), el("p", { class: "small muted" },
    `${d.period.start} 至 ${d.period.end}：检验 ${k.inspections_total} 次（来料合格率 ${k.iqc_pass_rate ?? "—"}%，出厂合格率 ${k.fqc_pass_rate ?? "—"}%），NCR ${k.ncr_closed}/${k.ncr_opened} 关闭，按期校准率 ${k.calibration_on_time_rate ?? "—"}%`));
}

async function downloadFile(path, fallback) {
  const r = await api(path);
  if (!r.ok) return toast(await detail(r), 6000);
  const url = URL.createObjectURL(await r.blob());
  const a = el("a", { href: url, download: fallback });
  document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 5000);
}

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try { await login($("#key").value.trim()); $("#login-err").hidden = true; }
  catch (err) { $("#login-err").textContent = err.message; $("#login-err").hidden = false; }
});
$("#logout").addEventListener("click", logout);
$("#b-refresh").addEventListener("click", refresh);
$("#mode").addEventListener("change", refresh);
$("#b-coa-docx").addEventListener("click", () => {
  const b = $("#coa-batch").value.trim();
  if (!b) return toast("请输入生产批号");
  downloadFile(`/v1/quality/batches/${encodeURIComponent(b)}/certificate/export?format=docx`, `质保书-${b}.docx`);
});
$("#b-coa-xlsx").addEventListener("click", () => {
  const b = $("#coa-batch").value.trim();
  if (!b) return toast("请输入生产批号");
  downloadFile(`/v1/quality/batches/${encodeURIComponent(b)}/certificate/export?format=xlsx`, `质保书-${b}.xlsx`);
});
$("#b-iso-xlsx").addEventListener("click", () => downloadFile("/v1/quality/iso-audit/export", "ISO审核证据包.xlsx"));
$("#b-remind").addEventListener("click", async () => {
  const r = await api(q("/v1/quality/instruments/remind"), { method: "POST" });
  if (!r.ok) return toast(await detail(r), 6000);
  const j = await r.json();
  toast(`已生成 ${j.count} 条计量器具校准提醒待办`);
});

const saved = store("get");
if (saved) login(saved).catch(() => { $("#login").hidden = false; });
else $("#login").hidden = false;
