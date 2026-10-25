'use strict';

const emailNotifications = { config:null, error:'' };

async function refreshEmailSettings() {
  if (!isAdministrator()) return;
  try {
    const config = await api('/api/notifications');
    if (!isAdministrator()) return;
    emailNotifications.config = config;
    emailNotifications.error = '';
  } catch (error) { emailNotifications.error = error.message; }
}

function emailDeliverySummary(config) {
  return `${config.last_sent_at ? `最近发信：${stamp(config.last_sent_at)}${config.last_sent_kind === 'test' ? '（测试邮件）' : '（提交通知）'}` : '尚未发送邮件'} · 等待发送 ${config.pending_count || 0} 封${config.failed_count ? ` · 发送失败 ${config.failed_count} 封` : ''}${config.last_error ? `。最近错误：${config.last_error}` : ''}`;
}

function emailNotificationSettingsSection() {
  if (!isAdministrator()) return '';
  if (emailNotifications.error || !emailNotifications.config) return `<section id="email-settings-section" class="settings-group"><h3>新追更邮件提醒</h3><p class="form-error">${esc(emailNotifications.error || '邮件设置暂未读取，请重试。')}</p><button class="button secondary" type="button" data-email-action="reload">重新读取邮件设置</button></section>`;
  const config = emailNotifications.config;
  return `<section id="email-settings-section" class="settings-group">
    <div class="settings-title-row"><h3>新追更邮件提醒</h3><span id="email-enabled-status" class="status ${config.enabled ? 'status-updating' : 'status-unverified'}">${config.enabled ? '已开启' : '未开启'}</span></div>
    <p>普通用户提交新的追更时，发送邮件提醒管理员。重复点击提交不重复通知，发送失败会自动重试；无需保持网页打开。</p>
    <form id="email-notification-form">
      <div class="form-grid">
        <label class="span-all">收件邮箱<input name="to_email" type="email" maxlength="254" autocomplete="off" placeholder="接收追更提醒的邮箱" value="${esc(config.to_email || '')}"></label>
        <label class="span-all">发件邮箱<input name="from_email" type="email" maxlength="254" autocomplete="off" placeholder="用于发送提醒的邮箱" value="${esc(config.from_email || '')}"></label>
        <label class="span-all">SMTP 服务器<input name="smtp_host" type="text" maxlength="254" autocomplete="off" placeholder="例如 smtp.qq.com" value="${esc(config.smtp_host || '')}"></label>
        <label>加密方式<select name="security"><option value="ssl"${config.security !== 'starttls' ? ' selected' : ''}>SSL/TLS</option><option value="starttls"${config.security === 'starttls' ? ' selected' : ''}>STARTTLS</option></select></label>
        <label>SMTP 端口<input name="smtp_port" type="number" min="1" max="65535" value="${esc(config.smtp_port || 465)}"><small>通常 SSL/TLS 使用 465，STARTTLS 使用 587，以邮箱服务商为准。</small></label>
        <label class="span-all">SMTP 用户名<input name="smtp_username" type="text" maxlength="254" autocomplete="off" placeholder="通常与发件邮箱相同" value="${esc(config.smtp_username || '')}"></label>
        <label class="span-all">SMTP 密码 / 授权码<input name="smtp_password" type="password" maxlength="4096" autocomplete="new-password" value="" placeholder="${config.has_password ? '已配置 · 留空保留已有授权码' : '填写邮箱的 SMTP 授权码或应用密码'}"><small>按邮箱服务商要求开启 SMTP；授权码在服务器加密保存，不会返回网页。更换服务器或用户名后需重新填写。</small></label>
        ${config.has_password ? '<label class="checkbox-label span-all"><input name="clear_password" type="checkbox">清除已保存的授权码</label>' : ''}
        <label class="span-all">邮件中的网站地址（可选）<input name="site_url" type="url" maxlength="2000" placeholder="https://shiying.example.com" value="${esc(config.site_url || window.location?.origin || '')}"><small>填写后，邮件会附上打开拾影的入口。</small></label>
        <label class="checkbox-label span-all"><input name="enabled" type="checkbox"${config.enabled ? ' checked' : ''}>开启新追更邮件提醒</label>
      </div>
      <p id="email-settings-error" class="form-error" role="alert"></p>
      <div class="settings-actions"><button class="button primary" type="submit">${icon('check')}保存邮件设置</button><button class="button secondary" type="button" data-email-action="test">发送测试邮件</button><button class="button secondary" type="button" data-email-action="retry"${!config.enabled || !(config.pending_count || config.failed_count) ? ' disabled' : ''}>重试未发送通知</button></div>
      <p id="email-test-result" class="integration-test-result" role="status"></p>
      <p id="email-delivery-summary" class="form-hint">${esc(emailDeliverySummary(config))}</p>
    </form>
  </section>`;
}

