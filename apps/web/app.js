/* ===== 短剧工坊工作台 · 纯前端零构建 · 3 视图 hash 路由 ===== */
'use strict';

/* ---------- 全局状态 ---------- */
const state = {
  route: { view: 'dashboard' },
  // 片库
  projects: [],
  taskSummary: null,
  dbQuery: '',
  dbFilter: 'all',
  dbSort: 'recent',
  dbView: 'grid',
  // 工作室
  currentProjectId: null,
  bundle: null,          // GET /projects/{id} → {project, scenes[], shots[], runs[]}
  characters: [],        // GET /projects/{id}/characters
  assets: [],            // GET /assets?project_id=
  step: 'script',
  stepTimer: null,       // 当前步的轮询定时器
  portraitWorking: new Set(),
  portraitTimers: new Set(),
  noCharPatch: false,    // 后端不支持 PATCH /characters 后置 true,描述转只读
  propFilter: '__all',   // 道具步素材库子筛选
  sbDrafts: {},          // 分镜编辑草稿 {shotId: {action, duration_s}}
  sbExpandRun: null,     // 分镜展开播放的 run_id
  editSelectedShotId: null,
  directorReport: null,
  reviewing: false,
  exporting: false,
  // 建立片场
  nf: { genre: '都市情感', style: '写实电影感', ratio: '9:16' },
  // 对话出片(对话 -> 结构化分镜预览 -> 一键出片)
  chat: { messages: [], busy: false, proposal: null, plan: null,
          planBusy: false, planError: '', producing: false, planToken: 0,
          planShown: false, checklist: {} },
  producePending: false,   // 项目生产线程在跑(定妆/首帧/排队),SSE 事件驱动
  ocMode: 'auto',          // 一键出片模式: auto=全自动 / confirm=素材后确认
  ocMaterial: null,        // 一键出片上传的材料:{name, text|docxB64, chars}
  assetsView: { project: 'all', type: 'all', group: 'all' },   // 资产页筛选
  ocCelebrated: null,     // 已放过彩带的项目(避免重复庆祝)
  tasksStatPrev: {},      // 任务统计的上一帧数值(用于数字滚动)
  // SSE
  es: null,
  lastSeq: 0,
  seenEventIds: new Set(),
  events: [],
  sseRetryDelay: 1000,
  sseRetryTimer: null,
  sseStatus: 'off',
};

const STEPS = [
  { key: 'script', label: '剧本', note: '故事与场景' },
  { key: 'character', label: '角色', note: '人物与定妆' },
  { key: 'scene', label: '场景', note: '空间与光线' },
  { key: 'prop', label: '道具', note: '素材与分类' },
  { key: 'storyboard', label: '分镜', note: '镜头与生成' },
  { key: 'edit', label: '剪辑', note: '审核与成片' },
];

const CATEGORY_MAP = {
  character: '角色参考', location: '场景参考', prop: '道具', style: '风格参考',
  footage: '视频素材', audio: '音频', export: '成片', other: '其他',
};
const CATEGORIES = ['character', 'location', 'prop', 'style', 'footage', 'audio', 'export', 'other'];

const ISSUE_TYPE_MAP = {
  story: '剧情连贯', character: '人物不一致', goof: '穿帮',
  unexpected: '非预期', text: '画面文字',
};

const RUN_STATES_BUSY = ['QUEUED', 'RENDERING', 'SCORING'];
const RUN_STATES_PRE = ['PLANNED', 'ASSET_READY', 'PROMPT_READY'];
// 生产编排事件:驱动「生成准备中」状态与工作室静默刷新
const PRODUCE_EVENTS = ['quick.started', 'casting.started', 'casting.completed',
                        'first_frame.started', 'first_frame.completed',
                        'quick.orchestrated', 'quick.failed',
                        'produce.stop_requested', 'produce.stopped',
                        'produce.resumed', 'produce.awaiting_confirm',
                        'produce.confirmed',
                        'run.cancel_requested', 'run.cancelled'];

/* ---------- 工具 ---------- */
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function toast(msg, kind) {
  const box = $('#toast-container');
  const el = document.createElement('div');
  el.className = 'toast' + (kind ? ' ' + kind : '');
  el.textContent = msg;
  box.appendChild(el);
  setTimeout(() => el.remove(), 4200);
}

function fmtBytes(n) {
  if (n == null || isNaN(n)) return '-';
  const units = ['B', 'KB', 'MB', 'GB'];
  let i = 0, v = Number(n);
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return v.toFixed(i === 0 ? 0 : 1) + ' ' + units[i];
}

function fmtElapsed(ms) {
  if (ms == null || ms < 0) return '-';
  const s = Math.floor(ms / 1000);
  if (s < 60) return s + 's';
  const m = Math.floor(s / 60);
  if (m < 60) return m + 'm' + (s % 60) + 's';
  const h = Math.floor(m / 60);
  return h + 'h' + (m % 60) + 'm';
}

function parseTime(t) {
  if (!t && t !== 0) return null;
  let v = t;
  // API 的 created_at / started_at 是秒级 Unix 时间戳,new Date(秒) 会被当成毫秒
  if (typeof v === 'number' && v > 0 && v < 1e12) v = v * 1000;
  if (typeof v === 'string' && /^[0-9]+(\.[0-9]+)?$/.test(v.trim())) {
    const n = Number(v.trim());
    if (n > 0 && n < 1e12) v = n * 1000;
  }
  const d = new Date(v);
  return isNaN(d.getTime()) ? null : d.getTime();
}

function newCommandId() {
  return crypto.randomUUID();
}

function categoryLabel(cat) {
  if (!cat) return '未分类';
  return CATEGORY_MAP[cat] || cat;
}

/* ---------- API ---------- */
async function api(path, opts) {
  opts = opts || {};
  const headers = opts.headers || {};
  if (opts.json !== undefined) {
    headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(opts.json);
  }
  const res = await fetch(path, {
    method: opts.method || 'GET',
    headers,
    body: opts.body,
  });
  if (!res.ok) {
    let detail = res.status + ' ' + res.statusText;
    try {
      const text = await res.text();
      if (text) detail += ' — ' + text.slice(0, 300);
    } catch (e) { /* ignore */ }
    throw new Error(detail);
  }
  const ct = res.headers.get('content-type') || '';
  if (ct.includes('application/json')) return res.json();
  return res;
}

function isVideoAsset(a) {
  const name = (a.filename || a.name || a.storage_key || '').toLowerCase();
  const mime = (a.mime || a.content_type || '').toLowerCase();
  return a.media_type === 'video' || mime.startsWith('video/') || /\.(mp4|webm|mov|mkv|avi)$/.test(name);
}

function isAudioAsset(a) {
  const name = (a.filename || a.name || a.storage_key || '').toLowerCase();
  const mime = (a.mime || a.content_type || '').toLowerCase();
  return a.media_type === 'audio' || mime.startsWith('audio/') || /\.(mp3|wav|m4a|aac|ogg|flac)$/.test(name);
}

function assetFileUrl(a) {
  return '/assets/' + encodeURIComponent(a.asset_id || a.id) + '/file';
}

function assetName(a) {
  const id = a.asset_id || a.id;
  return (a.storage_key || '').split('/').pop() || a.filename || a.name || id;
}

function openProjectVideo(assetId, title, kind) {
  openAssetPreview(assetId, title, 'video', kind);
}

// 通用资产预览:视频 / 图片 / 文本 共用同一个弹窗
function openAssetPreview(assetId, title, mediaType, kind) {
  const modal = $('#project-video-modal');
  if (!modal || !assetId) return;
  const suffix = kind === 'export' ? ' · 成片预览'
    : mediaType === 'image' ? ' · 图片预览'
      : mediaType === 'text' ? ' · 文本预览' : ' · 镜头预览';
  $('#project-video-title').textContent = (title || '作品') + suffix;
  const url = '/assets/' + encodeURIComponent(assetId) + '/file';
  const body = $('#project-video-body');
  if (mediaType === 'image') {
    body.innerHTML = '<img class="asset-preview-img" src="' + url + '" alt="">';
  } else if (mediaType === 'text') {
    body.innerHTML = '<pre class="asset-preview-text">加载中…</pre>';
    fetch(url).then((r) => r.text()).then((t) => {
      body.innerHTML = '<pre class="asset-preview-text">' + esc(t.slice(0, 30000)) + '</pre>';
    }).catch(() => {
      body.innerHTML = '<pre class="asset-preview-text">文本加载失败</pre>';
    });
  } else {
    body.innerHTML = '<video src="' + url +
      '" controls autoplay preload="metadata"></video>';
  }
  modal.classList.remove('hidden');
}

function closeProjectVideo() {
  const modal = $('#project-video-modal');
  if (!modal) return;
  const video = modal.querySelector('video');
  if (video) video.pause();
  $('#project-video-body').innerHTML = '';
  modal.classList.add('hidden');
}

$('#project-video-close').addEventListener('click', closeProjectVideo);
$('#project-video-modal').addEventListener('click', (e) => {
  if (e.target === e.currentTarget) closeProjectVideo();
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') closeProjectVideo();
});

/* ---------- Hash 路由 ---------- */
function parseHash() {
  const h = location.hash.slice(1) || '/';
  const qIdx = h.indexOf('?');
  const path = qIdx >= 0 ? h.slice(0, qIdx) : h;
  const params = new URLSearchParams(qIdx >= 0 ? h.slice(qIdx + 1) : '');
  const parts = path.split('/').filter(Boolean);
  if (parts[0] === 'projects') return { view: 'projects' };
  if (parts[0] === 'new') return { view: 'new' };
  if (parts[0] === 'chat') return { view: 'chat' };
  if (parts[0] === 'tasks') return { view: 'tasks' };
  if (parts[0] === 'assets') return { view: 'assets' };
  if (parts[0] === 'oneclick') {
    return { view: 'oneclick', project: params.get('project') || '' };
  }
  if (parts[0] === 'studio' && parts[1]) {
    return { view: 'studio', projectId: decodeURIComponent(parts[1]),
             step: params.get('step') || 'script', from: params.get('from') || '' };
  }
  return { view: 'dashboard' };
}

function go(hash) {
  if (location.hash === hash) render();
  else location.hash = hash;
}

window.addEventListener('hashchange', render);

async function render() {
  clearStepTimer();
  const r = parseHash();
  state.route = r;
  ['view-dashboard', 'view-new', 'view-studio', 'view-chat', 'view-tasks',
   'view-oneclick', 'view-assets'].forEach((id) => $('#' + id).classList.add('hidden'));
  // 侧栏一级导航激活态
  const navMap = { dashboard: 'home', projects: 'projects', new: 'new',
                   chat: 'chat', tasks: 'tasks', oneclick: 'oneclick',
                   assets: 'assets' };
  const activeNav = navMap[r.view] || '';
  $$('[data-dashboard-view]').forEach((a) => {
    a.classList.toggle('active', a.dataset.dashboardView === activeNav);
  });
  if (r.view === 'new') {
    closeSSE();
    $('#view-new').classList.remove('hidden');
  } else if (r.view === 'chat') {
    closeSSE();
    $('#view-chat').classList.remove('hidden');
    renderChatView();
  } else if (r.view === 'tasks') {
    closeSSE();
    $('#view-tasks').classList.remove('hidden');
    await renderTasksView();
  } else if (r.view === 'oneclick') {
    closeSSE();
    $('#view-oneclick').classList.remove('hidden');
    await renderOneclickView();
  } else if (r.view === 'assets') {
    closeSSE();
    $('#view-assets').classList.remove('hidden');
    await renderAssetsView();
  } else if (r.view === 'studio') {
    $('#view-studio').classList.remove('hidden');
    await enterStudio(r.projectId, r.step);
  } else {
    closeSSE();
    const dashboard = $('#view-dashboard');
    dashboard.classList.toggle('is-project-library', r.view === 'projects');
    dashboard.classList.remove('hidden');
    await renderDashboard();
  }
}

/* ---------- 视图一:片库 Dashboard ---------- */
$('#db-new-btn').addEventListener('click', () => go('#/new'));
$('#db-search').addEventListener('input', (e) => {
  state.dbQuery = e.target.value;
  renderProjectCards();
});
$('#db-sort').addEventListener('change', (e) => {
  state.dbSort = e.target.value;
  renderProjectCards();
});
$$('.db-filter').forEach((btn) => btn.addEventListener('click', () => {
  state.dbFilter = btn.dataset.filter;
  $$('.db-filter').forEach((item) => item.classList.toggle('active', item === btn));
  renderProjectCards();
}));
$$('.db-view-btn').forEach((btn) => btn.addEventListener('click', () => {
  state.dbView = btn.dataset.view;
  $$('.db-view-btn').forEach((item) => item.classList.toggle('active', item === btn));
  renderProjectCards();
}));

async function renderDashboard() {
  const grid = $('#db-grid');
  const isLibrary = state.route.view === 'projects';
  $('#db-section-label').textContent = isLibrary ? 'PROJECT LIBRARY' : 'RECENT PROJECTS';
  $('#db-section-title').textContent = isLibrary ? '我的项目' : '最近创作';
  grid.innerHTML = '<div class="panel db-empty">正在读取本地片库…</div>';
  try {
    const res = await api('/projects');
    state.projects = res.projects || res || [];
    state.taskSummary = res.task_summary || null;
  } catch (err) {
    grid.innerHTML = '<div class="panel db-empty">暂时无法读取片库，请确认本地服务已启动后刷新页面。</div>';
    renderHomeStudio([]);
    return;
  }
  renderProjectCards();
}

const SEG_DEFS = [
  { label: '剧本', lit: (pg) => num(pg.scenes) > 0 },
  { label: '角色', lit: (pg) => num(pg.characters) > 0 },
  { label: '场景', lit: (pg) => num(pg.locations != null ? pg.locations : pg.location) > 0 },
  { label: '道具', lit: (pg) => num(pg.props != null ? pg.props : pg.prop) > 0 },
  { label: '分镜', lit: (pg) => num(pg.shots) > 0 },
  { label: '剪辑', lit: (pg) => num(pg.shots) > 0 && num(pg.accepted) >= num(pg.shots) },
];

function num(v) { const n = Number(v); return isNaN(n) ? 0 : n; }

function projectStatus(p) {
  const pg = p.progress || {};
  if (SEG_DEFS[5].lit(pg)) return { key: 'complete', label: '已完成' };
  if (SEG_DEFS.some((s) => s.lit(pg))) return { key: 'active', label: '创作中' };
  return { key: 'draft', label: '草稿' };
}

function renderProjectCards() {
  const grid = $('#db-grid');
  const q = state.dbQuery.trim().toLowerCase();
  let list = state.projects.filter((p) => {
    const matchesQuery = !q || ((p.title || '') + ' ' + (p.genre || '')).toLowerCase().includes(q);
    const status = projectStatus(p).key;
    const matchesFilter = state.dbFilter === 'all' || status === state.dbFilter;
    return matchesQuery && matchesFilter;
  });
  list = list.slice().sort((a, b) => {
    if (state.dbSort === 'title') return String(a.title || '').localeCompare(String(b.title || ''), 'zh-CN');
    return String(b.updated_at || b.created_at || '').localeCompare(String(a.updated_at || a.created_at || ''));
  });
  const isLibrary = state.route.view === 'projects';
  const shownProjects = isLibrary ? list : list.slice(0, 3);
  grid.classList.toggle('is-list', isLibrary && state.dbView === 'list');
  if (!isLibrary) renderHomeStudio(state.projects);
  if (!state.projects.length) {
    grid.innerHTML = '<div class="panel db-empty">片场还是空的 — 点右上角「建立新片场」开始第一部短剧。</div>';
    return;
  }
  if (!shownProjects.length) {
    grid.innerHTML = '<div class="panel db-empty">没有匹配「' + esc(state.dbQuery) + '」的项目。</div>';
    return;
  }
  grid.innerHTML = shownProjects.map((p) => {
    const id = p.project_id || p.id;
    const pg = p.progress || {};
    const updated = p.updated_at || p.created_at;
    const updatedDate = updated ? new Date(updated) : null;
    const updatedTxt = updatedDate && !isNaN(updatedDate.getTime()) && updatedDate.getFullYear() >= 2000
      ? updatedDate.toLocaleDateString('zh-CN') : '';
    const status = projectStatus(p);
    const previewAssetId = p.preview_video_asset_id;
    const previewKind = p.preview_video_kind || 'clip';
    const ratio = p.aspect_ratio || p.ratio || '';
    const segs = SEG_DEFS.map((s, i) =>
      '<div><div class="proj-seg-bar' + (s.lit(pg) ? ' lit-' + i : '') + '"></div>' +
      '<div class="proj-seg-label">' + s.label + '</div></div>').join('');
    const stats = '剧本 ' + num(pg.scenes) + ' · 镜头 ' + num(pg.accepted) + '/' + num(pg.shots) +
      ' · 角色 ' + num(pg.characters) + ' · 定妆 ' + num(pg.portraits) + ' · 成片 ' + num(pg.exports);
    return '<article class="panel proj-card" data-id="' + esc(id) + '">' +
      '<div class="proj-cover">' +
        (previewAssetId ? '<video src="/assets/' + encodeURIComponent(previewAssetId) + '/file" preload="metadata" muted></video>' : '') +
        '<span class="proj-status ' + status.key + '">' + status.label + '</span>' +
        (previewAssetId ? '<button class="proj-play" data-asset-id="' + esc(previewAssetId) + '" data-title="' + esc(p.title || '项目') + '" data-kind="' + esc(previewKind) + '">▶ <span>播放</span></button>' : '') +
      '</div>' +
      '<div class="proj-card-main">' +
        '<div class="proj-card-updated">' + (updatedTxt ? 'UPDATED ' + esc(updatedTxt) : '') + '</div>' +
        '<h3 class="proj-card-title">' + esc(p.title || '(无标题)') + '</h3>' +
        '<div class="proj-card-meta">' + esc([p.genre, p.style, ratio].filter(Boolean).join(' / ') || '未设置风格') + '</div>' +
        '<p class="proj-card-brief">' + esc(p.brief || '尚未生成剧本') + '</p>' +
        '<div class="proj-progress">' + segs + '</div>' +
      '</div>' +
      '<div class="proj-card-foot"><span>' + esc(stats) + '</span>' +
      '<div class="proj-foot-actions">' +
        (p.stoppable ? '<button class="btn btn-small db-stop-btn" data-id="' + esc(id) + '" data-title="' + esc(p.title || id) + '">■ 停止</button>' : '') +
        '<button class="btn btn-ghost btn-small db-del-btn" data-id="' + esc(id) + '" data-title="' + esc(p.title || id) + '">删除</button>' +
      '</div></div>' +
    '</article>';
  }).join('');
  grid.querySelectorAll('.proj-card-main').forEach((el) => {
    el.addEventListener('click', () => go('#/studio/' + encodeURIComponent(el.closest('.proj-card').dataset.id)));
  });
  grid.querySelectorAll('.db-del-btn').forEach((btn) => {
    btn.addEventListener('click', () => deleteProject(btn.dataset.id, btn.dataset.title));
  });
  grid.querySelectorAll('.db-stop-btn').forEach((btn) => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      stopProject(btn.dataset.id, btn.dataset.title, btn);
    });
  });
  grid.querySelectorAll('.proj-play').forEach((btn) => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      openProjectVideo(btn.dataset.assetId, btn.dataset.title, btn.dataset.kind);
    });
  });
  bindCardTilt(grid);
}

