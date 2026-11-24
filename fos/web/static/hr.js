(function () {
  const $ = (id) => document.getElementById(id);
  const KEY_STORE = "fos_api_key";

  function apiKey() {
    const v = $("apiKey").value.trim();
    if (v) localStorage.setItem(KEY_STORE, v);
    return v || localStorage.getItem(KEY_STORE) || "";
  }

  function curMonth() {
    const v = $("month").value.trim();
    if (v) return v;
    const d = new Date();
    const m = String(d.getMonth() + 1).padStart(2, "0");
    return `${d.getFullYear()}-${m}`;
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
    const m = curMonth();
    $("month").value = m;
    setMsg("加载中...");
    try {
      const me = await (await api("/v1/me")).json();
      $("who").textContent = `${me.tenant_name} · ${me.display_name}`;
      const [pay, certs, emps, rst] = await Promise.all([
        api(`/v1/hr/payroll?month=${encodeURIComponent(m)}`).then((r) => r.json()),
        api("/v1/hr/certs").then((r) => r.json()),
        api("/v1/hr/employees").then((r) => r.json()),
        api(`/v1/hr/rosters?month=${encodeURIComponent(m)}`).then((r) => r.json()),
      ]);
      const t = pay.totals;
      $("paySummary").textContent =
        `${pay.month} 共 ${t.headcount} 人 | 基本折算 ${t.base_pay} 元 | 加班费 ${t.overtime_pay} 元 | ` +
        `计件工资 ${t.piece_pay} 元 | 应发合计 ${t.gross_pay} 元`;

      fillTable("payTable", pay.items || [], [
        (r) => r.emp_no,
        (r) => r.name,
        (r) => r.department,
        (r) => r.present_days,
        (r) => r.overtime_hours,
        (r) => r.piece_qty,
        (r) => r.base_pay,
        (r) => r.overtime_pay,
        (r) => r.piece_pay,
        (r) => r.gross_pay,
      ]);
      fillTable("certTable", certs.items || [], [
        (r) => r.cert_no,
        (r) => r.employee_name,
        (r) => r.kind_text,
        (r) => r.issued_date,
        (r) => r.expires_date,
        (r) => r.cert_state_text,
      ]);
      fillTable("empTable", emps.items || [], [
        (r) => r.emp_no,
        (r) => r.name,
        (r) => r.department,
        (r) => r.position,
        (r) => r.shift_group,
        (r) => r.base_salary,
        (r) => r.status_text,
      ]);
      fillTable("rstTable", rst.items || [], [
        (r) => r.work_date,
        (r) => r.shift_text,
        (r) => r.employee_name,
        (r) => r.attendance_text,
        (r) => r.hours,
        (r) => r.overtime_hours,
        (r) => r.piece_qty,
      ]);
      setMsg("已刷新");
    } catch (e) {
      setMsg(e.message);
    }
  }

  $("btnLoad").addEventListener("click", loadAll);
  $("btnRemind").addEventListener("click", async () => {
    try {
      const r = await (await api("/v1/hr/certs/remind", { method: "POST" })).json();
      setMsg(`已生成 ${r.created} 条特种证照到期提醒待办`);
    } catch (e) {
      setMsg(e.message);
    }
  });
  $("btnExport").addEventListener("click", async () => {
    try {
      const m = curMonth();
      const res = await api(`/v1/hr/report.xlsx?month=${encodeURIComponent(m)}`);
      const blob = await res.blob();
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = `人力排班与薪酬_${m}.xlsx`;
      a.click();
      URL.revokeObjectURL(a.href);
      setMsg("已导出人力与薪酬报表");
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
