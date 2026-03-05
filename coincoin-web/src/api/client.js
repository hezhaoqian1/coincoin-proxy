const PROXY_BASE = ''

/** Get stored API key */
export function getApiKey() {
    return localStorage.getItem('coincoin_api_key') || ''
}

/** Set API key */
export function setApiKey(key) {
    localStorage.setItem('coincoin_api_key', key)
}

/** Clear API key (logout) */
export function clearApiKey() {
    localStorage.removeItem('coincoin_api_key')
    localStorage.removeItem('coincoin_user_id')
}

export function getUserId() {
    return localStorage.getItem('coincoin_user_id') || ''
}

export function setUserId(id) {
    localStorage.setItem('coincoin_user_id', id)
}

export function getUsername() {
    return localStorage.getItem('coincoin_username') || ''
}

export function setUsername(u) {
    localStorage.setItem('coincoin_username', u)
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

/** Confirm payment order (proxy verifies with payment service then adds balance) */
export async function confirmOrder(orderNo) {
    const res = await fetch(`${PROXY_BASE}/v1/orders/confirm`, {
        method: 'POST',
        headers: authHeaders(),
        body: JSON.stringify({ order_no: orderNo })
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
    price_input_per_million: 1.75,
    price_output_per_million: 14.00
}

export const MOCK_USAGE = {
    user_id: 'u_demo_user_001',
    total: 127,
    limit: 20,
    offset: 0,
    data: [
        { created_at: '2026-02-26T15:30:00', endpoint: 'responses', model: 'gpt-5.2-codex', input_tokens: 15230, output_tokens: 2100, total_tokens: 17330, cost_cents: 32, cost_usd: 0.32, duration_ms: 3420, status_code: 200 },
        { created_at: '2026-02-26T14:22:00', endpoint: 'chat/completions', model: 'gpt-5.2-codex', input_tokens: 8540, output_tokens: 1540, total_tokens: 10080, cost_cents: 23, cost_usd: 0.23, duration_ms: 2180, status_code: 200 },
        { created_at: '2026-02-26T13:15:00', endpoint: 'responses', model: 'gpt-5.2-codex', input_tokens: 22100, output_tokens: 5430, total_tokens: 27530, cost_cents: 80, cost_usd: 0.80, duration_ms: 5670, status_code: 200 },
        { created_at: '2026-02-26T12:05:00', endpoint: 'chat/completions', model: 'gpt-5.2-codex', input_tokens: 3200, output_tokens: 890, total_tokens: 4090, cost_cents: 13, cost_usd: 0.13, duration_ms: 1520, status_code: 200 },
        { created_at: '2026-02-26T10:48:00', endpoint: 'responses', model: 'gpt-5.2-codex', input_tokens: 45600, output_tokens: 8700, total_tokens: 54300, cost_cents: 130, cost_usd: 1.30, duration_ms: 8900, status_code: 200 },
        { created_at: '2026-02-25T22:30:00', endpoint: 'responses', model: 'gpt-5.2-codex', input_tokens: 12300, output_tokens: 3400, total_tokens: 15700, cost_cents: 50, cost_usd: 0.50, duration_ms: 4100, status_code: 200 },
        { created_at: '2026-02-25T20:15:00', endpoint: 'chat/completions', model: 'gpt-5.2-codex', input_tokens: 6700, output_tokens: 1200, total_tokens: 7900, cost_cents: 18, cost_usd: 0.18, duration_ms: 1800, status_code: 200 },
        { created_at: '2026-02-25T18:42:00', endpoint: 'responses', model: 'gpt-5.2-codex', input_tokens: 35000, output_tokens: 6500, total_tokens: 41500, cost_cents: 97, cost_usd: 0.97, duration_ms: 7200, status_code: 200 },
        { created_at: '2026-02-25T16:30:00', endpoint: 'responses', model: 'gpt-5.2-codex', input_tokens: 18900, output_tokens: 4200, total_tokens: 23100, cost_cents: 62, cost_usd: 0.62, duration_ms: 4800, status_code: 200 },
        { created_at: '2026-02-25T14:10:00', endpoint: 'chat/completions', model: 'gpt-5.2-codex', input_tokens: 5100, output_tokens: 980, total_tokens: 6080, cost_cents: 15, cost_usd: 0.15, duration_ms: 1400, status_code: 200 },
        { created_at: '2026-02-24T21:00:00', endpoint: 'responses', model: 'gpt-5.2-codex', input_tokens: 28000, output_tokens: 5600, total_tokens: 33600, cost_cents: 83, cost_usd: 0.83, duration_ms: 6100, status_code: 200 },
        { created_at: '2026-02-24T19:30:00', endpoint: 'chat/completions', model: 'gpt-5.2-codex', input_tokens: 4200, output_tokens: 750, total_tokens: 4950, cost_cents: 12, cost_usd: 0.12, duration_ms: 1100, status_code: 200 },
    ]
}

export const PRICING_PLANS = [
    {
        name: '体验包',
        price: '¥9.9',
        priceNote: '',
        money: '9.90',
        balanceLabel: '$18 余额',
        features: ['$18.00 账户余额', '约 1,000 次 API 调用', 'gpt-5.2-codex 模型', '按量计费 · 用多少扣多少'],
        badge: null,
        highlight: false
    },
    {
        name: '轻量版',
        price: '¥29.9',
        priceNote: '',
        money: '29.90',
        balanceLabel: '$66 余额',
        features: ['$66.00 账户余额', '约 3,900 次 API 调用', 'gpt-5.2-codex 模型', '按量计费 · 用多少扣多少'],
        badge: null,
        highlight: false
    },
    {
        name: '基础版',
        price: '¥59.9',
        priceNote: '',
        money: '59.90',
        balanceLabel: '$138 余额',
        features: ['$138.00 账户余额', '约 8,100 次 API 调用', 'gpt-5.2-codex 模型', '按量计费 · 用多少扣多少'],
        badge: '最受欢迎',
        highlight: true
    },
    {
        name: '进阶版',
        price: '¥99.9',
        priceNote: '',
        money: '99.90',
        balanceLabel: '$238 余额',
        features: ['$238.00 账户余额', '约 14,000 次 API 调用', 'gpt-5.2-codex 模型', '按量计费 · 用多少扣多少'],
        badge: null,
        highlight: false
    },
    {
        name: '专业版',
        price: '¥199.9',
        priceNote: '',
        money: '199.90',
        balanceLabel: '$518 余额',
        features: ['$518.00 账户余额', '约 30,000 次 API 调用', 'gpt-5.2-codex 模型', '按量计费 · 用多少扣多少'],
        badge: null,
        highlight: false
    },
    {
        name: '旗舰版',
        price: '¥499.9',
        priceNote: '',
        money: '499.90',
        balanceLabel: '$1388 余额',
        features: ['$1,388.00 账户余额', '约 81,000 次 API 调用', 'gpt-5.2-codex 模型', '按量计费 · 用多少扣多少'],
        badge: '最划算',
        highlight: false
    }
]
