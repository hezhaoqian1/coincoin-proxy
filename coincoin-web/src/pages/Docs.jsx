import { useState } from 'react'
import { describePublicModel, formatModelPrice } from '../api/client'
import { usePublicModels } from '../hooks/usePublicModels'
import './Docs.css'

const SITE = typeof window !== 'undefined' ? window.location.origin : ''
const TABS = ['快速开始', '模型与价格', 'API 参考', '代码示例']

function formatCaps(model) {
    return (model.coincoin_capabilities || []).join(' · ')
}

function formatTier(model) {
    const tier = model.coincoin_metadata?.tier || ''
    if (tier === 'preview') return '预览'
    if (tier === 'explicit') return '显式'
    if (tier === 'stable') return '稳定'
    return '可用'
}

export default function Docs() {
    const [activeTab, setActiveTab] = useState(0)
    const { models, textModels, imageModels, defaultTextModel, defaultImageModel } = usePublicModels()
    const primaryTextModel = defaultTextModel || textModels[0] || models[0]
    const primaryImageModel = defaultImageModel || imageModels[0] || null

    return (
        <div className="page-wrapper">
            <div className="container">
                <div className="page-header">
                    <h1 className="page-title">接入文档</h1>
                    <p className="page-desc">公开模型目录、兼容规则和多客户端接入方式都在这里</p>
                </div>

                <div className="docs-layout">
                    <nav className="docs-nav glass-card">
                        {TABS.map((tab, i) => (
                            <button
                                key={tab}
                                className={`docs-nav-item ${activeTab === i ? 'active' : ''}`}
                                onClick={() => setActiveTab(i)}
                            >
                                {['🚀', '💎', '📡', '💻'][i]} {tab}
                            </button>
                        ))}
                    </nav>

                    <div className="docs-content glass-card">
                        {activeTab === 0 && <QuickStart primaryTextModel={primaryTextModel} primaryImageModel={primaryImageModel} />}
                        {activeTab === 1 && <ModelsAndPricing textModels={textModels} imageModels={imageModels} />}
                        {activeTab === 2 && <ApiReference primaryTextModel={primaryTextModel} primaryImageModel={primaryImageModel} />}
                        {activeTab === 3 && <CodeExamples primaryTextModel={primaryTextModel} primaryImageModel={primaryImageModel} />}
                    </div>
                </div>
            </div>
        </div>
    )
}

