const $ = (id) => document.getElementById(id);

function getKey() {
  return $("apiKey").value.trim() || localStorage.getItem("fos_key") || "";
}

function setMsg(text, isErr = false) {
  const el = $("msg");
  el.textContent = text;
  el.style.color = isErr ? "#c00" : "#555";
}

async function api(path, opts = {}) {
  const key = getKey();
  if (!key) throw new Error("请先填入 API Key");
  localStorage.setItem("fos_key", key);
  const res = await fetch(path, {
    ...opts,
    headers: { Authorization: `Bearer ${key}`, "Content-Type": "application/json", ...(opts.headers || {}) },
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
    const [ar, ap, vs] = await Promise.all([
      api("/v1/finance/ar"),
      api("/v1/finance/ap"),
      api("/v1/finance/vouchers"),
    ]);
    const s1 = ar.summary;
    $("arSummary").textContent = `共 ${s1.count} 笔 · 应收 ¥${s1.total_amount.toFixed(2)} · 已收 ¥${s1.total_received.toFixed(2)} · 未收 ¥${s1.total_balance.toFixed(2)} · 逾期 ${s1.overdue_count} 笔（¥${s1.overdue_balance.toFixed(2)}）`;
    renderTable("arTable", ar.items, [
      (r) => r.ar_no,
      (r) => r.customer_name,
      (r) => r.order || "—",
      (r) => r.amount.toFixed(2),
      (r) => r.received_amount.toFixed(2),
      (r) => r.balance.toFixed(2),
      (r) => r.due_date || "—",
      (r) => (r.overdue_days > 0 ? `${r.overdue_days} 天` : "—"),
      (r) => r.invoice_no || "—",
      (r) => r.status_text,
    ]);
    const s2 = ap.summary;
    $("apSummary").textContent = `共 ${s2.count} 笔 · 应付 ¥${s2.total_amount.toFixed(2)} · 已付 ¥${s2.total_paid.toFixed(2)} · 未付 ¥${s2.total_balance.toFixed(2)} · 逾期 ${s2.overdue_count} 笔（¥${s2.overdue_balance.toFixed(2)}）`;
    renderTable("apTable", ap.items, [
      (r) => r.ap_no,
      (r) => r.supplier_name,
      (r) => r.purchase_order || "—",
      (r) => r.amount.toFixed(2),
      (r) => r.paid_amount.toFixed(2),
      (r) => r.balance.toFixed(2),
      (r) => r.due_date || "—",
      (r) => (r.overdue_days > 0 ? `${r.overdue_days} 天` : "—"),
      (r) => r.status_text,
    ]);
    renderTable("voucherTable", vs.items, [
      (r) => r.voucher_no,
      (r) => r.voucher_date,
      (r) => r.source_type_text,
      (r) => r.summary,
      (r) => r.total_debit.toFixed(2),
      (r) => r.total_credit.toFixed(2),
      (r) => r.external_system,
      (r) => r.status_text,
    ]);
    setMsg("已刷新");
  } catch (e) {
    setMsg(e.message, true);
  }
}

async function downloadVouchers(target) {
  try {
    const key = getKey();
    const res = await fetch(`/v1/finance/vouchers/export?target=${target}`, {
      headers: { Authorization: `Bearer ${key}` },
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(err.detail || `HTTP ${res.status}`);
    }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `vouchers_${target}.xlsx`;
    a.click();
    URL.revokeObjectURL(url);
    setMsg(`已导出 ${target} 凭证`);
    await loadAll();
  } catch (e) {
    setMsg(e.message, true);
  }
}

$("apiKey").value = localStorage.getItem("fos_key") || "";
$("btnLoad").onclick = loadAll;
$("btnExportKingdee").onclick = () => downloadVouchers("kingdee");
$("btnExportYonyou").onclick = () => downloadVouchers("yonyou");
$("btnRemind").onclick = async () => {
  try {
    const r = await api("/v1/finance/remind", { method: "POST" });
    setMsg(`已生成 ${r.count} 条逾期提醒待办`);
  } catch (e) {
    setMsg(e.message, true);
  }
};
$("btnBatchCost").onclick = async () => {
  const id = $("costBatchId").value.trim();
  if (!id) return setMsg("请填写生产批号", true);
  try {
    const rep = await api(`/v1/finance/cost/batches/${encodeURIComponent(id)}`);
    $("costOut").textContent = JSON.stringify(rep, null, 2);
    setMsg(`已核算批次 ${id}`);
  } catch (e) {
    setMsg(e.message, true);
  }
};
$("btnOrderMargin").onclick = async () => {
  const id = $("costOrderId").value.trim();
  if (!id) return setMsg("请填写销售订单号", true);
  try {
    const rep = await api(`/v1/finance/cost/orders/${encodeURIComponent(id)}`);
    $("costOut").textContent = JSON.stringify(rep, null, 2);
    setMsg(`已核算订单 ${id}`);
  } catch (e) {
    setMsg(e.message, true);
  }
};
if ($("apiKey").value) loadAll();
