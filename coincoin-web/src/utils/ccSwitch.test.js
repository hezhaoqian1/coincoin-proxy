import test from 'node:test'
import assert from 'node:assert/strict'
import { buildCcSwitchLink, getCcSwitchEndpoint, getKeyAvailability, isFullDeveloperKey, selectDeveloperKey } from './ccSwitch.js'

const input = { apiKey: 'sk_cc_test_only_0123456789', baseUrl: 'https://gateway.example', keyName: '开发 & 测试 #1' }

test('Codex imports use the Responses base URL and preserve encoded provider names and keys', () => {
    const link = new URL(buildCcSwitchLink({ ...input, app: 'codex' }))
    assert.equal(link.protocol, 'ccswitch:')
    assert.equal(link.host, 'v1')
    assert.equal(link.pathname, '/import')
    const params = link.searchParams
    assert.equal(params.get('resource'), 'provider')
    assert.equal(params.get('app'), 'codex')
    assert.equal(params.get('endpoint'), 'https://gateway.example/v1')
    assert.equal(params.get('apiKey'), input.apiKey)
    assert.equal(params.get('name'), 'ClawFather · 开发 & 测试 #1')
    assert.equal(params.get('model'), 'gpt-5.6-sol')
    assert.equal(params.has('configUrl'), false)
    assert.equal(params.has('usageScript'), false)
})

test('Claude imports select the Anthropic root and the documented model', () => {
    const params = new URL(buildCcSwitchLink({ ...input, app: 'claude' })).searchParams
    assert.equal(params.get('app'), 'claude')
    assert.equal(params.get('endpoint'), input.baseUrl)
    assert.equal(params.get('model'), 'claude-sonnet-4-6')
    assert.equal(params.get('homepage'), input.baseUrl)
})

test('trailing slashes and existing v1 suffix never create duplicate API paths', () => {
    for (const suffix of ['', '/', '/v1', '/v1/']) {
        assert.equal(getCcSwitchEndpoint(`${input.baseUrl}${suffix}`, 'codex'), `${input.baseUrl}/v1`)
        assert.equal(getCcSwitchEndpoint(`${input.baseUrl}${suffix}`, 'claude'), input.baseUrl)
    }
})

test('unusable credentials cannot become import links', () => {
    for (const apiKey of ['', undefined, 'sk_cc_xxxxx', 'sk_cc_demo_key', 'sk_cc_ab...cdef', 'sk_cc_ab••••cdef', 'YOUR_DEVELOPER_API_KEY', 'session_token', 'sk_cc_key&app=gemini', 'sk_cc_key\n']) {
        assert.equal(isFullDeveloperKey(apiKey), false, String(apiKey))
        assert.throws(() => buildCcSwitchLink({ ...input, app: 'codex', apiKey }))
    }
})

test('console sessions are never selected as developer keys', () => {
    const sessionKey = 'sk_cc_console_session_0123456789'
    assert.equal(selectDeveloperKey({ apiKey: sessionKey, isConsoleSession: true }), '')
    assert.equal(selectDeveloperKey({ apiKey: input.apiKey, isConsoleSession: false }), input.apiKey)
    assert.equal(selectDeveloperKey({ apiKey: sessionKey, isConsoleSession: true, recoverableApiKey: input.apiKey }), input.apiKey)
    assert.equal(selectDeveloperKey({ apiKey: sessionKey, isConsoleSession: true, generatedApiKey: input.apiKey }), input.apiKey)
})

test('unsupported clients and invalid endpoints fail before navigation', () => {
    for (const app of ['gemini', 'constructor', '__proto__', '']) {
        assert.throws(() => buildCcSwitchLink({ ...input, app }))
    }
    const credentialedUrl = new URL('https://example.com')
    credentialedUrl.username = 'user'
    credentialedUrl.password = 'pass'
    for (const baseUrl of ['javascript:alert(1)', credentialedUrl.href, 'https://example.com?key=secret', 'https://example.com/#hash']) {
        assert.throws(() => buildCcSwitchLink({ ...input, app: 'claude', baseUrl }))
    }
})

test('disabled, expired and exhausted keys cannot be offered as usable', () => {
    const now = Date.parse('2026-09-20T08:00:00Z')
    assert.equal(getKeyAvailability({ status: 'active' }, now).usable, true)
    assert.equal(getKeyAvailability({ status: 'disabled' }, now).usable, false)
    assert.equal(getKeyAvailability({ status: 'active', expires_at: '2026-09-20T08:00:00' }, now).label, '已过期')
    assert.equal(getKeyAvailability({ status: 'active', expires_at: '2026-09-20T08:00:01' }, now).usable, true)
    for (const period of ['monthly', 'total']) {
        assert.equal(getKeyAvailability({ status: 'active', [`${period}_quota_cents`]: 100, [`${period}_used_cents`]: 100 }, now).label, '额度用尽')
        assert.equal(getKeyAvailability({ status: 'active', [`${period}_quota_cents`]: 0, [`${period}_used_cents`]: 100 }, now).usable, true)
    }
})
