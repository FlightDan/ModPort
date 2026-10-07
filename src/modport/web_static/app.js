'use strict';
const $ = (id) => document.getElementById(id);
const labels = {
  running: '进行中',
  interrupted: '执行中断／待恢复',
  active: '进行中',
  leased: '执行中',
  succeeded: '执行完成',
  completed: '已完成',
  failed: '失败',
  cancelled: '已取消',
  waiting: '等待处理',
  blocked: '步骤受阻',
  planned: '等待依赖',
  pending: '尚未开始',
  queued: '排队中',
  ready: '已就绪',
  retrying: '等待重试',
  timed_out: '已超时',
  dead: '已停止',
  unknown: '状态未知',
  unavailable: '无法读取',
  skipped: '已跳过',
  pending_dispatch: '等待派发',
  recovery_required: '等待恢复',
  superseded: '历史记录',
  invalidated: '等待返工',
};
const symbols = {
  running: '◉',
  active: '◉',
  leased: '◉',
  succeeded: '✓',
  completed: '✓',
  failed: '!',
  waiting: 'Ⅱ',
  blocked: 'Ⅱ',
  cancelled: '−',
  pending: '○',
  queued: '○',
};
let selectedRun = '',
  selectedStep = '',
  taskDetail = null,
  evidenceDetail = null,
  taskQueryValue = '',
  epoch = 0,
  timer,
  busy = false,
  loggedIn = false;
