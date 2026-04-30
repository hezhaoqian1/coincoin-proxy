import { useMemo, useState } from 'react'
import { Link, Navigate, useParams } from 'react-router-dom'
import AppShell from '../components/AppShell'
import { useAuth } from '../hooks/useAuth'
import { usePublicModels } from '../hooks/usePublicModels'
import './GuideDetail.css'

const SITE_ROOT = typeof window !== 'undefined' ? window.location.origin : ''
const OPENAI_BASE_URL = SITE_ROOT ? `${SITE_ROOT}/v1` : '/v1'

function CopyButton({ text, idleLabel = '复制', doneLabel = '已复制' }) {
    const [copied, setCopied] = useState(false)

    const handleCopy = async () => {
        await navigator.clipboard.writeText(text)
        setCopied(true)
        window.setTimeout(() => setCopied(false), 2000)
    }

    return (
        <button className="btn btn-primary btn-sm" onClick={handleCopy}>
            {copied ? `\u2713 ${doneLabel}` : idleLabel}
        </button>
    )
}

function GuideCommand({ title, summary, code }) {
    return (
        <section className="guide-command glass-card">
            <div className="guide-command-header">
                <div>
                    <span className="guide-kicker">Terminal</span>
                    <h2>{title}</h2>
                    <p>{summary}</p>
                </div>
                <CopyButton text={code} idleLabel="复制命令" />
            </div>
            <pre className="guide-code-block">{code}</pre>
        </section>
    )
}

function GuideCommandGroup({ items }) {
    return (
        <div className="guide-command-group">
            {items.map((item) => (
                <GuideCommand
                    key={item.title}
                    title={item.title}
                    summary={item.summary}
                    code={item.code}
                />
            ))}
        </div>
    )
}