function renderHomeStudio(projects) {
  const el = $('#home-studio');
  if (!el) return;
  const summary = state.taskSummary || { running_projects: 0, waiting_projects: 0, completed_projects: 0, eta_seconds: 0 };
  const eta = Number(summary.eta_seconds || 0);
  const etaText = eta ? '约 ' + Math.max(1, Math.ceil(eta / 60)) + ' 分钟后' : '暂无排队任务';
  const hasProjects = projects && projects.length;
  el.innerHTML = '<div class="home-studio-head"><div><div class="label">CURRENT TASKS</div><h2>当前工作状态</h2></div>' +
    '<span class="home-task-live"><i></i>' + (eta ? '队列运行中' : '队列空闲') + '</span></div>' +
    '<p class="home-task-intro">生成队列会在这里自动汇总，项目卡片仍可直接进入对应工作台。</p>' +
    '<div class="home-task-grid">' +
      '<div class="home-task-card running"><b>' + num(summary.running_projects) + '</b><span>正在执行</span></div>' +
      '<div class="home-task-card waiting"><b>' + num(summary.waiting_projects) + '</b><span>等待执行</span></div>' +
      '<div class="home-task-card complete"><b>' + num(summary.completed_projects) + '</b><span>已完成项目</span></div>' +
    '</div>' +
    '<div class="home-eta"><span>预计完成时间</span><strong>' + etaText + '</strong><small>' + (eta ? '按当前队列节奏估算' : (hasProjects ? '提交生成任务后将显示预估时间' : '创建项目后可在此查看任务状态')) + '</small></div>';
}

async function deleteProject(id, title) {
  if (!confirm('删除「' + title + '」?\n项目记录(剧本/分镜/任务)将被删除;\n本地素材与成片文件仍保留。')) return;
  try {
    await api('/projects/' + encodeURIComponent(id), { method: 'DELETE' });
    toast('项目已删除', 'ok');
    renderDashboard();
  } catch (err) {
    toast('删除失败:' + err.message, 'err');
  }
}

// 停止项目的全部执行(服务端行为,关掉浏览器也生效)
async function stopProject(id, title, btn) {
  const name = title || '当前项目';
  if (!confirm('停止「' + name + '」的全部执行?\n\n· 定妆/首帧会在当前这张图完成后停止\n· 在途镜头会取消(已验收的镜头与素材保留)\n· 之后可随时重新「开始生成」')) return;
  const original = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = '停止中…'; }
  try {
    await api('/projects/' + encodeURIComponent(id) + '/stop',
              { method: 'POST', json: {} });
    toast('已发送停止指令', 'ok');
    if (state.route && state.route.view === 'studio') await refreshQuiet();
    else await render();
  } catch (err) {
    toast('停止失败:' + err.message, 'err');
    if (btn) { btn.disabled = false; btn.textContent = original; }
  }
}

/* ---------- 视图二:建立片场 ---------- */
function bindPresetPills(containerSel, onPick) {
  $$(containerSel + ' .preset-pill').forEach((btn) => {
    btn.addEventListener('click', () => {
      $$(containerSel + ' .preset-pill').forEach((b) => b.classList.toggle('active', b === btn));
      onPick(btn.dataset.value);
    });
  });
}

bindPresetPills('#nf-genres', (v) => { state.nf.genre = v; });
bindPresetPills('#nf-styles', (v) => { state.nf.style = v; });

$$('.ratio-btn').forEach((btn) => {
  btn.addEventListener('click', () => {
    $$('.ratio-btn').forEach((b) => b.classList.toggle('active', b === btn));
    state.nf.ratio = btn.dataset.value;
  });
});

$('#nf-create-btn').addEventListener('click', async () => {
  const title = $('#nf-title').value.trim();
  if (!title) { toast('先给项目起个名字', 'err'); return; }
  const genre = $('#nf-genre-custom').value.trim() || state.nf.genre;
  const body = {
    title,
    style: state.nf.style,
    brief: $('#nf-brief').value.trim() || undefined,
    duration_target_s: Number($('#nf-duration').value) || 60,
    genre,
    aspect_ratio: state.nf.ratio,
  };
  const btn = $('#nf-create-btn');
  btn.disabled = true;
  btn.textContent = '正在建立片场…';
  try {
    const res = await api('/projects', { method: 'POST', json: body });
    const id = res.project_id || res.id || (res.project && (res.project.project_id || res.project.id));
    if (!id) throw new Error('响应中未找到项目 ID');
    toast('片场已建立,正在触发 AI 规划…', 'ok');
    // 立即触发规划;失败不阻塞进入工作室
    api('/projects/' + encodeURIComponent(id) + '/plan', { method: 'POST', json: {} })
      .catch((err) => toast('触发规划失败(可在剧本步重试):' + err.message, 'err'));
    go('#/studio/' + encodeURIComponent(id) + '?step=script');
  } catch (err) {
    toast('建立失败:' + err.message, 'err');
  } finally {
    btn.disabled = false;
    btn.textContent = '建立并进入剧本';
  }
});


/* ---------- 视图:对话出片 ---------- */
const CHAT_GREETING = '你好，我是制片助理。先聊聊你想拍的短剧吧——我会顺着问几个关键问题（主角、冲突、风格…），帮你想清楚；如果你一句话就说全了，也可以直接开拍。';

// 信息清单:对话引导的进度可视化(与后端 checklist 对齐)
const CHAT_CHECKLIST_FIELDS = [
  ['topic', '题材'], ['lead', '主角'], ['conflict', '冲突/反转'],
  ['ending', '结尾'], ['style', '风格'], ['duration', '时长'], ['aspect', '画幅'],
];

function mergeChatChecklist(next) {
  if (!next || typeof next !== 'object') return;
  if (!state.chat.checklist) state.chat.checklist = {};
  CHAT_CHECKLIST_FIELDS.forEach(([key]) => {
    const val = String(next[key] || '').trim();
    if (val) state.chat.checklist[key] = val;
  });
  drawChatChecklist();
}

function drawChatChecklist() {
  const el = $('#chat-checklist');
  if (!el) return;
  const data = state.chat.checklist || {};
  const missing = [];
  const rows = CHAT_CHECKLIST_FIELDS.map(([key, label]) => {
    const val = String(data[key] || '').trim();
    if (!val) missing.push(label);
    return '<div class="cc-row ' + (val ? 'done' : 'pending') + '">' +
      '<i>' + (val ? '✓' : '○') + '</i>' +
      '<span class="cc-label">' + label + '</span>' +
      '<span class="cc-value">' + (val ? esc(val) : '待聊') + '</span></div>';
  }).join('');
  const skip = state.chat.plan || state.chat.planBusy
    ? '' : '<button id="chat-skip-btn" class="btn btn-ghost btn-small cc-skip">⏭ 不想聊了，直接开拍</button>';
  el.innerHTML = rows +
    (missing.length ? '<div class="cc-missing">还差：' + esc(missing.join('、')) + '</div>' : '') +
    skip;
  const btn = $('#chat-skip-btn');
  if (btn) {
    btn.addEventListener('click', () => {
      const input = $('#chat-input');
      input.value = '都可以，你定吧，直接开始';
      sendChatMessage();
    });
  }
}

function renderChatView() {
  if (!state.chat.messages.length) {
    state.chat.messages = [{ role: 'assistant', content: CHAT_GREETING }];
  }
  drawChatMessages();
  drawChatChecklist();
  autoGrowChatInput($('#chat-input'));
  $('#chat-input').focus();
}

// 输入框随内容长高(上限约 6 行)
function autoGrowChatInput(el) {
  if (!el) return;
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 148) + 'px';
}

function chatThinkingRow(text, who) {
  return '<div class="chat-row bot">' +
    '<span class="chat-avatar bot">✦</span>' +
    '<div class="chat-body"><div class="chat-name">' + esc(who || '制片助理') + '</div>' +
    '<div class="chat-bubble chat-typing">' +
      '<span class="tdot"></span><span class="tdot"></span><span class="tdot"></span>' +
      '<span class="chat-typing-text">' + esc(text) + '</span>' +
    '</div></div></div>';
}

// 顶部三步进度:聊需求 → 看分镜 → 一键出片(圆环节点)
function updateChatSteps() {
  const steps = $$('#chat-steps li');
  if (!steps.length) return;
  const hasPlan = !!state.chat.plan;
  const states = [
    'done',
    hasPlan ? 'done' : (state.chat.planBusy ? 'on' : ''),
    hasPlan ? (state.chat.producing ? 'done' : 'on') : '',
  ];
  steps.forEach((li, i) => {
    li.className = states[i] || '';
    const dot = li.querySelector('i');
    if (dot) {
      dot.textContent = states[i] === 'done' ? '✓' : String(i + 1).padStart(2, '0');
    }
  });
}

function updateChatStatus() {
  const el = $('#chat-status');
  if (!el) return;
  let text = '在线 · 需求齐了自动拆解分镜';
  if (state.chat.busy) text = '正在理解你的需求…';
  else if (state.chat.planBusy) text = '导演正在拆解分镜，约需 1 分钟…';
  else if (state.chat.producing) text = '正在出片，去工作室看进度…';
  else if (state.chat.plan) text = '分镜已就绪 · 确认后点「一键出片」';
  el.innerHTML = '<i class="chat-dot"></i>' + esc(text);
}

function drawChatMessages() {
  const list = $('#chat-list');
  let html = state.chat.messages.map((m) => {
    const mine = m.role === 'user';
    return '<div class="chat-row ' + (mine ? 'user' : 'bot') + '">' +
      '<span class="chat-avatar ' + (mine ? 'me' : 'bot') + '">' +
        (mine ? '我' : '✦') + '</span>' +
      '<div class="chat-body">' +
        '<div class="chat-name">' + (mine ? '你' : '制片助理') + '</div>' +
        '<div class="chat-bubble">' + esc(m.content) + '</div>' +
      '</div></div>';
  }).join('');
  if (state.chat.busy) html += chatThinkingRow('正在想…');
  if (state.chat.planBusy) {
    html += chatThinkingRow('正在把需求拆成完整分镜（场次/角色/每镜结构化提示词）…',
                            '导演');
  }
  if (state.chat.plan) html += renderChatPlan(state.chat.plan);
  if (state.chat.planError) html += renderChatPlanError();
  list.innerHTML = html;
  bindChatPlanEvents();
  updateChatSteps();
  updateChatStatus();
  const send = $('#chat-send');
  if (send) send.disabled = !!state.chat.busy;
  // 方案首次出现时定位到分镜开头,便于从头过目;其他情况保持在最新消息
  if (state.chat.plan && !state.chat.planShown) {
    state.chat.planShown = true;
    const planEl = list.querySelector('.chat-plan');
    if (planEl) list.scrollTop = Math.max(0, planEl.offsetTop - list.offsetTop - 8);
  } else {
    list.scrollTop = list.scrollHeight;
  }
}

/* ---- 方案卡:预览结构化分镜,点「一键出片」才落库生成 ---- */
function chatPlanStats(plan) {
  const scenes = plan.scenes || [];
  let shots = 0, duration = 0;
  scenes.forEach((sc) => (sc.shots || []).forEach((sh) => {
    shots += 1;
    duration += Number(sh.duration_s) || 0;
  }));
  return { scenes: scenes.length, shots, duration };
}

function renderChatPlan(plan) {
  const st = chatPlanStats(plan);
  const chips = [
    plan.style || '',
    (plan.duration_target_s || st.duration) + 's',
    plan.aspect_ratio || '16:9',
    st.scenes + ' 场 · ' + st.shots + ' 镜',
  ].filter(Boolean).map((t) => '<span class="chat-chip">' + esc(t) + '</span>').join('');
  const noteChip = plan.constraint_note
    ? '<span class="chat-chip accent">✓ ' + esc(plan.constraint_note) + '</span>' : '';
  let shotNo = 0;
  const scenes = (plan.scenes || []).map((sc, si) => {
    const shots = (sc.shots || []).map((sh) => {
      shotNo += 1;
      return renderPlanShot(sh, shotNo);
    }).join('');
    return '<div class="cps-scene">' +
      '<div class="cps-scene-head">' +
        '<i class="cps-scene-no">' + String(si + 1).padStart(2, '0') + '</i>' +
        '<b>' + esc(sc.title || '场景') + '</b>' +
        '<span>' + (sc.shots || []).length + ' 镜</span></div>' +
      (shots || '<p class="empty-hint">本场没有可用镜头</p>') + '</div>';
  }).join('');
  const producing = state.chat.producing;
  return '<div class="chat-plan">' +
    '<div class="chat-plan-head">' +
      '<div><div class="label">STORYBOARD READY</div>' +
      '<h3>' + esc(plan.title || '出片方案') + '</h3></div>' +
      '<div class="chat-plan-meta">' + noteChip + chips + '</div>' +
    '</div>' +
    (plan.brief ? '<p class="chat-plan-brief">' + esc(plan.brief) + '</p>' : '') +
    '<div class="chat-plan-scenes">' + scenes + '</div>' +
    '<div class="chat-plan-actions">' +
      '<span class="chat-plan-hint">确认分镜后开始:定妆参考图 → 镜头首帧 → 逐镜生成 → 质检,可在工作室随时改词重生成。</span>' +
      '<button id="chat-replan" class="btn btn-ghost btn-small"' + (producing ? ' disabled' : '') + '>重新拆解</button>' +
      '<button id="chat-produce" class="btn btn-projector"' + (producing ? ' disabled' : '') + '>' +
        (producing ? '正在开机…' : '✦ 一键出片') + '</button>' +
    '</div></div>';
}

function planRow(label, value) {
  const text = String(value == null ? '' : value).trim();
  if (!text) return '';
  return '<div class="cps-row"><span>' + esc(label) + '</span><p>' + esc(text) + '</p></div>';
}

function renderPlanShot(shot, n) {
  const motion = shot.motion_contract || {};
  const cam = shot.camera || {};
  const camText = [cam.shot, cam.movement].filter(Boolean).join(' · ');
  const objects = (shot.object_states || []).map((o) => {
    const count = o.count ? '(' + o.count + ')' : '';
    const state = (o.start_state || o.end_state)
      ? '：' + (o.start_state || '保持现状') + ' → ' + (o.end_state || '保持') : '';
    return (o.name || '') + count + state;
  }).filter(Boolean).join('；');
  const beats = shot.beats || {};
  const beatText = [
    (beats.already_happened || []).length ? '已演不重演: ' + beats.already_happened.join('；') : '',
    (beats.this_clip_only || []).length ? '本镜独占: ' + beats.this_clip_only.join('；') : '',
    (beats.reserved_for_later || []).length ? '后续预留: ' + beats.reserved_for_later.join('；') : '',
  ].filter(Boolean).join(' | ');
  const acceptance = shot.acceptance || {};
  const acceptText = [
    (acceptance.required || []).length ? '必达: ' + acceptance.required.join('；') : '',
    (acceptance.forbidden || []).length ? '禁止: ' + acceptance.forbidden.join('；') : '',
  ].filter(Boolean).join(' | ');
  const details = [
    planRow('剧情节拍', shot.narrative_beat),
    planRow('主动作', motion.primary_motion),
    planRow('次运动', motion.secondary_motion),
    planRow('开始状态', motion.start_state),
    planRow('结束状态', motion.end_state),
    planRow('运镜', motion.camera_motion || camText),
    planRow('光线与调色', shot.lighting_palette),
    planRow('关键物体', objects),
    planRow('节拍', beatText),
    planRow('验收', acceptText),
    planRow('内心意图', shot.felt_intent),
  ].filter(Boolean).join('');
  return '<div class="cps-shot">' +
    '<div class="cps-head"><span class="cps-no">SHOT ' + String(n).padStart(2, '0') + '</span>' +
      '<span class="cps-dur">' + esc(shot.duration_s || 5) + 's</span>' +
      (shot.dialogue ? '<span class="cps-tag">台词</span>' : '') +
      (camText ? '<span class="cps-tag">' + esc(camText) + '</span>' : '') +
    '</div>' +
    '<div class="cps-action">' + esc(shot.action || '') + '</div>' +
    (shot.dialogue ? '<div class="cps-dialogue">“' + esc(shot.dialogue) + '”</div>' : '') +
    (details ? '<details class="cps-more"><summary>结构化提示词</summary>' +
      '<div class="cps-grid">' + details + '</div></details>' : '') +
  '</div>';
}

function renderChatPlanError() {
  return '<div class="chat-plan chat-plan-error">' +
    '<div class="chat-plan-head"><div><div class="label">PLAN FAILED</div>' +
    '<h3>分镜拆解失败</h3></div></div>' +
    '<p class="chat-plan-brief">' + esc(state.chat.planError) + '</p>' +
    '<div class="chat-plan-actions">' +
      '<span class="chat-plan-hint">可以重试拆解,或直接走「一键出片」全自动链路(后台自动规划)。</span>' +
      '<button id="chat-legacy" class="btn btn-ghost btn-small">一键出片（全自动）</button>' +
      '<button id="chat-replan" class="btn btn-projector">重试拆解</button>' +
    '</div></div>';
}

function bindChatPlanEvents() {
  const produce = $('#chat-produce');
  if (produce) produce.addEventListener('click', chatProduceFromPlan);
  const replan = $('#chat-replan');
  if (replan) replan.addEventListener('click', () => requestChatPlan(state.chat.proposal, true));
  const legacy = $('#chat-legacy');
  if (legacy) legacy.addEventListener('click', () => chatQuickFallback(state.chat.proposal));
}

