'use strict';
// Pure layout: only observed dependency edges determine the graph.
function layoutWorkflow(steps) {
  const scheduled = steps.filter(s => s.scheduled === true);
  const byId = new Map(scheduled.map(s => [s.id, s]));
  const inherited = scheduled.filter(s => s.inherited);
  const depth = new Map(inherited.map(s => [s.id, -1]));
  let pending = scheduled.filter(s => !s.inherited);
  while (pending.length) {
    const ready = pending.filter(s => (s.dependencies || []).every(id => byId.has(id) && depth.has(id)));
    if (!ready.length) break;
    for (const s of ready) depth.set(s.id, Math.max(-1, ...(s.dependencies || []).map(id => depth.get(id))) + 1);
    const ids = new Set(ready.map(s => s.id));
    pending = pending.filter(s => !ids.has(s.id));
  }
  const layers = [];
  for (const s of scheduled) {
    if (!s.inherited && depth.has(s.id)) (layers[depth.get(s.id)] ||= []).push(s);
  }
  const width = Math.max(320, ...layers.map(row => row.length * 272 + 48));
  const nodes = new Map();
  layers.forEach((row, level) => row.forEach((s, col) => {
    nodes.set(s.id, {step: s, x: (width - row.length * 272) / 2 + col * 272 + 16, y: level * 190 + 64});
  }));
  const edges = [];
  for (const [id, node] of nodes) for (const dep of node.step.dependencies || []) {
    if (nodes.has(dep)) edges.push({from: dep, to: id});
  }
  return {layers, nodes, edges, width, height: Math.max(240, layers.length * 190 + 40),
    inherited, unresolved: pending, upcoming: steps.filter(s => s.scheduled !== true)};
}
const workflowViews = new Map();
function disposeWorkflow(host) {
  host.workflowResize?.disconnect();
  host.workflowResize = null;
  delete host.dataset.signature;
}
function renderWorkflow(host, steps, runId, selectedId, select, makeBadge) {
  const mobile = window.matchMedia('(max-width: 800px)').matches;
  const previous = workflowViews.get(runId) || {zoom: 1, left: 0, top: 0, fitWidth: mobile, manualZoom: false};
  const signature = JSON.stringify([steps, selectedId]);
  if (host.dataset.signature === signature && host.dataset.run === runId) return;
  host.dataset.signature = signature;
  host.dataset.run = runId;
  host.workflowResize?.disconnect();
  host.replaceChildren();
  const make = (tag, text, cls) => {
    const n = document.createElement(tag);
    if (text !== undefined) n.textContent = text;
    if (cls) n.className = cls;
    return n;
  };
  const graph = layoutWorkflow(steps);
  const toolbar = make('div', undefined, 'graph-toolbar');
  const controls = make('div', undefined, 'graph-controls');
  const title = make('div');
  title.append(make('strong', '本轮任务流程'), make('span', `${graph.nodes.size} 个本轮节点 · ${graph.edges.length} 条本轮依赖 · ${graph.inherited.length} 项沿用结果`, 'meta'));
  toolbar.append(title, controls);
  const zoomLabel = make('span', '', 'graph-zoom');
  zoomLabel.setAttribute('aria-live', 'polite');
  const viewport = make('div', undefined, 'graph-viewport');
  viewport.tabIndex = 0;
  viewport.dataset.focus = 'graph:viewport';
  viewport.setAttribute('role', 'region');
  viewport.setAttribute('aria-label', '任务依赖流程图，可用方向键滚动');
  const frame = make('div', undefined, 'graph-frame');
  const canvas = make('div', undefined, 'graph-canvas');
  canvas.style.width = `${graph.width}px`;
  canvas.style.height = `${graph.height}px`;
  frame.append(canvas); viewport.append(frame);
  const save = () => {
    previous.left = viewport.scrollLeft; previous.top = viewport.scrollTop;
    workflowViews.set(runId, previous);
  };
  const zoom = (value, manual = true) => {
    if (manual) { previous.fitWidth = false; previous.manualZoom = true; }
    const cx = (viewport.scrollLeft + viewport.clientWidth / 2) / previous.zoom;
    const cy = (viewport.scrollTop + viewport.clientHeight / 2) / previous.zoom;
    previous.zoom = Math.max(0.05, Math.min(1.5, value));
    canvas.style.transform = `scale(${previous.zoom})`;
    frame.style.width = `${graph.width * previous.zoom}px`;
    frame.style.height = `${graph.height * previous.zoom}px`;
    viewport.style.setProperty('--graph-height', `${graph.height * previous.zoom + 24}px`);
    zoomLabel.textContent = `${Math.round(previous.zoom * 100)}%`;
    viewport.scrollLeft = cx * previous.zoom - viewport.clientWidth / 2;
    viewport.scrollTop = cy * previous.zoom - viewport.clientHeight / 2;
    save();
  };
  const addControl = (label, action) => {
    const b = make('button', label); b.type = 'button';
    b.dataset.focus = `graph:${label}`; b.onclick = action;
    controls.append(b); return b;
  };
  addControl('缩小', () => zoom(previous.zoom - 0.15));
  controls.append(zoomLabel);
  addControl('放大', () => zoom(previous.zoom + 0.15));
  const fitWidth = () => {
    previous.fitWidth = true;
    previous.manualZoom = false;
    zoom((viewport.clientWidth - 24) / graph.width, false);
  };
  addControl('适应宽度', fitWidth);
  const active = steps.filter(s => ['running', 'active', 'leased', 'queued', 'pending_dispatch', 'recovery_required'].includes(s.state) && graph.nodes.has(s.id));
  let activeIndex = 0;
  const center = (id) => {
    const n = graph.nodes.get(id);
    if (!n) return;
    viewport.scrollLeft = (n.x + 120) * previous.zoom - viewport.clientWidth / 2;
    viewport.scrollTop = (n.y + 58) * previous.zoom - viewport.clientHeight / 2;
    save();
  };
  const locate = addControl(active.length > 1 ? `定位当前 (${active.length})` : '定位当前', () => {
    const target = active.length ? active[activeIndex++ % active.length].id : selectedId;
    center(target);
  });
  locate.disabled = !active.length && !graph.nodes.has(selectedId);
  const hint = make('p', graph.edges.length
    ? '从上往下读取 · 本轮同层节点无前后依赖，可并行，实际执行以状态为准 · 图内滑动查看'
    : '本轮尚无任务间依赖记录，暂不显示连线。沿用结果在下方单独列出。', 'graph-hint');
  host.append(toolbar, hint, viewport);
  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns, 'svg');
  svg.setAttribute('width', graph.width); svg.setAttribute('height', graph.height);
  svg.setAttribute('aria-hidden', 'true'); svg.classList.add('graph-edges');
  const defs = document.createElementNS(ns, 'defs');
  const marker = document.createElementNS(ns, 'marker');
  marker.id = 'dependency-arrow';
  for (const [k,v] of Object.entries({viewBox:'0 0 10 10',refX:'9',refY:'5',markerWidth:'6',markerHeight:'6',orient:'auto-start-reverse'})) marker.setAttribute(k,v);
  const arrow = document.createElementNS(ns, 'path'); arrow.setAttribute('d','M 0 0 L 10 5 L 0 10 z'); arrow.setAttribute('fill','#8293ab');
  marker.append(arrow); defs.append(marker); svg.append(defs);
  for (const edge of graph.edges) {
    const a = graph.nodes.get(edge.from), b = graph.nodes.get(edge.to);
    const path = document.createElementNS(ns, 'path');
    const ax = a.x + 120, ay = a.y + 116, bx = b.x + 120, by = b.y - 4;
    // Route long edges through the outer gutter to avoid crossing intervening nodes.
    const d = by - ay > 100
      ? `M ${ax} ${ay} L ${ax} ${ay + 20} L 12 ${ay + 20} L 12 ${by - 20} L ${bx} ${by - 20} L ${bx} ${by}`
      : `M ${ax} ${ay} C ${ax} ${(ay+by)/2}, ${bx} ${(ay+by)/2}, ${bx} ${by}`;
    path.setAttribute('d',d); path.setAttribute('marker-end','url(#dependency-arrow)');
    path.classList.add('graph-edge');
    if (edge.from === selectedId || edge.to === selectedId) path.classList.add('highlight');
    svg.append(path);
  }
  canvas.append(svg);
  const buttonFor = (s, cls) => {
    const b = make('button', undefined, cls + (s.id === selectedId ? ' selected' : ''));
    b.type = 'button'; b.dataset.step = s.id; b.dataset.focus = `step:${s.id}`;
    b.setAttribute('aria-pressed', String(s.id === selectedId));
    const status = make('span', undefined, 'graph-node-status');
    status.append(makeBadge(s.state));
    const inheritedInputs = (s.dependencies || []).filter(id => graph.inherited.some(item => item.id === id)).length;
    if (inheritedInputs) status.append(make('span', `沿用输入 ${inheritedInputs} 项`, 'graph-input-note'));
    b.append(make('span', s.label, 'node-label'), status);
    const deps = (s.dependencies || []).map(id => steps.find(n => n.id === id)?.label || '未提供的依赖节点');
    b.title = [s.label, s.inherited ? '沿用已有结果，未提供历史依赖' : deps.length ? `依赖：${deps.join('、')}` : '无已记录前置依赖'].join('\n');
    b.onclick = () => select(s.id);
    return b;
  };
  graph.layers.forEach((row, level) => {
    const label = make('div', `第 ${level + 1} 层${row.length > 1 ? ` · ${row.length} 项可并行` : ''}`, 'graph-level');
    label.style.top = `${level * 190 + 28}px`; canvas.append(label);
    row.forEach(s => {
      const n = graph.nodes.get(s.id), b = buttonFor(s, 'graph-node');
      b.style.left = `${n.x}px`; b.style.top = `${n.y}px`;
      b.dataset.state = s.state;
      b.addEventListener('focus', () => { if (document.activeElement === b) save(); });
      canvas.append(b);
    });
  });
  if (!graph.nodes.size) canvas.append(make('p', '暂无可绘制的依赖记录', 'graph-empty'));
  const appendShelf = (items, heading, explanation) => {
    if (!items.length) return;
    const section = make('section', undefined, 'graph-shelf');
    section.append(make('h3', `${heading} · ${items.length}`), make('p', explanation, 'meta'));
    const grid = make('div', undefined, 'graph-shelf-grid');
    items.forEach(s => grid.append(buttonFor(s, 'graph-shelf-node')));
    section.append(grid); host.append(section);
  };
  appendShelf(graph.inherited, '沿用的结果', '这些结果来自此前运行，按卡片换行展示；未提供历史依赖，不表示它们曾并行执行。');
  appendShelf(graph.unresolved, '依赖关系待确认', '依赖记录缺失或存在循环，暂不推断这些节点的顺序。');
  appendShelf(graph.upcoming, '尚未调度的步骤', '运行尚未提供这些步骤的依赖关系，调度后自动进入上方流程图。');
  const left = previous.left, top = previous.top;
  if (previous.fitWidth) fitWidth();
  else zoom(previous.zoom, false);
  viewport.scrollLeft = left; viewport.scrollTop = top;
  if (!workflowViews.get(runId)?.positioned) {
    if (mobile) { viewport.scrollLeft = 0; viewport.scrollTop = 0; }
    else center(active[0]?.id || selectedId);
    previous.positioned = true;
  }
  save(); viewport.addEventListener('scroll', save, {passive:true});
  let fittedWidth = viewport.clientWidth;
  host.workflowResize = new ResizeObserver(() => {
    if (!viewport.isConnected || !viewport.clientWidth) return;
    const enterMobile = !previous.manualZoom && !previous.fitWidth && window.matchMedia('(max-width: 800px)').matches;
    if (enterMobile || (previous.fitWidth && viewport.clientWidth !== fittedWidth)) fitWidth();
    fittedWidth = viewport.clientWidth;
  });
  host.workflowResize.observe(viewport);
  let drag;
  viewport.addEventListener('pointerdown', e => {
    if (e.pointerType !== 'mouse' || e.button !== 0 || e.target.closest('button')) return;
    drag = {x:e.clientX, y:e.clientY, left:viewport.scrollLeft, top:viewport.scrollTop};
    viewport.setPointerCapture(e.pointerId); viewport.classList.add('dragging'); e.preventDefault();
  });
  viewport.addEventListener('pointermove', e => {
    if (!drag) return;
    viewport.scrollLeft = drag.left - e.clientX + drag.x;
    viewport.scrollTop = drag.top - e.clientY + drag.y;
  });
  const stop = () => {drag = null; viewport.classList.remove('dragging');};
  viewport.addEventListener('pointerup', stop); viewport.addEventListener('pointercancel', stop);
}
