const PROXY_BASE = ''

function emitAuthChange() {
    if (typeof window !== 'undefined') {
        window.dispatchEvent(new Event('coincoin-auth-changed'))
    }
}

/** Get stored API key */
export function getApiKey() {
    return localStorage.getItem('coincoin_api_key') || ''
}

/** Set API key */
export function setApiKey(key) {
    localStorage.setItem('coincoin_api_key', key)
    emitAuthChange()
}

/** Clear API key (logout) */
export function clearApiKey() {
    localStorage.removeItem('coincoin_api_key')
    localStorage.removeItem('coincoin_user_id')
    emitAuthChange()
}

export function getUserId() {
    return localStorage.getItem('coincoin_user_id') || ''
}

export function setUserId(id) {
    localStorage.setItem('coincoin_user_id', id)
    emitAuthChange()
}

export function getUsername() {
    return localStorage.getItem('coincoin_username') || ''
}

export function setUsername(u) {
    localStorage.setItem('coincoin_username', u)
    emitAuthChange()
}

export function getGeneratedKey() {
    return localStorage.getItem('coincoin_generated_key') || ''
}

export function setGeneratedKey(key) {
    localStorage.setItem('coincoin_generated_key', key)
    emitAuthChange()
}

export function clearGeneratedKey() {
    localStorage.removeItem('coincoin_generated_key')
    emitAuthChange()
}

export async function getDeveloperKeyState() {
    const res = await fetch(`${PROXY_BASE}/v1/keys/me`, {
        headers: authHeaders()
    })
    const data = await res.json()
    if (!res.ok) throw new Error(data.detail || data.error?.message || 'Failed to fetch developer key state')
    return data
}

export async function listDeveloperKeys() {
    const res = await fetch(`${PROXY_BASE}/v1/keys`, {
        headers: authHeaders()
    })
    const data = await res.json()
    if (!res.ok) throw new Error(data.detail || data.error?.message || 'Failed to list developer keys')
    return data
}

export async function createDeveloperKey() {
    const res = await fetch(`${PROXY_BASE}/v1/keys`, {
        method: 'POST',
        headers: authHeaders(),
    })
    const data = await res.json()
    if (!res.ok) throw new Error(data.detail || data.error?.message || 'Failed to create developer key')
    return data
}

export async function updateDeveloperKey(keyId, payload) {
    const res = await fetch(`${PROXY_BASE}/v1/keys/${keyId}`, {
        method: 'PATCH',
        headers: authHeaders(),
        body: JSON.stringify(payload),
    })
    const data = await res.json()
    if (!res.ok) throw new Error(data.detail || data.error?.message || 'Failed to update developer key')
    return data
}

/** Auth header helper */
function authHeaders() {
    return {
        'Authorization': `Bearer ${getApiKey()}`,
        'Content-Type': 'application/json'
    }
}

// ===== Auth APIs =====

async function parseApiResponse(res, fallbackMessage) {
    const contentType = res.headers.get('content-type') || ''
    if (contentType.includes('application/json')) {
        const data = await res.json()
        if (!res.ok) {
            const detail = data.detail
            if (typeof detail === 'string' && detail) throw new Error(detail)
            if (Array.isArray(detail) && detail.length > 0) {
                const first = detail[0]
                if (typeof first === 'string') throw new Error(first)
                if (first && typeof first === 'object' && first.msg) throw new Error(first.msg)
            }
            if (detail && typeof detail === 'object' && detail.msg) throw new Error(detail.msg)
            throw new Error(data.error?.message || fallbackMessage)
        }
        return data
    }

    const text = await res.text()
    if (!res.ok) throw new Error(text || fallbackMessage)
    return text
}

export async function registerUser(username, email, password, referralCode, verificationId, verificationCode) {
    const body = { username, email, password }
    if (referralCode) body.referral_code = referralCode
    if (verificationId) body.verification_id = verificationId
    if (verificationCode) body.verification_code = verificationCode
    const res = await fetch(`${PROXY_BASE}/v1/auth/register`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
    })
    return parseApiResponse(res, 'registration failed')
}

