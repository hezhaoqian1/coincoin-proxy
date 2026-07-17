import test from 'node:test'
import assert from 'node:assert/strict'
import { getGuideCodePreview } from './guideCodePreview.js'

test('guide code previews replace every occurrence of the current secret', () => {
    const secret = 'sk_cc_test_secret'
    const code = `Authorization: Bearer ${secret}\napi_key=${secret}`

    assert.equal(
        getGuideCodePreview(code, secret),
        'Authorization: Bearer YOUR_DEVELOPER_API_KEY\napi_key=YOUR_DEVELOPER_API_KEY',
    )
})

test('guide code previews keep placeholder commands unchanged', () => {
    const code = 'Authorization: Bearer YOUR_DEVELOPER_API_KEY'
    assert.equal(getGuideCodePreview(code, ''), code)
})
