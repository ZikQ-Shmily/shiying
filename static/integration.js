'use strict';

const connection = { config: null, health: null, healthError: '', healthRequest: 0, timer: null, polling: false, pending: false, generation: 0, search: { query: '', page: 1, totalPages: 1, results: [] }, selected: null, request: 0 };

function embyStateLabel(item) {
  if (!item.tmdb) return '手动记录';
  const status = item.emby?.state || 'never';
  return { never: '尚未比对 Emby', matched: '已匹配 Emby', absent: '尚未入库', unmatched: '刮削信息待核对', error: 'Emby 比对失败' }[status] || 'Emby 待核对';
}
function hasVerifiedLibrary(item) { return !item.tmdb || ['matched', 'absent'].includes(item.emby?.state); }
function premiereLabel(item) { return item.kind === 'movie' ? '今日上映' : '今日开播'; }

function todayScheduleSummary(item) {
  if (!item.airing_today_count) return '';
  return `今日排期 ${item.airing_today_count} ${item.kind === 'movie' ? '部' : '集'}`;
}
function airingScheduleNotice(item) {
  if (!item.tmdb) return '';
  const summary = todayScheduleSummary(item);
  return `<div class="airing-schedule-note">${summary ? `<p>${icon('clock')}<strong>${esc(summary)}</strong><span>${item.kind === 'movie' ? '今日上映排期' : `第 ${esc(item.airing_today_episodes)} 集`}</span></p>` : ''}<small>已播按北京时间昨日及以前的 TMDB 排期估算。当天排期单独显示，不直接计入缺集；实际入库以 Emby 为准。</small></div>`;
}

function airedCount(item) { return Number.isFinite(item.aired_count) ? item.aired_count : episodeCount(item.aired_episodes); }
function poster(path, title, extraClass = '') { return `<div class="tmdb-poster ${esc(extraClass)}">${icon('film')}${path ? `<img src="/api/tmdb/poster?path=${encodeURIComponent(path)}" alt="${esc(title || '')}" loading="lazy">` : ''}</div>`; }
function stamp(value) { if (!value) return '尚未同步'; const date = new Date(value); if (Number.isNaN(date.getTime())) return '—'; return new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false }).format(date); }

function stopIntegrationPolling() { connection.generation += 1; clearTimeout(connection.timer); connection.timer = null; connection.pending = false; }
async function initializeIntegrations() { stopIntegrationPolling(); if (!isAdministrator()) { connection.config = { tmdb_configured: state.tmdbConfigured }; startUserPolling(); return; } const generation = connection.generation; try { await refreshIntegrationStatus(false); } catch (error) { connection.healthError = error.message; renderServiceConnections(); toast(`连接状态读取失败：${error.message}`, true); } if (generation === connection.generation && !$('#app').hidden) scheduleIntegrationPoll(); }
function scheduleIntegrationPoll() { clearTimeout(connection.timer); if (!isAdministrator()) return; if ($('#app').hidden) return; connection.timer = setTimeout(pollIntegrations, connection.config?.sync_running || connection.config?.source_scan_running || connection.pending || connection.health?.checking ? 2000 : 60000); }
async function pollIntegrations() { if ($('#app').hidden || connection.polling) return; const generation = connection.generation; connection.polling = true; try { const identity = await api('/api/session'); if (!identity.authenticated) { showLogin(); configureLogin(identity); return; } if (identity.role !== state.role) { applyIdentity(identity); state.username = identity.username; await showApp(); return; } await refreshIntegrationStatus(true); } catch (error) { connection.healthError = error.message; renderServiceConnections(); if (connection.pending) toast(error.message, true); } finally { connection.polling = false; if (generation === connection.generation) scheduleIntegrationPoll(); } }
async function refreshIntegrationStatus(reloadOnFinish = true) { if (!isAdministrator()) { connection.config = { tmdb_configured: state.tmdbConfigured }; return connection.config; }
  const previous = connection.config; const config = await api('/api/integrations'); connection.config = config; updateSyncLabels(); await refreshServiceConnections();
  const finished = !config.sync_running && !config.source_scan_running && (previous?.sync_running || previous?.source_scan_running || connection.pending || (previous?.last_sync && config.last_sync !== previous.last_sync) || (config.source_scan_at && config.source_scan_at !== previous?.source_scan_at));
  if (finished && reloadOnFinish) {
    connection.pending = false; const detailID = $('#modal').open ? $('#modal').dataset.itemId : null;
    await loadItems(); renderPage(); if (detailID && itemById(detailID)) openDetail(detailID);
    toast(config.last_error ? `同步已结束：${config.last_error}` : '同步完成，已刷新来源集数、播出与入库进度。', !!config.last_error);
  }
  return config;
}
function updateSyncLabels() {
  const config = connection.config; const running = config?.sync_running || config?.source_scan_running || connection.pending;
  const label = $('#global-sync-label'); if (label) label.textContent = running ? '正在同步…' : '同步片库';
  $$('[data-integration="sync-all"]').forEach(button => { button.disabled = !!running; button.classList.toggle('is-syncing', !!running); });
  const summary = $('#integration-sync-summary'); if (summary) summary.textContent = running ? '正在更新网盘来源、TMDB 播出与 Emby 入库进度…' : `上次同步：${stamp(config?.last_sync)}${config?.last_error ? ` · ${config.last_error}` : ''}`;
}