export async function sendRegisterEmailCode(email) {
    const res = await fetch(`${PROXY_BASE}/v1/auth/register/send-code`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email })
    })
    return parseApiResponse(res, 'failed to send code')
}

export async function checkRegisterEmailCode(verificationId, code) {
    const res = await fetch(`${PROXY_BASE}/v1/auth/register/check-code`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ verification_id: verificationId, code })
    })
    return parseApiResponse(res, 'verification failed')
}

export async function verifyEmail(userId, code) {
    const res = await fetch(`${PROXY_BASE}/v1/auth/verify-email`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ user_id: userId, code })
    })
    const data = await res.json()
    if (!res.ok) throw new Error(data.detail || 'verification failed')
    return data
}

export async function resendVerification(userId) {
    const res = await fetch(`${PROXY_BASE}/v1/auth/resend-verification`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ user_id: userId })
    })
    const data = await res.json()
    if (!res.ok) throw new Error(data.detail || 'resend failed')
    return data
}

export async function loginUser(username, password) {
    const res = await fetch(`${PROXY_BASE}/v1/auth/login`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password })
    })
    const data = await res.json()
    if (!res.ok) throw new Error(data.detail || 'login failed')
    return data
}

export async function getAuthProfile() {
    const res = await fetch(`${PROXY_BASE}/v1/auth/me`, {
        headers: authHeaders()
    })
    const data = await res.json()
    if (!res.ok) throw new Error(data.detail || 'failed to fetch profile')
    return data
}

export async function sendAccountEmailCode(email) {
    const res = await fetch(`${PROXY_BASE}/v1/auth/me/email/send-code`, {
        method: 'POST',
        headers: authHeaders(),
        body: JSON.stringify({ email })
    })
    const data = await res.json()
    if (!res.ok) throw new Error(data.detail || 'failed to send code')
    return data
}

export async function verifyAccountEmail(code) {
    const res = await fetch(`${PROXY_BASE}/v1/auth/me/email/verify`, {
        method: 'POST',
        headers: authHeaders(),
        body: JSON.stringify({ code })
    })
    const data = await res.json()
    if (!res.ok) throw new Error(data.detail || 'failed to verify email')
    return data
}

// ===== User-side APIs (real) =====

