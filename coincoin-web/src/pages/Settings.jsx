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

    return (
        <div className="page-wrapper">
            <div className="container">
                <div className="page-header">
                    <h1 className="page-title">设置</h1>
                    <p className="page-desc">管理开发者接入信息、模型选择和客户端配置</p>
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

                    <div className="glass-card settings-section animate-fade-in-up" style={{ animationDelay: '100ms' }}>
                        <h3>&#127760; 接入信息</h3>
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
                                <code>chat/completions, responses, models, images/generations</code>
                            </div>
                            <div className="info-item">
                                <span className="info-label">认证提示</span>
                                <code>{hasDeveloperKey ? '使用 Bearer 开发者 Key' : '先生成开发者 Key，再接入客户端'}</code>
                            </div>
                        </div>
                    </div>

                    <div className="glass-card settings-section animate-fade-in-up" style={{ animationDelay: '150ms' }}>
                        <h3>&#129302; 模型配置</h3>
                        <p className="settings-hint" style={{ marginBottom: 'var(--space-lg)' }}>
                            现在可以直接通过修改 <code>model</code> 选择公开模型；老客户端不传 <code>model</code> 仍会保持默认 GPT 兼容行为。
                        </p>
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

                    <div className="glass-card settings-section animate-fade-in-up" style={{ animationDelay: '200ms' }}>
                        <h3>&#9881; 接入配置生成器</h3>
                        <p className="settings-hint" style={{ marginBottom: 'var(--space-lg)' }}>
                            选择模型后，一键复制配置代码。Base URL 不变，只需要改 <code>model</code>。
                            {!hasDeveloperKey && ' 当前未检测到开发者 Key，示例里会保留占位符。'}
                        </p>
                        <div className="snippets-list">
                            {snippets.map((snippet) => (
                                <ConfigSnippet key={snippet.title} title={snippet.title} code={snippet.code} />
                            ))}
                        </div>
                    </div>

                    <div className="glass-card settings-section animate-fade-in-up" style={{ animationDelay: '300ms' }}>
                        <h3>&#128231; 联系支持</h3>
                        <p className="settings-text">如需帮助，请联系管理员：</p>
                        <ul className="settings-list">
                            <li>API Key 丢失：管理员可为你生成新 Key</li>
                            <li>模型接入问题：先确认客户端有没有把 <code>model</code> 真的发出来</li>
                            <li>Gemini 生图调用：使用 <code>/v1/images/generations</code> 和图片 alias</li>
                        </ul>
                    </div>
                </div>
            </div>
        </div>
    )
}
