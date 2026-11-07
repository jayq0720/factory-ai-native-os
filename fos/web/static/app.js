// 文档解析控制台。无构建步骤，原生 JS；只调用 /v1 接口，API Key 存在本机 localStorage。
"use strict";

const $ = (s) => document.querySelector(s);
const KEY = "fos.apiKey";
const ACTIVE = ["queued", "parsing", "extracting"];
const KIND = { tech: "技术文件", tender: "招标文件", drawing: "图纸", boq: "工程量清单", receipt: "单据", report: "报告", other: "其他" };
const STATUS = { queued: "排队中", parsing: "解析中", extracting: "抽取中", parsed: "已完成", failed: "失败" };
const FSTATUS = { extracted: "待核对", missing: "缺失", corrected: "已修改", confirmed: "已确认" };
const METHOD = { text_layer: "文字层", ocr: "OCR", ocr_mock: "模拟识别", docx: "Word", xlsx: "Excel" };
const FMETHOD = { rule: "规则", llm: "模型", "rule+llm": "规则+模型", manual: "人工", none: "" };

const state = { key: "", me: null, ready: null, docs: [], cur: null, fields: null, page: 1, tab: "text", filter: "all", hl: [], timer: null, imgUrl: null };

function store(op, v) {
  try {
    if (op === "get") return localStorage.getItem(KEY) || "";
    if (op === "set") localStorage.setItem(KEY, v);
    if (op === "del") localStorage.removeItem(KEY);
  } catch (_) { /* 隐私模式下不可用：只在本页内有效 */ }
  return "";
}

async function api(path, opts = {}) {
  const headers = Object.assign({ Authorization: "Bearer " + state.key }, opts.headers || {});
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

function toast(msg, ms = 2600) {
  const t = $("#toast"); t.textContent = msg; t.hidden = false;
  clearTimeout(toast._t); toast._t = setTimeout(() => (t.hidden = true), ms);
}
function el(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined && v !== false) e.setAttribute(k, v === true ? "" : v);
  }
  for (const k of kids.flat()) if (k !== null && k !== undefined && k !== false) e.append(k.nodeType ? k : String(k));
  return e;
}
const chip = (text, cls = "") => el("span", { class: "chip " + cls }, text);
const fill = (node, ...kids) => node.replaceChildren(...kids.flat().filter((k) => k !== null && k !== undefined && k !== false));

// ---------------- 登录 ----------------

async function login(key) {
  state.key = key;
  state.me = await getJSON("/v1/me");
  store("set", key);
  try { state.ready = await (await fetch("/readyz")).json(); } catch (_) { state.ready = null; }
  $("#login").hidden = true; $("#app").hidden = false; $("#logout").hidden = false;
  $("#who").textContent = `${state.me.tenant_name} · ${state.me.display_name}`;
  renderModes();
  await loadDocs();
  const id = location.hash.slice(1);
  if (id) openDoc(id).catch(() => {});
}

function logout() {
  store("del"); state.key = ""; state.cur = null;
  $("#app").hidden = true; $("#login").hidden = false; $("#logout").hidden = true; $("#who").textContent = "";
  $("#modes").replaceChildren(); $("#mock-banner").hidden = true;
}

function renderModes() {
  const r = state.ready; const m = $("#modes"); m.replaceChildren();
  if (!r) return;
  const ocr = { mock: ["OCR：模拟", "mock"], xiaomi: ["OCR：Xiaomi-OCR-0", "ok"], qwen_vl: ["OCR：百炼 Qwen-VL", "ok"] }[r.ocr_mode] || [r.ocr_mode, ""];
  m.append(chip(ocr[0], ocr[1]));
  m.append(chip(r.llm_extraction ? "模型抽取：开" : "模型抽取：关（只用规则）", r.llm_extraction ? "ok" : ""));
}

// ---------------- 列表与上传 ----------------

async function loadDocs() {
  const j = await getJSON("/v1/documents?limit=100");
  state.docs = j.items;
  $("#doc-count").textContent = `共 ${j.total} 份`;
  renderDocs();
  schedule();
}

