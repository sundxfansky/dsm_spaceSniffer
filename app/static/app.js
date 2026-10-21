const state = {
  credentials: null,
  discovery: null,
  result: null,
  activeTab: "treemap",
  jobStatus: "queued",
  pollTimer: null,
  jobId: null,
  pollSequence: 0,
  pollError: false,
  stopRequested: false,
  transmissionServers: [],
  selectedTransmissionIds: new Set(),
  page: 1,
  pageSize: 100,
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

let connectionEdited = false;
async function saveConnection() {
  try {
    await window.connectionStore.save(readCredentials());
    $("#saved-connection-status").textContent = "连接信息（含密码）已加密保存在当前浏览器，下次打开自动填回。";
  } catch (error) {
    $("#saved-connection-status").textContent = `未保存：${error.message}`;
  }
}
$("#connection-form").addEventListener("input", () => {
  connectionEdited = true;
  saveConnection();
});
window.connectionStore.load().then((saved) => {
  if (!saved || connectionEdited) return;
  for (const [id, field] of [["address", "address"], ["ssh-port", "ssh_port"], ["username", "username"], ["password", "password"]]) {
    if (typeof saved[field] === "string" || typeof saved[field] === "number") $("#" + id).value = saved[field];
  }
  $("#saved-connection-status").textContent = "已恢复上次的连接信息，点击连接即可使用。";
}).catch((error) => { $("#saved-connection-status").textContent = `自动恢复不可用：${error.message}`; });

function identityText(identity) {
  if (!identity || identity.uid == null) return "远端身份未确认";
  return `远端用户 ${identity.username}，UID=${identity.uid}${identity.uid === 0 ? "（root）" : "（当前 SSH 会话不是 root；其他终端的 sudo 权限不会继承）"}`;
}

function formatBytes(bytes) {
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  const value = bytes / (1024 ** index);
  return `${value.toLocaleString("zh-CN", { maximumFractionDigits: index > 2 ? 2 : 1 })} ${units[index]}`;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  let payload;
  try { payload = await response.json(); } catch { payload = {}; }
  if (!response.ok) {
    const labels = { large_file_mb: "文件大小阈值", transmission_age_days: "TR 任务年龄", transmission_max_file_mb: "TR 单文件上限", transmission_servers: "Transmission 配置", username: "账户", password: "密码", ssh_port: "SSH 端口", address: "群晖地址", volumes: "存储空间" };
    const detail = Array.isArray(payload.detail)
      ? payload.detail.map((item) => `${labels[item.loc?.[1]] || item.loc?.slice(1).join(".") || "参数"}：${item.msg || "输入无效"}`).join("；")
      : payload.detail;
    throw new Error(typeof detail === "string" ? detail : `请求失败 (${response.status})`);
  }
  return payload;
}

function readCredentials() {
  return {
    address: $("#address").value.trim(),
    ssh_port: Number($("#ssh-port").value),
    username: $("#username").value.trim(),
    password: $("#password").value,
  };
}

function setErrorDetails(id, messages = []) {
  const details = $("#" + id);
  details.classList.toggle("is-hidden", messages.length === 0);
  // Keep the user's expanded/collapsed choice across live result updates.
  if (!messages.length) details.open = false;
  $("#" + id + " summary").textContent = `错误详情（${messages.length} 条，点击展开/收起）`;
  $("#" + id + " pre").textContent = messages.join("\n\n");
}

function setConnectionMessage(message, isError = false) {
  const element = $("#connection-message");
  element.textContent = isError ? "操作未完成，请展开错误详情查看原因。" : message;
  element.classList.toggle("is-error", isError);
  setErrorDetails("connection-error", isError ? [message] : []);
}

function renderVolumes(volumes) {
  $("#volume-grid").innerHTML = volumes.map((volume) => {
    const warning = volume.usage_percent >= 85 ? "is-warning" : "";
    return `
      <label class="volume-card ${warning}">
        <input type="checkbox" name="volume" value="${escapeHtml(volume.path)}" checked>
        <div>
          <strong>${escapeHtml(volume.path)}</strong>
          <div class="volume-meta">${escapeHtml(volume.filesystem)}</div>
          <div class="usage-track"><span style="width:${Math.min(volume.usage_percent, 100)}%"></span></div>
          <div class="usage-copy"><span>已用 ${formatBytes(volume.used_bytes)}</span><span>${volume.usage_percent}% / ${formatBytes(volume.total_bytes)}</span></div>
        </div>
      </label>`;
  }).join("");
}

function transmissionTemplate(server, index) {
  const value = (key) => escapeHtml(server[key] ?? "");
  return `<div class="transmission-card" data-index="${index}">
    <label class="field"><span>名称</span><input data-key="name" value="${value("name")}" placeholder="客厅 TR"></label>
    <label class="field"><span>RPC 地址</span><input data-key="address" value="${value("address")}" placeholder="http://tr.local"></label>
    <label class="field"><span>端口</span><input data-key="port" type="number" min="1" max="65535" value="${value("port")}"></label>
    <button type="button" class="secondary-button remove-transmission" data-remove="${index}">移除</button>
    <label class="field"><span>账户（可选）</span><input data-key="username" value="${value("username")}" autocomplete="username"></label>
    <label class="field"><span>密码（可选）</span><input data-key="password" type="password" value="${value("password")}" autocomplete="current-password"></label>
    <label class="field"><span>TR 下载根目录</span><input data-key="download_root" value="${escapeHtml(server.download_root || "/downloads")}" placeholder="/downloads"></label>
    <div class="transmission-status" data-status>${server.statusText || "未测试连接"}</div>
    <div class="transmission-mappings" data-mappings>${mappingsHtml({download_root: server.download_root})}</div>
  </div>`;
}

// Mappings arrive with every live snapshot; until the scan ends an unfound
// entry only means it has not been matched yet.
function mappingsHtml(connection) {
  const root = escapeHtml(connection.download_root || "/downloads");
  const rows = connection.path_mappings;
  if (!rows) return `等待扫描，将在 ${root} 下各一级目录与 DSM 路径之间实时匹配`;
  if (!rows.length) return `${root} 下没有任务`;
  const final = Boolean(connection.mappings_final);
  const found = rows.filter((row) => row.dsm_path).length;
  const header = `<strong>当前挂载目录（${final ? "扫描完成" : "实时更新"}）：已定位 ${found} / ${rows.length}</strong>`;
  return header + rows.map((row) => row.dsm_path
    ? `<span>${escapeHtml(row.tr_path)} → ${escapeHtml(row.dsm_path)} <small>${row.matched_files} 个文件匹配</small></span>`
    : `<span class="${final ? "is-missing" : "is-pending"}">${escapeHtml(row.tr_path)} → ${final ? "未找到" : "查找中…"}</span>`).join("");
}

function renderTransmissionServers() {
  const list = $("#transmission-list");
  if (!list) return;
  list.innerHTML = state.transmissionServers.length
    ? state.transmissionServers.map(transmissionTemplate).join("")
    : `<div class="transmission-empty">尚未添加 Transmission。没有配置时只执行群晖文件扫描。</div>`;
}

function readTransmissionServers() {
  return [...document.querySelectorAll(".transmission-card")].map((card, index) => {
    const row = {...state.transmissionServers[index]};
    card.querySelectorAll("[data-key]").forEach((input) => {
      row[input.dataset.key] = input.dataset.key === "port" ? Number(input.value) : input.value.trim();
    });
    row.enabled = true;
    return row;
  });
}

async function testTransmission(index) {
  const server = readTransmissionServers()[index];
  if (!server) return;
  const status = document.querySelector(`.transmission-card[data-index="${index}"] [data-status]`);
  if (status) status.textContent = "连接中…";
  try {
    const result = await api("/api/transmission/test", {method: "POST", body: JSON.stringify(server)});
    state.transmissionServers[index] = {...server, statusText: `在线 · ${result.torrent_count} 个任务${result.version ? ` · ${result.version}` : ""}`};
    if (status) { status.textContent = state.transmissionServers[index].statusText; status.classList.add("is-online"); status.classList.remove("is-error"); }
  } catch (error) {
    state.transmissionServers[index] = {...server, statusText: `连接失败：${error.message}`};
    if (status) { status.textContent = state.transmissionServers[index].statusText; status.classList.add("is-error"); status.classList.remove("is-online"); }
  }
}

$("#add-transmission").addEventListener("click", () => {
  state.transmissionServers = readTransmissionServers();
  state.transmissionServers.push({name: `Transmission ${state.transmissionServers.length + 1}`, address: "", port: 9091, username: "", password: "", rpc_path: "/transmission/rpc", download_root: "/downloads", enabled: true});
  renderTransmissionServers();
});

$("#test-transmissions").addEventListener("click", async () => {
  state.transmissionServers = readTransmissionServers();
  if (!state.transmissionServers.length) { $("#transmission-message").textContent = "请先添加 Transmission"; return; }
  $("#transmission-message").textContent = "正在测试连接…";
  renderTransmissionServers();
  await Promise.all(state.transmissionServers.map((_, index) => testTransmission(index)));
  $("#transmission-message").textContent = "连接状态已更新";
});

$("#transmission-list").addEventListener("click", (event) => {
  const remove = event.target.closest?.("[data-remove]");
  if (remove) {
    state.transmissionServers = readTransmissionServers().filter((_, index) => index !== Number(remove.dataset.remove));
    renderTransmissionServers();
  }
});

renderTransmissionServers();

$("#connection-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = $("#connect-button");
  button.disabled = true;
  setConnectionMessage("正在建立 SSH 连接...");
  try {
    state.credentials = readCredentials();
    await saveConnection();
    state.discovery = await api("/api/discover", { method: "POST", body: JSON.stringify(state.credentials) });
    renderVolumes(state.discovery.volumes);
    $("#fingerprint").textContent = `SSH 指纹 ${state.discovery.server_fingerprint}`;
    $("#workspace").classList.remove("is-hidden");
    $("#connection-state").classList.add("is-online");
    $("#connection-state span").textContent = `${state.discovery.host} 已连接`;
    setConnectionMessage(`已发现 ${state.discovery.volumes.length} 个存储空间。${identityText(state.discovery.identity)}`);
  } catch (error) {
    state.discovery = null;
    $("#workspace").classList.add("is-hidden");
    $("#connection-state").classList.remove("is-online");
    $("#connection-state span").textContent = "连接失败";
    setConnectionMessage(error.message, true);
  } finally {
    button.disabled = false;
  }
});