async function sendChatMessage() {
  const input = $('#chat-input');
  const text = input.value.trim();
  if (!text || state.chat.busy) return;
  input.value = '';
  autoGrowChatInput(input);
  state.chat.messages.push({ role: 'user', content: text });
  state.chat.busy = true;
  drawChatMessages();
  try {
    const res = await api('/assistant/chat', {
      method: 'POST', json: { messages: state.chat.messages },
    });
    state.chat.messages.push({ role: 'assistant', content: res.reply || '…' });
    mergeChatChecklist(res.checklist);      // 引导进度:信息清单
    if (res.proposal && res.proposal.brief) {
      state.chat.proposal = res.proposal;
      state.chat.plan = null;
      state.chat.planShown = false;
      state.chat.planError = '';
      requestChatPlan(res.proposal);   // 需求齐全:立刻拆结构化分镜供预览
    }
  } catch (err) {
    state.chat.messages.push({ role: 'assistant',
      content: '制片助理暂时不可用:' + err.message });
  } finally {
    state.chat.busy = false;
    drawChatMessages();
    $('#chat-input').focus();
  }
}

async function requestChatPlan(proposal, force) {
  if (!proposal || !proposal.brief) return;
  const token = ++state.chat.planToken;
  state.chat.planBusy = true;
  state.chat.planError = '';
  state.chat.producing = false;
  if (force) { state.chat.plan = null; state.chat.planShown = false; }
  drawChatMessages();
  try {
    const res = await api('/assistant/plan', {
      method: 'POST',
      json: { messages: state.chat.messages, proposal },
    });
    if (token !== state.chat.planToken) return;      // 已有更新的拆解请求
    state.chat.plan = res.plan || null;
    if (!state.chat.plan) state.chat.planError = '方案为空,请重试';
  } catch (err) {
    if (token !== state.chat.planToken) return;
    state.chat.planError = err.message;
  } finally {
    if (token === state.chat.planToken) {
      state.chat.planBusy = false;
      drawChatMessages();
    }
  }
}

async function chatProduceFromPlan() {
  const plan = state.chat.plan;
  const proposal = state.chat.proposal;
  if (!plan || !proposal || state.chat.producing) return;
  state.chat.producing = true;
  drawChatMessages();
  const body = {
    title: proposal.title || plan.title || proposal.brief.slice(0, 15),
    brief: proposal.brief,
    style: proposal.style || '',
    duration_target_s: Number(proposal.duration_target_s) || 60,
    aspect_ratio: proposal.aspect_ratio || '16:9',
    mode: 'text',
    plan,
  };
  try {
    const res = await api('/projects/quick', { method: 'POST', json: body });
    const pid = res.project_id || res.id;
    if (!pid) throw new Error('响应中未找到 project_id');
    toast('分镜已填入工作室:请过目/修改,确认后点「开始生成」', 'ok');
    state.chat.messages = [];
    state.chat.plan = null;
    state.chat.planShown = false;
    state.chat.proposal = null;
    go('#/studio/' + encodeURIComponent(pid) + '?step=storyboard&from=chat');
  } catch (err) {
    toast('出片失败:' + err.message, 'err');
    state.chat.producing = false;
    drawChatMessages();
  }
}

async function chatQuickFallback(proposal) {
  // 结构化拆解失败时的兜底:一键出片全自动链路(后台编剧→角色→导演链路)
  if (!proposal || !proposal.brief || state.chat.producing) return;
  state.chat.producing = true;
  drawChatMessages();
  const body = {
    title: proposal.title || proposal.brief.slice(0, 15),
    brief: proposal.brief,
    duration_target_s: Number(proposal.duration_target_s) || 60,
    aspect_ratio: proposal.aspect_ratio || '16:9',
    mode: 'text',
  };
  if (proposal.style) body.style = proposal.style;
  try {
    const res = await api('/projects/quick', { method: 'POST', json: body });
    const pid = res.project_id || res.id;
    if (!pid) throw new Error('响应中未找到 project_id');
    toast('已按标准链路开机,去工作室看进度', 'ok');
    state.chat.messages = [];
    state.chat.plan = null;
    state.chat.planShown = false;
    state.chat.proposal = null;
    state.chat.planError = '';
    go('#/studio/' + encodeURIComponent(pid) + '?step=storyboard');
  } catch (err) {
    toast('出片失败:' + err.message, 'err');
    state.chat.producing = false;
    drawChatMessages();
  }
}

$('#chat-send').addEventListener('click', sendChatMessage);
$('#chat-input').addEventListener('input', (e) => autoGrowChatInput(e.target));
$('#chat-input').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendChatMessage();
  }
});
// 快捷开始:点击填入输入框,可改完再发
$$('.rail-suggest button').forEach((btn) => {
  btn.addEventListener('click', () => {
    const input = $('#chat-input');
    input.value = btn.dataset.suggest || '';
    autoGrowChatInput(input);
    input.focus();
  });
});
$('#chat-reset').addEventListener('click', () => {
  state.chat.planToken += 1;      // 作废在途拆解结果
  state.chat.messages = [];
  state.chat.busy = false;
  state.chat.planBusy = false;
  state.chat.producing = false;
  state.chat.plan = null;
  state.chat.planShown = false;
  state.chat.planError = '';
  state.chat.proposal = null;
  state.chat.checklist = {};
  renderChatView();
});

/* ---------- 步骤流(圆环节点):任务中心与一键出片共用 ---------- */
const STEP_FLOW = [
  ['plan', '规划分镜'], ['cast', '定妆参考图'], ['frame', '镜头首帧'],
  ['render', '逐镜渲染'], ['judge', '质检'], ['compose', '拼接成片'],
];
const STEP_STAGE_PCT = { planning: 8, preparing: 26, casting: 26, first_frame: 42,
                         queued: 48, rendering: 48, scoring: 72, review: 72,
                         awaiting_confirm: 65, done: 100, planned: 10, empty: 3 };
const STEP_ACTIVE = { planning: 'plan', preparing: 'frame', casting: 'cast',
                      first_frame: 'frame', queued: 'render', rendering: 'render',
                      scoring: 'judge', review: 'judge' };

function stepFlowState(stage, counts) {
  const c = counts || {};
  const doneSet = new Set();
  if ((c.shots || 0) > 0 || ['queued', 'rendering', 'scoring', 'review',
                             'awaiting_confirm', 'done'].includes(stage)) {
    doneSet.add('plan');
  }
  if (['first_frame', 'queued', 'rendering', 'scoring', 'review',
       'awaiting_confirm', 'done'].includes(stage)) {
    doneSet.add('cast');
    doneSet.add('frame');
  }
  if (['rendering', 'scoring', 'review', 'done'].includes(stage)) doneSet.add('render');
  if (['scoring', 'review', 'done'].includes(stage)) doneSet.add('judge');
  if (stage === 'done' && (c.exports || 0) > 0) doneSet.add('compose');
  const activeName = STEP_ACTIVE[stage] || '';
  const pct = (stage === 'rendering' && c.shots)
    ? Math.round(48 + 24 * (c.accepted || 0) / c.shots)
    : (STEP_STAGE_PCT[stage] || 0);
  return { doneSet, activeName, pct };
}

function renderStepFlow(stage, counts, opts) {
  const compact = !!(opts && opts.compact);
  const { doneSet, activeName } = stepFlowState(stage, counts);
  const nodes = STEP_FLOW.map(([key, label], i) => {
    const done = doneSet.has(key);
    const active = !done && key === activeName;
    return '<li class="' + (done ? 'done' : active ? 'on' : '') + '">' +
      '<span class="oc-step-dot">' + (done ? '✓' : (i + 1)) + '</span>' +
      '<span class="oc-step-label">' + label + '</span></li>';
  }).join('');
  return '<ul class="step-flow' + (compact ? ' compact' : '') + '">' + nodes + '</ul>';
}

/* ---------- 视图:任务中心 #/tasks ---------- */
const TASK_STATE = {
  PLANNED: ['st-idle', '待开始'], ASSET_READY: ['st-idle', '准备素材'],
  PROMPT_READY: ['st-idle', '提示词就绪'], QUEUED: ['st-busy', '排队中'],
  RENDERING: ['st-busy', '渲染中'], GENERATED: ['st-busy', '已生成'],
  NORMALIZING: ['st-busy', '规范化'], SCORING: ['st-busy', '质检中'],
  REPAIRING: ['st-busy', '修复中'], RETRY_WAIT: ['st-idle', '等待重试'],
  HUMAN_REVIEW: ['st-review', '待审核'], ACCEPTED: ['st-done', '已完成'],
  CANCEL_REQUESTED: ['st-idle', '取消中'], CANCELLED: ['st-fail', '已取消'],
  FAILED: ['st-fail', '失败'],
};
const TASK_BUSY_STAGES = ['planning', 'preparing', 'casting', 'first_frame',
                          'rendering', 'scoring'];

function taskStateInfo(stateName) {
  return TASK_STATE[stateName] || ['st-idle', stateName || '-'];
}

async function renderTasksView() {
  await refreshTasks();
  state.stepTimer = setInterval(refreshTasks, 5000);   // 任务页 5 秒轮询
}

async function refreshTasks() {
  try {
    state.tasks = await api('/tasks');
  } catch (err) {
    const list = $('#tasks-list');
    if (list) {
      list.innerHTML = '<div class="panel db-empty">任务加载失败：' +
        esc(err.message) + '</div>';
    }
    return;
  }
  drawTasks();
}

function drawTasks() {
  const data = state.tasks || { summary: {}, projects: [] };
  const sum = data.summary || {};
  const paused = !!sum.paused;
  const pauseBtn = $('#tasks-pause-btn');
  if (pauseBtn) {
    pauseBtn.textContent = paused ? '▶ 恢复全部' : '⏸ 暂停全部';
    pauseBtn.classList.toggle('btn-projector', paused);
  }
  const summaryEl = $('#tasks-summary');
  if (summaryEl) {
    const stats = [
      ['进行中', sum.active || 0, 'on'],
      ['排队 / 等待', sum.waiting || 0, ''],
      ['待确认', sum.awaiting || 0, 'review'],
      ['待人工审核', sum.review || 0, 'review'],
      ['已完成', sum.completed || 0, 'done'],
      ['待生成 / 空白', sum.idle || 0, ''],
    ];
    const prevStats = state.tasksStatPrev || {};
    summaryEl.innerHTML =
      (paused ? '<div class="tasks-paused">⏸ 引擎已暂停：在途任务不再前进' +
        '<button id="tasks-resume-inline" class="btn btn-small">▶ 恢复</button></div>' : '') +
      '<div class="tasks-stats">' + stats.map(([label, n, cls]) =>
        '<div class="tasks-stat ' + cls + '"><b data-from="' +
        (prevStats[label] || 0) + '">' + n + '</b><span>' + label +
        '</span></div>').join('') + '</div>';
    state.tasksStatPrev = {};
    stats.forEach(([label, n]) => { state.tasksStatPrev[label] = n; });
    $$('#tasks-summary .tasks-stat b').forEach((el) => {
      animateCount(el, Number(el.textContent) || 0);
    });
  }
  const list = $('#tasks-list');
  if (!list) return;
  const projects = (data.projects || []).filter(
    (p) => (p.counts && p.counts.shots) || p.stoppable);
  if (!projects.length) {
    list.innerHTML = '<div class="panel db-empty">没有进行中的任务。' +
      '去「对话出片」或「创建项目」开始一部短剧吧。</div>';
    return;
  }
  list.innerHTML = projects.map(renderTaskProject).join('');
  bindTaskEvents();
}

function renderTaskProject(p) {
  const info = taskStateInfo(p.stage);
  const c = p.counts || {};
  const flow = stepFlowState(p.stage, c);
  const busy = TASK_BUSY_STAGES.includes(p.stage);
  const currentLine = p.current
    ? '<div class="task-current">' + (busy ? '<span class="spinner"></span>' : '') +
      '<span class="task-current-text">' + esc(p.current.summary || p.stage_label) +
      '</span><em>#' + esc(p.current.shot_no || '-') + ' · 已耗时 ' +
      fmtElapsed((p.current.elapsed_s || 0) * 1000) + '</em></div>'
    : (p.last_event
      ? '<div class="task-current idle"><span class="task-current-text">' +
        esc(p.last_event.summary || p.last_event.type) + '</span></div>'
      : '');
  const runs = (p.runs || []).filter(
    (r) => r.cancellable || r.state === 'FAILED' || r.state === 'CANCELLED');
  const runsHtml = runs.map((r) => {
    const rInfo = taskStateInfo(r.state);
    return '<div class="task-run">' +
      '<span class="task-run-shot">SHOT ' + String(r.shot_no || '?').padStart(2, '0') + '</span>' +
      '<span class="status-badge ' + rInfo[0] + '">' + esc(rInfo[1]) + '</span>' +
      '<span class="task-run-meta">seed ' + esc(r.seed) +
        (r.repair_count ? ' · 修复 ' + r.repair_count : '') +
        (r.verdict ? ' · ' + esc(r.verdict) : '') +
        ' · ' + fmtElapsed((r.elapsed_s || 0) * 1000) + '</span>' +
      '<span class="task-run-summary">' + esc((r.summary || '').slice(0, 60)) + '</span>' +
      '<span class="task-run-actions">' +
        (r.retryable ? '<button class="btn btn-small task-retry-btn" data-run="' +
          esc(r.run_id) + '">重试</button>' : '') +
        (r.cancellable ? '<button class="btn btn-small btn-danger task-cancel-btn" data-run="' +
          esc(r.run_id) + '">取消</button>' : '') +
      '</span></div>';
  }).join('');
  const stats = [
    '镜头 ' + (c.accepted || 0) + '/' + (c.shots || 0),
    '进行 ' + (c.active || 0),
    '排队 ' + (c.queued || 0),
    (c.review ? '待审核 ' + c.review : ''),
    (c.failed ? '失败 ' + c.failed : ''),
    '成片 ' + (c.exports || 0),
  ].filter(Boolean).join(' · ');
  return '<article class="panel task-card" data-id="' + esc(p.project_id) + '">' +
    '<div class="task-card-head">' +
      '<div class="task-card-title">' +
        '<span class="status-badge ' + info[0] + '">' + esc(p.stage_label) + '</span>' +
        '<h3>' + esc(p.title || '(无标题)') + '</h3>' +
        '<span class="task-card-meta">' +
          esc([p.style, p.duration_target_s ? p.duration_target_s + 's' : '',
               p.aspect_ratio].filter(Boolean).join(' / ')) + '</span>' +
      '</div>' +
      '<div class="task-card-actions">' +
        (p.awaiting_confirm ? '<button class="btn btn-projector btn-small task-confirm-btn" data-id="' +
          esc(p.project_id) + '">✅ 确认生成镜头</button>' : '') +
        '<button class="btn btn-small task-open-btn" data-id="' + esc(p.project_id) +
          '">打开工作室</button>' +
        (p.stoppable ? '<button class="btn btn-small btn-danger task-stop-btn" data-id="' +
          esc(p.project_id) + '" data-title="' + esc(p.title || '') +
          '">■ 停止</button>' : '') +
      '</div>' +
    '</div>' +
    renderStepFlow(p.stage, c, { compact: true }) +
    '<div class="task-card-stats">' + esc(stats) +
      '<span class="task-pct">' + flow.pct + '%</span></div>' +
    currentLine +
    (runs.length ? '<details class="task-runs"><summary>任务明细（' + runs.length +
      '）</summary>' + runsHtml + '</details>' : '') +
  '</article>';
}

function bindTaskEvents() {
  $$('.task-open-btn').forEach((btn) => btn.addEventListener('click', () => {
    go('#/studio/' + encodeURIComponent(btn.dataset.id) + '?step=storyboard');
  }));
  $$('.task-stop-btn').forEach((btn) => btn.addEventListener('click', () =>
    tasksStopProject(btn.dataset.id, btn.dataset.title, btn)));
  $$('.task-confirm-btn').forEach((btn) => btn.addEventListener('click', () =>
    tasksConfirmProject(btn.dataset.id, btn)));
  $$('.task-cancel-btn').forEach((btn) => btn.addEventListener('click', () =>
    tasksRunCommand(btn.dataset.run, 'cancel', btn)));
  $$('.task-retry-btn').forEach((btn) => btn.addEventListener('click', () =>
    tasksRunCommand(btn.dataset.run, 'retry', btn)));
  const resume = $('#tasks-resume-inline');
  if (resume) resume.addEventListener('click', () => tasksTogglePause(resume));
}

async function tasksStopProject(id, title, btn) {
  if (!confirm('停止「' + (title || '当前项目') + '」的全部执行?\n\n' +
      '· 定妆/首帧会在当前这张图完成后停止\n' +
      '· 在途镜头会取消(已验收的镜头与素材保留)\n' +
      '· 之后可随时重新「开始生成」')) return;
  btn.disabled = true;
  btn.textContent = '停止中…';
  try {
    await api('/projects/' + encodeURIComponent(id) + '/stop',
              { method: 'POST', json: {} });
    toast('已发送停止指令', 'ok');
    await refreshCurrentView();
  } catch (err) {
    toast('停止失败:' + err.message, 'err');
    btn.disabled = false;
    btn.textContent = '■ 停止';
  }
}

async function tasksConfirmProject(pid, btn) {
  btn.disabled = true;
  btn.textContent = '正在开始渲染…';
  try {
    await api('/projects/' + encodeURIComponent(pid) + '/confirm',
              { method: 'POST', json: {} });
    toast('已确认：开始逐镜渲染，完成后自动拼接', 'ok');
    await refreshCurrentView();
  } catch (err) {
    toast('确认失败:' + err.message, 'err');
    btn.disabled = false;
    btn.textContent = '✅ 确认生成镜头';
  }
}

// 动作完成后刷新「当前所在页面」的数据
async function refreshCurrentView() {
  const v = state.route && state.route.view;
  if (v === 'tasks') await refreshTasks();
  else if (v === 'oneclick' && state.oneclick) await ocRefresh(state.oneclick.projectId);
  else if (v === 'studio') await refreshQuiet();
  else await render();
}

async function tasksRunCommand(runId, action, btn) {
  const label = action === 'retry' ? '重试' : '取消';
  if (action === 'cancel' && !confirm('取消这个镜头任务?已生成的 TAKE 会保留。')) return;
  btn.disabled = true;
  try {
    await api('/runs/' + encodeURIComponent(runId) + '/commands',
              { method: 'POST', json: { action, command_id: newCommandId() } });
    toast('已提交' + label + '指令', 'ok');
    await refreshTasks();
  } catch (err) {
    toast(label + '失败:' + err.message, 'err');
    btn.disabled = false;
  }
}

