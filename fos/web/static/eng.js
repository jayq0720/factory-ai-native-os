// 工程方案控制台。无构建步骤，原生 JS；只调用 /v1 接口，API Key 与文档解析页共用（localStorage）。
// 所有数字由后端确定性代码计算，这里只展示和提交修改。
"use strict";

const $ = (s) => document.querySelector(s);
const KEY = "fos.apiKey";
const SEV = { high: ["高", "bad"], medium: ["中", "warn"], low: ["低", ""] };
const TYPE = { missing: "缺失", conflict: "冲突", unchecked: "未核对", material: "选材", thermal: "传热", commercial: "商务", data: "数据" };
const DISP = { resolved: "已处理", accepted: "接受风险", na: "不适用" };
const DSTATUS = { draft: ["草稿", ""], submitted: ["审批中", "warn"], published: ["已发布", "ok"], superseded: ["旧版本", ""] };
const COND = [
  ["name", "项目名称", "text"], ["customer_name", "客户", "text"], ["furnace_type", "炉型", "text"],
  ["positions", "部位", "text"], ["normal_temp", "正常工作温度 ℃", "number"], ["max_temp", "最高温度 ℃", "number"],
  ["atmosphere", "气氛", "text"], ["shell_temp", "外壁温度要求 ℃", "number"], ["ambient_temp", "环境温度 ℃", "number"],
  ["area", "衬里面积 m²", "number"], ["thickness_limit", "总厚度要求 mm", "number"], ["lining_spec", "客户要求的衬里", "text"],
  ["standards", "执行标准", "text"], ["tax_rate", "税率 %", "number"], ["payment_terms", "付款方式", "text"],
];
const state = { key: "", me: null, projects: [], cur: null, design: null, kb: [] };

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
  if (opts.json !== undefined) { headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(opts.json); }
  const r = await fetch(path, Object.assign({}, opts, { headers }));
  if (r.status === 401) { logout(); throw new Error("登录已失效，请重新输入 API Key"); }
  if (!r.ok) {
    let msg = r.status + " " + r.statusText;
    try { const j = await r.json(); msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail); } catch (_) {}
    throw new Error(msg);
  }
  return r;
}
const getJSON = async (p, o) => (await api(p, o)).json();
function toast(msg, ms = 3000) { const t = $("#toast"); t.textContent = msg; t.hidden = false; clearTimeout(toast._t); toast._t = setTimeout(() => (t.hidden = true), ms); }
function el(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (k === "value") e.value = v;
    else if (v !== null && v !== undefined && v !== false) e.setAttribute(k, v === true ? "" : v);
  }
  for (const k of kids.flat()) if (k !== null && k !== undefined && k !== false) e.append(k.nodeType ? k : String(k));
  return e;
}
const chip = (t, c = "") => el("span", { class: "chip " + c }, t);
const fill = (n, ...k) => n.replaceChildren(...k.flat().filter((x) => x !== null && x !== undefined && x !== false));
const yuan = (v) => (v === null || v === undefined ? "—" : Number(v).toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 }));
const fmt = (v, d = 1) => (v === null || v === undefined ? "—" : Number(v).toFixed(d));
const busy = async (btn, fn) => { if (btn) btn.disabled = true; try { await fn(); } catch (e) { toast(e.message, 5000); } finally { if (btn) btn.disabled = false; } };

// ---------------- 登录 ----------------
async function login(key) {
  state.key = key;
  state.me = await getJSON("/v1/me");
  store("set", key);
  $("#login").hidden = true; $("#app").hidden = false; $("#logout").hidden = false;
  $("#who").textContent = `${state.me.tenant_name} · ${state.me.display_name}`;
  try { state.kb = (await getJSON("/v1/kb/products")).items; } catch (_) { state.kb = []; }
  await Promise.all([loadProjects(), loadDocs()]);
  const id = location.hash.slice(1);
  if (id) openProject(id).catch(() => {});
}
function logout() { store("del"); state.key = ""; $("#app").hidden = true; $("#login").hidden = false; $("#logout").hidden = true; $("#who").textContent = ""; }

async function loadDocs() {
  const sel = $("#doc-pick");
  try {
    const r = await getJSON("/v1/documents?kind=tech&limit=100");
    fill(sel, el("option", { value: "" }, "选择已解析的技术文件…"),
      r.items.filter((d) => d.status === "parsed").map((d) => el("option", { value: d.id }, `${d.filename}${d.data_mode === "mock" ? "（模拟）" : ""}`)));
  } catch (_) { /* 没有文档读权限 */ }
}

