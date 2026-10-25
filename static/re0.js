'use strict';
const re0Picker = { generation: 0, itemId: null, rows: [] };
let cmsRunTimer;
function cmsRunSection(item) {
  const run = item.cms_run, busy = ['queued', 'running', 'unconfirmed'].includes(run?.state);
  const label = {queued:'等待立即执行', running:'正在读取来源并转存', submitted:'已提交 CMS 转存', exists:'CMS 已有任务或记录', error:'本次执行失败', unknown:'执行结果待核对', unconfirmed:'正在确认执行请求', cancelled:'本次执行已取消'}[run?.state] || '绑定后会立即执行一次';
  return `<div class="cms-run-status" role="status"><strong>${esc(label)}</strong>${run?.message ? `<p>${esc(run.message)}</p>` : ''}${run?.updated_at ? `<p>${esc(stamp(run.updated_at))}</p>` : ''}<div class="settings-actions">${item.re0 && item.tracking !== false ? `<button class="button primary small" data-re0="run" data-id="${esc(item.id)}"${busy ? ' disabled' : ''}>${icon('download')}${busy ? '执行中…' : '立即入库一次'}</button><button class="text-link" data-re0="run-status" data-id="${esc(item.id)}">${icon('refresh')}刷新执行状态</button>` : ''}</div><p>立即提交当前固定链接；CMS 转存后自动整理，Emby 扫描后才算实际入库。后续仍按原定时计划追更。</p></div>`;
}
function scheduleCmsRunPoll(item) {
  clearTimeout(cmsRunTimer);
  if (!['queued', 'running', 'unconfirmed'].includes(item.cms_run?.state)) return;
  cmsRunTimer = setTimeout(async () => {
    if (!isAdministrator() || !$('#modal').open || $('#modal').dataset.itemId !== item.id || !$('#cms-run-panel')) return;
    try {
      const result = await api(`/api/items/${encodeURIComponent(item.id)}/cms/run`);
      if (!isAdministrator() || !$('#modal').open || $('#modal').dataset.itemId !== item.id) return;
      const current = itemById(item.id);
      if (current) current.cms_run = result.item.cms_run;
      const panel = $('#cms-run-panel'); if (panel) panel.innerHTML = cmsRunSection(result.item);
      scheduleCmsRunPoll(result.item);
    } catch { const panel = $('#cms-run-panel'); if (panel) panel.insertAdjacentHTML('beforeend', '<p class="form-error">状态暂时无法读取，请点击刷新执行状态；无需重复提交任务。</p>'); }
  }, 3000);
}
function cmsDeliverySection(item) {
  const delivery = item.cms_delivery;
  if (!delivery && !item.re0) return '';
  scheduleCmsRunPoll(item);
  const label = !delivery ? '待下发 CMS' : {submitted:'已下发 CMS', paused:'CMS 该季已暂停', error:'CMS 下发失败'}[delivery.state] || '待下发 CMS';
  return `<div class="cms-delivery note-block"><div class="settings-title-row"><strong>${label}</strong><button class="text-link" data-re0="deliver" data-id="${esc(item.id)}">${icon('refresh')}${delivery?.state === 'error' ? '重试下发' : '重新下发'}</button></div>${delivery?.address ? `<p>${connection.config?.re0_via_cms ? 'CMS' : '执行端'}：${esc(delivery.address)}</p>` : ''}${delivery?.subscription_id ? `<p>CMS 媒体订阅 #${esc(delivery.subscription_id)} · ${esc(stamp(delivery.checked_at))}</p>` : ''}${delivery?.error ? `<p class="form-error">${esc(delivery.error)}${delivery.active === false ? '。CMS 暂停尚未确认，原订阅可能仍在执行。' : ''}</p>` : ''}<div id="cms-run-panel">${cmsRunSection(item)}</div></div>`;
}
function re0BindingSection(item, trashed = false) {
  const binding = item.re0;
  return `<section class="form-section re0-binding"><div class="settings-title-row"><h3 class="section-label">RE0 自动来源与 CMS 订阅</h3>${!trashed ? `<button class="button ${binding ? 'secondary' : 'primary'} small" data-re0="pick" data-id="${esc(item.id)}">${icon('search')}${binding ? '更换来源' : '选择 RE0 来源'}</button>` : ''}</div>${binding ? `<div class="note-block"><strong>${esc(binding.uploader)}</strong><p>${esc(binding.title)}</p><p>固定检查同一条目与发布者 · ${esc(stamp(binding.checked_at))}</p>${binding.error ? `<p class="form-error">${esc(binding.error)} · 上次链接已保留</p>` : ''}${!trashed ? `<button class="text-link" data-re0="unbind" data-id="${esc(item.id)}">解绑来源并暂停 CMS 该季追更</button>` : ''}</div>` : '<p class="form-hint">从 RE0 选择明确覆盖本季的版本，来源信息自动记录，绑定后自动识别网盘集数、立即执行一次转存，并将作品、季数、条目与固定发布者下发给你的 CMS。免费或已解锁条目可直接使用，收费条目可按显示的积分解锁。</p>'}${cmsDeliverySection(item)}<p class="form-hint">${connection.config?.re0_via_cms ? 'RE0 查询、解锁与订阅均使用你这台 CMS 自身的授权。' : 'RE0 查询经已授权 CMS，订阅交给你的 CMS 执行。'}CMS 按作品建一条媒体订阅，拾影逐季保存选定来源；自动检查仅使用选定条目，不自动换发布者或购买新的收费资源。</p></section>`;
}
async function openRe0Picker(id) {
  if (!isAdministrator()) return;
  const item = itemById(id); if (!item?.tmdb) return;
  const generation = ++re0Picker.generation; re0Picker.itemId = id; re0Picker.rows = [];
  openModal(`${modalHeader('SOURCES FROM RE0', '为作品选择来源', `${titleOf(item)} · 请选择明确覆盖当前季的版本。`)}<div class="modal-body"><div class="view-banner">${icon('info')}<span>按 TMDB 精确查找作品。剧集的不同季可能共用条目，请核对资源标题；不以来源标题推断已有集数。</span></div><div id="re0-picker-content"><div class="search-loading"><span class="spinner"></span><p>${connection.config?.re0_via_cms ? '正在通过你的 CMS 查询 RE0…' : '正在通过已授权查询端查询 RE0…'}</p></div></div><p id="re0-picker-error" class="form-error" role="alert"></p></div><footer class="modal-footer"><button class="button secondary" data-action="detail" data-id="${esc(id)}">返回作品</button></footer>`, '900px');
  $('#modal').dataset.itemId = '';
  try {
    const response = await api(`/api/items/${encodeURIComponent(id)}/re0`);
    if (generation !== re0Picker.generation || !$('#re0-picker-content')) return;
    re0Picker.rows = response.results || [];
    $('#re0-picker-content').innerHTML = `<div class="re0-filter"><div class="search-box">${icon('search')}<input id="re0-filter" type="search" placeholder="筛选发布者、清晰度或版本" aria-label="筛选 RE0 来源"></div><span>${re0Picker.rows.length} 个 115 条目</span></div><div id="re0-results"></div>`;
    renderRe0Results();
  } catch (error) { if (generation === re0Picker.generation && $('#re0-picker-content')) $('#re0-picker-content').innerHTML = `<div class="note-block">${esc(error.message)}。请在设置中测试 CMS 与 RE0 授权。</div>`; }
}
function renderRe0Results(query = '') {
  const root = $('#re0-results'); if (!root) return;
  const filtered = re0Picker.rows.map((row, index) => ({ row, index })).filter(({ row }) => [row.title, row.uploader, row.resolution, row.source, row.remark].join(' ').toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()));
  root.innerHTML = filtered.length ? filtered.map(({ row, index }) => `<article class="re0-candidate"><div class="re0-candidate-top"><strong>${esc(row.uploader || '发布者未知')}</strong><span class="status ${row.can_bind ? 'status-updating' : 'status-unverified'}">${row.is_unlocked ? '已解锁' : row.unlock_points === 0 ? '免费' : row.unlock_points == null ? '价格待核对' : `${esc(row.unlock_points)} 积分`}</span></div><h3>${esc(row.title)}</h3><p>${esc([row.resolution, row.source, row.size].filter(Boolean).join(' · '))}</p>${row.remark ? `<p class="re0-remark">${esc(row.remark)}</p>` : ''}<div class="re0-candidate-actions"><a class="text-link" href="${esc(row.resource_url)}" target="_blank" rel="noopener noreferrer">查看 RE0 原帖 ${icon('external')}</a><button class="button primary small" data-re0="bind" data-index="${index}"${row.can_bind || row.can_unlock ? '' : ' disabled'}>${row.can_bind ? '绑定并立即入库' : row.can_unlock ? `解锁并立即入库 · ${esc(row.unlock_points)} 积分` : !row.uploader ? '缺少发布者' : '价格待核对'}</button></div></article>`).join('') : emptyState('没有找到符合条件的来源', '可以调整筛选，或稍后等待 RE0 发布资源。');
}
document.addEventListener('input', event => { if (event.target.id === 're0-filter') renderRe0Results(event.target.value); });
document.addEventListener('click', async event => {
  const button = event.target.closest('[data-re0]'); if (!button || !isAdministrator()) return;
  const action = button.dataset.re0;
  try {
    if (action === 'pick') return await openRe0Picker(button.dataset.id);
    button.disabled = true;
    if (action === 'bind') {
      const id = re0Picker.itemId, row = re0Picker.rows[Number(button.dataset.index)];
      if (!row || (!row.can_bind && !row.can_unlock)) return;
      button.textContent = row.can_bind ? '正在绑定…' : '正在解锁…';
      const result = await api(`/api/items/${encodeURIComponent(id)}/re0`, { method: 'POST', body: { slug: row.slug, uploader: row.uploader, ...(!row.can_bind && row.can_unlock ? { max_points: row.unlock_points } : {}) } });
      await refreshIntegrationStatus(false); await loadItems(); renderPage(); openDetail(id); scheduleIntegrationPoll(); toast(result.item?.cms_delivery?.error ? '来源已绑定，CMS 下发失败，可在详情重试。' : '已绑定固定来源，后台自动识别集数，并请求 CMS 立即执行一次；实际入库以 Emby 为准。', !!result.item?.cms_delivery?.error);
    } else if (action === 'run' || action === 'run-status') {
      const id = button.dataset.id;
      const result = await api(`/api/items/${encodeURIComponent(id)}/cms/run`, action === 'run' ? {method:'POST',body:{}} : {});
      await loadItems(); renderPage(); openDetail(id);
      toast(result.item?.cms_delivery?.error || result.item?.cms_run?.message || '已刷新执行状态。', !!result.item?.cms_delivery?.error || ['error', 'unknown'].includes(result.item?.cms_run?.state));
    } else if (action === 'deliver') {
      const id = button.dataset.id;
      const result = await api(`/api/items/${encodeURIComponent(id)}/cms`, {method:'POST',body:{}});
      await loadItems(); renderPage(); openDetail(id);
      toast(result.item?.cms_delivery?.error || 'CMS 已接收当前订阅规则。', !!result.item?.cms_delivery?.error);
    } else if (action === 'unbind') {
      const id = button.dataset.id;
      const result = await api(`/api/items/${encodeURIComponent(id)}/re0`, { method: 'DELETE', body: {} });
      await loadItems(); renderPage(); openDetail(id); toast(result.item?.cms_delivery?.error ? '来源已解绑，CMS 暂停失败，请重试下发暂停。' : '已解绑来源并暂停 CMS 该季追更，历史来源已保留。', !!result.item?.cms_delivery?.error);
    }
  } catch (error) { if ($('#re0-picker-error')) $('#re0-picker-error').textContent = error.message; else toast(error.message, true); }
  finally { button.disabled = false; if (action === 'bind' && button.isConnected) { const row = re0Picker.rows[Number(button.dataset.index)]; button.textContent = row?.can_bind ? '绑定并立即入库' : row?.can_unlock ? `解锁并立即入库 · ${row.unlock_points} 积分` : '价格待核对'; } }
});

// Managed source fields are filled from RE0; episode notes remain editable.
const re0OriginalEditor = openEditor;
openEditor = function(id) {
  re0OriginalEditor(id);
  const item = id ? itemById(id) : null;
  if (!item?.re0) return;
  const card = $$('.source-card').find(card => card.dataset.sourceId === item.re0.source_id);
  if (!card) return;
  for (const name of ['sharer', 'version', 'post_url', 'share_url', 'code']) {
    const input = $(`[name="${name}"]`, card); if (input) input.readOnly = true;
  }
  card.insertAdjacentHTML('afterbegin', '<p class="form-hint">此来源由 RE0 自动维护。更换发布者或版本，请返回作品详情选择新的 RE0 来源。</p>');
};
