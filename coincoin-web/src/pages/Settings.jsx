import { useEffect, useState } from 'react'
import { describePublicModel } from '../api/client'
import { useAuth } from '../hooks/useAuth'
import { usePublicModels } from '../hooks/usePublicModels'
import './Settings.css'

const BASE_URL_DISPLAY = typeof window !== 'undefined' ? `${window.location.origin}/v1` : '/v1'

function ConfigSnippet({ title, code }) {
    const [copied, setCopied] = useState(false)

    const handleCopy = () => {
        navigator.clipboard.writeText(code)
        setCopied(true)
        setTimeout(() => setCopied(false), 2000)
    }

    return (
        <div className="config-snippet">
            <div className="snippet-header">
                <span className="snippet-title">{title}</span>
                <button className="btn btn-ghost btn-sm" onClick={handleCopy}>
                    {copied ? '\u2713 已复制' : '复制'}
                </button>
            </div>
            <pre className="snippet-code">{code}</pre>
        </div>
    )
}

export default function Settings() {
    const { authMode, effectiveApiKey, generatedApiKey, hasDeveloperKey, isConsoleSession, username } = useAuth()
    const { models, textModels, defaultTextModel, defaultImageModel } = usePublicModels()
    const [copied, setCopied] = useState(false)
    const [selectedModel, setSelectedModel] = useState(defaultTextModel?.id || 'gpt-5.2-codex')
    const [activeSnippet, setActiveSnippet] = useState('Python (openai SDK)')

    useEffect(() => {
        if (!textModels.find(model => model.id === selectedModel) && defaultTextModel?.id) {
            setSelectedModel(defaultTextModel.id)
        }
    }, [defaultTextModel, selectedModel, textModels])

    const maskedKey = effectiveApiKey
        ? `${effectiveApiKey.substring(0, 8)}\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022${effectiveApiKey.substring(effectiveApiKey.length - 4)}`
        : ''
    const key = hasDeveloperKey ? effectiveApiKey : 'YOUR_DEVELOPER_API_KEY'
    const baseUrl = BASE_URL_DISPLAY
    const imageModel = defaultImageModel?.id || 'gemini-image'
    const selectedModelInfo = textModels.find(model => model.id === selectedModel) || defaultTextModel

    const handleCopy = () => {
        if (!effectiveApiKey) return
        navigator.clipboard.writeText(effectiveApiKey)
        setCopied(true)
        setTimeout(() => setCopied(false), 2000)
    }

    const snippets = [
        {
            title: 'Python (openai SDK)',
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
            code: `# ~/.codex/config.toml
model = "${selectedModel}"
model_provider = "coincoin"
model_reasoning_effort = "high"

[model_providers.coincoin]
name = "CoinCoin"
base_url = "${baseUrl}"
env_key = "COINCOIN_API_KEY"
wire_api = "responses"

# 然后设置环境变量：
# export COINCOIN_API_KEY="${key}"`
        },
        {
            title: 'OpenClaw',
            code: `{
  "models": {
    "providers": {
      "coincoin": {
        "baseUrl": "${baseUrl}",
        "apiKey": "${key}",
        "api": "openai-completions",
        "models": [{"id": "${selectedModel}", "contextWindow": 131072}]
      }
    },
    "defaults": {
      "provider": "coincoin",
      "model": "${selectedModel}"
    }
  }
}`
        },
        {
            title: 'Image Generation',
            code: `curl ${baseUrl}/images/generations \\
  -H "Authorization: Bearer ${key}" \\
  -H "Content-Type: application/json" \\
  -d '{
    "model": "${imageModel}",
    "prompt": "A cinematic poster about AI routing",
    "size": "1024x1024"
  }'`
        }
    ]
    const activeSnippetContent = snippets.find((snippet) => snippet.title === activeSnippet) || snippets[0]
    const readinessChecks = [
        hasDeveloperKey ? '已拿到开发者 API Key，可以直接接 SDK / CLI。' : '当前只有控制台会话，先去仪表盘生成开发者 API Key。',
        '客户端 Base URL 固定为同一个 /v1 入口，不用为不同模型换域名。',
        '切换模型时优先改 model，不要手改内部上游地址。',
        '如果请求 403，先排查是不是把 session key 当成 API Key 在用。',
    ]
    const troubleshootingItems = [
        'Codex CLI / Continue 接不上时，先确认你填的是开发者 API Key，而不是控制台 session。',
        '模型没切换成功时，先抓请求体，看客户端是否真的把 model 字段发出来了。',
        'Gemini 生图请走 /v1/images/generations 或 /v1/images/edits，不要直连内部 gateway。',
        '余额、充值、请求日志和开发者 Key 管理都以控制台为准，不要分散到外部脚本里维护。',
    ]

    return (
        <div className="page-wrapper">
            <div className="container">
                <div className="page-header">
                    <h1 className="page-title">接入配置</h1>
                    <p className="page-desc">把控制台会话、开发者 API Key、模型选择和客户端配置放到同一张工作台里</p>
                </div>

                <div className="settings-grid">
                    {authMode === 'session_only' && (
                        <div className="glass-card settings-section settings-alert settings-alert-warning animate-fade-in-up">
                            <h3>当前是控制台 session，不是开发者 API Key</h3>
                            <p className="settings-text">
                                你已经用用户名密码进入控制台，但当前保存的是站内 session key。它只能访问 Dashboard、充值和设置；
                                要给 Codex CLI、Continue、Aider 或 cURL 调接口，请先回到仪表盘生成开发者 API Key。
                            </p>
                            <div className="settings-inline-meta">
                                <span className="meta-pill">账户：{username || '未命名用户'}</span>
                                <span className="meta-pill">开发者 Key：未生成</span>
                            </div>
                        </div>
                    )}

                    {authMode === 'session_with_api' && (
                        <div className="glass-card settings-section settings-alert settings-alert-success animate-fade-in-up">
                            <h3>开发者接入已就绪</h3>
                            <p className="settings-text">
                                当前控制台账号已绑定一个可用的开发者 API Key。下面的代码片段会优先使用这个 Key，
                                适合直接复制到客户端配置里。
                            </p>
                            <div className="settings-inline-meta">
                                <span className="meta-pill">账户：{username || '未命名用户'}</span>
                                <span className="meta-pill">开发者 Key：已生成</span>
                            </div>
                        </div>
                    )}

                    {authMode === 'api' && (
                        <div className="glass-card settings-section settings-alert animate-fade-in-up">
                            <h3>当前是 API Key 直登模式</h3>
                            <p className="settings-text">
                                你现在是通过开发者 API Key 直接登录。下面的示例可以直接使用；
                                但如果需要站内管理、重新生成密钥或查看邀请返佣，建议改用用户名密码登录控制台。
                            </p>
                            <div className="settings-inline-meta">
                                <span className="meta-pill">登录方式：开发者 Key</span>
                                <span className="meta-pill">{isConsoleSession ? '控制台会话' : '可直接调用 API'}</span>
                            </div>
                        </div>
                    )}

                    <div className="glass-card settings-section animate-fade-in-up">
                        <h3>&#128273; 开发者 API Key</h3>
                        <div className="key-info-row">
                            <code className="masked-key">{maskedKey || '尚未生成开发者 API Key'}</code>
                            <button onClick={handleCopy} className="btn btn-secondary btn-sm" disabled={!effectiveApiKey}>
                                {copied ? '\u2713 已复制' : '复制开发者 Key'}
                            </button>
                        </div>
                        <p className="settings-hint">
                            {generatedApiKey
                                ? '这个 Key 来自你在控制台生成的开发者密钥，适合放进客户端配置。'
                                : hasDeveloperKey
                                    ? '当前正在使用开发者 API Key 直登，可以直接拿来调用接口。'
                                    : '这里不会把控制台 session key 伪装成开发者密钥。请先去仪表盘生成正式的开发者 API Key。'}
                        </p>
                    </div>

                    <div className="settings-two-column">
                        <div className="glass-card settings-section animate-fade-in-up" style={{ animationDelay: '100ms' }}>
                            <div className="settings-section-head">
                                <div>
                                    <h3>&#127760; 接入信息</h3>
                                    <p className="settings-subtitle">真正要给 SDK、CLI 和服务端使用的，是这里这套开发者接入参数。</p>
                                </div>
                                <span className="meta-pill">统一入口</span>
                            </div>
                            <div className="info-grid">
                                <div className="info-item">
                                    <span className="info-label">Base URL</span>
                                    <code>{baseUrl}</code>
                                </div>
                                <div className="info-item">
                                    <span className="info-label">默认文本模型</span>
                                    <code>{defaultTextModel?.id || 'gpt-5.2-codex'}</code>
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
                                    <code>{hasDeveloperKey ? 'Bearer 开发者 Key' : '先生成开发者 Key'}</code>
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
                                    <p className="settings-subtitle">现在切模型的动作应该尽量轻，只改 <code>model</code>，不再折腾多套域名和脚本。</p>
                                </div>
                                <span className="meta-pill">模型工作区</span>
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

                    <div className="glass-card settings-section animate-fade-in-up" style={{ animationDelay: '200ms' }}>
                        <div className="settings-section-head">
                            <div>
                                <h3>&#9881; 配置片段</h3>
                                <p className="settings-subtitle">先选客户端，再复制对应的最短可用配置。页面只展示当前这一个片段，减少视觉噪音。</p>
                            </div>
                            <span className="meta-pill">可直接复制</span>
                        </div>
                        <p className="settings-hint" style={{ marginBottom: 'var(--space-lg)' }}>
                            选择模型后，一键复制配置代码。Base URL 不变，只需要改 <code>model</code>。
                            {!hasDeveloperKey && ' 当前未检测到开发者 Key，示例里会保留占位符。'}
                        </p>
                        <div className="snippet-tabs">
                            {snippets.map((snippet) => (
                                <button
                                    key={snippet.title}
                                    className={`snippet-tab ${activeSnippet === snippet.title ? 'active' : ''}`}
                                    onClick={() => setActiveSnippet(snippet.title)}
                                >
                                    {snippet.title}
                                </button>
                            ))}
                        </div>
                        <div className="snippets-list">
                            <ConfigSnippet title={activeSnippetContent.title} code={activeSnippetContent.code} />
                        </div>
                    </div>

                    <div className="settings-two-column">
                        <div className="glass-card settings-section animate-fade-in-up" style={{ animationDelay: '260ms' }}>
                            <div className="settings-section-head">
                                <div>
                                    <h3>&#128269; 接入前检查</h3>
                                    <p className="settings-subtitle">排查问题时，先查这些最容易出错的地方，别一上来就怀疑上游模型。</p>
                                </div>
                            </div>
                            <ul className="settings-list">
                                {troubleshootingItems.map((item) => (
                                    <li key={item}>{item}</li>
                                ))}
                            </ul>
                        </div>

                        <div className="glass-card settings-section animate-fade-in-up" style={{ animationDelay: '300ms' }}>
                            <div className="settings-section-head">
                                <div>
                                    <h3>&#128241; 下一步建议</h3>
                                    <p className="settings-subtitle">当前这页解决的是“怎么接”。如果你要继续运营或排查，下一步一般会回到这些页面。</p>
                                </div>
                            </div>
                            <ul className="settings-list">
                                <li>去仪表盘看余额、生成或轮换开发者 Key。</li>
                                <li>去请求日志确认每次调用的模型、耗时和扣费记录。</li>
                                <li>去文档页查支持矩阵、错误码和多图图生图的接口规则。</li>
                                <li>如果是团队协作场景，统一让同事从这页复制配置，减少野生接法。</li>
                            </ul>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    )
}
