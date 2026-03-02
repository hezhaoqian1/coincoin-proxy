import { useState } from 'react'
import './Docs.css'

const SITE = typeof window !== 'undefined' ? window.location.origin : ''
const TABS = ['快速开始', '模型与价格', 'API 参考', '代码示例']

export default function Docs() {
    const [activeTab, setActiveTab] = useState(0)

    return (
        <div className="page-wrapper">
            <div className="container">
                <div className="page-header">
                    <h1 className="page-title">接入文档</h1>
                    <p className="page-desc">快速了解如何使用 CoinCoin API</p>
                </div>

                <div className="docs-layout">
                    <nav className="docs-nav glass-card">
                        {TABS.map((tab, i) => (
                            <button
                                key={i}
                                className={`docs-nav-item ${activeTab === i ? 'active' : ''}`}
                                onClick={() => setActiveTab(i)}
                            >
                                {['🚀', '💎', '📡', '💻'][i]} {tab}
                            </button>
                        ))}
                    </nav>

                    <div className="docs-content glass-card">
                        {activeTab === 0 && <QuickStart />}
                        {activeTab === 1 && <ModelsAndPricing />}
                        {activeTab === 2 && <ApiReference />}
                        {activeTab === 3 && <CodeExamples />}
                    </div>
                </div>
            </div>
        </div>
    )
}

function QuickStart() {
    return (
        <div className="doc-section animate-fade-in">
            <h2>🚀 快速开始</h2>
            <p className="doc-intro">三步完成接入，和使用 OpenAI 完全一样的体验。</p>

            <h3>Step 1: 获取 API Key</h3>
            <p>在 <a href="/register">注册页面</a> 创建账户，获取你的专属 API Key。</p>

            <h3>Step 2: 配置 Codex CLI</h3>
            <p>编辑 <code>~/.codex/config.toml</code>：</p>
            <pre className="code-block">{`model = "gpt-5.2-codex"
model_provider = "azure"
model_reasoning_effort = "high"

[model_providers.azure]
name = "Azure OpenAI"
base_url = "${SITE}/v1"
env_key = "COINCOIN_API_KEY"
wire_api = "responses"`}</pre>

            <h3>Step 3: 设置环境变量</h3>
            <pre className="code-block">{`# 临时设置
export COINCOIN_API_KEY="sk_cc_xxxxx"

# 永久设置
echo 'export COINCOIN_API_KEY="sk_cc_xxxxx"' >> ~/.zshrc
source ~/.zshrc`}</pre>

            <h3>第三方客户端配置</h3>
            <p>支持所有 OpenAI 兼容的客户端（Continue、Aider、ChatBox 等）：</p>
            <div className="config-table">
                <div className="config-row">
                    <span className="config-label">Base URL</span>
                    <code>{SITE}/v1</code>
                </div>
                <div className="config-row">
                    <span className="config-label">API Key</span>
                    <code>sk_cc_xxxxx</code>
                </div>
                <div className="config-row">
                    <span className="config-label">Model</span>
                    <code>gpt-5.2-codex</code>
                </div>
            </div>
        </div>
    )
}

function ModelsAndPricing() {
    return (
        <div className="doc-section animate-fade-in">
            <h2>💎 模型与价格</h2>
            <p className="doc-intro">按实际 Token 使用量计费，透明无隐藏费用。</p>

            <h3>可用模型</h3>
            <table className="data-table">
                <thead>
                    <tr><th>模型</th><th>上游</th><th>Input 价格</th><th>Output 价格</th><th>状态</th></tr>
                </thead>
                <tbody>
                    <tr>
                        <td><code className="model-tag-sm">gpt-5.2-codex</code></td>
                        <td>Azure OpenAI</td>
                        <td>$0.99 / M Token</td>
                        <td>$6.99 / M Token</td>
                        <td><span className="badge badge-success">可用</span></td>
                    </tr>
                </tbody>
            </table>

            <h3>计费说明</h3>
            <ul className="doc-list">
                <li>计费模式为 <strong>余额扣费</strong>（balance），按实际使用量扣除</li>
                <li>费用计算：<code>费用(分) = round(input × 99/1M + output × 699/1M)</code></li>
                <li>余额用完后请求返回 <code>HTTP 402</code>，充值后即可恢复</li>
                <li>所有价格单位为美元（USD）</li>
            </ul>

            <h3>充值方式</h3>
            <ul className="doc-list">
                <li>登录后在 <a href="/recharge">充值中心</a> 使用支付宝支付或兑换码充值</li>
                <li>充值后余额实时到账，按使用量自动扣除</li>
            </ul>
        </div>
    )
}