async function tasksTogglePause(btn) {
  const paused = !!(state.tasks && state.tasks.summary && state.tasks.summary.paused);
  if (btn) btn.disabled = true;
  try {
    await api(paused ? '/engine/resume' : '/engine/pause',
              { method: 'POST', json: {} });
    toast(paused ? '已恢复执行' : '已暂停执行', 'ok');
    await refreshTasks();
  } catch (err) {
    toast('操作失败:' + err.message, 'err');
    if (btn) btn.disabled = false;
  }
}

$('#tasks-refresh-btn').addEventListener('click', refreshTasks);
$('#tasks-pause-btn').addEventListener('click', (e) => tasksTogglePause(e.currentTarget));

/* ---------- 视图:一键出片 #/oneclick ---------- */

function ocCnNumber(raw) {
  const D = { 零: 0, 一: 1, 二: 2, 两: 2, 三: 3, 四: 4, 五: 5, 六: 6, 七: 7, 八: 8, 九: 9 };
  raw = String(raw || '').trim();
  if (!raw) return null;
  if (/^\d+$/.test(raw)) return Number(raw);
  if (raw.includes('十')) {
    const [l, r] = raw.split('十');
    return (l ? (D[l] || 1) : 1) * 10 + (r ? (D[r] || 0) : 0);
  }
  let v = 0;
  for (const ch of raw) { if (!(ch in D)) return null; v = v * 10 + D[ch]; }
  return v || null;
}

// 与后端 extract_oneclick_params 对齐的即时预判(仅用于输入提示)
function ocExtract(text) {
  const out = {};
  const mDur = text.match(/([0-9]{1,4}|[一二两三四五六七八九十]{1,3})\s*(?:秒|s\b)/i);
  if (mDur) {
    const n = ocCnNumber(mDur[1]);
    if (n >= 10 && n <= 600) out.duration = n;
  }
  const aspects = [['9:16', ['9:16', '9比16', '九比十六', '竖屏', '竖版', '竖向']],
                   ['1:1', ['1:1', '1比1', '一比一', '方形', '方屏']],
                   ['16:9', ['16:9', '16比9', '十六比九', '横屏', '横版', '横向']]];
  for (const [a, hs] of aspects) {
    if (hs.some((h) => text.includes(h))) { out.aspect = a; break; }
  }
  const mShots = text.match(/([0-9]{1,3}|[一二两三四五六七八九十]{1,3})\s*(?:个|段)?\s*(?:镜头|分镜|镜)/);
  if (mShots) {
    const n = ocCnNumber(mShots[1]);
    if (n) out.shots = n;
  }
  const mSec = text.match(/每(?:个)?(?:镜头|分镜|镜|视频)\s*(?:约|大概|各)?\s*([0-9]{1,2})\s*秒/);
  if (mSec) out.shotSeconds = Number(mSec[1]);
  return out;
}

function ocUpdateHints() {
  const el = $('#oc-hints');
  const input = $('#oc-input');
  if (!el || !input) return;
  const parsed = ocExtract(input.value.trim());
  const chips = [];
  if (parsed.duration) chips.push(parsed.duration + ' 秒');
  if (parsed.aspect) chips.push(parsed.aspect);
  if (parsed.shots) chips.push(parsed.shots + ' 个镜头');
  if (parsed.shotSeconds) chips.push('每镜 ' + parsed.shotSeconds + ' 秒');
  el.innerHTML = chips.length
    ? '<span class="oc-chip dim">识别到</span>' + chips.map((t) =>
        '<span class="oc-chip">' + esc(t) + '</span>').join('')
    : '<span class="oc-chip dim">未指定项将自动补默认（30 秒 · 9:16）</span>';
}

function ocUpdateMode() {
  $$('#oc-modes .oc-mode').forEach((b) => {
    b.classList.toggle('active', b.dataset.mode === state.ocMode);
  });
  const note = $('#oc-note');
  if (note) {
    note.textContent = state.ocMode === 'confirm'
      ? '素材后确认：定妆/场景图/首帧生成完先停下，你确认后再逐镜渲染并自动拼接。'
      : '全自动：规划 → 定妆 → 首帧 → 渲染 → 质检 → 自动拼接，中途不停。';
  }
}

/* ---- 材料上传(md/txt/docx):材料会被逐段覆盖生成 ---- */
const OC_TEXT_EXTS = ['md', 'markdown', 'txt', 'json', 'csv', 'srt', 'log'];
const OC_MATERIAL_MAX_CHARS = 24000;
const OC_DOCX_MAX_BYTES = 8 * 1024 * 1024;

function ocRenderMaterialChip() {
  const chip = $('#oc-material-chip');
  if (!chip) return;
  const mat = state.ocMaterial;
  if (!mat) {
    chip.classList.add('hidden');
    chip.innerHTML = '';
    return;
  }
  const size = mat.chars
    ? (mat.chars >= 1000 ? (mat.chars / 1000).toFixed(1) + 'k 字' : mat.chars + ' 字')
    : 'docx';
  chip.classList.remove('hidden');
  chip.innerHTML = '<span class="oc-chip">📄 ' + esc(mat.name) + ' · ' + esc(size) +
    '</span><button id="oc-material-remove" class="oc-material-x" title="移除材料">×</button>';
  chip.querySelector('#oc-material-remove').addEventListener('click', () => {
    state.ocMaterial = null;
    ocRenderMaterialChip();
  });
}

function ocSetMaterialFile(file) {
  const ext = (file.name.split('.').pop() || '').toLowerCase();
  if (ext === 'docx') {
    if (file.size > OC_DOCX_MAX_BYTES) {
      toast('docx 太大（限 8MB）', 'err');
      return;
    }
    const reader = new FileReader();
    reader.onload = () => {
      const bytes = new Uint8Array(reader.result);
      let binary = '';
      const step = 0x8000;
      for (let i = 0; i < bytes.length; i += step) {
        binary += String.fromCharCode.apply(null, bytes.subarray(i, i + step));
      }
      state.ocMaterial = { name: file.name, docxB64: btoa(binary), chars: 0 };
      ocRenderMaterialChip();
      toast('已读取 docx：' + file.name, 'ok');
    };
    reader.onerror = () => toast('文件读取失败', 'err');
    reader.readAsArrayBuffer(file);
    return;
  }
  if (OC_TEXT_EXTS.includes(ext)) {
    const reader = new FileReader();
    reader.onload = () => {
      const text = String(reader.result || '').slice(0, OC_MATERIAL_MAX_CHARS);
      state.ocMaterial = { name: file.name, text, chars: text.length };
      ocRenderMaterialChip();
      toast('已读取材料：' + file.name + '（' + text.length + ' 字）', 'ok');
    };
    reader.onerror = () => toast('文件读取失败', 'err');
    reader.readAsText(file);
    return;
  }
  toast('暂支持 md / txt / docx；' + (ext || '该格式') + ' 请先转成文本', 'err');
}

async function renderOneclickView() {
  const pid = (state.route && state.route.project) || '';
  const compose = $('#oc-compose');
  const progress = $('#oc-progress');
  if (pid) {
    state.oneclick = { projectId: pid };
    compose.classList.add('hidden');
    progress.classList.remove('hidden');
    await ocRefresh(pid);
    if (state.route.view === 'oneclick') {
      state.stepTimer = setInterval(() => ocRefresh(pid), 3000);
    }
  } else {
    state.oneclick = null;
    progress.classList.add('hidden');
    compose.classList.remove('hidden');
    ocUpdateMode();
    ocRenderMaterialChip();
    ocUpdateHints();
    autoGrowChatInput($('#oc-input'));
    $('#oc-input').focus();
  }
}

async function ocRefresh(pid) {
  let data;
  try {
    data = await api('/tasks');
  } catch (err) {
    return;
  }
  if (!state.oneclick || state.oneclick.projectId !== pid) return;
  const proj = (data.projects || []).find((p) => p.project_id === pid);
  let assets = [];
  try {
    const res = await api('/assets?project_id=' + encodeURIComponent(pid));
    assets = res.assets || res || [];
  } catch (err) { /* 忽略素材查询失败 */ }
  ocDraw(pid, proj, assets);
}

function ocDraw(pid, proj, assets) {
  const el = $('#oc-progress');
  if (!el) return;
  if (!proj) {
    el.innerHTML = '<div class="panel db-empty">项目不存在或已删除。' +
      '<a href="#/oneclick" class="btn btn-small" style="margin-left:12px">再拍一部</a></div>';
    return;
  }
  const c = proj.counts || {};
  const stage = proj.stage;
  const flow = stepFlowState(stage, c);
  const running = !!flow.activeName || (c.active || 0) > 0;
  const shotsHtml = (proj.runs || []).slice(-24).map((r) => {
    const info = taskStateInfo(r.state);
    return '<span class="oc-shot ' + info[0] + '" title="' + esc(info[1]) + '">#' +
      esc(r.shot_no || '?') + '</span>';
  }).join('');
  const current = proj.current
    ? '<div class="oc-current">' + (flow.activeName ? '<span class="spinner"></span>' : '') +
      '<span class="oc-current-text">' + esc(proj.current.summary || proj.stage_label) +
      '</span><em>#' + esc(proj.current.shot_no || '-') + ' · 已耗时 ' +
      fmtElapsed((proj.current.elapsed_s || 0) * 1000) + '</em></div>'
    : (proj.last_event
      ? '<div class="oc-current"><span class="oc-current-text">' +
        esc(proj.last_event.summary || proj.last_event.type) + '</span></div>'
      : '');
  const exportVideo = [...assets].reverse().find(
    (a) => a.source === 'derived' && isVideoAsset(a));
  if (exportVideo && state.ocCelebrated !== pid) {
    state.ocCelebrated = pid;
    fireConfetti();
  }
  let resultHtml = '';
  if (proj.awaiting_confirm) {
    const imgs = assets.filter((a) => a.media_type === 'image').slice(-12);
    resultHtml = '<div class="panel oc-card oc-confirm">' +
      '<div class="oc-card-head"><div><div class="label">STEP 2 / 3</div>' +
      '<h2>素材已就绪 · 确认后开始渲染镜头</h2>' +
      '<div class="oc-meta">定妆 / 场景参考图 / 镜头首帧已生成；确认后逐镜渲染、质检，' +
      '并自动拼接成片，无需再次确认。</div></div>' +
      '<div class="oc-card-actions"><button id="oc-confirm-btn" ' +
        'class="btn btn-projector" data-id="' + esc(pid) + '">✅ 确认生成镜头</button>' +
      '</div></div>' +
      (imgs.length
        ? '<div class="oc-confirm-grid">' + imgs.map((a) =>
            '<a href="/assets/' + encodeURIComponent(a.asset_id) +
            '/file" target="_blank" rel="noopener" title="' +
            esc((a.metadata && a.metadata.original_filename) || '') + '">' +
            '<img src="/assets/' + encodeURIComponent(a.asset_id) +
            '/file" loading="lazy" alt=""></a>').join('') + '</div>'
        : '') +
    '</div>';
  } else if (exportVideo) {
    resultHtml = '<div class="panel oc-card oc-result">' +
      '<div class="oc-card-head"><div><div class="label">FINAL CUT</div>' +
      '<h2>完整成片已就绪</h2><div class="oc-meta">' +
      esc((exportVideo.metadata && exportVideo.metadata.original_filename) || '') +
      '</div></div><div class="oc-card-actions">' +
      '<a class="btn btn-small" href="/assets/' + encodeURIComponent(exportVideo.asset_id) +
        '/file" download>下载</a>' +
      '<button class="btn btn-projector btn-small oc-studio-btn" data-id="' +
        esc(pid) + '">打开工作室</button></div></div>' +
      '<video class="oc-video" src="/assets/' + encodeURIComponent(exportVideo.asset_id) +
        '/file" controls preload="metadata"></video></div>';
  } else if (stage === 'done') {
    resultHtml = '<div class="panel oc-card"><div class="oc-empty">' +
      '镜头已全部完成，成片正在拼接（或已降级为清单），稍后刷新看看。</div></div>';
  }
  el.innerHTML = '<div class="panel oc-card">' +
    '<div class="oc-card-head"><div>' +
      '<div class="label">' + esc(proj.stage_label || '进行中') + '</div>' +
      '<h2>' + esc(proj.title || '(无标题)') + '</h2>' +
      '<div class="oc-meta">' + esc([proj.style,
        proj.duration_target_s ? proj.duration_target_s + 's' : '',
        proj.aspect_ratio,
        proj.material_name ? '材料：' + proj.material_name : ''
      ].filter(Boolean).join(' / ')) + '</div></div>' +
      '<div class="oc-card-actions">' +
      (proj.stoppable ? '<button id="oc-stop-btn" class="btn btn-small btn-danger" ' +
        'data-id="' + esc(pid) + '" data-title="' + esc(proj.title || '') +
        '">■ 停止</button>' : '') +
      '<button class="btn btn-small oc-studio-btn" data-id="' + esc(pid) +
        '">打开工作室</button></div></div>' +
    renderStepFlow(stage, c) +
    '<div class="oc-bar-row"><div class="oc-bar"><div class="oc-bar-fill' +
      (running ? ' active' : '') + '" style="width:' + flow.pct + '%"></div></div>' +
      '<span class="oc-bar-pct">' + flow.pct + '%</span></div>' +
    '<div class="oc-card-stats">' + esc('镜头 ' + (c.accepted || 0) + '/' + (c.shots || 0) +
      ' · 进行 ' + (c.active || 0) + ' · 排队 ' + (c.queued || 0) +
      (c.review ? ' · 待审核 ' + c.review : '') +
      (c.failed ? ' · 失败 ' + c.failed : '')) + '</div>' +
    current +
    (shotsHtml ? '<div class="oc-shots">' + shotsHtml + '</div>' : '') +
  '</div>' + resultHtml;
  const stopBtn = $('#oc-stop-btn');
  if (stopBtn) stopBtn.addEventListener('click', () =>
    tasksStopProject(pid, stopBtn.dataset.title, stopBtn));
  const confirmBtn = $('#oc-confirm-btn');
  if (confirmBtn) confirmBtn.addEventListener('click', () =>
    ocConfirmProject(pid, confirmBtn));
  $$('.oc-studio-btn').forEach((btn) => btn.addEventListener('click', () => {
    go('#/studio/' + encodeURIComponent(btn.dataset.id) + '?step=storyboard');
  }));
}

async function ocConfirmProject(pid, btn) {
  btn.disabled = true;
  btn.textContent = '正在开始渲染…';
  try {
    await api('/projects/' + encodeURIComponent(pid) + '/confirm',
              { method: 'POST', json: {} });
    toast('已确认：开始逐镜渲染，完成后自动拼接', 'ok');
    await ocRefresh(pid);
  } catch (err) {
    toast('确认失败:' + err.message, 'err');
    btn.disabled = false;
    btn.textContent = '✅ 确认生成镜头';
  }
}

async function ocStart() {
  const input = $('#oc-input');
  const text = (input.value || '').trim();
  const material = state.ocMaterial;
  if (!text && !material) {
    toast('先描述一句要拍什么，或上传材料（md/txt/docx）', 'err');
    input.focus();
    return;
  }
  const btn = $('#oc-start-btn');
  btn.disabled = true;
  btn.textContent = '正在创建…';
  const body = { text, confirm_after_assets: state.ocMode === 'confirm' };
  if (material) {
    body.material_name = material.name;
    if (material.docxB64) body.material_docx_b64 = material.docxB64;
    else body.material_text = material.text || '';
  }
  try {
    const res = await api('/projects/oneclick', { method: 'POST', json: body });
    if (!res.project_id) throw new Error('响应中未找到 project_id');
    const modeNote = state.ocMode === 'confirm' ? '，素材完成后停下等你确认' : '';
    toast(material
      ? '已开始：材料逐段覆盖生成' + modeNote
      : '已开始：规划 → 定妆 → 首帧 → 渲染 → 自动拼接' + modeNote, 'ok');
    go('#/oneclick?project=' + encodeURIComponent(res.project_id));
  } catch (err) {
    toast('创建失败:' + err.message, 'err');
  } finally {
    btn.disabled = false;
    btn.textContent = '✦ 开始生成';
  }
}

$('#oc-start-btn').addEventListener('click', ocStart);
$('#oc-file-btn').addEventListener('click', () => $('#oc-file-input').click());
$('#oc-file-input').addEventListener('change', (e) => {
  const file = e.target.files && e.target.files[0];
  if (file) ocSetMaterialFile(file);
  e.target.value = '';
});
$$('#oc-modes .oc-mode').forEach((btn) => {
  btn.addEventListener('click', () => {
    state.ocMode = btn.dataset.mode || 'auto';
    ocUpdateMode();
  });
});
$('#oc-input').addEventListener('input', () => {
  autoGrowChatInput($('#oc-input'));
  ocUpdateHints();
});
$('#oc-input').addEventListener('keydown', (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') { e.preventDefault(); ocStart(); }
});
$$('.oc-suggest button').forEach((btn) => {
  btn.addEventListener('click', () => {
    const input = $('#oc-input');
    input.value = btn.dataset.suggest || '';
    autoGrowChatInput(input);
    ocUpdateHints();
    input.focus();
  });
});

/* ---------- 视图:资产 #/assets ---------- */
const ASSET_GROUPS = [
  ['material', '📄 材料', '文档 / 导入素材'],
  ['portrait', '🎭 角色定妆', '身份锁定参考图'],
  ['location', '🏞 场景参考', '空间与光线锁定'],
  ['first_frame', '🎬 镜头首帧', '关键物体初始状态'],
  ['clip', '🎞 镜头片段', '逐镜渲染 TAKE'],
  ['export', '📦 成片', '拼接输出'],
  ['other', '🗂 其他', '未归类作品'],
];
const ASSET_GROUP_NAME = { material: '材料', portrait: '定妆', location: '场景',
                           first_frame: '首帧', clip: '片段', export: '成片',
                           other: '其他' };

async function renderAssetsView() {
  await refreshAssetsGallery();
}

async function refreshAssetsGallery() {
  try {
    state.gallery = await api('/assets/gallery');
  } catch (err) {
    const list = $('#assets-list');
    if (list) {
      list.innerHTML = '<div class="panel db-empty">作品加载失败：' +
        esc(err.message) + '</div>';
    }
    return;
  }
  drawAssetsGallery();
}

function assetMetaLine(a) {
  const type = { image: '图片', video: '视频', text: '文本',
                 audio: '音频' }[a.media_type] || a.media_type;
  return [type, a.size ? fmtBytes(a.size) : '',
          (a.width && a.height) ? a.width + '×' + a.height : '']
    .filter(Boolean).join(' · ');
}

