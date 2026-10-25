'use strict';

const $ = (selector, parent = document) => parent.querySelector(selector);
const $$ = (selector, parent = document) => [...parent.querySelectorAll(selector)];
const state = { role: 'user', provider: '', userId: '', tmdbConfigured: false, embyAvailable: false, csrf: '', username: '', items: [], trash: [], view: 'overview', search: '', kind: '', status: '', editing: null, busy: false };
let navigationGeneration = 0, trashLoading = false, trashError = '';
let mobileMenuOpen = false;
function setMobileMenu(open) {
  mobileMenuOpen = open;
  for (const [element, name] of [[$('.sidebar'), 'open'], [$('#nav-backdrop'), 'visible'], [document.documentElement, 'nav-open']]) {
    if (open) element.classList.add(name); else element.classList.remove(name);
  }
  $('#mobile-nav-trigger')?.setAttribute?.('aria-expanded', String(open));
  if (open) $('.mobile-nav-close')?.focus?.();
}
function updatePhoneViewport() {
  const height = window.visualViewport?.height || window.innerHeight;
  if (Number.isFinite(height) && (!window.visualViewport || window.visualViewport.scale === 1)) {
    document.documentElement.style?.setProperty('--phone-viewport-height', `${Math.round(height)}px`);
  }
}
window.addEventListener('resize', updatePhoneViewport);
window.visualViewport?.addEventListener('resize', updatePhoneViewport);
updatePhoneViewport();
const statusLabels = { missing: '追更中，待补集', updating: '追更中，已补集', premiering: '今日开播', upcoming: '尚未播出', wanted: '待找资源', complete: '已完成', broken: '来源异常' };
const tmdbReadOnlyFields = ['title', 'kind', 'year', 'season', 'total_episodes', 'finished'];
const kindLabels = { tv: '剧集', movie: '电影', anime: '动漫', documentary: '纪录片' };
const viewLabels = { overview: '概览', all: '全部资源', due: '待处理', complete: '已完成', trash: '回收站', users:'用户与提交' };
const viewDetails = {
  overview: ['YOUR LIBRARY, IN FOCUS', '片库近况', '让每一部想看的作品，都有迹可循。'],
  all: ['EVERY STORY HAS A PLACE', '全部资源', '来源、版本与进度，在这里一目了然。'],
  due: ['KEEP YOUR STORIES MOVING', '待处理', '待找资源、缺集、来源异常与同步失败，集中在这里处理。'],
  complete: ['COLLECTED, WITH CARE', '已完成', '那些已经收齐的好故事，随时等你打开。'],
  trash: ['STILL HERE, FOR A WHILE', '回收站', '暂时移出的资源，可以随时恢复到片库。'],
  users: ['ACCOUNTS & REQUESTS', '用户与提交', '选择用户，查看 TA 提交的作品与当前入库进度。']
};

function esc(value) { return String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])); }
function icon(name, cls = '') { return `<svg class="icon ${esc(cls)}" aria-hidden="true"><use href="#i-${esc(name)}"/></svg>`; }
function safeURL(value) { try { const url = new URL(value); return ['http:', 'https:'].includes(url.protocol) ? url.href : ''; } catch { return ''; } }
function sourceOf(item) { return (item.sources || []).find(source => String(source.id) === String(item.active_source_id)) || null; }
function itemById(id) { return [...state.items, ...state.trash].find(item => String(item.id) === String(id)); }
function titleOf(item) { return `${item.title || '未命名作品'}${item.kind !== 'movie' ? ` · 第 ${item.season || 1} 季` : ''}`; }
function episodeCount(value) { if (!value) return 0; let count = 0; for (const part of String(value).split(',')) { const [a, b] = part.split('-').map(Number); if (Number.isFinite(a)) count += b ? b - a + 1 : 1; } return count; }
function libraryCount(item) { return Number.isFinite(item.library_count) ? item.library_count : episodeCount(item.library_episodes); }
function availableCount(item) { return Number.isFinite(item.available_count) ? item.available_count : episodeCount(sourceOf(item)?.episodes); }
function sourceEpisodeInfo(item) {
  const source = sourceOf(item), scan = source?.episode_scan, unit = item.kind === 'movie' ? '部' : '集';
  const known = !!source?.episodes;
  const uncertain = !!scan && scan.state !== 'complete';
  return {count:known ? episodeCount(source.episodes) : null,unit,
    label:!source ? '未绑定来源' : !known ? item.kind === 'movie' ? '电影资源待检测' : '网盘集数待识别' : item.kind === 'movie' ? `${uncertain ? '上次记录有正片' : scan?.state === 'complete' ? '网盘已有正片' : '来源已记录正片'}` : `${uncertain ? '上次记录' : scan?.state === 'complete' ? '网盘已有' : '来源记录'} ${episodeCount(source.episodes)} ${unit}`,
    range:source?.episodes ? item.kind === 'movie' ? '已有电影正片' : source.episodes : (source ? item.kind === 'movie' ? '等待检测电影正片' : '等待识别当前来源' : '请先选择来源'),
    pendingLabel:item.kind === 'movie' ? '待检测' : '待识别',
    note:scan?.message || (scan?.checked_at ? `识别于 ${stamp(scan.checked_at)}` : '随网站后台同步自动更新'),uncertain};
}
function sourceEpisodeProgress(item) {
  if (!sourceOf(item)) return '';
  const info = sourceEpisodeInfo(item);
  return `<div class="source-episode-progress${info.uncertain ? ' uncertain' : ''}" title="${esc(info.note)}">${esc(info.label)}${info.count !== null && item.kind !== 'movie' ? ` · ${esc(info.range)}` : ''}${info.uncertain ? '<small>识别待核对</small>' : ''}</div>`;
}
function sourceEpisodeMetric(item) {
  const info = sourceEpisodeInfo(item);
  return `<div class="source-comparison-value"><small>网盘当前来源</small><strong${info.count === null ? ' class="unknown-count"' : ''}>${info.count ?? info.pendingLabel}<em>${info.count === null ? '' : info.unit}</em></strong><span>${esc(info.range)}</span><span class="source-scan-note${info.uncertain ? ' uncertain' : ''}">${esc(info.note)}</span></div>`;
}
function resourceStatus(item) { return item.source_broken ? 'broken' : ['pending', 'missing', 'waiting_source'].includes(item.status) ? 'missing' : item.status === 'updating' && (!hasVerifiedLibrary(item) || !libraryCount(item) || item.missing_episodes) ? 'updating_group' : Object.hasOwn(statusLabels, item.status) ? item.status : 'wanted'; }
function statusBadge(item) { const status = resourceStatus(item); return `<span class="status status-${status === 'updating_group' ? 'updating' : status}">${esc(status === 'premiering' ? premiereLabel(item) : status === 'updating_group' ? '追更中' : statusLabels[status])}</span>`; }
function dateString(date = new Date()) { const parts = new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' }).formatToParts(date); const get = name => parts.find(p => p.type === name)?.value; return `${get('year')}-${get('month')}-${get('day')}`; }
function afterDays(days) { const date = new Date(`${dateString()}T12:00:00Z`); date.setUTCDate(date.getUTCDate() + days); return date.toISOString().slice(0, 10); }
function prettyDate(value, withYear = false) { if (!value) return '尚未设置'; const date = new Date(value.length === 10 ? `${value}T12:00:00+08:00` : value); if (Number.isNaN(date.getTime())) return '—'; return new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', ...(withYear ? { year: 'numeric' } : {}), month: 'numeric', day: 'numeric' }).format(date); }
const attentionTypes = {
  sync_error: {label:'同步失败',tone:'broken',hint:'检查连接与授权后，重新同步。'},
  source_error: {label:'来源异常',tone:'broken',hint:'核对当前原帖和网盘链接，必要时重新选择 RE0 来源。'},
  source_missing: {label:'待找资源',tone:'wanted',hint:'在作品详情中选择并绑定 RE0 来源，确认固定发布者后再补集。'},
  missing: {label:'待入库 / 补集',tone:'missing',hint:'通过 CloudMediaSync 补齐后，再比对 Emby 入库进度。'}
};
function attentionReasons(item) {
  const reasons = Array.isArray(item.attention_reasons) ? item.attention_reasons : [];
  return Object.keys(attentionTypes).flatMap(code => {
    const reason = reasons.find(entry => entry && entry.code === code);
    return reason ? [{...attentionTypes[code],label:code === 'missing' ? (item.kind === 'movie' ? '待入库' : '待补集') : attentionTypes[code].label,code,detail:typeof reason.detail === 'string' ? reason.detail : ''}] : [];
  });
}
function needsAttention(item) { return item.tracking !== false && item.needs_attention === true && attentionReasons(item).length > 0; }
function isTrackingInProgress(item) { return item.tracking !== false && item.status !== 'complete' && !item.deleted_at; }
function hasAttentionReason(item, code) { return attentionReasons(item).some(reason => reason.code === code); }
function attentionPriority(item) { return attentionReasons(item).reduce((priority, reason) => Math.min(priority, Object.keys(attentionTypes).indexOf(reason.code)), 3); }
function attentionBadges(item) { return attentionReasons(item).map(reason => `<span class="status status-${reason.tone}">${esc(reason.label)}</span>`).join(''); }
function attentionCell(item) {
  if (state.view !== 'due') return `<span class="date-cell">${esc(stamp(item.updated_at))}</span>`;
  const reasons = attentionReasons(item);
  return `<div class="attention-cell"><div class="attention-badges">${attentionBadges(item)}</div><small>${esc(reasons.map(reason => reason.detail).filter(Boolean).join('；'))}</small></div>`;
}
function attentionPanel(item) {
  const reasons = attentionReasons(item);
  if (!reasons.length) return '';
  return `<section class="attention-panel" aria-label="处理建议"><div class="attention-heading"><h3>需要留意</h3><span>${item.tracking === false ? '追更已暂停，不计入待处理' : '同步后自动更新处理状态'}</span></div><ul>${reasons.map(reason => `<li><span class="status status-${reason.tone}">${esc(reason.label)}</span><div>${reason.detail ? `<p>${esc(reason.detail)}</p>` : ''}<small>${esc(reason.hint)}</small></div></li>`).join('')}</ul></section>`;
}
function manualReviewHistory(item) {
  if (!item.last_check && !item.next_check) return '';
  return `<section class="form-section"><h3 class="section-label">人工复查记录 <small>历史记录，不影响待处理状态</small></h3>${item.last_check ? `<div class="detail-line"><span>上次人工复查</span><div>${esc(prettyDate(item.last_check, true))}</div></div>` : ''}${item.next_check ? `<div class="detail-line"><span>原复查日期</span><div>${esc(prettyDate(item.next_check, true))}</div></div>` : ''}</section>`;
}
function cover(item) { return `<span class="mini-cover cover-${esc(item.kind)}">${icon('film')}${item.tmdb?.poster_path ? `<img src="/api/tmdb/poster?path=${encodeURIComponent(item.tmdb.poster_path)}" alt="" loading="lazy">` : ''}</span>`; }
function toast(message, error = false) { const node = document.createElement('div'); node.className = `toast${error ? ' error' : ''}`; node.innerHTML = `${icon(error ? 'info' : 'circle-check')}<span>${esc(message)}</span>`; $('#toast-container').append(node); setTimeout(() => node.remove(), error ? 6500 : 3600); }

async function api(path, options = {}) {
  const headers = { ...(options.body ? { 'Content-Type': 'application/json' } : {}), ...(options.method && options.method !== 'GET' && path !== '/api/login' ? { 'X-CSRF-Token': state.csrf } : {}), ...(options.headers || {}) };
  let response;
  try { response = await fetch(path, { ...options, headers, credentials: 'same-origin', ...(options.body ? { body: JSON.stringify(options.body) } : {}) }); }
  catch { throw new Error('连接服务器失败，请检查网络后重试。'); }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401 && path !== '/api/login' && path !== '/api/session') showLogin();
    throw new Error(data.error || `请求失败（${response.status}），请稍后重试。`);
  }
  return data;
}