function updateEmailSettingsStatus(form, config) {
  if (!isAdministrator() || form !== $('#email-notification-form')) return;
  emailNotifications.config = config;
  const password = $('[name="smtp_password"]', form);
  password.value = '';
  password.placeholder = config.has_password ? '已配置 · 留空保留已有授权码' : '填写邮箱的 SMTP 授权码或应用密码';
  const clear = $('[name="clear_password"]', form); if (clear) clear.checked = false;
  const badge = $('#email-enabled-status');
  badge.textContent = config.enabled ? '已开启' : '未开启';
  badge.className = `status ${config.enabled ? 'status-updating' : 'status-unverified'}`;
  $('#email-delivery-summary').textContent = emailDeliverySummary(config);
}

async function saveEmailSettings(form) {
  if (!isAdministrator()) return;
  const fields = new FormData(form), body = {};
  for (const key of ['to_email','from_email','smtp_host','smtp_username','smtp_password','site_url','security']) body[key] = String(fields.get(key) || '').trim();
  body.smtp_port = Number(fields.get('smtp_port') || 465);
  body.enabled = fields.has('enabled');
  body.clear_password = fields.has('clear_password');
  const config = await api('/api/notifications', {method:'PUT', body});
  updateEmailSettingsStatus(form, config);
  return config;
}

async function emailSettingsAction(form, action) {
  if (!isAdministrator() || !form || form.dataset.mailBusy === 'true' || !form.reportValidity()) return;
  form.dataset.mailBusy = 'true';
  const buttons = $$('button', form); buttons.forEach(button => { button.disabled = true; });
  $('#email-settings-error').textContent = '';
  const resultNode = $('#email-test-result'); resultNode.textContent = '';
  try {
    await saveEmailSettings(form);
    if (form !== $('#email-notification-form') || !isAdministrator()) return;
    if (action === 'test') {
      resultNode.textContent = '正在发送测试邮件…';
      const result = await api('/api/notifications/test', {method:'POST', body:{}});
      if (form !== $('#email-notification-form') || !isAdministrator()) return;
      updateEmailSettingsStatus(form, result);
      resultNode.textContent = result.message;
      resultNode.className = 'integration-test-result success';
    } else if (action === 'retry') {
      const config = await api('/api/notifications/retry', {method:'POST', body:{}});
      updateEmailSettingsStatus(form, config);
      resultNode.textContent = '已提交后台重试，可稍后重新打开设置查看结果。';
    } else { resultNode.textContent = '邮件设置已保存。'; }
  } catch (error) {
    if (form === $('#email-notification-form') && isAdministrator()) $('#email-settings-error').textContent = error.message;
  } finally {
    delete form.dataset.mailBusy;
    buttons.forEach(button => { button.disabled = false; });
    const config = emailNotifications.config;
    const retry = $('[data-email-action="retry"]', form);
    if (retry && config) retry.disabled = !config.enabled || !(config.pending_count || config.failed_count);
  }
}

document.addEventListener('submit', event => {
  if (event.target.id !== 'email-notification-form') return;
  event.preventDefault(); emailSettingsAction(event.target, 'save');
});
document.addEventListener('click', async event => {
  const button = event.target.closest('[data-email-action]');
  if (!button || !isAdministrator() || button.disabled) return;
  if (button.dataset.emailAction === 'reload') {
    button.disabled = true;
    await refreshEmailSettings();
    const section = $('#email-settings-section');
    if (section && isAdministrator()) section.outerHTML = emailNotificationSettingsSection();
  } else { await emailSettingsAction(button.closest('form'), button.dataset.emailAction); }
});
document.addEventListener('change', event => {
  if (!event.target.matches('#email-notification-form [name="security"]')) return;
  const port = $('[name="smtp_port"]', event.target.form);
  if (['465','587'].includes(port.value)) port.value = event.target.value === 'starttls' ? '587' : '465';
});