function renderServiceConnections() {
  const list = $('#connection-services');
  if (!list) return;
  if (!isAdministrator()) { list.innerHTML = ''; return; }
  if (!connection.config && !connection.health) { list.innerHTML = '<p class="connection-loading">正在读取连接信息…</p>'; return; }
  const config = connection.config || {};
  let services = connection.health?.services || [
    {id:'emby',name:'Emby',address:config.emby_url || '',configured:config.emby_configured,route:'拾影 → Emby'},
    {id:'tmdb',name:'TMDB',address:'https://api.themoviedb.org/3',configured:config.tmdb_configured,route:'拾影 → TMDB'},
    {id:'re0',name:'RE0 查询端',address:config.re0_gateway_url || '',configured:config.re0_configured,route:'拾影 → 已授权 CMS → RE0'},
    {id:'cms',name:'CMS 执行端',address:config.cms_url || '',configured:config.cms_configured,route:'拾影 → 我的 CMS → 115'}
  ];
  if (config.re0_via_cms) services = services.filter(service => service.id !== 're0').map(service => service.id === 'cms' ? {...service,name:'CloudMediaSync',route:'拾影 → 我的 CMS → RE0 / 115'} : service);
  const states = {connected:['已连接','connected'],error:['连接异常','error'],unconfigured:['未配置','muted'],unknown:['待检测','muted'],checking:['检测中','checking']};
  list.innerHTML = services.map(service => {
    const status = connection.healthError ? 'unknown' : connection.health?.checking && service.configured ? 'checking' : service.state || (service.configured ? 'unknown' : 'unconfigured');
    const [label,tone] = states[status] || states.unknown;
    return `<article class="service-connection service-${esc(service.id)}"><div class="service-title"><strong>${esc(service.name)}</strong><span class="connection-badge connection-${tone}"><i></i>${label}</span></div><p class="service-route">${esc(service.route)}</p>${status === 'error' ? '<p class="service-error">连接失败，请在设置中检查</p>' : ''}<p class="service-checked">${service.checked_at ? `检查于 ${esc(stamp(service.checked_at))}` : service.configured ? '等待 API 可用性检测' : '在设置中配置连接'}</p></article>`;
  }).join('');
  const note = $('#connection-status-note');
  if (note) note.textContent = connection.healthError ? '状态读取失败，稍后重试' : connection.health?.checking ? '正在检测 API 可用性…' : `每分钟检测${services.find(service => service.checked_at)?.checked_at ? ` · ${stamp(services.find(service => service.checked_at).checked_at)}` : ''}`;
  $$('[data-integration="check-connections"]').forEach(button => { button.disabled = !!connection.health?.checking; });
}
async function refreshServiceConnections(force = false) {
  if (!isAdministrator() || $('#app').hidden) return;
  const request = ++connection.healthRequest, generation = connection.generation;
  try {
    const health = await api(force ? '/api/connections/check' : '/api/connections', force ? {method:'POST',body:{}} : {});
    if (request !== connection.healthRequest || generation !== connection.generation || !isAdministrator()) return;
    connection.health = health; connection.healthError = '';
  } catch (error) {
    if (request !== connection.healthRequest || generation !== connection.generation || !isAdministrator()) return;
    connection.healthError = error.message;
  }
  renderServiceConnections();
}

async function startSync(id = null, notify = true) {
  if (!connection.config) await refreshIntegrationStatus(false);
  if (!connection.config.tmdb_configured && !connection.config.emby_configured) { if (notify) { toast('先在设置中填写 TMDB 与 Emby 连接信息。'); openSettings(); } return; }
  if (!isAdministrator()) return;
  const result = await api(id ? `/api/items/${encodeURIComponent(id)}/sync` : '/api/sync', { method: 'POST', body: {} });
  connection.pending = true; if (result.running || result.started) connection.config.sync_running = true; updateSyncLabels(); scheduleIntegrationPoll();
  if (notify) toast(result.started ? '已开始同步，结果会自动刷新。' : '已有同步正在进行，完成后会自动刷新。');
}