function renderDocs() {
  const ul = $("#docs"); ul.replaceChildren();
  for (const d of state.docs) {
    const st = d.status === "parsed" ? chip(STATUS[d.status], "ok") : d.status === "failed" ? chip("失败", "bad") : chip(STATUS[d.status] || d.status, "info");
    const meta = el("div", { class: "meta" }, chip(KIND[d.kind] || d.kind), st, d.data_mode === "mock" ? chip("模拟", "mock") : null,
      d.fields && d.fields.missing_required ? chip(`缺 ${d.fields.missing_required}`, "bad") : null,
      d.fields && d.fields.conflicts ? chip(`冲突 ${d.fields.conflicts}`, "warn") : null);
    const open = () => openDoc(d.id).then(() => { if (innerWidth < 760) $("#detail").scrollIntoView({ behavior: "smooth" }); }).catch((e) => toast(e.message));
    const li = el("li", { class: state.cur && state.cur.id === d.id ? "on" : "", onclick: open },
      el("div", { class: "name", title: d.filename }, d.filename), meta);
    if (ACTIVE.includes(d.status)) {
      const pct = d.page_count ? Math.round((100 * d.progress) / d.page_count) : 5;
      li.append(el("div", { class: "mini-bar" }, el("i", { style: `width:${pct}%` })));
    }
    ul.append(li);
  }
}

async function uploadFiles(files) {
  const kind = $("#kind").value; const msg = $("#upload-msg");
  for (const f of files) {
    msg.hidden = false; msg.textContent = `正在上传 ${f.name}…`;
    const fd = new FormData(); fd.append("file", f); fd.append("kind", kind);
    try {
      const r = await api("/v1/documents?wait=false", { method: "POST", body: fd });
      const d = await r.json();
      msg.textContent = d.duplicate ? `${f.name} 已经上传过，直接打开` : `${f.name} 已上传，正在解析`;
      await loadDocs();
      await openDoc(d.id);
    } catch (e) { msg.textContent = `${f.name} 上传失败：${e.message}`; }
  }
}

// ---------------- 文档详情 ----------------

async function openDoc(id) {
  const d = await getJSON(`/v1/documents/${id}`);
  const same = state.cur && state.cur.id === id;
  state.cur = d;
  if (!same) { state.page = 1; state.hl = []; state.filter = "all"; setTabs(".filters", "all", "filter"); }
  if (location.hash.slice(1) !== id) history.replaceState(null, "", "#" + id);
  $("#empty").hidden = true; $("#doc").hidden = false;
  renderDocHead();
  renderDocs();
  if (d.status === "parsed") {
    state.fields = await getJSON(`/v1/documents/${id}/fields`);
    renderFields();
    if (!same) { state.tab = d.format === "pdf" || d.format === "image" ? "image" : "text"; setTabs(".tabs", state.tab, "tab"); }
    await renderPage();
  } else {
    state.fields = null;
    $("#f-list").replaceChildren(); fill($("#f-summary")); $("#f-schema").textContent = "";
    $("#p-text").replaceChildren(el("p", { class: "muted" }, d.status === "failed" ? "解析失败" : "解析完成后显示原文"));
    $("#p-label").textContent = ""; $("#p-method").textContent = "";
  }
  schedule();
}

