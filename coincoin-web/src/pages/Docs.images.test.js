import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

const docsSource = await readFile(new URL('./Docs.jsx', import.meta.url), 'utf8')

test('quickstart labels the image section for both generation and editing', () => {
    assert.match(docsSource, /<h3>图片生成与图生图<\/h3>/)
    assert.doesNotMatch(docsSource, /<h3>Gemini 生图<\/h3>/)
})

test('image API reference explains timeout, size, and URL result behavior', () => {
    assert.match(docsSource, /客户端自身的总超时/)
    assert.match(docsSource, /1K.*2K.*4K/)
    assert.match(docsSource, /curl -L/)
    assert.match(docsSource, /拆成创建和轮询多个短请求/)
})

test('model examples display a placeholder key while copy uses the populated key', () => {
    assert.match(docsSource, /const copyKey = effectiveApiKey/)
    assert.match(docsSource, /const previewKey = effectiveApiKey \? 'YOUR_DEVELOPER_API_KEY'/)
    assert.match(docsSource, /Authorization: Bearer \$\{copyKey\}/)
    assert.match(docsSource, /Authorization: Bearer \$\{previewKey\}/)
})
