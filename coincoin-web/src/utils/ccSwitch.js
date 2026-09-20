import { parseBackendTimestamp } from './time.js'

export const CC_SWITCH_DOWNLOAD_URL = 'https://github.com/farion1231/cc-switch/releases/latest'
export const CODEX_MODEL_ID = 'gpt-5.4'
export const CLAUDE_MODEL_ID = 'claude-sonnet-4-6'

export const CC_SWITCH_CLIENTS = {
    codex: { label: 'Codex', model: CODEX_MODEL_ID, guide: '/guides/codex' },
    claude: { label: 'Claude Code', model: CLAUDE_MODEL_ID, guide: '/guides/claude-code' },
}

export function isFullDeveloperKey(value) {
    // Shape validation only: session keys share this prefix. Callers must use
    // /v1/keys results or useAuth.effectiveApiKey, never the console session key.
    return typeof value === 'string' && /^sk_cc_[A-Za-z0-9_-]+$/.test(value)
        && value !== 'sk_cc_xxxxx' && value !== 'sk_cc_demo_key'
}

export function selectDeveloperKey({ generatedApiKey = '', recoverableApiKey = '', apiKey = '', isConsoleSession = false } = {}) {
    const candidate = generatedApiKey || recoverableApiKey || (!isConsoleSession ? apiKey : '')
    return isFullDeveloperKey(candidate) ? candidate : ''
}

export function getKeyAvailability(item, now = Date.now()) {
    if (item.status !== 'active') return { label: '已禁用', tone: 'disabled', usable: false }
    const expires = parseBackendTimestamp(item.expires_at)
    if (expires && expires.getTime() <= now) return { label: '已过期', tone: 'danger', usable: false }
    const exhausted = ['monthly', 'total'].some((period) =>
        Number(item[`${period}_quota_cents`]) > 0
        && Number(item[`${period}_used_cents`] || 0) >= Number(item[`${period}_quota_cents`]))
    if (exhausted) return { label: '额度用尽', tone: 'warn', usable: false }
    return { label: '可用', tone: 'active', usable: true }
}

export function getCcSwitchEndpoint(baseUrl, app) {
    if (!Object.hasOwn(CC_SWITCH_CLIENTS, app)) throw new Error('请选择 Codex 或 Claude Code')
    const url = new URL(baseUrl)
    if (!['https:', 'http:'].includes(url.protocol) || url.username || url.password || url.search || url.hash) {
        throw new Error('站点地址无效')
    }
    const root = url.href.replace(/\/+$/, '').replace(/\/v1$/, '')
    return app === 'codex' ? `${root}/v1` : root
}

// Implements CC Switch's public v1 provider deep-link protocol.
export function buildCcSwitchLink({ app, apiKey, baseUrl, keyName = '' }) {
    if (!isFullDeveloperKey(apiKey)) throw new Error('需要完整的开发者 Key，不能使用脱敏 Key 或占位符')
    const endpoint = getCcSwitchEndpoint(baseUrl, app)
    const client = CC_SWITCH_CLIENTS[app]
    const params = new URLSearchParams({
        resource: 'provider',
        app,
        name: `ClawFather${keyName.trim() ? ` · ${keyName.trim()}` : ''}`,
        homepage: getCcSwitchEndpoint(baseUrl, 'claude'),
        endpoint,
        apiKey,
        model: client.model,
    })
    return `ccswitch://v1/import?${params.toString()}`
}
