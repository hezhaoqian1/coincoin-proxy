(function () {
  const POLL_INTERVAL_MS = 15000;
  let latestOverview = null;
  let pollTimer = null;
  let loading = false;
  let alertPolicyDirty = false;
  let alertEditRevision = 0;
  let alertConfigLoadGeneration = 0;
  let alertActionRunning = false;
  const runningMonitors = new Set();

  function html(value) {
    if (typeof window.escapeHtml === 'function') return window.escapeHtml(value == null ? '' : String(value));
    return String(value == null ? '' : value)
      .replaceAll('&', '&amp;')
      .replaceAll('<', '&lt;')
      .replaceAll('>', '&gt;')
      .replaceAll('"', '&quot;')
      .replaceAll("'", '&#039;');
  }

  function number(value) {
    const parsed = Number(value || 0);
    return Number.isFinite(parsed) ? parsed.toLocaleString('zh-CN') : '0';
  }

  function percent(value) {
    const parsed = Number(value || 0);
    return `${(parsed * 100).toFixed(parsed > 0 && parsed < 0.01 ? 2 : 1)}%`;
  }

  function latency(value) {
    const parsed = Number(value || 0);
    return parsed > 0 ? `${Math.round(parsed).toLocaleString('zh-CN')} ms` : '-';
  }

  function dateTime(value) {
    if (!value) return '-';
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return html(value);
    return parsed.toLocaleString('zh-CN', { hour12: false });
  }

  function statusMeta(status) {
    const normalized = String(status || 'pending').toLowerCase();
    const labels = {
      operational: '正常',
      degraded: '降级',
      cooling: '冷却',
      failed: '故障',
      pending: '待检测',
      unconfigured: '未配置',
      disabled: '已停用',
    };
    return { value: normalized, label: labels[normalized] || normalized };
  }

  function statusBadge(status) {
    const meta = statusMeta(status);
    return `<span class="sr-status sr-status-${html(meta.value)}">${html(meta.label)}</span>`;
  }

  function pageIsActive() {
    return document.getElementById('page-service-reliability')?.classList.contains('active') === true;
  }

  function schedulePoll() {
    if (pollTimer) window.clearTimeout(pollTimer);
    pollTimer = null;
    if (document.hidden || !pageIsActive()) return;
    pollTimer = window.setTimeout(() => loadServiceReliability(), POLL_INTERVAL_MS);
  }

  function alertCategoryLabel(category) {
    return {
      availability: '可用性',
      rate_limit: '429 限流',
      authentication: '鉴权',
      fallback_exhausted: 'Fallback 全部失败',
      configuration_test: '配置测试',
    }[String(category || '')] || String(category || '-');
  }

  function alertDeliveryBadge(deliveryStatus) {
    const normalized = String(deliveryStatus || 'pending');
    const mappedStatus = normalized === 'sent' ? 'operational' : normalized;
    const labels = { pending: '发送中', sent: '已送达', failed: '失败' };
    return `<span class="sr-status sr-status-${html(mappedStatus)}">${html(labels[normalized] || normalized)}</span>`;
  }

  function renderAlertConfig(payload) {
    const state = document.getElementById('serviceReliabilityAlertState');
    const webhookState = document.getElementById('serviceReliabilityAlertWebhookState');
    const enabled = payload.enabled === true;
    const configured = payload.webhook_configured === true;
    state.className = `sr-status sr-status-${enabled ? 'operational' : 'disabled'}`;
    state.textContent = enabled ? '告警已启用' : '告警已停用';
    webhookState.className = `sr-status sr-status-${configured ? 'operational' : 'unconfigured'}`;
    webhookState.textContent = configured ? 'Webhook 已配置' : 'Webhook 未配置';
    document.getElementById('serviceReliabilityAlertTest').disabled = alertActionRunning || !configured;

    if (!alertPolicyDirty) {
      document.getElementById('serviceReliabilityAlertEnabled').checked = enabled;
      document.getElementById('serviceReliabilityAlertWebhookUrl').value = payload.webhook_url ?? '';
      document.getElementById('serviceReliabilityAlertAvailabilityThreshold').value = payload.availability_threshold ?? '';
      document.getElementById('serviceReliabilityAlertAuthenticationThreshold').value = payload.authentication_threshold ?? '';
      document.getElementById('serviceReliabilityAlertWindowSeconds').value = payload.window_seconds ?? '';
      document.getElementById('serviceReliabilityAlertDedupSeconds').value = payload.dedup_seconds ?? '';
      document.getElementById('serviceReliabilityAlertMaxPendingTasks').value = payload.max_pending_tasks ?? '';
    }
    document.getElementById('serviceReliabilityAlertDeliverySummary').textContent =
      `最近成功：${dateTime(payload.last_success_at)} · 最近失败：${dateTime(payload.last_failure_at)}`;
  }

  function renderAlertEvents(payload) {
    const events = payload.events || [];
    const body = document.getElementById('serviceReliabilityAlertEventsBody');
    body.innerHTML = events.length ? events.map(item => {
      const trigger = item.failure_count
        ? `${number(item.failure_count)} 次 / ${number(item.window_seconds)} 秒`
        : '-';
      const result = item.delivery_status === 'failed'
        ? (item.error_summary || `HTTP ${number(item.response_status)}`)
        : (item.response_status ? `HTTP ${number(item.response_status)}` : '-');
      return `
        <tr>
          <td>${dateTime(item.created_at)}</td>
          <td><div class="sr-primary">${html(alertCategoryLabel(item.category))}</div><div class="sr-secondary">${html(item.alert_type || '-')}</div></td>
          <td>${alertDeliveryBadge(item.delivery_status)}</td>
          <td><div class="sr-primary">${html(item.model || '-')}</div><div class="sr-secondary">${html(item.endpoint || '-')} · HTTP ${number(item.status_code)}</div></td>
          <td>${html(item.channel_id || '-')}</td>
          <td>${html(trigger)}</td>
          <td><span class="sr-alert-request-id">${html(item.request_id || '-')}</span></td>
          <td><div>${html(result)}</div><div class="sr-secondary">${dateTime(item.completed_at)}</div></td>
        </tr>
      `;
    }).join('') : '<tr><td colspan="8"><div class="sr-empty">当前筛选条件下还没有推送记录</div></td></tr>';
  }

  async function loadServiceReliabilityAlerts() {
    if (!pageIsActive()) return;
    const loadGeneration = ++alertConfigLoadGeneration;
    const category = document.getElementById('serviceReliabilityAlertCategoryFilter')?.value || '';
    const deliveryStatus = document.getElementById('serviceReliabilityAlertStatusFilter')?.value || '';
    const params = new URLSearchParams({ limit: '50' });
    if (category) params.set('category', category);
    if (deliveryStatus) params.set('delivery_status', deliveryStatus);
    try {
      const [configResponse, eventsResponse] = await Promise.all([
        fetch('/admin/alerts/config', { cache: 'no-store', headers: window.adminHeaders() }),
        fetch(`/admin/alerts/events?${params.toString()}`, { cache: 'no-store', headers: window.adminHeaders() }),
      ]);
      const [config, events] = await Promise.all([configResponse.json(), eventsResponse.json()]);
      if (loadGeneration !== alertConfigLoadGeneration) return;
      if (!configResponse.ok) throw new Error(config.detail || '告警配置加载失败');
      if (!eventsResponse.ok) throw new Error(events.detail || '告警历史加载失败');
      renderAlertConfig(config);
      renderAlertEvents(events);
    } catch (error) {
      if (loadGeneration !== alertConfigLoadGeneration) return;
      console.error(error);
      const body = document.getElementById('serviceReliabilityAlertEventsBody');
      if (body) body.innerHTML = `<tr><td colspan="8"><div class="sr-error">${html(error.message || '告警数据加载失败')}</div></td></tr>`;
    }
  }

  function pythonIsSpaceCodePoint(codePoint) {
    return (codePoint >= 0x0009 && codePoint <= 0x000d)
      || (codePoint >= 0x001c && codePoint <= 0x0020)
      || codePoint === 0x0085
      || codePoint === 0x00a0
      || codePoint === 0x1680
      || (codePoint >= 0x2000 && codePoint <= 0x200a)
      || (codePoint >= 0x2028 && codePoint <= 0x2029)
      || codePoint === 0x202f
      || codePoint === 0x205f
      || codePoint === 0x3000;
  }

  function validDingTalkWebhookUrl(value) {
    if (/[\u0000-\u001f\u007f]/.test(value)) return false;
    const authorityMatch = value.match(/^https:\/\/([^/?#]+)(?:[/?#]|$)/i);
    if (!authorityMatch || authorityMatch[1] !== 'oapi.dingtalk.com') return false;
    try {
      const webhookUrl = new URL(value);
      const accessTokens = webhookUrl.searchParams.getAll('access_token');
      return webhookUrl.protocol === 'https:'
        && webhookUrl.host === 'oapi.dingtalk.com'
        && webhookUrl.pathname === '/robot/send'
        && webhookUrl.username === ''
        && webhookUrl.password === ''
        && accessTokens.length === 1
        && accessTokens[0] !== ''
        && !Array.from(accessTokens[0]).some(character => {
          const codePoint = character.codePointAt(0);
          return codePoint < 0x20 || codePoint === 0x7f || pythonIsSpaceCodePoint(codePoint);
        });
    } catch (_error) {
      return false;
    }
  }

  function alertPolicyPayload() {
    return {
      enabled: document.getElementById('serviceReliabilityAlertEnabled').checked,
      webhook_url: document.getElementById('serviceReliabilityAlertWebhookUrl').value.trim(),
      availability_threshold: Number(document.getElementById('serviceReliabilityAlertAvailabilityThreshold').value),
      authentication_threshold: Number(document.getElementById('serviceReliabilityAlertAuthenticationThreshold').value),
      window_seconds: Number(document.getElementById('serviceReliabilityAlertWindowSeconds').value),
      dedup_seconds: Number(document.getElementById('serviceReliabilityAlertDedupSeconds').value),
      max_pending_tasks: Number(document.getElementById('serviceReliabilityAlertMaxPendingTasks').value),
    };
  }

  async function saveServiceReliabilityAlertConfig() {
    if (alertActionRunning) return;
    const submittedRevision = alertEditRevision;
    const payload = alertPolicyPayload();
    const numericPolicyFields = [
      'availability_threshold',
      'authentication_threshold',
      'window_seconds',
      'dedup_seconds',
      'max_pending_tasks',
    ];
    if (numericPolicyFields.some(key => !Number.isInteger(payload[key]) || payload[key] < 1)) {
      if (typeof window.toast === 'function') window.toast('告警策略必须填写有效的正整数', 'error');
      return;
    }
    if (payload.dedup_seconds < payload.window_seconds) {
      if (typeof window.toast === 'function') window.toast('去重时间不能短于统计窗口', 'error');
      return;
    }
    if (payload.webhook_url && !validDingTalkWebhookUrl(payload.webhook_url)) {
      if (typeof window.toast === 'function') window.toast('Webhook 地址必须是有效的钉钉机器人地址', 'error');
      return;
    }
    alertConfigLoadGeneration += 1;
    alertActionRunning = true;
    document.getElementById('serviceReliabilityAlertSave').disabled = true;
    document.getElementById('serviceReliabilityAlertTest').disabled = true;
    try {
      const response = await fetch('/admin/alerts/config', {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json', ...window.adminHeaders() },
        body: JSON.stringify(payload),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || '告警策略保存失败');
      alertConfigLoadGeneration += 1;
      if (alertEditRevision === submittedRevision) alertPolicyDirty = false;
      renderAlertConfig(result);
      if (typeof window.toast === 'function') window.toast('告警配置已保存', 'success');
    } catch (error) {
      console.error(error);
      if (typeof window.toast === 'function') window.toast(error.message || '告警策略保存失败', 'error');
    } finally {
      alertActionRunning = false;
      document.getElementById('serviceReliabilityAlertSave').disabled = false;
      await loadServiceReliabilityAlerts();
    }
  }

  async function testServiceReliabilityAlertDestination() {
    if (alertActionRunning) return;
    alertActionRunning = true;
    document.getElementById('serviceReliabilityAlertSave').disabled = true;
    document.getElementById('serviceReliabilityAlertTest').disabled = true;
    try {
      const response = await fetch('/admin/alerts/test', {
        method: 'POST',
        headers: window.adminHeaders(),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || '配置测试发送失败');
      if (typeof window.toast === 'function') {
        window.toast(result.sent ? '钉钉配置测试已送达' : '钉钉配置测试未送达，请查看推送历史', result.sent ? 'success' : 'warning');
      }
    } catch (error) {
      console.error(error);
      if (typeof window.toast === 'function') window.toast(error.message || '配置测试发送失败', 'error');
    } finally {
      alertActionRunning = false;
      document.getElementById('serviceReliabilityAlertSave').disabled = false;
      await loadServiceReliabilityAlerts();
    }
  }

  function renderSummary(payload) {
    const overall = payload.overall || {};
    const summary = document.getElementById('serviceReliabilitySummary');
    summary.innerHTML = [
      ['整体状态', statusBadge(overall.health_status), '按渠道最差状态判定'],
      ['渠道正常', `${number(overall.channels_operational)} / ${number(overall.channels_total)}`, `${number(overall.channels_affected)} 个受影响`],
      ['路由覆盖', `${number(overall.models_operational)} / ${number(overall.models_total)}`, `${number(overall.models_affected)} 个公开模型受影响`],
      ['5 分钟请求', number(overall.requests_5m), `${number(overall.failed_requests_5m)} 次失败`],
      ['Fallback', percent(overall.fallback_rate_5m), `${number(overall.fallback_requests_5m)} 次切换`],
      ['当前事件', number(overall.active_incidents), overall.active_incidents ? '需要关注' : '暂无事件'],
    ].map(([label, value, note]) => `
      <div class="sr-summary-item">
        <div class="sr-summary-label">${html(label)}</div>
        <div class="sr-summary-value">${value}</div>
        <div class="sr-summary-note">${html(note)}</div>
      </div>
    `).join('');
  }

  function renderIncidents(payload) {
    const incidents = payload.incidents || [];
    const node = document.getElementById('serviceReliabilityIncidents');
    if (!incidents.length) {
      node.hidden = true;
      node.innerHTML = '';
      node.classList.remove('is-critical');
      return;
    }
    const critical = incidents.some(item => item.severity === 'critical');
    node.hidden = false;
    node.classList.toggle('is-critical', critical);
    node.innerHTML = `
      <strong>${critical ? '当前存在服务故障' : '当前存在服务降级'}</strong>
      <div class="sr-incident-list">
        ${incidents.slice(0, 6).map(item => `
          <div class="sr-incident-item">
            <span>${html(item.channel_name || item.channel_id || '未知通道')} · ${html(item.message || '状态异常')}</span>
            <span>${statusBadge(item.status)}</span>
          </div>
        `).join('')}
      </div>
    `;
  }

  function renderModels(payload) {
    const models = payload.models || [];
    const body = document.getElementById('serviceReliabilityModelsBody');
    body.innerHTML = models.length ? models.map(item => `
      <tr>
        <td><div class="sr-primary">${html(item.public_model_id || '-')}</div><div class="sr-secondary">${number(item.active_route_count)} / ${number(item.route_count)} 条 route 可用</div></td>
        <td>${statusBadge(item.health_status)}</td>
        <td>${number(item.requests_5m)}</td>
        <td>${number(item.failed_requests_5m)}</td>
        <td><div>${percent(item.fallback_rate_5m)}</div><div class="sr-secondary">${number(item.fallback_requests_5m)} 次</div></td>
        <td>${latency(item.avg_latency_ms_5m)}</td>
        <td><button class="btn btn-sm" type="button" data-sr-model="${html(encodeURIComponent(item.public_model_id || ''))}">查看 ${number((item.routes || []).length)} 条</button></td>
      </tr>
    `).join('') : '<tr><td colspan="7"><div class="sr-empty">还没有配置模型 route</div></td></tr>';
    body.querySelectorAll('[data-sr-model]').forEach(button => {
      button.addEventListener('click', () => openServiceReliabilityRoutes(button.dataset.srModel || ''));
    });
  }

  function renderChannels(payload) {
    const channels = payload.channels || [];
    const body = document.getElementById('serviceReliabilityChannelsBody');
    body.innerHTML = channels.length ? channels.map(item => {
      const monitorId = item.monitor_id || '';
      const monitorMode = item.monitor_mode === 'manual' ? '手动' : (item.monitor_mode === 'auto' ? '自动' : '未配置');
      const monitorTarget = item.monitor_model
        ? `<div class="sr-primary">${html(item.monitor_model)}</div><div class="sr-secondary">${html(item.monitor_endpoint || '-')} · ${html(monitorMode)}</div>`
        : `<div class="sr-secondary">${html(monitorMode)}</div>`;
      const action = monitorId
        ? `<button class="btn btn-sm" type="button" data-sr-monitor="${html(encodeURIComponent(monitorId))}" ${runningMonitors.has(monitorId) ? 'disabled' : ''}>${runningMonitors.has(monitorId) ? '检测中' : '立即探测'}</button>`
        : '<span class="sr-secondary">未配置代表探测</span>';
      return `
        <tr>
          <td><div class="sr-primary">${html(item.name || item.id)}</div><div class="sr-secondary">${html(item.provider_platform || item.channel_type || '-')} · P${number(item.priority)} / W${number(item.weight)}</div></td>
          <td>${monitorTarget}</td>
          <td>${statusBadge(item.health_status)}${item.monitor_message ? `<div class="sr-secondary">${html(item.monitor_message)}</div>` : ''}</td>
          <td><div class="sr-primary">${number(item.active_route_count)} 条 route</div><div class="sr-secondary">${html((item.public_models || []).join(', ') || '-')}</div></td>
          <td><div>${number(item.requests_5m)}</div><div class="sr-secondary">失败 ${number(item.failed_requests_5m)}</div></td>
          <td>${percent(item.fallback_rate_5m)}</td>
          <td><div>${latency(item.avg_latency_ms_5m)}</div><div class="sr-secondary">检测 ${dateTime(item.last_checked_at)}</div>${item.cooldown_until ? `<div class="sr-secondary">冷却至 ${dateTime(item.cooldown_until)}</div>` : ''}</td>
          <td>${action}</td>
        </tr>
      `;
    }).join('') : '<tr><td colspan="8"><div class="sr-empty">还没有配置通道</div></td></tr>';
    body.querySelectorAll('[data-sr-monitor]').forEach(button => {
      button.addEventListener('click', () => runServiceReliabilityProbe(button.dataset.srMonitor || ''));
    });
  }

  function renderFailures(payload) {
    const failures = payload.recent_failures || [];
    const body = document.getElementById('serviceReliabilityFailuresBody');
    body.innerHTML = failures.length ? failures.map(item => `
      <tr>
        <td>${dateTime(item.created_at)}</td>
        <td><span class="sr-status sr-status-failed">${number(item.status_code)}</span></td>
        <td><div class="sr-primary">${html(item.model || '-')}</div></td>
        <td>${html(item.endpoint || '-')}</td>
        <td>${html(item.channel_id || '-')}</td>
        <td>${latency(item.duration_ms)}</td>
        <td><div>${html(item.route_reason || '-')}</div>${Number(item.route_attempt || 0) > 0 ? `<div class="sr-secondary">Fallback attempt ${number(item.route_attempt)}</div>` : ''}</td>
      </tr>
    `).join('') : '<tr><td colspan="7"><div class="sr-empty">最近 5 分钟没有失败请求</div></td></tr>';
  }

  function renderOverview(payload) {
    latestOverview = payload;
    renderSummary(payload);
    renderIncidents(payload);
    renderChannels(payload);
    renderModels(payload);
    renderFailures(payload);
    const updated = document.getElementById('serviceReliabilityUpdatedAt');
    updated.textContent = `更新于 ${dateTime(payload.generated_at)} · 每 15 秒刷新`;
  }

  function renderLoadError(message) {
    const summary = document.getElementById('serviceReliabilitySummary');
    if (summary) summary.innerHTML = `<div class="sr-error">${html(message || '可靠性数据加载失败')}</div>`;
  }

  async function loadServiceReliability(force = false) {
    if (!document.getElementById('page-service-reliability')) return;
    if (!pageIsActive() && !force) return;
    if (loading) return;
    loading = true;
    const alertLoad = loadServiceReliabilityAlerts();
    try {
      const response = await fetch('/admin/reliability/overview', {
        cache: 'no-store',
        headers: window.adminHeaders(),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail || '可靠性数据加载失败');
      renderOverview(payload);
    } catch (error) {
      console.error(error);
      renderLoadError(error.message);
      if (force && typeof window.toast === 'function') window.toast(error.message || '可靠性数据加载失败', 'error');
    } finally {
      await alertLoad;
      loading = false;
      schedulePoll();
    }
  }

  function openServiceReliabilityRoutes(encodedModel) {
    if (!latestOverview) return;
    const modelId = decodeURIComponent(encodedModel || '');
    const model = (latestOverview.models || []).find(item => item.public_model_id === modelId);
    const drawer = document.getElementById('serviceReliabilityRouteDrawer');
    if (!drawer || !model) return;
    drawer.hidden = false;
    drawer.innerHTML = `
      <div class="sr-route-drawer-header">
        <div class="sr-route-drawer-title">${html(model.public_model_id)} 的通道路由</div>
        <button class="btn btn-sm" type="button" id="serviceReliabilityCloseRoutes" title="关闭路由详情">关闭</button>
      </div>
      <div class="sr-route-list">
        ${(model.routes || []).length ? model.routes.map(route => `
          <div class="sr-route-row">
            <div><div class="sr-primary">${html(route.channel_name || route.channel_id)}</div><div class="sr-secondary">${html(route.channel_id || '-')}</div></div>
            <div><div class="sr-primary">${html(route.upstream_model || '-')}</div><div class="sr-secondary">${html(route.endpoint || '全部端点')}</div></div>
            <div><div class="sr-secondary">优先级</div><div>${number(route.priority)}</div></div>
            <div><div class="sr-secondary">权重</div><div>${number(route.weight)}</div></div>
            <div>${statusBadge(route.health_status)}</div>
          </div>
        `).join('') : '<div class="sr-empty">当前没有可用 route</div>'}
      </div>
    `;
    document.getElementById('serviceReliabilityCloseRoutes')?.addEventListener('click', () => {
      drawer.hidden = true;
      drawer.innerHTML = '';
    });
  }

  async function runServiceReliabilityProbe(encodedMonitorId) {
    const monitorId = decodeURIComponent(encodedMonitorId || '');
    if (!monitorId || runningMonitors.has(monitorId)) return;
    runningMonitors.add(monitorId);
    if (latestOverview) renderChannels(latestOverview);
    try {
      const response = await fetch(`/admin/provider-channel-monitors/${encodeURIComponent(monitorId)}/run`, {
        method: 'POST',
        headers: window.adminHeaders(),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail || '检测失败');
      const results = payload.results || [];
      const failed = results.find(item => item.status !== 'operational');
      if (typeof window.toast === 'function') {
        window.toast(failed ? `检测完成：${failed.message || failed.status}` : '检测完成：通道正常', failed ? 'warning' : 'success');
      }
      await loadServiceReliability(true);
    } catch (error) {
      console.error(error);
      if (typeof window.toast === 'function') window.toast(error.message || '检测失败', 'error');
    } finally {
      runningMonitors.delete(monitorId);
      if (latestOverview) renderChannels(latestOverview);
    }
  }

  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      if (pollTimer) window.clearTimeout(pollTimer);
      pollTimer = null;
      return;
    }
    if (pageIsActive()) loadServiceReliability();
  });

  document.querySelectorAll('[data-sr-alert-policy]').forEach(control => {
    control.addEventListener('input', () => {
      alertEditRevision += 1;
      alertPolicyDirty = true;
    });
  });
  document.getElementById('serviceReliabilityAlertSave')?.addEventListener('click', saveServiceReliabilityAlertConfig);
  document.getElementById('serviceReliabilityAlertTest')?.addEventListener('click', testServiceReliabilityAlertDestination);
  document.getElementById('serviceReliabilityAlertCategoryFilter')?.addEventListener('change', loadServiceReliabilityAlerts);
  document.getElementById('serviceReliabilityAlertStatusFilter')?.addEventListener('change', loadServiceReliabilityAlerts);

  window.loadServiceReliability = loadServiceReliability;
  window.openServiceReliabilityRoutes = openServiceReliabilityRoutes;
  window.runServiceReliabilityProbe = runServiceReliabilityProbe;
  window.saveServiceReliabilityAlertConfig = saveServiceReliabilityAlertConfig;
  window.testServiceReliabilityAlertDestination = testServiceReliabilityAlertDestination;
})();