/** Activate: create user and get API key (kind=api) */
export async function activateKey(username) {
    const res = await fetch(`${PROXY_BASE}/v1/keys/activate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username })
    })
    return res.json()
}

/** Get balance & usage info */
export async function getBalance() {
    const res = await fetch(`${PROXY_BASE}/v1/balance`, {
        headers: authHeaders()
    })
    if (!res.ok) throw new Error('Invalid API Key')
    return res.json()
}

/** Get usage logs */
export async function getUsageLogs(limit = 50, offset = 0, filters = {}) {
    const params = new URLSearchParams({ limit, offset })
    Object.entries(filters || {}).forEach(([key, value]) => {
        if (value !== undefined && value !== null && value !== '') params.set(key, value)
    })
    const res = await fetch(`${PROXY_BASE}/v1/usage?${params}`, {
        headers: authHeaders()
    })
    if (!res.ok) throw new Error('Failed to fetch usage')
    return res.json()
}

// ===== Payment APIs =====

/** Create order via proxy (proxy creates order + calls payment service) */
export async function createOrder({ name, money, pay_type = 'alipay' }) {
    const res = await fetch(`${PROXY_BASE}/v1/orders/create`, {
        method: 'POST',
        headers: authHeaders(),
        body: JSON.stringify({ name, money, pay_type })
    })
    return res.json()
}

/** Confirm payment order. Prefer signed proof_url from return page; fallback to backend reconciliation. */
export async function confirmOrder(orderNo, proofUrl) {
    const body = { order_no: orderNo }
    if (proofUrl) {
        body.proof_url = proofUrl
    }
    const res = await fetch(`${PROXY_BASE}/v1/orders/confirm`, {
        method: 'POST',
        headers: authHeaders(),
        body: JSON.stringify(body)
    })
    return res.json()
}

/** Redeem a code */
export async function redeemCode(code) {
    const res = await fetch(`${PROXY_BASE}/v1/redeem`, {
        method: 'POST',
        headers: authHeaders(),
        body: JSON.stringify({ code })
    })
    return res.json()
}

/** Get daily usage stats */
export async function getDailyUsage(days = 7) {
    const res = await fetch(`${PROXY_BASE}/v1/usage/daily?days=${days}`, {
        headers: authHeaders()
    })
    if (!res.ok) throw new Error('Failed to fetch daily usage')
    return res.json()
}

/** Get referral info */
export async function getReferralInfo() {
    const res = await fetch(`${PROXY_BASE}/v1/referral`, {
        headers: authHeaders()
    })
    if (!res.ok) throw new Error('Failed to fetch referral info')
    return res.json()
}

export async function updateReferralCode(referralCode) {
    const res = await fetch(`${PROXY_BASE}/v1/referral/code`, {
        method: 'PATCH',
        headers: authHeaders(),
        body: JSON.stringify({ referral_code: referralCode })
    })
    return parseApiResponse(res, 'Failed to update referral code')
}

export async function getStationApplication() {
    const res = await fetch(`${PROXY_BASE}/v1/stations/application`, {
        headers: authHeaders()
    })
    if (!res.ok) throw new Error('Failed to fetch station application')
    return res.json()
}

export async function applyForStation(payload) {
    const res = await fetch(`${PROXY_BASE}/v1/stations/apply`, {
        method: 'POST',
        headers: authHeaders(),
        body: JSON.stringify(payload)
    })
    const data = await res.json()
    if (!res.ok) throw new Error(data.detail || 'Failed to apply for station')
    return data
}

export async function getStationSummary() {
    const res = await fetch(`${PROXY_BASE}/v1/stations/me/summary`, {
        headers: authHeaders()
    })
    const data = await res.json()
    if (!res.ok) throw new Error(data.detail || 'Failed to fetch station summary')
    return data
}

export async function getStationCustomers() {
    const res = await fetch(`${PROXY_BASE}/v1/stations/me/customers`, {
        headers: authHeaders()
    })
    const data = await res.json()
    if (!res.ok) throw new Error(data.detail || 'Failed to fetch station customers')
    return data
}

export async function createStationCustomer(payload) {
    const res = await fetch(`${PROXY_BASE}/v1/stations/me/customers`, {
        method: 'POST',
        headers: authHeaders(),
        body: JSON.stringify(payload)
    })
    const data = await res.json()
    if (!res.ok) throw new Error(data.detail || 'Failed to create station customer')
    return data
}

export async function getStationCommissionLedger(statusFilter = '') {
    const suffix = statusFilter ? `?status_filter=${encodeURIComponent(statusFilter)}` : ''
    const res = await fetch(`${PROXY_BASE}/v1/stations/me/commission-ledger${suffix}`, {
        headers: authHeaders()
    })
    const data = await res.json()
    if (!res.ok) throw new Error(data.detail || 'Failed to fetch station commission ledger')
    return data
}

export async function getStationPayoutBatches() {
    const res = await fetch(`${PROXY_BASE}/v1/stations/me/payout-batches`, {
        headers: authHeaders()
    })
    const data = await res.json()
    if (!res.ok) throw new Error(data.detail || 'Failed to fetch station payouts')
    return data
}

export async function updateStationSettlement(payload) {
    const res = await fetch(`${PROXY_BASE}/v1/stations/me/settlement`, {
        method: 'POST',
        headers: authHeaders(),
        body: JSON.stringify(payload)
    })
    const data = await res.json()
    if (!res.ok) throw new Error(data.detail || 'Failed to update station settlement')
    return data
}

/** Get active announcements */
export async function getAnnouncements() {
    const res = await fetch(`${PROXY_BASE}/v1/announcements`)
    if (!res.ok) return []
    return res.json()
}

// ===== Public model catalog =====

export const PUBLIC_MODEL_CATALOG_FALLBACK = [
    { id: 'opus', object: 'model', owned_by: 'coincoin', coincoin_capabilities: ['chat/completions', 'responses'], coincoin_billable_sku: 'claude-code-compat-text', coincoin_routing_mode: 'direct', coincoin_default_for: ['text'], coincoin_metadata: { tier: 'stable' }, coincoin_price_input_per_million: 500, coincoin_price_cached_input_per_million: 50, coincoin_price_output_per_million: 3000, coincoin_price_per_image_cents: 0 },
    { id: 'sonnet', object: 'model', owned_by: 'coincoin', coincoin_capabilities: ['chat/completions', 'responses'], coincoin_billable_sku: 'claude-code-compat-text', coincoin_routing_mode: 'direct', coincoin_default_for: [], coincoin_metadata: { tier: 'stable' }, coincoin_price_input_per_million: 500, coincoin_price_cached_input_per_million: 50, coincoin_price_output_per_million: 3000, coincoin_price_per_image_cents: 0 },
    { id: 'haiku', object: 'model', owned_by: 'coincoin', coincoin_capabilities: ['chat/completions', 'responses'], coincoin_billable_sku: 'claude-code-compat-text', coincoin_routing_mode: 'direct', coincoin_default_for: [], coincoin_metadata: { tier: 'stable' }, coincoin_price_input_per_million: 500, coincoin_price_cached_input_per_million: 50, coincoin_price_output_per_million: 3000, coincoin_price_per_image_cents: 0 },
    { id: 'gpt-5.5', object: 'model', owned_by: 'openai', coincoin_capabilities: ['chat/completions', 'responses'], coincoin_billable_sku: 'legacy-gpt-5.5-text', coincoin_routing_mode: 'legacy_auto', coincoin_default_for: [], coincoin_metadata: {}, coincoin_price_input_per_million: 500, coincoin_price_cached_input_per_million: 50, coincoin_price_output_per_million: 3000, coincoin_price_per_image_cents: 0 },
    { id: 'gpt-5.4', object: 'model', owned_by: 'openai', coincoin_capabilities: ['chat/completions', 'responses'], coincoin_billable_sku: 'legacy-gpt-5.4-text', coincoin_routing_mode: 'legacy_auto', coincoin_default_for: [], coincoin_metadata: {}, coincoin_price_input_per_million: 250, coincoin_price_cached_input_per_million: 25, coincoin_price_output_per_million: 1500, coincoin_price_per_image_cents: 0 },
    { id: 'gpt-5.4-mini', object: 'model', owned_by: 'openai', coincoin_capabilities: ['chat/completions', 'responses'], coincoin_billable_sku: 'legacy-gpt-5.4-mini-text', coincoin_routing_mode: 'legacy_auto', coincoin_default_for: [], coincoin_metadata: {}, coincoin_price_input_per_million: 75, coincoin_price_cached_input_per_million: 7.5, coincoin_price_output_per_million: 450, coincoin_price_per_image_cents: 0 },
    { id: 'gpt-5.3-codex', object: 'model', owned_by: 'openai', coincoin_capabilities: ['chat/completions', 'responses'], coincoin_billable_sku: 'legacy-gpt-5.3-codex-text', coincoin_routing_mode: 'legacy_auto', coincoin_default_for: [], coincoin_metadata: {}, coincoin_price_input_per_million: 175, coincoin_price_cached_input_per_million: 17.5, coincoin_price_output_per_million: 1400, coincoin_price_per_image_cents: 0 },
    { id: 'gpt-5.2-codex', object: 'model', owned_by: 'openai', coincoin_capabilities: ['chat/completions', 'responses'], coincoin_billable_sku: 'legacy-gpt-5.2-codex-text', coincoin_routing_mode: 'legacy_auto', coincoin_default_for: [], coincoin_metadata: {}, coincoin_price_input_per_million: 175, coincoin_price_cached_input_per_million: 17.5, coincoin_price_output_per_million: 1400, coincoin_price_per_image_cents: 0 },
    { id: 'gpt-5.2', object: 'model', owned_by: 'openai', coincoin_capabilities: ['chat/completions', 'responses'], coincoin_billable_sku: 'legacy-gpt-5.2-text', coincoin_routing_mode: 'legacy_auto', coincoin_default_for: [], coincoin_metadata: {}, coincoin_price_input_per_million: 175, coincoin_price_cached_input_per_million: 17.5, coincoin_price_output_per_million: 1400, coincoin_price_per_image_cents: 0 },
    { id: 'gpt-5.1-codex-max', object: 'model', owned_by: 'openai', coincoin_capabilities: ['chat/completions', 'responses'], coincoin_billable_sku: 'legacy-gpt-5.1-codex-max-text', coincoin_routing_mode: 'legacy_auto', coincoin_default_for: [], coincoin_metadata: {}, coincoin_price_input_per_million: 500, coincoin_price_cached_input_per_million: 50, coincoin_price_output_per_million: 3000, coincoin_price_per_image_cents: 0 },
    { id: 'gpt-5.1-codex', object: 'model', owned_by: 'openai', coincoin_capabilities: ['chat/completions', 'responses'], coincoin_billable_sku: 'legacy-gpt-5.1-codex-text', coincoin_routing_mode: 'legacy_auto', coincoin_default_for: [], coincoin_metadata: {}, coincoin_price_input_per_million: 125, coincoin_price_cached_input_per_million: 12.5, coincoin_price_output_per_million: 1000, coincoin_price_per_image_cents: 0 },
    { id: 'gpt-5.1-codex-mini', object: 'model', owned_by: 'openai', coincoin_capabilities: ['chat/completions', 'responses'], coincoin_billable_sku: 'legacy-gpt-5.1-codex-mini-text', coincoin_routing_mode: 'legacy_auto', coincoin_default_for: [], coincoin_metadata: {}, coincoin_price_input_per_million: 75, coincoin_price_cached_input_per_million: 7.5, coincoin_price_output_per_million: 450, coincoin_price_per_image_cents: 0 },
    { id: 'gpt-5.1', object: 'model', owned_by: 'openai', coincoin_capabilities: ['chat/completions', 'responses'], coincoin_billable_sku: 'legacy-gpt-5.1-text', coincoin_routing_mode: 'legacy_auto', coincoin_default_for: [], coincoin_metadata: {}, coincoin_price_input_per_million: 125, coincoin_price_cached_input_per_million: 12.5, coincoin_price_output_per_million: 1000, coincoin_price_per_image_cents: 0 },
    { id: 'gpt-5', object: 'model', owned_by: 'openai', coincoin_capabilities: ['chat/completions', 'responses'], coincoin_billable_sku: 'legacy-gpt-5-text', coincoin_routing_mode: 'legacy_auto', coincoin_default_for: [], coincoin_metadata: {}, coincoin_price_input_per_million: 125, coincoin_price_cached_input_per_million: 12.5, coincoin_price_output_per_million: 1000, coincoin_price_per_image_cents: 0 },
    { id: 'gpt-5-codex', object: 'model', owned_by: 'openai', coincoin_capabilities: ['chat/completions', 'responses'], coincoin_billable_sku: 'legacy-gpt-5-codex-text', coincoin_routing_mode: 'legacy_auto', coincoin_default_for: [], coincoin_metadata: {}, coincoin_price_input_per_million: 175, coincoin_price_cached_input_per_million: 17.5, coincoin_price_output_per_million: 1400, coincoin_price_per_image_cents: 0 },
    { id: 'gpt-5-codex-mini', object: 'model', owned_by: 'openai', coincoin_capabilities: ['chat/completions', 'responses'], coincoin_billable_sku: 'legacy-gpt-5-codex-mini-text', coincoin_routing_mode: 'legacy_auto', coincoin_default_for: [], coincoin_metadata: {}, coincoin_price_input_per_million: 75, coincoin_price_cached_input_per_million: 7.5, coincoin_price_output_per_million: 450, coincoin_price_per_image_cents: 0 },
    { id: 'gemini-image', object: 'model', owned_by: 'google', coincoin_capabilities: ['images/generations', 'images/edits'], coincoin_billable_sku: 'gemini-image', coincoin_routing_mode: 'direct', coincoin_default_for: ['image'], coincoin_metadata: { tier: 'stable' }, coincoin_price_input_per_million: 0, coincoin_price_output_per_million: 0, coincoin_price_per_image_cents: 3.9 },
]

export async function getPublicModels() {
    try {
        const res = await fetch(`${PROXY_BASE}/v1/models`)
        if (!res.ok) throw new Error('failed to fetch models')
        const data = await res.json()
        if (Array.isArray(data?.data) && data.data.length > 0) {
            return data.data
        }
    } catch {
        // fall through to checked-in fallback catalog for UI resilience
    }
    return PUBLIC_MODEL_CATALOG_FALLBACK
}

export function isTextCapableModel(model) {
    const capabilities = model?.coincoin_capabilities || []
    return capabilities.includes('chat/completions') || capabilities.includes('responses')
}

export function isImageCapableModel(model) {
    const capabilities = model?.coincoin_capabilities || []
    return capabilities.includes('images/generations') || capabilities.includes('images/edits')
}

export function getDefaultTextModel(models = PUBLIC_MODEL_CATALOG_FALLBACK) {
    return models.find(model => (model.coincoin_default_for || []).includes('text'))
        || models.find(isTextCapableModel)
        || null
}

export function getDefaultImageModel(models = PUBLIC_MODEL_CATALOG_FALLBACK) {
    return models.find(model => (model.coincoin_default_for || []).includes('image'))
        || models.find(isImageCapableModel)
        || null
}

export function describePublicModel(model) {
    const id = model?.id || ''
    const capabilities = model?.coincoin_capabilities || []
    if (capabilities.includes('images/generations') || capabilities.includes('images/edits')) {
        if (id.includes('preview')) return '图片生成预览模型，适合视觉创作和风格探索'
        return '图片模型，支持文生图和图生图，适合营销图、插画和快速视觉草稿'
    }
    if (['opus', 'claude-opus-4-7', 'best', 'default', 'opus[1m]', 'opusplan'].includes(id)) {
        return '高质量文本模型，适合复杂代码生成、重构和 agent 工作流'
    }
    if (['sonnet', 'haiku', 'claude-sonnet-4-6', 'claude-haiku-4-5', 'claude-haiku-4-5-20251001', 'sonnet[1m]'].includes(id)) {
        return '快速文本模型，适合低延迟、多轮对话和批量任务'
    }
    if (id.startsWith('gpt-5')) {
        if (id.includes('codex-max')) return '高配 Codex 文本模型，适合复杂代码生成、重构和 agent 工作流'
        if (id.includes('codex')) return 'Codex 风格文本模型，适合编程、补全和工具调用场景'
        if (id.includes('mini')) return '轻量文本模型，适合低延迟、多轮对话和批量任务'
        return '文本模型，适合通用对话、写作和兼容 OpenAI 风格客户端'
    }
    if (id.includes('reasoning') || id.includes('-pro')) {
        return '高质量推理与复杂 agent / coding 任务'
    }
    if (id.includes('lite')) {
        return '低成本、高吞吐，适合批量处理和轻量问答'
    }
    if (id.includes('flash')) {
        return '快速通用模型，适合对话、工具调用和结构化输出'
    }
    return '公开可选模型'
}

export function getCachedInputPricePerMillion(model) {
    const explicit = model?.coincoin_price_cached_input_per_million
    if (explicit !== undefined && explicit !== null) return Number(explicit) || 0
    const input = Number(model?.coincoin_price_input_per_million || 0)
    return input > 0 ? input * 0.1 : 0
}

export function formatModelPrice(model) {
    if (isImageCapableModel(model)) {
        const cents = model?.coincoin_price_per_image_cents || 0
        return cents > 0 ? `$${(cents / 100).toFixed(3)} / image` : '按后台配置计费'
    }
    const input = Number(model?.coincoin_price_input_per_million || 0)
    const cachedInput = getCachedInputPricePerMillion(model)
    const output = Number(model?.coincoin_price_output_per_million || 0)
    if (!input && !output) return '按后台配置计费'
    return `Input $${(input / 100).toFixed(2)} / M · Cached $${(cachedInput / 100).toFixed(3)} / M · Output $${(output / 100).toFixed(2)} / M`
}

// ===== Mock Data =====

export const MOCK_BALANCE = {
    user_id: 'u_demo_user_001',
    balance: 9850,
    balance_usd: 98.50,
    token_used: 156230,
    input_tokens_used: 128450,
    output_tokens_used: 27780,
    token_limit: null,
    token_remaining: null,
    price_input_per_million: 0.99,
    price_cached_input_per_million: 0.099,
    price_output_per_million: 6.99
}

export const MOCK_USAGE = {
    user_id: 'u_demo_user_001',
    total: 127,
    limit: 20,
    offset: 0,
    data: [
        { created_at: '2026-02-26T15:30:00', endpoint: 'responses', model: 'opus', usage_unit_type: 'tokens', usage_unit_count: 17330, image_count: 0, input_tokens: 15230, output_tokens: 2100, total_tokens: 17330, cost_cents: 32, cost_usd: 0.32, duration_ms: 3420, status_code: 200, billable_sku: 'claude-code-compat-text' },
        { created_at: '2026-02-26T14:22:00', endpoint: 'chat/completions', model: 'gemini-fast', usage_unit_type: 'tokens', usage_unit_count: 10080, image_count: 0, input_tokens: 8540, output_tokens: 1540, total_tokens: 10080, cost_cents: 23, cost_usd: 0.23, duration_ms: 2180, status_code: 200, billable_sku: 'gemini-fast-text' },
        { created_at: '2026-02-26T13:15:00', endpoint: 'responses', model: 'gemini-reasoning', usage_unit_type: 'tokens', usage_unit_count: 27530, image_count: 0, input_tokens: 22100, output_tokens: 5430, total_tokens: 27530, cost_cents: 80, cost_usd: 0.80, duration_ms: 5670, status_code: 200, billable_sku: 'gemini-reasoning-text' },
        { created_at: '2026-02-26T12:05:00', endpoint: 'images/generations', model: 'gemini-image', usage_unit_type: 'images', usage_unit_count: 2, image_count: 2, input_tokens: 0, output_tokens: 0, total_tokens: 0, cost_cents: 18, cost_usd: 0.18, duration_ms: 6520, status_code: 200, billable_sku: 'gemini-image' },
        { created_at: '2026-02-26T10:48:00', endpoint: 'responses', model: 'vertex-gemini-3.1-pro-preview', usage_unit_type: 'tokens', usage_unit_count: 54300, image_count: 0, input_tokens: 45600, output_tokens: 8700, total_tokens: 54300, cost_cents: 130, cost_usd: 1.30, duration_ms: 8900, status_code: 200, billable_sku: 'vertex-gemini-3.1-pro-preview-text' },
        { created_at: '2026-02-25T22:30:00', endpoint: 'responses', model: 'gemini-balanced', usage_unit_type: 'tokens', usage_unit_count: 15700, image_count: 0, input_tokens: 12300, output_tokens: 3400, total_tokens: 15700, cost_cents: 50, cost_usd: 0.50, duration_ms: 4100, status_code: 200, billable_sku: 'gemini-balanced-text' },
        { created_at: '2026-02-25T20:15:00', endpoint: 'chat/completions', model: 'vertex-gemini-2.5-flash-lite', usage_unit_type: 'tokens', usage_unit_count: 7900, image_count: 0, input_tokens: 6700, output_tokens: 1200, total_tokens: 7900, cost_cents: 18, cost_usd: 0.18, duration_ms: 1800, status_code: 200, billable_sku: 'vertex-gemini-2.5-flash-lite-text' },
        { created_at: '2026-02-25T18:42:00', endpoint: 'images/generations', model: 'vertex-gemini-3.1-flash-image-preview', usage_unit_type: 'images', usage_unit_count: 1, image_count: 1, input_tokens: 0, output_tokens: 0, total_tokens: 0, cost_cents: 11, cost_usd: 0.11, duration_ms: 7010, status_code: 200, billable_sku: 'vertex-gemini-3.1-flash-image-preview' },
        { created_at: '2026-02-25T16:30:00', endpoint: 'responses', model: 'vertex-gemini-3-flash-preview', usage_unit_type: 'tokens', usage_unit_count: 23100, image_count: 0, input_tokens: 18900, output_tokens: 4200, total_tokens: 23100, cost_cents: 62, cost_usd: 0.62, duration_ms: 4800, status_code: 200, billable_sku: 'vertex-gemini-3-flash-preview-text' },
        { created_at: '2026-02-25T14:10:00', endpoint: 'chat/completions', model: 'vertex-gemini-2.5-pro', usage_unit_type: 'tokens', usage_unit_count: 6080, image_count: 0, input_tokens: 5100, output_tokens: 980, total_tokens: 6080, cost_cents: 15, cost_usd: 0.15, duration_ms: 1400, status_code: 200, billable_sku: 'vertex-gemini-2.5-pro-text' },
        { created_at: '2026-02-24T21:00:00', endpoint: 'responses', model: 'vertex-gemini-2.5-flash', usage_unit_type: 'tokens', usage_unit_count: 33600, image_count: 0, input_tokens: 28000, output_tokens: 5600, total_tokens: 33600, cost_cents: 83, cost_usd: 0.83, duration_ms: 6100, status_code: 200, billable_sku: 'vertex-gemini-2.5-flash-text' },
        { created_at: '2026-02-24T19:30:00', endpoint: 'chat/completions', model: 'vertex-gemini-3.1-flash-lite-preview', usage_unit_type: 'tokens', usage_unit_count: 4950, image_count: 0, input_tokens: 4200, output_tokens: 750, total_tokens: 4950, cost_cents: 12, cost_usd: 0.12, duration_ms: 1100, status_code: 200, billable_sku: 'vertex-gemini-3.1-flash-lite-preview-text' },
    ]
}

export const PRICING_PLANS = [
    {
        name: '体验包',
        price: '¥9.9',
        priceNote: '',
        money: '9.90',
        balanceLabel: '$49.99 余额',
        features: ['$49.99 账户余额', '适合接入测试与小流量使用', '多模型可选', '按量计费 · 用多少扣多少'],
        badge: null,
        highlight: false
    },
    {
        name: '轻量版',
        price: '¥29.9',
        priceNote: '',
        money: '29.90',
        balanceLabel: '$149.99 余额',
        features: ['$149.99 账户余额', '适合日常编码和对话请求', '支持文本与图片模型', '按量计费 · 用多少扣多少'],
        badge: null,
        highlight: false
    },
    {
        name: '基础版',
        price: '¥59.9',
        priceNote: '',
        money: '59.90',
        balanceLabel: '$299.99 余额',
        features: ['$299.99 账户余额', '适合多客户端长期使用', '默认兼容文本 + 多模型', '按量计费 · 用多少扣多少'],
        badge: '最受欢迎',
        highlight: true
    },
    {
        name: '进阶版',
        price: '¥99.9',
        priceNote: '',
        money: '99.90',
        balanceLabel: '$499.99 余额',
        features: ['$499.99 账户余额', '适合团队协作与代理工作流', '文本 + 生图请求统一计费', '按量计费 · 用多少扣多少'],
        badge: null,
        highlight: false
    },
    {
        name: '专业版',
        price: '¥199.9',
        priceNote: '',
        money: '199.90',
        balanceLabel: '$999.99 余额',
        features: ['$999.99 账户余额', '适合高频自动化与批量调用', '支持稳定版与预览版模型', '按量计费 · 用多少扣多少'],
        badge: null,
        highlight: false
    },
    {
        name: '旗舰版',
        price: '¥499.9',
        priceNote: '',
        money: '499.90',
        balanceLabel: '$2499.99 余额',
        features: ['$2,499.99 账户余额', '适合多账号、长上下文和图片工作流', 'ClawFather 长期主力方案', '按量计费 · 用多少扣多少'],
        badge: '最划算',
        highlight: false
    }
]
