/* Local-only treemap: no CDN, uploads, or NAS operations. */
(function (global) {
  "use strict";
  const parentPath = path => path.slice(0, path.lastIndexOf("/")) || "/";
  const nameOf = path => path.slice(path.lastIndexOf("/") + 1) || "全部存储空间";
  function kind(node) {
    if (node.type !== "file") return node.type;
    if (/\.(mkv|mp4|avi|mov|ts|m2ts|webm)$/i.test(node.path)) return "video";
    if (/\.(zip|rar|7z|tar|gz|iso|img)$/i.test(node.path)) return "archive";
    return "file";
  }
  const sorted = rows => rows.sort((a, b) => b.size - a.size || a.id.localeCompare(b.id));
  function buildTree(result) {
    const root = {id: "/", path: "/", name: "全部存储空间", type: "folder", size: 0, count: 0, children: []};
    const nodes = new Map([[root.id, root]]);
    for (const row of result.tree_directories || []) {
      nodes.set(row.path, {id: row.path, path: row.path, name: nameOf(row.path), type: "folder",
        size: row.size_bytes, count: row.file_count, directSize: row.direct_size_bytes,
        directCount: row.direct_file_count, listedSize: 0, listedCount: 0, children: []});
    }
    for (const node of nodes.values()) {
      if (node === root) continue;
      const parent = nodes.get(parentPath(node.path));
      if (parent) { node.parent = parent.id; parent.children.push(node); }
    }
    const files = new Map([...(result.tree_file_previews || []), ...(result.large_files || [])].map(row => [row.path, row]));
    for (const row of files.values()) {
      const parent = nodes.get(parentPath(row.path));
      if (!parent) continue;
      const node = {id: "file:" + row.path, parent: parent.id, path: row.path, name: nameOf(row.path),
        type: "file", size: row.size_bytes, count: 1, links: row.link_count, children: []};
      nodes.set(node.id, node);
      parent.children.push(node);
      parent.listedSize += node.size;
      parent.listedCount += 1;
    }
    for (const node of [...nodes.values()]) {
      if (node === root || node.type !== "folder") continue;
      const size = Math.max(0, node.directSize - node.listedSize);
      const count = Math.max(0, node.directCount - node.listedCount);
      if (count > 0) {
        const rest = {id: "small:" + node.id, parent: node.id, path: node.path, name: `其余文件（${count} 个，点击展开）`,
          type: "aggregate", size, count, children: []};
        node.children.push(rest);
        nodes.set(rest.id, rest);
      }
      sorted(node.children);
    }
    sorted(root.children);
    root.size = root.children.reduce((sum, n) => sum + n.size, 0);
    root.count = root.children.reduce((sum, n) => sum + n.count, 0);
    return {root, nodes};
  }

  // Squarified layout. Inputs are exclusive siblings, never recursive totals added together.
  function layout(items, width, height) {
    const total = items.reduce((sum, n) => sum + Math.max(0, n.size), 0);
    if (!total || width <= 0 || height <= 0) return [];
    const pending = sorted(items.filter(n => n.size > 0).slice()).map(node => ({node, area: node.size / total * width * height}));
    const output = [];
    let x = 0, y = 0, w = width, h = height, row = [];
    function worst(list, edge) {
      const sum = list.reduce((s, item) => s + item.area, 0);
      const min = list[list.length - 1].area, max = list[0].area;
      return Math.max(edge * edge * max / (sum * sum), sum * sum / (edge * edge * min));
    }
    function place() {
      const sum = row.reduce((s, item) => s + item.area, 0);
      const vertical = w >= h;
      const thickness = sum / (vertical ? h : w);
      let offset = 0;
      for (const item of row) {
        const length = item.area / thickness;
        output.push({node: item.node, x: x + (vertical ? 0 : offset), y: y + (vertical ? offset : 0),
          width: vertical ? thickness : length, height: vertical ? length : thickness});
        offset += length;
      }
      if (vertical) { x += thickness; w = Math.max(0, w - thickness); }
      else { y += thickness; h = Math.max(0, h - thickness); }
      row = [];
    }
    for (const item of pending) {
      const edge = Math.min(w, h);
      if (row.length && worst([...row, item], edge) > worst(row, edge)) place();
      row.push(item);
    }
    if (row.length) place();
    return output;
  }
  function visibleItems(items, limit = 80) {
    const positive = sorted(items.filter(n => n.size > 0).slice());
    if (positive.length <= limit) return positive;
    const tail = positive.slice(limit - 1);
    return [...positive.slice(0, limit - 1), {id: "overflow", name: `其余 ${tail.length} 项（见右侧列表）`, type: "aggregate",
      size: tail.reduce((sum, n) => sum + n.size, 0), count: tail.reduce((sum, n) => sum + n.count, 0), children: []}];
  }
  function nestedLayout(items, width, height, maxDepth = 5) {
    let remaining = 1200;
    function visit(rows, w, h, depth) {
      const rectangles = layout(visibleItems(rows, depth ? 40 : 80), w, h);
      remaining -= rectangles.length;
      return rectangles.map(rect => {
        if (rect.node.id === "overflow") rect.node = {...rect.node, id: "overflow:" + (rows[0]?.parent || "/")};
        const children = rect.node.children || [];
        const count = Math.min(40, children.filter(n => n.size > 0).length);
        const expanded = depth < maxDepth && rect.width >= 120 && rect.height >= 90 && count > 0 && remaining >= count;
        return {...rect, depth, children: expanded ? visit(children, rect.width - 4, rect.height - 28, depth + 1) : []};
      });
    }
    return visit(items, width, height, 0);
  }
  if (typeof module !== "undefined") module.exports = {buildTree, layout, visibleItems, nestedLayout, kind};
  if (typeof document === "undefined") return;

  const $ = id => document.getElementById(id);
  const escape = value => String(value).replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;");
  const bytes = value => {
    if (!value) return "0 B";
    const i = Math.min(5, Math.floor(Math.log(value) / Math.log(1024)));
    return `${(value / 1024 ** i).toLocaleString("zh-CN", {maximumFractionDigits: 1})} ${["B", "KB", "MB", "GB", "TB", "PB"][i]}`;
  };
  let tree = null, currentId = "/", selectedId = null, page = 1, search = "", status = "", supported = true;
  let sourceResult = null, jobId = null, details = null, requestKey = "", requestSerial = 0, controller = null, loading = false, loadError = "";
  function nodeFromRow(row) {
    if (row.type === "folder") return tree.nodes.get(row.path);
    const node = {id: "file:" + row.path, parent: parentPath(row.path), path: row.path, name: nameOf(row.path),
      type: "file", size: row.size_bytes, count: 1, links: row.link_count, children: []};
    tree.nodes.set(node.id, node);
    return node;
  }
  function applyDetails() {
    if (!details || details.path !== currentId) return;
    const current = tree.nodes.get(currentId);
    if (!current) return;
    current.children = details.map_items.map(nodeFromRow).filter(Boolean);
    current.size = details.size_bytes;
    current.count = details.file_count;
    const rest = details.map_total - current.children.length;
    if (rest > 0) current.children.push({id: "overflow:" + currentId, parent: currentId, path: currentId,
      name: `其余 ${rest} 项（右侧可分页查看）`, type: "aggregate",
      size: Math.max(0, current.size - current.children.reduce((s, n) => s + n.size, 0)), count: 0, children: []});
    details.items.forEach(nodeFromRow);
  }
  async function loadDirectory() {
    if (!jobId || !sourceResult?.tree_file_index || currentId === "/") return;
    const key = JSON.stringify([jobId, currentId, page, search, sourceResult.scanned_files]);
    if (key === requestKey) return;
    requestKey = key;
    controller?.abort();
    controller = new AbortController();
    const serial = ++requestSerial;
    loading = true;
    $("map-prev").disabled = true;
    $("map-next").disabled = true;
    loadError = "";
    showSelection();
    try {
      const params = new URLSearchParams({path: currentId, offset: String((page - 1) * 30), limit: "30", q: search});
      const response = await fetch(`/api/jobs/${encodeURIComponent(jobId)}/tree?${params}`, {signal: controller.signal});
      const data = await response.json();
      if (serial !== requestSerial) return;
      if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "读取目录失败");
      details = data;
      page = Math.floor(data.offset / 30) + 1;
      applyDetails();
    } catch (error) {
      if (serial !== requestSerial) return;
      loadError = `全部文件加载失败：${error.message}。点击“重试加载”。`;
      requestKey = "";
    } finally {
      if (serial === requestSerial) { loading = false; draw(); }
    }
  }
  function choose(id) {
    const node = tree?.nodes.get(id);
    if (!node) {
      if (id.startsWith("overflow:")) {
        choose(id.slice("overflow:".length));
        $("map-selection").textContent = "细小项目已合并以保持地图清晰。右侧列表可分页查看全部项目。";
      }
      return;
    }
    if (node.type === "aggregate" && node.id.startsWith("small:")) {
      if (sourceResult?.tree_file_index && jobId) choose(node.parent);
      else { selectedId = id; showSelection(); }
      return;
    }
    if (node.type === "folder") {
      controller?.abort(); requestSerial++; requestKey = ""; loading = false; loadError = "";
      currentId = node.id; selectedId = null; page = 1; details = null; draw(); loadDirectory();
    }
    else { selectedId = id; showSelection(); }
  }
  function showSelection() {
    const node = tree?.nodes.get(selectedId);
    $("map-selection").textContent = loadError || (loading ? "正在加载本目录全部文件（不受大小阈值限制）…" : node ? `${node.path}\n${node.name} · ${bytes(node.size)}${node.type === "aggregate" ? " · 旧结果未保留完整文件索引，请用新版重新扫描" : ` · 硬链接数 ${node.links}`}` : "点击文件夹进入；全部文件均可分页查看，不受大小阈值限制。");
    $("map-retry").classList.toggle("is-hidden", !loadError);
  }
  function tile(rect, total) {
    const n = rect.node;
    const dimmed = search && !(n.path || n.name).toLowerCase().includes(search);
    const label = `${n.path || n.name} · ${bytes(n.size)} · ${total ? (n.size / total * 100).toFixed(1) : 0}%`;
    const showLabel = rect.width > 64 && rect.height > 38;
    const geometry = `left:${rect.x}px;top:${rect.y}px;width:${rect.width}px;height:${rect.height}px`;
    const attributes = `data-map-id="${escape(n.id)}" data-kind="${kind(n)}" aria-label="${escape(label)}" title="${escape(label)}"`;
    if (rect.children.length) {
      return `<div class="map-group" data-depth="${rect.depth}" style="${geometry}"><button type="button" class="map-folder-label${dimmed ? " is-dimmed" : ""}" ${attributes}><span>${escape(n.name)}</span><small>${bytes(n.size)} ↗</small></button><div class="map-children">${rect.children.map(r => tile(r, n.size)).join("")}</div></div>`;
    }
    return `<button type="button" class="map-tile${dimmed ? " is-dimmed" : ""}" ${attributes} style="${geometry}">${showLabel ? `<span class="map-tile-name">${escape(n.name)}${n.type === "folder" ? " ↗" : ""}</span><span class="map-tile-size">${bytes(n.size)}</span>` : ""}</button>`;
  }
  function draw() {
    if (!tree || $("space-map").classList.contains("is-hidden")) return;
    const focused = document.activeElement?.dataset.mapId;
    const current = tree.nodes.get(currentId) || tree.root;
    currentId = current.id;
    $("map-status").textContent = status;
    $("map-up").disabled = current === tree.root;
    const trail = [];
    for (let node = current; node; node = tree.nodes.get(node.parent)) trail.unshift(node);
    $("map-breadcrumbs").innerHTML = trail.map(n => `<button type="button" data-map-id="${escape(n.id)}" ${n === current ? 'aria-current="location"' : ""}>${escape(n.name)}</button>`).join('<span aria-hidden="true">/</span>');
    $("map-current").innerHTML = `<span>${escape(current.name)}</span><strong>${bytes(current.size)}</strong><small>${current.count.toLocaleString("zh-CN")} 个已读取文件 · ${details?.path === currentId ? details.map_total : current.children.length} 个项目</small>`;
    showSelection();
    const canvas = $("map-canvas");
    const width = canvas.clientWidth, height = canvas.clientHeight;
    canvas.innerHTML = current.size > 0 ? nestedLayout(current.children, width, height).map(r => tile(r, current.size)).join("") : `<div class="map-empty">${supported ? "暂无可绘制的数据，等待扫描读取文件。零字节项目可在右侧列表查看。" : "这份结果不含目录层级数据，请重新开始分析以生成空间地图。"}</div>`;
    const matches = current.children.filter(n => !search || n.path.toLowerCase().includes(search) || n.name.toLowerCase().includes(search));
    const full = details?.path === currentId;
    const total = full ? details.total : matches.length;
    const pages = Math.max(1, Math.ceil(total / 30));
    page = Math.min(page, pages);
    const shown = full ? details.items.map(nodeFromRow).filter(Boolean) : matches.slice((page - 1) * 30, page * 30);
    $("map-list").innerHTML = shown.map(n => `<button type="button" class="map-list-item" data-map-id="${escape(n.id)}" data-kind="${kind(n)}"><span>${escape(n.name)}</span><strong>${bytes(n.size)}</strong></button>`).join("") || '<p class="map-list-empty">没有匹配的项目</p>';
    $("map-page").textContent = `${page} / ${pages} · ${total} 项${full ? "（全部）" : ""}`;
    $("map-prev").disabled = loading || page <= 1;
    $("map-next").disabled = loading || page >= pages;
    if (focused) [...$("space-map").querySelectorAll("[data-map-id]")].find(el => el.dataset.mapId === focused)?.focus({preventScroll: true});
  }
  $("space-map").addEventListener("click", event => {
    const target = event.target.closest("[data-map-id]");
    if (target) choose(target.dataset.mapId);
  });
  $("map-up").addEventListener("click", () => choose(tree?.nodes.get(currentId)?.parent || "/"));
  $("map-prev").addEventListener("click", () => { page = Math.max(1, page - 1); if (details) loadDirectory(); else draw(); });
  $("map-next").addEventListener("click", () => { page++; if (details) loadDirectory(); else draw(); });
  $("map-retry").addEventListener("click", () => { requestKey = ""; loadDirectory(); });
  let resizeFrame;
  new ResizeObserver(() => { cancelAnimationFrame(resizeFrame); resizeFrame = requestAnimationFrame(draw); }).observe($("map-canvas"));
  global.spaceMap = {
    reset() { controller?.abort(); requestSerial++; currentId = "/"; selectedId = null; page = 1; tree = null; details = null; requestKey = ""; jobId = null; },
    update(result, jobStatus, query, currentJobId) {
      sourceResult = result;
      jobId = currentJobId;
      supported = Array.isArray(result.tree_directories);
      tree = buildTree(result);
      if (search !== query) page = 1;
      search = query;
      applyDetails();
      status = jobStatus === "completed" ? result.complete ? "扫描完成" : "部分结果 · 存在未读取目录" : ["cancelled", "failed"].includes(jobStatus) ? "扫描已停止 · 部分结果" : "实时扫描 · 面积持续更新";
      draw();
      loadDirectory();
    },
  };
})(typeof window === "undefined" ? globalThis : window);
