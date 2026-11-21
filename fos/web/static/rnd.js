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
      const [projs, forms, trials] = await Promise.all([
        api("/v1/rnd/projects").then((r) => r.json()),
        api("/v1/rnd/formulas").then((r) => r.json()),
        api("/v1/rnd/trials").then((r) => r.json()),
      ]);
      const pc = projs.counts;
      const fc = forms.counts;
      const tc = trials.counts;
      $("projSummary").textContent =
        `研发项目 ${pc.total}（研发中 ${pc.active}，已量产 ${pc.released}）` +
        ` | 配方版本 ${fc.total}（试验中 ${fc.testing}，已定型 ${fc.approved}）` +
        ` | 试验 ${tc.total} 次（合格率 ${tc.pass_rate}%）`;

      fillTable("projTable", projs.items || [], [
        (r) => r.project_no,
        (r) => r.name,
        (r) => r.target_product,
        (r) => r.target_al2o3_min,
        (r) => r.target_bd_min,
        (r) => r.target_ccs_min,
        (r) => r.owner_name,
        (r) => r.due_date,
        (r) => r.status_text,
      ]);
      fillTable("frmTable", forms.items || [], [
        (r) => r.formula_no,
        (r) => r.project_name,
        (r) => r.version_code,
        (r) => r.total_pct,
        (r) => r.calc_al2o3_pct,
        (r) => r.est_cost_per_ton,
        (r) => r.firing_temp_c,
        (r) => r.status_text,
      ]);
      fillTable("trlTable", trials.items || [], [
        (r) => r.trial_no,
        (r) => r.formula_code,
        (r) => r.stage_text,
        (r) => r.trial_date,
        (r) => r.al2o3_pct,
        (r) => r.bulk_density,
        (r) => r.ccs_mpa,
        (r) => (r.passed ? "达标" : "不达标"),
        (r) => r.conclusion,
      ]);
      setMsg("已刷新");
    } catch (e) {
      setMsg(e.message);
    }
  }

  $("btnLoad").addEventListener("click", loadAll);
  $("btnExport").addEventListener("click", async () => {
    try {
      const res = await api("/v1/rnd/report.xlsx");
      const blob = await res.blob();
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = "研发配方与试验.xlsx";
      a.click();
      URL.revokeObjectURL(a.href);
      setMsg("已导出研发配方报表");
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
