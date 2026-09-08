(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const labels = { unknown: '待采集', offline: '未开播', live: '直播中', blocked: '采集受限', error: '采集失败' };
  const numberFormat = new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 0 });
  const selectedRooms = new Set();
  const model = { state: null, selected: null, hours: 24, history: [], historyKey: '', historyTime: 0, historyRequest: 0, connected: false, eventsSeen: null, removeId: null, settingsRoom: null, settingsKey: '', tableKey: '', eventsKey: '', syncPending: false, syncAgain: false };
  let desktopEnabled = false;
  try { desktopEnabled = localStorage.getItem('live-monitor-desktop') === 'true'; } catch (_) { /* Storage can be unavailable in private contexts. */ }

  function escape(value) {
    return String(value ?? '').replace(/[&<>"']/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
  }
  function numeric(value) { return value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value)) ? Number(value) : null; }
  function formatNumber(value) { const n = numeric(value); return n === null ? '—' : numberFormat.format(n); }
  function dateOf(value) {
    if (!value) return null;
    const date = new Date(typeof value === 'number' && value < 100000000000 ? value * 1000 : value);
    return Number.isFinite(date.getTime()) ? date : null;
  }
  function time(value, seconds = false) {
    const d = dateOf(value);
    return d ? d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', ...(seconds ? { second: '2-digit' } : {}), hour12: false }) : '—';
  }
  function day(value) { const d = dateOf(value); return d ? `${String(d.getMonth() + 1).padStart(2, '0')}/${String(d.getDate()).padStart(2, '0')}` : ''; }
  function fullTime(value) { const d = dateOf(value); return d ? d.toLocaleString('zh-CN', { hour12: false }) : '尚未采集成功'; }
  function shortAge(value) {
    const d = dateOf(value);
    if (!d) return '尚未采集成功';
    const seconds = Math.max(0, Math.floor((Date.now() - d.getTime()) / 1000));
    if (seconds < 60) return '刚刚更新';
    if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`;
    return `${Math.floor(seconds / 86400)} 天前`;
  }
  function statusOf(room) { return Object.hasOwn(labels, room?.status) ? room.status : 'unknown'; }
  function statusBadge(room) { const status = statusOf(room); const label = status === "live" && room?.latest?.source === "douyin-profile-live_status" ? "主页显示在播" : labels[status]; return `<span class="status-badge ${status}">${label}</span>`; }
  function roomName(room) { return room?.name || room?.latest?.nickname || '未命名直播间'; }
  function roomById(id) { return model.state?.rooms?.find((room) => String(room.id) === String(id)); }
  function onlineText(latest) { return numeric(latest?.online) !== null ? formatNumber(latest.online) : (latest?.online_display ? String(latest.online_display) : '—'); }
  function isApprox(latest, field) {
    const fields = latest?.approx_fields;
    return Array.isArray(fields) ? fields.includes(field) : Boolean(fields && typeof fields === 'object' && fields[field]);
  }
  function safeExternalURL(value) {
    try { const url = new URL(value); return /^https?:$/.test(url.protocol) ? url.href : null; } catch (_) { return null; }
  }
  function toast(message, error = false) {
    const item = document.createElement('div');
    item.className = `toast${error ? ' error' : ''}`;
    item.textContent = message;
    $('toast-stack').append(item);
    setTimeout(() => item.remove(), error ? 7000 : 4300);
  }
  async function request(path, options = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), options.timeout || 25000);
    const headers = { Accept: 'application/json', ...(options.body !== undefined ? { 'Content-Type': 'application/json' } : {}) };
    try {
      const response = await fetch(path, { ...options, body: options.body !== undefined ? JSON.stringify(options.body) : undefined, headers, signal: controller.signal, cache: 'no-store' });
      let data;
      try { data = await response.json(); } catch (_) { throw new Error(response.ok ? '服务返回的数据暂时无法读取。' : `请求未完成（${response.status}），请稍后重试。`); }
      if (!response.ok) {
        let message = data.error || data.detail || data.message;
        if (message && typeof message === 'object') message = message.message || JSON.stringify(message);
        throw new Error(message || `请求未完成（${response.status}），请稍后重试。`);
      }
      return data;
    } catch (error) {
      if (error.name === 'AbortError') throw new Error('本地服务响应超时，请稍后再试。');
      if (error instanceof TypeError) throw new Error('无法连接本地服务，请确认直播监控台仍在运行。');
      throw error;
    } finally { clearTimeout(timer); }
  }
  function preserveFocus(container, html) {
    const active = document.activeElement;
    const key = container.contains(active) ? active?.dataset?.focusKey : null;
    container.innerHTML = html;
    if (key) container.querySelector(`[data-focus-key="${CSS.escape(key)}"]`)?.focus({ preventScroll: true });
  }
  function renderConnection(connected, error) {
    model.connected = connected;
    $('service-status').className = `service-status ${connected ? 'connected' : 'disconnected'}`;
    $('service-status').innerHTML = `<i></i>${connected ? '本地服务已连接' : '本地服务未连接'}`;
    $('connection-notice').hidden = connected;
    if (!connected) $('connection-notice').textContent = `${error || '连接中断。'} ${model.state ? '当前展示的是上次读取的数据，将自动尝试重连。' : '将自动尝试重连。'}`;
    $('login-button').disabled = !connected;
    $('add-room').disabled = !connected;
    $('empty-add-room').disabled = !connected;
    $('pause-all').disabled = !connected || !model.state?.rooms?.length;
    $('start-all').disabled = !connected || !model.state?.rooms?.length;
    if (!connected && !model.state) {
      $('rooms-empty-title').textContent = '等待本地服务连接';
      $('rooms-empty-description').textContent = '服务连接恢复后，这里会自动显示你的直播间。';
      $('collection-status').textContent = '未连接';
    }
    if (!connected && model.state) $('collection-status').textContent = '连接中断';
  }
  function renderOverview(state) {
    const rooms = state.rooms || [];
    const enabled = rooms.filter((room) => room.enabled).length;
    $('stat-total').textContent = rooms.length;
    $('stat-enabled').textContent = `${enabled} 个正在监控`;
    $('stat-live').textContent = rooms.filter((room) => room.status === 'live' && !room.stale).length;
    $('stat-attention').textContent = rooms.filter((room) => ['error', 'blocked'].includes(room.status) || room.stale).length;
    $('collection-status').textContent = state.collector?.busy ? '正在采集' : enabled ? '等待下一轮' : rooms.length ? '监控已暂停' : '待添加直播间';
    $('last-sync').textContent = `面板同步 ${time(state.server_time || Date.now(), true)}`;
    $('room-count').textContent = rooms.length;
  }
  function renderRooms() {
    const rooms = model.state?.rooms || [];
    $('rooms-table-wrap').hidden = !rooms.length;
    $('rooms-empty').hidden = rooms.length > 0;
    if (!rooms.length && model.state) {
      $('rooms-empty-title').textContent = '从一个直播间开始';
      $('rooms-empty-description').textContent = '粘贴主播链接，开始记录每一次开播与人数变化。';
      $('empty-add-room').hidden = false;
    }
    const key = JSON.stringify([rooms, model.selected, model.connected, [...selectedRooms]]);
    if (model.tableKey === key) return;
    model.tableKey = key;
    preserveFocus($('room-rows'), rooms.map((room) => {
      const id = String(room.id), latest = room.latest || {}, name = roomName(room);
      const title = latest.title || (room.status === 'offline' ? '当前未开播' : '等待获取直播信息');
      const last = room.last_success;
      return `<tr class="${String(model.selected) === id ? 'selected' : ''}" data-room-id="${escape(id)}" aria-selected="${String(model.selected) === id}"><td class="select-col"><input type="checkbox" data-room-select="${escape(id)}" aria-label="选择 ${escape(name)}" ${selectedRooms.has(id) ? 'checked' : ''}></td><td><div class="account"><span class="account-avatar" aria-hidden="true">${escape(Array.from(name)[0] || '播')}</span><div class="account-info"><button type="button" class="account-name" data-action="select" data-id="${escape(id)}" data-focus-key="select-${escape(id)}" title="查看 ${escape(name)} 的详情">${escape(name)}</button><span class="account-title" title="${escape(title)}">${escape(title)}</span>${room.group_name ? `<small>${escape(room.group_name)}</small>` : ''}</div></div></td><td>${statusBadge(room)}${room.stale ? '<span class="stale-label">数据已过期</span>' : ''}</td><td class="numeric ${room.stale ? 'stale-number' : ''}" title="${isApprox(latest, 'online') ? '约数，按页面显示换算' : '最近成功采集的在线人数'}">${escape(onlineText(latest))}${room.stale && (numeric(latest.online) !== null || latest.online_display) ? '<span class="stale-label">上次数据</span>' : ''}</td><td class="numeric ${room.stale ? 'stale-number' : ''}" title="${isApprox(latest, 'likes') ? '约数，按页面显示换算' : '最近成功采集的累计点赞'}">${formatNumber(latest.likes)}</td><td class="time-cell" title="${escape(fullTime(last))}">${last ? `${day(last)} ${time(last, true)}` : '—'}<small>${room.stale ? '等待新的有效数据' : shortAge(last)}</small></td><td class="centered"><button type="button" class="switch" role="switch" aria-label="监控 ${escape(name)}" aria-checked="${Boolean(room.enabled)}" data-action="toggle" data-id="${escape(id)}" data-focus-key="toggle-${escape(id)}" ${!model.connected ? 'disabled' : ''}></button></td><td class="centered"><button type="button" class="switch" role="switch" aria-label="录像 ${escape(name)}" aria-checked="${Boolean(room.record_enabled)}" data-action="record" data-id="${escape(id)}" ${!model.connected ? 'disabled' : ''}></button><small class="record-cell-state">${!model.connected ? '未连接' : room.record_status?.state==='recording' ? '正在写入' : room.record_status?.state==='limit' ? '已到上限' : room.record_status?.state==='error' ? '录制异常' : room.record_enabled ? '等待音视频' : '未开启'}</small><button class="text-button record-location-button" type="button" data-action="record-location" data-id="${escape(id)}" aria-label="打开 ${escape(name)} 的录像位置" title="打开最近一次录像所在文件夹" ${!model.connected ? 'disabled' : ''}>打开位置</button></td><td><div class="row-actions"><button type="button" class="text-button" data-action="refresh" data-id="${escape(id)}" data-focus-key="refresh-${escape(id)}" aria-label="立即刷新 ${escape(name)}" ${!model.connected ? 'disabled' : ''}>刷新</button><button type="button" class="text-button remove-action" data-action="remove" data-id="${escape(id)}" data-focus-key="remove-${escape(id)}" aria-label="移除 ${escape(name)}" ${!model.connected ? 'disabled' : ''}>移除</button></div></td></tr>`;
    }).join(''));
  }
  function metric(label, value, primary, note) {
    return `<div class="detail-metric"><div class="metric-label">${label}</div><div class="metric-value ${primary ? 'primary' : ''}">${escape(value)}</div><div class="metric-note">${escape(note || '')}</div></div>`;
  }
  function renderGlobalRecording(){
    const rooms=model.state?.rooms||[];
    const enabled=rooms.filter(r=>r.record_enabled).length;
    const writing=rooms.filter(r=>r.record_status?.state==='recording').length;
    const failures=rooms.filter(r=>r.record_enabled&&r.record_status?.error).length;
    $('record-heading').textContent='全局录像设置';
    $('record-status').textContent=!model.connected?'服务未连接，暂时无法保存设置。':`已开启录像 ${enabled} 个账号 · 正在录制 ${writing} 个${failures?' · 异常 '+failures+' 个':''}。在直播间列表中单独开关录像。`;
    $('change-record-folder').disabled=!model.connected;
    const folderDraft=window.LiveMonitor?.recordingFolderDraft;
    $('record-folder-label').textContent=folderDraft?`待保存：${folderDraft}`:model.state?.settings?.recordings_dir?`统一保存到：${model.state.settings.recordings_dir}`:'首次录制前选择一次保存文件夹，所有账号共用。';
    const save=$('record-settings-form').querySelector('button[type="submit"]');save.disabled=!model.connected||$('record-settings-form').dataset.saving==='true';
    window.dispatchEvent(new Event('live-monitor-settings'));
  }
  function renderDetail() {
    const room = roomById(model.selected);
    renderGlobalRecording();
    $('detail-empty').hidden = Boolean(room);
    $('detail-content').hidden = !room;
    $('detail-links').hidden = !room;
    $('detail-status').innerHTML = room ? statusBadge(room) : '';
    if (!room) { model.settingsRoom = null; return; }
    const latest = room.latest || {};
    $('selected-name').textContent = roomName(room);
    $('selected-title').textContent = latest.title || (room.status === 'offline' ? '当前未开播，等待下一次直播。' : '直播信息将在采集成功后显示。');
    const link = safeExternalURL(room.url);
    $('open-room').hidden = !link;
    if (link) $('open-room').href = link;
    $('export-data').href = `/api/rooms/${encodeURIComponent(room.id)}/export.csv`;
    const warnings = [];
    if (!model.connected) warnings.push('与本地服务的连接已中断，以下数据未继续更新。');
    if (room.error) warnings.push(String(room.error));
    if (room.stale) warnings.push(`当前显示上次成功采集的数据（${fullTime(room.last_success)}），请留意更新时间。`);
    else if (!room.enabled) warnings.push('持续监控已暂停。开启账号旁的监控开关后，将按设定间隔自动采集。');
    $('room-warning').hidden = !warnings.length;
    $('room-warning').className = `room-warning${room.status === 'error' ? ' error' : ''}`;
    $('room-warning').textContent = warnings.join(' ');
    const note = (field) => room.stale ? '上次数据 · 已过期' : isApprox(latest, field) ? '约数，按页面显示换算' : numeric(latest[field]) === null ? '暂未获取' : '';
    $('detail-metrics').innerHTML = metric('在线人数', onlineText(latest), true, note('online')) + metric('累计点赞', formatNumber(latest.likes), false, note('likes')) + metric('累计观看', formatNumber(latest.total_viewers), false, note('total_viewers')) + metric('账号粉丝', formatNumber(latest.followers), false, note('followers'));
    window.dispatchEvent(new Event('live-monitor-selection'));
    renderSettings(room);
    renderTranscription(room);
    $('save-settings').disabled = !model.connected;
    loadHistory();
  }
  function renderSettings(room) {
    const key = JSON.stringify([room.interval_seconds, room.alert_above, room.alert_below, room.record_enabled]);
    $('settings-summary').textContent = `采集后等待 ${room.interval_seconds || 30} 秒${room.record_enabled ? ' · 自动录制' : ''}`;
    if (model.settingsRoom === room.id && (model.settingsKey === key || $('settings-form').dataset.dirty === 'true')) return;
    model.settingsRoom = room.id;
    model.settingsKey = key;
    $('setting-interval').value = room.interval_seconds || 30;
    $('setting-above').value = room.alert_above ?? '';
    $('setting-below').value = room.alert_below ?? '';
    $('settings-form').dataset.dirty = 'false';
    $('settings-message').textContent = '';
    $('settings-message').className = 'form-message';
  }
  function renderEvents() {
    const events = Array.isArray(model.state?.events) ? model.state.events : [];
    const sorted = events.slice().sort((a, b) => (dateOf(b.created_at)?.getTime() || 0) - (dateOf(a.created_at)?.getTime() || 0));
    $('event-count').textContent = sorted.length;
    const key = JSON.stringify([sorted, (model.state?.rooms || []).map((room) => [room.id, roomName(room)])]);
    if (model.eventsKey === key) return;
    model.eventsKey = key;
    if (!sorted.length) {
      $('events-list').innerHTML = '<div class="events-empty"><span aria-hidden="true">◷</span><strong>暂时没有动态</strong><p>新的直播变化和提醒会出现在这里。</p></div>';
      return;
    }
    $('events-list').innerHTML = sorted.slice(0, 100).map((event) => {
      const room = roomById(event.room_id);
      const kind = ['live', 'offline', 'error', 'blocked', 'alert'].includes(event.kind) ? event.kind : /alert|above|below/.test(event.kind || '') ? 'alert' : /error/.test(event.kind || '') ? 'error' : '';
      return `<article class="event-item"><span class="event-dot ${kind}" aria-hidden="true"></span><div><div class="event-name">${escape(room ? roomName(room) : event.room_id ? '已移除的直播间' : '监控台')}</div><p class="event-message">${escape(event.message || '监控状态已变化')}</p></div><time class="event-time" title="${escape(fullTime(event.created_at))}">${time(event.created_at, true)}<small>${day(event.created_at)}</small></time></article>`;
    }).join('');
    $('events-footer').textContent = sorted.length > 100 ? '显示最近 100 条动态 · 所有直播间' : '所有直播间的最近动态';
  }
  function processNotifications(events) {
    const eventKey = (event) => String(event.id ?? `${event.created_at}:${event.room_id}:${event.message}`);
    if (model.eventsSeen === null) { model.eventsSeen = new Set(events.map(eventKey)); return; }
    for (const event of events) {
      const key = eventKey(event);
      if (model.eventsSeen.has(key)) continue;
      model.eventsSeen.add(key);
      const actionable = /live|offline|alert|above|below|error|blocked|record/.test(event.kind || '');
      if (desktopEnabled && 'Notification' in window && Notification.permission === 'granted' && actionable) {
        const room = roomById(event.room_id);
        try {
          const notification = new Notification(room ? `直播监控台 · ${roomName(room)}` : '直播监控台', { body: event.message || '直播状态发生变化', tag: `live-monitor-${key}`, silent: false });
          notification.onclick = () => { window.focus(); if (room) selectRoom(room.id); notification.close(); };
        } catch (_) { /* Desktop notification support depends on browser configuration. */ }
      }
    }
    if (model.eventsSeen.size > 2000) model.eventsSeen = new Set([...model.eventsSeen].slice(-1000));
  }
  function renderNotificationButton() {
    const supported = 'Notification' in window;
    $('notifications-button').textContent = !supported ? '此浏览器不支持桌面提醒' : desktopEnabled && Notification.permission === 'granted' ? '桌面提醒已开启' : Notification.permission === 'denied' ? '桌面提醒已被阻止' : '开启桌面提醒';
    $('notifications-button').disabled = !supported;
    $('notifications-button').title = desktopEnabled ? '点击关闭桌面提醒' : '仅提醒开启后新发生的动态';
  }
  function selectRoom(id) {
    if (String(model.selected) === String(id)) return;
    model.selected = id;
    model.history = [];
    model.historyKey = '';
    model.historyRequest += 1;
    model.settingsRoom = null;
    model.settingsKey = '';
    $('settings-form').dataset.dirty = 'false';
    renderRooms(); renderDetail();
  }
  function chartEmpty(title, description = '') {
    $('chart-host').innerHTML = `<div class="chart-empty"><strong>${escape(title)}</strong>${description ? `<p>${escape(description)}</p>` : ''}</div>`;
  }
  function compactNumber(n) {
    if (Math.abs(n) >= 100000000) return `${Number((n / 100000000).toFixed(1))}亿`;
    if (Math.abs(n) >= 10000) return `${Number((n / 10000).toFixed(1))}万`;
    return numberFormat.format(n);
  }
  function renderChart(points) {
    const room = roomById(model.selected);
    const clean = points.map((point) => ({ ...point, t: dateOf(point.observed_at)?.getTime() })).filter((point) => Number.isFinite(point.t)).sort((a, b) => a.t - b.t);
    const data = clean.filter((point) => numeric(point.online) !== null && Number(point.online) >= 0 && !['error', 'blocked', 'unknown'].includes(point.status));
    $('chart-caption').textContent = data.length ? `${data.length} 个采集点 · 中断时保留空缺` : '采集中断时，曲线保留空缺';
    if (!data.length) {
      chartEmpty('这个时间段还没有人数数据', room?.status === 'offline' ? '当前未开播，开播并采集成功后开始绘制。' : room?.status === 'blocked' || room?.status === 'error' ? '采集恢复后，将显示真实的人数变化。' : '首次获取在线人数后，这里会出现第一个采集点。');
      return;
    }
    const width = Math.max(300, $('chart-host').clientWidth || 600), height = $('chart-host').clientHeight || 218;
    const pad = { top: 18, right: 13, bottom: 27, left: 42 }, plotW = width - pad.left - pad.right, plotH = height - pad.top - pad.bottom;
    const first = clean[0].t, last = clean[clean.length - 1].t;
    const minT = data.length === 1 ? first - 60000 : first;
    const maxT = data.length === 1 ? last + 60000 : Math.max(first + 1000, last);
    const maxValue = Math.max(...data.map((point) => Number(point.online)), 1);
    const order = Math.pow(10, Math.floor(Math.log10(maxValue)));
    const maxY = Math.ceil(maxValue * 1.15 / order) * order;
    const x = (t) => pad.left + ((t - minT) / (maxT - minT)) * plotW;
    const y = (value) => pad.top + plotH * (1 - value / maxY);
    const green = '#226648';
    let svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${width} ${height}" role="img" aria-label="${escape(roomName(room))} 最近 ${model.hours} 小时在线人数趋势，共 ${data.length} 个有效采集点"><title>在线人数趋势</title><desc>仅显示实际采集数据，采集中断与缺失数据之间不连接。横轴为采集时间，纵轴为在线人数。</desc>`;
    for (let i = 0; i <= 4; i++) {
      const value = maxY * i / 4, lineY = y(value);
      svg += `<line x1="${pad.left}" y1="${lineY.toFixed(2)}" x2="${width - pad.right}" y2="${lineY.toFixed(2)}" stroke="#edf1e7" stroke-width="1"/><text x="${pad.left - 9}" y="${(lineY + 3).toFixed(2)}" fill="#98a18d" text-anchor="end" font-family="Consolas,monospace" font-size="9">${compactNumber(value)}</text>`;
    }
    const tickCount = width < 400 ? 3 : 5;
    for (let i = 0; i < tickCount; i++) {
      const t = minT + (maxT - minT) * i / (tickCount - 1);
      svg += `<text x="${x(t).toFixed(2)}" y="${height - 6}" fill="#98a18d" text-anchor="${i === 0 ? 'start' : i === tickCount - 1 ? 'end' : 'middle'}" font-family="Consolas,monospace" font-size="9">${time(t, maxT - minT < 600000)}</text>`;
    }
    const gapLimit = Math.max(90000, (room?.interval_seconds || 30) * 1000 * 2.5);
    let segments = [], current = [], previous = null;
    for (const point of clean) {
      const online = numeric(point.online);
      const valid = online !== null && online >= 0 && !['error', 'blocked', 'unknown'].includes(point.status);
      const discontinuity = previous && (point.t - previous.t > gapLimit || (point.status && previous.status && point.status !== previous.status) || (point.session_id && previous.session_id && point.session_id !== previous.session_id));
      if (!valid || discontinuity) { if (current.length) segments.push(current); current = []; }
      if (valid) current.push({ ...point, online });
      previous = point;
    }
    if (current.length) segments.push(current);
    for (const segment of segments) {
      if (segment.length > 1) {
        const path = segment.map((point, i) => `${i === 0 ? 'M' : 'L'}${x(point.t).toFixed(2)},${y(point.online).toFixed(2)}`).join(' ');
        svg += `<path d="${path}" stroke="${green}" stroke-width="1.8" fill="none" stroke-linejoin="round" stroke-linecap="round"/>`;
      }
      const dotPoints = data.length > 150 && segment.length > 2 ? [segment[0], segment[segment.length - 1]] : segment;
      for (const point of dotPoints) svg += `<circle cx="${x(point.t).toFixed(2)}" cy="${y(point.online).toFixed(2)}" r="${data.length === 1 ? 4 : 2.2}" fill="${green}" stroke="white" stroke-width="1"><title>${escape(fullTime(point.observed_at))} · 在线 ${formatNumber(point.online)} 人</title></circle>`;
    }
    svg += '</svg>';
    $('chart-host').innerHTML = svg;
  }
  async function loadHistory(force = false) {
    const room = roomById(model.selected);
    if (!room) return;
    const key = `${room.id}:${model.hours}`;
    const now = Date.now();
    if (!force && key === model.historyKey && now - model.historyTime < 12000) return;
    const changed = key !== model.historyKey;
    model.historyKey = key;
    model.historyTime = now;
    const sequence = ++model.historyRequest;
    if (changed) chartEmpty('正在读取采集记录');
    try {
      const result = await request(`/api/rooms/${encodeURIComponent(room.id)}/history?hours=${model.hours}`);
      if (sequence !== model.historyRequest || key !== `${model.selected}:${model.hours}`) return;
      model.history = Array.isArray(result.points) ? result.points : [];
      renderChart(model.history);
    } catch (error) {
      if (sequence !== model.historyRequest) return;
      chartEmpty('暂时无法读取走势', error.message);
      $('chart-caption').textContent = '连接恢复后会自动重试';
    }
  }
  async function syncState() {
    if (model.syncPending) { model.syncAgain = true; return; }
    model.syncPending = true;
    try {
      const state = await request('/api/state');
      if (!state || !Array.isArray(state.rooms)) throw new Error('直播间列表暂时无法读取，请稍后再试。');
      model.state = state;
      if (!state.rooms.some((room) => String(room.id) === String(model.selected))) {
        model.selected = state.rooms[0]?.id ?? null;
        model.historyKey = '';
        model.historyRequest += 1;
        model.settingsRoom = null;
        $('settings-form').dataset.dirty = 'false';
      }
      renderConnection(true);
      $('background-status').textContent = state.settings?.autostart ? '后台守护已启用 · 登录 Windows 后自动启动 · 关机或休眠期间无法采集' : '未设置登录后自动启动 · 双击启动入口可启用后台守护';
      renderOverview(state); renderRooms(); renderDetail(); renderEvents();
      processNotifications(Array.isArray(state.events) ? state.events : []);
    } catch (error) {
      renderConnection(false, error.message);
      renderRooms();
      if (model.state) renderDetail();
    } finally {
      model.syncPending = false;
      if (model.syncAgain) { model.syncAgain = false; syncState(); }
    }
  }
  async function mutate(path, options, successMessage, button) {
    if (button) button.disabled = true;
    try {
      const result = await request(path, options);
      if (successMessage) toast(successMessage);
      await syncState();
      return result;
    } catch (error) { toast(error.message, true); throw error; }
    finally { if (button?.isConnected) button.disabled = false; }
  }
  function openAdd() {
    $('add-form').reset(); $('add-error').hidden = true;
    $('add-dialog').showModal();
  }

  $('add-room').addEventListener('click', openAdd);
  $('empty-add-room').addEventListener('click', openAdd);
  document.querySelectorAll('.close-dialog').forEach((button) => button.addEventListener('click', () => button.closest('dialog').close()));
  document.querySelectorAll('dialog').forEach((dialog) => dialog.addEventListener('click', (event) => {
    if (event.target !== dialog) return;
    const box = dialog.getBoundingClientRect();
    if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) dialog.close();
  }));
  $('add-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    if (!$('add-form').reportValidity()) return;
    $('add-error').hidden = true; $('add-submit').disabled = true;
    try {
      const input=$('room-url').value.trim();
      const multiple=input.split(/\r?\n/).filter(x=>x.trim()).length>1||/^[{\[]/.test(input);
      if(multiple){
        const result=await request('/api/rooms/import',{method:'POST',body:{text:input}});
        await syncState();
        const message=`新增 ${result.added.length} 个，跳过重复 ${result.duplicates.length} 个，失败 ${result.errors.length} 个。`;
        if(result.errors.length){$('add-error').textContent=message+result.errors.map(r=>`第 ${r.line} 行：${r.error}`).join(' ');$('add-error').hidden=false;}
        else{$('add-dialog').close();toast(message);}
        return;
      }
      const result = await request('/api/rooms', { method: 'POST', body: { url: input, name: $('room-name').value.trim() } });
      if (result.room?.id !== undefined) model.selected = result.room.id;
      model.settingsRoom = null; model.historyKey = '';
      $('add-dialog').close();
      toast('直播间已添加，正在等待首次检查。');
      await syncState();
    } catch (error) { $('add-error').textContent = error.message; $('add-error').hidden = false; }
    finally { $('add-submit').disabled = false; }
  });
  $('room-rows').addEventListener('click', async (event) => {
    const button = event.target.closest('button[data-action]');
    const row = event.target.closest('tr[data-room-id]');
    if (event.target.closest('[data-room-select]')) return;
    if (!button && row) { selectRoom(row.dataset.roomId); return; }
    if (!button || button.disabled) return;
    const { action, id } = button.dataset;
    const room = roomById(id);
    if (!room) return;
    if (action === 'record') { await toggleRecording(room,button); return; }
    if (action === 'record-location') { await openRecordingLocation(`/api/rooms/${encodeURIComponent(id)}/open-recordings`,button); return; }
    if (action === 'select') { selectRoom(id); return; }
    if (action === 'remove') {
      model.removeId = id; $('remove-error').hidden = true;
      $('remove-description').textContent = `确认移除「${roomName(room)}」？这会停止并移出采集列表；直播档案、历史数据、话术和录像均保留。`;
      $('remove-dialog').showModal(); return;
    }
    try {
      if (action === 'toggle') await mutate(`/api/rooms/${encodeURIComponent(id)}`, { method: 'PATCH', body: { enabled: !room.enabled } }, room.enabled ? '已暂停该直播间的持续监控。' : '已开始持续监控。', button);
      if (action === 'refresh') await mutate(`/api/rooms/${encodeURIComponent(id)}/refresh`, { method: 'POST', body: {} }, '已加入采集队列，结果将在完成后更新。', button);
    } catch (_) { /* The mutation helper already presents the error. */ }
  });
  $('remove-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    if (model.removeId === null) return;
    $('remove-submit').disabled = true;
    try {
      await request(`/api/rooms/${encodeURIComponent(model.removeId)}`, { method: 'DELETE' });
      $('remove-dialog').close(); model.removeId = null;
      toast('直播间已移除。'); await syncState();
    } catch (error) { $('remove-error').textContent = error.message; $('remove-error').hidden = false; }
    finally { $('remove-submit').disabled = false; }
  });
  for (const [id, enabled] of [['start-all', true], ['pause-all', false]]) $(id).addEventListener('click', async () => {
    try { await mutate('/api/control', { method: 'POST', body: { enabled } }, enabled ? '已开始监控所有直播间。' : '已暂停所有直播间的持续监控。', $(id)); } catch (_) { /* Already shown. */ }
  });
  $('login-button').addEventListener('click', async () => {
    const button = $('login-button'); button.disabled = true;
    try {
      await request('/api/login', { method: 'POST', body: { ...(roomById(model.selected)?.url ? { url: roomById(model.selected).url } : {}) }, timeout: 60000 });
      toast('已请求打开登录浏览器，请在浏览器中完成登录。');
    } catch (error) { toast(error.message, true); }
    finally { button.disabled = !model.connected; }
  });
  $('settings-form').addEventListener('input', () => { $('settings-form').dataset.dirty = 'true'; $('settings-message').textContent = '有未保存的更改'; $('settings-message').className = 'form-message'; });
  $('settings-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    if (!$('settings-form').reportValidity()) return;
    const room = roomById(model.selected);
    if (!room) return;
    const above = $('setting-above').value.trim() === '' ? null : Number($('setting-above').value);
    const below = $('setting-below').value.trim() === '' ? null : Number($('setting-below').value);
    if (above !== null && below !== null && below >= above) {
      $('settings-message').textContent = '低于提醒的人数需小于高于提醒的人数。'; $('settings-message').className = 'form-message error'; return;
    }
    $('save-settings').disabled = true;
    $('settings-message').textContent = '正在保存…';
    try {
      await request(`/api/rooms/${encodeURIComponent(room.id)}`, { method: 'PATCH', body: { interval_seconds: Number($('setting-interval').value), alert_above: above, alert_below: below, record_enabled: room.record_enabled } });
      if (String(model.selected) === String(room.id)) { $('settings-form').dataset.dirty = 'false'; model.settingsKey = ''; }
      await syncState();
      $('settings-message').textContent = '设置已保存'; $('settings-message').className = 'form-message';
    } catch (error) { $('settings-message').textContent = error.message; $('settings-message').className = 'form-message error'; }
    finally { $('save-settings').disabled = !model.connected; }
  });
  document.querySelectorAll('[data-hours]').forEach((button) => button.addEventListener('click', () => {
    model.hours = Number(button.dataset.hours);
    document.querySelectorAll('[data-hours]').forEach((item) => { const selected = item === button; item.classList.toggle('active', selected); item.setAttribute('aria-pressed', String(selected)); });
    loadHistory(true);
  }));
  $('notifications-button').addEventListener('click', async () => {
    if (!('Notification' in window)) return;
    if (desktopEnabled && Notification.permission === 'granted') { desktopEnabled = false; toast('已关闭桌面提醒，动态仍会保留在监控台。'); }
    else if (Notification.permission === 'denied') { toast('浏览器已阻止桌面提醒，可在地址栏的网站设置中允许通知。', true); return; }
    else {
      try {
        const permission = Notification.permission === 'granted' ? 'granted' : await Notification.requestPermission();
        desktopEnabled = permission === 'granted';
        toast(desktopEnabled ? '桌面提醒已开启，仅提醒接下来新发生的动态。' : '尚未允许桌面提醒，动态仍会保留在监控台。');
      } catch (_) { toast('当前浏览器无法开启桌面提醒，动态仍会保留在监控台。', true); }
    }
    try { localStorage.setItem('live-monitor-desktop', String(desktopEnabled)); } catch (_) { /* Optional preference persistence. */ }
    renderNotificationButton();
  });

  const transcriptState = { busy: false, room: null, key: '', older: [], pageMore: null, day: '' };
  const transcriptQuery = () => transcriptState.day ? `?day=${encodeURIComponent(transcriptState.day)}` : '';
  const transcriptURL = (id) => `/api/rooms/${encodeURIComponent(id)}/transcript.txt${transcriptQuery()}`;
  function renderTranscription(room) {
    const state = room.transcription_status || {};
    const count = state.counts || {};
    const audioActive = state.audio_state === 'recording';
    const caption = !model.connected ? '连接中断 · 数据已停止更新' : state.processing ? '正在识别话术' : audioActive ? '正在采集音频' : room.transcribe_enabled && room.enabled ? (room.status === 'offline' ? '等待主播开播' : room.latest?.stream_available === false ? '正在解析直播音频' : '等待直播音频') : '转写已暂停';
    $('transcription-status').textContent = `${caption} · 已识别 ${count.done || 0} 段${count.pending ? ` · 排队 ${count.pending} 段` : ''}`;
    $('toggle-transcription').textContent = room.transcribe_enabled ? '暂停转写' : '开始转写';
    $('toggle-transcription').disabled = !model.connected;
    for (const id of ['export-transcript','copy-transcript','save-transcript-local','transcript-day','transcript-more']) $(id).disabled = !model.connected;
    $('transcript-error').hidden = !state.error;
    $('transcript-error').textContent = state.error || '';
    $('retry-transcript').hidden = !state.error;
    if (String(transcriptState.room) !== String(room.id)) {
      transcriptState.room = room.id; transcriptState.key = ''; transcriptState.older = []; transcriptState.day = ''; transcriptState.pageMore = null;
      $('transcript-day').innerHTML = '<option value="">全部日期</option>';
      $('transcript-list').innerHTML = '<p class="transcript-empty">正在读取话术记录…</p>';
    }
    if (model.connected && !transcriptState.busy) loadTranscripts(room.id);
  }
  async function loadTranscripts(id, more = false) {
    const selectedDay = transcriptState.day;
    transcriptState.busy = true;
    try {
      let query = transcriptQuery();
      if (more && transcriptState.older.length) query += `${query ? '&' : '?'}before=${Math.min(...transcriptState.older.map(x => x.id))}`;
      const result = await request(`/api/rooms/${encodeURIComponent(id)}/transcripts${query}`);
      if (String(model.selected) !== String(id) || selectedDay !== transcriptState.day) return;
      const byId = new Map(transcriptState.older.map(x => [x.id, x]));
      for (const item of result.items || []) byId.set(item.id, item);
      const items = [...byId.values()].sort((a,b) => a.id-b.id);
      transcriptState.older = items;
      if (more) transcriptState.pageMore = result.has_more;
      $('transcript-more').hidden = !(transcriptState.pageMore ?? result.has_more);
      $('transcript-range').textContent = `已显示 ${items.length} 段 · ${selectedDay || '全部日期'}`;
      $('transcript-error').hidden = !result.summary?.error;
      $('transcript-error').textContent = result.summary?.error || '';
      const dates = await request(`/api/rooms/${encodeURIComponent(id)}/transcript-days`);
      if (String(model.selected) !== String(id) || selectedDay !== transcriptState.day) return;
      const choices = '<option value="">全部日期</option>' + dates.days.map(x => `<option value="${escape(x.day)}">${escape(x.day)}（${x.chunks} 段）</option>`).join('');
      if ($('transcript-day').innerHTML !== choices) { $('transcript-day').innerHTML = choices; $('transcript-day').value = selectedDay; }

      const key = JSON.stringify([id, items]);
      if (transcriptState.key === key) return;
      transcriptState.key = key;
      const list = $('transcript-list');
      const nearBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 60;
      if (!items.length) {
        list.innerHTML = '<p class="transcript-empty">开启后，从当前直播开始积累话术记录。<br>首段音频完成采集和识别后，文字会自动出现在这里。</p>';
      } else {
        const states = { pending: '音频已保存，等待识别…', processing: '正在识别…', silent: '此段未检测到人声', error: '此段转写失败，可点击重试' };
        list.innerHTML = items.map(item => `<article class="transcript-row ${escape(item.status)}"><time title="${escape(fullTime(item.captured_at))}">${escape(day(item.captured_at))} ${time(item.captured_at,true)}</time><p>${escape(item.status === 'done' ? item.text : states[item.status] || '等待处理')}</p></article>`).join('');
        if (nearBottom) list.scrollTop = list.scrollHeight;
      }
    } catch (error) {
      $('transcript-error').hidden = false; $('transcript-error').textContent = error.message;
    } finally { transcriptState.busy = false; }
  }
  $('toggle-transcription').addEventListener('click', async () => {
    const room = roomById(model.selected); if (!room) return;
    try {
      await mutate(`/api/rooms/${encodeURIComponent(room.id)}`, { method:'PATCH', body:room.transcribe_enabled ? {transcribe_enabled:false} : {transcribe_enabled:true,enabled:true} }, room.transcribe_enabled ? '已停止采音，保存好的音频片段会继续转写。' : '已开启本地话术转写，首段文字稍后出现。', $('toggle-transcription'));
    } catch (_) { }
  });
  $('retry-transcript').addEventListener('click', async () => {
    const room = roomById(model.selected); if (!room) return;
    try { await mutate(`/api/rooms/${encodeURIComponent(room.id)}/transcripts/retry`, {method:'POST',body:{}}, '失败片段已加入重试队列。', $('retry-transcript')); } catch (_) { }
  });
  $('copy-transcript').addEventListener('click', async () => {
    const room = roomById(model.selected); if (!room) return;
    try {
      const response = await fetch(transcriptURL(room.id),{cache:'no-store'});
      if (!response.ok) throw new Error('无法读取话术记录');
      await navigator.clipboard.writeText((await response.text()).replace(/^\uFEFF/,''));
      toast('话术全文已复制。');
    } catch (_) { toast('未能复制，请使用“导出 TXT”保存话术。',true); }
  });

  $('transcript-day').addEventListener('change', () => {
    transcriptState.day = $('transcript-day').value; transcriptState.older = []; transcriptState.pageMore = null; transcriptState.key = '';
    loadTranscripts(model.selected);
  });
  $('transcript-more').addEventListener('click', () => { if (!transcriptState.busy) loadTranscripts(model.selected, true); });
  async function downloadText(url, filename, button) {
    button.disabled = true;
    try {
      const response = await fetch(url, {cache:'no-store', signal:AbortSignal.timeout(20000)});
      if (!response.ok) throw new Error(`导出未完成（${response.status}）`);
      const blob = await response.blob();
      const objectURL = URL.createObjectURL(blob);
      const a = document.createElement('a'); a.href = objectURL; a.download = filename; document.body.append(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(objectURL), 60000);
      toast('文件已准备好，已交给浏览器保存；也可用“保存到本机”直接落盘。');
    } catch (error) { toast('下载未完成：本地服务可能已断开。连接恢复后重试，或点击“保存到本机”。', true); }
    finally { button.disabled = !model.connected; }
  }
  $('export-transcript').addEventListener('click', () => {
    const room = roomById(model.selected); if (!room) return;
    downloadText(transcriptURL(room.id), `话术_${room.id}_${transcriptState.day || '全部日期'}.txt`, $('export-transcript'));
  });
  $('export-data').addEventListener('click', event => {
    event.preventDefault(); const room = roomById(model.selected); if (!room || !model.connected) return;
    downloadText(`/api/rooms/${encodeURIComponent(room.id)}/export.csv`, `直播数据_${room.id}.csv`, $('export-data'));
  });
  $('save-transcript-local').addEventListener('click', async () => {
    const room = roomById(model.selected); if (!room) return;
    const button = $('save-transcript-local'); button.disabled = true;
    try {
      const result = await request(`/api/rooms/${encodeURIComponent(room.id)}/save-transcript`, {method:'POST',body:{day:transcriptState.day}});
      $('export-result').hidden = false; $('export-result').textContent = `已保存到：${result.path}`;
      toast(`已保存：${result.path}`);
    } catch (error) { toast(error.message, true); }
    finally { button.disabled = !model.connected; }
  });
  function openRecordFolder() {
    $('record-folder-path').value = window.LiveMonitor?.recordingFolderDraft || model.state?.settings?.recordings_dir || '';
    $('record-folder-error').textContent = '';
    $('folder-browser').hidden=true;
    $('save-record-folder').textContent = $('record-settings-dialog').open ? '使用此文件夹' : '保存文件夹';
    $('record-folder-dialog').showModal();
  }
  $('change-record-folder').addEventListener('click', () => openRecordFolder());
  async function openRecordingLocation(url,button) {
    button.disabled=true;
    try { const result=await request(url,{method:'POST',body:{}});toast(`已打开录像文件夹：${result.path}`); }
    catch(error){toast(error.message,true);}
    finally{button.disabled=!model.connected;}
  }
  $('open-recording-root').addEventListener('click',event=>openRecordingLocation('/api/recordings/open-folder',event.currentTarget));
  async function toggleRecording(room,button) {
    if (!room) return;
    if (!room.record_enabled && !model.state?.settings?.recordings_dir) { window.LiveMonitor.openRecordingSettings(room.id); return; }
    try { await mutate(`/api/rooms/${encodeURIComponent(room.id)}`, {method:'PATCH',body:room.record_enabled ? {record_enabled:false} : {record_enabled:true,enabled:true}}, room.record_enabled ? '已关闭视频录制，已有视频保留。' : '自动录制已开启，收到直播画面后开始保存。', button); } catch (_) { }
  }
  let folderBrowserState=null,folderBrowseSequence=0;
  async function browseFolders(path='') {
    const sequence=++folderBrowseSequence;
    $('folder-browser').hidden=false;$('folder-browser-list').textContent='正在读取文件夹…';
    $('folder-browser-use').disabled=true;$('folder-browser-parent').disabled=true;
    $('record-folder-error').textContent='';
    try {
      const result=await request('/api/folders'+(path?'?path='+encodeURIComponent(path):''),{timeout:20000});
      if(sequence!==folderBrowseSequence)return;
      folderBrowserState=result;
      $('folder-browser-current').textContent=result.path||'选择本机磁盘';
      $('folder-browser-list').innerHTML=result.directories.map(item=>`<button class="folder-browser-item" type="button" data-folder-path="${escape(item.path)}"><span>${escape(item.name)}</span><span aria-hidden="true">›</span></button>`).join('')||'<p class="muted">这里没有子文件夹，可以选择当前文件夹。</p>';
      $('folder-browser-use').disabled=!result.path;$('folder-browser-parent').disabled=!result.path;
    } catch(error) {
      if(sequence!==folderBrowseSequence)return;
      $('folder-browser-list').textContent='读取失败，可返回本机磁盘重新选择，或直接填写完整路径。';
      $('record-folder-error').textContent=error.message;
    }
  }
  $('browse-record-folder').addEventListener('click',()=>browseFolders());
  $('folder-browser-roots').addEventListener('click',()=>browseFolders());
  $('folder-browser-parent').addEventListener('click',()=>browseFolders(folderBrowserState?.parent||''));
  $('folder-browser-list').addEventListener('click',event=>{const button=event.target.closest('[data-folder-path]');if(button)browseFolders(button.dataset.folderPath);});
  $('folder-browser-use').addEventListener('click',()=>{
    if(folderBrowserState?.path){$('record-folder-path').value=folderBrowserState.path;$('folder-browser').hidden=true;$('save-record-folder').focus();}
  });
  $('record-folder-dialog').addEventListener('close',()=>{folderBrowseSequence++;});
  $('record-folder-form').addEventListener('submit', async event => {
    event.preventDefault(); if (!$('record-folder-form').reportValidity()) return;
    const button = $('save-record-folder'); button.disabled = true;
    try {
      const path=$('record-folder-path').value.trim();
      if ($('record-settings-dialog').open) {
        window.LiveMonitor.recordingFolderDraft=path;
        $('record-settings-form').dataset.dirty='true';
        $('record-settings-error').textContent='文件夹已选择，请保存录像设置后生效。';
        $('record-folder-dialog').close();renderGlobalRecording();return;
      }
      await request('/api/recording-folder', {method:'POST',body:{path}});
      $('record-folder-dialog').close(); await syncState(); toast('保存文件夹已更新。');
    } catch (error) { $('record-folder-error').textContent = error.message; }
    finally { button.disabled = false; }
  });

  window.LiveMonitor = {model,selectedRooms,request,toast,syncState,roomById,roomName,escape,time,fullTime,downloadText,openRecordFolder,openRecordingLocation,renderRooms};
  let resizeTimer;
  window.addEventListener('resize', () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(() => { if (model.history.length && roomById(model.selected)) renderChart(model.history); }, 150); });
  document.addEventListener('visibilitychange', () => { if (!document.hidden) syncState(); });
  renderNotificationButton(); renderEvents();
  $('start-all').disabled = true; $('pause-all').disabled = true;
  syncState();
  setInterval(syncState, 4000);
})();