$("#toggle-password").addEventListener("click", () => {
  const input = $("#password");
  const showing = input.type === "text";
  input.type = showing ? "password" : "text";
  $("#toggle-password").textContent = showing ? "显示" : "隐藏";
  $("#toggle-password").setAttribute("aria-label", showing ? "显示密码" : "隐藏密码");
});

$("#analyze-button").addEventListener("click", async () => {
  if (!$("#connection-form").reportValidity() || !$("#large-file-mb").reportValidity() || !$("#tr-age-days").reportValidity() || !$("#tr-max-file-mb").reportValidity()) return;
  const volumes = $$("input[name='volume']:checked").map((input) => input.value);
  if (!volumes.length) {
    setConnectionMessage("请至少选择一个存储空间", true);
    return;
  }
  const button = $("#analyze-button");
  button.disabled = true;
  clearTimeout(state.pollTimer);
  state.pollSequence += 1;
  state.jobId = null;
  state.stopRequested = false;
  state.pollError = false;
  setErrorDetails("scan-error");
  setErrorDetails("scan-warnings");
  $("#stop-button").disabled = true;
  $("#stop-button").textContent = "停止分析";
  $("#connect-button").disabled = true;
  state.result = null;
  window.spaceMap.reset();
  state.page = 1;
  state.jobStatus = "queued";
  $("#results").classList.add("is-hidden");
  $("#progress-panel").classList.remove("is-hidden");
  updateProgress(0, "创建扫描任务");
  $("#progress-panel p").textContent = "扫描时间取决于文件数量。分析期间请保持群晖在线。";
  try {
    state.transmissionServers = readTransmissionServers();
    const request = {
      ...readCredentials(),
      volumes,
      large_file_mb: Number($("#large-file-mb").value),
      transmission_servers: state.transmissionServers,
      transmission_age_days: Number($("#tr-age-days").value),
      transmission_max_file_mb: Number($("#tr-max-file-mb").value),
    };
    const created = await api("/api/jobs", { method: "POST", body: JSON.stringify(request) });
    state.jobId = created.job_id;
    $("#stop-button").disabled = false;
    pollJob(created.job_id);
  } catch (error) {
    button.disabled = false;
    $("#connect-button").disabled = false;
    showScanError(error.message);
  }
});

