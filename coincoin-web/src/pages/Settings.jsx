import { useEffect, useMemo, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { describePublicModel } from '../api/client'
import { useAuth } from '../hooks/useAuth'
import { usePublicModels } from '../hooks/usePublicModels'
import AppShell from '../components/AppShell'
import './Settings.css'

const BASE_URL_DISPLAY = typeof window !== 'undefined' ? `${window.location.origin}/v1` : '/v1'
const ANTHROPIC_BASE_URL_DISPLAY = typeof window !== 'undefined' ? window.location.origin : ''

function ConfigSnippet({ title, summary, code }) {
    const [copied, setCopied] = useState(false)

    const handleCopy = () => {
        navigator.clipboard.writeText(code)
        setCopied(true)
        setTimeout(() => setCopied(false), 2000)
    }

    return (
        <div className="config-snippet">
            <div className="snippet-header">
                <div className="snippet-heading">
                    <span className="snippet-title">{title}</span>
                    {summary ? <span className="snippet-summary">{summary}</span> : null}
                </div>
                <button className="btn btn-ghost btn-sm" onClick={handleCopy}>
                    {copied ? '\u2713 已复制' : '复制'}
                </button>
            </div>
            <pre className="snippet-code">{code}</pre>
        </div>
    )
}

export default function Settings() {
    const [searchParams, setSearchParams] = useSearchParams()
    const { authMode, effectiveApiKey, generatedApiKey, hasDeveloperKey, hasLocalDeveloperKey, isConsoleSession, latestDeveloperKey, username } = useAuth()
    const { models, textModels, defaultTextModel, defaultImageModel } = usePublicModels()
    const snippetsRef = useRef(null)
    const [copied, setCopied] = useState(false)
    const [selectedModel, setSelectedModel] = useState('')
    const [activeSnippet, setActiveSnippet] = useState(searchParams.get('snippet') || 'Python (openai SDK)')

    useEffect(() => {
        if ((!selectedModel || !textModels.find((model) => model.id === selectedModel)) && defaultTextModel?.id) {
            setSelectedModel(defaultTextModel.id)
        }
    }, [defaultTextModel, selectedModel, textModels])

    useEffect(() => {
        const requestedSnippet = searchParams.get('snippet')
        if (requestedSnippet) {
            setActiveSnippet(requestedSnippet)
        }
    }, [searchParams])

    const maskedKey = effectiveApiKey
        ? `${effectiveApiKey.substring(0, 8)}\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022${effectiveApiKey.substring(effectiveApiKey.length - 4)}`
        : latestDeveloperKey?.masked_key || ''
    const canUseDeveloperKeyNow = !!effectiveApiKey
    const key = canUseDeveloperKeyNow ? effectiveApiKey : 'YOUR_DEVELOPER_API_KEY'
    const baseUrl = BASE_URL_DISPLAY
    const anthropicBaseUrl = ANTHROPIC_BASE_URL_DISPLAY
    const imageModel = defaultImageModel?.id || 'gemini-image'
    const selectedModelInfo = textModels.find((model) => model.id === selectedModel) || defaultTextModel
    const defaultCodingModel = textModels.find((model) => model.id === 'gpt-5.3-codex')
        || textModels.find((model) => model.id === 'gpt-5.5')
        || defaultTextModel
    const codexModel = defaultCodingModel?.id || selectedModel || 'gpt-5.3-codex'

    const handleCopy = () => {
        if (!effectiveApiKey) return
        navigator.clipboard.writeText(effectiveApiKey)
        setCopied(true)
        setTimeout(() => setCopied(false), 2000)
    }

    const snippets = useMemo(() => [
        {
            title: 'Python (openai SDK)',
            summary: '服务端脚本和后端接口最常用。',
            code: `from openai import OpenAI

client = OpenAI(
    api_key="${key}",
    base_url="${baseUrl}"
)

response = client.chat.completions.create(
    model="${selectedModel}",
    messages=[{"role": "user", "content": "Hello!"}]
)
print(response.choices[0].message.content)`
        },
        {
            title: 'JavaScript (openai SDK)',
            summary: 'Node 服务和本地工具脚本。',
            code: `import OpenAI from 'openai';

const client = new OpenAI({
  apiKey: '${key}',
  baseURL: '${baseUrl}'
});

const response = await client.chat.completions.create({
  model: '${selectedModel}',
  messages: [{ role: 'user', content: 'Hello!' }]
});
console.log(response.choices[0].message.content);`
        },
        {
            title: 'cURL',
            summary: '先打通第一条请求时最直接。',
            code: `curl ${baseUrl}/chat/completions \\
  -H "Authorization: Bearer ${key}" \\
  -H "Content-Type: application/json" \\
  -d '{
    "model": "${selectedModel}",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'`
        },
        {
            title: 'Codex CLI (config.toml)',
            summary: 'Codex CLI 走 OpenAI 兼容入口。',
            code: `# ~/.codex/config.toml
model = "${codexModel}"
model_provider = "clawfather"
disable_response_storage = true
model_reasoning_effort = "high"
web_search = "live"
personality = "pragmatic"

[model_providers.clawfather]
name = "ClawFather"
base_url = "${baseUrl}"
env_key = "CLAWFATHER_OPENAI_API_KEY"
wire_api = "responses"

# 启动前设置环境变量：
# export CLAWFATHER_OPENAI_API_KEY="${key}"`
        },
        {
            title: 'Claude Code',
            summary: 'Claude Code 走 Anthropic 兼容入口。',
            code: `# macOS / Linux
export ANTHROPIC_BASE_URL="${anthropicBaseUrl}"
export ANTHROPIC_AUTH_TOKEN="${key}"
export ANTHROPIC_MODEL="claude-opus-4-7"
export ANTHROPIC_DEFAULT_OPUS_MODEL="claude-opus-4-7"
export ANTHROPIC_DEFAULT_SONNET_MODEL="claude-sonnet-4-6"
export ANTHROPIC_DEFAULT_HAIKU_MODEL="claude-haiku-4-5"

# 旧登录态会干扰新地址，先在 Claude Code 里执行 /logout
claude --model claude-opus-4-7`
        },
        {
            title: 'OpenClaw',
            summary: '已有 provider 配置时直接替换即可。',
            code: `{
  "models": {
    "providers": {
      "clawfather": {
        "baseUrl": "${baseUrl}",
        "apiKey": "${key}",
        "api": "openai-completions",
        "models": [{"id": "${codexModel}", "contextWindow": 131072}]
      }
    },
    "defaults": {
      "provider": "clawfather",
      "model": "${codexModel}"
    }
  }
}`
        },
        {
            title: 'Image Generation',
            summary: '文生图和图编辑统一看这段。',
            code: `curl ${baseUrl}/images/generations \\
  -H "Authorization: Bearer ${key}" \\
  -H "Content-Type: application/json" \\
  -d '{
    "model": "${imageModel}",
    "prompt": "A cinematic poster about AI routing",
    "size": "1024x1024"
  }'`
        }
    ], [baseUrl, anthropicBaseUrl, key, selectedModel, codexModel, imageModel])

    const activeSnippetContent = snippets.find((snippet) => snippet.title === activeSnippet) || snippets[0]
    const activePanel = searchParams.get('panel') || 'keys'
    const readinessChecks = [
        hasDeveloperKey
            ? (canUseDeveloperKeyNow ? '当前浏览器已有可用开发者 Key。' : '当前账号已有开发者 Key，但本浏览器没有保存明文。')
            : '当前只有控制台会话，需要先生成开发者 Key。',
        'OpenAI 兼容客户端统一走同一个 /v1 入口。',
        '平时主要改 model，不用改地址。',
        '403 通常是把 session key 当成了 API Key。',
    ]
    const troubleshootingItems = [
        'Codex CLI / Continue 接不上时，先看是不是用了开发者 Key。',
        'Claude Code 的地址填根域名，不要手动加 /v1。',
        '模型没切换成功时，先检查请求体里的 model。',
        'Gemini 生图走 /v1/images/generations 或 /v1/images/edits。',
        '余额、充值和日志都以控制台记录为准。',
    ]

    useEffect(() => {
        if (activePanel !== 'snippets') return
        if (!snippetsRef.current) return
        snippetsRef.current.scrollIntoView({ behavior: 'auto', block: 'start' })
    }, [activePanel, activeSnippet])

    const openSnippet = (title) => {
        setActiveSnippet(title)
        const next = new URLSearchParams(searchParams)
        next.set('panel', 'snippets')
        next.set('snippet', title)
        setSearchParams(next, { replace: true })
    }

    return (
        <AppShell
            title="接入配置"
            description="连接信息、模型和配置片段都在这一页。"
        >
            <div className="settings-grid">
                {authMode === 'session_only' && (
                    <div className="glass-card settings-section settings-alert settings-alert-warning animate-fade-in-up">
                        <h3>还差一把开发者 Key</h3>
                        <p className="settings-text">
                            你已经进了控制台，但还不能直接给 CLI 或 SDK 发请求。
                            先回概览页生成开发者 Key。
                        </p>
                        <div className="settings-inline-meta">
                            <span className="meta-pill">账户：{username || '未命名用户'}</span>
                            <span className="meta-pill">开发者 Key：未生成</span>
                        </div>
                    </div>
                )}

                {authMode === 'session_with_api' && (
                    <div className="glass-card settings-section settings-alert settings-alert-success animate-fade-in-up">
                        <h3>接入已经就绪</h3>
                        <p className="settings-text">
                            {hasLocalDeveloperKey
                                ? '当前控制台账号已有可用的开发者 Key。'
                                : '当前控制台账号已有开发者 Key，但这把 Key 的明文不会在新浏览器里恢复。'}
                        </p>
                        <div className="settings-inline-meta">
                            <span className="meta-pill">账户：{username || '未命名用户'}</span>
                            <span className="meta-pill">{hasLocalDeveloperKey ? '开发者 Key：可直接使用' : '开发者 Key：仅可见摘要'}</span>
                        </div>
                    </div>
                )}

                {authMode === 'api' && (
                    <div className="glass-card settings-section settings-alert animate-fade-in-up">
                        <h3>当前是开发者 Key 直登</h3>
                        <p className="settings-text">
                            当前就是开发者 Key 登录。充值和账户管理请回控制台账号处理。
                        </p>
                        <div className="settings-inline-meta">
                            <span className="meta-pill">登录方式：开发者 Key</span>
                            <span className="meta-pill">{isConsoleSession ? '控制台会话' : '可直接调用 API'}</span>
                        </div>
                    </div>
                )}

                <div className="glass-card settings-section animate-fade-in-up" id="api-keys">
                    <h3>&#128273; 开发者 Key</h3>
                    <div className="key-info-row">
                        <code className="masked-key">{maskedKey || '尚未生成开发者 Key'}</code>
                        <button onClick={handleCopy} className="btn btn-secondary btn-sm" disabled={!canUseDeveloperKeyNow}>
                            {copied ? '\u2713 已复制' : '复制开发者 Key'}
                        </button>
                    </div>
                    <p className="settings-hint">
                        {generatedApiKey
                            ? '这是控制台生成的开发者 Key。'
                            : authMode === 'api'
                                ? '当前就是开发者 Key 登录。'
                                : hasDeveloperKey
                                    ? '当前账户已有开发者 Key，但本浏览器没有保存明文。需要重新生成才会再次显示完整值。'
                                : '控制台登录态不能直接拿来调接口。'}
                    </p>
                </div>

                <div className="settings-connection-bar glass-card animate-fade-in-up" style={{ animationDelay: '80ms' }}>
                    <div className="connection-bar-main">
                        <div className="connection-bar-item">
                            <span className="info-label">Base URL</span>
                            <code>{baseUrl}</code>
                        </div>
                        <div className="connection-bar-item">
                            <span className="info-label">认证</span>
                            <code>{canUseDeveloperKeyNow ? 'Bearer 开发者 Key' : hasDeveloperKey ? '重新生成后再填 Bearer Key' : '先生成开发者 Key'}</code>
                        </div>
                        <div className="connection-bar-item">
                            <span className="info-label">默认文本模型</span>
                            <code>{defaultTextModel?.id || 'gpt-5.5'}</code>
                        </div>
                        <div className="connection-bar-item">
                            <span className="info-label">默认图片模型</span>
                            <code>{imageModel}</code>
                        </div>
                    </div>
                    <div className="connection-bar-side">
                        <span className="meta-pill">{authMode === 'api' ? 'API Key 直登' : isConsoleSession ? '控制台登录' : '未登录或 Demo'}</span>
                        <span className="meta-pill">支持: chat / responses / models / images</span>
                    </div>
                </div>

                {activePanel === 'keys' && (
                    <div className="glass-card settings-section animate-fade-in-up" style={{ animationDelay: '90ms' }}>
                        <div className="settings-section-head">
                            <div>
                                <h3>这页怎么用</h3>
                                <p className="settings-subtitle">先看密钥和地址，再选客户端片段。</p>
                            </div>
                            <span className="meta-pill">使用顺序</span>
                        </div>
                        <div className="settings-checklist">
                            <div className="settings-check-item"><span className="settings-check-dot"></span><span>程序调用只用开发者 Key，不用控制台登录态。</span></div>
                            <div className="settings-check-item"><span className="settings-check-dot"></span><span>OpenAI 兼容客户端走 <code>{baseUrl}</code>。</span></div>
                            <div className="settings-check-item"><span className="settings-check-dot"></span><span>Claude Code 用站点根地址，不带 <code>/v1</code>。</span></div>
                        </div>
                    </div>
                )}

                <div className="settings-two-column settings-top-layout">
                    <div className="glass-card settings-section animate-fade-in-up" style={{ animationDelay: '100ms' }}>
                        <div className="settings-section-head">
                            <div>
                                <h3>&#127760; 连接信息</h3>
                                <p className="settings-subtitle">先确认入口、认证方式和当前会话。</p>
                            </div>
                            <span className="meta-pill">连接概览</span>
                        </div>
                        <div className="info-grid">
                            <div className="info-item">
                                <span className="info-label">Base URL</span>
                                <code>{baseUrl}</code>
                            </div>
                            <div className="info-item">
                                <span className="info-label">默认文本模型</span>
                                <code>{defaultTextModel?.id || 'gpt-5.5'}</code>
                            </div>
                            <div className="info-item">
                                <span className="info-label">默认图片模型</span>
                                <code>{imageModel}</code>
                            </div>
                            <div className="info-item">
                                <span className="info-label">支持端点</span>
                                <code>chat/completions, responses, models, images/*</code>
                            </div>
                            <div className="info-item">
                                <span className="info-label">认证提示</span>
                                <code>{canUseDeveloperKeyNow ? 'Bearer 开发者 Key' : hasDeveloperKey ? '重新生成后再填 Bearer Key' : '先生成开发者 Key'}</code>
                            </div>
                            <div className="info-item">
                                <span className="info-label">当前会话</span>
                                <code>{authMode === 'api' ? 'API Key 直登' : isConsoleSession ? '控制台登录' : '未登录或 Demo'}</code>
                            </div>
                        </div>
                        <div className="settings-checklist">
                            {readinessChecks.map((item) => (
                                <div key={item} className="settings-check-item">
                                    <span className="settings-check-dot"></span>
                                    <span>{item}</span>
                                </div>
                            ))}
                        </div>
                    </div>

                    <div className="glass-card settings-section animate-fade-in-up" style={{ animationDelay: '150ms' }}>
                        <div className="settings-section-head">
                            <div>
                                <h3>&#129302; 模型配置</h3>
                                <p className="settings-subtitle">默认模型通常够用，需要时再切换。</p>
                            </div>
                            <span className="meta-pill">模型选择</span>
                        </div>
                        <div className="model-picker">
                            <label className="info-label">文本模型</label>
                            <select className="model-select" value={selectedModel} onChange={(e) => setSelectedModel(e.target.value)}>
                                {textModels.map((model) => (
                                    <option key={model.id} value={model.id}>{model.id}</option>
                                ))}
                            </select>
                            {selectedModelInfo && <p className="settings-hint">{describePublicModel(selectedModelInfo)}</p>}
                        </div>
                        <div className="model-chip-list">
                            {models.map((model) => (
                                <div key={model.id} className={`model-chip ${model.id === selectedModel ? 'active' : ''}`}>
                                    <strong>{model.id}</strong>
                                    <span>{model.coincoin_provider}</span>
                                </div>
                            ))}
                        </div>
                    </div>
                </div>

                <div ref={snippetsRef} className="glass-card settings-section animate-fade-in-up" style={{ animationDelay: '200ms' }} id="snippets">
                    <div className="settings-section-head">
                        <div>
                            <h3>&#9881; 配置片段</h3>
                            <p className="settings-subtitle">每次只看当前这一段，别在一大块配置里找半天。</p>
                        </div>
                        <span className="meta-pill">可直接复制</span>
                    </div>
                    <p className="settings-hint" style={{ marginBottom: 'var(--space-lg)' }}>
                        OpenAI 兼容客户端只需要 Base URL、开发者 Key 和 model。Claude Code 用根域名，不要带 <code>/v1</code>。
                        {!canUseDeveloperKeyNow && ' 当前没有可复制的开发者 Key 明文，示例会保留占位符。'}
                    </p>
                    <div className="snippet-tabs">
                        {snippets.map((snippet) => (
                            <button
                                key={snippet.title}
                                className={`snippet-tab ${activeSnippet === snippet.title ? 'active' : ''}`}
                                onClick={() => openSnippet(snippet.title)}
                            >
                                {snippet.title}
                            </button>
                        ))}
                    </div>
                    <div className="snippets-list">
                        <ConfigSnippet
                            title={activeSnippetContent.title}
                            summary={activeSnippetContent.summary}
                            code={activeSnippetContent.code}
                        />
                    </div>
                </div>

                <div className="glass-card settings-section settings-troubleshooting animate-fade-in-up" style={{ animationDelay: '260ms' }}>
                    <div className="settings-section-head">
                        <div>
                            <h3>&#128269; 常见问题</h3>
                            <p className="settings-subtitle">接不上时，先查这几项。</p>
                        </div>
                        <div className="settings-action-links">
                            <a href="/dashboard">回概览</a>
                            <a href="/usage">请求日志</a>
                            <a href="/docs">接入文档</a>
                        </div>
                    </div>
                    <div className="settings-troubleshooting-grid">
                        {troubleshootingItems.map((item) => (
                            <div key={item} className="troubleshooting-card">
                                <span className="troubleshooting-index">Check</span>
                                <p>{item}</p>
                            </div>
                        ))}
                    </div>
                </div>
            </div>
        </AppShell>
    )
}