function QuickStart({ primaryTextModel, primaryImageModel }) {
    const textModelId = primaryTextModel?.id || 'gpt-5.2-codex'
    const imageModelId = primaryImageModel?.id || 'gemini-image'

    return (
        <div className="doc-section animate-fade-in">
            <h2>🚀 快速开始</h2>
            <p className="doc-intro">CoinCoin 对外保持 OpenAI 兼容协议，但公开模型目录现在已经支持 GPT 文本、Gemini 文本和 Gemini 图片能力。</p>

            <div className="doc-callout">
                <strong>模型切换规则</strong>
                <p>现在可以直接通过请求里的 <code>model</code> 选择公开模型。老客户端如果不传 <code>model</code>，仍然会默认走兼容 GPT 文本模型，不会被这次升级打断。</p>
            </div>

            <h3>Step 1: 获取 API Key</h3>
            <p>在 <a href="/register">注册页面</a> 创建账户，获取你的专属 API Key。</p>

            <h3>Step 2: 配置客户端</h3>
            <p>以 Codex CLI 为例，编辑 <code>~/.codex/config.toml</code>：</p>
            <pre className="code-block">{`model = "${textModelId}"
model_provider = "coincoin"
model_reasoning_effort = "high"

[model_providers.coincoin]
name = "CoinCoin"
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
            <p>支持所有 OpenAI 兼容客户端。大多数客户端只需要这 3 个值：</p>
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
                    <code>{textModelId}</code>
                </div>
            </div>

            <h3>客户端支持矩阵</h3>
            <table className="data-table">
                <thead>
                    <tr><th>客户端</th><th>状态</th><th>推荐接法</th><th>说明</th></tr>
                </thead>
                <tbody>
                    <tr>
                        <td>Codex CLI</td>
                        <td><span className="badge badge-success">一等支持</span></td>
                        <td><code>/v1 + responses</code></td>
                        <td>最推荐的命令行客户端路径，直接通过 <code>model</code> 选择公开 alias。</td>
                    </tr>
                    <tr>
                        <td>OpenClaw</td>
                        <td><span className="badge badge-success">支持</span></td>
                        <td><code>/v1 + openai-completions</code></td>
                        <td>建议优先走 <code>chat/completions</code> 兼容面，不把 <code>responses</code> 作为默认承诺。</td>
                    </tr>
                    <tr>
                        <td>Gemini CLI</td>
                        <td><span className="badge badge-warning">暂缓</span></td>
                        <td><code>不建议直连 CoinCoin</code></td>
                        <td>它仍偏 Google 原生协议面。后续若要支持，建议单独做 CLI sidecar，而不是直接膨胀公共入口。</td>
                    </tr>
                </tbody>
            </table>

            <h3>切换模型时你要改什么？</h3>
            <ul className="doc-list">
                <li>只需要把请求或客户端配置中的 <code>model</code> 改成公开 alias，例如 <code>gemini-fast</code>、<code>gemini-reasoning</code>、<code>gemini-image</code>。</li>
                <li>Base URL 和 API Key 不需要改，仍然走同一个 CoinCoin 入口。</li>
                <li>文本请求推荐走 <code>/v1/chat/completions</code> 或 <code>/v1/responses</code>，图片请求走 <code>/v1/images/generations</code> 或 <code>/v1/images/edits</code>，并使用 <code>{imageModelId}</code> 这类图片 alias。</li>
                <li>Gemini 图片请求当前统一由 CoinCoin 控制面直连 Vertex 处理，而不是让终端用户直连内部 gateway。</li>
            </ul>
        </div>
    )
}

function ModelsAndPricing({ textModels, imageModels }) {
    return (
        <div className="doc-section animate-fade-in">
            <h2>💎 模型与价格</h2>
            <p className="doc-intro">公开模型目录来自 CoinCoin 的真实运行配置。你可以直接通过 <code>GET /v1/models</code> 拉取。</p>

            <h3>文本模型</h3>
            <table className="data-table">
                <thead>
                    <tr><th>Alias</th><th>上游</th><th>能力</th><th>价格</th><th>状态</th></tr>
                </thead>
                <tbody>
                    {textModels.map((model) => (
                        <tr key={model.id}>
                            <td>
                                <code className="model-tag-sm">{model.id}</code>
                                {(model.coincoin_default_for || []).includes('text') && <span className="inline-badge">默认文本</span>}
                            </td>
                            <td>
                                <div>{model.coincoin_provider}</div>
                                <div className="table-subtle">{model.coincoin_provider_model}</div>
                            </td>
                            <td>
                                <div>{formatCaps(model)}</div>
                                <div className="table-subtle">{describePublicModel(model)}</div>
                            </td>
                            <td>{formatModelPrice(model)}</td>
                            <td><span className={`badge ${model.coincoin_metadata?.tier === 'preview' ? 'badge-warning' : 'badge-success'}`}>{formatTier(model)}</span></td>
                        </tr>
                    ))}
                </tbody>
            </table>

            <h3>图片模型</h3>
            <table className="data-table">
                <thead>
                    <tr><th>Alias</th><th>上游</th><th>能力</th><th>价格</th><th>状态</th></tr>
                </thead>
                <tbody>
                    {imageModels.map((model) => (
                        <tr key={model.id}>
                            <td>
                                <code className="model-tag-sm">{model.id}</code>
                                {(model.coincoin_default_for || []).includes('image') && <span className="inline-badge">默认图片</span>}
                            </td>
                            <td>
                                <div>{model.coincoin_provider}</div>
                                <div className="table-subtle">{model.coincoin_provider_model}</div>
                            </td>
                            <td>
                                <div>{formatCaps(model)}</div>
                                <div className="table-subtle">{describePublicModel(model)}</div>
                            </td>
                            <td>{formatModelPrice(model)}</td>
                            <td><span className={`badge ${model.coincoin_metadata?.tier === 'preview' ? 'badge-warning' : 'badge-success'}`}>{formatTier(model)}</span></td>
                        </tr>
                    ))}
                </tbody>
            </table>

            <h3>计费说明</h3>
            <ul className="doc-list">
                <li>文本模型按 Input / Output Token 计费；图片模型按图片张数计费。</li>
                <li>同一个账户余额同时覆盖 GPT 文本、Gemini 文本和 Gemini 生图，不需要分开充值。</li>
                <li>老客户端不传 <code>model</code> 时，仍然走默认文本 alias，以保证兼容。</li>
            </ul>
        </div>
    )
}

function ApiReference({ primaryTextModel, primaryImageModel }) {
    const textModelId = primaryTextModel?.id || 'gpt-5.2-codex'
    const imageModelId = primaryImageModel?.id || 'gemini-image'

    return (
        <div className="doc-section animate-fade-in">
            <h2>📡 API 参考</h2>
            <p className="doc-intro">所有接口均兼容 OpenAI API 风格。当前推荐先从 <code>/v1/models</code> 拉取公开目录，再决定请求用哪个 alias。</p>

            <h3>认证方式</h3>
            <pre className="code-block">{`Authorization: Bearer sk_cc_xxxxx`}</pre>

            <h3>模型目录</h3>
            <div className="endpoint-block">
                <span className="method get">GET</span>
                <code>/v1/models</code>
            </div>
            <div className="endpoint-block">
                <span className="method get">GET</span>
                <code>/v1/models/{'{model_id}'}</code>
            </div>

            <h3>Chat Completions</h3>
            <div className="endpoint-block">
                <span className="method post">POST</span>
                <code>/v1/chat/completions</code>
            </div>
            <pre className="code-block">{`{
  "model": "${textModelId}",
  "messages": [{"role": "user", "content": "Hello"}],
  "stream": false
}`}</pre>

            <h3>Responses</h3>
            <div className="endpoint-block">
                <span className="method post">POST</span>
                <code>/v1/responses</code>
            </div>
            <pre className="code-block">{`{
  "model": "${textModelId}",
  "input": "Hello"
}`}</pre>

            <h3>Images: 生成</h3>
            <div className="endpoint-block">
                <span className="method post">POST</span>
                <code>/v1/images/generations</code>
            </div>
            <pre className="code-block">{`{
  "model": "${imageModelId}",
  "prompt": "A futuristic coin mascot in a glass city",
  "size": "1024x1024"
}`}</pre>

            <h3>Images: 编辑 / 图生图</h3>
            <div className="endpoint-block">
                <span className="method post">POST</span>
                <code>/v1/images/edits</code>
            </div>
            <pre className="code-block">{`curl ${SITE}/v1/images/edits \\
  -H "Authorization: Bearer sk_cc_xxxxx" \\
  -F "model=${imageModelId}" \\
  -F "prompt=Turn this into a clean pixel-art icon" \\
  -F "n=1" \\
  -F "size=1024x1024" \\
  -F "image=@./input.png"`}</pre>

            <ul className="doc-list">
                <li>当前 Gemini 图生图支持 1 张或多张输入图，但输出候选数当前只支持 <code>n=1</code>。</li>
                <li>当前 Gemini 图生图不支持 <code>mask</code> 上传；如果传了掩码，会返回 <code>mask_not_supported</code>。</li>
                <li>如果平台运营侧没有配置好 Vertex 图片变量，Gemini 图片请求会返回配置错误，而不是偷偷回退到别的模型。</li>
            </ul>

            <h3>默认兼容规则</h3>
            <ul className="doc-list">
                <li>如果文本请求里省略 <code>model</code>，CoinCoin 会保持默认 GPT 文本模型的兼容行为。</li>
                <li>如果图片请求里省略 <code>model</code>，CoinCoin 会自动选择默认图片 alias。</li>
                <li>显式指定 Gemini alias 后，如果 Gemini 上游失败，不会偷偷回退到 GPT。</li>
            </ul>

            <h3>错误码</h3>
            <table className="data-table">
                <thead>
                    <tr><th>状态码</th><th>含义</th><th>说明</th></tr>
                </thead>
                <tbody>
                    <tr><td>400</td><td>模型或参数错误</td><td>例如模型不存在、模型不支持该端点</td></tr>
                    <tr><td>401</td><td>认证失败</td><td>API Key 缺失或无效</td></tr>
                    <tr><td>402</td><td>余额不足</td><td>请充值后重试</td></tr>
                    <tr><td>403</td><td>禁止访问</td><td>Key 被禁用、用户被封禁，或使用了 session key 访问 API</td></tr>
                    <tr><td>429</td><td>请求过多</td><td>超出速率或额度限制</td></tr>
                    <tr><td>503</td><td>平台未配置 Gemini 图片运行时</td><td>例如 <code>vertex_image_generation_not_configured</code>、<code>vertex_image_edit_not_configured</code></td></tr>
                </tbody>
            </table>
        </div>
    )
}

function CodeExamples({ primaryTextModel, primaryImageModel }) {
    const textModelId = primaryTextModel?.id || 'gpt-5.2-codex'
    const imageModelId = primaryImageModel?.id || 'gemini-image'

    return (
        <div className="doc-section animate-fade-in">
            <h2>💻 代码示例</h2>
            <p className="doc-intro">核心原则只有一条：Base URL 不变，只改 <code>model</code> 就能切换到 CoinCoin 公开目录里的不同模型。图片能力的上游细节由平台控制，不需要终端用户理解内部 gateway。</p>

            <div className="doc-callout">
                <strong>关于 Gemini CLI</strong>
                <p>当前不把 Gemini CLI 作为 CoinCoin 公共入口的一等客户端承诺。技术上它存在代理和自定义 base URL 的空间，但当前更适合作为后续独立兼容侧车处理，而不是直接和 Codex / OpenClaw 共用这套公共 OpenAI 面。</p>
            </div>

            <h3>Python (openai 库)</h3>
            <pre className="code-block">{`from openai import OpenAI

client = OpenAI(
    api_key="sk_cc_xxxxx",
    base_url="${SITE}/v1"
)

response = client.chat.completions.create(
    model="${textModelId}",
    messages=[{"role": "user", "content": "Hello!"}]
)

print(response.choices[0].message.content)`}</pre>

            <h3>JavaScript (fetch, 生图示例)</h3>
            <pre className="code-block">{`const res = await fetch(
  '${SITE}/v1/images/generations',
  {
    method: 'POST',
    headers: {
      'Authorization': 'Bearer sk_cc_xxxxx',
      'Content-Type': 'application/json'
    },
    body: JSON.stringify({
      model: '${imageModelId}',
      prompt: 'A cinematic poster for a developer tools launch',
      size: '1024x1024'
    })
  }
);

const data = await res.json();
console.log(data.data[0]);`}</pre>

            <h3>Codex CLI</h3>
            <pre className="code-block">{`model = "${textModelId}"
model_provider = "coincoin"
model_reasoning_effort = "high"

[model_providers.coincoin]
name = "CoinCoin"
base_url = "${SITE}/v1"
env_key = "COINCOIN_API_KEY"
wire_api = "responses"`}</pre>

            <h3>OpenClaw</h3>
            <pre className="code-block">{`{
  "models": {
    "providers": {
      "coincoin": {
        "baseUrl": "${SITE}/v1",
        "apiKey": "sk_cc_xxxxx",
        "api": "openai-completions",
        "models": [{"id": "${textModelId}", "contextWindow": 131072}]
      }
    },
    "defaults": {
      "provider": "coincoin",
      "model": "${textModelId}"
    }
  }
}`}</pre>

            <h3>为什么还要看 Vertex 官方文档？</h3>
            <ul className="doc-list">
                <li>LiteLLM 负责代理和协议适配，但上游 Gemini 的真实能力边界仍以 Vertex 官方文档为准。</li>
                <li>当你遇到 function calling、参数支持或模型生命周期问题时，先查 Vertex 官方文档，再看 CoinCoin/LiteLLM 配置。</li>
                <li>简单说：客户端接 CoinCoin，网关看 LiteLLM，模型能力和鉴权边界看 Vertex。</li>
            </ul>
        </div>
    )
}
