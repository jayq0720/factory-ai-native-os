// 招标拆解：登记 → 需求矩阵 / 偏离表 / 资料清单 / 评分点 → 转工程项目 → 提交投标。无构建步骤，原生 JS；只调用 /v1 接口，API Key 与其他页面共用。
"use strict";

const $ = (s) => document.querySelector(s);
const KEY = "fos.apiKey";
const state = { key: "", tender: null, analysis: null };
const LEVEL = { star: "★", key: "▲", must: "必须", normal: "" };
const STATUS = { open: "未处理", responded: "已响应", accepted: "接受偏离", na: "不适用" };

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
const table = (heads, rows) => el("table", {}, el("tr", {}, heads.map((x) => el("th", {}, x))), rows);
const wrap = (fn) => () => fn().catch((e) => toast(e.message, 8000));
async function getJ(path) { const r = await api(path); if (!r.ok) throw new Error(await detail(r)); return r.json(); }
async function send(method, path, body) {
  const r = await api(path, { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
  if (!r.ok) throw new Error(await detail(r));
  return [r.status, await r.json()];
}
const fmt = (iso) => (iso ? new Date(iso).toLocaleString("zh-CN", { hour12: false }).slice(0, 16) : "—");
const left = (h) => (h === undefined || h === null ? "" : h <= 0 ? "已截止" : h >= 48 ? `还有 ${(h / 24).toFixed(1)} 天` : `还有 ${h.toFixed(0)} 小时`);

async function login(key) {
  state.key = key;
  const r = await api("/v1/me");
  if (!r.ok) throw new Error(await detail(r));
  const me = await r.json();
  store("set", key);
  $("#login").hidden = true; $("#app").hidden = false; $("#logout").hidden = false;
  $("#who").textContent = `${me.tenant_name} · ${me.display_name}`;
  loadList();
}
function logout() { store("del"); state.key = ""; $("#app").hidden = true; $("#login").hidden = false; $("#logout").hidden = true; $("#who").textContent = ""; }

// ---------------- 列表、登记 ----------------
async function loadList() {
  const s = await getJ("/v1/tenders");
  const rows = s.items.map((t) => {
    const sm = (t.analysis || {}).summary || {};
    const tr = el("tr", { class: "pick" },
      el("td", {}, t.id), el("td", {}, t.name, t.data_mode === "mock" ? el("span", { class: "warn small" }, "（模拟）") : null),
      el("td", {}, fmt(t.bid_deadline)), el("td", { class: t.hours_left < 72 ? "bad" : "" }, t.status === "open" ? left(t.hours_left) : ""),
      el("td", {}, t.status_name),
      el("td", { class: sm.star_open ? "bad" : "" }, sm.star === undefined ? "" : `${sm.star_open}/${sm.star}`),
      el("td", { class: sm.docs_missing ? "bad" : "" }, sm.docs_missing ?? ""));
    tr.addEventListener("click", wrap(() => open(t.id)));
    return tr;
  });
  fill($("#list"), rows.length ? table(["编号", "项目", "投标截止", "剩余", "状态", "★未处理", "缺资料"], rows) : el("p", { class: "muted" }, "还没有招标项目。"));
}
async function create() {
  const body = { document_id: $("#doc-id").value.trim() };
  if ($("#owner").value.trim()) body.owner = $("#owner").value.trim();
  const [st, out] = await send("POST", "/v1/tenders", body);
  toast(st === 200 ? `这份文件已登记过：${out.tender.id}` : `已登记 ${out.tender.id}，拆出 ${out.analysis.summary.total} 条需求`);
  loadList();
  open(out.tender.id);
}

// ---------------- 详情 ----------------
async function open(id) {
  const d = await getJ(`/v1/tenders/${id}`);
  state.tender = d.tender; state.analysis = d.analysis;
  const t = d.tender, a = d.analysis;
  $("#detail").hidden = false;
  $("#mock-banner").hidden = !(t.data_mode === "mock" || (a && a.data_mode === "mock"));
  $("#d-title").textContent = `${t.name}（${t.id}）· ${t.status_name}`;
  const kv = [
    ["招标编号", t.tender_no], ["招标人", t.purchaser], ["代理机构", t.agency],
    ["最高限价", t.budget ? `${(t.budget / 10000).toLocaleString()} 万元` : ""], ["投标保证金", t.bid_bond ? `${t.bid_bond} 元` : ""],
    ["澄清截止", fmt(t.clarification_deadline)], ["投标截止", `${fmt(t.bid_deadline)} ${t.status === "open" ? left(t.hours_left) : ""}`],
    ["开标", fmt(t.opening_time)], ["评标办法", t.evaluation_method], ["交货期", t.delivery_period], ["质保期", t.warranty],
    ["负责人", t.owner], ["工程项目", t.eng_project ? el("a", { href: "eng.html" }, t.eng_project) : "未转"],
    ["提醒", d.reminders.filter((x) => x.status === "open").map((x) => `${x.title.split("：")[0]} ${fmt(x.due_at)}`).join("；") || "—"],
  ];
  fill($("#d-info"), kv.filter(([, v]) => v).map(([k, v]) => el("div", {}, el("span", { class: "muted" }, k + "："), v)));
  $("#b-export").href = "#";
  if (!a) { fill($("#items")); return; }
  fill($("#d-risks"), a.risks.length
    ? el("ul", { class: "lines" }, a.risks.map((r) => el("li", { class: r.level === "block" ? "bad" : "warn" }, `【${r.type}】`, r.page ? `第 ${r.page} 页 ` : "", r.text)))
    : el("p", { class: "ok" }, "没有发现风险。"));
  render();
}

function render() {
  const a = state.analysis;
  const s = a.summary;
  $("#summary").textContent = `拆解 v${a.version}${a.stale ? "（招标信息已改，请重新拆解）" : ""}：${s.total} 条，★ ${s.star}（未处理 ${s.star_open}），必须处理的未处理 ${s.must_open}，负偏离 ${s.negative}，资料缺 ${s.docs_missing}/${s.docs_total}`;
  const f = $("#filter").value;
  const show = a.items.filter((i) =>
    f === "all" ? true
      : f === "must" ? i.level !== "normal" || i.disqualify
      : f === "neg" ? i.deviation && ["负偏离", "待确认"].includes(i.deviation.status)
      : i.status === "open");
  const editable = a.status === "draft";
  const rows = show.map((i) => {
    const sel = el("select", { "data-id": i.id }, Object.entries(STATUS).map(([k, v]) => el("option", k === i.status ? { value: k, selected: "selected" } : { value: k }, v)));
    const resp = el("textarea", { "data-id": i.id }, i.response);
    if (!editable) { sel.disabled = true; resp.disabled = true; }
    sel.addEventListener("change", wrap(() => patchItem(i.id, { status: sel.value, response: resp.value })));
    resp.addEventListener("change", wrap(() => patchItem(i.id, { response: resp.value })));
    const dev = i.deviation;
    return el("tr", {},
      el("td", {}, i.no), el("td", {}, i.page), el("td", { class: "small" }, `${i.section_name}·${i.category}`),
      el("td", { class: i.level === "star" ? "star" : "" }, LEVEL[i.level], i.disqualify ? el("div", { class: "bad small" }, "可能废标") : null),
      el("td", { class: "small", style: "max-width:380px" }, i.text),
      el("td", { class: dev && dev.status === "负偏离" ? "bad" : dev && dev.status === "待确认" ? "warn" : "ok" }, dev ? dev.status : ""),
      el("td", { style: "min-width:220px" }, resp), el("td", {}, sel),
      el("td", { class: "small muted" }, i.assignee || i.owner_role || "", i.by_name ? el("div", {}, `${i.by_name} 处理`) : null));
  });
  fill($("#items"), table(["条款", "页", "章节·类别", "级别", "原文", "偏离", "响应", "状态", "负责"], rows));

  fill($("#devs"), a.deviations.length ? table(["条款", "指标", "招标要求", "我方依据", "偏离"], a.deviations.map((d) => el("tr", {},
    el("td", {}, d.no), el("td", {}, d.name), el("td", {}, d.required), el("td", { class: "small" }, d.basis),
    el("td", { class: d.status === "负偏离" ? "bad" : d.status === "待确认" ? "warn" : "ok" }, d.status))))
    : el("p", { class: "muted" }, "没有可比较的指标。"));

  fill($("#docs"), table(["资料", "页", "要求", "资料库", "状态", ""], a.checklist.map((d) => {
    const req = [d.max_age_months != null ? `${d.max_age_months} 个月内` : "", d.min_count ? `不少于 ${d.min_count} 份` : ""].filter(Boolean).join("，");
    const bad = ["missing", "expired", "insufficient"].includes(d.status) && !d.disposition;
    const btn = bad && editable ? el("button", { class: "btn small" }, "已线下备齐") : null;
    if (btn) btn.addEventListener("click", wrap(() => patchDoc(d.id, "prepared")));
    return el("tr", {},
      el("td", { class: d.star ? "star" : "" }, (d.star ? "★ " : "") + d.name), el("td", {}, d.sources[0] ? d.sources[0].page : ""),
      el("td", {}, req), el("td", { class: "small" }, d.matches.map((m) => `${m.name}${m.valid_until ? "（至 " + m.valid_until + "）" : ""}${m.problem ? " ⚠" + m.problem : ""}`).join("；")),
      el("td", { class: bad ? "bad" : d.status === "ok" ? "ok" : "" }, d.disposition ? (d.disposition === "prepared" ? "已备齐" : "不适用") : d.status_text),
      el("td", {}, btn));
  })));

  fill($("#score"), a.scoring.length ? table(["得分项", "分值", "页", "需要的证明材料"], a.scoring.map((s) => el("tr", {},
    el("td", {}, s.item), el("td", {}, s.points), el("td", {}, s.page),
    el("td", {}, s.evidence.map((e) => (a.checklist.find((d) => d.id === e) || { name: e }).name).join("、")))))
    : el("p", { class: "muted" }, "没有识别到评分点。"));
  fill($("#sections"), a.sections.map((x) => `第 ${x.page} 页 ${x.title}（${x.name}）`).join(" → ") || "没有识别到章节标题");
}

async function patchItem(id, body) {
  const [, out] = await send("PATCH", `/v1/tender-analyses/${state.analysis.id}/items/${id}`, body);
  const i = state.analysis.items.findIndex((x) => x.id === id);
  state.analysis.items[i] = out.item; state.analysis.summary = out.summary;
  render();
}
async function patchDoc(id, disposition) {
  const [, out] = await send("PATCH", `/v1/tender-analyses/${state.analysis.id}/documents/${id}`, { disposition, note: "网页标记" });
  const i = state.analysis.checklist.findIndex((x) => x.id === id);
  state.analysis.checklist[i] = out.document; state.analysis.summary = out.summary;
  render();
}
async function analyze() { await send("POST", `/v1/tenders/${state.tender.id}/analyze`); toast("已重新拆解"); open(state.tender.id); }
async function toEng() {
  const [st, out] = await send("POST", `/v1/tenders/${state.tender.id}/eng-project`);
  toast(st === 201 ? `已建工程项目 ${out.eng_project}，到“工程方案”页匹配选材` : `已关联工程项目 ${out.eng_project}`, 6000);
  open(state.tender.id);
}
async function exportXlsx() {
  const r = await api(`/v1/tender-analyses/${state.analysis.id}/export`);
  if (!r.ok) throw new Error(await detail(r));
  const url = URL.createObjectURL(await r.blob());
  const a = el("a", { href: url, download: `招标拆解-${state.tender.tender_no || state.tender.id}.xlsx` });
  document.body.append(a); a.click(); a.remove(); URL.revokeObjectURL(url);
}
async function submit() {
  const body = $("#bid-amount").value ? { bid_amount: +$("#bid-amount").value } : {};
  const [st, out] = await send("POST", `/v1/tender-analyses/${state.analysis.id}/submit`, body);
  toast(st === 202 ? out.message : "已提交投标", 8000);
  open(state.tender.id); loadList();
}
async function close() {
  await send("POST", `/v1/tenders/${state.tender.id}/close`, { status: $("#close-status").value });
  toast("已登记结果"); open(state.tender.id); loadList();
}

$("#login-form").addEventListener("submit", (e) => {
  e.preventDefault();
  login($("#key").value.trim()).catch((err) => { const p = $("#login-err"); p.textContent = err.message; p.hidden = false; });
});
$("#logout").addEventListener("click", logout);
$("#b-list").addEventListener("click", wrap(loadList));
$("#b-create").addEventListener("click", wrap(create));
$("#b-analyze").addEventListener("click", wrap(analyze));
$("#b-eng").addEventListener("click", wrap(toEng));
$("#b-export").addEventListener("click", (e) => { e.preventDefault(); wrap(exportXlsx)(); });
$("#b-submit").addEventListener("click", wrap(submit));
$("#b-close").addEventListener("click", wrap(close));
$("#filter").addEventListener("change", () => state.analysis && render());
const saved = store("get");
if (saved) login(saved).catch(() => logout()); else $("#login").hidden = false;
