import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

const appShellSource = await readFile(new URL('./AppShell.jsx', import.meta.url), 'utf8')
const docsSource = await readFile(new URL('../pages/Docs.jsx', import.meta.url), 'utf8')
const guideSource = await readFile(new URL('../pages/GuideDetail.jsx', import.meta.url), 'utf8')

test('integration guide navigation exposes the Grok quickstart', () => {
    assert.match(appShellSource, /to: '\/guides\/grok-build'.*label: 'Grok 快速接入'/)
})

test('models page links Grok models to the quickstart guide', () => {
    assert.match(docsSource, /to="\/guides\/grok-build"/)
    assert.match(docsSource, /grok-4\.5 · grok-build/)
})

test('the Grok quickstart link resolves to a configured guide', () => {
    assert.match(guideSource, /'grok-build':\s*\{/)
    assert.match(guideSource, /title: 'Grok Build 配置'/)
})
