const PROXY_BASE = ''

function emitAuthChange() {
    if (typeof window !== 'undefined') {
        window.dispatchEvent(new Event('coincoin-auth-changed'))
    }
}

function clearLocalAuthState() {
    localStorage.removeItem('coincoin_api_key')
    localStorage.removeItem('coincoin_user_id')
    localStorage.removeItem('coincoin_username')
    localStorage.removeItem('coincoin_generated_key')
}

export function setStationContext(station) {
    if (!station?.slug) return
    localStorage.setItem('coincoin_station_slug', station.slug)
    localStorage.setItem('coincoin_station_display_name', station.display_name || station.slug)
}

export function clearStationContext() {
    localStorage.removeItem('coincoin_station_slug')
    localStorage.removeItem('coincoin_station_display_name')
}

export function getStationContext() {
    return {
        slug: localStorage.getItem('coincoin_station_slug') || '',
        displayName: localStorage.getItem('coincoin_station_display_name') || '',
    }
}

function extractErrorMessage(data, fallbackMessage) {
    const detail = data?.detail
    if (typeof detail === 'string' && detail) return detail
    if (Array.isArray(detail) && detail.length > 0) {
        const first = detail[0]
        if (typeof first === 'string') return first
        if (first && typeof first === 'object' && first.msg) return first.msg
    }
    if (detail && typeof detail === 'object' && detail.msg) return detail.msg
    return data?.error?.message || fallbackMessage
}

function isSessionExpiredMessage(message) {
    return String(message || '').toLowerCase().includes('session expired')
}

function handleAuthFailure(status, message) {
    if ((status === 401 || status === 403) && isSessionExpiredMessage(message)) {
        clearLocalAuthState()
        emitAuthChange()
    }
}

async function parseJsonResponse(res, fallbackMessage) {
    let data = {}
    try {
        data = await res.json()
    } catch {
        data = {}
    }
    if (!res.ok) {
        const message = extractErrorMessage(data, fallbackMessage)
        handleAuthFailure(res.status, message)
        throw new Error(message)
    }
    return data
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
    clearLocalAuthState()
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
    return parseJsonResponse(res, 'Failed to fetch developer key state')
}

export async function listDeveloperKeys() {
    const res = await fetch(`${PROXY_BASE}/v1/keys`, {
        headers: authHeaders()
    })
    return parseJsonResponse(res, 'Failed to list developer keys')
}

export async function createDeveloperKey(payload = {}) {
    const res = await fetch(`${PROXY_BASE}/v1/keys`, {
        method: 'POST',
        headers: authHeaders(),
        body: JSON.stringify(payload || {}),
    })
    return parseJsonResponse(res, 'Failed to create developer key')
}

export async function updateDeveloperKey(keyId, payload) {
    const res = await fetch(`${PROXY_BASE}/v1/keys/${keyId}`, {
        method: 'PATCH',
        headers: authHeaders(),
        body: JSON.stringify(payload),
    })
    return parseJsonResponse(res, 'Failed to update developer key')
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
            const message = extractErrorMessage(data, fallbackMessage)
            handleAuthFailure(res.status, message)
            throw new Error(message)
        }
        return data
    }

    const text = await res.text()
    if (!res.ok) {
        const message = text || fallbackMessage
        handleAuthFailure(res.status, message)
        throw new Error(message)
    }
    return text
}

