const $ = (id) => document.getElementById(id);

function getKey() {
  return $("apiKey").value.trim() || localStorage.getItem("fos_key") || "";
}

function setMsg(text, isErr = false) {
  const el = $("msg");
  el.textContent = text;
  el.className = isErr ? "bad" : "muted";
}

async function api(path, opts = {}) {
  const key = getKey();
  if (key) localStorage.setItem("fos_key", key);
  const auth = key ? { Authorization: `Bearer ${key}` } : {};
  const res = await fetch(path, {
    credentials: "same-origin",
    ...opts,
    headers: { ...auth, "Content-Type": "application/json", ...(opts.headers || {}) },
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || `HTTP ${res.status}`);
  }
  return res.json();
}

function td(text) {
  const cell = document.createElement("td");
  cell.textContent = text == null ? "" : String(text);
  return cell;
}

function renderTable(tableId, rows, cols) {
  const tb = $(tableId).querySelector("tbody");
  tb.innerHTML = "";
  for (const r of rows) {
    const tr = document.createElement("tr");
    for (const c of cols) tr.appendChild(td(c(r)));
    tb.appendChild(tr);
  }
}

async function loadAll() {
  try {
    setMsg("加载中...");
    const me = await api("/v1/me");
    $("who").textContent = `${me.display_name}（${me.tenant_name}）`;
    const [eq, pm, wo, sp, rel] = await Promise.all([
      api("/v1/eam/equipment"),
      api("/v1/eam/pm"),
      api("/v1/eam/work-orders"),
      api("/v1/eam/spare-parts"),
      api("/v1/eam/reliability"),
    ]);
    const m = rel.metrics;
    $("relSummary").textContent = `可用率 ${m.availability_pct}% · 故障 ${m.breakdown_count} 次 · 总停机 ${m.total_downtime_minutes} 分钟 · MTTR ${m.mttr_minutes ?? "—"} 分 · MTBF ${m.mtbf_hours ?? "—"} 小时`;
    const c = eq.counts;
    $("eqSummary").textContent = `共 ${c.total} 台：运行 ${c.running} / 维保 ${c.maintenance} / 闲置 ${c.idle} / 报废 ${c.retired}`;
    renderTable("eqTable", eq.items, [
      (r) => r.code,
      (r) => r.name,
      (r) => r.category_text,
      (r) => r.model || "—",
      (r) => r.location || "—",
      (r) => r.criticality,
      (r) => r.status_text,
      (r) => r.next_pm_date || "—",
    ]);
    renderTable("pmTable", pm.items, [
      (r) => r.plan_no,
      (r) => r.equipment_name,
      (r) => r.title,
      (r) => r.cycle_days,
      (r) => r.last_done_date || "—",
      (r) => r.next_due_date || "—",
      (r) => r.pm_text,
    ]);
    renderTable("woTable", wo.items, [
      (r) => r.wo_no,
      (r) => r.equipment_name,
      (r) => r.kind_text,
      (r) => r.priority_text,
      (r) => r.status_text,
      (r) => r.downtime_minutes,
      (r) => r.symptom || "",
      (r) => r.action_taken || "—",
    ]);
    renderTable("spTable", sp.items, [
      (r) => r.id,
      (r) => r.name,
      (r) => r.spec || "—",
      (r) => `${r.on_hand} ${r.unit}`,
      (r) => (r.safety_stock != null ? `${r.safety_stock} ${r.unit}` : "—"),
      (r) => (r.low_stock ? "低库存" : "正常"),
    ]);
    setMsg("已刷新");
  } catch (e) {
    setMsg(e.message, true);
  }
}

$("apiKey").value = localStorage.getItem("fos_key") || "";
$("btnLoad").onclick = loadAll;
$("btnRemind").onclick = async () => {
  try {
    const r = await api("/v1/eam/pm/remind", { method: "POST" });
    setMsg(`已生成 ${r.count} 条保养到期提醒`);
  } catch (e) {
    setMsg(e.message, true);
  }
};
$("btnExport").onclick = async () => {
  try {
    const res = await fetch("/v1/eam/report/export", { headers: { Authorization: `Bearer ${getKey()}` } });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "eam_report.xlsx";
    a.click();
    URL.revokeObjectURL(url);
    setMsg("已导出设备报表");
  } catch (e) {
    setMsg(e.message, true);
  }
};
if ($("apiKey").value) loadAll();
