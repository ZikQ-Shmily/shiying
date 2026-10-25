'use strict';

let userPollTimer = null;
function isAdministrator() { return state.role === 'admin'; }
function applyIdentity(data) { if (state.role !== data.role) { resetAdminUsers(); state.view = 'overview'; state.trash = []; state.search = ''; state.status = ''; } state.role = data.role || 'user'; state.provider = data.provider || ''; state.userId = data.user_id || ''; state.tmdbConfigured = !!data.tmdb_configured; state.embyAvailable = !!data.emby_available; }
function configureLogin(data) {
  state.embyAvailable = !!data.emby_available;
  const select = $('#login-provider');
  const embyOption = $('option[value="emby"]', select);
  embyOption.disabled = !data.emby_available;
  select.value = data.emby_available ? 'emby' : 'local';
  updateLoginHint();
}
function updateLoginHint() {
  $('#login-form [name="password"]').required = $('#login-provider').value !== 'emby';
  $('#login-auth-hint').textContent = $('#login-provider').value === 'emby' ? '使用 EMBY 的用户名和密码；登录后自动分配对应权限。' : '本地管理员用于配置连接和维护系统。';
}
function applyRoleInterface() {
  const admin = isAdministrator();
  document.body.dataset.role = admin ? 'admin' : 'user';
  $$('[data-admin-only]').forEach(node => { node.hidden = !admin; });
  const names = admin ? {overview:'概览', all:'全部资源', complete:'已完成'} : {overview:'我的追更', all:'提交记录', complete:'已收齐'};
  Object.entries(names).forEach(([view, label]) => { const node = $(`.nav-item[data-view="${view}"] > span`); if (node) node.textContent = label; });
  $('.nav-caption').textContent = admin ? '媒体库管理' : '我的片单';
  $('.user-row small').textContent = admin ? '管理员 · 全部资源' : '普通用户 · 我的追更';
  $('.page-actions [data-action="add"]').innerHTML = `${icon('plus')}${admin ? '添加资源' : '提交追更'}`;
  $('.page-footer span:last-child').textContent = admin ? 'TMDB 播出信息 · Emby 入库对比 · 站内提醒' : '用 EMBY 账号登录 · 随时查看自己的追更';
  $('#login-form [name="password"]').value = '';
  if (typeof renderServiceConnections === 'function') renderServiceConnections();
}
function stopUserPolling() { clearTimeout(userPollTimer); userPollTimer = null; }
function startUserPolling() { stopUserPolling(); if (isAdministrator() || $('#app').hidden) return; userPollTimer = setTimeout(refreshUserPortal, 30000); }
async function refreshUserPortal() {
  if (isAdministrator() || $('#app').hidden) return;
  try {
    const identity = await api('/api/session');
    if (!identity.authenticated) { showLogin(); configureLogin(identity); return; }
    applyIdentity(identity); state.username = identity.username;
    if (isAdministrator()) { await showApp(); return; }
    state.tmdbConfigured = !!identity.tmdb_configured;
    connection.config = {tmdb_configured: state.tmdbConfigured};
    applyRoleInterface(); await loadItems(); renderPage();
    const detail = $('#modal').open && $('#modal').dataset.itemId;
    if (detail && itemById(detail)) openUserDetail(detail);
  } catch { /* Keep the last visible progress while a connection is temporarily unavailable. */ }
  finally { startUserPolling(); }
}
function userRequestBucket(item) {
  if (!item.request_active) return 'cancelled';
  return item.emby?.state === 'matched' && item.status === 'complete' ? 'complete' : 'ongoing';
}
function userCollections(items = state.items) {
  const records = [...items].sort((a, b) => (Date.parse(b.requested_at) || 0) - (Date.parse(a.requested_at) || 0));
  return {
    records,
    ongoing: records.filter(item => userRequestBucket(item) === 'ongoing'),
    complete: records.filter(item => userRequestBucket(item) === 'complete'),
    cancelled: records.filter(item => userRequestBucket(item) === 'cancelled')
  };
}
function requestStatus(item) {
  const bucket = userRequestBucket(item);
  if (bucket === 'cancelled') return ['已取消', 'unverified'];
  if (bucket === 'complete') return ['已收齐', 'complete'];
  if (item.tracking === false) return ['更新检查已暂停', 'unverified'];
  if (item.status === 'premiering') return [premiereLabel(item), 'premiering'];
  if (item.status === 'wanted') return ['待找资源', 'wanted'];
  if (!hasVerifiedLibrary(item)) return [item.status === 'upcoming' ? '等待播出' : item.emby?.state === 'error' ? '片库连接异常' : '片库信息待核对', 'unverified'];
  if (item.status === 'upcoming') return ['等待播出', 'upcoming'];
  if (!item.library_count) return ['等待入库', 'pending'];
  if (item.missing_episodes) return ['等待补齐', 'missing'];
  return ['已跟上更新', 'updating'];
}
function requestDate(value) {
  const date = new Date(value);
  return value && !Number.isNaN(date.getTime()) ? new Intl.DateTimeFormat('zh-CN', {timeZone:'Asia/Shanghai',year:'numeric',month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit',hour12:false}).format(date) : '时间待更新';
}
function matchesUserSearch(item) {
  const query = String(state.search || '').trim().toLocaleLowerCase();
  return !query || `${titleOf(item)} ${item.year || ''} ${kindLabels[item.kind] || ''}`.toLocaleLowerCase().includes(query);
}
function renderUserPortal() {
  const collections = userCollections();
  const pages = {
    overview:['YOUR STORIES, ON THE WAY','我的追更','只看正在追的作品，随时查看播出与入库进度。'],
    all:['YOUR REQUEST HISTORY','提交记录','按提交时间查看你的作品记录，包括已收齐和已取消的追更。'],
    complete:['COLLECTED, READY TO WATCH','已收齐','整部电影或整季剧集已经收齐的作品，都在这里。']
  };
  const [eyebrow,title,description] = pages[state.view] || pages.overview;
  $('#page-eyebrow').textContent = eyebrow;
  $('#page-title').innerHTML = `${title}<span class="heading-dot">.</span>`;
  $('#page-description').textContent = description;
  $('#nav-all').textContent = collections.records.length;
  $('#nav-complete').textContent = collections.complete.length;
  if (state.view === 'all') renderUserHistory(collections);
  else renderUserProgress(collections, state.view === 'complete');
}
function userSearchField(placeholder) {
  return `<div class="search-box">${icon('search')}<input id="user-resource-search" type="search" aria-label="搜索我的作品" placeholder="${esc(placeholder)}" value="${esc(state.search)}"></div>`;
}
function userEmptyState(view, filtered) {
  let title = '还没有正在追的作品', description = '搜索想看的电影或剧集，提交后即可在这里查看进度。';
  if (view === 'all') { title = '还没有提交记录'; description = '你提交过的作品会保存在这里，取消后也可以查看。'; }
  else if (view === 'complete') { title = '还没有已收齐的作品'; description = 'EMBY 确认整部或整季收齐后，作品会自动出现在这里。'; }
  if (filtered) { title = '没有符合条件的作品'; description = '换个片名，或清除筛选再看看。'; }
  return `<div class="empty-state"><div class="empty-icon">${icon('film')}</div><h3>${title}</h3><p>${description}</p>${filtered ? '<button class="button secondary" data-user-action="clear-filters">清除筛选</button>' : view !== 'complete' ? `<button class="button primary" data-action="add">${icon('search')}搜索并提交追更</button>` : '<button class="button secondary" data-view="overview">查看正在追更</button>'}</div>`;
}
function renderUserProgress(collections, collected) {
  const entries = collected ? collections.complete : collections.ongoing;
  const visible = entries.filter(matchesUserSearch);
  const needsCheck = collections.ongoing.filter(item => !hasVerifiedLibrary(item)).length;
  const missing = collections.ongoing.filter(item => hasVerifiedLibrary(item) && !!item.missing_episodes).length;
  const stats = [
    ['正在追更',collections.ongoing.length,'refresh','尚未收齐的有效追更'],
    ['等待补齐',missing,'download','已核对片库，仍有集数待补'],
    ['待核对片库',needsCheck,'clock','等待确认 EMBY 对应作品'],
    ['已经收齐',collections.complete.length,'circle-check','整部或整季已经收齐']
  ];
  const summary = collected ? '' : `<div class="user-overview-stats">${stats.map(([label,count,symbol,note]) => `<div class="user-overview-stat"><span>${icon(symbol)}${label}</span><strong>${count}</strong><small>${note}</small></div>`).join('')}</div>`;
  $('#page-content').innerHTML = `${summary}<section class="panel user-requests"><div class="panel-heading"><div><h2>${collected ? '已收齐的片单' : '正在追的作品'} <span class="tiny-pill">${visible.length} 部</span></h2><p>${collected ? '已经通过 EMBY 确认整部或整季收齐。' : '已收齐和已取消的作品保留在提交记录中。'}</p></div><div class="user-panel-actions"><button class="text-link" data-view="all">提交记录 ${icon('arrow')}</button><button class="text-link" data-user-action="refresh">${icon('refresh')}刷新进度</button></div></div><div class="filter-toolbar">${userSearchField(collected ? '搜索已收齐的作品…' : '搜索正在追的作品…')}</div><div class="user-request-grid">${visible.map(userCard).join('') || userEmptyState(collected ? 'complete' : 'overview', !!state.search.trim())}</div></section>`;
}
function renderUserHistory(collections) {
  const options = [['','全部记录',collections.records.length],['ongoing','追更中',collections.ongoing.length],['complete','已收齐',collections.complete.length],['cancelled','已取消',collections.cancelled.length]];
  const entries = collections.records.filter(item => (!state.status || userRequestBucket(item) === state.status) && matchesUserSearch(item));
  const filtered = !!(state.search.trim() || state.status);
  $('#page-content').innerHTML = `<section class="panel user-history"><div class="panel-heading"><div><h2>我的提交记录 <span class="tiny-pill">${collections.records.length} 部</span></h2><p>每部作品保留一条记录，按最近一次提交时间排序。</p></div><button class="text-link" data-user-action="refresh">${icon('refresh')}刷新记录</button></div><div class="user-history-toolbar">${userSearchField('搜索提交过的作品…')}<label class="user-history-filter">状态<select id="user-history-filter" aria-label="筛选提交状态">${options.map(([value,label,count]) => `<option value="${value}"${state.status === value ? ' selected' : ''}>${label} · ${count}</option>`).join('')}</select></label></div>${entries.length ? `<div class="user-history-scroll"><table class="user-history-table"><thead><tr><th scope="col">作品</th><th scope="col">提交时间</th><th scope="col">当前入库</th><th scope="col">追更状态</th><th scope="col"><span class="history-action-label">操作</span></th></tr></thead><tbody>${entries.map(userHistoryRow).join('')}</tbody></table></div><div class="user-history-foot">显示 ${entries.length} / ${collections.records.length} 部作品</div>` : userEmptyState('all', filtered)}</section>`;
}
function userHistoryRow(item) {
  const [label,status] = requestStatus(item), verified = hasVerifiedLibrary(item);
  const unit = item.kind === 'movie' ? '部' : '集';
  const progress = verified ? `${item.library_count || 0} ${unit}` : '待核对';
  return `<tr><td><div class="user-history-title">${poster(item.tmdb?.poster_path, '')}<div><strong>${esc(titleOf(item))}</strong><small>${esc(kindLabels[item.kind] || '')} · ${esc(item.year || '年份待定')}</small></div></div></td><td><time datetime="${esc(item.requested_at)}">${esc(requestDate(item.requested_at))}</time>${!item.request_active && item.withdrawn_at ? `<small>取消于 ${esc(requestDate(item.withdrawn_at))}</small>` : ''}</td><td><strong class="user-history-progress">${esc(progress)}</strong><small>已播 ${item.aired_count || 0} ${unit}</small>${item.airing_today_count ? `<small>${esc(todayScheduleSummary(item))}</small>` : ''}</td><td><span class="status status-${status}">${label}</span></td><td><button class="text-link" data-user-action="detail" data-id="${esc(item.id)}">查看 ${icon('arrow')}</button></td></tr>`;
}
function userCard(item) {
  const [label, status] = requestStatus(item), verified = hasVerifiedLibrary(item);
  return `<article class="user-request-card">${poster(item.tmdb?.poster_path, item.title)}<div class="user-request-copy"><div class="user-request-top"><h3>${esc(titleOf(item))}</h3><span class="status status-${status}">${label}</span></div><p class="muted">${esc(kindLabels[item.kind] || '')} · ${esc(item.year || '年份待定')} · ${esc(stamp(item.requested_at))} 提交</p><div class="user-progress"><span>已播 <b>${item.aired_count || 0}</b></span><span>入库 <b>${verified ? item.library_count : '待核对'}</b></span>${item.airing_today_count ? `<span>今日排期 <b>${item.airing_today_count}</b></span>` : ''}${item.not_aired_count ? `<span>后续 / 待定 <b>${item.not_aired_count}</b></span>` : ''}</div><div class="user-card-bottom"><span>${!item.request_active ? '已取消，可重新提交' : !item.tracking ? '管理员已暂停更新检查' : item.status === 'wanted' ? '等待管理员绑定资源来源' : verified && item.missing_episodes ? `待补集数：${esc(item.missing_episodes)}` : '后台持续更新入库情况'}</span><button class="text-link" data-user-action="detail" data-id="${esc(item.id)}">查看进度 ${icon('arrow')}</button></div></div></article>`;
}
function openUserDetail(id) {
  const item = itemById(id); if (!item) return;
  const [label, status] = requestStatus(item), verified = hasVerifiedLibrary(item);
  openModal(`${modalHeader('YOUR REQUEST, IN PROGRESS', titleOf(item), `${kindLabels[item.kind] || ''} · ${item.year || '年份待定'}`)}<div class="modal-body"><div class="tmdb-confirm-hero">${poster(item.tmdb?.poster_path, item.title, 'large')}<div><span class="status status-${status}">${label}</span><p class="user-detail-overview">${esc(item.tmdb?.overview || '作品信息正在整理。')}</p><p class="form-hint">提交时间：${esc(stamp(item.requested_at))}</p></div></div><div class="comparison-values"><div><small>TMDB 已播 · 按排期</small><strong>${item.aired_count || 0}<em>集 / 部</em></strong><span>${esc(item.aired_episodes || '播出信息待更新')}</span></div><div><small>EMBY 已入库</small><strong>${verified ? item.library_count : '待核对'}</strong><span>${verified ? esc(item.library_episodes || '暂无已编号集数') : '管理员正在核对片库对应作品'}</span></div><div><small>后续 / 日期待定</small><strong>${item.not_aired_count || 0}</strong></div></div>${airingScheduleNotice(item)}${item.status === 'wanted' ? '<div class="view-banner">等待管理员选择并绑定资源来源。</div>' : verified && item.missing_episodes ? `<div class="view-banner">${icon('refresh')}<span>等待补齐：${esc(item.missing_episodes)}。进度会在后台同步后更新。</span></div>` : ''}<p class="form-hint">最近比对：${esc(stamp(item.emby?.checked_at))}。${item.request_active ? '你可以随时取消自己的追更。' : '这条追更已取消，可重新提交。'}</p></div><footer class="modal-footer"><button class="button secondary" data-action="close-modal">关闭</button>${item.request_active ? `<button class="button subtle" data-user-action="withdraw-confirm" data-id="${esc(id)}">取消我的追更</button>` : `<button class="button primary" data-user-action="resubmit" data-id="${esc(id)}">重新提交追更</button>`}</footer>`, '780px');
  $('#modal').dataset.itemId = id;
}
function requesterSummary(item) {
  if (!isAdministrator()) return '';
  const names = (item.requesters || []).filter(entry => entry.active).map(entry => entry.username);
  return names.length ? `<div class="requester-summary">${icon('bookmark')}提交：${esc(names.join('、'))}</div>` : '';
}
function requesterPanel(item) {
  if (!isAdministrator()) return '';
  const requesters = item.requesters || [];
  return `<section class="form-section"><h3 class="section-label">提交用户 <small>${requesters.filter(entry => entry.active).length} 人正在追更</small></h3>${requesters.length ? `<div class="requester-list">${requesters.map(entry => `<div><span class="requester-avatar">${esc(entry.username.slice(0, 1) || '用')}</span><div><strong>${esc(entry.username)}</strong><small>${entry.provider === 'emby' ? 'EMBY 账号' : '本地管理员'} · ${esc(stamp(entry.submitted_at))} 提交</small></div><span class="status status-${entry.active ? 'updating' : 'unverified'}">${entry.active ? '追更中' : '已取消'}</span></div>`).join('')}</div>` : '<p class="form-hint">历史记录尚未记录提交用户。</p>'}</section>`;
}
function adaptAdminSettings() {
  const form = $('#password-form');
  if (form && state.provider !== 'local') form.closest('.settings-group').hidden = true;
  $('.modal-body').insertAdjacentHTML('afterbegin', '<div class="view-banner"><span>账号与权限继承 EMBY：EMBY 管理员进入管理界面，其他账号进入个人追更页面。EMBY 密码在 EMBY 中修改。</span></div>');
}
const adminUsers = {rows:null, loading:false, error:'', query:'', selected:'all', generation:0};
function resetAdminUsers() {
  adminUsers.rows = null; adminUsers.loading = false; adminUsers.error = ''; adminUsers.query = ''; adminUsers.selected = 'all'; adminUsers.generation++;
}
function adminAccountKey(user) { return `emby:${user.server_id}:${user.id}`; }
function adminSubmissionRows(selected = adminUsers.selected) {
  const rows = [];
  for (const item of state.items) for (const entry of item.requesters || []) {
    if (selected === 'all' || entry.user_id === selected) rows.push({item,entry});
  }
  return rows.sort((a,b) => (Date.parse(b.entry.submitted_at) || 0) - (Date.parse(a.entry.submitted_at) || 0));
}
function renderAdminUsersPage() {
  if (!isAdministrator()) return;
  $('#page-content').innerHTML = `<section class="panel users-panel" aria-label="用户与提交"><div class="users-toolbar"><div><h2>账号与追更提交</h2><p>账号、权限及登录验证由 Emby 管理。</p></div><button class="button secondary small" data-admin-users="refresh"${adminUsers.loading ? ' disabled' : ''}>${icon('refresh')}刷新</button></div><div id="users-content" class="users-content"></div></section>`;
  renderAdminUsersContent();
  if (!adminUsers.rows && !adminUsers.loading && !adminUsers.error) loadAdminUsers();
}
function renderAdminUsersContent() {
  if (!isAdministrator() || state.view !== 'users' || !$('#users-content')) return;
  const root = $('#users-content');
  if (adminUsers.loading && !adminUsers.rows) { root.innerHTML = '<div class="search-loading"><span class="spinner"></span><p>正在读取 Emby 用户与提交记录…</p></div>'; return; }
  if (adminUsers.error && !adminUsers.rows) { root.innerHTML = `<div class="users-load-error"><p class="form-error" role="alert">${esc(adminUsers.error)}</p><button class="button secondary" data-admin-users="refresh">${icon('refresh')}重新读取</button></div>`; return; }
  const users = adminUsers.rows || [], selected = users.find(user => adminAccountKey(user) === adminUsers.selected);
  if (adminUsers.selected !== 'all' && !selected) adminUsers.selected = 'all';
  const entries = adminSubmissionRows();
  const active = entries.filter(row => row.entry.active !== false).length;
  root.innerHTML = `<div class="users-workspace"><aside class="users-accounts" aria-label="Emby 账号"><div class="users-account-heading"><h3>Emby 用户 <span>${users.length}</span></h3><div class="search-box">${icon('search')}<input id="admin-user-search" type="search" value="${esc(adminUsers.query)}" placeholder="搜索用户名" aria-label="搜索 Emby 用户"></div></div><div id="users-account-list" class="users-account-list" role="region" aria-label="用户列表" tabindex="0"></div></aside><section class="users-submissions" aria-labelledby="users-submissions-title"><div class="users-submission-heading"><div><h3 id="users-submissions-title">${selected ? esc(selected.username) + ' 的提交' : '全部提交'}</h3><p>${entries.length} 条提交 · ${active} 条保留追更${selected ? ` · ${selected.is_admin ? '管理员' : '普通用户'}` : ' · 包含本地管理员记录'}</p></div>${selected ? `<span class="status status-${selected.disabled ? 'broken' : 'updating'}">${selected.disabled ? '账号已禁用' : '账号正常'}</span>` : ''}</div><div class="users-request-scroll" role="region" aria-label="追更提交记录" tabindex="0">${entries.length ? `<div class="users-request-list">${entries.map(({item,entry}) => {
    const user = users.find(user => adminAccountKey(user) === entry.user_id);
    const label = entry.active === false ? '已取消' : item.status === 'complete' ? '已收齐' : item.tracking === false ? '检查已暂停' : '追更中';
    return `<article class="users-request-row">${cover(item)}<div class="users-request-copy"><button class="admin-activity-title" data-action="detail" data-id="${esc(item.id)}">${esc(titleOf(item))}${icon('arrow')}</button><p>${esc(user?.username || entry.username)} · ${entry.provider === 'emby' ? 'Emby 用户' : '本地管理员'} · ${esc(stamp(entry.submitted_at))} 提交</p><small>${hasVerifiedLibrary(item) ? `已入库 ${item.library_count || 0} ${item.kind === 'movie' ? '部' : '集'}` : '入库进度待核对'}</small></div><span class="status status-${entry.active === false ? 'unverified' : item.status === 'complete' ? 'complete' : 'updating'}">${label}</span></article>`;
  }).join('')}</div>` : emptyState('还没有提交记录', selected ? '这个账号尚未提交追更。' : '用户提交追更后，会显示在这里。', true)}</div><div class="users-module-foot">在 Emby 中管理账号与权限；点击作品查看来源和入库详情。</div></section></div>${adminUsers.error ? `<p class="users-refresh-error form-error" role="alert">${esc(adminUsers.error)} · 当前保留上次读取结果</p>` : ''}`;
  renderAdminUserAccounts();
}
function renderAdminUserAccounts() {
  const root = $('#users-account-list'); if (!root || !isAdministrator() || state.view !== 'users') return;
  const users = (adminUsers.rows || []).filter(user => user.username.toLocaleLowerCase().includes(adminUsers.query.trim().toLocaleLowerCase()));
  root.innerHTML = `<button class="users-account-button${adminUsers.selected === 'all' ? ' selected' : ''}" data-admin-users="select" data-user-id="all" aria-pressed="${adminUsers.selected === 'all'}"><span class="requester-avatar">${icon('bookmark')}</span><span class="users-account-copy"><strong>全部提交</strong><small>查看所有用户的追更记录</small></span></button>${users.map(user => {
    const key = adminAccountKey(user);
    return `<button class="users-account-button${adminUsers.selected === key ? ' selected' : ''}" data-admin-users="select" data-user-id="${esc(key)}" aria-pressed="${adminUsers.selected === key}"><span class="requester-avatar">${esc(user.username.slice(0,1) || '用')}</span><span class="users-account-copy"><strong>${esc(user.username)}</strong><small>${user.is_admin ? '管理员' : '普通用户'} · ${user.requests || 0} 部追更${user.disabled ? ' · 已禁用' : ''}</small></span>${icon('arrow')}</button>`;
  }).join('')}${!users.length ? '<p class="users-search-empty">没有符合条件的 Emby 用户。</p>' : ''}`;
}
async function loadAdminUsers() {
  if (!isAdministrator() || state.view !== 'users' || adminUsers.loading) return;
  const generation = ++adminUsers.generation, csrf = state.csrf;
  adminUsers.loading = true; adminUsers.error = '';
  renderAdminUsersPage();
  try {
    const [data, records] = await Promise.all([api('/api/users'), api('/api/items')]);
    if (generation !== adminUsers.generation || !isAdministrator() || csrf !== state.csrf) return;
    if (!Array.isArray(data.users) || !Array.isArray(records.items)) throw new Error('用户或提交数据不完整，请重新读取。');
    adminUsers.rows = data.users;
    state.items = records.items; updateCounts();
  } catch (error) {
    if (generation === adminUsers.generation && isAdministrator() && csrf === state.csrf) adminUsers.error = error.message;
  } finally {
    if (generation === adminUsers.generation) {
      adminUsers.loading = false;
      if (isAdministrator() && state.view === 'users' && csrf === state.csrf) renderAdminUsersPage();
    }
  }
}
async function openUsers() { if (isAdministrator()) await navigate('users'); }
document.addEventListener('input', event => { if (event.target.id === 'admin-user-search' && isAdministrator()) { adminUsers.query = event.target.value; renderAdminUserAccounts(); } });
document.addEventListener('click', event => {
  const button = event.target.closest('[data-admin-users]'); if (!button || !isAdministrator() || state.view !== 'users') return;
  if (button.dataset.adminUsers === 'refresh') loadAdminUsers();
  else if (button.dataset.adminUsers === 'select') { adminUsers.selected = button.dataset.userId; renderAdminUsersContent(); }
});

document.addEventListener('change', event => { if (event.target.id === 'login-provider') updateLoginHint(); else if (event.target.id === 'user-history-filter') { state.status = event.target.value; renderUserPortal(); $('#user-history-filter').focus(); } });
document.addEventListener('input', event => { if (event.target.id === 'user-resource-search') { const cursor = event.target.selectionStart; state.search = event.target.value; renderUserPortal(); const input = $('#user-resource-search'); input.focus(); input.setSelectionRange(cursor, cursor); } });
document.addEventListener('click', async event => {
  const button = event.target.closest('[data-user-action]'); if (!button) return;
  const action = button.dataset.userAction, id = button.dataset.id, item = id && itemById(id);
  try {
    if (action === 'detail') openUserDetail(id);
    else if (action === 'clear-filters') { state.search = ''; state.status = ''; renderUserPortal(); }
    else if (action === 'refresh') { button.disabled = true; await loadItems(); renderPage(); toast('进度已刷新。'); }
    else if (action === 'withdraw-confirm') openModal(`${modalHeader('YOUR CHOICE', '取消这部作品的追更？')}<div class="modal-body"><p>取消后可以重新提交。其他用户对这部作品的追更会继续保留。</p></div><footer class="modal-footer"><button class="button secondary" data-user-action="detail" data-id="${esc(id)}">返回</button><button class="button danger" data-user-action="withdraw" data-id="${esc(id)}">确认取消</button></footer>`, '480px');
    else if (action === 'withdraw') { button.disabled = true; await api(`/api/items/${encodeURIComponent(id)}/withdraw`, {method:'POST',body:{}}); await loadItems(); renderPage(); openUserDetail(id); toast('已取消你的追更。'); }
    else if (action === 'resubmit' && item?.tmdb) { button.disabled = true; await api('/api/tmdb/track', {method:'POST',body:{tmdb_id:item.tmdb.id,media_type:item.tmdb.media_type,season:item.season}}); await loadItems(); renderPage(); openUserDetail(id); toast('已重新提交追更。'); }
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; }
});