$("#stop-button").addEventListener("click", async () => {
  if (!state.jobId || state.stopRequested) return;
  const jobId = state.jobId;
  state.stopRequested = true;
  setErrorDetails("scan-error");
  clearTimeout(state.pollTimer);
  state.pollSequence += 1;
  $("#stop-button").disabled = true;
  $("#stop-button").textContent = "正在停止…";
  $("#progress-panel p").textContent = "正在请求停止；已读取结果会保留。连接建立阶段可能需要等待连接超时。";
  try {
    await api(`/api/jobs/${jobId}/cancel`, { method: "POST" });
  } catch (error) {
    state.stopRequested = false;
    $("#stop-button").disabled = false;
    $("#stop-button").textContent = "重试停止";
    $("#progress-panel p").textContent = "停止请求未确认，请展开错误详情并重试。";
    setErrorDetails("scan-error", [error.message]);
  }
  if (state.jobId === jobId) pollJob(jobId);
});

function updateProgress(value, stage) {
  $("#progress-value").textContent = `${value}%`;
  $("#progress-bar").style.width = `${value}%`;
  $("#progress-stage").textContent = stage;
}

function showScanError(message) {
  $("#progress-stage").textContent = "分析未完成，请查看错误详情";
  setErrorDetails("scan-error", [message]);
  $("#progress-value").textContent = "失败";
  $("#progress-panel p").textContent = "请根据错误提示调整后重试；当前没有生成完整结果。";
}