function renderAssetTile(a, group) {
  const url = '/assets/' + encodeURIComponent(a.asset_id) + '/file';
  const name = a.filename || a.asset_id;
  let badge = '';
  if (group === 'clip') {
    const shotLabel = a.shot_no ? 'SHOT ' + String(a.shot_no).padStart(2, '0')
      : '历史片段';
    const takeLabel = a.take ? ' · TAKE ' + a.take : '';
    badge = '<span class="asset-badge' + (a.accepted ? ' ok' : '') + '">' +
      shotLabel + takeLabel + (a.accepted ? ' ✓ 已采用' : '') + '</span>';
  } else if (group === 'first_frame') {
    badge = '<span class="asset-badge">SHOT ' +
      String(a.shot_no || '?').padStart(2, '0') + '</span>';
  } else if (a.label) {
    badge = '<span class="asset-badge">' + esc(a.label) + '</span>';
  }
  const isPortrait = a.media_type === 'image'
    && (!a.width || !a.height || a.height >= a.width);
  const thumb = a.media_type === 'image'
    ? '<img class="' + (isPortrait ? 'portrait' : '') + '" src="' + url +
      '" loading="lazy" alt="">'
    : a.media_type === 'text'
      ? '<div class="asset-thumb-text">📄</div>'
      : '<div class="asset-thumb-video">▶</div>';
  return '<div class="asset-tile" data-asset="' +
    esc(a.asset_id) + '" data-type="' + esc(a.media_type) + '" data-name="' +
    esc(name) + '" data-group="' + esc(group) + '">' +
    '<div class="asset-thumb">' + thumb + '</div>' +
    '<div class="asset-tile-body">' + badge +
    '<div class="asset-tile-name" title="' + esc(name) + '">' + esc(name) + '</div>' +
    '<div class="asset-tile-meta">' + esc(assetMetaLine(a)) + '</div>' +
    '</div></div>';
}

function drawAssetsGallery() {
  const view = state.assetsView;
  const data = state.gallery || { projects: [], orphans: [] };
  const sel = $('#assets-project');
  if (sel) {
    sel.innerHTML = ['<option value="all">全部项目（' +
      (data.projects || []).length + '）</option>']
      .concat((data.projects || []).map((p) => '<option value="' +
        esc(p.project_id) + '"' + (view.project === p.project_id ? ' selected' : '') +
        '>' + esc(p.title || p.project_id) + '</option>')).join('');
  }
  const projects = (data.projects || []).filter(
    (p) => view.project === 'all' || p.project_id === view.project);
  let html = '';
  for (const p of projects) {
    const sections = ASSET_GROUPS
      .filter(([g]) => view.group === 'all' || view.group === g)
      .map(([g, label, hint]) => {
        const items = (p.groups[g] || []).filter(
          (a) => view.type === 'all' || a.media_type === view.type);
        if (!items.length) return '';
        return '<div class="asset-group"><div class="asset-group-head"><b>' + label +
          '</b><span>' + items.length + ' · ' + esc(hint) + '</span></div>' +
          '<div class="asset-grid">' +
          items.map((a) => renderAssetTile(a, g)).join('') + '</div></div>';
      }).join('');
    if (!sections) continue;
    const counts = Object.entries(p.counts || {}).filter(([, n]) => n)
      .map(([g, n]) => (ASSET_GROUP_NAME[g] || g) + ' ' + n).join(' · ');
    html += '<article class="panel asset-project"><div class="asset-project-head">' +
      '<div><h2>' + esc(p.title || p.project_id) + '</h2>' +
      '<div class="asset-project-meta">' + esc(counts) +
      (p.material_name ? ' · 材料：' + esc(p.material_name) : '') + '</div></div>' +
      '<button class="btn btn-small asset-open-btn" data-id="' + esc(p.project_id) +
      '">打开工作室</button></div>' + sections + '</article>';
  }
  const orphans = (data.orphans || []).filter(
    (a) => view.type === 'all' || a.media_type === view.type);
  if (orphans.length && view.project === 'all' &&
      (view.group === 'all' || view.group === 'clip')) {
    html += '<article class="panel asset-project asset-orphans">' +
      '<details class="asset-orphan-details"><summary><b>已删除项目的作品（' +
      orphans.length + '）</b><span>项目记录已删除，文件仍保留在磁盘 · 点击展开</span>' +
      '</summary><div class="asset-grid">' +
      orphans.map((a) => renderAssetTile(a, a.group)).join('') +
      '</div></details></article>';
  }
  const list = $('#assets-list');
  if (list) {
    list.innerHTML = html ||
      '<div class="panel db-empty">这个筛选下还没有作品。</div>';
  }
  $$('.asset-tile').forEach((el) => el.addEventListener('click', () => {
    openAssetPreview(el.dataset.asset, el.dataset.name, el.dataset.type,
                     el.dataset.group === 'export' ? 'export' : '');
  }));
  $$('.asset-open-btn').forEach((btn) => btn.addEventListener('click', () => {
    go('#/studio/' + encodeURIComponent(btn.dataset.id) + '?step=storyboard');
  }));
}

$('#assets-refresh').addEventListener('click', refreshAssetsGallery);
$('#assets-project').addEventListener('change', (e) => {
  state.assetsView.project = e.target.value;
  drawAssetsGallery();
});
$$('#assets-type .preset-pill').forEach((btn) => btn.addEventListener('click', () => {
  state.assetsView.type = btn.dataset.type;
  $$('#assets-type .preset-pill').forEach(
    (b) => b.classList.toggle('active', b === btn));
  drawAssetsGallery();
}));
$$('#assets-group .preset-pill').forEach((btn) => btn.addEventListener('click', () => {
  state.assetsView.group = btn.dataset.group;
  $$('#assets-group .preset-pill').forEach(
    (b) => b.classList.toggle('active', b === btn));
  drawAssetsGallery();
}));

/* ---------- 视图三:工作室骨架 ---------- */
function getRuns() {
  return (state.bundle && state.bundle.runs) || [];
}

function getShots() {
  return (state.bundle && state.bundle.shots) || [];
}

function getScenes() {
  return (state.bundle && state.bundle.scenes) || [];
}

function shotSpec(shot) {
  return shot.spec || {};
}

function shotId(shot) {
  return shot.shot_id || shot.id;
}

function runId(run) {
  return run.run_id || run.id;
}

function runState(run) {
  return (run.state || run.status || 'PLANNED').toUpperCase();
}

// 镜头最新 run(按创建时间)
function latestRunForShot(sid) {
  const runs = getRuns().filter((r) => r.shot_id === sid);
  if (!runs.length) return null;
  return runs.slice().sort((a, b) => (parseTime(a.created_at) || 0) - (parseTime(b.created_at) || 0)).pop();
}

// 镜头状态徽章:无 run=待生成 / 生成中 / 待审核 / 已完成 / 失败
function shotStatusInfo(sid) {
  const run = latestRunForShot(sid);
  if (!run) return { cls: 'st-idle', label: '待生成' };
  const st = runState(run);
  if (st === 'ACCEPTED') return { cls: 'st-done', label: '已完成' };
  if (st === 'HUMAN_REVIEW') return { cls: 'st-review', label: '待审核' };
  if (st === 'FAILED' || st === 'CANCELLED') return { cls: 'st-fail', label: '失败' };
  if (RUN_STATES_BUSY.includes(st) || RUN_STATES_PRE.includes(st)) return { cls: 'st-busy', label: '生成中' };
  return { cls: 'st-idle', label: st };
}

function scoreAvg(run) {
  const s = run.score;
  if (!s) return '-';
  const sd = s.scores || (s.detail && s.detail.scores);
  if (sd && typeof sd === 'object') {
    const vals = Object.values(sd).map(Number).filter((n) => !isNaN(n));
    if (vals.length) return (vals.reduce((a, b) => a + b, 0) / vals.length).toFixed(2);
  }
  if (typeof s.total === 'number') return s.total.toFixed(2);
  if (typeof s.overall === 'number') return s.overall.toFixed(2);
  return '-';
}

function clearStepTimer() {
  if (state.stepTimer) { clearInterval(state.stepTimer); state.stepTimer = null; }
}

async function enterStudio(pid, step) {
  state.currentProjectId = pid;
  state.step = STEPS.some((s) => s.key === step) ? step : 'script';
  state.directorReport = null;
  state.sbDrafts = {};
  state.sbExpandRun = null;
  state.editSelectedShotId = null;
  state.producePending = false;   // SSE 回放会按事件重建真实状态
  const root = $('#studio-root');
  root.innerHTML = '<div class="studio-loading"><span class="spinner"></span> 正在开机片场…</div>';
  try {
    await refreshBundle();
  } catch (err) {
    root.innerHTML = '<div class="studio-loading">片场开机失败:' + esc(err.message) +
      ' · <a href="#/" style="color:var(--projector)">返回片库</a></div>';
    return;
  }
  connectSSE();
  renderStudioShell();
}

// 全量重拉 bundle;写操作成功后一律走这个,不做客户端合并
async function refreshBundle() {
  const pid = state.currentProjectId;
  const [bundle, chars, assets] = await Promise.all([
    api('/projects/' + encodeURIComponent(pid) + '?prompts=1'),
    api('/projects/' + encodeURIComponent(pid) + '/characters').catch(() => ({ characters: [] })),
    api('/assets?project_id=' + encodeURIComponent(pid)).catch(() => ({ assets: [] })),
  ]);
  state.bundle = bundle;
  state.characters = chars.characters || [];
  state.assets = assets.assets || assets || [];
}

// 静默刷新 + 重渲当前步
async function refreshQuiet() {
  try {
    await refreshBundle();
    renderStudioShell();
  } catch (err) {
    toast('刷新失败:' + err.message, 'err');
  }
}

function studioCounts() {
  const chars = state.characters;
  const shots = getShots();
  const accepted = shots.filter((s) => !!s.accepted_run_id).length;
  const exports = (state.assets || []).filter((a) => a.source === 'derived' && isVideoAsset(a)).length;
  return {
    script: getScenes().length + ' 场',
    character: String(chars.filter((c) => (c.kind || 'character') === 'character').length),
    scene: String(chars.filter((c) => c.kind === 'location').length),
    prop: String((state.assets || []).filter((a) => a.category === 'prop').length),
    storyboard: accepted + '/' + shots.length + ' 镜',
    edit: exports ? exports + ' 条成片' : '未成片',
  };
}

function renderStudioShell() {
  const root = $('#studio-root');
  const proj = (state.bundle && state.bundle.project) || {};
  const counts = studioCounts();
  const pid = state.currentProjectId;
  const ratio = proj.aspect_ratio || proj.ratio || '-';
  const stepIdx = STEPS.findIndex((s) => s.key === state.step);
  const activeRuns = getRuns().filter((r) =>
    !['ACCEPTED', 'CANCELLED', 'FAILED'].includes(runState(r))).length;
  const stoppable = activeRuns > 0 || state.producePending;

  root.innerHTML =
    '<div class="studio-grid">' +
    '<aside class="sidebar">' +
      '<div class="sidebar-perforation"></div>' +
      '<div class="sidebar-inner">' +
        '<div class="sidebar-head">' +
          '<div class="sidebar-brand">SCENERY · PRODUCTION STUDIO</div>' +
          '<a href="#/" class="sidebar-back">← 返回片库</a>' +
          '<h1 class="sidebar-title">' + esc(proj.title || '(无标题)') + '</h1>' +
          '<div class="sidebar-meta">LOCAL / ' + esc(ratio) + ' / ' + esc(proj.genre || proj.style || '-') + '</div>' +
        '</div>' +
        '<nav class="sidebar-nav">' +
          STEPS.map((s, i) =>
            '<button class="step-btn' + (s.key === state.step ? ' active' : '') + '" data-step="' + s.key + '">' +
              '<span class="step-num">' + String(i + 1).padStart(2, '0') + '</span>' +
              '<span class="step-text"><span class="step-label">' + s.label + '</span>' +
              '<span class="step-note">' + s.note + ' · ' + counts[s.key] + '</span></span>' +
            '</button>').join('') +
        '</nav>' +
        '<div class="sidebar-foot">' +
          '<button id="sse-chip" class="sse-chip"><span>事件流</span><span id="sse-dot" class="sse-dot"></span></button>' +
          '<div class="sidebar-slogan">本地运行 · 无登录 · 无积分</div>' +
        '</div>' +
      '</div>' +
    '</aside>' +
    '<div class="studio-main">' +
      '<header class="studio-topbar">' +
        '<div><div class="label studio-stage-label">STAGE ' + String(stepIdx + 1).padStart(2, '0') + '</div>' +
        '<div class="studio-stage-title">' + STEPS[stepIdx].label + '</div></div>' +
        '<div class="studio-topbar-actions">' +
          (stoppable ? '<button id="studio-stop-btn" class="btn btn-small btn-danger">■ 停止生成</button>' : '') +
          '<button id="studio-refresh-btn" class="btn btn-small">↻ 刷新</button>' +
        '</div>' +
      '</header>' +
      '<div id="step-content" class="step-content"></div>' +
    '</div>' +
    '</div>';

  root.querySelectorAll('.step-btn').forEach((btn) => {
    btn.addEventListener('click', () =>
      go('#/studio/' + encodeURIComponent(pid) + '?step=' + btn.dataset.step));
  });
  $('#studio-refresh-btn').addEventListener('click', refreshQuiet);
  const studioStopBtn = $('#studio-stop-btn');
  if (studioStopBtn) {
    studioStopBtn.addEventListener('click', () =>
      stopProject(pid, proj.title, studioStopBtn));
  }
  $('#sse-chip').addEventListener('click', () => $('#sse-drawer').classList.toggle('hidden'));
  updateSSEIndicator();
  renderStepContent();
}

function renderStepContent() {
  clearStepTimer();
  const el = $('#step-content');
  if (!el) return;
  if (state.step === 'script') renderScriptStep(el);
  else if (state.step === 'character') renderEntityStep(el, 'character');
  else if (state.step === 'scene') renderEntityStep(el, 'location');
  else if (state.step === 'prop') renderPropStep(el);
  else if (state.step === 'storyboard') renderStoryboardStep(el);
  else renderEditStep(el);
}

/* ---------- 第 1 步:剧本 ---------- */
function renderScriptStep(el) {
  const proj = (state.bundle && state.bundle.project) || {};
  const scenes = getScenes();
  const shots = getShots();
  const ratio = proj.aspect_ratio || proj.ratio || '-';

  let html = '<div class="panel brief-panel">' +
    '<div class="brief-head"><div>' +
      '<div class="brief-title">' + esc(proj.title || '(无标题)') + '</div>' +
      '<div class="brief-meta">' + esc([proj.genre, proj.style, ratio, (proj.duration_target_s ? proj.duration_target_s + 's' : '')].filter(Boolean).join(' / ')) + '</div>' +
    '</div>' +
    '<button id="replan-btn" class="btn btn-small">↻ 重新规划</button></div>' +
    '<label class="brief-editor-label" for="project-brief-editor">创作提示词 <span>修改后保存，重新规划时会使用最新内容</span></label>' +
    '<textarea id="project-brief-editor" class="brief-editor" rows="7" placeholder="写下故事、人物、情绪、风格或已有素材的使用方式…">' + esc(proj.brief || '') + '</textarea>' +
    '<div class="brief-editor-actions"><span>这不会自动覆盖现有分镜；需要重拆时再点“重新规划”。</span><button id="save-brief-btn" class="btn btn-projector btn-small">保存提示词</button></div>' +
  '</div>';

  if (!scenes.length) {
    html += '<div class="panel waiting-panel"><span class="spinner"></span>' +
      '<div>规划进行中…AI 正在拆解剧本与场景</div>' +
      '<div class="empty-hint" style="padding:0">每 3 秒自动刷新;若长时间无结果,可点上方「重新规划」</div></div>';
  } else {
    html += '<div class="scene-list">' + scenes.map((sc, i) => {
      const sid = sc.scene_id || sc.id || ('scene_' + (i + 1));
      const count = shots.filter((sh) => {
        const ssid = sh.scene_id || shotSpec(sh).scene_id;
        return ssid === sid;
      }).length;
      return '<div class="scene-row">' +
        '<span class="scene-idx">' + String(i + 1).padStart(2, '0') + '</span>' +
        '<span class="scene-title">' + esc(sc.title || sc.name || ('场景 ' + String(i + 1).padStart(2, '0'))) + '</span>' +
        '<span class="scene-summary">' + esc(sc.summary || sc.description || '') + '</span>' +
        '<span class="scene-count">' + count + ' 镜</span>' +
      '</div>';
    }).join('') + '</div>';
  }
  el.innerHTML = html;

  $('#save-brief-btn').addEventListener('click', async () => {
    const btn = $('#save-brief-btn');
    const brief = $('#project-brief-editor').value;
    btn.disabled = true;
    btn.textContent = '保存中…';
    try {
      const updated = await api('/projects/' + encodeURIComponent(state.currentProjectId), {
        method: 'PATCH', json: { brief },
      });
      if (state.bundle && state.bundle.project) state.bundle.project.brief = updated.brief;
      toast('创作提示词已保存', 'ok');
    } catch (err) {
      toast('保存提示词失败:' + err.message, 'err');
    } finally {
      btn.disabled = false;
      btn.textContent = '保存提示词';
    }
  });

  $('#replan-btn').addEventListener('click', async () => {
    if (!confirm('重新规划?\n现有剧本场景、角色与分镜可能被替换;\n本地素材与成片文件仍保留。')) return;
    try {
      await api('/projects/' + encodeURIComponent(state.currentProjectId) + '/plan?force=true', { method: 'POST', json: {} });
      toast('已触发重新规划', 'ok');
      setTimeout(refreshQuiet, 800);
    } catch (err) {
      toast('重新规划失败:' + err.message, 'err');
    }
  });

  // 没有 scenes 时每 3 秒轮询
  if (!scenes.length) {
    state.stepTimer = setInterval(refreshQuiet, 3000);
  }
}