const openAttempts = new Map();
let revealStep = false;
function el(tag, text, cls) {
  const n = document.createElement(tag);
  if (text !== undefined) n.textContent = String(text);
  if (cls) n.className = cls;
  return n;
}
function badge(state) {
  return el(
    'span',
    `${symbols[state] || '·'} ${labels[state] || state || '状态未知'}`,
    `badge ${Object.hasOwn(labels, state) ? state : 'unknown'}`,
  );
}
function acceptanceLabel(status) {
  const names = { unverified: '未验证', verified: '已验证', failed: '未通过' };
  return Object.hasOwn(names, status) ? names[status] : '状态未知';
}
function date(value) {
  if (!value) return '暂无记录';
  const d = new Date(typeof value === 'number' ? value * 1000 : value);
  return Number.isNaN(d.valueOf()) ? String(value) : d.toLocaleString('zh-CN', { hour12: false });
}
async function api(path, body) {
  const response = await fetch(path, {
    credentials: 'same-origin',
    cache: 'no-store',
    signal: AbortSignal.timeout(15000),
    ...(body === undefined
      ? {}
      : {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        }),
  });
  const data = await response.json();
  if (!response.ok) {
    const error = new Error(data.error || '读取失败');
    error.status = response.status;
    throw error;
  }
  return data;
}
function showLogin(message = '') {
  $('password').type = 'password';
  $('toggle-password').textContent = '显示';
  $('toggle-password').setAttribute('aria-label', '显示密码');
  $('toggle-password').setAttribute('aria-pressed', 'false');
  loggedIn = false;
  disposeWorkflow($('stages'));
  epoch++;
  clearTimeout(timer);
  $('workspace').hidden = true;
  $('login').hidden = false;
  $('logout').hidden = true;
  $('initial-loading').hidden = true;
  $('login-error').textContent = message;
  $('password').focus();
}
function showWorkspace() {
  loggedIn = true;
  $('login').hidden = true;
  $('workspace').hidden = false;
  $('logout').hidden = false;
  $('initial-loading').hidden = true;
}
function notice(message = '') {
  $('notice').textContent = message;
  $('notice').hidden = !message;
}
function renderList(runs) {
  const list = $('run-list');
  list.replaceChildren();
  if (!runs.length) {
    list.append(el('p', '此目录还没有迁移运行。新任务出现后会自动显示。', 'empty'));
    return;
  }
  for (const run of runs) {
    const link = el('a', undefined, 'run-card');
    link.href = `#run=${encodeURIComponent(run.id)}`;
    link.dataset.focus = `run:${run.id}`;
    const identity = el('div');
    identity.append(
      el('div', run.mod_id || run.run_id, 'run-name'),
      el(
        'div',
        `${run.source_version || '未知版本'} → ${run.target_version || '未知版本'}`,
        'meta',
      ),
    );
    const current = el('div', undefined, 'run-current');
    current.append(
      el(
        'div',
        run.error ||
          (run.active_steps || []).join('、') ||
          (run.state === 'succeeded' ? '执行已结束' : '暂无活动步骤'),
      ),
      el('div', `业务验收：${acceptanceLabel(run.acceptance_status)}`, 'meta'),
      el('div', `最近更新 ${date(run.updated_at)}`, 'meta'),
    );
    link.append(identity, current, badge(run.state));
    list.append(link);
  }
}
function renderPanel(step) {
  const panel = $('step-panel');
  panel.replaceChildren();
  if (!step) {
    panel.append(el('p', '点击任一步骤查看摘要。', 'muted'));
    return;
  }
  const title = el('div', undefined, 'detail-title');
  const heading = el('h2', step.label);
  heading.tabIndex = -1;
  title.append(heading, badge(step.state));
  panel.append(title);
  for (const [label, value] of [
    ['这一步做什么', step.purpose],
    ['关键输入', step.inputs],
    ['当前进展', step.progress],
    ['结果', step.result || '暂无结果'],
    ['失败原因', step.error],
    ['摘要读取', step.summary_error],
  ]) {
    if (!value || (Array.isArray(value) && !value.length)) continue;
    panel.append(el('h3', label));
    if (Array.isArray(value)) {
      const ul = el('ul');
      value.forEach((v) => ul.append(el('li', v)));
      panel.append(ul);
    } else panel.append(el('p', value, label === '失败原因' ? 'error' : ''));
  }
  if (step.attempts?.length) {
    panel.append(el('h3', '执行记录'));
    step.attempts.forEach((attempt, i) => {
      const d = el('details', undefined, 'attempt');
      const key = `${selectedRun}:${step.id}:${i}`;
      d.open = openAttempts.has(key) ? openAttempts.get(key) : i === step.attempts.length - 1;
      d.addEventListener('toggle', () => {
        if (d.isConnected) openAttempts.set(key, d.open);
      });
      const s = el('summary');
      s.dataset.focus = `attempt:${key}`;
      s.append(el('span', attempt.label || `第 ${i + 1} 次`), badge(attempt.state));
      d.append(s, el('p', attempt.detail || '暂无结果', 'muted'));
      if (attempt.execution_attempt)
        d.append(
          el(
            'p',
            `技术执行 ${attempt.execution_attempt} 次 · 重试 ${attempt.execution_retries || 0} 次`,
            'meta',
          ),
        );
      panel.append(d);
    });
  }
}
function chooseStep(run) {
  const allSteps = (run.groups || []).flatMap((g) => g.steps);
  if (allSteps.some((s) => s.id === selectedStep)) return selectedStep;
  return (
    (allSteps.find((s) => ['running', 'active', 'leased'].includes(s.state)) ||
      allSteps.find((s) => ['waiting', 'recovery_required', 'failed'].includes(s.state)))?.id ||
    allSteps[0]?.id || ''
  );
}
function evidenceText(record) {
  if (!record || typeof record !== 'object') return '未知';
  const state = { observed: '已观察到', no_change: '确认无代码变化', sampled: '已观察到样本',
    sampled_observed: '已观察到样本', sampled_no_change: '样本中无代码变化', none: '无记录', unknown: '未知' };
  const freshness = record.stale ? '记录可能过期' : '记录新鲜';
  const completeness = record.selection_complete === false ? '最近记录未完整核查' : '选择完整';
  return `${state[record.status] || record.status || '未知'} · ${completeness} · ${freshness} · ${record.source || '来源未知'} · ${date(record.observed_at)}`;
}
function renderTaskDetail(data) {
  const panel = $('step-panel');
  panel.replaceChildren();
  const task = data?.task;
  if (!task) {
    panel.append(el('p', '尚未查询单个任务。', 'muted'));
    return;
  }
  const title = el('div', undefined, 'detail-title');
  const heading = el('h2', task.stage_id || task.task_id || '任务详情');
  heading.tabIndex = -1;
  title.append(heading, badge(task.execution_state));
  panel.append(title);
  panel.append(el('p', `任务 ID：${task.task_id || '未知'} · 业务状态：${task.business_status || '未知'}`, 'meta'));
  panel.append(el('p', `来源：${data.source || '未知'} · 读取于 ${date(data.observed_at)}${data.stale ? ' · 可能过期' : ''}`, 'meta'));
  if (task.detail) panel.append(el('p', task.detail));
  if (task.error_code) panel.append(el('p', `错误码：${task.error_code}`, 'error'));
  if (task.head) panel.append(el('p', `HEAD：${task.head}`, 'meta'));
  if (task.paths?.length) {
    panel.append(el('h3', '记录路径'));
    const list = el('ul');
    task.paths.forEach((path) => list.append(el('li', path)));
    panel.append(list);
  }
  const refs = Object.entries(task.artifact_refs || {});
  if (refs.length) {
    panel.append(el('h3', '工件引用'));
    const list = el('ul');
    refs.forEach(([name, ref]) => list.append(el('li', `${name} · ${ref.path || '路径未知'} · ${ref.sha256 || '无摘要'}`)));
    panel.append(list);
  }
}
function renderRun(run) {
  const allSteps = (run.groups || []).flatMap((g) => g.steps);
  const overview = el('div', undefined, 'overview');
  const title = el('div', undefined, 'overview-title');
  title.append(el('h2', run.mod_id || run.run_id), badge(run.state));
  overview.append(
    title,
    el(
      'div',
      `${run.source_version || '未知版本'} → ${run.target_version || '未知版本'} · ${run.run_id}`,
      'meta',
    ),
    el(
      'p',
      (run.active_steps || []).length
        ? `当前步骤：${run.active_steps.join('、')}`
        : run.state === 'succeeded'
          ? '工作流执行已结束'
          : '暂无活动步骤',
      'activity',
    ),
    el('p', `业务验收：${acceptanceLabel(run.acceptance_status)}`, 'meta'),
  );
  const evidence = el('div', undefined, 'status-evidence');
  evidence.append(
    el('h3', '最近代码变化'),
    el('p', evidenceText(run.last_code_change), 'meta'),
    el('h3', '最近成功的认证验证'),
    el('p', evidenceText(run.last_successful_authenticated_verification), 'meta'),
    el('h3', '当前等待'),
    el('p', `${run.current_wait?.status || '未知'} · ${run.current_wait?.source || '来源未知'} · ${run.current_wait?.stale ? '可能过期' : '新鲜'} · ${date(run.current_wait?.observed_at)}`, 'meta'),
  );
  if (run.current_wait?.detail) evidence.append(el('p', run.current_wait.detail, 'muted'));
  if (run.current_wait?.samples?.length) {
    const waits = el('ul');
    run.current_wait.samples.slice(0, 8).forEach((sample) => {
      waits.append(el('li', `${sample.reason || sample.stage || '等待原因未知'} · ${sample.source || '来源未知'} · ${date(sample.observed_at)}${sample.stale ? ' · 可能过期' : ''}`));
    });
    evidence.append(waits);
  }
  const taskForm = el('form', undefined, 'task-query');
  const taskInput = el('input');
  taskInput.type = 'text';
  taskInput.maxLength = 256;
  taskInput.required = true;
  taskInput.value = taskQueryValue;
  taskInput.dataset.focus = 'task-id-query';
  taskInput.placeholder = '输入 SDK task ID';
  taskInput.setAttribute('aria-label', 'SDK task ID');
  taskInput.oninput = () => { taskQueryValue = taskInput.value; };
  const taskButton = el('button', '查询单个任务');
  taskButton.type = 'submit';
  taskForm.append(taskInput, taskButton);
  taskForm.onsubmit = async (event) => {
    event.preventDefault();
    taskButton.disabled = true;
    const targetRun = selectedRun;
    const targetTask = taskInput.value;
    try {
      const result = await api(`/api/runs/${encodeURIComponent(targetRun)}/tasks/${encodeURIComponent(targetTask)}`);
      if (selectedRun !== targetRun) return;
      taskDetail = { runId: targetRun, value: result };
      renderTaskDetail(result);
    } catch (error) {
      notice(error.status === 401 ? '会话已过期，请重新登录。' : `任务详情读取失败：${error.message || '连接中断'}`);
    } finally {
      taskButton.disabled = false;
    }
  };
  evidence.append(taskForm);
  if (run.active_tasks?.length) {
    const tasks = el('ul');
    run.active_tasks.slice(0, 8).forEach((task) => {
      tasks.append(el('li', `${task.task_id || '任务 ID 未知'} · ${task.state || '状态未知'}`));
    });
    evidence.append(el('h3', '当前执行样本'), tasks);
  }
  overview.append(evidence);
  if (run.detail_pending) overview.append(el('p', run.error || '正在读取运行详情…', 'notice'));
  else if (run.error) overview.append(el('p', run.error, 'error'));
  $('run-overview').replaceChildren(overview);
  renderWorkflow($('stages'), allSteps, selectedRun, selectedStep, (id) => {
    taskDetail = null;
    revealStep = true;
    const hash = `#run=${encodeURIComponent(selectedRun)}&step=${encodeURIComponent(id)}`;
    if (location.hash === hash) navigate();
    else location.hash = hash;
  }, badge);
  if (taskDetail?.runId === selectedRun) renderTaskDetail(taskDetail.value);
  else renderPanel(allSteps.find((s) => s.id === selectedStep));
}
async function refresh() {
  if (!loggedIn || busy) return;
  clearTimeout(timer);
  busy = true;
  $('refresh').disabled = true;
  const version = epoch;
  try {
    const data = await api(
      selectedRun ? `/api/runs/${encodeURIComponent(selectedRun)}` : '/api/runs',
    );
    if (version !== epoch) return;
    let nextStep = '';
    if (selectedRun) {
      if (!evidenceDetail || evidenceDetail.runId !== selectedRun ||
          Date.now() - evidenceDetail.fetchedAt > 60000) {
        try {
          const value = await api(`/api/runs/${encodeURIComponent(selectedRun)}/evidence`);
          if (version !== epoch) return;
          evidenceDetail = { runId: selectedRun, fetchedAt: Date.now(), value };
        } catch (_error) {
          evidenceDetail = { runId: selectedRun, fetchedAt: Date.now(), value: null };
        }
      }
      if (evidenceDetail?.runId === selectedRun && evidenceDetail.value) {
        const evidence = evidenceDetail.value;
        const sameRevision = evidence.revision != null && data.execution?.revision != null &&
          evidence.revision === data.execution.revision;
        for (const field of ['last_code_change', 'last_successful_authenticated_verification']) {
          const record = evidence[field];
          if (record && typeof record === 'object') {
            data[field] = { ...record,
              stale: !!record.stale || !!evidence.stale || !sameRevision,
              selection_complete: record.selection_complete === true && sameRevision && !evidence.stale };
          }
        }
        if (sameRevision && !evidence.stale && evidence.current_wait) {
          data.current_wait = evidence.current_wait;
        }
      }
      nextStep = (data.groups || []).length ? chooseStep(data) : '';
      if (nextStep) {
        const step = await api(
          `/api/runs/${encodeURIComponent(selectedRun)}/steps/${encodeURIComponent(nextStep)}`,
        );
        if (version !== epoch) return;
        for (const group of data.groups) {
          group.steps = group.steps.map((item) => item.id === nextStep ? step : item);
        }
      }
    }
    // Capture immediately before rendering: a slow read must not undo scrolling
    // or keyboard navigation performed while the request was in flight.
    const focusKey = document.activeElement?.dataset.focus;
    const scroll = window.scrollY;
    notice(data.stale ? data.error || '暂时无法刷新，正在显示上次读取的进度。' : '');
    if (selectedRun) {
      selectedStep = nextStep;
      renderRun(data);
    } else renderList(data.runs);
    $('freshness').textContent = selectedRun && !data.observed_at
      ? '尚无有效进度 · 每 3 秒刷新'
      : `更新于 ${date(data.observed_at || Date.now() / 1000)} · 每 3 秒刷新`;
    if (focusKey) {
      const b = [...document.querySelectorAll('[data-focus]')].find(
        (n) => n.dataset.focus === focusKey,
      );
      b?.focus({ preventScroll: true });
    }
    if (revealStep && selectedRun && window.matchMedia('(max-width:800px)').matches) {
      const heading = $('step-panel').querySelector('h2');
      heading?.focus({ preventScroll: true });
      $('step-panel').scrollIntoView({ block: 'start' });
    } else window.scrollTo(0, scroll);
    revealStep = false;
  } catch (error) {
    if (version !== epoch) return;
    if (error.status === 401) showLogin('会话已过期，请重新登录。');
    else notice(`${error.message || '连接中断'}。保留上次内容，稍后自动重试。`);
  } finally {
    busy = false;
    $('refresh').disabled = false;
    if (loggedIn) timer = setTimeout(refresh, version === epoch ? 3000 : 0);
  }
}
function navigate() {
  const params = new URLSearchParams(location.hash.slice(1));
  const next = params.get('run') || '';
  const changed = next !== selectedRun;
  selectedRun = next;
  selectedStep = params.get('step') || '';
  epoch++;
  $('run-list').hidden = !!selectedRun;
  $('run-detail').hidden = !selectedRun;
  $('page-title').textContent = selectedRun ? '运行详情' : '全部运行';
  if (changed) {
    taskDetail = null;
    evidenceDetail = null;
    taskQueryValue = '';
    $('run-overview').replaceChildren();
    disposeWorkflow($('stages'));
    $('stages').replaceChildren();
    renderPanel(null);
  }
  notice();
  refresh();
}
$('login-form').onsubmit = async (event) => {
  event.preventDefault();
  const button = event.submitter || $('login-form').querySelector('button[type="submit"]');
  button.disabled = true;
  $('login-error').textContent = '';
  try {
    await api('/api/login', { password: $('password').value });
    $('password').value = '';
    showWorkspace();
    navigate();
  } catch (error) {
    $('login-error').textContent = error.message;
  } finally {
    button.disabled = false;
  }
};
$('toggle-password').onclick = () => {
  const visible = $('password').type === 'password';
  $('password').type = visible ? 'text' : 'password';
  $('toggle-password').textContent = visible ? '隐藏' : '显示';
  $('toggle-password').setAttribute('aria-label', visible ? '隐藏密码' : '显示密码');
  $('toggle-password').setAttribute('aria-pressed', String(visible));
};
$('logout').onclick = async () => {
  try {
    await api('/api/logout', {});
    showLogin();
  } catch (error) {
    notice('退出失败，请重试。');
  }
};
$('refresh').onclick = refresh;
window.addEventListener('hashchange', navigate);
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) refresh();
});
(async () => {
  try {
    await api('/api/session');
    showWorkspace();
    navigate();
  } catch (error) {
    showLogin(error.status === 401 ? '' : '暂时无法连接服务，请稍后重试。');
  }
})();
