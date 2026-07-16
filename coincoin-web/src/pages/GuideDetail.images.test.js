import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

const guideSource = await readFile(new URL('./GuideDetail.jsx', import.meta.url), 'utf8')

test('image guide covers synchronous and asynchronous generation on both platforms', () => {
    assert.match(guideSource, /macOS \/ Linux 文生图/)
    assert.match(guideSource, /Windows PowerShell 文生图/)
    assert.match(guideSource, /macOS \/ Linux 异步文生图/)
    assert.match(guideSource, /Windows PowerShell 异步文生图/)
})

test('image guide downloads URL results with curl instead of urllib', () => {
    assert.doesNotMatch(guideSource, /urlretrieve|Invoke-WebRequest/)
    assert.match(guideSource, /subprocess\.run\(\["curl", "-L", "-sS", "--fail"/)
    assert.match(guideSource, /curl\.exe -L -sS --fail -o \$Output \$Item\.url/)
})

test('image guide handles URL and base64 results for edits and jobs', () => {
    assert.match(guideSource, /elif item\.get\("url"\):/)
    assert.match(guideSource, /elseif \(\$Item\.url\)/)
    assert.match(guideSource, /data\.get\("result", \{\}\)\.get\("data"\)/)
})