export async function registerUser(username, email, password, referralCode, verificationId, verificationCode, stationSlug) {
    const body = { username, email, password }
    if (referralCode) body.referral_code = referralCode
    if (verificationId) body.verification_id = verificationId
    if (verificationCode) body.verification_code = verificationCode
    if (stationSlug) body.station_slug = stationSlug
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

export async function loginUser(username, password, stationSlug) {
    const body = { username, password }
    if (stationSlug) body.station_slug = stationSlug
    const res = await fetch(`${PROXY_BASE}/v1/auth/login`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
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

export async function changeAccountPassword(currentPassword, newPassword) {
    const res = await fetch(`${PROXY_BASE}/v1/auth/me/password`, {
        method: 'POST',
        headers: authHeaders(),
        body: JSON.stringify({
            current_password: currentPassword,
            new_password: newPassword,
        })
    })
    const data = await res.json()
    if (!res.ok) throw new Error(data.detail || 'failed to change password')
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
    return parseJsonResponse(res, 'Invalid API Key')
}

export async function getBillingState() {
    const res = await fetch(`${PROXY_BASE}/v1/billing/state`, {
        headers: authHeaders()
    })
    return parseJsonResponse(res, 'Failed to fetch billing state')
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
    return parseJsonResponse(res, 'Failed to fetch usage')
}

// ===== Payment APIs =====

/** Create order via proxy (proxy creates order + calls payment service) */
export async function createOrder({ name, money, pay_type = 'alipay', product_id = null }) {
    const body = { name, money, pay_type }
    if (product_id) body.product_id = product_id
    const res = await fetch(`${PROXY_BASE}/v1/orders/create`, {
        method: 'POST',
        headers: authHeaders(),
        body: JSON.stringify(body)
    })
    return res.json()
}

/** List current user's payment orders */
export async function listOrders(limit = 20) {
    const res = await fetch(`${PROXY_BASE}/v1/orders?limit=${limit}`, {
        headers: authHeaders()
    })
    const data = await parseJsonResponse(res, 'Failed to fetch orders')
    if (Array.isArray(data)) return data
    if (Array.isArray(data?.orders)) return data.orders
    if (Array.isArray(data?.data)) return data.data
    if (Array.isArray(data?.items)) return data.items
    return []
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

export async function getMyStationContext() {
    const res = await fetch(`${PROXY_BASE}/v1/stations/me/context`, {
        headers: authHeaders()
    })
    const data = await res.json()
    if (!res.ok) throw new Error(data.detail || 'Failed to fetch station context')
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

export async function getPublicStation(slug) {
    const res = await fetch(`${PROXY_BASE}/v1/stations/public/${encodeURIComponent(slug)}`)
    const data = await res.json()
    if (!res.ok) throw new Error(data.detail || 'Failed to fetch station')
    return data
}

export async function getStationAliasTargets() {
    const res = await fetch(`${PROXY_BASE}/v1/stations/me/alias-targets`, {
        headers: authHeaders()
    })
    const data = await res.json()
    if (!res.ok) throw new Error(data.detail || 'Failed to fetch station alias targets')
    return data
}

export async function getStationBranding() {
    const res = await fetch(`${PROXY_BASE}/v1/stations/me/branding`, {
        headers: authHeaders()
    })
    const data = await res.json()
    if (!res.ok) throw new Error(data.detail || 'Failed to fetch station branding')
    return data
}

export async function updateStationBranding(payload) {
    const res = await fetch(`${PROXY_BASE}/v1/stations/me/branding`, {
        method: 'PATCH',
        headers: authHeaders(),
        body: JSON.stringify(payload)
    })
    const data = await res.json()
    if (!res.ok) throw new Error(data.detail || 'Failed to update station branding')
    return data
}

export async function getStationAliases() {
    const res = await fetch(`${PROXY_BASE}/v1/stations/me/aliases`, {
        headers: authHeaders()
    })
    const data = await res.json()
    if (!res.ok) throw new Error(data.detail || 'Failed to fetch station aliases')
    return data
}

export async function updateStationAlias(aliasId, payload) {
    const res = await fetch(`${PROXY_BASE}/v1/stations/me/aliases/${aliasId}`, {
        method: 'PATCH',
        headers: authHeaders(),
        body: JSON.stringify(payload)
    })
    const data = await res.json()
    if (!res.ok) throw new Error(data.detail || 'Failed to update station alias')
    return data
}

export async function createStationAlias(payload) {
    const res = await fetch(`${PROXY_BASE}/v1/stations/me/aliases`, {
        method: 'POST',
        headers: authHeaders(),
        body: JSON.stringify(payload)
    })
    const data = await res.json()
    if (!res.ok) throw new Error(data.detail || 'Failed to create station alias')
    return data
}

export async function updateStationPricebook(entryId, payload) {
    const res = await fetch(`${PROXY_BASE}/v1/stations/me/pricebook/${entryId}`, {
        method: 'PATCH',
        headers: authHeaders(),
        body: JSON.stringify(payload)
    })
    const data = await res.json()
    if (!res.ok) throw new Error(data.detail || 'Failed to update station pricebook')
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
    { id: 'opus', object: 'model', owned_by: 'coincoin', coincoin_capabilities: ['chat/completions', 'responses'], coincoin_billable_sku: 'claude-code-compat-text', coincoin_routing_mode: 'direct', coincoin_default_for: ['text'], coincoin_metadata: { tier: 'stable' }, coincoin_price_input_per_million: 500, coincoin_price_cached_input_per_million: 50, coincoin_price_output_per_million: 2500, coincoin_price_per_image_cents: 0 },
    { id: 'claude-opus-4-8', object: 'model', owned_by: 'coincoin', coincoin_capabilities: ['chat/completions', 'responses'], coincoin_billable_sku: 'claude-code-compat-text', coincoin_routing_mode: 'direct', coincoin_delivery_lane: 'upstream_direct', coincoin_default_for: [], coincoin_metadata: { tier: 'stable', upstream_model: 'gpt-5.5', official_price_source: 'Anthropic Claude pricing' }, coincoin_price_input_per_million: 500, coincoin_price_cached_input_per_million: 50, coincoin_price_output_per_million: 2500, coincoin_price_per_image_cents: 0 },
    { id: 'claude-opus-4.8', object: 'model', owned_by: 'coincoin', coincoin_capabilities: ['chat/completions', 'responses'], coincoin_billable_sku: 'claude-code-compat-text', coincoin_routing_mode: 'direct', coincoin_delivery_lane: 'upstream_direct', coincoin_default_for: [], coincoin_metadata: { tier: 'stable', upstream_model: 'gpt-5.5', official_price_source: 'Anthropic Claude pricing' }, coincoin_price_input_per_million: 500, coincoin_price_cached_input_per_million: 50, coincoin_price_output_per_million: 2500, coincoin_price_per_image_cents: 0 },
    { id: 'sonnet', object: 'model', owned_by: 'coincoin', coincoin_capabilities: ['chat/completions', 'responses'], coincoin_billable_sku: 'claude-code-compat-text', coincoin_routing_mode: 'direct', coincoin_default_for: [], coincoin_metadata: { tier: 'stable' }, coincoin_price_input_per_million: 300, coincoin_price_cached_input_per_million: 30, coincoin_price_output_per_million: 1500, coincoin_price_per_image_cents: 0 },
    { id: 'haiku', object: 'model', owned_by: 'coincoin', coincoin_capabilities: ['chat/completions', 'responses'], coincoin_billable_sku: 'claude-code-compat-text', coincoin_routing_mode: 'direct', coincoin_default_for: [], coincoin_metadata: { tier: 'stable' }, coincoin_price_input_per_million: 100, coincoin_price_cached_input_per_million: 10, coincoin_price_output_per_million: 500, coincoin_price_per_image_cents: 0 },
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
    { id: 'gpt-image-2', object: 'model', owned_by: 'openai', coincoin_capabilities: ['images/generations', 'images/edits'], coincoin_billable_sku: 'openai-image', coincoin_routing_mode: 'direct', coincoin_delivery_lane: 'upstream_direct', coincoin_default_for: ['image'], coincoin_metadata: { tier: 'stable', official_price_source: 'OpenAI image generation pricing', official_price_basis: 'gpt-image-2 medium 1024x1024 reference price' }, coincoin_price_input_per_million: 0, coincoin_price_output_per_million: 0, coincoin_price_per_image_cents: 5.3 },
    { id: 'seedance-v2-720p', object: 'model', owned_by: 'bytedance', coincoin_capabilities: ['videos/generations'], coincoin_billable_sku: 'seedance-v2-720p-video-task', coincoin_routing_mode: 'direct', coincoin_delivery_lane: 'upstream_direct', coincoin_default_for: ['video'], coincoin_metadata: { tier: 'stable', output_resolution: '720p', upstream_contract_price_source: 'wgspai Seedance 2.0 video generation PDF' }, coincoin_price_input_per_million: 0, coincoin_price_output_per_million: 0, coincoin_price_per_image_cents: 0, coincoin_price_per_video_cents: 98 },
    { id: 'seedance-v2-720p-video', object: 'model', owned_by: 'bytedance', coincoin_capabilities: ['videos/generations'], coincoin_billable_sku: 'seedance-v2-720p-video-reference-task', coincoin_routing_mode: 'direct', coincoin_delivery_lane: 'upstream_direct', coincoin_default_for: [], coincoin_metadata: { tier: 'stable', output_resolution: '720p', upstream_contract_price_source: 'wgspai Seedance 2.0 video generation PDF' }, coincoin_price_input_per_million: 0, coincoin_price_output_per_million: 0, coincoin_price_per_image_cents: 0, coincoin_price_per_video_cents: 112 },
    { id: 'seedance-v2-1080p', object: 'model', owned_by: 'bytedance', coincoin_capabilities: ['videos/generations'], coincoin_billable_sku: 'seedance-v2-1080p-video-task', coincoin_routing_mode: 'direct', coincoin_delivery_lane: 'upstream_direct', coincoin_default_for: [], coincoin_metadata: { tier: 'stable', output_resolution: '1080p', upstream_contract_price_source: 'wgspai Seedance 2.0 video generation PDF' }, coincoin_price_input_per_million: 0, coincoin_price_output_per_million: 0, coincoin_price_per_image_cents: 0, coincoin_price_per_video_cents: 224 },
    { id: 'seedance-v2-1080p-video', object: 'model', owned_by: 'bytedance', coincoin_capabilities: ['videos/generations'], coincoin_billable_sku: 'seedance-v2-1080p-video-reference-task', coincoin_routing_mode: 'direct', coincoin_delivery_lane: 'upstream_direct', coincoin_default_for: [], coincoin_metadata: { tier: 'stable', output_resolution: '1080p', upstream_contract_price_source: 'wgspai Seedance 2.0 video generation PDF' }, coincoin_price_input_per_million: 0, coincoin_price_output_per_million: 0, coincoin_price_per_image_cents: 0, coincoin_price_per_video_cents: 280 },
    { id: 'gemini-balanced', object: 'model', owned_by: 'google', coincoin_capabilities: ['chat/completions', 'responses'], coincoin_billable_sku: 'gemini-balanced-text', coincoin_routing_mode: 'direct', coincoin_delivery_lane: 'cpa_gemini', coincoin_default_for: [], coincoin_metadata: { tier: 'stable', official_price_source: 'Google Gemini API pricing' }, coincoin_price_input_per_million: 10, coincoin_price_cached_input_per_million: 1, coincoin_price_output_per_million: 40, coincoin_price_per_image_cents: 0 },
    { id: 'gemini-fast', object: 'model', owned_by: 'google', coincoin_capabilities: ['chat/completions', 'responses'], coincoin_billable_sku: 'gemini-fast-text', coincoin_routing_mode: 'direct', coincoin_delivery_lane: 'cpa_gemini', coincoin_default_for: [], coincoin_metadata: { tier: 'stable', official_price_source: 'Google Gemini API pricing' }, coincoin_price_input_per_million: 30, coincoin_price_cached_input_per_million: 3, coincoin_price_output_per_million: 250, coincoin_price_per_image_cents: 0 },
    { id: 'gemini-reasoning', object: 'model', owned_by: 'google', coincoin_capabilities: ['chat/completions', 'responses'], coincoin_billable_sku: 'gemini-reasoning-text', coincoin_routing_mode: 'direct', coincoin_delivery_lane: 'cpa_gemini', coincoin_default_for: [], coincoin_metadata: { tier: 'stable', official_price_source: 'Google Gemini API pricing' }, coincoin_price_input_per_million: 125, coincoin_price_cached_input_per_million: 12.5, coincoin_price_output_per_million: 1000, coincoin_price_per_image_cents: 0 },
    { id: 'gemini-image', object: 'model', owned_by: 'google', coincoin_capabilities: ['images/generations', 'images/edits'], coincoin_billable_sku: 'gemini-image', coincoin_routing_mode: 'direct', coincoin_delivery_lane: 'cpa_gemini', coincoin_default_for: [], coincoin_metadata: { tier: 'stable', official_price_source: 'Google Gemini API pricing' }, coincoin_price_input_per_million: 0, coincoin_price_output_per_million: 0, coincoin_price_per_image_cents: 6.7 },
]

export async function getPublicModels(options = {}) {
    try {
        const headers = {}
        if (options.authenticated && getApiKey()) {
            headers.Authorization = `Bearer ${getApiKey()}`
        }
        const res = await fetch(`${PROXY_BASE}/v1/models`, { headers })
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

export function isVideoCapableModel(model) {
    const capabilities = model?.coincoin_capabilities || []
    return capabilities.includes('videos/generations')
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

export function getDefaultVideoModel(models = PUBLIC_MODEL_CATALOG_FALLBACK) {
    return models.find(model => (model.coincoin_default_for || []).includes('video'))
        || models.find(isVideoCapableModel)
        || null
}

export function describePublicModel(model) {
    const id = model?.id || ''
    const capabilities = model?.coincoin_capabilities || []
    if (capabilities.includes('videos/generations')) {
        if (id.endsWith('-video')) return 'Seedance 多模态参考视频模型'
        return 'Seedance 视频模型'
    }
    if (capabilities.includes('images/generations') || capabilities.includes('images/edits')) {
        if (id === 'gpt-image-2') return '默认图片模型，支持文生图和图生图，适合高质量生成、编辑和文字较多的图片'
        if (id === 'gemini-image') return 'Gemini 图片模型，显式传 model 时使用，适合 Gemini 生图和图生图工作流'
        if (id.includes('preview')) return '图片生成预览模型，适合视觉创作和风格探索'
        return '图片模型，支持文生图和图生图，适合营销图、插画和快速视觉草稿'
    }
    if (['opus', 'claude-opus-4-8', 'claude-opus-4-7', 'best', 'default', 'opus[1m]', 'opusplan'].includes(id)) {
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

export function hasModelPricingMultiplier(model) {
    return Number(model?.coincoin_model_multiplier || 1) !== 1
        || Number(model?.coincoin_output_multiplier || 1) !== 1
        || Number(model?.coincoin_image_multiplier || 1) !== 1
        || Number(model?.coincoin_video_multiplier || 1) !== 1
        || model?.coincoin_pricing_mode === 'multiplier'
}

export function formatModelPrice(model) {
    if (isVideoCapableModel(model)) {
        const cents = model?.coincoin_price_per_video_cents || 0
        return cents > 0 ? `$${(cents / 100).toFixed(3)} / video` : '按后台配置计费'
    }
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

export function centsToDollars(cents) {
    const value = Number(cents || 0)
    return value / 100
}

export function dollarsToCents(value) {
    const parsed = Number(value)
    if (!Number.isFinite(parsed) || parsed <= 0) return null
    return Math.round(parsed * 100)
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
    summary: {
        cost_cents: 534,
        cost_usd: 5.34,
        input_tokens: 203970,
        output_tokens: 41820,
        cached_tokens: 101220,
        cache_read_tokens: 101220,
        cache_creation_tokens: 15360,
        total_tokens: 245790,
        image_count: 3,
        usage_unit_count: 245790,
    },
    data: [
        { created_at: '2026-02-26T15:30:00', endpoint: 'messages', model: 'claude-opus-4-7', provider_model: 'gpt-5.5', usage_unit_type: 'tokens', usage_unit_count: 19460, image_count: 0, input_tokens: 17360, output_tokens: 2100, cached_tokens: 15360, cache_read_tokens: 15360, cache_creation_tokens: 0, total_tokens: 19460, cost_cents: 32, cost_usd: 0.32, duration_ms: 3420, status_code: 200, billable_sku: 'claude-code-compat-text' },
        { created_at: '2026-02-26T14:22:00', endpoint: 'chat/completions', model: 'gemini-fast', usage_unit_type: 'tokens', usage_unit_count: 10080, image_count: 0, input_tokens: 8540, output_tokens: 1540, cached_tokens: 0, cache_read_tokens: 0, cache_creation_tokens: 0, total_tokens: 10080, cost_cents: 23, cost_usd: 0.23, duration_ms: 2180, status_code: 200, billable_sku: 'gemini-fast-text' },
        { created_at: '2026-02-26T13:15:00', endpoint: 'responses', model: 'gemini-reasoning', usage_unit_type: 'tokens', usage_unit_count: 37530, image_count: 0, input_tokens: 32100, output_tokens: 5430, cached_tokens: 12300, cache_read_tokens: 12300, cache_creation_tokens: 15360, total_tokens: 37530, cost_cents: 80, cost_usd: 0.80, duration_ms: 5670, status_code: 200, billable_sku: 'gemini-reasoning-text' },
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
        id: 'monthly_light',
        name: '轻量月卡',
        kind: 'monthly',
        price: '¥49.9',
        priceNote: '/ 月',
        money: '49.90',
        balanceCents: 8000,
        balanceLabel: '$80 / 30 天套餐额度',
        unitLabel: '约 ¥0.62 / $1',
        features: ['$80 套餐额度', '每 30 天重置一次', '同档续费不重置本期用量', '用完后可买流量包或提前重置'],
        badge: null,
        highlight: false
    },
    {
        id: 'monthly_basic',
        name: '基础月卡',
        kind: 'monthly',
        price: '¥199',
        priceNote: '/ 月',
        money: '199.00',
        balanceCents: 40000,
        balanceLabel: '$400 / 30 天套餐额度',
        unitLabel: '约 ¥0.50 / $1',
        features: ['$400 套餐额度', '适合稳定主力使用', '高档升级按剩余天数补差', '比轻量档单价更低'],
        badge: '推荐',
        highlight: true
    },
    {
        id: 'monthly_flagship',
        name: '旗舰月卡',
        kind: 'monthly',
        price: '¥399',
        priceNote: '/ 月',
        money: '399.00',
        balanceCents: 100000,
        balanceLabel: '$1000 / 30 天套餐额度',
        unitLabel: '约 ¥0.40 / $1',
        features: ['$1,000 套餐额度', '适合高频调用和多工具工作流', '解锁全部流量包', '套餐内最低单价'],
        badge: null,
        highlight: false
    }
]

export const TRAFFIC_PACKS = [
    {
        id: 'addon_boost',
        name: '补量包',
        kind: 'addon',
        price: '¥149',
        priceNote: '',
        money: '149.00',
        balanceCents: 30000,
        balanceLabel: '$300 额外额度',
        unitLabel: '约 ¥0.50 / $1',
        features: ['$300 流量包额度', '有效期 180 天', '需要有效月卡', '轻量套餐起可买'],
        badge: null,
        highlight: false
    },
    {
        id: 'addon_project',
        name: '项目包',
        kind: 'addon',
        price: '¥399',
        priceNote: '',
        money: '399.00',
        balanceCents: 100000,
        balanceLabel: '$1000 额外额度',
        unitLabel: '约 ¥0.40 / $1',
        features: ['$1,000 流量包额度', '有效期 180 天', '基础套餐起可买', '适合一次项目或批量任务'],
        badge: '补量推荐',
        highlight: true
    },
    {
        id: 'addon_ultra',
        name: '超大包',
        kind: 'addon',
        price: '¥699',
        priceNote: '',
        money: '699.00',
        balanceCents: 200000,
        balanceLabel: '$2000 额外额度',
        unitLabel: '约 ¥0.35 / $1',
        features: ['$2,000 流量包额度', '有效期 180 天', '旗舰套餐可买', '最低补量单价'],
        badge: '最划算',
        highlight: false
    }
]
