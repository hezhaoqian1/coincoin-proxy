import test from 'node:test'
import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

const guideSource = await readFile(new URL('./GuideDetail.jsx', import.meta.url), 'utf8')

function renderTemplate(name) {
    const declaration = `const ${name} = `
    const declarationStart = guideSource.indexOf(declaration)
    assert.notEqual(declarationStart, -1, `${name} declaration should exist`)

    const expressionStart = declarationStart + declaration.length
    const templateStart = guideSource.indexOf('`', expressionStart)
    const templateEnd = guideSource.indexOf('`', templateStart + 1)
    assert.notEqual(templateStart, -1, `${name} should use a template literal`)
    assert.notEqual(templateEnd, -1, `${name} template literal should terminate`)

    const templateTag = guideSource.slice(expressionStart, templateStart).trim()
    const templateBody = guideSource.slice(templateStart + 1, templateEnd)
    const render = new Function(
        'OPENAI_BASE_URL',
        'snippetKey',
        `return ${templateTag}\`${templateBody}\``,
    )

    return render('https://coincoin.ai/v1', 'test-coincoin-key')
}

test('macOS and Linux Grok command preserves and applies the Python config editor', async () => {
    const command = renderTemplate('grokBuildCommand')
    const pythonBlock = command.match(/python3 <<'PY'\n([\s\S]*?)\nPY/)

    assert.ok(pythonBlock, 'generated command should contain the Python config editor')
    assert.match(command, /r"\(\?ms\)\^\\\[model\\\.grok-build\\\]\\s\*\.\*\?\(\?=\^\\\[\|\\Z\)"/)
    assert.match(command, /r"\(\?ms\)\^\\\[model\\\.grok-4\\\.5\\\]\\s\*\.\*\?\(\?=\^\\\[\|\\Z\)"/)
    assert.match(command, /r'\(\?ms\)\^\\\[model\\\."grok-4\\\.5"\\\]\\s\*\.\*\?\(\?=\^\\\[\|\\Z\)'/)
    assert.ok(command.includes(`section.rstrip() + '\\ndefault = "grok-4.5"\\n'`))
    assert.match(command, /printf 'Grok Build config written to %s\\n' "\$CONFIG"\ngrok inspect\n/)

    const syntaxCheck = spawnSync('python3', ['-c', 'import sys; compile(sys.stdin.read(), "grok-config", "exec")'], {
        input: pythonBlock[1],
        encoding: 'utf8',
    })
    assert.equal(syntaxCheck.status, 0, syntaxCheck.stderr)

    const home = await mkdtemp(join(tmpdir(), 'coincoin-grok-guide-'))
    const configPath = join(home, 'config.toml')
    try {
        await writeFile(configPath, '[cli]\ninstaller = "npm"\n\n[model.grok-build]\nmodel = "grok-build"\n\n[model."grok-4.5"]\nmodel = "stale"\n', 'utf8')
        const applyConfig = spawnSync('python3', ['-c', pythonBlock[1]], {
            encoding: 'utf8',
            env: { ...process.env, GROK_CONFIG: configPath },
        })
        assert.equal(applyConfig.status, 0, applyConfig.stderr)

        const config = await readFile(configPath, 'utf8')
        assert.match(config, /\[cli\]\ninstaller = "npm"/)
        assert.match(config, /\[models\]\ndefault = "grok-4\.5"/)
        assert.match(config, /web_search = "grok-4\.5"/)
        assert.match(config, /\[model\."grok-4\.5"\]/)
        assert.doesNotMatch(config, /model = "stale"/)
        assert.doesNotMatch(config, /\[model\.grok-build\]/)
        assert.match(config, /base_url = "https:\/\/coincoin\.ai\/v1"/)
        assert.match(config, /api_key = "test-coincoin-key"/)
        assert.match(config, /api_backend = "responses"/)
        assert.match(config, /supports_backend_search = true/)
        assert.match(command, /COINCOIN_GROK_WEB_SEARCH_OK/)

        const tomlCheck = spawnSync('python3', ['-c', [
            'import sys, tomllib',
            'with open(sys.argv[1], "rb") as handle:',
            '    config = tomllib.load(handle)',
            'assert config["models"]["default"] == "grok-4.5"',
            'assert config["model"]["grok-4.5"]["model"] == "grok-4.5"',
        ].join('\n'), configPath], { encoding: 'utf8' })
        assert.equal(tomlCheck.status, 0, tomlCheck.stderr)
    } finally {
        await rm(home, { recursive: true, force: true })
    }
})

test('Windows Grok command preserves PowerShell regex escapes', () => {
    const command = renderTemplate('grokBuildWindowsCommand')

    assert.ok(command.includes("'(?ms)^\\[model\\.grok-build\\]\\s*.*?(?=^\\[|\\z)'"))
    assert.ok(command.includes("'(?ms)^\\[model\\.grok-4\\.5\\]\\s*.*?(?=^\\[|\\z)'"))
    assert.ok(command.includes("'(?ms)^\\[model\\.\"grok-4\\.5\"\\]\\s*.*?(?=^\\[|\\z)'"))
    assert.ok(command.includes("'(?ms)^\\[models\\]\\s*.*?(?=^\\[|\\z)'"))
    assert.ok(command.includes("'web_search = \"grok-4.5\"'"))
    assert.ok(command.includes('supports_backend_search = true'))
    assert.match(command, /COINCOIN_GROK_WEB_SEARCH_OK/)
    assert.match(command, /Write-Host "Grok Build config written to \$Config"\ngrok inspect\n/)
})

test('Grok guide explains the user config path and login-free outcome', () => {
    assert.match(guideSource, /用户级 `~\/\.grok\/config\.toml`/)
    assert.match(guideSource, /成功后无需登录 xAI 账号/)
    assert.match(guideSource, /写入并检查 `~\/\.grok\/config\.toml`/)
})