async function loadProjects() {
  const r = await getJSON("/v1/eng/projects");
  state.projects = r.items;
  $("#p-count").textContent = `${r.items.length} 个`;
  fill($("#projects"), r.items.map((p) => el("li", { class: state.cur && state.cur.project.id === p.id ? "on" : "", onclick: () => openProject(p.id) },
    el("div", { class: "name" }, p.name),
    el("div", { class: "meta" }, chip(p.stage_name, p.stage === "quoted" ? "ok" : p.stage === "reviewing" ? "warn" : "info"),
      p.data_mode === "mock" ? chip("模拟", "mock") : null, chip(p.id), p.quote_amount ? chip("¥" + yuan(p.quote_amount)) : null))));
}

// ---------------- 项目 ----------------
async function openProject(id, designId) {
  state.cur = await getJSON(`/v1/eng/projects/${encodeURIComponent(id)}`);
  location.hash = id;
  $("#empty").hidden = true; $("#proj").hidden = false;
  loadProjects();
  const ds = state.cur.designs;
  const pick = designId || (state.design && ds.find((d) => d.id === state.design.id) ? state.design.id : ds[0] && ds[0].id);
  state.design = pick ? await getJSON(`/v1/eng/designs/${pick}`) : null;
  render();
}

function render() {
  const { project: p, designs, stage_name } = state.cur;
  const mock = p.data_mode === "mock" || (state.design && state.design.data_mode === "mock");
  $("#mock-banner").hidden = !mock;
  const head = el("div", { class: "eng-head" },
    el("div", {}, el("h2", {}, p.name), el("div", { class: "meta" }, chip(stage_name, "info"), chip(p.id),
      p.quote_no ? chip(`报价 ${p.quote_no} · ¥${yuan(p.quote_amount)}`, "ok") : null, chip(`工况 v${p.version}`))),
    el("div", { class: "actions" }, el("button", { class: "btn primary", onclick: (e) => busy(e.target, doMatch) }, designs.length ? "重新匹配方案" : "匹配方案")));
  fill($("#proj"), head, el("div", { class: "eng-grid" }, condSection(p), historySection()),
    designs.length ? designTabs(designs) : el("div", { class: "sec muted" }, "还没有方案。点“匹配方案”，系统按衬里模板和硬约束给出备选。"),
    state.design ? designView(state.design) : null);
}

function condSection(p) {
  const inputs = {};
  const rows = COND.map(([k, label, type]) => {
    const inp = el("input", { type, step: "any", value: p[k] ?? "" }); inputs[k] = inp;
    return [el("span", { class: "k" }, label), inp];
  });
  const save = async (e) => {
    const body = {};
    for (const [k, , type] of COND) {
      const v = inputs[k].value.trim(); const old = p[k] ?? "";
      if (String(v) === String(old)) continue;
      body[k] = v === "" ? null : type === "number" ? Number(v) : v;
    }
    if (!Object.keys(body).length) return toast("没有修改");
    body.note = prompt("修改原因（会记在账本里）", "客户补充信息") || "人工修改工况";
    await busy(e.target, async () => { await getJSON(`/v1/eng/projects/${p.id}`, { method: "PATCH", json: body }); toast("工况已更新，方案需要重新计算"); await openProject(p.id); });
  };
  return el("div", { class: "sec" }, el("h3", {}, "工况", el("span", { class: "muted small" }, "来自技术文件，修改记为账本事件")),
    el("div", { class: "kv" }, rows.flat()), el("div", { class: "opts" }, el("button", { class: "btn small", onclick: save }, "保存工况")));
}

function historySection() {
  const evs = state.cur.history;
  const first = evs[0];
  const cites = first ? first.evidence.filter((e) => e.kind === "document") : [];
  return el("div", { class: "sec" }, el("h3", {}, "证据与记录"),
    el("ul", { class: "checks" }, evs.map((e) => el("li", {}, `#${e.seq} ${e.type} · ${e.actor} · ${e.at.slice(0, 16).replace("T", " ")}`))),
    cites.length ? el("details", {}, el("summary", { class: "small muted" }, `原文出处 ${cites.length} 条`),
      el("ul", { class: "checks" }, cites.map((c) => el("li", {}, `${c.note}：第 ${c.page} 页「${c.quote || ""}」`)))) : null);
}