function renderDocHead() {
  const d = state.cur;
  $("#d-name").textContent = d.filename;
  const size = d.size_bytes > 1048576 ? (d.size_bytes / 1048576).toFixed(1) + " MB" : Math.max(1, Math.round(d.size_bytes / 1024)) + " KB";
  fill($("#d-meta"),
    `${KIND[d.kind] || d.kind} · ${d.page_count || "?"} 页 · ${size} · ${new Date(d.created_at).toLocaleString("zh-CN")} · `,
    chip(STATUS[d.status] || d.status, d.status === "parsed" ? "ok" : d.status === "failed" ? "bad" : "info"),
    d.data_mode === "mock" ? chip("模拟数据", "mock") : null,
  );
  $("#mock-banner").hidden = d.data_mode !== "mock";
  const active = ACTIVE.includes(d.status);
  $("#d-progress").hidden = !active;
  if (active) {
    const pct = d.page_count ? Math.round((100 * d.progress) / d.page_count) : 3;
    $("#d-bar").style.width = pct + "%";
    $("#d-ptext").textContent = d.status === "extracting" ? "正在抽取字段…" : d.status === "queued" ? "排队中…" : `已识别 ${d.progress} / ${d.page_count || "?"} 页`;
  }
  $("#d-error").hidden = !d.error; $("#d-error").textContent = d.error || "";
  $("#b-retry").hidden = d.status !== "failed";
  const done = d.status === "parsed";
  $("#b-extract").disabled = !done; $("#b-json").disabled = !done;
  $("#b-xlsx").disabled = !done || d.data_mode === "mock";
  $("#b-xlsx").title = d.data_mode === "mock" ? "模拟数据不能导出正式文件" : "";
}

// 轮询：有文档在解析时每 1.5 秒刷新一次
function schedule() {
  clearTimeout(state.timer);
  const busy = state.docs.some((d) => ACTIVE.includes(d.status)) || (state.cur && ACTIVE.includes(state.cur.status));
  if (!busy) return;
  state.timer = setTimeout(async () => {
    try {
      await loadDocs();
      if (state.cur) {
        const fresh = state.docs.find((d) => d.id === state.cur.id);
        if (fresh && (ACTIVE.includes(state.cur.status) || fresh.status !== state.cur.status)) await openDoc(state.cur.id);
      }
    } catch (e) { toast(e.message); }
  }, 1500);
}

// ---------------- 原文 ----------------

function safeTable(htmlText) {
  // 只保留表格结构与合并单元格，其余标签一律当文字（OCR 输出不可信）
  const doc = new DOMParser().parseFromString(htmlText, "text/html");
  const t = el("table");
  for (const tr of doc.querySelectorAll("tr")) {
    const row = el("tr");
    for (const c of tr.querySelectorAll("td,th")) {
      const cell = el(c.tagName === "TH" ? "th" : "td", {
        rowspan: /^\d+$/.test(c.getAttribute("rowspan") || "") ? c.getAttribute("rowspan") : null,
        colspan: /^\d+$/.test(c.getAttribute("colspan") || "") ? c.getAttribute("colspan") : null,
      }, c.textContent.trim());
      row.append(cell);
    }
    t.append(row);
  }
  return t;
}

