import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

const docsSource = await readFile(new URL('./Docs.jsx', import.meta.url), 'utf8')

test('docs explain which clients need /v1 in the base URL', () => {
    assert.match(docsSource, /Base URL 要不要带 \/v1？/)
    assert.match(docsSource, /服务端会自动去掉多出来的 <code>\/v1<\/code>/)
})

test('docs show Claude Desktop gateway setup with the site root', () => {
    assert.match(docsSource, /<h3>Claude 桌面端<\/h3>/)
    assert.match(docsSource, /<span className="config-label">Gateway base URL<\/span>\s*<code>\{SITE\}<\/code>/)
    assert.match(docsSource, /<td>Claude 桌面端<\/td>/)
})