async function pollJob(jobId) {
  clearTimeout(state.pollTimer);
  const sequence = ++state.pollSequence;
  try {
    const job = await api(`/api/jobs/${jobId}`);
    if (sequence !== state.pollSequence) return;
    if (state.pollError) {
      state.pollError = false;
      setErrorDetails("scan-error");
      $("#progress-panel p").textContent = state.stopRequested ? "正在停止分析，已读取结果会保留。" : "连接已恢复，扫描结果持续更新。";
    }
    state.jobStatus = job.status;
    const terminal = ["completed", "failed", "cancelled"].includes(job.status);
    $("#stop-button").disabled = terminal || job.status === "cancelling" || state.stopRequested;
    if (job.status === "cancelling") $("#stop-button").textContent = "正在停止…";
    if (terminal) {
      $("#analyze-button").disabled = false;
      $("#connect-button").disabled = false;
      $("#stop-button").textContent = job.status === "cancelled" ? "已停止" : "停止分析";
    }
    updateProgress(job.progress, job.stage);
    if (job.result) {
      state.result = job.result;
      renderResults();
    }
    if (job.status === "completed") {
      $("#analyze-button").disabled = false;
      return;
    }
    if (job.status === "cancelled") {
      setErrorDetails("scan-error");
      $("#progress-value").textContent = "已停止";
      $("#progress-panel p").textContent = "分析已停止，已读取的部分结果仍可查看和导出；可重新开始分析。";
      return;
    }
    if (job.status === "failed") {
      $("#analyze-button").disabled = false;
      showScanError(job.error || "分析失败");
      return;
    }
    state.pollTimer = setTimeout(() => pollJob(jobId), 1200);
  } catch (error) {
    if (sequence !== state.pollSequence) return;
    state.pollError = true;
    $("#progress-panel p").textContent = "暂时无法读取任务状态，请查看错误详情。正在重试，任务可能仍在运行。";
    setErrorDetails("scan-error", [error.message]);
    state.pollTimer = setTimeout(() => pollJob(jobId), 2000);
  }
}