function showLogin() { closeResourceFilter(); if (typeof stopUserPolling === 'function') stopUserPolling(); if (typeof resetAdminUsers === 'function') resetAdminUsers(); document.body.dataset.layout = 'page'; state.items = []; state.trash = []; state.role = 'user'; state.view = 'overview'; state.search = ''; state.status = ''; $('#page-content').innerHTML = ''; if (typeof connection !== 'undefined') { connection.config = null; connection.health = null; connection.healthError = ''; if (typeof renderServiceConnections === 'function') renderServiceConnections(); } if (typeof stopIntegrationPolling === 'function') stopIntegrationPolling(); state.csrf = ''; $('#boot').hidden = true; $('#app').hidden = true; $('#login-screen').hidden = false; if ($('#modal').open) $('#modal').close(); setMobileMenu(false); window.scrollTo?.({top:0,left:0,behavior:'auto'}); }
async function loadItems() { const data = await api('/api/items'); state.items = Array.isArray(data.items) ? data.items : []; if (state.view === 'trash') { const trashData = await api('/api/items?trash=1'); state.trash = trashData.items || []; } updateCounts(); }
function updateCounts() { $('#nav-all').textContent = state.items.length; $('#nav-due').textContent = state.items.filter(needsAttention).length; $('#nav-complete').textContent = state.items.filter(item => item.status === 'complete').length; }
async function showApp() { applyRoleInterface(); $('#boot').hidden = true; $('#login-screen').hidden = true; $('#app').hidden = false; $('#current-user').textContent = state.username || '管理员'; await loadItems(); await navigate(location.hash.slice(1) || 'overview', false); if (typeof initializeIntegrations === 'function') initializeIntegrations(); }

async function navigate(view, setHash = true) {
  closeResourceFilter();
  const generation = ++navigationGeneration, csrf = state.csrf;
  state.view = viewLabels[view] ? view : 'overview'; if (!isAdministrator() && ['due', 'trash', 'users'].includes(state.view)) state.view = 'overview'; state.search = ''; state.kind = ''; state.status = '';
  setMobileMenu(false);
  window.scrollTo?.({top:0,left:0,behavior:'auto'});
  if (setHash && location.hash !== `#${state.view}`) history.replaceState(null, '', `#${state.view}`);
  $$('.nav-item[data-view]').forEach(node => node.classList.toggle('active', node.dataset.view === state.view));
  const [eyebrow, title, description] = viewDetails[state.view];
  $('#page-eyebrow').textContent = eyebrow; $('#page-title').innerHTML = `${esc(title)}<span class="heading-dot">.</span>`; $('#page-description').textContent = description;
  if (state.view === 'trash') {
    trashLoading = true; trashError = '';
    renderPage();
    const current = () => generation === navigationGeneration && state.view === 'trash' && isAdministrator() && state.csrf === csrf;
    try { const data = await api('/api/items?trash=1'); if (current()) state.trash = data.items || []; }
    catch (error) { if (current()) trashError = error.message; }
    finally { if (current()) { trashLoading = false; renderResults(); } }
    return;
  }
  renderPage();
}

function renderPage() {
  closeResourceFilter();
  document.body.dataset.layout = isAdministrator() && ['all','due','complete','trash','users'].includes(state.view) ? 'workspace' : 'page';
  if (!isAdministrator()) { renderUserPortal(); return; }
  if (state.view === 'overview') { renderOverview(); return; }
  if (state.view === 'users') { renderAdminUsersPage(); return; }
  const banners = { due: '仅显示正在追踪的作品：尚未绑定来源、已确认缺集、当前来源异常或同步失败。暂停的作品不计入；问题解决并同步后会自动移出。', complete: '全集已入库的作品会收在这里。总集数未知时，需要手动确认已收齐。', trash: '这里保留已移除资源的来源和进度。恢复后，会重新出现在全部资源中。' };
  $('#page-content').innerHTML = `${banners[state.view] ? `<div class="view-banner">${icon('info')}<span>${banners[state.view]}</span></div>` : ''}<section class="panel resource-panel" aria-label="${esc(viewLabels[state.view])}">${toolbar()}<div id="resource-results"></div></section>`;
  renderResults();
}