/* ---------- 第 2/3 步:角色 / 场景(实体卡片) ---------- */
function renderEntityStep(el, kind) {
  const isChar = kind === 'character';
  const title = isChar ? '角色造型' : '空镜场景';
  const emptyHint = isChar
    ? '剧本规划后会自动整理角色;若为空,请到「剧本」步触发(重新)规划。'
    : '剧本规划后会自动整理场景;若为空,请到「剧本」步触发(重新)规划。';
  const list = state.characters.filter((c) => (c.kind || 'character') === kind);
  const imageAssets = (state.assets || []).filter((a) => !isVideoAsset(a) && !isAudioAsset(a));

  let html = '<div class="prop-toolbar"><span class="empty-hint" style="padding:0">' +
    (isChar ? '角色定妆照与参考图会注入分镜提示词,保证形象一致。' : '场景参考图会注入分镜提示词,保证空间光线一致。') +
    (state.noCharPatch ? ' · 后端暂不支持在线修改(描述为只读)' : '') +
    '</span></div>';

  if (!list.length) {
    html += '<div class="panel db-empty">' + esc(emptyHint) + '</div>';
    el.innerHTML = html;
    return;
  }

  html += '<div class="entity-grid">' + list.map((c) => {
    const cid = c.character_id || c.id;
    const working = state.portraitWorking.has(cid);
    const img = c.asset_id
      ? '<img src="/assets/' + encodeURIComponent(c.asset_id) + '/file" loading="lazy" alt="' + esc(c.name) + '">'
      : '<div class="entity-img-placeholder"><span class="ph-icon">' + (isChar ? '🎭' : '🏞') + '</span><span>暂无' + (isChar ? '定妆照' : '场景图') + '</span></div>';
    const refOptions = ['<option value="">参考图:未关联</option>'].concat(
      imageAssets.map((a) => {
        const aid = a.asset_id || a.id;
        return '<option value="' + esc(aid) + '"' + (c.asset_id === aid ? ' selected' : '') + '>' + esc(assetName(a)) + '</option>';
      })).join('');
    return '<div class="panel entity-card" data-id="' + esc(cid) + '">' +
      '<div class="entity-img-wrap">' + img +
        (working ? '<div class="entity-working"><span class="spinner"></span>生成中…</div>' : '') +
      '</div>' +
      '<div class="entity-body">' +
        '<div class="entity-name">' + esc(c.name || cid) + '</div>' +
        '<textarea class="entity-desc" rows="3" data-id="' + esc(cid) + '"' + (state.noCharPatch ? ' readonly' : '') +
          ' placeholder="外观 / 氛围描述">' + esc(c.description || '') + '</textarea>' +
        '<div class="entity-ref-row"><select class="field entity-ref-select" data-id="' + esc(cid) + '">' + refOptions + '</select></div>' +
        '<div class="entity-actions">' +
          '<button class="btn btn-projector btn-small portrait-btn" data-id="' + esc(cid) + '"' + (working ? ' disabled' : '') + '>' +
            (working ? '生成中…' : (c.asset_id ? '重新生成' : '生成') + (isChar ? '定妆照' : '场景图')) + '</button>' +
        '</div>' +
      '</div>' +
    '</div>';
  }).join('') + '</div>';
  el.innerHTML = html;

  el.querySelectorAll('.entity-desc').forEach((ta) => {
    ta.addEventListener('blur', () => saveCharDesc(ta.dataset.id, ta));
  });
  el.querySelectorAll('.entity-ref-select').forEach((sel) => {
    sel.addEventListener('change', () => setCharAsset(sel.dataset.id, sel.value, sel));
  });
  el.querySelectorAll('.portrait-btn').forEach((btn) => {
    btn.addEventListener('click', () => generatePortrait(btn.dataset.id));
  });
}

// 描述就地编辑:onBlur 保存;后端无 PATCH /characters 时转只读并提示
async function saveCharDesc(cid, ta) {
  const c = state.characters.find((x) => (x.character_id || x.id) === cid);
  const old = c ? (c.description || '') : '';
  if (ta.value === old) return;
  try {
    await api('/characters/' + encodeURIComponent(cid), { method: 'PATCH', json: { description: ta.value } });
    toast('描述已保存', 'ok');
    refreshQuiet();
  } catch (err) {
    state.noCharPatch = true;
    ta.value = old;
    toast('保存失败(后端可能未支持在线修改):' + err.message, 'err');
  }
}

// TODO: 后端暂无"单角色设置 asset_id"的专用接口,这里复用 PATCH /characters/{id};
// 若后端未实现该字段,仅提示失败,卡片上的下拉保持展示用途
async function setCharAsset(cid, assetId, sel) {
  if (!assetId) return;
  try {
    await api('/characters/' + encodeURIComponent(cid), { method: 'PATCH', json: { asset_id: assetId } });
    toast('参考图已关联', 'ok');
    refreshQuiet();
  } catch (err) {
    sel.value = '';
    toast('关联失败(后端可能未支持):' + err.message + ' — 可在「道具」步把素材分类为参考图', 'err');
  }
}

// 生成定妆照:单角色也走批量接口,轮询角色表直到 asset_id 出现
async function generatePortrait(cid) {
  const pid = state.currentProjectId;
  try {
    await api('/projects/' + encodeURIComponent(pid) + '/characters/portraits', {
      method: 'POST', json: { character_ids: [cid] },
    });
  } catch (err) {
    toast('生成请求失败:' + err.message, 'err');
    return;
  }
  state.portraitWorking.add(cid);
  renderStepContent();
  const started = Date.now();
  const timer = setInterval(async () => {
    if (state.currentProjectId !== pid || Date.now() - started > 120000) {
      clearInterval(timer);
      state.portraitTimers.delete(timer);
      state.portraitWorking.delete(cid);
      if (Date.now() - started > 120000) toast('生成超时,请稍后刷新查看', 'err');
      renderStepContent();
      return;
    }
    try {
      const res = await api('/projects/' + encodeURIComponent(pid) + '/characters');
      const fresh = (res.characters || []).find((x) => (x.character_id || x.id) === cid);
      if (fresh && fresh.asset_id) {
        clearInterval(timer);
        state.portraitTimers.delete(timer);
        state.portraitWorking.delete(cid);
        toast('图片已生成', 'ok');
        await refreshQuiet();
      }
    } catch (e) { /* 下次轮询再试 */ }
  }, 3000);
  state.portraitTimers.add(timer);
}

/* ---------- 第 4 步:道具(素材库 + 分类 + 上传) ---------- */
const PROP_PILLS = [
  { key: '__all', label: '全部', params: {} },
  { key: 'character', label: '角色参考', params: { category: 'character' } },
  { key: 'location', label: '场景参考', params: { category: 'location' } },
  { key: 'prop', label: '道具', params: { category: 'prop' } },
  { key: 'video', label: '视频', params: { media_type: 'video' } },
  { key: 'audio', label: '音频', params: { media_type: 'audio' } },
  { key: 'export', label: '成片', params: { category: 'export' } },
];

function renderPropStep(el) {
  el.innerHTML =
    '<div class="prop-toolbar">' +
      '<button id="prop-classify-btn" class="btn btn-projector btn-small">自动分类</button>' +
      '<button id="prop-upload-btn" class="btn btn-small">上传素材</button>' +
      '<input type="file" id="prop-file-input" multiple accept="image/*,video/*,audio/*" class="hidden">' +
      PROP_PILLS.map((p) =>
        '<button class="preset-pill prop-pill' + (state.propFilter === p.key ? ' active' : '') + '" data-key="' + p.key + '">' + p.label + '</button>'
      ).join('') +
    '</div>' +
    '<div id="prop-upload-status" class="empty-hint" style="padding:0 0 8px"></div>' +
    '<div id="prop-grid" class="asset-grid"><p class="empty-hint">加载中…</p></div>';

  el.querySelectorAll('.prop-pill').forEach((btn) => {
    btn.addEventListener('click', () => {
      state.propFilter = btn.dataset.key;
      el.querySelectorAll('.prop-pill').forEach((b) => b.classList.toggle('active', b === btn));
      loadPropAssets();
    });
  });
  $('#prop-classify-btn').addEventListener('click', classifyPropAssets);
  $('#prop-upload-btn').addEventListener('click', () => $('#prop-file-input').click());
  $('#prop-file-input').addEventListener('change', (e) => {
    if (e.target.files.length) uploadPropFiles(Array.from(e.target.files));
    e.target.value = '';
  });
  loadPropAssets();
}

async function loadPropAssets() {
  const grid = $('#prop-grid');
  if (!grid) return;
  const pill = PROP_PILLS.find((p) => p.key === state.propFilter) || PROP_PILLS[0];
  const params = ['project_id=' + encodeURIComponent(state.currentProjectId)];
  Object.entries(pill.params).forEach(([k, v]) => params.push(k + '=' + encodeURIComponent(v)));
  try {
    const res = await api('/assets?' + params.join('&'));
    renderPropGrid(res.assets || res || []);
  } catch (err) {
    grid.innerHTML = '<p class="empty-hint">加载素材失败:' + esc(err.message) + '</p>';
  }
}

function renderPropGrid(list) {
  const grid = $('#prop-grid');
  if (!grid) return;
  if (!list.length) {
    grid.innerHTML = '<p class="empty-hint">暂无素材 — 上传道具/参考素材,或点「自动分类」让 AI 整理现有素材。</p>';
    return;
  }
  grid.innerHTML = list.map((a) => {
    const id = a.asset_id || a.id;
    const name = assetName(a);
    const url = assetFileUrl(a);
    let media;
    if (isAudioAsset(a)) media = '<div class="asset-audio-thumb">♪</div>';
    else if (isVideoAsset(a)) media = '<video src="' + esc(url) + '" preload="metadata" muted></video>';
    else media = '<img src="' + esc(url) + '" loading="lazy" alt="' + esc(name) + '">';
    const catOptions = ['<option value="">未分类</option>'].concat(
      CATEGORIES.map((c) => '<option value="' + c + '"' + (a.category === c ? ' selected' : '') + '>' + CATEGORY_MAP[c] + '</option>')
    ).join('');
    const mt = a.media_type || (isVideoAsset(a) ? 'video' : isAudioAsset(a) ? 'audio' : 'image');
    return '<div class="asset-card">' + media +
      '<div class="asset-info"><div class="asset-name" title="' + esc(name) + '">' + esc(name) + '</div>' +
      '<div class="asset-sub">' + esc(mt) + ' · ' + esc(id) + '</div>' +
      '<div class="asset-cat-row"><select class="field prop-cat-select" data-id="' + esc(id) + '">' + catOptions + '</select></div>' +
      '</div></div>';
  }).join('');
  grid.querySelectorAll('.prop-cat-select').forEach((sel) => {
    sel.addEventListener('change', async () => {
      try {
        await api('/assets/' + encodeURIComponent(sel.dataset.id) + '/category', {
          method: 'POST', json: { category: sel.value },
        });
        toast('分类已更新:' + categoryLabel(sel.value), 'ok');
        refreshQuiet();
      } catch (err) {
        toast('设置分类失败:' + err.message, 'err');
      }
    });
  });
}

async function classifyPropAssets() {
  const btn = $('#prop-classify-btn');
  btn.disabled = true;
  btn.textContent = '分类中…';
  try {
    const res = await api('/projects/' + encodeURIComponent(state.currentProjectId) + '/assets/classify', {
      method: 'POST', json: {},
    });
    const n = res.classified ? Object.keys(res.classified).length : 0;
    toast('自动分类完成:' + n + ' 个素材', 'ok');
    await refreshQuiet();
  } catch (err) {
    toast('自动分类失败:' + err.message, 'err');
  } finally {
    btn.disabled = false;
    btn.textContent = '自动分类';
  }
}

/* ---- 分块上传(沿用既有流程) ---- */
async function uploadPropFiles(files) {
  const status = $('#prop-upload-status');
  for (const file of files) {
    try {
      if (status) status.textContent = '上传中:' + file.name + ' (' + fmtBytes(file.size) + ')…';
      await uploadOneFile(file);
      toast('上传完成:' + file.name, 'ok');
    } catch (err) {
      toast('上传失败(' + file.name + '):' + err.message, 'err');
    }
  }
  if (status) status.textContent = '';
  refreshQuiet();
}

async function uploadOneFile(file) {
  const initRes = await api('/assets/uploads', {
    method: 'POST',
    json: { filename: file.name, total_size: file.size, project_id: state.currentProjectId },
  });
  const uploadId = initRes.upload_id;
  const chunkSize = initRes.chunk_size || (4 * 1024 * 1024);
  if (!uploadId) throw new Error('响应缺少 upload_id');
  const totalParts = Math.max(1, Math.ceil(file.size / chunkSize));
  const status = $('#prop-upload-status');
  for (let n = 1; n <= totalParts; n++) {
    const start = (n - 1) * chunkSize;
    const blob = file.slice(start, Math.min(start + chunkSize, file.size));
    const res = await fetch('/assets/uploads/' + encodeURIComponent(uploadId) + '/parts/' + n, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/octet-stream' },
      body: blob,
    });
    if (!res.ok) throw new Error('分块 ' + n + ' 上传失败:' + res.status);
    if (status) status.textContent = '上传中:' + file.name + ' · 分块 ' + n + '/' + totalParts;
  }
  await api('/assets/uploads/' + encodeURIComponent(uploadId) + '/complete', {
    method: 'POST',
    json: { project_id: state.currentProjectId },
  });
}

/* ---------- 第 5 步:分镜 ---------- */
function shotOrder(shot, idx) {
  const spec = shotSpec(shot);
  const o = shot.shot_order != null ? shot.shot_order : (shot.order != null ? shot.order : spec.order);
  return o != null ? Number(o) : idx + 1;
}

function renderStoryboardStep(el) {
  const scenes = getScenes();
  const shots = getShots();
  if (!shots.length) {
    el.innerHTML = renderManualShotComposer(scenes) +
      '<div class="panel sb-empty"><div><b>还没有分镜</b><p>可以等待 AI 规划，也可以现在手动补充第一个镜头。没有场景时会自动新建“场景 01”。</p></div><button class="btn btn-projector btn-small sb-add-shot-btn">＋ 新增分镜</button></div>';
    bindManualShotComposer(el);
    return;
  }

  // 按场景分组
  const groups = [];
  const byId = {};
  scenes.forEach((sc) => {
    const id = sc.scene_id || sc.id;
    const g = { id, title: sc.title || sc.name || ('场景 ' + id), shots: [] };
    byId[id] = g;
    groups.push(g);
  });
  const ungrouped = { id: '__none__', title: '未分组镜头', shots: [] };
  shots.forEach((shot) => {
    const sid = shot.scene_id || shotSpec(shot).scene_id;
    if (sid && byId[sid]) byId[sid].shots.push(shot);
    else ungrouped.shots.push(shot);
  });
  if (ungrouped.shots.length) groups.push(ungrouped);

  const pendingProduce = shots.filter((s) =>
    !getRuns().some((r) => r.shot_id === shotId(s)));
  const fromChat = !!(state.route && state.route.from === 'chat');

  el.innerHTML = renderManualShotComposer(scenes) +
    renderProduceBar(pendingProduce, shots, fromChat) +
    groups.map((g) => {
      const sorted = g.shots.slice().sort(shotOrder);
      return '<div class="sb-scene-block">' +
        '<div class="sb-scene-head"><h3>' + esc(g.title) + '</h3><span>' + sorted.length + ' 个镜头</span></div>' +
        sorted.map((shot, i) => renderShotCard(shot, i)).join('') +
      '</div>';
    }).join('');

  bindShotCardEvents(el);
  bindManualShotComposer(el);
  const produceBtn = el.querySelector('#sb-produce-btn');
  if (produceBtn) produceBtn.addEventListener('click', () => startProjectProduce(produceBtn));
  const sbStopBtn = el.querySelector('#sb-stop-btn');
  if (sbStopBtn) {
    const proj = (state.bundle && state.bundle.project) || {};
    sbStopBtn.addEventListener('click', () =>
      stopProject(state.currentProjectId, proj.title, sbStopBtn));
  }

  // 有 run 生成中 / 生产准备中(定妆/首帧)时每 5 秒轮询
  const busy = getRuns().some((r) => RUN_STATES_BUSY.includes(runState(r)));
  if (busy || state.producePending) state.stepTimer = setInterval(refreshQuiet, 5000);
}

/* 分镜就绪条:确认后手动点「开始生成」,不自动开工;验收完自动拼成片 */
function renderProduceBar(pending, shots, fromChat) {
  const label = fromChat ? '对话方案已填入分镜:' : '分镜已就绪:';
  const hasExport = (state.assets || []).some(
    (a) => a.source === 'derived' && isVideoAsset(a));
  const status = pending.length
    ? '其中 ' + pending.length + ' 个待生成'
    : (hasExport ? '完整成片已就绪,可在「剪辑」步查看/下载' : '已全部提交生成');
  const activeRuns = getRuns().filter((r) =>
    !['ACCEPTED', 'CANCELLED', 'FAILED'].includes(runState(r))).length;
  const stoppable = state.producePending || activeRuns > 0;
  const preparing = state.producePending
    ? ' · <span class="spinner"></span> 正在准备(定妆参考图 → 镜头首帧 → 逐镜排队)…'
    : '';
  return '<div class="panel sb-produce-bar">' +
    '<div class="sb-produce-info"><b>' + label + '</b>' +
      shots.length + ' 个镜头,' + status + preparing +
      '<span class="empty-hint">过目或修改后点右侧「开始生成」,才开始 定妆 → 首帧 → 逐镜渲染;全部镜头验收后会自动拼接成完整成片。</span></div>' +
    '<div class="sb-produce-actions">' +
      (stoppable ? '<button id="sb-stop-btn" class="btn btn-danger">■ 停止</button>' : '') +
      '<button id="sb-produce-btn" class="btn btn-projector"' +
        (pending.length ? '' : ' disabled') + '>▶ 开始生成全部镜头</button>' +
    '</div>' +
  '</div>';
}

async function startProjectProduce(btn) {
  if (Object.keys(state.sbDrafts || {}).length) {
    toast('有未保存的镜头修改:先点镜头卡片里的「保存」再开始生成', 'err');
    return;
  }
  btn.disabled = true;
  btn.textContent = '正在排队…';
  state.producePending = true;
  try {
    await api('/projects/' + encodeURIComponent(state.currentProjectId) + '/produce', {
      method: 'POST', json: {},
    });
    toast('已开始:定妆 → 首帧 → 逐镜生成', 'ok');
    await refreshQuiet();
  } catch (err) {
    state.producePending = false;
    toast('开始生成失败:' + err.message, 'err');
    btn.disabled = false;
    btn.textContent = '▶ 开始生成全部镜头';
  }
}