export default function GuideDetail() {
    const { guideId } = useParams()
    const { effectiveApiKey, hasDeveloperKey, hasLocalDeveloperKey, latestDeveloperKey } = useAuth()
    const { models, textModels, defaultTextModel } = usePublicModels()

    const key = effectiveApiKey || ''
    const codingModel = textModels.find((model) => model.id === 'gpt-5.4')
        || textModels.find((model) => model.id === 'gpt-5.3-codex')
        || textModels.find((model) => model.id === 'gpt-5.5')
        || defaultTextModel
        || models[0]
    const defaultClaudeModel = 'claude-sonnet-4-6'
    const maskedKey = effectiveApiKey
        ? `${effectiveApiKey.slice(0, 8)}\u2022\u2022\u2022\u2022${effectiveApiKey.slice(-4)}`
        : latestDeveloperKey?.masked_key || '还没有本地可用开发者 Key'

    const guides = useMemo(() => {
        const apiQuickstartCommand = `curl ${OPENAI_BASE_URL}/chat/completions \\
  -H "Authorization: Bearer ${key}" \\
  -H "Content-Type: application/json" \\
  -d '{
    "model": "${codingModel?.id || 'gpt-5.4'}",
    "messages": [{"role": "user", "content": "Reply with only: OK"}],
    "stream": false
  }'`

        const codexCommand = `mkdir -p ~/.codex && cat > ~/.codex/config.toml <<'EOF'
model_provider = "coincoin"
model = "${codingModel?.id || 'gpt-5.4'}"
disable_response_storage = true
model_reasoning_effort = "high"
web_search = "live"
personality = "pragmatic"

[model_providers.coincoin]
name = "CoinCoin"
base_url = "${OPENAI_BASE_URL}"
experimental_bearer_token = "${key}"
wire_api = "responses"
EOF

codex`

        const codexWindowsCommand = `New-Item -ItemType Directory -Force "$HOME\\.codex" | Out-Null
@"
model_provider = "coincoin"
model = "${codingModel?.id || 'gpt-5.4'}"
disable_response_storage = true
model_reasoning_effort = "high"
web_search = "live"
personality = "pragmatic"

[model_providers.coincoin]
name = "CoinCoin"
base_url = "${OPENAI_BASE_URL}"
experimental_bearer_token = "${key}"
wire_api = "responses"
"@ | Set-Content "$HOME\\.codex\\config.toml" -Encoding UTF8

codex`

        const claudeCommand = `ANTHROPIC_BASE_URL="${SITE_ROOT}" \\
ANTHROPIC_AUTH_TOKEN="${key}" \\
ANTHROPIC_MODEL="${defaultClaudeModel}" \\
ANTHROPIC_DEFAULT_SONNET_MODEL="${defaultClaudeModel}" \\
ANTHROPIC_DEFAULT_OPUS_MODEL="claude-opus-4-7" \\
ANTHROPIC_DEFAULT_HAIKU_MODEL="claude-haiku-4-5" \\
claude --model "${defaultClaudeModel}"`

        return {
            'api-quickstart': {
                title: '默认 API 教程',
                description: '先确认开发者 Key 和余额，再打通第一条 OpenAI 兼容请求。',
                commandTitle: '直接发第一条请求',
                commandSummary: '把下面整段粘贴进终端即可。能返回 `OK` 就说明地址、Key 和 model 都通了。',
                command: apiQuickstartCommand,
            },
            codex: {
                title: 'Codex 配置',
                description: '直接把 token 写进 `~/.codex/config.toml`，不再要求额外改 `~/.zshrc`。',
                commandGroup: [
                    {
                        title: 'macOS / Linux 一键配置',
                        summary: '覆盖 `~/.codex/config.toml`，写入 CoinCoin provider，然后立刻启动 `codex`。',
                        code: codexCommand,
                    },
                    {
                        title: 'Windows PowerShell 一键配置',
                        summary: '覆盖 `$HOME\\.codex\\config.toml`，直接填入 key，然后立刻启动 `codex`。',
                        code: codexWindowsCommand,
                    },
                ],
            },
            'claude-code': {
                title: 'Claude Code 配置',
                description: 'Claude Code 走 Anthropic 兼容入口，地址填根域名，不要手动加 `/v1`。',
                commandTitle: '直接用环境变量启动 Claude Code',
                commandSummary: '如果之前登录过官方 Claude，先在 Claude Code 里执行一次 `/logout`，再粘贴下面这段命令。',
                command: claudeCommand,
            },
        }
    }, [codingModel?.id, key])

    const guide = guideId ? guides[guideId] : null
    if (!guide) {
        return <Navigate to="/guides/api-quickstart" replace />
    }

    return (
        <AppShell title={guide.title} description={guide.description}>
            <div className="guide-page">
                <section className="guide-hero glass-card">
                    <div>
                        <span className="guide-kicker">Guide</span>
                        <h1>{guide.title}</h1>
                        <p>{guide.description}</p>
                    </div>
                    <div className="guide-hero-meta">
                        <span className="meta-pill">开发者 Key：{hasDeveloperKey ? maskedKey : '未生成'}</span>
                        <span className="meta-pill">{hasLocalDeveloperKey ? '当前浏览器可直接复制真 Key' : '当前浏览器没有保存真 Key'}</span>
                    </div>
                </section>

                {!effectiveApiKey && (
                    <section className="guide-alert glass-card">
                        <strong>当前浏览器还没有可直接复制的开发者 Key</strong>
                        <p>先去 <Link to="/api-keys">API 密钥</Link> 页面生成或重新复制一把开发者 Key。拿到明文后，这里才会显示可直接复制的一键命令。</p>
                    </section>
                )}

                {effectiveApiKey && (guide.commandGroup ? (
                    <GuideCommandGroup items={guide.commandGroup} />
                ) : (
                    <GuideCommand
                        title={guide.commandTitle}
                        summary={guide.commandSummary}
                        code={guide.command}
                    />
                ))}
            </div>
        </AppShell>
    )
}
