// 试点上线：主数据导入 + 生产日报。无构建步骤，原生 JS；只调用 /v1 接口，API Key 与其他页面共用。
"use strict";

const $ = (s) => document.querySelector(s);
const KEY = "fos.apiKey";
const STATUS = { created: ["新增", "ok"], exists: ["已存在", "muted"], error: ["错误", "bad"] };
const state = { key: "", previewOk: false };

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
  $("#day").value = new Date(Date.now() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 10);
  loadDaily();
}
function logout() { store("del"); state.key = ""; $("#app").hidden = true; $("#login").hidden = false; $("#logout").hidden = true; $("#who").textContent = ""; }

// ---------------- 主数据导入 ----------------
async function template() {
  const r = await api("/v1/import/template");
  if (!r.ok) return toast(await detail(r));
  const url = URL.createObjectURL(await r.blob());
  const a = el("a", { href: url, download: "主数据导入模板.xlsx" });
  document.body.append(a); a.click(); a.remove(); URL.revokeObjectURL(url);
}
async function upload(commit) {
  const f = $("#file").files[0];
  if (!f) return toast("先选择填好的模板文件");
  const fd = new FormData(); fd.append("file", f);
  const btn = commit ? $("#b-commit") : $("#b-preview");
  btn.disabled = true;
  try {
    const r = await api(commit ? "/v1/import/commit" : "/v1/import/preview", { method: "POST", body: fd });
    if (r.status !== 200 && r.status !== 201 && !(commit && r.status === 422)) return toast(await detail(r));
    const rep = await r.json();
    if (r.status === 422 && !rep.sheets) return toast(await detail(r));
    render(rep);
    state.previewOk = rep.ok && !rep.committed;
    if (rep.committed) toast(`已入账：新增 ${rep.counts.created} 条，跳过已存在 ${rep.counts.exists} 条`);
  } finally {
    btn.disabled = false;
    $("#b-commit").disabled = !state.previewOk;
  }
}
function render(rep) {
  const c = rep.counts;
  const head = rep.committed ? "已入账" : rep.ok ? "预览通过（还没有写入）" : "有错误，改好后重新预览";
  const kids = [
    el("p", { class: rep.ok ? "ok" : "bad" }, `${head}：新增 ${c.created}，已存在 ${c.exists}，错误 ${c.error}`),
    rep.warnings.length ? el("ul", { class: "muted small" }, rep.warnings.map((w) => el("li", {}, w))) : null,
  ];
  for (const s of rep.sheets) {
    if (!s.rows.length && !s.errors.length) continue;
    kids.push(el("h3", {}, `${s.name}（新增 ${s.counts.created}，已存在 ${s.counts.exists}，错误 ${s.counts.error}）`));
    for (const e of s.errors) kids.push(el("p", { class: "bad" }, e));
    if (!s.rows.length) continue;
    kids.push(el("table", {},
      el("tr", {}, el("th", {}, "行"), el("th", {}, "结果"), el("th", {}, "名称 / 编号"), el("th", {}, "说明")),
      s.rows.map((r) => el("tr", {},
        el("td", {}, r.row), el("td", { class: STATUS[r.status][1] }, STATUS[r.status][0]),
        el("td", {}, r.label || r.object_id || ""), el("td", {}, r.message || "")))));
  }
  fill($("#imp"), kids);
}

// ---------------- 生产日报 ----------------
async function loadDaily() {
  const r = await api("/v1/reports/daily?date=" + encodeURIComponent($("#day").value));
  if (!r.ok) return toast(await detail(r));
  const rep = await r.json();
  $("#mock-banner").hidden = rep.data_mode !== "mock";
  fill($("#daily"),
    el("p", { class: "muted small" }, `${rep.date}，共 ${rep.events} 条记录，统计耗时 ${rep.elapsed_ms} ms`),
    el("ul", { class: "lines" }, rep.lines.map((ln) => el("li", { class: ln.startsWith("⚠") ? "bad" : "" }, ln))));
}

// ---------------- 事件 ----------------
$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try { await login($("#key").value.trim()); $("#login-err").hidden = true; }
  catch (err) { $("#login-err").textContent = err.message; $("#login-err").hidden = false; }
});
$("#logout").addEventListener("click", logout);
$("#b-tpl").addEventListener("click", () => template().catch((e) => toast(e.message)));
$("#b-preview").addEventListener("click", () => upload(false).catch((e) => toast(e.message)));
$("#b-commit").addEventListener("click", () => upload(true).catch((e) => toast(e.message)));
$("#file").addEventListener("change", () => { state.previewOk = false; $("#b-commit").disabled = true; fill($("#imp")); });
$("#b-daily").addEventListener("click", () => loadDaily().catch((e) => toast(e.message)));

const saved = store("get");
if (saved) login(saved).catch(() => { $("#login").hidden = false; });
else $("#login").hidden = false;