function renderManualShotComposer(scenes) {
  const options = (scenes || []).map((sc, i) => {
    const id = sc.scene_id || sc.id;
    return '<option value="' + esc(id) + '">' + esc(sc.title || ('场景 ' + String(i + 1).padStart(2, '0'))) + '</option>';
  }).join('');
  return '<div class="sb-toolbar"><div><div class="label">STORYBOARD</div><span>镜头画面与台词均可随时修改；保存后再提交生成。</span></div><button class="btn btn-projector btn-small sb-add-shot-btn">＋ 新增分镜</button></div>' +
    '<div id="manual-shot-composer" class="panel manual-shot-composer hidden">' +
      '<div class="manual-shot-head"><div><h3>补充一个镜头</h3><p>先写画面，再按需要补上角色台词。</p></div><button class="btn btn-ghost btn-small sb-cancel-shot-btn">取消</button></div>' +
      '<div class="manual-shot-fields"><label>所属场景<select id="manual-shot-scene" class="field"><option value="">' + (options ? '自动选择第一个场景' : '自动创建场景 01') + '</option>' + options + '</select></label>' +
      '<label>时长（秒）<input id="manual-shot-duration" class="field" type="number" min="1" max="120" value="5"></label></div>' +
      '<label>镜头画面<textarea id="manual-shot-action" class="shot-action" rows="3" placeholder="例如：清晨的厨房里，女孩切开番茄，阳光落在蓝色餐盘上。"></textarea></label>' +
      '<label>人物台词（可选）<textarea id="manual-shot-dialogue" class="shot-dialogue" rows="2" placeholder="例如：今天要做一顿特别的早餐。"></textarea></label>' +
      '<div class="manual-shot-submit"><span>新建后仍可在卡片中继续编辑。</span><button class="btn btn-projector btn-small sb-create-shot-btn">创建分镜</button></div>' +
    '</div>';
}

function bindManualShotComposer(el) {
  const composer = el.querySelector('#manual-shot-composer');
  const show = () => {
    composer.classList.remove('hidden');
    const action = composer.querySelector('#manual-shot-action');
    if (action) action.focus();
  };
  el.querySelectorAll('.sb-add-shot-btn').forEach((btn) => btn.addEventListener('click', show));
  const cancel = el.querySelector('.sb-cancel-shot-btn');
  if (cancel) cancel.addEventListener('click', () => composer.classList.add('hidden'));
  const create = el.querySelector('.sb-create-shot-btn');
  if (create) create.addEventListener('click', () => createManualShot(el, create));
}

async function createManualShot(el, btn) {
  const action = el.querySelector('#manual-shot-action').value.trim();
  const duration = Number(el.querySelector('#manual-shot-duration').value || 0);
  if (!action) { toast('先补充镜头画面描述', 'err'); return; }
  if (!duration || duration < 1) { toast('镜头时长至少为 1 秒', 'err'); return; }
  btn.disabled = true;
  btn.textContent = '创建中…';
  try {
    await api('/projects/' + encodeURIComponent(state.currentProjectId) + '/shots', {
      method: 'POST',
      json: {
        scene_id: el.querySelector('#manual-shot-scene').value || null,
        action,
        dialogue: el.querySelector('#manual-shot-dialogue').value,
        duration_s: duration,
      },
    });
    toast('分镜已创建，可继续编辑或直接生成', 'ok');
    state.sbDrafts = {};
    await refreshQuiet();
  } catch (err) {
    toast('创建分镜失败:' + err.message, 'err');
    btn.disabled = false;
    btn.textContent = '创建分镜';
  }
}

function renderShotCard(shot, idx) {
  const spec = shotSpec(shot);
  const sid = shotId(shot);
  const info = shotStatusInfo(sid);
  const draft = state.sbDrafts[sid] || {};
  const runs = getRuns().filter((r) => r.shot_id === sid)
    .sort((a, b) => (parseTime(a.created_at) || 0) - (parseTime(b.created_at) || 0));
  const needsReview = runs.some((r) => runState(r) === 'HUMAN_REVIEW');

  const refAssets = spec.reference_assets || shot.reference_assets || [];
  const characters = spec.characters || [];

  let html = '<div class="shot-card' + (needsReview ? ' needs-review' : '') + '" data-shot-id="' + esc(sid) + '">';
  if (needsReview) html += '<div class="review-banner">⚠ 有待审核的生成结果,请在下方 TAKE 中接受或拒绝</div>';
  html += '<div class="shot-head">' +
    '<span class="shot-badge">SHOT ' + String(shotOrder(shot, idx)).padStart(2, '0') + '</span>' +
    '<span class="status-badge ' + info.cls + '">' + info.label + '</span>' +
    '<span class="shot-dur-wrap">时长 <input type="number" class="shot-dur-input" data-shot-id="' + esc(sid) + '" min="1" value="' +
      esc(draft.duration_s != null ? draft.duration_s : (spec.duration_s || '')) + '"> s</span>' +
  '</div>';
  html += '<textarea class="shot-action" data-shot-id="' + esc(sid) + '" rows="3" placeholder="镜头动作 / 画面描述">' +
    esc(draft.action != null ? draft.action : (spec.action || '')) + '</textarea>';
  html += '<textarea class="shot-dialogue" data-shot-id="' + esc(sid) + '" rows="2" placeholder="人物台词（可选）">' +
    esc(draft.dialogue != null ? draft.dialogue : (spec.dialogue || '')) + '</textarea>';

  if (refAssets.length || characters.length) {
    html += '<div class="chip-row">';
    characters.forEach((c) => {
      const label = typeof c === 'object' ? (c.name || c.character_id || JSON.stringify(c)) : c;
      html += '<span class="chip char">' + esc(label) + '</span>';
    });
    refAssets.forEach((aid0) => {
      const aid = typeof aid0 === 'object' ? (aid0.asset_id || aid0.id) : aid0;
      html += '<span class="ref-chip"><img src="/assets/' + encodeURIComponent(aid) + '/file" loading="lazy" alt="ref">' +
        '<button class="ref-x" data-shot-id="' + esc(sid) + '" data-asset-id="' + esc(aid) + '" title="移除参考图">×</button></span>';
    });
    html += '</div>';
  }

  html += '<div class="shot-actions-row">' +
    '<button class="btn btn-projector btn-small sb-gen-btn" data-shot-id="' + esc(sid) + '">生成</button>' +
    '<button class="btn btn-small sb-save-btn hidden" data-shot-id="' + esc(sid) + '">保存</button>' +
    '<span class="shot-dirty-hint sb-dirty-hint hidden" data-shot-id="' + esc(sid) + '">● 未保存</span>' +
  '</div>';

  // 渲染提示词:已生成优先显示实际下发文本(run.prompt_spec),否则显示预览
  const latest = runs.length ? runs[runs.length - 1] : null;
  const promptText = (latest && latest.prompt_spec) || shot.prompt_preview || '';
  if (promptText) {
    html += '<details class="shot-prompt"><summary>渲染提示词' +
      (latest && latest.prompt_spec ? '(实际下发)' : '(预览,点「保存」后生效)') +
      '</summary><pre class="shot-prompt-text" data-shot-id="' + esc(sid) + '">' +
      esc(promptText) + '</pre>' +
      '<div class="shot-prompt-actions">' +
        '<button class="btn btn-ghost btn-small shot-prompt-copy" data-shot-id="' + esc(sid) + '">复制提示词</button>' +
      '</div></details>';
  }

  // TAKE 版本条
  if (runs.length) {
    html += '<div class="take-strip"><div class="take-strip-label">TAKES · ' + runs.length + '</div>';
    runs.forEach((run, i) => {
      const rid = runId(run);
      const st = runState(run);
      const stInfo = (() => {
        if (st === 'ACCEPTED') return { cls: 'st-done', label: 'ACCEPTED' };
        if (st === 'HUMAN_REVIEW') return { cls: 'st-review', label: 'HUMAN_REVIEW' };
        if (st === 'FAILED' || st === 'CANCELLED') return { cls: 'st-fail', label: st };
        if (RUN_STATES_BUSY.includes(st)) return { cls: 'st-busy', label: st };
        return { cls: 'st-idle', label: st };
      })();
      const expanded = state.sbExpandRun === rid;
      const candidates = run.candidate_asset_ids || run.candidates || [];
      const firstCid = candidates.length ? (typeof candidates[0] === 'object' ? (candidates[0].asset_id || candidates[0].id) : candidates[0]) : null;
      html += '<div class="take-row' + (firstCid ? ' expandable' : '') + (expanded ? ' expanded' : '') + '" data-run-id="' + esc(rid) + '">' +
        '<span class="take-num">TAKE ' + String(i + 1).padStart(2, '0') + '</span>' +
        '<span class="status-badge ' + stInfo.cls + '">' + stInfo.label + '</span>' +
        '<span class="take-score">评分 ' + scoreAvg(run) + '</span>' +
        (RUN_STATES_BUSY.includes(st) ? '<span class="take-elapsed" data-elapsed-run="' + esc(rid) + '">计时…</span>' : '') +
        (firstCid ? '<span class="chip">' + (expanded ? '收起' : '播放') + '</span>' : '') +
        '<span style="flex:1"></span>' +
        (st === 'HUMAN_REVIEW'
          ? '<span class="take-review-hint">需人工确认</span>' +
            '<button class="btn btn-small take-review-btn" data-decision="accept" data-run-id="' + esc(rid) + '">接受</button>' +
            '<button class="btn btn-small btn-danger take-review-btn" data-decision="reject" data-run-id="' + esc(rid) + '">拒绝</button>'
          : '') +
      '</div>';
      if (expanded && firstCid) {
        html += '<div class="take-video"><video src="/assets/' + encodeURIComponent(firstCid) + '/file" controls autoplay preload="metadata"></video></div>';
      }
    });
    html += '</div>';
  }
  html += '</div>';
  return html;
}

function bindShotCardEvents(el) {
  // 编辑草稿
  el.querySelectorAll('.shot-action').forEach((ta) => {
    ta.addEventListener('input', () => markShotDirty(ta.dataset.shotId, el));
  });
  el.querySelectorAll('.shot-dialogue').forEach((ta) => {
    ta.addEventListener('input', () => markShotDirty(ta.dataset.shotId, el));
  });
  el.querySelectorAll('.shot-dur-input').forEach((inp) => {
    inp.addEventListener('input', () => markShotDirty(inp.dataset.shotId, el));
  });
  // 复制渲染提示词
  el.querySelectorAll('.shot-prompt-copy').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const pre = el.querySelector('.shot-prompt-text[data-shot-id="' +
        CSS.escape(btn.dataset.shotId) + '"]');
      if (!pre) return;
      try {
        await navigator.clipboard.writeText(pre.textContent);
        toast('提示词已复制', 'ok');
      } catch (err) {
        toast('复制失败,可手动选择文本复制', 'err');
      }
    });
  });
  // 保存
  el.querySelectorAll('.sb-save-btn').forEach((btn) => {
    btn.addEventListener('click', () => saveShot(btn.dataset.shotId, btn));
  });
  // 生成
  el.querySelectorAll('.sb-gen-btn').forEach((btn) => {
    btn.addEventListener('click', () => submitShotRun(btn.dataset.shotId, btn));
  });
  // 参考图移除
  el.querySelectorAll('.ref-x').forEach((btn) => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      removeShotReference(btn.dataset.shotId, btn.dataset.assetId);
    });
  });
  // TAKE 展开播放
  el.querySelectorAll('.take-row.expandable').forEach((row) => {
    row.addEventListener('click', (e) => {
      if (e.target.closest('.take-review-btn')) return;
      const rid = row.dataset.runId;
      state.sbExpandRun = state.sbExpandRun === rid ? null : rid;
      renderStepContent();
    });
  });
  // TAKE 审核
  el.querySelectorAll('.take-review-btn').forEach((btn) => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      submitRunReview(btn.dataset.runId, btn.dataset.decision);
    });
  });
}

function markShotDirty(sid, el) {
  const card = el.querySelector('.shot-card[data-shot-id="' + CSS.escape(sid) + '"]');
  if (!card) return;
  const draft = state.sbDrafts[sid] || {};
  draft.action = card.querySelector('.shot-action').value;
  draft.dialogue = card.querySelector('.shot-dialogue').value;
  const dur = card.querySelector('.shot-dur-input').value;
  draft.duration_s = dur === '' ? undefined : Number(dur);
  state.sbDrafts[sid] = draft;
  card.querySelector('.sb-save-btn').classList.remove('hidden');
  card.querySelector('.sb-dirty-hint').classList.remove('hidden');
}

async function saveShot(sid, btn) {
  const draft = state.sbDrafts[sid];
  if (!draft) return;
  btn.disabled = true;
  try {
    const body = {};
    if (draft.action !== undefined) body.action = draft.action;
    if (draft.dialogue !== undefined) body.dialogue = draft.dialogue;
    if (draft.duration_s !== undefined && !isNaN(draft.duration_s)) body.duration_s = draft.duration_s;
    await api('/shots/' + encodeURIComponent(sid), { method: 'PATCH', json: body });
    delete state.sbDrafts[sid];
    toast('镜头已保存', 'ok');
    refreshQuiet();
  } catch (err) {
    toast('保存失败:' + err.message, 'err');
    btn.disabled = false;
  }
}

async function submitShotRun(sid, btn) {
  btn.disabled = true;
  btn.textContent = '提交中…';
  try {
    const res = await api('/shots/' + encodeURIComponent(sid) + '/runs', {
      method: 'POST', json: { command_id: newCommandId() },
    });
    toast('已提交生成,run_id: ' + (res.run_id || '?'), 'ok');
    refreshQuiet();
  } catch (err) {
    toast('提交生成失败:' + err.message, 'err');
    btn.disabled = false;
    btn.textContent = '生成';
  }
}

async function removeShotReference(sid, aid) {
  try {
    await api('/shots/' + encodeURIComponent(sid) + '/references/' + encodeURIComponent(aid), { method: 'DELETE' });
    toast('参考图已移除', 'ok');
    refreshQuiet();
  } catch (err) {
    toast('移除失败:' + err.message, 'err');
  }
}

async function submitRunReview(rid, decision) {
  try {
    await api('/runs/' + encodeURIComponent(rid) + '/review', { method: 'POST', json: { decision } });
    toast('审核已提交:' + decision, 'ok');
    refreshQuiet();
  } catch (err) {
    toast('审核提交失败:' + err.message, 'err');
  }
}

/* ---------- 第 6 步:剪辑 ---------- */
function acceptedShotsSorted() {
  const scenes = getScenes();
  const sceneOrder = {};
  scenes.forEach((sc, i) => { sceneOrder[sc.scene_id || sc.id] = i; });
  return getShots().filter((s) => !!s.accepted_run_id).sort((a, b) => {
    const sa = a.scene_id || shotSpec(a).scene_id || '';
    const sb = b.scene_id || shotSpec(b).scene_id || '';
    const oa = sceneOrder[sa] != null ? sceneOrder[sa] : 999;
    const ob = sceneOrder[sb] != null ? sceneOrder[sb] : 999;
    if (oa !== ob) return oa - ob;
    return shotOrder(a, 0) - shotOrder(b, 0);
  });
}

function acceptedVideoUrl(shot) {
  const run = getRuns().find((r) => runId(r) === shot.accepted_run_id);
  if (!run) return null;
  const candidates = run.candidate_asset_ids || run.candidates || [];
  if (!candidates.length) return null;
  const aid = typeof candidates[0] === 'object' ? (candidates[0].asset_id || candidates[0].id) : candidates[0];
  return '/assets/' + encodeURIComponent(aid) + '/file';
}

function renderEditStep(el) {
  const accepted = acceptedShotsSorted();
  const total = getShots().length;
  const missing = total - accepted.length;
  const exports = (state.assets || []).filter((a) => a.source === 'derived' && isVideoAsset(a));
  if (!accepted.some((s) => shotId(s) === state.editSelectedShotId)) {
    state.editSelectedShotId = accepted.length ? shotId(accepted[0]) : null;
  }
  const selected = accepted.find((s) => shotId(s) === state.editSelectedShotId);

  let html = '<div class="edit-layout"><div>' +
    '<div class="label" style="margin-bottom:8px">TIMELINE · 已验收 ' + accepted.length + '/' + total + '</div>' +
    '<div class="timeline">';
  if (!accepted.length) {
    html += '<div class="panel db-empty">还没有已验收的镜头 — 到「分镜」步生成并接受 TAKE。</div>';
  } else {
    html += accepted.map((shot) => {
      const sid = shotId(shot);
      const url = acceptedVideoUrl(shot);
      const dur = shotSpec(shot).duration_s;
      return '<div class="timeline-row' + (sid === state.editSelectedShotId ? ' selected' : '') + '" data-shot-id="' + esc(sid) + '">' +
        (url ? '<video src="' + esc(url) + '" preload="metadata" muted></video>' : '<div class="asset-audio-thumb" style="width:96px;height:60px;font-size:20px">🎬</div>') +
        '<div class="timeline-info"><div class="timeline-shot">' + esc(sid) + '</div>' +
        '<div class="timeline-meta">' + (dur ? esc(dur) + 's · ' : '') + '<span style="color:var(--ok)">已验收 ✓</span></div></div>' +
      '</div>';
    }).join('');
  }
  html += '</div></div>';

  // 右侧:PROGRAM MONITOR + 操作
  html += '<div class="panel monitor-panel">' +
    '<div class="monitor-head"><span>PROGRAM MONITOR</span><span>' + (selected ? esc(shotId(selected)) : 'NO SIGNAL') + '</span></div>' +
    '<div class="monitor-screen">' +
      (selected && acceptedVideoUrl(selected)
        ? '<video src="' + esc(acceptedVideoUrl(selected)) + '" controls preload="metadata"></video>'
        : '<div class="monitor-empty">' + (accepted.length ? '该镜头缺少已验收视频' : '时间线为空') + '</div>') +
    '</div>' +
    '<div class="edit-actions">' +
      '<button id="dr-review-btn" class="btn btn-small"' + (state.reviewing ? ' disabled' : '') + '>' +
        (state.reviewing ? '审核中…' : '导演审核') + '</button>' +
      '<button id="edit-export-btn" class="btn btn-projector"' + (missing > 0 || state.exporting ? ' disabled' : '') + '>' +
        (state.exporting ? '导出中…' : '导出成片(带字幕)') + '</button>' +
      (missing > 0 ? '<span class="edit-hint">还差 ' + missing + ' 镜未验收,全部验收后可导出</span>' : '') +
    '</div>' +
    '<div id="dr-report" class="dr-report"></div>' +
    '<div class="export-list"><div class="label" style="margin-top:14px">已导出成片(' + exports.length + ')</div>';
  if (exports.length) {
    html += exports.map((a) => {
      const id = a.asset_id || a.id;
      const name = assetName(a);
      const url = assetFileUrl(a);
      return '<div class="export-item"><video src="' + esc(url) + '" controls preload="metadata"></video>' +
        '<div class="export-meta"><span>' + esc(name) + '</span>' +
        '<a class="btn btn-small" href="' + esc(url) + '" download="' + esc(name) + '">下载</a></div></div>';
    }).join('');
  } else {
    html += '<p class="empty-hint">暂无成片</p>';
  }
  html += '</div></div></div>';
  el.innerHTML = html;

  el.querySelectorAll('.timeline-row').forEach((row) => {
    row.addEventListener('click', () => {
      state.editSelectedShotId = row.dataset.shotId;
      renderStepContent();
    });
  });
  $('#dr-review-btn').addEventListener('click', startDirectorReview);
  $('#edit-export-btn').addEventListener('click', exportFilm);
  if (state.directorReport) renderDirectorReport($('#dr-report'), state.directorReport);
}

