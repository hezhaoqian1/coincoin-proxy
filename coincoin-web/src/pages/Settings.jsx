import { useState } from 'react'
import { getApiKey } from '../api/client'
import { useAuth } from '../hooks/useAuth'
import './Settings.css'

const BASE_URL_DISPLAY = typeof window !== 'undefined' ? window.location.origin + '/v1' : '/v1'

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
    const [copied, setCopied] = useState(false)

    const maskedKey = apiKey ? apiKey.substring(0, 8) + '\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022' + apiKey.substring(apiKey.length - 4) : ''
    const key = apiKey || 'YOUR_API_KEY'
    const baseUrl = BASE_URL_DISPLAY

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
    model="gpt-5.2-codex",
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
  model: 'gpt-5.2-codex',
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
    "model": "gpt-5.2-codex",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'`
        },
        {
            title: 'Codex CLI (config.toml)',
            code: `# ~/.codex/config.toml
[providers.coincoin]
name = "CoinCoin"
base_url = "${baseUrl}"
env_key = "COINCOIN_API_KEY"
wire_api = "responses"

# 然后设置环境变量：
# export COINCOIN_API_KEY="${key}"`
        },
        {
            title: 'OpenClaw',
            code: `# 运行配置命令：
openclaw configure

# 填入以下信息：
# API Base URL: ${baseUrl}
# API Key: ${key}
# Model: gpt-5.2-codex`
        }
    ]

    return (
        <div className="page-wrapper">
            <div className="container">
                <div className="page-header">
                    <h1 className="page-title">设置</h1>
                    <p className="page-desc">管理账户信息和接入配置</p>
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
                                <span className="info-label">模型</span>
                                <code>gpt-5.2-codex</code>
                            </div>
                            <div className="info-item">
                                <span className="info-label">兼容格式</span>
                                <code>OpenAI API</code>
                            </div>
                            <div className="info-item">
                                <span className="info-label">支持端点</span>
                                <code>chat/completions, responses, models</code>
                            </div>
                        </div>
                    </div>

                    <div className="glass-card settings-section animate-fade-in-up" style={{ animationDelay: '200ms' }}>
                        <h3>&#9881; 接入配置生成器</h3>
                        <p className="settings-hint" style={{ marginBottom: 'var(--space-lg)' }}>
                            选择你的客户端，一键复制配置代码（已自动填入你的 API Key）
                        </p>
                        <div className="snippets-list">
                            {snippets.map((s, i) => <ConfigSnippet key={i} title={s.title} code={s.code} />)}
                        </div>
                    </div>

                    <div className="glass-card settings-section animate-fade-in-up" style={{ animationDelay: '300ms' }}>
                        <h3>&#128231; 联系支持</h3>
                        <p className="settings-text">如需帮助，请联系管理员：</p>
                        <ul className="settings-list">
                            <li>API Key 丢失：管理员可为你生成新 Key</li>
                            <li>账户异常：联系管理员排查</li>
                            <li>功能建议：欢迎反馈</li>
                        </ul>
                    </div>
                </div>
            </div>
        </div>
    )
}
