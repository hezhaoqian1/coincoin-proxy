import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

const guideSource = await readFile(new URL('./GuideDetail.jsx', import.meta.url), 'utf8')

test('image guide covers synchronous and asynchronous generation on both platforms', () => {
    assert.match(guideSource, /macOS \/ Linux 同步文生图/)
    assert.match(guideSource, /Windows PowerShell 同步文生图/)
    assert.match(guideSource, /macOS \/ Linux 异步文生图/)
    assert.match(guideSource, /Windows PowerShell 异步文生图/)
})

test('image guide groups commands by task and platform', () => {
    assert.match(guideSource, /commandTasks:/)
    assert.match(guideSource, /title: '同步文生图'/)
    assert.match(guideSource, /title: '异步文生图'/)
    assert.match(guideSource, /title: '单图图生图'/)
    assert.match(guideSource, /title: '查看用量'/)
    assert.doesNotMatch(guideSource, /title: 'Gemini 多图'/)
    assert.doesNotMatch(guideSource, /code: multiImageEditCommand/)
    assert.doesNotMatch(guideSource, /const multiImageEditCommand/)
    assert.doesNotMatch(guideSource, /const multiImageEditWindowsCommand/)
    assert.match(guideSource, /GuideTaskTabs tasks=\{guide\.commandTasks\}/)
})

test('image guide includes a Windows-native usage command', () => {
    assert.match(guideSource, /const usageWindowsCommand = `curl\.exe/)
    assert.match(guideSource, /title: 'Windows PowerShell 查看最近用量'/)
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

test('guide previews hide the secret while copy buttons keep the executable command', () => {
    assert.match(guideSource, /getGuideCodePreview\(code, secret\)/)
    assert.match(guideSource, /<CopyButton\s+text=\{code\}/)
    assert.match(guideSource, /<pre className="guide-code-block">\{previewCode\}<\/pre>/)
})