function ApiReference() {
    return (
        <div className="doc-section animate-fade-in">
            <h2>📡 API 参考</h2>
            <p className="doc-intro">所有接口均兼容 OpenAI API 格式，使用你的 API Key 进行认证。</p>

            <h3>认证方式</h3>
            <p>所有请求需在 Header 中携带 API Key：</p>
            <pre className="code-block">{`Authorization: Bearer sk_cc_xxxxx`}</pre>

            <h3>Chat Completions</h3>
            <div className="endpoint-block">
                <span className="method post">POST</span>
                <code>/v1/chat/completions</code>
            </div>
            <pre className="code-block">{`{
  "model": "gpt-5.2-codex",
  "messages": [{"role": "user", "content": "Hello"}],
  "stream": false
}`}</pre>

            <h3>Responses (Codex CLI)</h3>
            <div className="endpoint-block">
                <span className="method post">POST</span>
                <code>/v1/responses</code>
            </div>
            <pre className="code-block">{`{
  "model": "gpt-5.2-codex",
  "input": "Hello"
}`}</pre>

            <h3>查询余额</h3>
            <div className="endpoint-block">
                <span className="method get">GET</span>
                <code>/v1/balance</code>
            </div>

            <h3>错误码</h3>
            <table className="data-table">
                <thead>
                    <tr><th>状态码</th><th>含义</th><th>说明</th></tr>
                </thead>
                <tbody>
                    <tr><td>401</td><td>认证失败</td><td>API Key 缺失或无效</td></tr>
                    <tr><td>402</td><td>余额不足</td><td>请充值后重试</td></tr>
                    <tr><td>403</td><td>禁止访问</td><td>Key 被禁用、用户被封禁，或使用了 session key 访问 API</td></tr>
                    <tr><td>429</td><td>请求过多</td><td>超出速率或额度限制</td></tr>
                </tbody>
            </table>
        </div>
    )
}

function CodeExamples() {
    return (
        <div className="doc-section animate-fade-in">
            <h2>💻 代码示例</h2>
            <p className="doc-intro">常用语言和工具的集成示例。</p>

            <h3>Python (openai 库)</h3>
            <pre className="code-block">{`from openai import OpenAI

client = OpenAI(
    api_key="sk_cc_xxxxx",
    base_url="${SITE}/v1"
)

response = client.chat.completions.create(
    model="gpt-5.2-codex",
    messages=[{"role": "user", "content": "Hello!"}]
)

print(response.choices[0].message.content)`}</pre>

            <h3>JavaScript (fetch)</h3>
            <pre className="code-block">{`const res = await fetch(
  '${SITE}/v1/chat/completions',
  {
    method: 'POST',
    headers: {
      'Authorization': 'Bearer sk_cc_xxxxx',
      'Content-Type': 'application/json'
    },
    body: JSON.stringify({
      model: 'gpt-5.2-codex',
      messages: [{ role: 'user', content: 'Hello!' }]
    })
  }
);

const data = await res.json();
console.log(data.choices[0].message.content);`}</pre>

            <h3>cURL</h3>
            <pre className="code-block">{`curl -X POST ${SITE}/v1/chat/completions \\
  -H "Authorization: Bearer sk_cc_xxxxx" \\
  -H "Content-Type: application/json" \\
  -d '{
    "model": "gpt-5.2-codex",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'`}</pre>

            <h3>查询余额</h3>
            <pre className="code-block">{`curl ${SITE}/v1/balance \\
  -H "Authorization: Bearer sk_cc_xxxxx"`}</pre>
        </div>
    )
}
