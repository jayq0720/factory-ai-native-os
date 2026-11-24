(function () {
  const $ = (id) => document.getElementById(id);
  const KEY_STORE = "fos_api_key";

  function apiKey() {
    const v = $("apiKey").value.trim();
    if (v) localStorage.setItem(KEY_STORE, v);
    return v || localStorage.getItem(KEY_STORE) || "";
  }

  function setMsg(text) {
    $("msg").textContent = text;
  }

  async function api(path, opts = {}) {
    const k = apiKey();
    const headers = Object.assign({}, opts.headers || {});
    if (k) headers["X-API-Key"] = k;
    const res = await fetch(path, Object.assign({ credentials: "same-origin" }, opts, { headers }));
    if (!res.ok) {
      const txt = await res.text();
      throw new Error(`HTTP ${res.status}: ${txt.slice(0, 160)}`);
    }
    return res;
  }

  function fillTable(tableId, rows, cols) {
    const tbody = $(tableId).querySelector("tbody");
    tbody.textContent = "";
    if (!rows.length) {
      const tr = document.createElement("tr");
      const td = document.createElement("td");
      td.colSpan = cols.length;
      td.textContent = "暂无记录";
      tr.appendChild(td);
      tbody.appendChild(tr);
      return;
    }
    for (const r of rows) {
      const tr = document.createElement("tr");
      for (const fn of cols) {
        const td = document.createElement("td");
        const val = fn(r);
        td.textContent = val === null || val === undefined ? "" : String(val);
        tr.appendChild(td);
      }
      tbody.appendChild(tr);
    }
  }

  async function loadAll() {
    setMsg("加载中...");
    try {
      const me = await (await api("/v1/me")).json();
      $("who").textContent = `${me.tenant_name} · ${me.display_name}`;
      const [sum, hz, pt, env, inc] = await Promise.all([
        api("/v1/ehs/summary").then((r) => r.json()),
        api("/v1/ehs/hazards").then((r) => r.json()),
        api("/v1/ehs/permits").then((r) => r.json()),
        api("/v1/ehs/env").then((r) => r.json()),
        api("/v1/ehs/incidents").then((r) => r.json()),
      ]);
      const k = sum.kpis;
      $("kpiSummary").textContent =
        `隐患总数 ${k.hazards_total}（未销项 ${k.hazards_open}，超期 ${k.hazards_overdue}，闭环率 ${k.hazard_closure_rate_pct}%）` +
        ` | 特种作业票 ${k.permits_total}（作业中 ${k.permits_active}，高风险 ${k.permits_high_risk}）` +
        ` | 事故 ${k.incidents_total}（受伤 ${k.total_injured}）` +
        ` | 环保监测 ${k.env_total}（超标 ${k.env_exceeded}，达标率 ${k.env_compliance_rate_pct}%）`;

      fillTable("hzTable", hz.items || [], [
        (r) => r.hazard_no,
        (r) => r.location,
        (r) => r.category_text,
        (r) => r.level_text,
        (r) => r.status_text + (r.overdue ? " (超期)" : ""),
        (r) => r.due_date,
        (r) => r.description,
      ]);
      fillTable("ptTable", pt.items || [], [
        (r) => r.permit_no,
        (r) => r.kind_text,
        (r) => r.location,
        (r) => r.worker_name,
        (r) => r.status_text,
        (r) => r.valid_from,
        (r) => r.valid_until,
      ]);
      fillTable("envTable", env.items || [], [
        (r) => r.point_name,
        (r) => r.kind_text,
        (r) => r.value,
        (r) => r.limit_value,
        (r) => r.unit,
        (r) => (r.passed ? "达标" : "超标"),
        (r) => r.measured_at,
      ]);
      fillTable("incTable", inc.items || [], [
        (r) => r.incident_no,
        (r) => r.kind_text,
        (r) => r.location,
        (r) => r.injured_count,
        (r) => r.status_text,
        (r) => r.description,
      ]);
      setMsg("已刷新");
    } catch (e) {
      setMsg(e.message);
    }
  }

  $("btnLoad").addEventListener("click", loadAll);
  $("btnRemind").addEventListener("click", async () => {
    try {
      const r = await (await api("/v1/ehs/hazards/remind", { method: "POST" })).json();
      setMsg(`已生成 ${r.created} 条隐患超期/临期提醒待办`);
    } catch (e) {
      setMsg(e.message);
    }
  });
  $("btnExport").addEventListener("click", async () => {
    try {
      const res = await api("/v1/ehs/report.xlsx");
      const blob = await res.blob();
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = "EHS合规报告.xlsx";
      a.click();
      URL.revokeObjectURL(a.href);
      setMsg("已导出 EHS 合规报告");
    } catch (e) {
      setMsg(e.message);
    }
  });

  const saved = localStorage.getItem(KEY_STORE);
  if (saved) {
    $("apiKey").value = saved;
    loadAll();
  }
})();
