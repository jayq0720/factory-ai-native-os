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
    const res = await fetch(path, Object.assign({}, opts, { headers }));
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
      const [ck, leads, intel] = await Promise.all([
        api("/v1/market/cockpit").then((r) => r.json()),
        api("/v1/market/leads").then((r) => r.json()),
        api("/v1/market/intel").then((r) => r.json()),
      ]);
      const k = ck.kpis;
      $("cockpitSummary").textContent =
        `应收余额 ${k.finance_ar_outstanding}元 / 应付 ${k.finance_ap_outstanding}元` +
        ` | 未闭环NCR ${k.quality_open_ncr} | 未完修工单 ${k.eam_open_wo}` +
        ` | 未销项隐患 ${k.ehs_open_hazards}（重大 ${k.ehs_major_open_hazards}）` +
        ` | 在职 ${k.hr_active_headcount}人 | 在研 ${k.rnd_active_projects}项 | 在建施工 ${k.construction_active_sites}项`;

      const m = leads.metrics;
      $("funnelSummary").textContent =
        `总商机 ${m.total}（在跟 ${m.open_count}，赢单 ${m.won_count}，丢单 ${m.lost_count}）` +
        ` | 加权预测 ${m.weighted_pipeline_amount}元 | 赢单额 ${m.won_amount}元 | 赢单率 ${m.win_rate_pct}%`;

      fillTable("leadTable", leads.items || [], [
        (r) => r.lead_no,
        (r) => r.customer_name,
        (r) => r.industry_text,
        (r) => r.product_interest,
        (r) => r.est_amount,
        (r) => r.probability,
        (r) => r.weighted_amount,
        (r) => r.stage_text,
        (r) => r.owner_name,
      ]);
      fillTable("intelTable", intel.items || [], [
        (r) => r.intel_no,
        (r) => r.competitor,
        (r) => r.product_category,
        (r) => r.quoted_price,
        (r) => r.region,
        (r) => r.recorded_date,
        (r) => r.summary,
      ]);
      setMsg("已刷新");
    } catch (e) {
      setMsg(e.message);
    }
  }

  $("btnLoad").addEventListener("click", loadAll);
  $("btnExport").addEventListener("click", async () => {
    try {
      const res = await api("/v1/market/report.xlsx");
      const blob = await res.blob();
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = "经营决策与市场商机.xlsx";
      a.click();
      URL.revokeObjectURL(a.href);
      setMsg("已导出经营与市场报表");
    } catch (e) {
      setMsg(e.message);
    }
  });

  const now = new Date();
  $("reportMonth").value = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}`;

  $("btnMonthly").addEventListener("click", async () => {
    const m = $("reportMonth").value.trim();
    setMsg("正在生成月度经营报告...");
    try {
      const q = m ? `?month=${encodeURIComponent(m)}` : "";
      const rep = await (await api(`/v1/market/monthly-report${q}`)).json();
      $("monthlyCard").style.display = "block";
      $("monthlyPreview").textContent = rep.markdown || "";
      setMsg(`已加载 ${rep.month} 经营月报`);
    } catch (e) {
      setMsg(e.message);
    }
  });

  $("btnMonthlyXlsx").addEventListener("click", async () => {
    const m = $("reportMonth").value.trim();
    try {
      const q = m ? `?month=${encodeURIComponent(m)}` : "";
      const res = await api(`/v1/market/monthly-report.xlsx${q}`);
      const blob = await res.blob();
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = `全厂经营月报_${m || "current"}.xlsx`;
      a.click();
      URL.revokeObjectURL(a.href);
      setMsg("已导出全厂月度经营报表 (.xlsx)");
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