function renderOverview() {
  const complete = state.items.filter(item => item.status === 'complete').length;
  const active = state.items.filter(isTrackingInProgress).length;
  const pending = state.items.filter(needsAttention);
  const wanted = state.items.filter(item => item.status === 'wanted').length;
  const percent = state.items.length ? Math.round(complete / state.items.length * 100) : 0;
  const priorities = [...pending].sort((a, b) => attentionPriority(a) - attentionPriority(b) || String(b.updated_at || '').localeCompare(String(a.updated_at || '')));
  const stats = [
    ['全部资源', state.items.length, 'film', 'all', `${wanted ? `<b>${wanted}</b> 部等待寻找资源` : '每一部好故事，都有记录'}`],
    ['正在追更', active, 'refresh', 'updating', '未完成且已开启追更的作品'],
    ['已完成', complete, 'circle-check', 'complete', '已经收齐，随时可以观看'],
    ['待处理', pending.length, 'info', 'due', '待找资源、缺集、来源异常或同步失败']
  ];
  $('#page-content').innerHTML = `<div class="stats-grid">${stats.map(([name, count, symbol, view, note]) => `<div class="stat-card" role="button" tabindex="0" data-action="stat" data-target="${view}" aria-label="查看${name}"><div class="stat-top"><span>${name}</span><span class="stat-icon">${icon(symbol)}</span></div><div class="stat-value">${count}</div><div class="stat-note">${note}</div></div>`).join('')}</div>
    <div class="overview-columns overview-dashboard"><section class="panel overview-tasks"><div class="panel-heading"><div><h2 id="overview-tasks-title">接下来做什么 <span class="tiny-pill">${pending.length} 部待处理</span></h2><p>把更新接上，让片库慢慢完整。</p></div><button class="text-link" data-view="due">处理清单 ${icon('arrow')}</button></div><div class="overview-task-scroll" role="region" aria-labelledby="overview-tasks-title" tabindex="0">${priorities.length ? `<div class="task-list">${priorities.map(taskRow).join('')}</div>` : emptyState(state.items.length ? '当下，井井有条' : '从第一部作品开始', state.items.length ? '目前没有待找资源、缺集、来源异常或同步失败的作品。' : '添加一部正在追的剧，或一部准备收藏的电影。', true, !state.items.length)}</div></section>
    <div class="overview-side"><section class="panel progress-panel"><h3>片库收集进度</h3><div class="progress-ring" style="--progress:${percent}%"><div class="ring-inner"><strong>${percent}<span>%</span></strong><small>作品已完成</small></div></div><div class="legend-row"><span><i></i>已经收齐</span><b>${complete} 部</b></div><div class="legend-row"><span><i></i>还在路上</span><b>${state.items.length - complete} 部</b></div></section></div></div>
    ${recentActivityLauncher()}`;
}

const activityTypes = {
  requested: {label:'提交追更',symbol:'bookmark',tone:'request'},
  withdrawn: {label:'取消追更',symbol:'close',tone:'muted'},
  tracked: {label:'加入追更',symbol:'plus',tone:'request'},
  created: {label:'添加记录',symbol:'plus',tone:'request'},
  progress: {label:'更新入库',symbol:'edit',tone:'progress'},
  source: {label:'切换来源',symbol:'external',tone:'progress'},
  tracking: {label:'追更设置',symbol:'clock',tone:'muted'},
  checked: {label:'检查来源',symbol:'check',tone:'progress'},
  cms: {label:'CMS 订阅下发',symbol:'download',tone:'progress'},
  emby_sync: {label:'更新片库进度',symbol:'refresh',tone:'progress'},
  emby_absent: {label:'片库等待入库',symbol:'film',tone:'muted'},
  emby_unmatched: {label:'刮削信息待核对',symbol:'info',tone:'attention'},
  sync_error: {label:'同步异常',symbol:'info',tone:'attention'},
  source_episodes: {label:'更新来源集数',symbol:'refresh',tone:'progress'}
};
function activityMeta(action) {
  return Object.hasOwn(activityTypes, action) ? activityTypes[action] : {label:'更新记录',symbol:'edit',tone:'muted'};
}
function recentLibraryEvents(items = state.items, limit = 8) {
  const result = [];
  for (const item of items) {
    const events = (Array.isArray(item.events) ? item.events : []).flatMap((event, sequence) => {
      if (!event || typeof event !== 'object' || typeof event.at !== 'string') return [];
      const timestamp = Date.parse(event.at);
      return Number.isFinite(timestamp) ? [{at:event.at, timestamp, sequence, action:typeof event.action === 'string' ? event.action : '', detail:typeof event.detail === 'string' ? event.detail : ''}] : [];
    });
    const submissions = new Set(events.filter(event => event.action === 'requested').map(event => event.timestamp));
    for (const event of events) {
      if (['tracked','created'].includes(event.action) && submissions.has(event.timestamp)) continue;
      result.push({...event, item});
    }
    if (!events.length && item.created_at && Number.isFinite(Date.parse(item.created_at))) {
      result.push({item,at:item.created_at,timestamp:Date.parse(item.created_at),sequence:-1,action:'created',detail:'已有资源记录，早期整理轨迹未记录。'});
    }
  }
  return result.sort((a,b) => b.timestamp - a.timestamp || String(a.item.id).localeCompare(String(b.item.id)) || b.sequence - a.sequence).slice(0,limit);
}
function renderRecentActivity(limit = 30) {
  const entries = recentLibraryEvents(state.items, limit);
  if (!entries.length) return emptyState('还没有整理动态', '提交追更、检查来源或更新片库进度后，变化会显示在这里。', true);
  return `<ol class="admin-activity-list">${entries.map(entry => {
    const meta = activityMeta(entry.action);
    return `<li class="admin-activity-row"><span class="admin-activity-icon activity-${meta.tone}">${icon(meta.symbol)}</span><div class="admin-activity-copy"><div class="admin-activity-top"><span>${meta.label}</span><time datetime="${esc(entry.at)}">${esc(stamp(entry.at))}</time></div><button class="admin-activity-title" data-action="detail" data-id="${esc(entry.item.id)}">${esc(titleOf(entry.item))}${icon('arrow')}</button>${entry.detail ? `<p>${esc(entry.detail)}</p>` : ''}</div></li>`;
  }).join('')}</ol><div class="admin-activity-foot">最近 ${entries.length} 条动态 · 点击作品查看详情</div>`;
}

function recentActivityLauncher() {
  const latest = recentLibraryEvents(state.items, 1)[0];
  return `<section class="panel overview-activity"><button type="button" class="activity-launcher" data-action="recent-activity" aria-haspopup="dialog"><span class="activity-launcher-icon">${icon('clock')}</span><strong>近期动态</strong><span class="activity-launcher-preview">${latest ? `${esc(titleOf(latest.item))} · ${esc(activityMeta(latest.action).label)}` : '还没有整理动态'}</span>${latest ? `<time datetime="${esc(latest.at)}">${esc(stamp(latest.at))}</time>` : ''}<span class="activity-launcher-action">查看动态 ${icon('arrow')}</span></button></section>`;
}
function openRecentActivity() {
  if (!isAdministrator()) return;
  openModal(`${modalHeader('LIBRARY ACTIVITY', '近期动态', '查看最近的追更提交、来源变化与入库进度。')}<div class="modal-body overview-activity-dialog">${renderRecentActivity()}</div><footer class="modal-footer"><button class="button secondary" data-action="close-modal">关闭</button></footer>`, '720px');
}

