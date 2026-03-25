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
    const { apiKey } = useAuth()
    const { models, textModels, defaultTextModel, defaultImageModel } = usePublicModels()
    const [copied, setCopied] = useState(false)
    const [selectedModel, setSelectedModel] = useState(defaultTextModel?.id || 'gpt-5.2-codex')

    useEffect(() => {
        if (!textModels.find(model => model.id === selectedModel) && defaultTextModel?.id) {
            setSelectedModel(defaultTextModel.id)
        }
    }, [defaultTextModel, selectedModel, textModels])

    const maskedKey = apiKey
        ? `${apiKey.substring(0, 8)}\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022${apiKey.substring(apiKey.length - 4)}`
        : ''
    const key = apiKey || 'YOUR_API_KEY'
    const baseUrl = BASE_URL_DISPLAY
    const imageModel = defaultImageModel?.id || 'gemini-image'
    const selectedModelInfo = textModels.find(model => model.id === selectedModel) || defaultTextModel

    const handleCopy = () => {
        navigator.clipboard.writeText(apiKey)
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
                    <p className="page-desc">管理账户信息、可用模型和接入配置</p>
                </div>

                <div className="settings-grid">
                    <div className="glass-card settings-section animate-fade-in-up">
                        <h3>&#128273; API Key</h3>
                        <div className="key-info-row">
                            <code className="masked-key">{maskedKey}</code>
                            <button onClick={handleCopy} className="btn btn-secondary btn-sm">
                                {copied ? '\u2713 已复制' : '复制完整 Key'}
                            </button>
                        </div>
                        <p className="settings-hint">请妥善保管你的 API Key，不要泄露给他人。</p>
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
                            选择模型后，一键复制配置代码。Base URL 和 API Key 不变，只需要改 <code>model</code>。
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