function renderResults() {
  const result = state.result;
  const used = result.volumes.reduce((sum, volume) => sum + volume.used_bytes, 0);
  const total = result.volumes.reduce((sum, volume) => sum + volume.total_bytes, 0);
  const shared = result.hardlinks.reduce((sum, group) => sum + group.shared_bytes, 0);
  $("#summary-strip").innerHTML = `
    <div class="summary-item"><span>扫描空间</span><strong>${result.volumes.length}</strong></div>
    <div class="summary-item"><span>总容量</span><strong>${formatBytes(total)}</strong></div>
    <div class="summary-item"><span>已用容量</span><strong>${formatBytes(used)}</strong></div>
    <div class="summary-item"><span>硬链接共享数据</span><strong>${formatBytes(shared)}</strong></div>
    <div class="summary-item"><span>TR 清理候选</span><strong>${result.transmission_candidates_total || 0}</strong></div>`;
  $("#directory-count").textContent = result.directories.length;
  $("#large-count").textContent = result.large_files_total;
  $("#hardlink-count").textContent = result.hardlink_groups_total;
  $("#transmission-count").textContent = result.transmission_candidates_total || 0;
  (result.transmission_connections || []).forEach((connection, index) => {
    const status = document.querySelector(`.transmission-card[data-index="${index}"] [data-status]`);
    if (!status) return;
    status.textContent = connection.connected
      ? `在线 · ${connection.torrent_count || 0} 个任务`
      : `连接失败：${connection.error || "未连接"}`;
    status.classList.toggle("is-online", Boolean(connection.connected));
    status.classList.toggle("is-error", !connection.connected);
    const mappings = status.parentElement.querySelector("[data-mappings]");
    if (mappings && connection.connected) mappings.innerHTML = mappingsHtml(connection);
  });
  $("#large-directory-count").textContent = result.large_directories_total || 0;
  const warnings = result.warnings || [];
  $("#scan-notice").classList.toggle("is-error", warnings.length > 0);
  $("#scan-notice").textContent = [
    state.jobStatus === "completed" ? "扫描结束。" : state.jobStatus === "cancelled" ? "分析已停止，以下为已读取的部分结果，不代表完整扫描。" : state.jobStatus === "cancelling" ? "正在停止分析，以下为已读取的部分结果。" : state.jobStatus === "failed" ? "扫描中断，以下为已读取的部分结果。" : "正在扫描：结果持续更新，文件夹大小为当前已读累计值，尚未扫描的内容未计入。",
    `已读取 ${result.scanned_files ?? "未知"} 个普通文件；列出大小 ≥ ${result.settings.large_file_mb} MiB 的 ${result.large_files_total} 个文件，硬链接 ${result.hardlink_groups_total} 组。`,
    `达到阈值的文件夹 ${result.large_directories_total || 0} 个（含多层子目录）。`,
    `Transmission：${(result.transmission_connections || []).filter((row) => row.connected).length} / ${(result.transmission_connections || []).length} 个连接在线，符合条件的清理候选 ${result.transmission_candidates_total || 0} 个。`,
    result.scope || "",
    identityText(result.identity),
    warnings.length ? `结果不完整：共 ${warnings.length} 条错误/警告，请展开错误详情查看原因（不一定是权限问题）。` : "",
  ].filter(Boolean).join("\n");
  setErrorDetails("scan-warnings", warnings);
  $("#progress-panel").classList.toggle("is-hidden", state.jobStatus === "completed");
  $("#results").classList.remove("is-hidden");
  renderActiveTable();
}

function filteredRows(rows, search, pathExtractor) {
  const matches = search ? rows.filter((row) => pathExtractor(row).toLowerCase().includes(search)) : rows;
  const pages = Math.max(1, Math.ceil(matches.length / state.pageSize));
  state.page = Math.min(state.page, pages);
  $("#page-status").textContent = `第 ${state.page} / ${pages} 页 · 匹配 ${matches.length} / 全部 ${rows.length} 条`;
  $("#previous-page").disabled = state.page <= 1;
  $("#next-page").disabled = state.page >= pages;
  return matches.slice((state.page - 1) * state.pageSize, state.page * state.pageSize);
}

