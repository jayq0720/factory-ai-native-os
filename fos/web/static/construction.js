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
      const [sites, logs, dry] = await Promise.all([
        api("/v1/construction/sites").then((r) => r.json()),
        api("/v1/construction/logs").then((r) => r.json()),
        api("/v1/construction/dryout").then((r) => r.json()),
      ]);
      const sc = sites.counts;
      const lc = logs.counts;
      const dc = dry.counts;
      $("siteSummary").textContent =
        `施工项目 ${sc.total}（砌筑 ${sc.building}，烘炉 ${sc.drying}，已验收 ${sc.accepted}）` +
        ` | 日报 ${lc.total} 份（累计 ${lc.total_tons} 吨）` +
        ` | 烘炉测温 ${dc.total} 次（告警 ${dc.alerts} 次）`;

      fillTable("siteTable", sites.items || [], [
        (r) => r.site_no,
        (r) => r.name,
        (r) => r.customer_name,
        (r) => r.furnace_type,
        (r) => r.supervisor,
        (r) => r.planned_tons,
        (r) => r.completed_tons,
        (r) => r.progress_pct,
        (r) => r.status_text,
      ]);
      fillTable("logTable", logs.items || [], [
        (r) => r.log_no,
        (r) => r.site_name,
        (r) => r.log_date,
        (r) => r.crew_size,
        (r) => r.tons_installed,
        (r) => (r.safety_ok ? "正常" : "异常"),
        (r) => r.summary,
      ]);
      fillTable("dryTable", dry.items || [], [
        (r) => r.dryout_no,
        (r) => r.site_name,
        (r) => r.stage_name,
        (r) => r.target_temp_c,
        (r) => r.actual_temp_c,
        (r) => r.ramp_rate_c_per_h,
        (r) => (r.within_spec ? "正常" : "告警"),
        (r) => r.recorded_at,
      ]);
      setMsg("已刷新");
    } catch (e) {
      setMsg(e.message);
    }
  }

  $("btnLoad").addEventListener("click", loadAll);
  $("btnExport").addEventListener("click", async () => {
    try {
      const res = await api("/v1/construction/report.xlsx");
      const blob = await res.blob();
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = "筑炉施工与烘炉验收.xlsx";
      a.click();
      URL.revokeObjectURL(a.href);
      setMsg("已导出筑炉施工报表");
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