function taskRow(item) {
  const reasons = attentionReasons(item);
  return `<div class="task-row" role="button" tabindex="0" data-action="detail" data-id="${esc(item.id)}">${cover(item)}<div class="task-info"><p class="resource-title">${esc(titleOf(item))}</p><div class="resource-meta">${esc(reasons.map(reason => reason.detail || reason.label).join('；'))}</div></div><div class="task-side"><div class="attention-badges">${attentionBadges(item)}</div><small>查看原因与处理建议</small></div></div>`;
}
function resourceFilterOptions(field) {
  if (field === 'kind') return [['', '所有类型'], ...Object.entries(kindLabels)];
  if (state.view === 'due') return [['', '所有处理类型'], ...Object.entries(attentionTypes).map(([value, meta]) => [value, meta.label])];
  return [['', '所有状态'], ['updating_group', '追更中'], ...Object.entries(statusLabels)];
}
function resourceFilterLabel(field) { const choices = resourceFilterOptions(field); return (choices.find(([value]) => value === state[field]) || choices[0])[1]; }
function resourceFilterContent(field) { return `<span>${esc(resourceFilterLabel(field))}</span><svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg>`; }
function resourceFilterButton(field, label) { return `<button type="button" id="${field}-filter" class="resource-filter-trigger" data-resource-filter="${field}" aria-label="${esc(label)}" aria-haspopup="listbox" aria-expanded="false" aria-controls="resource-filter-menu">${resourceFilterContent(field)}</button>`; }
function toolbar() {
  return `<div class="resource-toolbar"><div class="search-box">${icon('search')}<input id="resource-search" type="search" placeholder="搜索作品、分享者或版本…" aria-label="搜索资源" value="${esc(state.search)}"></div><div class="filters">${resourceFilterButton('kind', '筛选类型')}${resourceFilterButton('status', state.view === 'due' ? '筛选处理类型' : '筛选状态')}</div></div>`;
}
let resourceFilterTrigger = null;
function closeResourceFilter(restoreFocus = false) {
  if (!resourceFilterTrigger) return;
  const trigger = resourceFilterTrigger;
  resourceFilterTrigger = null;
  trigger.setAttribute('aria-expanded', 'false');
  $('#resource-filter-menu').hidden = true;
  if (restoreFocus && trigger.isConnected) trigger.focus();
}
function openResourceFilter(trigger, edge = '') {
  const same = resourceFilterTrigger === trigger;
  closeResourceFilter();
  if (same && !edge) return;
  const field = trigger.dataset.resourceFilter;
  if (!['kind', 'status'].includes(field)) return;
  const menu = $('#resource-filter-menu'), options = resourceFilterOptions(field);
  menu.innerHTML = options.map(([value, label], index) => `<button type="button" role="option" class="resource-filter-option" data-filter-index="${index}" data-filter-value="${esc(value)}" aria-selected="${state[field] === value}"><span>${esc(label)}</span>${icon('check')}</button>`).join('');
  menu.setAttribute('aria-label', trigger.getAttribute('aria-label'));
  const rect = trigger.getBoundingClientRect(), viewport = window.visualViewport;
  const width = Math.min(rect.width, window.innerWidth - 24);
  const bottom = viewport ? viewport.offsetTop + viewport.height : window.innerHeight;
  const top = viewport?.offsetTop || 0;
  const below = bottom - rect.bottom - 16, above = rect.top - top - 16;
  const up = below < 200 && above > below;
  menu.style.width = `${width}px`;
  menu.style.left = `${Math.max(12, Math.min(rect.left, window.innerWidth - width - 12))}px`;
  menu.style.maxHeight = `${Math.max(40, Math.min(350, up ? above : below))}px`;
  menu.hidden = false;
  menu.style.top = `${up ? rect.top - menu.offsetHeight - 6 : rect.bottom + 6}px`;
  resourceFilterTrigger = trigger;
  trigger.setAttribute('aria-expanded', 'true');
  const buttons = $$('[role="option"]', menu);
  (edge === 'last' ? buttons.at(-1) : edge === 'first' ? buttons[0] : buttons.find(button => button.getAttribute('aria-selected') === 'true') || buttons[0])?.focus();
}
document.addEventListener('click', event => {
  const trigger = event.target.closest('[data-resource-filter]');
  if (trigger) { openResourceFilter(trigger); return; }
  const option = event.target.closest('.resource-filter-option');
  if (option && resourceFilterTrigger && $('#resource-filter-menu').contains(option)) {
    const field = resourceFilterTrigger.dataset.resourceFilter;
    state[field] = option.dataset.filterValue;
    resourceFilterTrigger.innerHTML = resourceFilterContent(field);
    closeResourceFilter(true);
    renderResults();
    return;
  }
  if (!event.target.closest('#resource-filter-menu')) closeResourceFilter();
});
document.addEventListener('keydown', event => {
  const trigger = event.target.closest('[data-resource-filter]');
  if (trigger && ['ArrowDown', 'ArrowUp'].includes(event.key)) {
    event.preventDefault(); openResourceFilter(trigger, event.key === 'ArrowUp' ? 'last' : 'first'); return;
  }
  if (!resourceFilterTrigger) return;
  if (event.key === 'Escape') { event.preventDefault(); closeResourceFilter(true); return; }
  if (event.key === 'Tab') { closeResourceFilter(true); return; }
  const menu = $('#resource-filter-menu');
  if (!menu.contains(event.target) || !['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
  event.preventDefault();
  const options = $$('[role="option"]', menu), index = options.indexOf(event.target.closest('[role="option"]'));
  const next = event.key === 'Home' ? 0 : event.key === 'End' ? options.length - 1 : (index + (event.key === 'ArrowUp' ? -1 : 1) + options.length) % options.length;
  options[next]?.focus();
});
window.addEventListener('resize', () => closeResourceFilter());
document.addEventListener('scroll', event => { if (resourceFilterTrigger && !$('#resource-filter-menu').contains(event.target)) closeResourceFilter(); }, true);
function emptyState(title, description, compact = false, add = false) { return `<div class="empty-state${compact ? ' compact' : ''}"><div class="empty-icon">${icon('film')}</div><h3>${esc(title)}</h3><p>${esc(description)}</p>${add ? `<button class="button primary small" data-action="add">${icon('plus')}添加第一部资源</button>` : ''}</div>`; }

function filteredItems() {
  let items = state.view === 'trash' ? state.trash : state.items;
  if (state.view === 'due') items = items.filter(needsAttention);
  if (state.view === 'complete') items = items.filter(item => item.status === 'complete');
  const query = state.search.trim().toLocaleLowerCase();
  return items.filter(item => (!state.kind || item.kind === state.kind) && (!state.status || (state.view === 'due' ? hasAttentionReason(item, state.status) : state.status === 'updating_group' ? isTrackingInProgress(item) : resourceStatus(item) === state.status)) && (!query || [item.title, item.year, item.notes, ...(item.requesters || []).map(entry => entry.username), ...(item.sources || []).flatMap(source => [source.sharer, source.version, source.note])].join(' ').toLocaleLowerCase().includes(query))).sort((a, b) => String(b.updated_at || '').localeCompare(String(a.updated_at || '')));
}
function renderResults() {
  const container = $('#resource-results'); if (!container) return;
  if (state.view === 'trash' && trashLoading) {
    container.innerHTML = '<div class="empty-state" role="status"><div class="empty-icon"><span class="spinner" aria-hidden="true"></span></div><p>正在读取回收站…</p></div>'; return;
  }
  if (state.view === 'trash' && trashError) {
    container.innerHTML = `<div class="empty-state" role="alert"><h3>回收站读取失败</h3><p>${esc(trashError)}</p><button class="button secondary small" data-view="trash">重新读取</button></div>`; return;
  }
  const items = filteredItems();
  if (!items.length) {
    const filtered = state.search || state.kind || state.status;
    const [title, description] = filtered ? ['没有找到匹配的资源', '试试其他关键词，或调整类型与状态筛选。'] : state.view === 'trash' ? ['回收站是空的', '移除的作品会暂存在这里，方便你随时恢复。'] : state.view === 'due' ? ['当前没有待处理事项', '尚未绑定来源、确认缺集、当前来源异常或同步失败时，会自动出现在这里。'] : state.view === 'complete' ? ['好故事，还在收集路上', '作品完结且全集入库后，会自动归档到这里。'] : ['你的片库手账，等待第一笔', '保存原帖、分享者和入库进度，从此不再忘记追到了哪里。'];
    container.innerHTML = emptyState(title, description, false, !filtered && !state.items.length && !['trash', 'complete', 'due'].includes(state.view)); return;
  }
  container.innerHTML = `<div class="table-scroll" role="region" aria-label="${esc(viewLabels[state.view])}列表" tabindex="0"><table class="resource-table" data-view="${esc(state.view)}"><thead><tr><th>作品 / 类型</th><th>状态</th><th>入库 / 已播进度</th><th>当前来源</th><th>${state.view === 'due' ? '处理原因' : '最近更新'}</th><th>操作</th></tr></thead><tbody>${items.map(tableRow).join('')}</tbody></table></div><div class="table-bottom"><span>共 ${items.length} 部${state.view === 'due' ? '待处理作品' : '资源'}</span><span>${state.view === 'trash' ? '保留来源与入库记录' : '进度以实际记录为准'}</span></div>`;
}
function tableRow(item) { if (item.tmdb) return integratedTableRow(item); const source = sourceOf(item); const library = libraryCount(item); const total = item.total_episodes; const percent = total ? Math.min(100, Math.round(library / total * 100)) : item.status === 'complete' ? 100 : 0; return `<tr data-action="detail" data-id="${esc(item.id)}" tabindex="0" aria-label="查看${esc(titleOf(item))}"><td><div class="table-title-cell">${cover(item)}<div><p class="resource-title">${esc(item.title)}</p>${requesterSummary(item)}<div class="resource-meta"><span>${esc(kindLabels[item.kind] || '其他')}${item.kind !== 'movie' ? ` · 第 ${item.season || 1} 季` : ''}</span>${item.year ? `<span>· ${item.year}</span>` : ''}</div><div class="mobile-resource-status">${statusBadge(item)}</div></div></div></td><td>${statusBadge(item)}${item.source_broken ? '<div class="source-version" style="color:#bf877b">来源异常</div>' : ''}</td><td><div class="progress-cell"><div class="progress-count"><strong>${library}</strong><span> / ${total || '?'} ${item.kind === 'movie' ? '部' : '集'}</span></div><div class="progress-track"><i style="width:${percent}%"></i></div></div></td><td><div class="source-name">${esc(source?.sharer || '暂未记录')}</div><div class="source-version">${esc(source?.version || (source ? '版本未填写' : '等待合适来源'))}</div>${sourceEpisodeProgress(item)}</td><td>${attentionCell(item)}</td><td><div class="row-actions">${state.view === 'trash' ? `<button class="icon-button" title="恢复资源" aria-label="恢复${esc(item.title)}" data-action="restore" data-id="${esc(item.id)}">${icon('refresh')}</button>` : `<button class="icon-button" title="编辑资源" aria-label="编辑${esc(item.title)}" data-action="edit" data-id="${esc(item.id)}">${icon('edit')}</button>`}<button class="icon-button" title="查看详情" aria-label="查看${esc(item.title)}详情" data-action="detail" data-id="${esc(item.id)}">${icon('arrow')}</button></div></td></tr>`; }

function openModal(html, width = '790px') {
  closeResourceFilter();
  const modal = $('#modal');
  delete modal.dataset.itemId;
  $('#modal-content').innerHTML = html;
  modal.style.width = width;
  if (!modal.open) modal.showModal();
  document.documentElement.classList.add('modal-open');
  modal.scrollTop = 0;
}
function modalHeader(eyebrow, title, description = '') { return `<header class="modal-header"><div><p class="eyebrow">${esc(eyebrow)}</p><h2>${esc(title)}</h2>${description ? `<p>${esc(description)}</p>` : ''}</div><button class="icon-button" type="button" data-action="close-modal" aria-label="关闭弹窗">${icon('close')}</button></header>`; }
function newSource() { return { id: globalThis.crypto?.randomUUID?.() || `source-${Date.now()}-${Math.random().toString(36).slice(2)}`, sharer: '', post_url: '', share_url: '', code: '', version: '', episodes: '', broken: false, note: '' }; }

function openEditor(id = null) { if (!isAdministrator()) return;
  const existing = id ? itemById(id) : null; state.editing = existing ? structuredClone(existing) : null;
  const item = existing || { title: '', kind: 'tv', year: null, season: 1, total_episodes: null, finished: false, completed: false, library_episodes: '', sources: [], active_source_id: '', next_check: '', notes: '' };
  const sources = item.sources?.length ? item.sources : [newSource()]; const activeID = item.active_source_id || sources[0].id;
  openModal(`${modalHeader(existing ? 'EDIT YOUR COLLECTION' : 'A NEW STORY BEGINS', existing ? '整理这部资源' : '添加一部好故事', '作品与季分别记录，来源进度和实际入库进度独立保存。')}
    <form id="item-form"><div class="modal-body">${item.tmdb ? `<div class="view-banner">${icon('info')}<span>作品信息由 TMDB 自动同步，不可手动修改。可调整追更开关、资源来源、入库记录和备注。</span></div>` : ''}<section class="form-section"><h3 class="section-label">01 <span>作品信息</span>${item.tmdb ? '<small>TMDB 自动更新 · 只读</small>' : ''}</h3><div class="form-grid"><label class="span-all">作品名称 <span aria-hidden="true">*</span><input name="title"${item.tmdb ? ' disabled' : ''} required maxlength="150" placeholder="例如：正在追的那部剧" value="${esc(item.title)}"></label><label>资源类型${item.tmdb ? `<input name="kind" value="${esc(kindLabels[item.kind] || item.kind)}" disabled>` : `<select name="kind">${Object.entries(kindLabels).map(([value, name]) => `<option value="${value}"${item.kind === value ? ' selected' : ''}>${name}</option>`).join('')}</select>`}</label><label>年份<input name="year"${item.tmdb ? ' disabled' : ''} type="number" min="1888" max="2200" placeholder="选填" value="${esc(item.year || '')}"></label><label class="series-field"${item.kind === 'movie' ? ' hidden' : ''}>第几季<input name="season"${item.tmdb ? ' disabled' : ''} type="number" min="1" max="1000" required value="${item.season || 1}"></label><label class="series-field"${item.kind === 'movie' ? ' hidden' : ''}>预计总集数<input name="total_episodes"${item.tmdb ? ' disabled' : ''} type="number" min="1" max="5000" placeholder="未知可留空" value="${esc(item.total_episodes || '')}"></label></div><div class="checkbox-row"><label class="checkbox-label"><input name="tracking" type="checkbox"${item.tracking !== false ? ' checked' : ''}>开启自动追更</label><label class="checkbox-label"><input name="finished"${item.tmdb ? ' disabled' : ''} type="checkbox"${item.finished ? ' checked' : ''}>作品已完结 / 已上映</label><label class="checkbox-label series-field"${item.kind === 'movie' ? ' hidden' : ''}><input name="completed" type="checkbox"${item.completed ? ' checked' : ''}>总集数未知，手动确认已收齐</label></div></section>
    <section class="form-section"><h3 class="section-label">02 <span>实际入库进度</span></h3><div class="series-field"${item.kind === 'movie' ? ' hidden' : ''}><label>EMBY 已入库集数<input name="library_episodes" maxlength="4000" placeholder="例如：1-8,10-12" value="${esc(item.library_episodes)}"></label><p class="form-hint">输入 <strong>1-12</strong> 表示第 1 至 12 集；单独输入 <strong>12</strong> 仅代表第 12 集。留空表示尚未入库。</p></div><label class="checkbox-label movie-field"${item.kind !== 'movie' ? ' hidden' : ''}><input name="movie_library" type="checkbox"${libraryCount(item) ? ' checked' : ''}>这部电影已经入库 EMBY</label></section>
    <section class="form-section"><h3 class="section-label">03 <span>资源来源</span><small>切换当前来源，原来源继续保留</small></h3><div id="sources-editor">${sources.map((source, index) => sourceEditor(source, index, String(source.id) === String(activeID), !!existing?.sources?.some(s => s.id === source.id), item.kind === 'movie')).join('')}</div><button type="button" class="button source-add" data-action="add-source">${icon('plus')}新增一个来源</button></section>
    <section class="form-section"><h3 class="section-label">04 <span>备注</span></h3><div class="form-grid"><label class="span-all">备注<textarea name="notes" maxlength="10000" placeholder="例如：等原分享者补齐，不更换其他版本">${esc(item.notes || '')}</textarea></label></div></section><p id="item-error" class="form-error" role="alert"></p></div><footer class="modal-footer"><button class="button secondary" type="button" data-action="close-modal">取消</button><button class="button primary" type="submit">${icon('check')}${existing ? '保存修改' : '加入片库'}</button></footer></form>`);
  const editing = state.editing;
  const selected = existing && sourceOf(existing);
  if (selected?.share_url && !selected.episodes) setTimeout(() => {
    if (state.editing !== editing || !$('#modal').open) return;
    const card = $$('.source-card').find(card => card.dataset.sourceId === selected.id);
    const button = card && $('[data-action="scan-source"]', card);
    if (button && !button.disabled) scanSourceEpisodes(button);
  }, 0);
}

function sourceEpisodeField(source, existing, movie) {
  const enabled = existing && !!state.editing?.id && !!source.share_url;
  return `<div class="source-episode-field"><label class="series-field"${movie ? ' hidden' : ''}>此来源已有集数<input name="episodes" maxlength="4000" placeholder="可自动识别，也可填写 1-12" value="${esc(source.episodes)}"></label><div class="movie-field movie-source-field"${movie ? '' : ' hidden'}><span class="field-label">电影资源</span><label class="checkbox-label movie-availability-control"><input name="movie_available" type="checkbox"${episodeCount(source.episodes) ? ' checked' : ''}><span>此来源已有电影资源</span></label></div><div class="source-scan-actions"><button type="button" class="button secondary small" data-action="scan-source"${enabled ? '' : ' disabled'}>${icon('search')}${movie ? '检测资源' : '识别集数'}</button><small class="source-scan-status" role="status">${enabled ? '后台随网站同步更新；手动检测后需保存。' : '先保存分享链接与提取码，即可自动检测。'}</small></div></div>`;
}
async function scanSourceEpisodes(button) {
  const item = state.editing, card = button.closest('.source-card');
  if (!isAdministrator() || !item || !card || button.disabled) return;
  const source = item.sources?.find(source => source.id === card.dataset.sourceId);
  const status = $('.source-scan-status', card), input = $('[name="episodes"]', card);
  if (!source) return;
  const form = $('#item-form'), kind = $('[name="kind"]', form);
  const unchanged = () => $('[name="share_url"]', card).value.trim() === source.share_url && $('[name="code"]', card).value.trim() === source.code &&
    Number($('[name="season"]', form).value || 1) === item.season && (!kind || kind.disabled || kind.value === item.kind);
  if (!unchanged()) { status.textContent = '分享链接、提取码、类型或季数已修改，请先保存再识别。'; return; }
  const movie = item.kind === 'movie', available = $('[name="movie_available"]', card);
  const original = input.value, originalAvailable = available?.checked;
  button.disabled = true; button.innerHTML = `${icon('refresh')}${movie ? '检测中…' : '识别中…'}`;
  status.textContent = movie ? '正在检测 115 分享目录中的电影正片…' : '正在读取 115 分享目录与视频集号…';
  try {
    const {result} = await api(`/api/items/${encodeURIComponent(item.id)}/sources/${encodeURIComponent(source.id)}/episodes`, {method:'POST',body:{}});
    if (state.editing !== item || !card.isConnected || !$('#modal').open || !isAdministrator()) return;
    if (!unchanged() || input.value !== original || available?.checked !== originalAvailable) { status.textContent = movie ? '检测已完成，当前输入已变化，已保留你的修改。' : `识别到 ${result.episode_count} 集（${result.episodes || '暂无集号'}）。当前输入已变化，已保留你的修改。`; return; }
    if (result.state === 'complete' && result.episode_count > 0) {
      input.value = result.episodes;
      if (movie && available) available.checked = true;
      status.textContent = movie ? `已检测到电影正片 · ${stamp(result.checked_at)}。保存修改后生效。` : `已识别 ${result.episode_count} 集：${result.episodes} · ${stamp(result.checked_at)}。保存修改后生效。`;
    } else if (result.state === 'partial') {
      status.textContent = movie ? '电影正片暂无法完整确认，原值保留，请核对分享目录。' : `已识别 ${result.episode_count} 集（${result.episodes || '暂无集号'}），另有 ${result.unrecognized_count} 个视频集号不明确，原值保留。${result.examples?.[0] ? '例如：' + result.examples[0].slice(0, 100) : ''}`;
    } else { status.textContent = movie ? '未检测到电影正片，原值保留，请核对分享目录。' : '没有识别到当前季的正片集号，原值保留，请核对分享目录或手动填写。'; }
  } catch (error) {
    if (state.editing === item && card.isConnected) status.textContent = error.message;
  } finally { button.disabled = false; button.innerHTML = `${icon('search')}${movie ? '检测资源' : '识别集数'}`; }
}

function sourceEditor(source, index, active, existing, movie = false) { return `<fieldset class="source-card${active ? ' active-source' : ''}" data-source-id="${esc(source.id)}" data-existing="${existing ? '1' : '0'}"><div class="source-card-header"><label class="checkbox-label"><input type="radio" name="active_source_id" value="${esc(source.id)}"${active ? ' checked' : ''}><span>设为当前使用的来源</span></label><span class="source-index">SOURCE ${String(index + 1).padStart(2, '0')}${existing ? ' · 已保留' : ''}</span>${!existing ? `<button class="icon-button" data-action="remove-source" type="button" aria-label="移除尚未保存的来源" title="移除此未保存来源">${icon('close')}</button>` : ''}</div><div class="form-grid"><label>分享者<input name="sharer" maxlength="200" placeholder="保留原分享者，便于统一版本" value="${esc(source.sharer)}"></label><label>版本信息<input name="version" maxlength="500" placeholder="例如：4K / 国语 / 内封简中" value="${esc(source.version)}"></label><label class="span-all">资源网站原帖<input name="post_url" type="url" maxlength="2000" placeholder="https://…" value="${esc(source.post_url)}"></label><label class="span-all">115 分享链接<input name="share_url" type="url" maxlength="2000" placeholder="https://115.com/…" value="${esc(source.share_url)}"></label><label>提取码<input name="code" maxlength="100" placeholder="选填" value="${esc(source.code)}" autocomplete="off"></label>${sourceEpisodeField(source, existing, movie)}<label class="span-all">来源说明<input name="note" maxlength="2000" placeholder="例如：从第 13 集起使用此来源" value="${esc(source.note)}"></label></div><div class="checkbox-row"><label class="checkbox-label"><input name="broken" type="checkbox"${source.broken ? ' checked' : ''}>链接失效 / 来源异常</label></div></fieldset>`; }

function normalizeEpisodes(value, label) {
  const normalized = String(value || '').trim().replace(/[，、；;]/g, ',').replace(/[－–—~～]/g, '-').replace(/\s/g, '');
  if (!normalized) return '';
  if (!/^\d+(?:-\d+)?(?:,\d+(?:-\d+)?)*$/.test(normalized)) throw new Error(`${label}格式不正确，请填写 1-8,10-12 这样的集数。`);
  for (const part of normalized.split(',')) { const [start, end = start] = part.split('-').map(Number); if (start < 1 || end < start || end > 5000) throw new Error(`${label}请使用 1 至 5000 的正整数，区间从小到大填写。`); }
  return normalized;
}
function numberOrNull(value) { return value === '' ? null : Number(value); }
async function saveItem(event) {
  event.preventDefault(); const form = event.target; const button = $('button[type="submit"]', form); const errorNode = $('#item-error'); errorNode.textContent = '';
  try {
    const fields = new FormData(form); const movie = (fields.get('kind') || state.editing?.kind) === 'movie';
    const sources = $$('.source-card', form).map(card => { const get = name => $(`[name="${name}"]`, card); const hasContent = ['sharer', 'post_url', 'share_url', 'code', 'version', 'episodes', 'note'].some(name => get(name).value.trim()) || get('broken').checked || (movie && get('movie_available').checked); if (!hasContent && card.dataset.existing !== '1') return null; return { id: card.dataset.sourceId, sharer: get('sharer').value.trim(), post_url: get('post_url').value.trim(), share_url: get('share_url').value.trim(), code: get('code').value.trim(), version: get('version').value.trim(), episodes: movie ? get('movie_available').checked ? '1' : '' : normalizeEpisodes(get('episodes').value, '来源集数'), broken: get('broken').checked, note: get('note').value.trim() }; }).filter(Boolean);
    const payload = { ...(state.editing || {}), title: String(fields.get('title') || '').trim(), kind: fields.get('kind') || state.editing?.kind || 'tv', year: numberOrNull(fields.get('year')), season: movie ? 1 : Number(fields.get('season') || state.editing?.season || 1), total_episodes: movie ? 1 : numberOrNull(fields.get('total_episodes')), tracking: fields.has('tracking'), finished: fields.has('finished'), completed: !movie && fields.has('completed'), library_episodes: movie ? fields.has('movie_library') ? '1' : '' : normalizeEpisodes(fields.get('library_episodes'), '入库集数'), sources, active_source_id: sources.some(source => source.id === fields.get('active_source_id')) ? fields.get('active_source_id') : sources[0]?.id || '', next_check: state.editing?.next_check || '', notes: String(fields.get('notes') || '').trim() };
    if (state.editing?.tmdb) for (const field of tmdbReadOnlyFields) payload[field] = state.editing[field];
    if (!payload.title) throw new Error('请填写作品名称。');
    button.disabled = true; button.innerHTML = '正在保存…';
    await api(state.editing ? `/api/items/${encodeURIComponent(state.editing.id)}` : '/api/items', { method: state.editing ? 'PUT' : 'POST', body: payload });
    $('#modal').close(); await loadItems(); renderPage(); toast(state.editing ? '资源已更新，来源历史已保留。' : '已加入片库，故事从这里开始。');
  } catch (error) { errorNode.textContent = error.message; errorNode.scrollIntoView({ block: 'nearest' }); }
  finally { button.disabled = false; button.innerHTML = `${icon('check')}${state.editing ? '保存修改' : '加入片库'}`; }
}

function linkButton(url, text) { const href = safeURL(url); return href ? `<a class="button secondary small" href="${esc(href)}" target="_blank" rel="noopener noreferrer">${icon('external')}${esc(text)}</a>` : ''; }
function openDetail(id) { if (!isAdministrator()) { openUserDetail(id); return; } if (itemById(id)?.tmdb) { openIntegratedDetail(id); return; }
  const item = itemById(id); if (!item) return; const trashed = state.view === 'trash'; const library = libraryCount(item); const available = availableCount(item);
  openModal(`${modalHeader('THE STORY SO FAR', titleOf(item), `${kindLabels[item.kind] || ''}${item.year ? ` · ${item.year}` : ''} · ${item.finished ? '已完结 / 已上映' : '尚未完结'}`)}<div class="modal-body"><div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:20px">${statusBadge(item)}${item.source_broken ? '<span class="status status-broken">当前来源异常</span>' : ''}</div>${attentionPanel(item)}<div class="detail-overview"><div class="detail-stat"><small>实际已入库</small><strong>${library}<span style="font-size:12px;color:#a3af96"> / ${item.total_episodes || '?'} ${item.kind === 'movie' ? '部' : '集'}</span></strong><p>${esc(item.library_episodes || '尚未入库')}</p></div><div class="detail-stat"><small>当前来源已有</small><strong>${available}<span style="font-size:12px;color:#a3af96"> ${item.kind === 'movie' ? '部' : '集'}</span></strong><p>${esc(sourceOf(item)?.episodes || '尚未记录集数')}</p></div></div>${item.missing_episodes ? `<div class="view-banner">${icon('download')}<span>来源已有、等待入库：<strong>${esc(item.missing_episodes)}</strong></span></div>` : ''}<section class="form-section"><h3 class="section-label">来源与版本 <small>${item.sources?.length || 0} 个来源，保留历史记录</small></h3>${item.sources?.length ? item.sources.map(source => detailSource(source, String(source.id) === String(item.active_source_id))).join('') : '<div class="note-block">还没有资源来源，可以先保存想收的作品。</div>'}</section>${item.notes ? `<section class="form-section"><h3 class="section-label">我的备注</h3><div class="note-block">${esc(item.notes)}</div></section>` : ''}${manualReviewHistory(item)}</div><footer class="modal-footer">${trashed ? `<button class="button primary" data-action="restore" data-id="${esc(item.id)}">${icon('refresh')}恢复到片库</button>` : `<button class="button subtle left-action" data-action="delete" data-id="${esc(item.id)}">${icon('trash')}移到回收站</button><button class="button secondary" data-action="close-modal">关闭</button><button class="button primary" data-action="edit" data-id="${esc(item.id)}">${icon('edit')}编辑资源</button>`}</footer>`);
  $('#modal .modal-body').insertAdjacentHTML('beforeend', requesterPanel(item) + eventTimeline(item));
  $('#modal').dataset.itemId = String(id);
}
function detailSource(source, current) { return `<div class="detail-source${current ? ' current' : ''}"><div class="detail-source-head"><strong>${esc(source.sharer || '未填写分享者')}</strong><small>${current ? '● 当前使用' : '历史来源'}${source.broken ? ' · 链接异常' : ''}</small></div><div class="detail-line"><span>资源版本</span><div>${esc(source.version || '尚未填写')}</div></div><div class="detail-line"><span>已有集数</span><div>${esc(source.episodes || '尚未记录')}</div></div>${source.code ? `<div class="detail-line"><span>提取码</span><div><code>${esc(source.code)}</code></div></div>` : ''}${source.note ? `<div class="detail-line"><span>来源说明</span><div>${esc(source.note)}</div></div>` : ''}<div class="source-links">${linkButton(source.post_url, '打开资源原帖')}${linkButton(source.share_url, '打开 115 分享')}${!safeURL(source.post_url) && !safeURL(source.share_url) ? '<span class="inline-note">尚未填写可打开的链接</span>' : ''}</div></div>`; }

async function markChecked(event) { event.preventDefault(); const form = event.target; const button = $('button', form); button.disabled = true; try { await api(`/api/items/${encodeURIComponent(form.dataset.id)}/check`, { method: 'POST', body: { next_check: new FormData(form).get('next_check') || '' } }); await loadItems(); renderPage(); openDetail(form.dataset.id); toast('已记录本次检查时间。'); } catch (error) { toast(error.message, true); } finally { button.disabled = false; } }
function confirmDelete(id) { const item = itemById(id); if (!item) return; openModal(`${modalHeader('KEEP IT FOR LATER', '移到回收站？')}<div class="modal-body"><p style="font-size:13px;color:#829473">「${esc(titleOf(item))}」会移出资源列表，已有来源与进度继续保留，之后可以恢复。</p></div><footer class="modal-footer"><button class="button secondary" data-action="detail" data-id="${esc(id)}">再想想</button><button class="button danger" data-action="confirm-delete" data-id="${esc(id)}">${icon('trash')}移到回收站</button></footer>`, '460px'); }
async function deleteItem(id, button) { button.disabled = true; try { const result = await api(`/api/items/${encodeURIComponent(id)}`, { method: 'DELETE' }); $('#modal').close(); await loadItems(); renderPage(); toast(result.cms_warning ? '已移到回收站，但 CMS 暂停失败，请在回收站详情重试下发。' : '已移到回收站，需要时可以恢复。', !!result.cms_warning); } catch (error) { toast(error.message, true); } finally { button.disabled = false; } }
async function restoreItem(id, button) { button.disabled = true; try { const result = await api(`/api/items/${encodeURIComponent(id)}/restore`, { method: 'POST', body: {} }); $('#modal').close(); await loadItems(); renderPage(); toast(result.cms_warning ? '已恢复到片库，CMS 下发失败，请在详情重试。' : '已恢复到片库。', !!result.cms_warning); } catch (error) { toast(error.message, true); } finally { button.disabled = false; } }

async function openSettings() { if (!isAdministrator()) return; await Promise.all([refreshIntegrationStatus(false).catch(error => toast(error.message, true)), refreshEmailSettings()]); if (!isAdministrator()) return; openModal(`${modalHeader('YOUR PRIVATE SPACE', '设置与备份', '管理服务连接、邮件提醒和数据备份。')}<div class="modal-body">${emailNotificationSettingsSection()}${integrationSettingsSection()}<section class="settings-group"><h3>数据备份</h3><p>导出作品、来源和进度为 JSON 文件。备份包含分享链接与提取码，请妥善保管。导入后按照服务器规则合并或跳过重复记录。</p><div class="settings-actions"><button class="button secondary" data-action="export">${icon('download')}导出 JSON 备份</button><button class="button secondary" data-action="import">${icon('upload')}导入 JSON 备份</button></div></section><section class="settings-group"><h3>修改登录密码</h3><p>当前登录：${esc(state.username || '管理员')}。建议使用至少 12 位、包含多种字符的密码。</p><form id="password-form"><div class="form-grid"><label class="span-all">当前密码<input name="old_password" type="password" autocomplete="current-password" required></label><label>新密码<input name="new_password" type="password" autocomplete="new-password" minlength="12" maxlength="200" required></label><label>再次输入新密码<input name="confirm_password" type="password" autocomplete="new-password" minlength="12" maxlength="200" required></label></div><p id="password-error" class="form-error" role="alert"></p><button class="button primary" type="submit">${icon('check')}保存新密码</button></form></section><p class="form-hint">时区：Asia/Shanghai · 新追更邮件提醒由后台发送。TMDB 提供播出信息，Emby 对比实际入库；RE0 自动维护来源，CMS 接收订阅并执行转存。</p></div><footer class="modal-footer"><button class="button secondary" data-action="close-modal">完成</button></footer>`, '650px'); adaptAdminSettings(); }
async function changePassword(event) { event.preventDefault(); const form = event.target; const fields = new FormData(form); const errorNode = $('#password-error'); errorNode.textContent = ''; if (fields.get('new_password') !== fields.get('confirm_password')) { errorNode.textContent = '两次输入的新密码不一致。'; return; } const button = $('button[type="submit"]', form); button.disabled = true; try { const data = await api('/api/password', { method: 'POST', body: { old_password: fields.get('old_password'), new_password: fields.get('new_password') } }); if (data.csrf) state.csrf = data.csrf; form.reset(); toast('登录密码已更新。'); } catch (error) { errorNode.textContent = error.message; } finally { button.disabled = false; } }

async function exportBackup() { try { const data = await api('/api/backup'); const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' }); const url = URL.createObjectURL(blob); const anchor = document.createElement('a'); anchor.href = url; anchor.download = `拾影备份-${dateString()}.json`; document.body.append(anchor); anchor.click(); anchor.remove(); setTimeout(() => URL.revokeObjectURL(url), 5000); toast('备份已导出，请妥善保存。'); } catch (error) { toast(error.message, true); } }
async function importBackup(file) { if (!file) return; if (file.size > 8 * 1024 * 1024) { toast('备份文件超过 8 MB，请检查文件。', true); return; } try { const data = JSON.parse(await file.text()); if (data.schema_version !== 1 || !Array.isArray(data.items)) throw new Error('备份格式不正确，需要 schema_version 为 1 且包含 items 列表的 JSON。'); openModal(`${modalHeader('BRING YOUR STORIES HOME', '导入备份')}<div class="modal-body"><p style="font-size:13px;color:#829473">已读取「${esc(file.name)}」，包含 ${data.items.length} 部资源。导入后，服务器会返回新增和跳过的数量。</p><p class="form-hint">导入前建议先导出当前片库备份。</p><p id="import-error" class="form-error" role="alert"></p></div><footer class="modal-footer"><button class="button secondary" data-action="close-modal">取消</button><button id="confirm-import" class="button primary">${icon('upload')}确认导入</button></footer>`, '480px'); $('#confirm-import').addEventListener('click', async event => { const button = event.currentTarget; button.disabled = true; try { const result = await api('/api/import', { method: 'POST', body: { schema_version: 1, items: data.items } }); $('#modal').close(); await loadItems(); renderPage(); toast(`导入完成：新增 ${result.imported || 0} 部，跳过 ${result.skipped || 0} 部。`); } catch (error) { $('#import-error').textContent = error.message; } finally { button.disabled = false; } }); } catch (error) { toast(error instanceof SyntaxError ? '无法读取 JSON，请选择有效的拾影备份文件。' : error.message, true); } }

function toggleMovieFields() { const form = $('#item-form'); if (!form) return; const movie = $('[name="kind"]', form).value === 'movie'; $$('.series-field', form).forEach(node => { node.hidden = movie; }); $$('.movie-field', form).forEach(node => { node.hidden = !movie; }); }

document.addEventListener('click', async event => {
  const trigger = event.target.closest('[data-action], [data-view]'); if (!trigger) return;
  if (trigger.matches('[data-view]')) { await navigate(trigger.dataset.view); return; }
  const action = trigger.dataset.action, id = trigger.dataset.id;
  if (action === 'menu') setMobileMenu(!mobileMenuOpen);
  else if (action === 'close-menu') { setMobileMenu(false); $('#mobile-nav-trigger')?.focus(); }
  else if (action === 'add') openTmdbSearch();
  else if (action === 'edit') openEditor(id);
  else if (action === 'detail') openDetail(id);
  else if (action === 'close-modal') $('#modal').close();
  else if (action === 'delete') confirmDelete(id);
  else if (action === 'confirm-delete') await deleteItem(id, trigger);
  else if (action === 'restore') await restoreItem(id, trigger);
  else if (action === 'settings') openSettings();
  else if (action === 'users') openUsers();
  else if (action === 'recent-activity') openRecentActivity();
  else if (action === 'export') await exportBackup();
  else if (action === 'import') $('#import-file').click();
  else if (action === 'logout') { trigger.disabled = true; try { await api('/api/logout', { method: 'POST', body: {} }); showLogin(); $('#login-form').reset(); configureLogin({emby_available:state.embyAvailable}); toast('已退出登录。'); } catch (error) { toast(error.message, true); } finally { trigger.disabled = false; } }
  else if (action === 'scan-source') await scanSourceEpisodes(trigger);
  else if (action === 'add-source') { const cards = $$('.source-card'); const source = newSource(); $('#sources-editor').insertAdjacentHTML('beforeend', sourceEditor(source, cards.length, !cards.length, false, $('[name="kind"]', $('#item-form')).value === 'movie')); const newCard = $$('.source-card').at(-1); newCard.scrollIntoView({ behavior: 'smooth', block: 'nearest' }); $('[name="sharer"]', newCard).focus({ preventScroll: true }); }
  else if (action === 'remove-source') { const card = trigger.closest('.source-card'); if (card.dataset.existing === '1') return; const wasActive = $('[name="active_source_id"]', card).checked; card.remove(); if (wasActive) { const first = $('.source-card input[name="active_source_id"]'); if (first) { first.checked = true; first.closest('.source-card').classList.add('active-source'); } } }
  else if (action === 'stat') { const target = trigger.dataset.target; await navigate(target === 'updating' ? 'all' : target); if (target === 'updating') { state.status = 'updating_group'; renderPage(); } }
});
document.addEventListener('submit', event => { if (event.target.id === 'item-form') saveItem(event); else if (event.target.id === 'check-form') markChecked(event); else if (event.target.id === 'password-form') changePassword(event); });
document.addEventListener('input', event => { if (event.target.id === 'resource-search') { state.search = event.target.value; renderResults(); } });
document.addEventListener('change', event => {
  if (event.target.name === 'kind') toggleMovieFields();
  if (event.target.name === 'active_source_id') $$('.source-card').forEach(card => card.classList.toggle('active-source', $('[name="active_source_id"]', card).checked));
});
document.addEventListener('keydown', event => { const trigger = event.target.closest('[role="button"][data-action], tr[data-action]'); if (trigger && ['Enter', ' '].includes(event.key) && !event.target.closest('button, input, select, textarea, a')) { event.preventDefault(); trigger.click(); } });
$('#nav-backdrop').addEventListener('click', () => setMobileMenu(false));
document.addEventListener('keydown', event => { if (event.key === 'Escape' && mobileMenuOpen) { setMobileMenu(false); $('#mobile-nav-trigger')?.focus(); } });
// Native Escape, buttons, backdrop and successful saves all dispatch close.
$('#modal').addEventListener('close', () => {
  if (!$('#modal').open) document.documentElement.classList.remove('modal-open');
});
$('#modal').addEventListener('click', event => { if (event.target === $('#modal')) { const rect = event.target.getBoundingClientRect(); if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) $('#modal').close(); } });
$('#import-file').addEventListener('change', event => { const file = event.target.files[0]; event.target.value = ''; importBackup(file); });
$('#login-form').addEventListener('submit', async event => { event.preventDefault(); const button = $('button[type="submit"]', event.target); const fields = new FormData(event.target); $('#login-error').textContent = ''; button.disabled = true; button.innerHTML = '正在登录…'; try { const data = await api('/api/login', { method: 'POST', body: { username: fields.get('username'), password: fields.get('password'), provider: fields.get('provider') || 'local' } }); state.csrf = data.csrf; state.username = data.username || String(fields.get('username')); applyIdentity(data); await showApp(); } catch (error) { $('#login-error').textContent = error.message; } finally { button.disabled = false; button.innerHTML = `登录拾影 ${icon('arrow')}`; } });
window.addEventListener('hashchange', () => { if (!$('#app').hidden) navigate(location.hash.slice(1) || 'overview', false); });

document.addEventListener('DOMContentLoaded', async function boot() { try { const session = await api('/api/session'); if (session.authenticated) { state.csrf = session.csrf || ''; state.username = session.username || ''; applyIdentity(session); await showApp(); } else { showLogin(); configureLogin(session); } } catch (error) { showLogin(); $('#login-error').textContent = error.message; } });