function renderActiveTable() {
  if (!state.result) return;
  const search = $("#result-search").value.trim().toLowerCase();
  const isMap = state.activeTab === "treemap";
  $("#space-map").classList.toggle("is-hidden", !isMap);
  $("#result-content").classList.toggle("is-hidden", isMap);
  $("#table-pagination").classList.toggle("is-hidden", isMap);
  $("#result-search").placeholder = isMap ? "高亮当前层级路径（不改变面积）" : "筛选路径";
  if (isMap) {
    window.spaceMap.update(state.result, state.jobStatus, search, state.jobId);
    return;
  }
  let html = "";
  if (state.activeTab === "transmission") {
    const allRows = state.result.transmission_candidates || [];
    const rows = filteredRows(allRows, search, (row) => `${row.server_name} ${row.task_name} ${row.dsm_path} ${(row.hardlink_paths || []).join(" ")}`.toLowerCase());
    const cleaned = new Set(state.result.transmission_cleaned_ids || []);
    html = tableOrEmpty(rows, `
      <table><thead><tr><th></th><th>Transmission 任务</th><th>单文件</th><th>匹配路径 / 硬链接</th><th class="number-cell">大小 / 年龄</th><th>操作</th></tr></thead><tbody>
      ${rows.map((row) => `<tr><td><input class="cleanup-checkbox" type="checkbox" data-cleanup-select="${escapeHtml(row.id)}" ${state.selectedTransmissionIds.has(row.id) ? "checked" : ""} ${cleaned.has(row.id) ? "disabled" : ""}></td><td><span class="volume-pill">${escapeHtml(row.server_name)}</span><br>${escapeHtml(row.task_name || "未命名任务")}<br><small>${escapeHtml(row.completed_at || "")}</small></td><td class="path-cell">${escapeHtml(row.file_name || "")}</td><td class="cleanup-paths"><strong>${escapeHtml(row.dsm_path || "")}</strong>${(row.hardlink_paths || []).map((path) => `<small>${escapeHtml(path)}</small>`).join("")}</td><td class="number-cell">${formatBytes(row.size_bytes)}<br>${row.age_days} 天前</td><td>${cleaned.has(row.id) ? `<span class="cleanup-done">已处理</span>` : ""}</td></tr>`).join("")}
      </tbody></table>`);
    const selected = [...state.selectedTransmissionIds].filter((id) => !cleaned.has(id));
    if (selected.length) html += `<div class="result-toolbar"><span>已选择 ${selected.length} 项：将删除 TR 任务、下载文件及全部硬链接，并为每个文件创建 .rmed 标记。</span><button class="secondary-button cleanup-action" id="cleanup-selected" type="button">确认删除所选</button></div>`;
  } else if (state.activeTab === "large-directories") {
    const rows = filteredRows(state.result.large_directories || [], search, (row) => row.path);
    html = tableOrEmpty(rows, `
      <table><thead><tr><th>存储空间</th><th>文件夹（含子目录）</th><th class="number-cell">累计逻辑大小</th><th class="number-cell">文件数</th></tr></thead><tbody>
      ${rows.map((row) => `<tr><td><span class="volume-pill">${escapeHtml(row.volume)}</span></td><td class="path-cell">${escapeHtml(row.path)}</td><td class="number-cell">${formatBytes(row.size_bytes)}</td><td class="number-cell">${row.file_count}</td></tr>`).join("")}
      </tbody></table>`);
  } else if (state.activeTab === "directories") {
    const rows = filteredRows(state.result.directories, search, (row) => row.path);
    html = tableOrEmpty(rows, `
      <table><thead><tr><th>存储空间</th><th>文件夹</th><th class="number-cell">占用</th></tr></thead><tbody>
      ${rows.map((row) => `<tr><td><span class="volume-pill">${escapeHtml(row.volume)}</span></td><td class="path-cell">${escapeHtml(row.path)}</td><td class="number-cell">${formatBytes(row.size_bytes)}</td></tr>`).join("")}
      </tbody></table>`);
  } else if (state.activeTab === "large-files") {
    const rows = filteredRows(state.result.large_files, search, (row) => row.path);
    html = tableOrEmpty(rows, `
      <table><thead><tr><th>存储空间</th><th>文件</th><th class="number-cell">大小</th><th class="number-cell">链接数</th></tr></thead><tbody>
      ${rows.map((row) => `<tr><td><span class="volume-pill">${escapeHtml(row.volume)}</span></td><td class="path-cell">${escapeHtml(row.path)}</td><td class="number-cell">${formatBytes(row.size_bytes)}</td><td class="number-cell">${row.link_count}</td></tr>`).join("")}
      </tbody></table>`);
  } else {
    const rows = filteredRows(state.result.hardlinks, search, (row) => row.paths.join(" "));
    html = tableOrEmpty(rows, `
      <table><thead><tr><th>存储空间</th><th>同一 inode 的路径</th><th class="number-cell">文件大小</th><th class="number-cell">已找到链接</th></tr></thead><tbody>
      ${rows.map((row) => `<tr><td><span class="volume-pill">${escapeHtml(row.volume)}</span></td><td class="path-cell"><div class="hardlink-paths">${row.paths.map((path) => `<span>${escapeHtml(path)}</span>`).join("")}</div></td><td class="number-cell">${formatBytes(row.size_bytes)}</td><td class="number-cell">${row.paths.length} / ${row.link_count}</td></tr>`).join("")}
      </tbody></table>`);
  }
  $("#result-content").innerHTML = html;
}

