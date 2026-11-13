// 退货单切片服务前端逻辑：原生 JS 轮询，无需构建
const TABLE_LABELS = {
  return_orders: "退货单",
  return_lines: "退货明细",
  orders: "订单",
  order_lines: "订单行",
  customers: "客户",
  products: "商品",
};
const STATUS_LABEL = {
  pending: "排队中",
  running: "执行中",
  success: "成功",
  failed: "失败",
};

async function api(path, options = {}) {
  const resp = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
  return data;
}

function fmtCounts(rowCounts) {
  if (!rowCounts) return '<span class="muted">—</span>';
  return (
    '<div class="counts">' +
    Object.entries(rowCounts)
      .map(
        ([t, n]) =>
          `${TABLE_LABELS[t] || t} <b>${n}</b>`
      )
      .join("　") +
    "</div>"
  );
}

function fmtProgress(job) {
  const p = job.progress || {};
  let text = p.message || "";
  if (p.phase === "traverse" && p.discovered != null) {
    text = `遍历中：已发现 ${p.discovered} 行，待处理 ${p.queued} 行`;
  } else if (p.phase === "copy" && p.table) {
    text = `拷贝中：${TABLE_LABELS[p.table] || p.table} 已拷贝 ${p.table_rows} 行`;
  }
  return text;
}

async function refreshJobs() {
  const jobs = await api("/api/jobs");
  const tbody = document.querySelector("#jobs-table tbody");
  tbody.innerHTML = "";
  for (const job of jobs) {
    const tr = document.createElement("tr");
    const action =
      job.status === "success"
        ? `<a class="dl" href="${job.download}">下载切片</a>`
        : job.status === "failed"
          ? `<span class="muted" title="${job.error || ""}">无残件</span>`
          : '<span class="muted">—</span>';
    tr.innerHTML = `
      <td>${job.id}</td>
      <td>${job.return_order_id}</td>
      <td><span class="badge ${job.status}">${STATUS_LABEL[job.status] || job.status}</span>
          ${job.error ? `<div class="progress-cell">${job.error}</div>` : ""}</td>
      <td class="progress-cell">${fmtProgress(job)}</td>
      <td>${fmtCounts(job.row_counts)}</td>
      <td>${action}</td>`;
    tbody.appendChild(tr);
  }
  const active = jobs.some(
    (j) => j.status === "running" || j.status === "pending"
  );
  return active;
}

async function refreshSource() {
  const data = await api("/api/source");
  const tbody = document.querySelector("#source-table tbody");
  tbody.innerHTML = "";
  if (!data.available) {
    tbody.innerHTML = `<tr><td colspan="3" class="muted">源库不可用：${data.error}</td></tr>`;
    return;
  }
  for (const [table, info] of Object.entries(data.tables)) {
    tbody.innerHTML += `<tr><td>${table}</td><td>${info.label}</td><td>${info.rows}</td></tr>`;
  }
}

async function refreshWriter() {
  const s = await api("/api/demo/status");
  const el = document.getElementById("writer-state");
  el.textContent = `写入器：${s.running ? "运行中" : "停止"}（tick ${s.ticks}）`;
  el.className = "badge " + (s.running ? "running" : "");
}

document.getElementById("job-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const msg = document.getElementById("form-msg");
  msg.textContent = "";
  const id = document.getElementById("return-order-id").value;
  try {
    await api("/api/jobs", {
      method: "POST",
      body: JSON.stringify({ return_order_id: Number(id) }),
    });
    await refreshJobs();
  } catch (err) {
    msg.textContent = err.message;
  }
});

for (const [btn, path] of [
  ["writer-start", "/api/demo/start"],
  ["writer-stop", "/api/demo/stop"],
  ["writer-tick", "/api/demo/tick"],
]) {
  document.getElementById(btn).addEventListener("click", async () => {
    await api(path, { method: "POST" });
    await Promise.all([refreshWriter(), refreshSource()]);
  });
}

let timer = null;
async function tick() {
  let active = false;
  try {
    active = await refreshJobs();
    await refreshSource();
    await refreshWriter();
  } catch (err) {
    console.error(err);
  }
  // 有在途任务时加快轮询
  const interval = active ? 1000 : 3000;
  timer = setTimeout(tick, interval);
}
tick();
