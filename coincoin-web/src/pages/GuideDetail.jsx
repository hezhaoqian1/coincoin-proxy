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

function GuideChecklist({ title, items }) {
    return (
        <section className="guide-section glass-card">
            <h3>{title}</h3>
            <ul className="guide-list">
                {items.map((item) => (
                    <li key={item}>{item}</li>
                ))}
            </ul>
        </section>
    )
}

export default function GuideDetail() {
    const { guideId } = useParams()
    const { effectiveApiKey, hasDeveloperKey, hasLocalDeveloperKey, latestDeveloperKey } = useAuth()
    const { models, textModels, defaultTextModel } = usePublicModels()

    const key = effectiveApiKey || 'YOUR_DEVELOPER_API_KEY'
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
                callouts: [
                    '统一 OpenAI 兼容入口是根域名后加 /v1。',
                    '程序调用一律使用开发者 Key，不要把网页登录态拿去发 API。',
                    '最小成功标准是先跑通一条文本请求，再去切模型和客户端。',
                ],
                checklistTitle: '排查顺序',
                checklist: [
                    `Base URL 是否是 ${OPENAI_BASE_URL}`,
                    'Authorization 是否是 sk_cc_ 开头的开发者 Key',
                    '余额是否大于 0',
                    `model 是否在 /v1/models 公开目录里可见，例如 ${codingModel?.id || 'gpt-5.4'}`,
                ],
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
                callouts: [
                    '这里走的是 Codex 官方支持的 `experimental_bearer_token` 路径，体验上最省一步。',
                    '不需要把 key 填进 `env_key`；`env_key` 只能写环境变量名，不接受原始 token。',
                    '如果更看重 secret 不落盘，可以改回 `env_key` + 环境变量方案，但主路径不再要求改 zshrc 或 PowerShell 用户变量。',
                ],
                checklistTitle: '你会得到什么',
                checklist: [
                    `provider 指向 ${OPENAI_BASE_URL}`,
                    `默认模型使用 ${codingModel?.id || 'gpt-5.4'}`,
                    'web search、reasoning 和 pragmatic personality 都会一起配好',
                    '后续直接运行 codex 即可，不需要再额外 export',
                ],
            },
            'claude-code': {
                title: 'Claude Code 配置',
                description: 'Claude Code 走 Anthropic 兼容入口，地址填根域名，不要手动加 `/v1`。',
                commandTitle: '直接用环境变量启动 Claude Code',
                commandSummary: '如果之前登录过官方 Claude，先在 Claude Code 里执行一次 `/logout`，再粘贴下面这段命令。',
                command: claudeCommand,
                callouts: [
                    'Claude Code 的 Base URL 必须是站点根地址，例如 https://coincoin.ai，而不是 /v1。',
                    '官方 Claude Code 会自己请求 /v1/messages 和 /v1/models。',
                    '如果新地址没生效，优先排查是不是旧登录态还在接管请求。',
                ],
                checklistTitle: '常见坑位',
                checklist: [
                    `不要把 ${OPENAI_BASE_URL} 填给 Claude Code`,
                    `ANTHROPIC_BASE_URL 应该是 ${SITE_ROOT || 'https://your-domain.example'}`,
                    '如果模型名不对，优先使用 claude-sonnet-4-6、claude-opus-4-7 这类公开 alias',
                    '修改完后重新启动 claude 进程，不要沿用旧会话',
                ],
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
                        <strong>这页里的命令还没完全可直接用</strong>
                        <p>当前浏览器没有保存开发者 Key，所以命令里会出现 <code>YOUR_DEVELOPER_API_KEY</code>。先去 <Link to="/api-keys">API 密钥</Link> 页面生成或复制一把开发者 Key。</p>
                    </section>
                )}

                {guide.commandGroup ? (
                    <GuideCommandGroup items={guide.commandGroup} />
                ) : (
                    <GuideCommand
                        title={guide.commandTitle}
                        summary={guide.commandSummary}
                        code={guide.command}
                    />
                )}

                <div className="guide-grid">
                    <GuideChecklist title={guide.checklistTitle} items={guide.checklist} />
                    <GuideChecklist title="为什么这么配" items={guide.callouts} />
                </div>
            </div>
        </AppShell>
    )
}
