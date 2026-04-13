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

/** Auth header helper */
function authHeaders() {
    return {
        'Authorization': `Bearer ${getApiKey()}`,
        'Content-Type': 'application/json'
    }
}

// ===== Auth APIs =====

export async function registerUser(username, password, referralCode) {
    const body = { username, password }
    if (referralCode) body.referral_code = referralCode
    const res = await fetch(`${PROXY_BASE}/v1/auth/register`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
    })
    const data = await res.json()
    if (!res.ok) throw new Error(data.detail || 'registration failed')
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
export async function getUsageLogs(limit = 50, offset = 0) {
    const res = await fetch(`${PROXY_BASE}/v1/usage?limit=${limit}&offset=${offset}`, {
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

/** Get active announcements */
export async function getAnnouncements() {
    const res = await fetch(`${PROXY_BASE}/v1/announcements`)
    if (!res.ok) return []
    return res.json()
}

// ===== Public model catalog =====

export const PUBLIC_MODEL_CATALOG_FALLBACK = [
    { id: 'gpt-5.2-codex', object: 'model', owned_by: 'openai', coincoin_provider: 'OpenAI', coincoin_provider_model: 'gpt-5.2-codex', coincoin_capabilities: ['chat/completions', 'responses'], coincoin_billable_sku: 'legacy-default-text', coincoin_routing_mode: 'legacy_auto', coincoin_default_for: ['text'], coincoin_metadata: { tier: 'stable' }, coincoin_price_input_per_million: 99, coincoin_price_output_per_million: 699, coincoin_price_per_image_cents: 0 },
    { id: 'gpt-5', object: 'model', owned_by: 'openai', coincoin_provider: 'OpenAI', coincoin_provider_model: 'gpt-5', coincoin_capabilities: ['chat/completions', 'responses'], coincoin_billable_sku: 'legacy-gpt-5-text', coincoin_routing_mode: 'legacy_auto', coincoin_default_for: [], coincoin_metadata: { tier: 'stable' }, coincoin_price_input_per_million: 99, coincoin_price_output_per_million: 699, coincoin_price_per_image_cents: 0 },
    { id: 'gpt-5.1', object: 'model', owned_by: 'openai', coincoin_provider: 'OpenAI', coincoin_provider_model: 'gpt-5.1', coincoin_capabilities: ['chat/completions', 'responses'], coincoin_billable_sku: 'legacy-gpt-5.1-text', coincoin_routing_mode: 'legacy_auto', coincoin_default_for: [], coincoin_metadata: { tier: 'stable' }, coincoin_price_input_per_million: 99, coincoin_price_output_per_million: 699, coincoin_price_per_image_cents: 0 },
    { id: 'gpt-5.1-codex', object: 'model', owned_by: 'openai', coincoin_provider: 'OpenAI', coincoin_provider_model: 'gpt-5.1-codex', coincoin_capabilities: ['chat/completions', 'responses'], coincoin_billable_sku: 'legacy-gpt-5.1-codex-text', coincoin_routing_mode: 'legacy_auto', coincoin_default_for: [], coincoin_metadata: { tier: 'stable' }, coincoin_price_input_per_million: 99, coincoin_price_output_per_million: 699, coincoin_price_per_image_cents: 0 },
    { id: 'gpt-5.1-codex-mini', object: 'model', owned_by: 'openai', coincoin_provider: 'OpenAI', coincoin_provider_model: 'gpt-5.1-codex-mini', coincoin_capabilities: ['chat/completions', 'responses'], coincoin_billable_sku: 'legacy-gpt-5.1-codex-mini-text', coincoin_routing_mode: 'legacy_auto', coincoin_default_for: [], coincoin_metadata: { tier: 'stable' }, coincoin_price_input_per_million: 99, coincoin_price_output_per_million: 699, coincoin_price_per_image_cents: 0 },
    { id: 'gpt-5.1-codex-max', object: 'model', owned_by: 'openai', coincoin_provider: 'OpenAI', coincoin_provider_model: 'gpt-5.1-codex-max', coincoin_capabilities: ['chat/completions', 'responses'], coincoin_billable_sku: 'legacy-gpt-5.1-codex-max-text', coincoin_routing_mode: 'legacy_auto', coincoin_default_for: [], coincoin_metadata: { tier: 'stable' }, coincoin_price_input_per_million: 99, coincoin_price_output_per_million: 699, coincoin_price_per_image_cents: 0 },
    { id: 'gpt-5.2', object: 'model', owned_by: 'openai', coincoin_provider: 'OpenAI', coincoin_provider_model: 'gpt-5.2', coincoin_capabilities: ['chat/completions', 'responses'], coincoin_billable_sku: 'legacy-gpt-5.2-text', coincoin_routing_mode: 'legacy_auto', coincoin_default_for: [], coincoin_metadata: { tier: 'stable' }, coincoin_price_input_per_million: 99, coincoin_price_output_per_million: 699, coincoin_price_per_image_cents: 0 },
    { id: 'gpt-5.3-codex', object: 'model', owned_by: 'openai', coincoin_provider: 'OpenAI', coincoin_provider_model: 'gpt-5.3-codex', coincoin_capabilities: ['chat/completions', 'responses'], coincoin_billable_sku: 'legacy-gpt-5.3-codex-text', coincoin_routing_mode: 'legacy_auto', coincoin_default_for: [], coincoin_metadata: { tier: 'stable' }, coincoin_price_input_per_million: 99, coincoin_price_output_per_million: 699, coincoin_price_per_image_cents: 0 },
    { id: 'gpt-5.4-mini', object: 'model', owned_by: 'openai', coincoin_provider: 'OpenAI', coincoin_provider_model: 'gpt-5.4-mini', coincoin_capabilities: ['chat/completions', 'responses'], coincoin_billable_sku: 'legacy-gpt-5.4-mini-text', coincoin_routing_mode: 'legacy_auto', coincoin_default_for: [], coincoin_metadata: { tier: 'stable' }, coincoin_price_input_per_million: 99, coincoin_price_output_per_million: 699, coincoin_price_per_image_cents: 0 },
    { id: 'gpt-5-codex', object: 'model', owned_by: 'openai', coincoin_provider: 'OpenAI', coincoin_provider_model: 'gpt-5-codex', coincoin_capabilities: ['chat/completions', 'responses'], coincoin_billable_sku: 'legacy-gpt-5-codex-text', coincoin_routing_mode: 'legacy_auto', coincoin_default_for: [], coincoin_metadata: { tier: 'stable' }, coincoin_price_input_per_million: 99, coincoin_price_output_per_million: 699, coincoin_price_per_image_cents: 0 },
    { id: 'gpt-5-codex-mini', object: 'model', owned_by: 'openai', coincoin_provider: 'OpenAI', coincoin_provider_model: 'gpt-5-codex-mini', coincoin_capabilities: ['chat/completions', 'responses'], coincoin_billable_sku: 'legacy-gpt-5-codex-mini-text', coincoin_routing_mode: 'legacy_auto', coincoin_default_for: [], coincoin_metadata: { tier: 'stable' }, coincoin_price_input_per_million: 99, coincoin_price_output_per_million: 699, coincoin_price_per_image_cents: 0 },
    { id: 'text-embedding-3-small', object: 'model', owned_by: 'openai', coincoin_provider: 'OpenAI', coincoin_provider_model: 'text-embedding-3-small', coincoin_capabilities: ['embeddings'], coincoin_billable_sku: 'azure-text-embedding-3-small', coincoin_routing_mode: 'direct', coincoin_delivery_lane: 'upstream_direct', coincoin_default_for: ['embedding'], coincoin_metadata: { tier: 'stable' }, coincoin_price_input_per_million: 99, coincoin_price_output_per_million: 0, coincoin_price_per_image_cents: 0 },
    { id: 'gemini-balanced', object: 'model', owned_by: 'google', coincoin_provider: 'Google', coincoin_provider_model: 'gemini-2.5-flash-lite', coincoin_capabilities: ['chat/completions', 'responses'], coincoin_billable_sku: 'gemini-balanced-text', coincoin_routing_mode: 'direct', coincoin_default_for: [], coincoin_metadata: { tier: 'stable' }, coincoin_price_input_per_million: 0, coincoin_price_output_per_million: 0, coincoin_price_per_image_cents: 0 },
    { id: 'gemini-fast', object: 'model', owned_by: 'google', coincoin_provider: 'Google', coincoin_provider_model: 'gemini-2.5-flash', coincoin_capabilities: ['chat/completions', 'responses'], coincoin_billable_sku: 'gemini-fast-text', coincoin_routing_mode: 'direct', coincoin_default_for: [], coincoin_metadata: { tier: 'stable' }, coincoin_price_input_per_million: 0, coincoin_price_output_per_million: 0, coincoin_price_per_image_cents: 0 },
    { id: 'gemini-reasoning', object: 'model', owned_by: 'google', coincoin_provider: 'Google', coincoin_provider_model: 'gemini-2.5-pro', coincoin_capabilities: ['chat/completions', 'responses'], coincoin_billable_sku: 'gemini-reasoning-text', coincoin_routing_mode: 'direct', coincoin_default_for: [], coincoin_metadata: { tier: 'stable' }, coincoin_price_input_per_million: 0, coincoin_price_output_per_million: 0, coincoin_price_per_image_cents: 0 },
    { id: 'vertex-gemini-2.5-flash-lite', object: 'model', owned_by: 'google', coincoin_provider: 'Google', coincoin_provider_model: 'gemini-2.5-flash-lite', coincoin_capabilities: ['chat/completions', 'responses'], coincoin_billable_sku: 'vertex-gemini-2.5-flash-lite-text', coincoin_routing_mode: 'direct', coincoin_default_for: [], coincoin_metadata: { tier: 'explicit' }, coincoin_price_input_per_million: 0, coincoin_price_output_per_million: 0, coincoin_price_per_image_cents: 0 },
    { id: 'vertex-gemini-2.5-flash', object: 'model', owned_by: 'google', coincoin_provider: 'Google', coincoin_provider_model: 'gemini-2.5-flash', coincoin_capabilities: ['chat/completions', 'responses'], coincoin_billable_sku: 'vertex-gemini-2.5-flash-text', coincoin_routing_mode: 'direct', coincoin_default_for: [], coincoin_metadata: { tier: 'explicit' }, coincoin_price_input_per_million: 0, coincoin_price_output_per_million: 0, coincoin_price_per_image_cents: 0 },
    { id: 'vertex-gemini-2.5-pro', object: 'model', owned_by: 'google', coincoin_provider: 'Google', coincoin_provider_model: 'gemini-2.5-pro', coincoin_capabilities: ['chat/completions', 'responses'], coincoin_billable_sku: 'vertex-gemini-2.5-pro-text', coincoin_routing_mode: 'direct', coincoin_default_for: [], coincoin_metadata: { tier: 'explicit' }, coincoin_price_input_per_million: 0, coincoin_price_output_per_million: 0, coincoin_price_per_image_cents: 0 },
    { id: 'vertex-gemini-3.1-flash-lite-preview', object: 'model', owned_by: 'google', coincoin_provider: 'Google', coincoin_provider_model: 'gemini-3.1-flash-lite-preview', coincoin_capabilities: ['chat/completions', 'responses'], coincoin_billable_sku: 'vertex-gemini-3.1-flash-lite-preview-text', coincoin_routing_mode: 'direct', coincoin_default_for: [], coincoin_metadata: { tier: 'preview' }, coincoin_price_input_per_million: 0, coincoin_price_output_per_million: 0, coincoin_price_per_image_cents: 0 },
    { id: 'vertex-gemini-3-flash-preview', object: 'model', owned_by: 'google', coincoin_provider: 'Google', coincoin_provider_model: 'gemini-3-flash-preview', coincoin_capabilities: ['chat/completions', 'responses'], coincoin_billable_sku: 'vertex-gemini-3-flash-preview-text', coincoin_routing_mode: 'direct', coincoin_default_for: [], coincoin_metadata: { tier: 'preview' }, coincoin_price_input_per_million: 0, coincoin_price_output_per_million: 0, coincoin_price_per_image_cents: 0 },
    { id: 'vertex-gemini-3.1-pro-preview', object: 'model', owned_by: 'google', coincoin_provider: 'Google', coincoin_provider_model: 'gemini-3.1-pro-preview', coincoin_capabilities: ['chat/completions', 'responses'], coincoin_billable_sku: 'vertex-gemini-3.1-pro-preview-text', coincoin_routing_mode: 'direct', coincoin_default_for: [], coincoin_metadata: { tier: 'preview' }, coincoin_price_input_per_million: 0, coincoin_price_output_per_million: 0, coincoin_price_per_image_cents: 0 },
    { id: 'gemini-image', object: 'model', owned_by: 'google', coincoin_provider: 'Google', coincoin_provider_model: 'gemini-2.5-flash-image', coincoin_capabilities: ['images/generations', 'images/edits'], coincoin_billable_sku: 'gemini-image', coincoin_routing_mode: 'direct', coincoin_default_for: ['image'], coincoin_metadata: { tier: 'stable' }, coincoin_price_input_per_million: 0, coincoin_price_output_per_million: 0, coincoin_price_per_image_cents: 0 },
    { id: 'vertex-gemini-2.5-flash-image', object: 'model', owned_by: 'google', coincoin_provider: 'Google', coincoin_provider_model: 'gemini-2.5-flash-image', coincoin_capabilities: ['images/generations', 'images/edits'], coincoin_billable_sku: 'vertex-gemini-2.5-flash-image', coincoin_routing_mode: 'direct', coincoin_default_for: [], coincoin_metadata: { tier: 'explicit' }, coincoin_price_input_per_million: 0, coincoin_price_output_per_million: 0, coincoin_price_per_image_cents: 0 },
    { id: 'vertex-gemini-3.1-flash-image-preview', object: 'model', owned_by: 'google', coincoin_provider: 'Google', coincoin_provider_model: 'gemini-3.1-flash-image-preview', coincoin_capabilities: ['images/generations', 'images/edits'], coincoin_billable_sku: 'vertex-gemini-3.1-flash-image-preview', coincoin_routing_mode: 'direct', coincoin_default_for: [], coincoin_metadata: { tier: 'preview' }, coincoin_price_input_per_million: 0, coincoin_price_output_per_million: 0, coincoin_price_per_image_cents: 0 },
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
        if (id.includes('3.1')) return 'Gemini 图片生成预览模型，适合更强视觉创作和风格探索'
        return 'Gemini 图片模型，支持文生图和图生图，适合营销图、插画和快速视觉草稿'
    }
    if (id === 'gpt-5.2-codex') {
        return '默认兼容文本模型，保留给旧客户端和 Codex 风格工作流'
    }
    if (id.startsWith('gpt-5')) {
        if (id.includes('codex-max')) return '高配 Codex 文本模型，适合复杂代码生成、重构和 agent 工作流'
        if (id.includes('codex')) return 'Codex 风格文本模型，适合编程、补全和工具调用场景'
        if (id.includes('mini')) return '轻量 GPT 文本模型，适合低延迟、多轮对话和批量任务'
        return 'GPT 文本模型，适合通用对话、写作和兼容 OpenAI 风格客户端'
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

export function formatModelPrice(model) {
    if (isImageCapableModel(model)) {
        const cents = model?.coincoin_price_per_image_cents || 0
        return cents > 0 ? `$${(cents / 100).toFixed(2)} / image` : '按后台配置计费'
    }
    const input = model?.coincoin_price_input_per_million || 0
    const output = model?.coincoin_price_output_per_million || 0
    if (!input && !output) return '按后台配置计费'
    return `Input $${(input / 100).toFixed(2)} / M · Output $${(output / 100).toFixed(2)} / M`
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
    price_output_per_million: 6.99
}

export const MOCK_USAGE = {
    user_id: 'u_demo_user_001',
    total: 127,
    limit: 20,
    offset: 0,
    data: [
        { created_at: '2026-02-26T15:30:00', endpoint: 'responses', model: 'gpt-5.2-codex', provider_model: 'gpt-5.2-codex', usage_unit_type: 'tokens', usage_unit_count: 17330, image_count: 0, input_tokens: 15230, output_tokens: 2100, total_tokens: 17330, cost_cents: 32, cost_usd: 0.32, duration_ms: 3420, status_code: 200, billable_sku: 'legacy-default-text' },
        { created_at: '2026-02-26T14:22:00', endpoint: 'chat/completions', model: 'gemini-fast', provider_model: 'gemini-2.5-flash', usage_unit_type: 'tokens', usage_unit_count: 10080, image_count: 0, input_tokens: 8540, output_tokens: 1540, total_tokens: 10080, cost_cents: 23, cost_usd: 0.23, duration_ms: 2180, status_code: 200, billable_sku: 'gemini-fast-text' },
        { created_at: '2026-02-26T13:15:00', endpoint: 'responses', model: 'gemini-reasoning', provider_model: 'gemini-2.5-pro', usage_unit_type: 'tokens', usage_unit_count: 27530, image_count: 0, input_tokens: 22100, output_tokens: 5430, total_tokens: 27530, cost_cents: 80, cost_usd: 0.80, duration_ms: 5670, status_code: 200, billable_sku: 'gemini-reasoning-text' },
        { created_at: '2026-02-26T12:05:00', endpoint: 'images/generations', model: 'gemini-image', provider_model: 'gemini-2.5-flash-image', usage_unit_type: 'images', usage_unit_count: 2, image_count: 2, input_tokens: 0, output_tokens: 0, total_tokens: 0, cost_cents: 18, cost_usd: 0.18, duration_ms: 6520, status_code: 200, billable_sku: 'gemini-image' },
        { created_at: '2026-02-26T10:48:00', endpoint: 'responses', model: 'vertex-gemini-3.1-pro-preview', provider_model: 'gemini-3.1-pro-preview', usage_unit_type: 'tokens', usage_unit_count: 54300, image_count: 0, input_tokens: 45600, output_tokens: 8700, total_tokens: 54300, cost_cents: 130, cost_usd: 1.30, duration_ms: 8900, status_code: 200, billable_sku: 'vertex-gemini-3.1-pro-preview-text' },
        { created_at: '2026-02-25T22:30:00', endpoint: 'responses', model: 'gemini-balanced', provider_model: 'gemini-2.5-flash-lite', usage_unit_type: 'tokens', usage_unit_count: 15700, image_count: 0, input_tokens: 12300, output_tokens: 3400, total_tokens: 15700, cost_cents: 50, cost_usd: 0.50, duration_ms: 4100, status_code: 200, billable_sku: 'gemini-balanced-text' },
        { created_at: '2026-02-25T20:15:00', endpoint: 'chat/completions', model: 'vertex-gemini-2.5-flash-lite', provider_model: 'gemini-2.5-flash-lite', usage_unit_type: 'tokens', usage_unit_count: 7900, image_count: 0, input_tokens: 6700, output_tokens: 1200, total_tokens: 7900, cost_cents: 18, cost_usd: 0.18, duration_ms: 1800, status_code: 200, billable_sku: 'vertex-gemini-2.5-flash-lite-text' },
        { created_at: '2026-02-25T18:42:00', endpoint: 'images/generations', model: 'vertex-gemini-3.1-flash-image-preview', provider_model: 'gemini-3.1-flash-image-preview', usage_unit_type: 'images', usage_unit_count: 1, image_count: 1, input_tokens: 0, output_tokens: 0, total_tokens: 0, cost_cents: 11, cost_usd: 0.11, duration_ms: 7010, status_code: 200, billable_sku: 'vertex-gemini-3.1-flash-image-preview' },
        { created_at: '2026-02-25T16:30:00', endpoint: 'responses', model: 'vertex-gemini-3-flash-preview', provider_model: 'gemini-3-flash-preview', usage_unit_type: 'tokens', usage_unit_count: 23100, image_count: 0, input_tokens: 18900, output_tokens: 4200, total_tokens: 23100, cost_cents: 62, cost_usd: 0.62, duration_ms: 4800, status_code: 200, billable_sku: 'vertex-gemini-3-flash-preview-text' },
        { created_at: '2026-02-25T14:10:00', endpoint: 'chat/completions', model: 'vertex-gemini-2.5-pro', provider_model: 'gemini-2.5-pro', usage_unit_type: 'tokens', usage_unit_count: 6080, image_count: 0, input_tokens: 5100, output_tokens: 980, total_tokens: 6080, cost_cents: 15, cost_usd: 0.15, duration_ms: 1400, status_code: 200, billable_sku: 'vertex-gemini-2.5-pro-text' },
        { created_at: '2026-02-24T21:00:00', endpoint: 'responses', model: 'vertex-gemini-2.5-flash', provider_model: 'gemini-2.5-flash', usage_unit_type: 'tokens', usage_unit_count: 33600, image_count: 0, input_tokens: 28000, output_tokens: 5600, total_tokens: 33600, cost_cents: 83, cost_usd: 0.83, duration_ms: 6100, status_code: 200, billable_sku: 'vertex-gemini-2.5-flash-text' },
        { created_at: '2026-02-24T19:30:00', endpoint: 'chat/completions', model: 'vertex-gemini-3.1-flash-lite-preview', provider_model: 'gemini-3.1-flash-lite-preview', usage_unit_type: 'tokens', usage_unit_count: 4950, image_count: 0, input_tokens: 4200, output_tokens: 750, total_tokens: 4950, cost_cents: 12, cost_usd: 0.12, duration_ms: 1100, status_code: 200, billable_sku: 'vertex-gemini-3.1-flash-lite-preview-text' },
    ]
}

export const PRICING_PLANS = [
    {
        name: '体验包',
        price: '¥9.9',
        priceNote: '',
        money: '9.90',
        balanceLabel: '$49.99 余额',
        features: ['$49.99 账户余额', '适合接入测试与小流量使用', 'GPT + Gemini 多模型可选', '按量计费 · 用多少扣多少'],
        badge: null,
        highlight: false
    },
    {
        name: '轻量版',
        price: '¥29.9',
        priceNote: '',
        money: '29.90',
        balanceLabel: '$149.99 余额',
        features: ['$149.99 账户余额', '适合日常编码和对话请求', '支持 Gemini 文本与图片模型', '按量计费 · 用多少扣多少'],
        badge: null,
        highlight: false
    },
    {
        name: '基础版',
        price: '¥59.9',
        priceNote: '',
        money: '59.90',
        balanceLabel: '$299.99 余额',
        features: ['$299.99 账户余额', '适合多客户端长期使用', '默认 GPT 兼容 + Gemini 多模型', '按量计费 · 用多少扣多少'],
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
        features: ['$999.99 账户余额', '适合高频自动化与批量调用', '支持稳定版与预览版 Gemini', '按量计费 · 用多少扣多少'],
        badge: null,
        highlight: false
    },
    {
        name: '旗舰版',
        price: '¥499.9',
        priceNote: '',
        money: '499.90',
        balanceLabel: '$2499.99 余额',
        features: ['$2,499.99 账户余额', '适合多账号、长上下文和图片工作流', 'CoinCoin 长期主力方案', '按量计费 · 用多少扣多少'],
        badge: '最划算',
        highlight: false
    }
]