async function openTmdbSearch(query = '') {
  connection.request += 1; connection.search = { query, page: 1, totalPages: 1, results: [] }; connection.selected = null;
  if (!connection.config) { try { await refreshIntegrationStatus(false); } catch (error) { toast(error.message, true); } }
  const configured = !!connection.config?.tmdb_configured;
  openModal(`${modalHeader('FIND YOUR NEXT STORY', '先找到这部作品', '搜索 TMDB，确认作品和季，再让拾影持续对比入库进度。')}<div class="modal-body tmdb-search-body">${!configured ? `<div class="view-banner">${icon('info')}<span>${isAdministrator() ? '还没有配置 TMDB 凭据，请在设置中填写。' : '管理员尚未配置搜索服务，请联系管理员。'}</span>${isAdministrator() ? `<button class="text-link" data-action="settings">前往设置 ${icon('arrow')}</button>` : ''}</div>` : ''}<form id="tmdb-search-form" class="tmdb-search-form"><div class="search-box">${icon('search')}<input id="tmdb-query" name="query" type="search" required maxlength="200" placeholder="输入中文片名，也可以搜索原名…" value="${esc(query)}" autocomplete="off"${!configured ? ' disabled' : ''}></div><button class="button primary" type="submit"${!configured ? ' disabled' : ''}>${icon('search')}搜索作品</button></form><div class="tmdb-search-caption"><span>作品信息来自 TMDB · 电影 / 剧集</span>${isAdministrator() ? `<button class="text-link" data-integration="manual">找不到作品？手动录入 ${icon('arrow')}</button>` : ''}</div><p id="tmdb-search-error" class="form-error" role="alert"></p><div id="tmdb-results">${emptyState(configured ? '找到作品，再开始追更' : '配置好连接，就能开始搜索', configured ? '搜索你想收的剧或电影，确认同名作品和年份。' : (isAdministrator() ? '在设置中保存 TMDB 凭据，即可搜索作品。' : '管理员配置搜索服务后，即可提交作品。'), true)}</div></div><footer class="modal-footer">${isAdministrator() ? '<button class="button secondary left-action" data-integration="manual">手动录入</button>' : ''}<button class="button secondary" data-action="close-modal">关闭</button></footer>`, '860px');
  if (configured) { $('#tmdb-query').focus(); if (query) searchTmdb(query, 1); }
}
async function searchTmdb(query, page = 1) {
  const trimmed = query.trim(); if (!trimmed) return;
  if (!connection.config?.tmdb_configured) { $('#tmdb-search-error').textContent = isAdministrator() ? '请先在设置中配置 TMDB 凭据。' : '请联系管理员配置搜索服务。'; return; }
  const request = ++connection.request; connection.search.query = trimmed; $('#tmdb-search-error').textContent = '';
  const results = $('#tmdb-results'); if (!results) return;
  results.innerHTML = `<div class="search-loading"><span class="spinner"></span><p>正在查找「${esc(trimmed)}」…</p></div>`;
  const button = $('#tmdb-search-form button'); if (button) button.disabled = true;
  try {
    const data = await api(`/api/tmdb/search?q=${encodeURIComponent(trimmed)}&page=${page}`);
    if (request !== connection.request || !$('#tmdb-results')) return;
    connection.search = { query: trimmed, page: data.page || page, totalPages: data.total_pages || 1, results: data.results || [] };
    renderTmdbResults();
  } catch (error) { if (request === connection.request && $('#tmdb-search-error')) { $('#tmdb-search-error').textContent = error.message; $('#tmdb-results').innerHTML = emptyState('搜索暂时没有完成', isAdministrator() ? '检查连接设置后重试，或选择手动录入。' : '稍后重试，或联系管理员检查搜索服务。', true); } }
  finally { if (request === connection.request && $('#tmdb-search-form button')) $('#tmdb-search-form button').disabled = false; }
}
function renderTmdbResults() {
  const results = connection.search.results; const node = $('#tmdb-results'); if (!node) return;
  if (!results.length) { node.innerHTML = emptyState('还没找到匹配的作品', isAdministrator() ? '试试作品原名、去掉季数，或使用手动录入。' : '试试作品原名，或去掉季数重新搜索。', true); return; }
  node.innerHTML = `<div class="tmdb-result-list">${results.map(result => `<button class="tmdb-result" data-integration="select-title" data-tmdb-id="${esc(result.tmdb_id)}" data-media-type="${esc(result.media_type)}">${poster(result.poster_path, '')}<div class="tmdb-result-copy"><div class="tmdb-result-title"><h3>${esc(result.title)}</h3><span>${result.media_type === 'movie' ? '电影' : '剧集'}</span></div><p class="tmdb-result-meta">${esc(result.year || '年份待定')}${result.original_title && result.original_title !== result.title ? ` · ${esc(result.original_title)}` : ''}</p><p class="tmdb-result-overview">${esc(result.overview || '暂无简介，点击确认作品详情。')}</p></div><span class="result-chevron">${icon('arrow')}</span></button>`).join('')}</div><div class="tmdb-pagination"><button class="button secondary small" data-integration="search-page" data-page="${connection.search.page - 1}"${connection.search.page <= 1 ? ' disabled' : ''}>上一页</button><span>第 ${connection.search.page} / ${connection.search.totalPages} 页</span><button class="button secondary small" data-integration="search-page" data-page="${connection.search.page + 1}"${connection.search.page >= connection.search.totalPages ? ' disabled' : ''}>下一页</button></div>`;
}
function seasonChoices(seasons) {
  const options = seasons.length ? seasons : [{season_number:1,name:'第 1 季',episode_count:0}];
  return `<fieldset class="season-picker"><legend>${options.length === 1 ? '追踪这一季' : '选择追踪的季'}</legend><div class="season-choices">${options.map((season,index) => `<label class="season-choice"><input type="radio" name="season" value="${esc(season.season_number)}" required${index === 0 ? ' checked' : ''}><span class="season-choice-surface"><span class="season-choice-top"><strong>${esc(season.name || `第 ${season.season_number} 季`)}</strong><span class="season-choice-check">${icon('check')}</span></span><span class="season-choice-meta">${season.episode_count ? `${esc(season.episode_count)} 集` : '集数待更新'}<span>·</span>${season.air_date ? `${esc(season.air_date)} 首播` : '首播日期待定'}</span></span></label>`).join('')}</div><p class="season-choice-hint">${options.length === 1 ? '已选中，可直接确认追更。' : '每季独立追踪，选择后确认加入追更。'}</p></fieldset>`;
}
async function confirmTmdbTitle(mediaType, id, button) {
  button.disabled = true;
  try {
    const detail = await api(`/api/tmdb/details/${encodeURIComponent(mediaType)}/${encodeURIComponent(id)}`); connection.selected = detail;
    const seasons = (detail.seasons || []).filter(season => season.season_number > 0); const movie = detail.media_type === 'movie';
    openModal(`${modalHeader('MAKE SURE IT IS THE ONE', '确认作品，开始追更', isAdministrator() ? '每一季独立追踪，入库时仍可保留你习惯的资源来源。' : '确认年份与季数，提交后即可查看入库进度。')}<form id="tmdb-track-form"><div class="modal-body"><div class="tmdb-confirm-hero">${poster(detail.poster_path, detail.title, 'large')}<div><span class="eyebrow">${movie ? 'MOVIE' : 'TV SERIES'} · TMDB ${esc(detail.tmdb_id)}</span><h3>${esc(detail.title)}</h3><p class="tmdb-confirm-meta">${esc(detail.year || '年份待定')}${detail.original_title && detail.original_title !== detail.title ? ` · ${esc(detail.original_title)}` : ''}${movie && detail.release_date ? ` · ${esc(detail.release_date)} 上映` : ''}</p><p class="tmdb-confirm-overview">${esc(detail.overview || '暂无简介。')}</p></div></div><div class="form-grid tmdb-track-options">${!movie ? seasonChoices(seasons) : '<input type="hidden" name="season" value="1">'}</div><div class="view-banner tmdb-track-explanation">${icon('refresh')}<span>加入后按 TMDB 排期估算已播集数，并对比 Emby 实有集数。首播当天显示“今日开播”，当天排期单独列出。片库中没有的作品显示等待入库，连接失败和刮削不一致会分别提示。</span></div>${isAdministrator() && !connection.config?.emby_configured ? `<p class="form-hint">Emby 尚未配置。可以先加入追更，稍后在设置中填写连接信息。</p>` : ''}<p id="tmdb-track-error" class="form-error" role="alert"></p></div><footer class="modal-footer"><button type="button" class="button secondary left-action" data-integration="back-search">重新搜索</button><button class="button primary" type="submit">${icon('plus')}确认，加入追更</button></footer></form>`, '800px');
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; }
}
async function trackTmdb(event) {
  event.preventDefault(); const detail = connection.selected; if (!detail) return; const form = event.target; const data = new FormData(form); const button = $('button[type="submit"]', form); button.disabled = true; $('#tmdb-track-error').textContent = '';
  try {
    const result = await api('/api/tmdb/track', { method: 'POST', body: { tmdb_id: detail.tmdb_id, media_type: detail.media_type, season: Number(data.get('season') || 1) } });
    await loadItems(); renderPage(); if (result.item?.id) openDetail(result.item.id); else $('#modal').close();
    toast(result.added_request ? (isAdministrator() ? '已加入追更，提交用户已记录。' : '已提交追更，可以在我的追更中查看进度。') : '你已经追更了这部作品，已打开现有记录。');
    if (isAdministrator() && result.item?.id && connection.config?.emby_configured && !result.already_exists) await startSync(result.item.id, false);
    else scheduleIntegrationPoll();
  } catch (error) { const node = $('#tmdb-track-error'); if (node) node.textContent = error.message; else toast(error.message, true); }
  finally { button.disabled = false; }
}

