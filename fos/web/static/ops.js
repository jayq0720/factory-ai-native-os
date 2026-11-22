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
      const [ov, sec] = await Promise.all([
        api("/v1/ops/tenants").then((r) => r.json()),
        api("/v1/ops/security").then((r) => r.json()),
      ]);
      const t = ov.totals;
      $("saasSummary").textContent =
        `已接入工厂 ${ov.tenant_count} 家 | 活跃用户 ${t.users_active} | 账本事件 ${t.events_total}` +
        ` | 未闭环待办 ${t.open_todos} | 待审批 ${t.pending_approvals} | 7日 Token ${t.llm_tokens_7d}`;

      fillTable("tenantTable", ov.tenants || [], [
        (r) => r.tenant_id,
        (r) => r.name,
        (r) => r.industry_pack,
        (r) => (r.enabled_modules || []).length,
        (r) => r.counts.users_active,
        (r) => r.counts.events_total,
        (r) => r.counts.open_todos,
        (r) => r.counts.pending_approvals,
        (r) => r.counts.llm_tokens_7d,
        (r) => (r.security_passed ? "通过" : "异常"),
      ]);
      fillTable("secTable", sec.checks || [], [
        (r) => r.name,
        (r) => (r.ok ? "通过" : "未通过"),
        (r) => r.severity,
        (r) => r.detail,
      ]);
      setMsg("已刷新");
    } catch (e) {
      setMsg(e.message);
    }
  }

  $("btnLoad").addEventListener("click", loadAll);
  $("btnWatchdog").addEventListener("click", async () => {
    try {
      const r = await (await api("/v1/ops/watchdog/run", { method: "POST" })).json();
      setMsg(
        `巡检完成：新建待办 ${r.total_todos_created} 条，清理超时审批 ${r.expired_approvals} 条`
      );
      loadAll();
    } catch (e) {
      setMsg(e.message);
    }
  });
  $("btnEval").addEventListener("click", async () => {
    setMsg("正在执行黄金评测...");
    try {
      const r = await (await api("/v1/ops/eval")).json();
      const status = r.all_passed ? "100% 全部通过" : `存在 ${r.failed} 条 Badcase`;
      $("evalSummary").textContent =
        `数据集 ${r.suite} | 总计 ${r.total} 条 | 通过 ${r.passed} | 失败 ${r.failed} | 通过率 ${r.pass_rate_pct}%（${status}）`;
      const rows = Object.entries(r.categories || {}).map(([cat, s]) => ({
        cat,
        total: s.total,
        passed: s.passed,
        failed: s.failed,
      }));
      fillTable("evalTable", rows, [
        (x) => x.cat,
        (x) => x.total,
        (x) => x.passed,
        (x) => x.failed,
      ]);
      setMsg(`黄金评测完成：${status}`);
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