function tableOrEmpty(rows, table) {
  return rows.length ? table : `<div class="empty-state">${state.jobStatus === "running" || state.jobStatus === "queued" ? "扫描进行中，暂未读取到符合条件的结果" : "没有符合条件的结果"}</div>`;
}

$$('.tab').forEach((tab) => tab.addEventListener("click", () => {
  $$(".tab").forEach((item) => item.classList.remove("is-active"));
  tab.classList.add("is-active");
  state.activeTab = tab.dataset.tab;
  state.page = 1;
  renderActiveTable();
}));

$("#result-search").addEventListener("input", () => { state.page = 1; renderActiveTable(); });
$("#result-content").addEventListener("change", (event) => {
  const checkbox = event.target.closest?.("[data-cleanup-select]");
  if (!checkbox) return;
  if (checkbox.checked) state.selectedTransmissionIds.add(checkbox.dataset.cleanupSelect);
  else state.selectedTransmissionIds.delete(checkbox.dataset.cleanupSelect);
  renderActiveTable();
});
$("#result-content").addEventListener("click", async (event) => {
  if (!event.target.closest?.("#cleanup-selected")) return;
  const ids = [...state.selectedTransmissionIds];
  if (!ids.length || !window.confirm(`确认删除 ${ids.length} 项？将删除 Transmission 任务、文件、全部硬链接，并创建 .rmed 标记。`)) return;
  try {
    const job = await api(`/api/jobs/${state.jobId}/cleanup`, {method: "POST", body: JSON.stringify({candidate_ids: ids, delete_files: true, delete_transmission_tasks: true, create_rmed: true})});
    state.result = job.result;
    state.selectedTransmissionIds.clear();
    renderResults();
    $("#transmission-message").textContent = (job.result.transmission_cleanup?.warnings || []).join("；") || "清理完成";
  } catch (error) {
    $("#transmission-message").textContent = `清理失败：${error.message}`;
  }
});
$("#previous-page").addEventListener("click", () => { state.page = Math.max(1, state.page - 1); renderActiveTable(); });
$("#next-page").addEventListener("click", () => { state.page += 1; renderActiveTable(); });

$("#export-button").addEventListener("click", () => {
  if (!state.result) return;
  const blob = new Blob([JSON.stringify(state.result, null, 2)], { type: "application/json" });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = `dsm-storage-${state.jobStatus === "completed" ? "" : "partial-"}${new Date().toISOString().slice(0, 10)}.json`;
  link.click();
  URL.revokeObjectURL(link.href);
});