function designTabs(designs) {
  return el("div", { class: "dtabs" }, designs.map((d) => {
    const st = DSTATUS[d.status] || [d.status, ""];
    return el("button", { class: "dtab" + (state.design && state.design.id === d.id ? " on" : ""), onclick: () => openProject(state.cur.project.id, d.id) },
      el("div", { class: "t" }, `${d.label} · ${d.title}`),
      el("div", { class: "m" }, `v${d.version} · ${d.total_thickness_mm} mm · 外壁 ${fmt(d.shell_temp)}℃`),
      el("div", { class: "m" }, `¥${yuan(d.total)} · 毛利 ${d.gross_margin_pct ?? "—"}%`),
      el("div", { class: "meta" }, chip(st[0], st[1]), d.feasible ? chip("满足约束", "ok") : chip("不满足约束", "bad"), d.stale ? chip("工况已变", "warn") : null,
        d.review_summary.open ? chip(`待处理 ${d.review_summary.open}`, "warn") : null));
  }));
}

// ---------------- 方案 ----------------
function designView(d) {
  const editable = d.status === "draft";
  const data = d.data;
  // 衬里层：可改厚度、换产品
  const rows = d.inputs.layers.map((ly) => ({ ...ly }));
  const layerTable = el("table", { class: "tbl" }, el("tr", {}, el("th", {}, "层"), el("th", {}, "产品"), el("th", { class: "num" }, "厚度 mm"), el("th", { class: "num" }, "热面温度 ℃"), el("th", { class: "num" }, "分类温度 ℃")),
    rows.map((ly, i) => {
      const prod = state.kb.find((p) => p.id === ly.product_id);
      const t = (data.heat_max && data.heat_max.interface_temps[i]) ?? null;
      const sel = el("select", { onchange: (e) => (ly.product_id = e.target.value), disabled: !editable },
        (state.kb.length ? state.kb : [{ id: ly.product_id, code: ly.product_id, name: "" }]).filter((p) => p.thermal_ready || p.id === ly.product_id)
          .map((p) => { const o = el("option", { value: p.id }, `${p.code} ${p.name}`); if (p.id === ly.product_id) o.selected = true; return o; }));
      const th = el("input", { type: "number", step: "1", min: "0", value: ly.thickness_mm, disabled: !editable, onchange: (e) => (ly.thickness_mm = Number(e.target.value)) });
      return el("tr", {}, el("td", {}, ly.role), el("td", {}, sel), el("td", { class: "num" }, th), el("td", { class: "num" }, fmt(t, 0)), el("td", { class: "num" }, prod ? prod.classification_temp : "—"));
    }),
    el("tr", { class: "sum" }, el("td", {}, "合计"), el("td"), el("td", { class: "num" }, data.total_thickness_mm), el("td"), el("td")));
  const o = d.inputs.options;
  const optIn = {};
  const opt = (k, label) => { optIn[k] = el("input", { type: "number", step: "any", value: o[k], disabled: !editable }); return el("label", {}, label + " ", optIn[k]); };
  const recalc = (e) => busy(e.target, async () => {
    const options = {}; for (const k in optIn) options[k] = Number(optIn[k].value);
    const nd = await getJSON(`/v1/eng/designs/${d.id}`, { method: "PATCH", json: { layers: rows, options, note: "网页修改" } });
    toast(`已生成第 ${nd.version} 版，用量与报价已重算`);
    await openProject(state.cur.project.id, nd.id);
  });

  const hn = data.heat_normal || {};
  const left = el("div", {},
    el("div", { class: "sec" }, el("h3", {}, "衬里结构", d.stale ? chip("工况已变，请重算", "warn") : null),
      el("div", { class: "scroll" }, layerTable),
      el("div", { class: "opts" }, opt("discount_pct", "折扣 %"), opt("freight_per_t", "运费 元/t"), opt("install_per_m2", "施工 元/m²"), opt("tax_rate", "税率 %")),
      editable ? el("button", { class: "btn primary", onclick: recalc }, "重新计算（生成新版本）") : el("span", { class: "muted small" }, "这个版本已提交或发布，不能修改")),
    el("div", { class: "sec" }, el("h3", {}, "传热校核", el("span", { class: "muted small" }, `热流 ${hn.heat_flux ?? "—"} W/m² · 外壁 ${hn.shell_temp ?? "—"}℃`)),
      profile(data), el("ul", { class: "checks" }, data.checks.map((c) => el("li", { class: c.ok ? "" : "no" }, c.text))),
      el("p", { class: "muted small" }, (hn.formulas || []).join("；"))),
    el("div", { class: "sec" }, el("h3", {}, "选材理由"), el("ul", { class: "reasons" }, (data.reasons || []).map((r) => el("li", {}, r)))));

  const q = data.quote;
  const bom = el("table", { class: "tbl" }, el("tr", {}, ["层", "牌号", "名称", "体积 m³", "重量 t", "数量", "单位"].map((h, i) => el("th", { class: i > 2 ? "num" : "" }, h))),
    data.bom.map((b) => el("tr", { title: b.formula }, el("td", {}, b.role), el("td", {}, b.code), el("td", {}, b.name), el("td", { class: "num" }, b.volume_m3 ?? "—"), el("td", { class: "num" }, b.weight_t), el("td", { class: "num" }, b.qty ?? "—"), el("td", {}, b.unit))));
  const quote = el("table", { class: "tbl" }, el("tr", {}, ["牌号", "数量", "单价", "金额", "价格版本"].map((h, i) => el("th", { class: i && i < 4 ? "num" : "" }, h))),
    q.lines.map((l) => el("tr", {}, el("td", {}, l.code), el("td", { class: "num" }, `${l.qty ?? "—"} ${l.unit}`), el("td", { class: "num" }, yuan(l.unit_price)), el("td", { class: "num" }, yuan(l.amount)), el("td", { class: "small muted" }, l.price_ref || "无价格"))),
    [["材料金额", q.material_list], [`折扣 ${q.options.discount_pct}%`, -q.discount], ["材料净价", q.material_net], [`运费（${q.total_weight_t} t）`, q.freight], ["施工费", q.install], [`税额 ${q.options.tax_rate}%`, q.tax]]
      .map(([k, v]) => el("tr", {}, el("td", { colspan: 3 }, k), el("td", { class: "num" }, yuan(v)), el("td"))),
    el("tr", { class: "sum" }, el("td", { colspan: 3 }, "含税合计"), el("td", { class: "num" }, yuan(q.total)), el("td", { class: "small" }, `毛利 ${q.gross_margin_pct ?? "—"}%`)));

  const s = d.review_summary;
  const review = el("ul", { class: "review" }, d.review.map((it) => {
    const sev = SEV[it.severity];
    const sel = el("select", { disabled: !editable || it.blocking, onchange: (e) => dispose(d, it, e.target.value) },
      el("option", { value: "" }, it.blocking ? "必须修正" : "未处理"), Object.entries(DISP).map(([k, v]) => { const op = el("option", { value: k }, v); if (it.disposition === k) op.selected = true; return op; }));
    const src = it.source && it.source.page ? ` · 第 ${it.source.page} 页` : it.source && it.source.pages ? ` · 第 ${it.source.pages.join(", ")} 页` : it.source && it.source.calculation ? " · 计算依据" : "";
    return el("li", {}, chip(sev[0], sev[1]), el("div", {}, el("div", { class: "msg" }, `【${TYPE[it.type] || it.type}】${it.message}`), el("div", { class: "sug" }, it.suggestion + src + (it.by_name ? ` · ${it.by_name}` : ""))), sel);
  }));
  const publish = (e) => busy(e.target, async () => {
    const r = await api(`/v1/eng/designs/${d.id}/publish`, { method: "POST", json: {} });
    const j = await r.json();
    toast(r.status === 202 ? j.message : `报价已发布：${j.quote_no}`, 5000);
    await openProject(state.cur.project.id, d.id);
  });
  const right = el("div", {},
    el("div", { class: "sec" }, el("h3", {}, "审核清单", chip(`${s.total} 条`), s.open ? chip(`待处理 ${s.open}`, "warn") : chip("已全部处理", "ok"), s.blocking ? chip(`必须修正 ${s.blocking}`, "bad") : null), review,
      el("div", { class: "opts" }, el("button", { class: "btn primary", disabled: !editable || !s.ready || d.data_mode === "mock", onclick: publish }, "发布报价（送审批）"),
        el("button", { class: "btn small", disabled: d.data_mode === "mock", onclick: () => download(d, "xlsx") }, "导出 BOM / 报价 Excel"),
        el("button", { class: "btn small", disabled: d.data_mode === "mock", onclick: () => download(d, "docx") }, "导出技术方案 Word"))),
    el("div", { class: "sec" }, el("h3", {}, "BOM 材料清单"), el("div", { class: "scroll" }, bom)),
    el("div", { class: "sec" }, el("h3", {}, "报价"), el("div", { class: "scroll" }, quote)),
    el("div", { class: "sec" }, el("h3", {}, "技术方案正文"), (d.proposal || []).map((sec) => el("div", {}, el("b", {}, sec.title),
      sec.blocks.map((b) => Array.isArray(b) ? el("table", { class: "tbl" }, b.map((row, ri) => el("tr", {}, row.map((c) => el(ri ? "td" : "th", {}, c))))) : el("p", { class: "small" }, b))))));
  return el("div", { class: "eng-grid" }, left, right);
}