function renderMarkdown(md, box) {
  box.replaceChildren();
  const parts = md.split(/(<table[\s\S]*?<\/table>)/i);
  for (const part of parts) {
    if (/^<table/i.test(part)) { box.append(safeTable(part)); continue; }
    let pipe = [];
    const flush = () => {
      if (!pipe.length) return;
      const t = el("table");
      for (const cells of pipe) t.append(el("tr", {}, cells.map((c) => el("td", {}, c))));
      box.append(t); pipe = [];
    };
    for (const raw of part.split("\n")) {
      const line = raw.trimEnd();
      if (/^\s*\|.*\|\s*$/.test(line)) {
        const cells = line.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
        if (!cells.every((c) => /^:?-{2,}:?$/.test(c) || c === "")) pipe.push(cells);
        continue;
      }
      flush();
      if (!line.trim()) continue;
      const h = line.match(/^(#{1,6})\s+(.*)$/);
      if (h) box.append(el("h" + Math.min(4, h[1].length + 1), {}, h[2]));
      else if (line.startsWith(">")) box.append(el("blockquote", {}, line.replace(/^>\s?/, "")));
      else box.append(el("p", {}, line));
    }
    flush();
  }
}

function highlight(box, strings) {
  const norm = (s) => s.replace(/\s+/g, "");
  const wanted = strings.map((s) => s.trim()).filter((s) => s.length >= 1);
  let first = null;
  // 表格单元格：整格高亮
  for (const td of box.querySelectorAll("td,th")) {
    const t = norm(td.textContent);
    if (t && wanted.some((w) => norm(w) === t || (norm(w).length >= 4 && t.includes(norm(w))))) { td.classList.add("hl"); first = first || td; }
  }
  // 普通文字：包 <mark>
  const walker = document.createTreeWalker(box, NodeFilter.SHOW_TEXT);
  const nodes = []; while (walker.nextNode()) nodes.push(walker.currentNode);
  for (const n of nodes) {
    if (n.parentElement.closest("td,th")) continue;
    for (const w of wanted) {
      const i = n.data.indexOf(w);
      if (i < 0) continue;
      const after = n.splitText(i); after.splitText(w.length);
      const m = el("mark", {}, after.data); after.replaceWith(m); first = first || m;
      break;
    }
  }
  if (first) first.scrollIntoView({ block: "center", behavior: "smooth" });
}

async function renderPage() {
  const d = state.cur; if (!d || d.status !== "parsed") return;
  const n = Math.max(1, Math.min(state.page, d.page_count)); state.page = n;
  $("#p-label").textContent = `第 ${n} / ${d.page_count} 页`;
  $("#p-prev").disabled = n <= 1; $("#p-next").disabled = n >= d.page_count;
  const p = await getJSON(`/v1/documents/${d.id}/pages/${n}`);
  $("#p-method").textContent = METHOD[p.method] || p.method;
  $("#p-method").className = "chip " + (p.data_mode === "mock" ? "mock" : "");
  const box = $("#p-text");
  renderMarkdown(p.markdown || "", box);
  if (state.hl.length) highlight(box, state.hl);
  const hasImage = d.format === "pdf" || d.format === "image";
  document.querySelector('.tab[data-tab="image"]').hidden = !hasImage;
  if (!hasImage && state.tab === "image") { state.tab = "text"; setTabs(".tabs", "text", "tab"); }
  $("#p-text").hidden = state.tab !== "text"; $("#p-image").hidden = state.tab !== "image";
  if (state.tab === "image") {
    try {
      const blob = await (await api(`/v1/documents/${d.id}/pages/${n}/image`)).blob();
      if (state.imgUrl) URL.revokeObjectURL(state.imgUrl);
      state.imgUrl = URL.createObjectURL(blob); $("#p-img").src = state.imgUrl;
    } catch (e) { toast("预览图加载失败：" + e.message); }
  }
}

function gotoSource(src) {
  state.page = src.page; state.hl = src.highlight || [src.quote];
  state.tab = "text"; setTabs(".tabs", "text", "tab");
  renderPage();
}

function setTabs(sel, value, attr) {
  for (const b of document.querySelectorAll(sel + " .tab")) b.classList.toggle("on", b.dataset[attr] === value);
}

// ---------------- 字段 ----------------

function renderFields() {
  const F = state.fields; const list = $("#f-list"); list.replaceChildren();
  $("#f-none").hidden = !!F.schema;
  $("#f-schema").textContent = F.schema ? `${F.schema.name}（v${F.schema.version}）` : "未抽取字段";
  const s = F.summary;
  fill($("#f-summary"),
    chip(`字段 ${s.total}`), chip(`已确认 ${s.confirmed}`, "ok"),
    s.missing_required ? chip(`必填缺失 ${s.missing_required}`, "bad") : null,
    s.missing - s.missing_required > 0 ? chip(`其他缺失 ${s.missing - s.missing_required}`) : null,
    s.conflicts ? chip(`冲突 ${s.conflicts}`, "warn") : null,
  );
  const keep = (f) => state.filter === "all" || (state.filter === "missing" && f.status === "missing")
    || (state.filter === "conflict" && f.conflict) || (state.filter === "todo" && (f.status === "extracted" || (f.status === "missing" && f.required)));
  let group = null;
  for (const f of F.fields.filter(keep)) {
    if (f.group !== group) { group = f.group; list.append(el("div", { class: "group-title" }, group)); }
    list.append(fieldRow(f));
  }
  if (!list.children.length && F.schema) list.append(el("p", { class: "muted" }, "没有符合条件的字段"));
}

function fieldRow(f) {
  const val = f.value
    ? el("span", {}, f.value, f.unit ? el("span", { class: "unit" }, f.unit) : null)
    : el("span", { class: "none" }, "原文中未找到");
  const stCls = { confirmed: "ok", corrected: "info", missing: f.required ? "bad" : "", extracted: "" }[f.status];
  const btns = el("div", { class: "fbtns" },
    f.value && f.status !== "confirmed" ? el("button", { class: "btn icon", title: "确认无误", onclick: () => patch(f, { action: "confirm" }) }, "✓ 确认") : null,
    el("button", { class: "btn icon", title: "修改", onclick: () => editField(f) }, "修改"),
    f.status === "corrected" || f.status === "confirmed" ? el("button", { class: "btn icon", title: "回到抽取结果", onclick: () => patch(f, { action: "reset" }) }, "还原") : null,
  );
  const pages = [];
  const seen = new Set();
  for (const src of f.sources || []) {
    if (seen.has(src.page)) continue; seen.add(src.page);  // 每页一个按钮，悬停看原文
    if (pages.length >= 8) break;
    pages.push(el("button", { class: "cite", title: src.quote, onclick: () => gotoSource(src) }, "第 " + src.page + " 页"));
  }
  const sub = el("div", { class: "fsub" }, chip(FSTATUS[f.status] || f.status, stCls),
    f.conflict ? chip("多处不一致", "warn") : null,
    f.data_mode === "mock" ? chip("模拟", "mock") : null,
    f.status !== "missing" && f.method !== "manual" ? el("span", { class: "muted" }, `${FMETHOD[f.method] || f.method} · 可信度 ${f.confidence}`) : null,
    pages);
  const row = el("div", { class: `field ${f.status} ${f.required ? "req" : ""} ${f.conflict ? "conflict" : ""}` },
    el("div", { class: "fname" }, f.name, f.required ? el("span", { class: "req", title: "必填" }, "*") : null),
    el("div", { class: "fval" }, val), btns, sub);
  const alts = (f.candidates || []).filter((c) => !(c.value === f.value && c.unit === f.unit));
  if (alts.length || (f.history || []).length) {
    const det = el("details", {}, el("summary", {}, [alts.length ? `其他候选 ${alts.length}` : null, f.history.length ? `修改记录 ${f.history.length}` : null].filter(Boolean).join(" · ")));
    if (alts.length) {
      det.append(el("ul", {}, alts.slice(0, 10).map((c) => el("li", {},
        `${c.value}${c.unit ? " " + c.unit : ""}（${FMETHOD[c.method] || c.method}，${c.confidence}）`,
        (c.sources || []).slice(0, 3).map((s) => el("button", { class: "cite", title: s.quote, onclick: () => gotoSource(s) }, "第 " + s.page + " 页")),
        el("button", { class: "btn icon", onclick: () => patch(f, { action: "correct", value: c.value, unit: c.unit || "", note: "采用候选值" }) }, "采用")))));
    }
    if (f.history.length) {
      det.append(el("ul", {}, f.history.slice().reverse().map((h) => el("li", {},
        `${new Date(h.at).toLocaleString("zh-CN")} ${h.by_name} ${{ correct: "修改", confirm: "确认", reset: "还原" }[h.action] || h.action}：`
        + `${h.before.value ?? "（空）"} → ${h.after.value ?? "（空）"}${h.note ? "（" + h.note + "）" : ""}`))));
    }
    row.append(det);
  }
  return row;
}

async function patch(f, body) {
  try {
    await getJSON(`/v1/documents/${state.cur.id}/fields/${f.key}`, {
      method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    state.fields = await getJSON(`/v1/documents/${state.cur.id}/fields`);
    renderFields();
    loadDocs();
  } catch (e) { toast(e.message); }
}

function editField(f) {
  const dlg = $("#edit-dlg");
  $("#e-title").textContent = `修改：${f.name}`;
  $("#e-value").value = f.value || ""; $("#e-unit").value = f.unit || ""; $("#e-note").value = "";
  dlg.onclose = () => {
    if (dlg.returnValue !== "ok") return;
    patch(f, { action: "correct", value: $("#e-value").value, unit: $("#e-unit").value, note: $("#e-note").value || null });
  };
  dlg.showModal();
}

async function reExtract() {
  const d = state.cur;
  const sch = await getJSON("/v1/extraction/schemas");
  const sel = $("#s-select"); sel.replaceChildren();
  for (const s of sch.items) sel.append(el("option", { value: s.id, selected: s.id === d.schema_id }, `${s.name}（${s.fields.length} 个字段）`));
  const dlg = $("#schema-dlg");
  dlg.onclose = async () => {
    if (dlg.returnValue !== "ok") return;
    try {
      const r = await getJSON(`/v1/documents/${d.id}/extract`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ schema_id: sel.value }) });
      toast(`已重新抽取，保留人工修改 ${r.kept_human_edits} 项`);
      await openDoc(d.id); loadDocs();
    } catch (e) { toast(e.message); }
  };
  dlg.showModal();
}

async function download(path, fallbackName) {
  try {
    const r = await api(path);
    const cd = r.headers.get("content-disposition") || "";
    const m = cd.match(/filename\*=UTF-8''([^;]+)/);
    const name = m ? decodeURIComponent(m[1]) : fallbackName;
    const url = URL.createObjectURL(await r.blob());
    const a = el("a", { href: url, download: name }); document.body.append(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 5000);
  } catch (e) { toast(e.message); }
}

// ---------------- 事件 ----------------

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("#login-err").hidden = true;
  try { await login($("#key").value.trim()); } catch (err) { $("#login-err").textContent = err.message; $("#login-err").hidden = false; }
});
$("#logout").addEventListener("click", logout);
$("#file").addEventListener("change", (e) => { uploadFiles([...e.target.files]); e.target.value = ""; });
const drop = $("#drop");
drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
drop.addEventListener("dragleave", () => drop.classList.remove("over"));
drop.addEventListener("drop", (e) => { e.preventDefault(); drop.classList.remove("over"); uploadFiles([...e.dataTransfer.files]); });
$("#p-prev").addEventListener("click", () => { state.page--; state.hl = []; renderPage(); });
$("#p-next").addEventListener("click", () => { state.page++; state.hl = []; renderPage(); });
for (const b of document.querySelectorAll(".tabs .tab")) b.addEventListener("click", () => { state.tab = b.dataset.tab; setTabs(".tabs", state.tab, "tab"); renderPage(); });
for (const b of document.querySelectorAll(".filters .tab")) b.addEventListener("click", () => { state.filter = b.dataset.filter; setTabs(".filters", state.filter, "filter"); renderFields(); });
$("#b-xlsx").addEventListener("click", () => download(`/v1/documents/${state.cur.id}/export?format=xlsx`, "fields.xlsx"));
$("#b-json").addEventListener("click", () => download(`/v1/documents/${state.cur.id}/export?format=json`, "fields.json"));
$("#b-original").addEventListener("click", () => download(`/v1/documents/${state.cur.id}/original`, state.cur.filename));
$("#b-extract").addEventListener("click", reExtract);
$("#b-retry").addEventListener("click", async () => {
  try { await api(`/v1/documents/${state.cur.id}/retry`, { method: "POST" }); await openDoc(state.cur.id); loadDocs(); } catch (e) { toast(e.message); }
});
document.addEventListener("keydown", (e) => {
  if (!state.cur || e.target.closest("input,textarea,select,dialog")) return;
  if (e.key === "ArrowLeft" && state.page > 1) { state.page--; state.hl = []; renderPage(); }
  if (e.key === "ArrowRight" && state.cur.page_count && state.page < state.cur.page_count) { state.page++; state.hl = []; renderPage(); }
});

(async () => {
  const k = store("get");
  if (k) { try { await login(k); return; } catch (_) { /* 失效就显示登录 */ } }
  $("#login").hidden = false;
})();
