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
      const [ov, sec, aud, bud, ks, us] = await Promise.all([
        api("/v1/ops/tenants").then((r) => r.json()),
        api("/v1/ops/security").then((r) => r.json()),
        api("/v1/ops/audit?limit=20").then((r) => r.json()),
        api("/v1/ops/budget").then((r) => r.json()),
        api("/v1/ops/keys").then((r) => r.json()),
        api("/v1/ops/users").then((r) => r.json()),
      ]);
      const t = ov.totals;
      $("saasSummary").textContent =
        `已接入工厂 ${ov.tenant_count} 家 | 活跃用户 ${t.users_active} | 账本事件 ${t.events_total}` +
        ` | 未闭环待办 ${t.open_todos} | 待审批 ${t.pending_approvals} | 7日 Token ${t.llm_tokens_7d}`;
      $("budgetSummary").textContent =
        `状态：${bud.status}（${bud.reason}） | 今日 ${bud.daily_used}/${bud.daily_limit} (${bud.daily_pct}%)` +
        ` | 本月 ${bud.monthly_used}/${bud.monthly_limit} (${bud.monthly_pct}%)`;

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
      const userTbody = $("userTable").querySelector("tbody");
      userTbody.textContent = "";
      for (const u of us.items || []) {
        const tr = document.createElement("tr");
        for (const val of [u.login, u.display_name, (u.roles || []).join(","), u.is_active ? "启用" : "停用"]) {
          const td = document.createElement("td");
          td.textContent = String(val);
          tr.appendChild(td);
        }
        const actTd = document.createElement("td");
        const btn = document.createElement("button");
        btn.textContent = u.is_active ? "停用" : "启用";
        btn.addEventListener("click", async () => {
          try {
            await api(`/v1/ops/users/${encodeURIComponent(u.login)}`, {
              method: "PATCH",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ is_active: !u.is_active }),
            });
            setMsg(`已更新用户 ${u.login} 状态`);
            loadAll();
          } catch (e) {
            setMsg(e.message);
          }
        });
        actTd.appendChild(btn);
        tr.appendChild(actTd);
        userTbody.appendChild(tr);
      }
      const keyTbody = $("keyTable").querySelector("tbody");
      keyTbody.textContent = "";
      for (const k of ks.items || []) {
        const tr = document.createElement("tr");
        const cells = [
          k.id,
          k.login,
          (k.roles || []).join(","),
          k.label || "",
          k.edge_id || "-",
          k.active ? "有效" : "已吊销",
          (k.last_used_at || "-").slice(0, 19).replace("T", " "),
        ];
        for (const val of cells) {
          const td = document.createElement("td");
          td.textContent = String(val);
          tr.appendChild(td);
        }
        const actTd = document.createElement("td");
        if (k.active) {
          const btn = document.createElement("button");
          btn.textContent = "吊销";
          btn.addEventListener("click", async () => {
            await api(`/v1/ops/keys/${encodeURIComponent(k.id)}/revoke`, { method: "POST" });
            setMsg(`已吊销密钥 ${k.id}`);
            loadAll();
          });
          actTd.appendChild(btn);
        }
        tr.appendChild(actTd);
        keyTbody.appendChild(tr);
      }
      fillTable("auditTable", aud.items || [], [
        (r) => r.seq,
        (r) => r.type,
        (r) => `${r.object_type}:${r.object_id}`,
        (r) => `${r.actor_name} (${r.actor_kind})`,
        (r) => r.data_mode,
        (r) => r.evidence_count,
        (r) => (r.occurred_at || "").slice(0, 19).replace("T", " "),
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
  async function downloadFile(url, fallbackName, okText) {
    try {
      const res = await api(url);
      const blob = await res.blob();
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = fallbackName;
      a.click();
      URL.revokeObjectURL(a.href);
      setMsg(okText);
    } catch (e) {
      setMsg(e.message);
    }
  }
  $("btnAuditXlsx").addEventListener("click", () =>
    downloadFile("/v1/ops/audit.xlsx", "账本审计流水.xlsx", "已导出审计流水 (.xlsx)")
  );
  $("btnBackupJsonl").addEventListener("click", () =>
    downloadFile("/v1/ops/backup.jsonl", "ledger_backup.jsonl", "已下载防篡改账本灾备 (.jsonl)")
  );

  const saved = localStorage.getItem(KEY_STORE);
  if (saved) {
    $("apiKey").value = saved;
    loadAll();
  }
})();