function profile(data) {
  const h = data.heat_max; if (!h) return el("p", { class: "muted small" }, "缺少热工参数，无法计算");
  const NS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(NS, "svg"); svg.setAttribute("viewBox", "0 0 400 170"); svg.setAttribute("class", "profile");
  const total = data.total_thickness_mm; const T0 = h.hot_temp; const x = (mm) => 30 + (mm / total) * 350; const y = (t) => 150 - (t / T0) * 130;
  let acc = 0; const pts = [[x(0), y(h.interface_temps[0])]];
  data.layers.forEach((ly, i) => { acc += ly.thickness_mm; pts.push([x(acc), y(h.interface_temps[i + 1])]); });
  const add = (tag, attrs, text) => { const e = document.createElementNS(NS, tag); for (const k in attrs) e.setAttribute(k, attrs[k]); if (text) e.textContent = text; svg.append(e); return e; };
  acc = 0;
  data.layers.forEach((ly) => { add("rect", { x: x(acc), y: 20, width: x(acc + ly.thickness_mm) - x(acc), height: 130, fill: "none", stroke: "currentColor", "stroke-opacity": ".2" }); add("text", { x: x(acc) + 3, y: 162 }, `${ly.code.replace(/^LN-/, "")} ${ly.thickness_mm}`); acc += ly.thickness_mm; });
  add("polyline", { points: pts.map((p) => p.join(",")).join(" "), class: "curve" });
  pts.forEach((p, i) => add("text", { x: Math.min(p[0] + 2, 360), y: p[1] - 4 }, `${Math.round(h.interface_temps[i])}℃`));
  return svg;
}

