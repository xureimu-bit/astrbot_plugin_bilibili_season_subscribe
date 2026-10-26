const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;
const $ = (id) => document.getElementById(id);
const state = { tab: 'overview', group: 'summary', data: null, settings: null, dirty: {}, clear: new Set(), loading: false };
const groups = {
  summary: ['✨ AI 总结', '短一点，重点更清楚。聊天总结无需开启自动推送。', ['summary_enabled', 'summary_provider_id', 'summary_max_output_chars', 'summary_max_input_chars', 'summary_max_segments', 'summary_timeout_seconds']],
  asr: ['🎙 语音转写', '没有字幕时，使用 AstrBot 已配置的 STT。', ['asr_enabled', 'asr_provider_id', 'asr_timeout_seconds', 'asr_max_duration_seconds', 'asr_chunk_seconds', 'asr_max_download_mb', 'asr_cache_hours']],
  meme: ['✦ 标题梗与热梗', '标题接梗直接生效；联网短评需在 AstrBot 中配置网页搜索。', ['meme_enabled', 'meme_timeout_seconds', 'meme_cache_hours']],
  download: ['↓ 下载与推送', '设置视频筛选与文件保留规则。', ['send_title_message', 'max_video_height', 'max_duration_seconds', 'max_filesize_mb', 'retain_download_hours', 'preferred_video_codec', 'preferred_ext', 'notify_on_first_subscribe']],
  network: ['⚙ 轮询与连接', '连接 B 站接口，以及本地 ffmpeg。', ['poll_interval_minutes', 'request_timeout_seconds', 'page_size', 'ffmpeg_path', 'bilibili_sessdata', 'bilibili_cookie_file']],
};
function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key.startsWith('on')) node.addEventListener(key.slice(2).toLowerCase(), value);
    else if (key === 'class') node.className = value;
    else if (key in node) node[key] = value;
    else node.setAttribute(key, value);
  }
  for (const child of children.flat()) if (child !== null && child !== undefined) node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  return node;
}
function notify(message, error = false) { const node = $('notice'); node.hidden = false; node.className = `notice${error ? ' error' : ''}`; node.textContent = message; }
function date(value) { return value ? new Date(value).toLocaleString([], { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }) : '尚未检查'; }
function pill(text, active) { return el('span', { class: `pill ${active ? 'ok' : ''}` }, text); }
async function confirmAction(title, text) {
  $('confirm-title').textContent = title; $('confirm-text').textContent = text;
  const dialog = $('confirm-dialog'); dialog.returnValue = ''; dialog.showModal();
  return new Promise((resolve) => dialog.addEventListener('close', () => resolve(dialog.returnValue === 'confirm'), { once: true }));
}
async function refresh() {
  if (state.loading) return;
  state.loading = true; $('refresh').disabled = true;
  try {
    state.data = await bridge.apiGet('overview');
    $('version').textContent = state.data.version;
    $('status').className = `status ${state.data.active ? 'warning' : state.data.running ? 'ok' : ''}`;
    $('status').textContent = state.data.active ? '正在检查' : state.data.running ? '轮询运行中' : '轮询未启动';
    $('check').disabled = state.data.busy;
    if (state.tab !== 'settings') render();
  } catch (error) { notify(error.message || '读取状态失败', true); }
  finally { state.loading = false; $('refresh').disabled = false; }
}
function metric(label, value, caption, symbol) { return el('article', { class: 'card metric' }, el('div', { class: 'label' }, label, el('span', { class: 'symbol' }, symbol)), el('div', { class: 'value' }, value), el('div', { class: 'caption' }, caption)); }
function card(title, subtitle, content) { return el('section', { class: 'card' }, el('div', { class: 'panel-head' }, el('h2', {}, title), subtitle ? el('span', { class: 'caption' }, subtitle) : null), content); }
function empty(title, text) { return el('div', { class: 'empty' }, el('strong', {}, title), text); }
function overview() {
  const data = state.data, m = data.metrics;
  const metrics = el('div', { class: 'metrics' }, metric('订阅合集', m.subscriptions, '跨会话合并追更', '▤'), metric('推送会话', m.sessions, '独立管理每个会话的订阅', '◎'), metric('待重试视频', m.pending, '下次检查会继续尝试', '↻'), metric('已处理视频', m.processed, '包含已发送与按规则跳过', '✓'));
  const days = Array.from({ length: 14 }, (_, i) => { const d = new Date(); d.setUTCDate(d.getUTCDate() - 13 + i); return d.toISOString().slice(0, 10); });
  const maximum = Math.max(1, ...days.map((day) => data.daily[day] || 0));
  const chart = el('div', { class: 'chart', role: 'img', 'aria-label': '近14天检查次数，按UTC日期统计' });
  for (const day of days) { const count = data.daily[day] || 0; chart.append(el('div', { class: 'bar-col', title: `${day} · ${count} 次` }, el('span', { class: 'bar-number' }, count), el('div', { class: `bar ${count ? '' : 'zero'}`, style: `height:${Math.max(1, count / maximum * 85)}%` }))); }
  const trends = card('检查活动', '近14天 · UTC', el('div', {}, chart, el('div', { class: 'chart-labels' }, days.map((day) => el('span', {}, day.slice(5)))), el('p', { class: 'caption', style: 'padding:0 22px 10px' }, '记录从此版本启用后开始，保留最近 200 次检查。')));
  const features = el('div', { class: 'features' });
  for (const [key, symbol, title, copy] of [['summary_enabled', '✦', 'AI 视频总结', '视频推送后附上简短要点'], ['asr_enabled', '≋', '无字幕语音转写', '复用 AstrBot STT 提取内容'], ['meme_enabled', '✧', '联网热梗短评', '有贴切资料才补一句短评']]) {
    features.append(el('div', { class: 'feature-row' }, el('div', { class: 'feature-label' }, el('span', { class: 'feature-icon' }, symbol), el('div', {}, el('h3', {}, title), el('div', { class: 'caption' }, copy))), pill(data.features[key] ? '已开启' : '未开启', data.features[key])));
  }
  features.append(el('button', { class: 'link-button', style: 'margin:12px 0', onClick: () => switchTab('settings') }, '前往配置中心 →'));
  const latest = data.history[0];
  const task = el('section', { class: 'card task' }, el('div', { class: `task-icon ${data.active ? 'spinner' : ''}` }, data.active ? '↻' : '✓'), el('div', { class: 'task-copy' }, el('h3', {}, data.active ? `${data.active.mode}进行中` : '等待下一次更新'), el('p', {}, data.active ? `开始于 ${date(data.active.time)}，检查完成后状态会自动刷新。` : `每 ${data.interval} 分钟轮询 · ${data.next_check ? `下次预计 ${date(data.next_check)}` : '轮询启动后显示下次时间'}`), latest ? el('p', {}, `最近结果：${latest.message}`) : null), el('button', { class: 'button secondary', onClick: () => switchTab('history') }, '查看记录'));
  return [metrics, el('div', { class: 'grid' }, trends, card('功能状态', '按需开启', features)), task];
}
function subscriptions() {
  const data = state.data;
  const toolbar = el('div', { class: 'toolbar' }, el('div', {}, el('h2', {}, '订阅管理'), el('p', {}, '按会话管理合集；移除一个会话不会影响其他订阅者。')), el('button', { class: 'button primary', onClick: openAdd, disabled: data.busy }, '+ 添加合集'));
  if (!data.subscriptions.length) return [toolbar, empty('还没有订阅的合集', '先在需要推送的聊天里发送 /bili合集订阅 添加 <合集链接>，之后可在这里继续管理。')];
  const body = el('tbody');
  for (const item of data.subscriptions) {
    const sessions = el('div');
    for (const sub of item.subscribers) sessions.append(el('div', { class: 'subscriber' }, el('span', { title: sub.origin }, sub.label), el('button', { class: 'link-button', disabled: data.busy, onClick: () => removeSubscription(item, sub) }, '移除')));
    body.append(el('tr', {}, el('td', {}, el('div', { class: 'row-title' }, el('a', { href: item.url, target: '_blank', rel: 'noopener noreferrer' }, item.title || '未命名合集')), el('span', { class: 'caption' }, `${item.mid}:${item.season_id}`)), el('td', {}, sessions), el('td', {}, pill(`${item.pending} 待重试`, !item.pending)), el('td', {}, item.processed), el('td', { class: 'muted' }, date(item.last_checked))));
  }
  return [toolbar, el('div', { class: 'card table-wrap' }, el('table', {}, el('thead', {}, el('tr', {}, ['合集', '推送会话', '待处理', '已处理', '最近检查'].map((label) => el('th', {}, label)))), body)), el('p', { class: 'caption' }, '首次订阅是否推送历史视频，遵循“下载与推送”中的配置。')];
}
function history() {
  const data = state.data;
  const toolbar = el('div', { class: 'toolbar' }, el('div', {}, el('h2', {}, '检查记录'), el('p', {}, '记录检查结果与实际耗时，重载后保留。')), el('button', { class: 'button secondary', disabled: data.busy, onClick: () => startCheck(true) }, '重试待处理'));
  if (!data.history.length) return [toolbar, empty('新的记录会出现在这里', '运行一次检查，或等待下一次自动轮询。')];
  return [toolbar, el('div', { class: 'card table-wrap' }, el('table', {}, el('thead', {}, el('tr', {}, ['时间 / 类型', '结果', '耗时', '状态'].map((x) => el('th', {}, x)))), el('tbody', {}, data.history.map((entry) => el('tr', {}, el('td', {}, date(entry.time), el('span', { class: 'caption', style: 'display:block' }, entry.mode)), el('td', { class: 'history-message' }, entry.message), el('td', {}, `${entry.duration}s`), el('td', {}, pill(entry.outcome === 'complete' ? '已完成' : entry.outcome === 'cancelled' ? '已取消' : '已中断', entry.outcome === 'complete')))))))];
}
function settings() {
  if (!state.settings) return [empty('正在读取配置', '请稍候…')];
  const cfg = state.settings, [title, copy, keys] = groups[state.group];
  const nav = el('aside', { class: 'settings-nav', 'aria-label': '配置分组' });
  for (const [key, group] of Object.entries(groups)) nav.append(el('button', { class: key === state.group ? 'selected' : '', 'aria-pressed': key === state.group, onClick: () => { state.group = key; render(); } }, group[0]));
  const fields = el('div', { class: 'form-grid' });
  for (const key of keys) {
    const spec = cfg.schema[key]; if (!spec) continue;
    const value = key in state.dirty ? state.dirty[key] : cfg.values[key];
    const id = `setting-${key}`;
    const label = el('label', { class: `field ${spec.type === 'bool' ? 'toggle-field' : ''}`, htmlFor: id });
    const caption = el('span', {}, spec.description, spec.hint ? el('small', {}, spec.hint) : null);
    let input;
    if (cfg.options[key]) {
      input = el('select', { id }, el('option', { value: '' }, '使用当前会话的模型'));
      const options = [...cfg.options[key]];
      if (value && !options.some((option) => option.id === value)) options.push({ id: value, label: `${value}（当前保存）` });
      for (const option of options) input.append(el('option', { value: option.id }, option.label));
      input.value = value;
    } else input = el('input', { id, type: spec.type === 'bool' ? 'checkbox' : key === 'bilibili_sessdata' ? 'password' : spec.type === 'int' ? 'number' : 'text', ...(spec.type === 'bool' ? { checked: value } : { value: value ?? '' }), ...(cfg.bounds[key] ? { min: cfg.bounds[key][0], max: cfg.bounds[key][1], step: 1 } : {}), autocomplete: 'off' });
    input.addEventListener('input', () => {
      const next = spec.type === 'bool' ? input.checked : spec.type === 'int' ? (input.value === '' ? null : Number(input.value)) : input.value;
      if (next === cfg.values[key]) delete state.dirty[key]; else state.dirty[key] = next;
      updateSaveCaption();
    });
    label.append(caption, input);
    if (key === 'bilibili_sessdata' && cfg.configured[key]) {
      input.placeholder = '已配置；留空保留现有值';
      label.append(el('span', { class: 'secret-clear' }, el('input', { type: 'checkbox', 'aria-label': '清除已保存的 SESSDATA', checked: state.clear.has(key), onChange: (event) => { event.target.checked ? state.clear.add(key) : state.clear.delete(key); updateSaveCaption(); } }), '清除已保存的 SESSDATA'));
    }
    fields.append(label);
  }
  const form = el('form', { class: 'card settings-body', onSubmit: saveSettings }, el('div', { class: 'settings-intro' }, el('h2', {}, title), el('p', {}, copy)), fields, el('div', { class: 'save-bar' }, el('div', { class: 'caption' }, el('span', { id: 'save-caption' }, '修改后点击保存。'), el('br'), '保存后立即应用；当前轮询等待时间保持不变，下个周期使用新间隔。'), el('button', { class: 'button primary', id: 'save-settings', type: 'submit' }, '保存配置')));
  return [el('div', { class: 'settings-layout' }, nav, form)];
}
function updateSaveCaption() { if ($('save-caption')) $('save-caption').textContent = Object.keys(state.dirty).length || state.clear.size ? `有 ${new Set([...Object.keys(state.dirty), ...state.clear]).size} 项未保存。` : '修改后点击保存。'; }
function render() {
  if (!state.data) return;
  $('content').replaceChildren(...({ overview, subscriptions, settings, history }[state.tab])()); updateSaveCaption();
}
async function switchTab(tab) {
  state.tab = tab;
  document.querySelectorAll('[data-tab]').forEach((node) => { const active = node.dataset.tab === tab; node.classList.toggle('selected', active); node.setAttribute('aria-selected', active); });
  render();
  if (tab === 'settings' && !state.settings) {
    try { state.settings = await bridge.apiGet('settings'); render(); } catch (error) { notify(error.message, true); }
  }
}
async function saveSettings(event) {
  event.preventDefault();
  if (state.clear.has('bilibili_sessdata') && state.dirty.bilibili_sessdata) return notify('请在填写新 SESSDATA 和清除旧值之间选择一项。', true);
  const button = $('save-settings'); button.disabled = true;
  try {
    state.settings = await bridge.apiPost('settings/save', { revision: state.settings.revision, values: state.dirty, clear_secrets: [...state.clear] });
    state.dirty = {}; state.clear.clear(); render(); notify('配置已保存并应用。'); await refresh();
  } catch (error) { notify(error.message, true); }
  finally { if (button.isConnected) button.disabled = false; }
}
async function startCheck(retry = false) {
  if (!await confirmAction(retry ? '重试待处理视频' : '立即检查全部合集', '此操作会按现有订阅与配置，向推送会话发送符合条件的视频及总结。')) return;
  try { await bridge.apiPost('check', { retry_only: retry }); notify('检查任务已启动，完成后记录会自动更新。'); await refresh(); } catch (error) { notify(error.message, true); }
}
function openAdd() {
  if (!state.data.sessions.length) return notify('首次订阅请在目标聊天里发送 /bili合集订阅 添加 <合集链接>。');
  $('add-origin').replaceChildren(...state.data.sessions.map((session) => el('option', { value: session.origin }, session.label)));
  $('add-url').value = ''; $('add-dialog').showModal();
}
async function removeSubscription(item, sub) {
  if (!await confirmAction('移除此会话订阅', `确定从“${sub.label}”移除“${item.title}”？其他会话的订阅会保留。`)) return;
  try { const response = await bridge.apiPost('subscriptions/remove', { mid: item.mid, season_id: item.season_id, origin: sub.origin }); notify(response.message); await refresh(); } catch (error) { notify(error.message, true); }
}
$('add-cancel').addEventListener('click', () => $('add-dialog').close());
$('add-form').addEventListener('submit', async (event) => {
  event.preventDefault(); const button = event.submitter; button.disabled = true;
  try { const response = await bridge.apiPost('subscriptions/add', { origin: $('add-origin').value, url: $('add-url').value }); $('add-dialog').close(); notify(response.message); await refresh(); } catch (error) { notify(error.message, true); } finally { button.disabled = false; }
});
$('refresh').addEventListener('click', async () => {
  if (state.tab === 'settings') {
    if ((Object.keys(state.dirty).length || state.clear.size) && !await confirmAction('重新读取配置', '这会放弃当前页面尚未保存的修改。')) return;
    try { state.settings = await bridge.apiGet('settings'); state.dirty = {}; state.clear.clear(); render(); }
    catch (error) { notify(error.message, true); }
  }
  await refresh();
});
$('check').addEventListener('click', () => startCheck());
document.querySelectorAll('[data-tab]').forEach((button) => button.addEventListener('click', () => switchTab(button.dataset.tab)));
window.addEventListener('beforeunload', (event) => { if (Object.keys(state.dirty).length || state.clear.size) event.preventDefault(); });
try {
  if (!bridge) throw new Error('请从 AstrBot 的插件页面打开此控制台。');
  await bridge.ready(); await refresh();
  setInterval(() => { if (!document.hidden) refresh(); }, 15000);
} catch (error) { $('content').replaceChildren(empty('暂时无法连接控制台', error.message)); $('check').disabled = true; notify(error.message, true); }