function integrationSettingsSection() {
  const config = connection.config || {};
  return `<section class="settings-group integration-settings"><div class="settings-title-row"><h3>媒体服务与追更执行</h3><span class="status ${config.auto_sync ? 'status-updating' : 'status-unverified'}">${config.auto_sync ? '自动同步已开启' : '自动同步已暂停'}</span></div><p>先保存凭据，再搜索作品。空密钥表示保留已有配置，服务器不会将密钥返回到网页。</p><form id="integration-form"><div class="form-grid"><label class="span-all">TMDB API Read Access Token / API Key<input name="tmdb_credential" type="password" autocomplete="new-password" maxlength="3000" placeholder="${config.tmdb_configured ? '已配置 · 留空保留现有凭据' : '填写 TMDB 读取令牌或 API Key'}" value=""><small>${config.tmdb_configured ? '✓ 已配置' : '尚未配置'} · 使用你自己的 TMDB 凭据。</small></label><label class="span-all">Emby 地址（HTTP / HTTPS）<input name="emby_url" type="url" maxlength="2000" placeholder="http://服务器IP:8096" value="${esc(config.emby_url || '')}"><small>填写服务器访问地址，API Key 单独填写。</small></label><label class="span-all">Emby API Key<input name="emby_api_key" type="password" autocomplete="new-password" maxlength="1000" placeholder="${config.emby_configured ? '已配置 · 留空保留现有密钥' : '填写 Emby API Key'}" value=""><small>${config.emby_configured ? '✓ 已配置' : '尚未配置'} · 只在服务器后台使用。</small></label>${!config.re0_via_cms ? `<label class="span-all">RE0 查询端 · 已授权 CMS 地址<input name="re0_gateway_url" type="url" maxlength="2000" placeholder="http://已授权CMS地址:9526" value="${esc(config.re0_gateway_url || '')}"><small>借用此实例已有的 RE0 授权，查询端不接收拾影的下载任务。</small></label><label class="span-all">RE0 查询桥接密钥<input name="re0_gateway_token" type="password" autocomplete="new-password" maxlength="4096" placeholder="${config.re0_configured ? '已配置 · 留空保留' : '填写查询桥接密钥'}"></label>` : '<div class="note-block span-all">RE0 查询与追更使用这台 CMS 自身的账号授权，统一由下面的 CMS 连接管理。</div>'}<label class="span-all">${config.re0_via_cms ? 'CloudMediaSync 地址（HTTP / HTTPS）' : 'CMS 执行端 · 我的 CMS 地址（HTTP / HTTPS）'}<input name="cms_url" type="url" maxlength="2000" placeholder="http://cms.example.com:9527" value="${esc(config.cms_url || '')}"><small>${config.re0_via_cms ? '在这台 CMS 内完成 RE0 查询、解锁和订阅，固定发布者后执行转存。RE0 账号授权在 CMS 内管理。' : '在此实例创建订阅、保存固定发布者并执行转存。执行端通过查询端读取 RE0，无需再次授权。'}</small></label><label class="span-all">${config.re0_via_cms ? 'CMS 连接密钥' : 'CMS 执行下发密钥'}<input name="cms_token" type="password" autocomplete="new-password" maxlength="4096" placeholder="${config.cms_configured ? '已配置 · 留空保留' : '填写执行端下发密钥'}"><small>${config.re0_via_cms ? '查询与订阅共用这条 CMS 连接，密钥仅在后台加密保存。' : '两端分别安装对应的拾影适配模块，密钥仅在后台加密保存。'}</small></label><label>自动同步间隔（分钟）<input name="interval_minutes" type="number" min="15" max="1440" required value="${config.interval_minutes || 60}"><small>15 至 1440 分钟。</small></label><label class="checkbox-label integration-auto"><input name="auto_sync" type="checkbox"${config.auto_sync ? ' checked' : ''}>开启后台自动同步</label></div><p id="integration-error" class="form-error" role="alert"></p><div class="settings-actions"><button class="button primary" type="submit">${icon('check')}保存连接设置</button><button class="button secondary" type="button" data-integration="test" data-target="tmdb">测试 TMDB</button><button class="button secondary" type="button" data-integration="test" data-target="emby">测试 Emby</button><button class="button secondary" type="button" data-integration="test" data-target="cms">${config.re0_via_cms ? '测试 CMS' : '测试 CMS 执行端'}</button><button class="button secondary" type="button" data-integration="test" data-target="re0">${config.re0_via_cms ? '测试 RE0 授权' : '测试 RE0 查询端'}</button><button class="button secondary" type="button" data-integration="sync-all">${icon('refresh')}立即同步全部</button></div><p id="integration-test-result" class="integration-test-result" role="status"></p><p id="integration-sync-summary" class="form-hint">上次同步：${esc(stamp(config.last_sync))}${config.last_error ? ` · ${esc(config.last_error)}` : ''}</p></form><div class="integration-boundary">${icon('info')}<span>首次绑定来源后自动识别网盘集数，后续随网站后台同步更新；选择 RE0 来源后自动下发 CMS 订阅，并立即提交一次当前链接转存，固定该条目和发布者；暂停追更、解绑来源或移到回收站会同步暂停这条 CMS 追更。CMS 负责转存与下载，最终入库由 Emby 核对。</span></div><div class="tmdb-attribution"><a href="https://www.themoviedb.org" target="_blank" rel="noopener noreferrer"><img class="tmdb-brand-logo" src="/static/tmdb-logo.svg" alt="The Movie Database" width="100" height="14"><span>TMDB</span></a><p>This product uses the TMDB API but is not endorsed or certified by TMDB.</p></div></section>`;
}
async function saveIntegrationSettings(quiet = false) {
  const form = $('#integration-form'); if (!form) return connection.config; const data = new FormData(form); const url = String(data.get('emby_url') || '').trim();
  if (!form.reportValidity()) throw new Error('请检查连接设置中的填写内容。');
  if (url && !/^https?:\/\//i.test(url)) throw new Error('Emby 地址请以 http:// 或 https:// 开头。');
  const config = await api('/api/integrations', { method: 'PUT', body: { re0_gateway_url: String(data.get('re0_gateway_url') || '').trim(), re0_gateway_token: String(data.get('re0_gateway_token') || '').trim(), cms_url: String(data.get('cms_url') || '').trim(), cms_token: String(data.get('cms_token') || '').trim(), tmdb_credential: String(data.get('tmdb_credential') || '').trim(), emby_url: url, emby_api_key: String(data.get('emby_api_key') || '').trim(), auto_sync: data.has('auto_sync'), interval_minutes: Number(data.get('interval_minutes') || 60) } });
  connection.config = config;
  for (const name of ['cms_token','re0_gateway_token','tmdb_credential','emby_api_key']) { const field = $(`[name="${name}"]`, form); if (field) field.value = ''; }
  $('[name="tmdb_credential"]', form).placeholder = config.tmdb_configured ? '已配置 · 留空保留现有凭据' : '填写 TMDB 读取令牌或 API Key';
  $('[name="emby_api_key"]', form).placeholder = config.emby_configured ? '已配置 · 留空保留现有密钥' : '填写 Emby API Key';
  const badge = $('.integration-settings .settings-title-row .status'); if (badge) { badge.textContent = config.auto_sync ? '自动同步已开启' : '自动同步已暂停'; badge.className = `status ${config.auto_sync ? 'status-updating' : 'status-unverified'}`; }
  $('#integration-error').textContent = ''; updateSyncLabels(); await refreshServiceConnections(); scheduleIntegrationPoll(); if (!quiet) toast('连接设置已保存。'); return config;
}
async function testIntegration(target, button) {
  const label = button.textContent; button.disabled = true; button.textContent = '正在测试…'; const resultNode = $('#integration-test-result'); resultNode.textContent = '';
  try { await saveIntegrationSettings(true); const result = await api('/api/integrations/test', { method: 'POST', body: { target } }); resultNode.textContent = result.message || (result.ok ? '连接成功。' : '连接失败，请检查配置。'); resultNode.className = `integration-test-result ${result.ok ? 'success' : 'error'}`; }
  catch (error) { resultNode.textContent = error.message; resultNode.className = 'integration-test-result error'; }
  finally { button.disabled = false; button.textContent = label; }
}
async function toggleTracking(id, button) {
  const item = itemById(id); if (!item) return; button.disabled = true;
  try { const tracking = item.tracking === false; const result = await api(`/api/items/${encodeURIComponent(id)}`, { method: 'PUT', body: { ...item, tracking } }); await loadItems(); renderPage(); openDetail(id); toast(result.item?.cms_delivery?.error ? '本地状态已保存，CMS 联动失败，请在详情中重试下发。' : tracking ? '已开启这部作品的自动追更。' : '已暂停这部作品的自动追更。', !!result.item?.cms_delivery?.error); }
  catch (error) { toast(error.message, true); } finally { button.disabled = false; }
}

function embyComparison(item) {
  if (!item.tmdb) return '';
  const verified = hasVerifiedLibrary(item); const embyState = item.emby?.state || 'never';
  const explanations = { never: '尚未完成 Emby 比对，当前入库情况待核实。', absent: '已按 TMDB ID 和作品名称查询 Emby，库中未找到对应作品，当前为 0 集，等待入库。', unmatched: item.emby?.error || '库中存在同名或相似作品，但 TMDB ID 不一致，请检查刮削信息；历史入库进度已保留。', error: item.emby?.error || 'Emby 比对暂时失败，请检查连接后重新同步。', matched: '已通过作品标识匹配 Emby，以下为实际入库、已播与网盘来源集数对比。' };
  return `<section class="comparison-panel"><div class="comparison-heading"><div><span class="eyebrow">TMDB × EMBY</span><h3>播出与入库对比</h3></div><span class="status ${verified ? 'status-updating' : embyState === 'error' ? 'status-broken' : 'status-unverified'}">${esc(embyStateLabel(item))}</span></div><div class="comparison-values with-source"><div><small>Emby 入库</small><strong>${verified ? libraryCount(item) : '待核对'}<em>${verified ? item.kind === 'movie' ? '部' : '集' : ''}</em></strong><span>${verified ? esc(item.library_episodes || '未发现对应集数') : esc(item.library_episodes ? `手动记录：${item.library_episodes}` : '不将未匹配视为未入库')}</span></div><div><small>TMDB 已播 · 按排期</small><strong>${airedCount(item)}<em>${item.kind === 'movie' ? '部' : '集'}</em></strong><span>${esc(item.aired_episodes || (item.status === 'premiering' ? premiereLabel(item) : item.status === 'upcoming' ? '尚未播出' : '播出信息待更新'))}</span></div>${sourceEpisodeMetric(item)}<div><small>后续 / 日期待定</small><strong>${Number.isFinite(item.not_aired_count) ? item.not_aired_count : '—'}<em>${item.kind === 'movie' ? '部' : '集'}</em></strong><span>不含今日排期</span></div></div>${airingScheduleNotice(item)}<p class="comparison-explanation ${verified ? '' : 'unverified'}">${icon(verified ? 'circle-check' : 'info')}<span>${esc(explanations[embyState] || explanations.never)}</span></p>${item.tmdb.metadata_error ? `<p class="form-error">TMDB 信息更新失败：${esc(item.tmdb.metadata_error)}</p>` : ''}<div class="comparison-foot"><span>TMDB ${esc(stamp(item.tmdb.synced_at))} · Emby ${esc(stamp(item.emby?.checked_at))}</span><button class="text-link" data-integration="sync-item" data-id="${esc(item.id)}">${icon('refresh')}立即比对</button></div></section>`;
}
function eventTimeline(item) {
  if (!Array.isArray(item.events) || !item.events.length) return '';
  const events = [...item.events].reverse().slice(0, 12);
  return `<section class="form-section timeline-section"><h3 class="section-label">整理轨迹 <small>最近 ${events.length} 条</small></h3><ol class="event-timeline">${events.map(event => `<li><span class="timeline-dot"></span><div><strong>${esc(activityMeta(event.action).label)}</strong>${event.detail ? `<p>${esc(typeof event.detail === 'string' ? event.detail : JSON.stringify(event.detail))}</p>` : ''}<time>${esc(stamp(event.at))}</time></div></li>`).join('')}</ol></section>`;
}

function integratedTableRow(item) {
  const source = sourceOf(item), library = libraryCount(item), total = item.total_episodes, verified = hasVerifiedLibrary(item), aired = airedCount(item);
  const percent = verified && aired ? Math.min(100, Math.round(library / aired * 100)) : verified && item.status === 'complete' ? 100 : 0;
  const progress = verified ? `<strong>Emby ${library}</strong><span> / 已播 ${aired}</span>` : '<strong style="font-size:10px;color:#a093ad">入库待核对</strong>';
  return `<tr data-action="detail" data-id="${esc(item.id)}" tabindex="0" aria-label="查看${esc(titleOf(item))}"><td><div class="table-title-cell">${cover(item)}<div><p class="resource-title">${esc(item.title)}</p>${requesterSummary(item)}<div class="resource-meta"><span>${esc(kindLabels[item.kind] || '其他')}${item.kind !== 'movie' ? ` · 第 ${item.season || 1} 季` : ''}</span>${item.year ? `<span>· ${item.year}</span>` : ''}${item.tracking === false ? '<span class="tracking-paused">已暂停</span>' : ''}</div><div class="mobile-resource-status">${statusBadge(item)}</div></div></div></td><td>${statusBadge(item)}${item.source_broken ? '<div class="source-version" style="color:#bf877b">来源异常</div>' : ''}</td><td><div class="progress-cell"><div class="progress-count">${progress}</div><div class="progress-track"><i style="width:${percent}%"></i></div><div class="table-progress-note${verified ? '' : ' uncertain'}">${verified ? `共 ${total || '?'} ${item.kind === 'movie' ? '部' : '集'}${item.not_aired_count ? ` · 后续 / 待定 ${item.not_aired_count}` : ''}` : `TMDB 已播 ${aired}${library ? ` · 手录 ${library}` : ''}`}</div>${item.airing_today_count ? `<div class="table-progress-note">${esc(todayScheduleSummary(item))}</div>` : ''}</div></td><td><div class="source-name">${esc(source?.sharer || '暂未记录')}</div><div class="source-version">${esc(source?.version || (source ? '版本未填写' : '等待合适来源'))}</div>${sourceEpisodeProgress(item)}</td><td>${attentionCell(item)}</td><td><div class="row-actions">${state.view === 'trash' ? `<button class="icon-button" title="恢复资源" aria-label="恢复${esc(item.title)}" data-action="restore" data-id="${esc(item.id)}">${icon('refresh')}</button>` : `<button class="icon-button" title="编辑资源" aria-label="编辑${esc(item.title)}" data-action="edit" data-id="${esc(item.id)}">${icon('edit')}</button>`}<button class="icon-button" title="查看详情" aria-label="查看${esc(item.title)}详情" data-action="detail" data-id="${esc(item.id)}">${icon('arrow')}</button></div></td></tr>`;
}

function openIntegratedDetail(id) {
  const item = itemById(id); if (!item) return;
  const trashed = state.view === 'trash', library = libraryCount(item), available = availableCount(item), verified = hasVerifiedLibrary(item);
  openModal(`${modalHeader('THE STORY SO FAR', titleOf(item), `${kindLabels[item.kind] || ''}${item.year ? ` · ${item.year}` : ''} · ${item.finished ? '已完结 / 已上映' : '尚未完结'}`)}<div class="modal-body"><div class="detail-story-hero">${poster(item.tmdb.poster_path, '')}<div class="detail-story-copy"><p class="eyebrow">METADATA FROM TMDB · ${esc(item.tmdb.id)}</p><p>${esc(item.tmdb.overview || '作品已连接 TMDB，播出信息会在同步时更新。')}</p></div></div><div class="detail-track-actions">${statusBadge(item)}${item.source_broken ? '<span class="status status-broken">当前来源异常</span>' : ''}${!trashed ? `<button class="button secondary small" data-integration="toggle-tracking" data-id="${esc(id)}">${icon(item.tracking === false ? 'refresh' : 'clock')}${item.tracking === false ? '开启自动追更' : '暂停自动追更'}</button>` : ''}</div>${attentionPanel(item)}${embyComparison(item)}${item.missing_episodes ? verified ? `<div class="view-banner">${icon('download')}<span>已播或来源已有、等待入库：<strong>${esc(item.missing_episodes)}</strong>。未播出集数不计入缺集。</span></div>` : '<p class="missing-unverified">已播 / 来源已有集数已记录。Emby 匹配结果待核对，暂不据此断定缺集。</p>' : ''}${re0BindingSection(item, trashed)}<section class="form-section"><h3 class="section-label">来源与版本 <small>${item.sources?.length || 0} 个来源，保留历史记录</small></h3>${item.sources?.length ? item.sources.map(source => detailSource(source, String(source.id) === String(item.active_source_id))).join('') : '<div class="note-block">还没有资源来源。选择 RE0 条目后，自动记录发布者、版本和 115 链接。</div>'}</section>${item.notes ? `<section class="form-section"><h3 class="section-label">我的备注</h3><div class="note-block">${esc(item.notes)}</div></section>` : ''}${manualReviewHistory(item)}${eventTimeline(item)}</div><footer class="modal-footer">${trashed ? `<button class="button primary" data-action="restore" data-id="${esc(item.id)}">${icon('refresh')}恢复到片库</button>` : `<button class="button subtle left-action" data-action="delete" data-id="${esc(item.id)}">${icon('trash')}移到回收站</button><button class="button secondary" data-action="close-modal">关闭</button><button class="button primary" data-action="edit" data-id="${esc(item.id)}">${icon('edit')}编辑资源</button>`}</footer>`);
  $('#modal .modal-body').insertAdjacentHTML('beforeend', requesterPanel(item));
  $('#modal').dataset.itemId = String(id);
}

document.addEventListener('submit', event => {
  if (event.target.id === 'tmdb-search-form') { event.preventDefault(); searchTmdb(new FormData(event.target).get('query') || '', 1); }
  else if (event.target.id === 'tmdb-track-form') trackTmdb(event);
  else if (event.target.id === 'integration-form') { event.preventDefault(); const button = $('button[type="submit"]', event.target); button.disabled = true; saveIntegrationSettings().catch(error => { $('#integration-error').textContent = error.message; }).finally(() => { button.disabled = false; }); }
});
document.addEventListener('click', async event => {
  const button = event.target.closest('[data-integration]'); if (!button) return; const action = button.dataset.integration;
  try {
    if (action === 'check-connections') { await refreshServiceConnections(true); scheduleIntegrationPoll(); }
    else if (action === 'manual') openEditor();
    else if (action === 'select-title') await confirmTmdbTitle(button.dataset.mediaType, button.dataset.tmdbId, button);
    else if (action === 'search-page') await searchTmdb(connection.search.query, Number(button.dataset.page));
    else if (action === 'back-search') await openTmdbSearch(connection.search.query);
    else if (action === 'test') await testIntegration(button.dataset.target, button);
    else if (action === 'sync-all') { if ($('#integration-form')) await saveIntegrationSettings(true); await startSync(); }
    else if (action === 'sync-item') { button.disabled = true; await startSync(button.dataset.id); }
    else if (action === 'toggle-tracking') await toggleTracking(button.dataset.id, button);
  } catch (error) { toast(error.message, true); }
  finally { if (action === 'sync-item') button.disabled = false; }
});
document.addEventListener('error', event => { if (event.target.tagName === 'IMG' && (event.target.closest('.tmdb-poster') || event.target.closest('.mini-cover'))) event.target.remove(); if (event.target.classList?.contains('tmdb-brand-logo')) event.target.remove(); }, true);