async function dispose(d, it, value) {
  try {
    const r = await getJSON(`/v1/eng/designs/${d.id}/review/${it.id}`, { method: "PATCH", json: { disposition: value || null } });
    it.disposition = value || null; d.review_summary = r.review_summary;
    render();
  } catch (e) { toast(e.message, 5000); render(); }
}

async function download(d, fmt) {
  try {
    const r = await api(`/v1/eng/designs/${d.id}/export?format=${fmt}`);
    const cd = r.headers.get("content-disposition") || ""; const m = cd.match(/filename\*=UTF-8''(.+)$/);
    const a = el("a", { href: URL.createObjectURL(await r.blob()), download: m ? decodeURIComponent(m[1]) : `方案.${fmt}` });
    document.body.append(a); a.click(); a.remove();
  } catch (e) { toast(e.message, 5000); }
}

async function doMatch() {
  const r = await getJSON(`/v1/eng/projects/${state.cur.project.id}/match`, { method: "POST" });
  if (!r.designs.length) {
    toast("没有找到满足约束的方案：" + r.rejected.map((x) => `${x.reason}（${x.count}）`).join("；"), 8000);
    return;
  }
  toast(`试算 ${r.tried} 种组合，${r.feasible} 种满足约束，给出 ${r.designs.length} 个方案`);
  state.design = null;
  await openProject(state.cur.project.id, r.designs[0].id);
}

// ---------------- 事件 ----------------
$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault(); $("#login-err").hidden = true;
  try { await login($("#key").value.trim()); } catch (err) { $("#login-err").textContent = err.message; $("#login-err").hidden = false; }
});
$("#logout").addEventListener("click", logout);
$("#new-proj").addEventListener("submit", (e) => {
  e.preventDefault();
  const id = $("#doc-pick").value; if (!id) return;
  busy(e.submitter, async () => {
    const r = await getJSON("/v1/eng/projects", { method: "POST", json: { document_id: id } });
    toast(r.duplicate ? "这份文件已经建过项目" : "工程项目已创建");
    await openProject(r.project.id);
  });
});
window.addEventListener("hashchange", () => { const id = location.hash.slice(1); if (id && (!state.cur || state.cur.project.id !== id)) openProject(id).catch(() => {}); });

(async () => {
  const k = store("get");
  if (k) { try { await login(k); return; } catch (_) {} }
  $("#login").hidden = false;
})();