async function startDirectorReview() {
  const pid = state.currentProjectId;
  state.reviewing = true;
  renderStepContent();
  try {
    await api('/projects/' + encodeURIComponent(pid) + '/director-review', { method: 'POST', json: {} });
  } catch (err) {
    state.reviewing = false;
    toast('发起审核失败:' + err.message, 'err');
    renderStepContent();
    return;
  }
  const started = Date.now();
  const timer = setInterval(async () => {
    if (state.currentProjectId !== pid || Date.now() - started > 120000) {
      clearInterval(timer);
      state.reviewing = false;
      if (Date.now() - started > 120000) toast('审核超时,请稍后手动刷新查看', 'err');
      renderStepContent();
      return;
    }
    try {
      const res = await fetch('/projects/' + encodeURIComponent(pid) + '/director-review');
      if (res.status === 404) return;
      if (!res.ok) throw new Error(res.status + ' ' + res.statusText);
      const report = await res.json();
      clearInterval(timer);
      state.reviewing = false;
      state.directorReport = report;
      toast('导演审核完成', 'ok');
      renderStepContent();
    } catch (e) { /* 下次轮询再试 */ }
  }, 3000);
}

function renderDirectorReport(box, report) {
  if (!box) return;
  const issues = report.issues || [];
  let html = '<div class="dr-report-head">';
  if (report.story_coherence != null && !isNaN(Number(report.story_coherence))) {
    html += '<span class="coherence-score">剧情连贯性 ' + Number(report.story_coherence).toFixed(2) + '</span>';
  }
  if (report.created_at) html += '<span class="empty-hint" style="padding:0">' + esc(report.created_at) + '</span>';
  html += '</div>';
  if (report.summary) html += '<p class="dr-summary">' + esc(report.summary) + '</p>';
  if (issues.length) {
    html += '<table class="issues-table"><thead><tr><th>镜头</th><th>类型</th><th>严重度</th><th>详情</th></tr></thead><tbody>' +
      issues.map((it) => {
        const sev = String(it.severity || 'info').toLowerCase();
        return '<tr><td>' + esc(it.shot_id || '-') + '</td>' +
          '<td>' + esc(ISSUE_TYPE_MAP[it.type] || it.type || '-') + '</td>' +
          '<td><span class="severity-pill sev-' + esc(sev) + '">' + esc(it.severity || '-') + '</span></td>' +
          '<td>' + esc(it.detail || '') + '</td></tr>';
      }).join('') + '</tbody></table>';
  } else {
    html += '<p class="empty-hint">未发现问题 ✓</p>';
  }
  box.innerHTML = html;
}

async function exportFilm() {
  state.exporting = true;
  renderStepContent();
  try {
    const res = await api('/projects/' + encodeURIComponent(state.currentProjectId) + '/export', {
      method: 'POST', json: { subtitles: true },
    });
    const asset = res.asset || {};
    const aid = asset.asset_id || res.export_id || '';
    toast('导出成功' + (aid ? ': ' + aid : ''), 'ok');
  } catch (err) {
    toast('导出失败:' + err.message, 'err');
  } finally {
    state.exporting = false;
    refreshQuiet();
  }
}

/* ---------- SSE 事件流(侧栏指示器 + 抽屉) ---------- */
function updateSSEIndicator() {
  const dot = $('#sse-dot');
  if (!dot) return;
  dot.className = 'sse-dot' + (state.sseStatus === 'on' ? ' on' : state.sseStatus === 'err' ? ' err' : '');
}

function connectSSE() {
  closeSSE();
  if (!state.currentProjectId) return;
  const url = '/projects/' + encodeURIComponent(state.currentProjectId) + '/events?seq=' + state.lastSeq;
  const es = new EventSource(url);
  state.es = es;

  es.onopen = () => {
    state.sseRetryDelay = 1000;
    state.sseStatus = 'on';
    updateSSEIndicator();
  };
  es.onmessage = (e) => {
    let evt;
    try { evt = JSON.parse(e.data); } catch (err) { return; }
    handleSSEEvent(evt);
  };
  es.onerror = () => {
    state.sseStatus = 'err';
    updateSSEIndicator();
    es.close();
    if (state.es === es) state.es = null;
    const delay = state.sseRetryDelay;
    state.sseRetryDelay = Math.min(state.sseRetryDelay * 2, 30000);
    state.sseRetryTimer = setTimeout(() => {
      if (state.currentProjectId && state.route.view === 'studio') connectSSE();
    }, delay);
  };
}

function closeSSE() {
  if (state.es) { state.es.close(); state.es = null; }
  if (state.sseRetryTimer) { clearTimeout(state.sseRetryTimer); state.sseRetryTimer = null; }
  state.sseStatus = 'off';
  $('#sse-drawer').classList.add('hidden');
}

function handleSSEEvent(evt) {
  if (evt.event_id) {
    if (state.seenEventIds.has(evt.event_id)) return;
    state.seenEventIds.add(evt.event_id);
    if (state.seenEventIds.size > 3000) {
      state.seenEventIds = new Set(Array.from(state.seenEventIds).slice(-1500));
    }
  }
  if (typeof evt.seq === 'number' && evt.seq > state.lastSeq) state.lastSeq = evt.seq;
  state.events.push(evt);
  if (state.events.length > 300) state.events = state.events.slice(-200);
  appendSSECard(evt);

  // 生成相关事件 → 静默刷新当前步;生产过程(定妆/首帧/排队)标记在途状态
  const t = evt.type || evt.event_type || '';
  if (PRODUCE_EVENTS.includes(t)) {
    if (t === 'quick.started' || t === 'casting.started'
        || t === 'first_frame.started' || t === 'produce.resumed'
        || t === 'produce.confirmed') {
      state.producePending = true;
    }
    if (t === 'quick.orchestrated' || t === 'quick.failed'
        || t === 'produce.stopped' || t === 'produce.stop_requested'
        || t === 'produce.awaiting_confirm'
        || t === 'run.cancelled') state.producePending = false;
    refreshQuiet();
  } else if (t === 'export.completed') {
    // 只对这一轮新发生的拼接提示(SSE 回放旧事件不弹)
    const age = Date.now() / 1000 - Number(evt.timestamp || 0);
    if (age >= 0 && age < 120) {
      toast('完整成片已拼接完成,可在「剪辑」步查看', 'ok');
      fireConfetti();
    }
    refreshQuiet();
  } else if (['step.completed', 'run.failed', 'quality.evaluated',
              'artifact.created'].includes(t)) {
    refreshQuiet();
  }
}

function appendSSECard(evt) {
  const list = $('#sse-drawer-list');
  if (!list) return;
  const hint = list.querySelector('.empty-hint');
  if (hint) hint.remove();
  const t = evt.type || evt.event_type || 'unknown';
  const ts = evt.ts || evt.timestamp || '';
  const time = ts ? new Date(ts).toLocaleTimeString('zh-CN', { hour12: false })
    : new Date().toLocaleTimeString('zh-CN', { hour12: false });
  const summary = evt.summary || evt.message || evt.action_summary || '';
  const card = document.createElement('div');
  card.className = 'sse-event';
  card.innerHTML = '<div class="sse-event-head"><span>' + esc(t) + '</span><span>' +
    (evt.seq != null ? '#' + esc(evt.seq) + ' · ' : '') + esc(time) + '</span></div>' +
    (summary ? '<div>' + esc(summary) + '</div>' : '');
  list.prepend(card);
}

$('#sse-drawer-close').addEventListener('click', () => $('#sse-drawer').classList.add('hidden'));

/* ---------- 生成中计时(每秒) ---------- */
setInterval(() => {
  $$('[data-elapsed-run]').forEach((el) => {
    const run = getRuns().find((r) => runId(r) === el.dataset.elapsedRun);
    if (!run) return;
    const state = runState(run);
    const started = parseTime(run.started_at);
    const since = started || parseTime(run.created_at);
    if (state === 'QUEUED') {
      el.textContent = since ? '排队 ' + fmtElapsed(Date.now() - since) : '排队中';
      return;
    }
    if (!since) { el.textContent = '执行中'; return; }
    el.textContent = '已耗时 ' + fmtElapsed(Date.now() - since);
  });
}, 1000);


/* ================= 酷炫功能:命令面板 / 强调色 / 彩带 / 3D 倾斜 / 数字滚动 ================= */

/* ---- 强调色主题(珊瑚 / 青紫 / 翡翠) ---- */
const ACCENTS = ['coral', 'violet', 'emerald'];

function applyAccent(name) {
  const accent = ACCENTS.includes(name) ? name : 'coral';
  if (accent === 'coral') document.documentElement.removeAttribute('data-accent');
  else document.documentElement.dataset.accent = accent;
  try { localStorage.setItem('svf_accent', accent); } catch (err) { /* 忽略 */ }
  $$('#accent-menu .accent-dot').forEach((dot) => {
    dot.dataset.current = dot.dataset.accent === accent ? '1' : '';
  });
}

function initAccent() {
  let saved = 'coral';
  try { saved = localStorage.getItem('svf_accent') || 'coral'; } catch (err) { /* 忽略 */ }
  applyAccent(saved);
  const btn = $('#accent-btn');
  const menu = $('#accent-menu');
  if (!btn || !menu) return;
  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    menu.classList.toggle('hidden');
  });
  $$('#accent-menu .accent-dot').forEach((dot) => dot.addEventListener('click', () => {
    applyAccent(dot.dataset.accent);
    menu.classList.add('hidden');
    toast('强调色已切换', 'ok');
  }));
  document.addEventListener('click', (e) => {
    if (!e.target.closest('.accent-wrap')) menu.classList.add('hidden');
  });
}

/* ---- 命令面板(⌘K / Ctrl+K) ---- */
const PALETTE_ACTIONS = [
  { ico: '⌂', label: '首页', hint: '#/', run: () => go('#/') },
  { ico: '▶', label: '一键出片', hint: '#/oneclick', run: () => go('#/oneclick') },
  { ico: '✦', label: '对话出片', hint: '#/chat', run: () => go('#/chat') },
  { ico: '＋', label: '创建项目', hint: '#/new', run: () => go('#/new') },
  { ico: '▣', label: '我的项目', hint: '#/projects', run: () => go('#/projects') },
  { ico: '◔', label: '任务中心', hint: '#/tasks', run: () => go('#/tasks') },
  { ico: '▤', label: '作品中心', hint: '#/assets', run: () => go('#/assets') },
  { ico: '🎨', label: '强调色:珊瑚', hint: 'accent', run: () => applyAccent('coral') },
  { ico: '🎨', label: '强调色:青紫', hint: 'accent', run: () => applyAccent('violet') },
  { ico: '🎨', label: '强调色:翡翠', hint: 'accent', run: () => applyAccent('emerald') },
];

const paletteState = { items: [], shown: [], active: 0 };

function paletteItems() {
  const items = PALETTE_ACTIONS.slice();
  (state.paletteProjects || []).forEach((proj) => {
    const pid = proj.project_id || proj.id;
    items.push({ ico: '▣', label: proj.title || pid, hint: '打开项目',
                 run: () => go('#/studio/' + encodeURIComponent(pid) + '?step=storyboard') });
    if (proj.stoppable) {
      items.push({ ico: '■', label: '停止：' + (proj.title || pid), hint: '停止执行',
                   run: () => stopProject(pid, proj.title || '', null) });
    }
  });
  return items;
}

function openPalette() {
  const mask = $('#palette');
  const input = $('#palette-input');
  if (!mask || !input) return;
  mask.classList.remove('hidden');
  input.value = '';
  paletteState.items = paletteItems();
  paletteState.active = 0;
  drawPalette('');
  input.focus();
  api('/projects').then((res) => {
    state.paletteProjects = res.projects || res || [];
    paletteState.items = paletteItems();
    drawPalette(input.value);
  }).catch(() => { /* 忽略 */ });
}

function closePalette() {
  const mask = $('#palette');
  if (mask) mask.classList.add('hidden');
}

function drawPalette(q) {
  const list = $('#palette-list');
  if (!list) return;
  const query = String(q || '').trim().toLowerCase();
  const items = paletteState.items.filter(
    (it) => !query || (it.label + ' ' + (it.hint || '')).toLowerCase().includes(query));
  paletteState.shown = items;
  if (paletteState.active >= items.length) paletteState.active = 0;
  list.innerHTML = items.length
    ? items.map((it, i) => '<button class="palette-item' +
        (i === paletteState.active ? ' active' : '') + '" data-i="' + i + '">' +
        '<span class="palette-ico">' + it.ico + '</span>' + esc(it.label) +
        '<span class="palette-hinttext">' + esc(it.hint || '') + '</span></button>').join('')
    : '<div class="palette-empty">没有匹配项</div>';
  $$('#palette-list .palette-item').forEach((btn) => {
    btn.addEventListener('mousemove', () => {
      paletteState.active = Number(btn.dataset.i);
      $$('#palette-list .palette-item').forEach(
        (b) => b.classList.toggle('active', b === btn));
    });
    btn.addEventListener('click', () => runPaletteItem(Number(btn.dataset.i)));
  });
}

function runPaletteItem(i) {
  const item = (paletteState.shown || [])[i];
  closePalette();
  if (item && item.run) item.run();
}

function initPalette() {
  const btn = $('#palette-btn');
  const mask = $('#palette');
  const input = $('#palette-input');
  if (!btn || !mask || !input) return;
  btn.addEventListener('click', openPalette);
  mask.addEventListener('click', (e) => { if (e.target === mask) closePalette(); });
  input.addEventListener('input', (e) => drawPalette(e.target.value));
  document.addEventListener('keydown', (e) => {
    const open = !mask.classList.contains('hidden');
    if ((e.metaKey || e.ctrlKey) && String(e.key).toLowerCase() === 'k') {
      e.preventDefault();
      if (open) closePalette(); else openPalette();
      return;
    }
    if (!open) return;
    if (e.key === 'Escape') { closePalette(); }
    else if (e.key === 'ArrowDown') {
      e.preventDefault();
      paletteState.active = Math.min(paletteState.active + 1,
                                     (paletteState.shown || []).length - 1);
      drawPalette(input.value);
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      paletteState.active = Math.max(paletteState.active - 1, 0);
      drawPalette(input.value);
    } else if (e.key === 'Enter') {
      e.preventDefault();
      runPaletteItem(paletteState.active);
    }
  });
}

/* ---- 成片庆祝:彩带 ---- */
function fireConfetti() {
  const canvas = $('#confetti');
  if (!canvas || !canvas.getContext) return;
  const ctx = canvas.getContext('2d');
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  canvas.width = window.innerWidth * dpr;
  canvas.height = window.innerHeight * dpr;
  canvas.classList.remove('hidden');
  const colors = ['#E96F5D', '#F2A52A', '#7C6CF0', '#22D3EE', '#4FC79A'];
  const parts = [];
  for (let i = 0; i < 140; i += 1) {
    parts.push({
      x: canvas.width * (0.5 + (Math.random() - 0.5) * 0.4),
      y: canvas.height * 0.34,
      vx: (Math.random() - 0.5) * 15 * dpr,
      vy: (Math.random() * -13 - 3) * dpr,
      w: (6 + Math.random() * 7) * dpr,
      h: (4 + Math.random() * 6) * dpr,
      rot: Math.random() * Math.PI,
      vr: (Math.random() - 0.5) * 0.32,
      color: colors[(Math.random() * colors.length) | 0],
    });
  }
  const t0 = performance.now();
  function tick(now) {
    const t = now - t0;
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    parts.forEach((pc) => {
      pc.vy += 0.32 * dpr;
      pc.x += pc.vx; pc.y += pc.vy; pc.rot += pc.vr;
      ctx.save();
      ctx.translate(pc.x, pc.y);
      ctx.rotate(pc.rot);
      ctx.globalAlpha = Math.max(0, 1 - t / 2600);
      ctx.fillStyle = pc.color;
      ctx.fillRect(-pc.w / 2, -pc.h / 2, pc.w, pc.h);
      ctx.restore();
    });
    if (t < 2600) requestAnimationFrame(tick);
    else {
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      canvas.classList.add('hidden');
    }
  }
  requestAnimationFrame(tick);
}

/* ---- 项目卡 3D 倾斜 + 光斑跟随 ---- */
function bindCardTilt(root) {
  if (!window.matchMedia || !window.matchMedia('(hover: hover)').matches) return;
  const scope = root || document;
  scope.querySelectorAll('.db-grid .proj-card').forEach((card) => {
    if (card.dataset.tilt) return;
    card.dataset.tilt = '1';
    card.addEventListener('mousemove', (e) => {
      const r = card.getBoundingClientRect();
      const px = (e.clientX - r.left) / r.width;
      const py = (e.clientY - r.top) / r.height;
      card.style.setProperty('--mx', (px * 100).toFixed(1) + '%');
      card.style.setProperty('--my', (py * 100).toFixed(1) + '%');
      const rx = (0.5 - py) * 7;
      const ry = (px - 0.5) * 9;
      card.style.transform = 'perspective(900px) rotateX(' + rx.toFixed(2) +
        'deg) rotateY(' + ry.toFixed(2) + 'deg) translateY(-2px)';
    });
    card.addEventListener('mouseleave', () => { card.style.transform = ''; });
  });
}

/* ---- 数字滚动 ---- */
function animateCount(el, to) {
  const from = Number(el.dataset.from || 0);
  if (!isFinite(to)) return;
  if (from === to) { el.textContent = String(to); return; }
  const t0 = performance.now();
  function step(now) {
    const k = Math.min(1, (now - t0) / 450);
    const eased = 1 - Math.pow(1 - k, 3);
    el.textContent = String(Math.round(from + (to - from) * eased));
    if (k < 1) requestAnimationFrame(step);
    else el.textContent = String(to);
  }
  requestAnimationFrame(step);
}

initAccent();
initPalette();

/* ---------- 启动 ---------- */
if (!location.hash) location.hash = '#/';
render();
