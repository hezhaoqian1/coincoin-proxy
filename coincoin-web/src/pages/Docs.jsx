import { useEffect, useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { describePublicModel, getBalance, getCachedInputPricePerMillion } from '../api/client'
import AppShell from '../components/AppShell'
import { useAuth } from '../hooks/useAuth'
import { usePublicModels } from '../hooks/usePublicModels'
import './Docs.css'

const SITE = typeof window !== 'undefined' ? window.location.origin : ''
const TABS = [
    {
        label: '快速开始',
        kicker: 'Getting Started',
        intro: '从开发者 Key 到第一条请求。'
    },
    {
        label: '可用模型',
        kicker: 'Catalog',
        intro: '模型、价格和当前接入状态。'
    },
    {
        label: 'API 参考',
        kicker: 'Protocol',
        intro: '看端点、认证方式和图片接口边界。'
    },
    {
        label: '代码示例',
        kicker: 'Snippets',
        intro: '常见客户端、CLI 和 SDK 配置。'
    }
]

const TAB_INDEX_BY_KEY = {
    quickstart: 0,
    models: 1,
    api: 2,
    snippets: 3,
}

const TAB_KEY_BY_INDEX = ['quickstart', 'models', 'api', 'snippets']

const GEMINI_OFFICIAL_PRICE_SOURCE = 'Google Gemini API pricing · checked 2026-05-09'
const OPENAI_IMAGE_OFFICIAL_PRICE_SOURCE = 'OpenAI image generation pricing · checked 2026-05-09'
const OFFICIAL_PRICING = {
    'gpt-image-2': {
        providerModel: 'GPT Image 2',
        image: '$0.053 / image · medium 1024x1024 reference',
        source: OPENAI_IMAGE_OFFICIAL_PRICE_SOURCE,
    },
    'gemini-balanced': {
        providerModel: 'Gemini 2.5 Flash-Lite',
        input: '$0.10 / 1M input tokens',
        output: '$0.40 / 1M output tokens',
        source: GEMINI_OFFICIAL_PRICE_SOURCE,
    },
    'gemini-fast': {
        providerModel: 'Gemini 2.5 Flash',
        input: '$0.30 / 1M input tokens',
        output: '$2.50 / 1M output tokens',
        source: GEMINI_OFFICIAL_PRICE_SOURCE,
    },
    'gemini-reasoning': {
        providerModel: 'Gemini 2.5 Pro (<=200K prompt)',
        input: '$1.25 / 1M input tokens',
        output: '$10.00 / 1M output tokens',
        source: GEMINI_OFFICIAL_PRICE_SOURCE,
    },
    'gemini-image': {
        providerModel: 'Gemini 3.1 Flash Image Preview (1K)',
        image: '$0.067 / 1K image',
        source: GEMINI_OFFICIAL_PRICE_SOURCE,
    },
    'gemini-3.1-flash-image': {
        providerModel: 'Gemini 3.1 Flash Image Preview (1K)',
        image: '$0.067 / 1K image',
        source: GEMINI_OFFICIAL_PRICE_SOURCE,
    },
    'vertex-gemini-2.5-flash-image': {
        providerModel: 'Gemini 3.1 Flash Image Preview (1K)',
        image: '$0.067 / 1K image',
        source: GEMINI_OFFICIAL_PRICE_SOURCE,
    },
    'vertex-gemini-3.1-flash-image-preview': {
        providerModel: 'Gemini 3.1 Flash Image Preview (1K)',
        image: '$0.067 / 1K image',
        source: GEMINI_OFFICIAL_PRICE_SOURCE,
    },
}

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

function formatUsdPerMillion(cents, precision = 2) {
    const value = Number(cents || 0)
    if (!value) return '后台配置'
    return `$${(value / 100).toFixed(precision)} / 1M tokens`
}

function formatImagePrice(cents) {
    const value = Number(cents || 0)
    if (!value) return '后台配置'
    return `$${(value / 100).toFixed(3)} / image`
}

function getOfficialPricing(model) {
    return OFFICIAL_PRICING[model?.id || ''] || null
}

function getProviderName(model) {
    return model?.owned_by === 'google' ? 'Google' : model?.owned_by || 'provider'
}

export default function Docs() {
    const { isLoggedIn } = useAuth()
    const [searchParams, setSearchParams] = useSearchParams()
    const requestedTab = searchParams.get('tab')
    const [activeTab, setActiveTab] = useState(TAB_INDEX_BY_KEY[requestedTab] ?? 0)
    const { models, textModels, imageModels, defaultTextModel, defaultImageModel } = usePublicModels()
    const primaryTextModel = defaultTextModel || textModels[0] || models[0]
    const primaryImageModel = defaultImageModel || imageModels[0] || null
    const activeSection = TABS[activeTab]

    useEffect(() => {
        setActiveTab(TAB_INDEX_BY_KEY[requestedTab] ?? 0)
    }, [requestedTab])

    const docsIntro = useMemo(() => {
        if (requestedTab === 'models') return '模型、价格和当前接入状态。'
        if (requestedTab === 'api') return '端点、认证方式和兼容边界。'
        if (requestedTab === 'snippets') return '常见客户端、CLI 和 SDK 配置。'
        return '从开发者 Key 到第一条请求。'
    }, [requestedTab])

    const handleTabChange = (index) => {
        setActiveTab(index)
        const next = new URLSearchParams(searchParams)
        next.set('tab', TAB_KEY_BY_INDEX[index])
        setSearchParams(next, { replace: true })
    }

    const content = (
        <div className="page-wrapper">
            <div className="container">
                <div className="page-header">
                    <h1 className="page-title">接入文档</h1>
                    <p className="page-desc">模型目录、接口说明和客户端配置</p>
                </div>

                <div className="docs-layout">
                    <nav className="docs-nav glass-card">
                        <div className="docs-nav-header">
                            <span className="docs-nav-kicker">{activeSection.kicker}</span>
                            <h2>{activeSection.label}</h2>
                            <p>{activeSection.intro}</p>
                        </div>
                        {TABS.map((tab, i) => (
                            <button
                                key={tab.label}
                                className={`docs-nav-item ${activeTab === i ? 'active' : ''}`}
                                onClick={() => handleTabChange(i)}
                            >
                                {tab.label}
                            </button>
                        ))}
                    </nav>

                    <div className="docs-content glass-card">
                        {activeTab === 0 && <QuickStart primaryTextModel={primaryTextModel} primaryImageModel={primaryImageModel} />}
                        {activeTab === 1 && <ModelsAndPricing textModels={textModels} imageModels={imageModels} defaultTextModel={defaultTextModel} defaultImageModel={defaultImageModel} />}
                        {activeTab === 2 && <ApiReference primaryTextModel={primaryTextModel} primaryImageModel={primaryImageModel} />}
                        {activeTab === 3 && <CodeExamples primaryTextModel={primaryTextModel} primaryImageModel={primaryImageModel} />}
                    </div>
                </div>
            </div>
        </div>
    )

    if (isLoggedIn) {
        if (activeTab === 1) {
            return (
                <AppShell title="可用模型" description="模型、价格和当前接入状态。">
                    <div className="docs-shell-page">
                        <div className="docs-content glass-card">
                            <ModelsAndPricing textModels={textModels} imageModels={imageModels} defaultTextModel={defaultTextModel} defaultImageModel={defaultImageModel} />
                        </div>
                    </div>
                </AppShell>
            )
        }

        return (
            <AppShell title="接入文档" description="模型目录、接口说明和客户端配置。">
                <div className="docs-shell-page">
                    <div className="docs-shell-hero glass-card">
                        <div>
                            <span className="docs-shell-kicker">Documentation</span>
                            <h2>{activeSection.label}</h2>
                            <p>{docsIntro}</p>
                        </div>
                    </div>
                    <div className="docs-layout">
                        <nav className="docs-nav glass-card">
                            <div className="docs-nav-header">
                                <span className="docs-nav-kicker">{activeSection.kicker}</span>
                                <h2>{activeSection.label}</h2>
                                <p>{activeSection.intro}</p>
                            </div>
                            {TABS.map((tab, i) => (
                                <button
                                    key={tab.label}
                                    className={`docs-nav-item ${activeTab === i ? 'active' : ''}`}
                                    onClick={() => handleTabChange(i)}
                                >
                                    {tab.label}
                                </button>
                            ))}
                        </nav>

                        <div className="docs-content glass-card">
                            {activeTab === 0 && <QuickStart primaryTextModel={primaryTextModel} primaryImageModel={primaryImageModel} />}
                            {activeTab === 1 && <ModelsAndPricing textModels={textModels} imageModels={imageModels} defaultTextModel={defaultTextModel} defaultImageModel={defaultImageModel} />}
                            {activeTab === 2 && <ApiReference primaryTextModel={primaryTextModel} primaryImageModel={primaryImageModel} />}
                            {activeTab === 3 && <CodeExamples primaryTextModel={primaryTextModel} primaryImageModel={primaryImageModel} />}
                        </div>
                    </div>
                </div>
            </AppShell>
        )
    }

    return content
}

function AudienceGuide() {
    const routes = [
        {
            title: '直连 API / cURL',
            tag: '通用',
            desc: '服务端脚本、后端接口和 OpenAI 兼容调用。',
            bullets: ['先看 API 参考', '常用端点是 chat / responses / models', '失败时再查错误码和余额']
        },
        {
            title: 'Codex CLI',
            tag: '推荐',
            desc: '命令行工作流，走 OpenAI 兼容入口。',
            bullets: ['准备开发者 Key', '直接照抄 config.toml', '通常用 responses']
        },
        {
            title: 'OpenCode',
            tag: '已实测',
            desc: '本地 coding agent 工作流，已验证基础可用。',
            bullets: ['先看 OpenCode quickstart', '默认用 opus', '需要更快时再试 sonnet']
        },
        {
            title: 'Continue / Aider',
            tag: '常见客户端',
            desc: '只要支持 OpenAI-compatible 配置，基本都能按这套接。',
            bullets: ['填 Base URL + API Key + model', '默认用 opus 或 sonnet', '接不上时先排查 key 类型']
        },
        {
            title: 'Claude Code',
            tag: '推荐',
            desc: 'Claude Code 走 Anthropic 兼容入口。',
            bullets: ['用 ANTHROPIC_BASE_URL 根域名', '用 ANTHROPIC_AUTH_TOKEN', '模型名可填 opus / sonnet / haiku']
        },
        {
            title: 'OpenClaw',
            tag: '兼容',
            desc: '已有 OpenAI 风格 provider 配置时，直接换 provider 和默认模型。',
            bullets: ['看代码示例里的 OpenClaw', '优先走 openai-completions', '上下文窗口按示例填写']
        },
        {
            title: '生图 / 图生图',
            tag: '图片',
            desc: '图片生成、少图编辑和多图异步任务。',
            bullets: ['看 API 参考里的 images', '1-2 张图走 edits', '3-8 张图走 image-jobs']
        }
    ]

    return (
        <div className="audience-guide">
            <h3>按接入方式查看</h3>
            <div className="audience-grid">
                {routes.map((route) => (
                    <div key={route.title} className="audience-card">
                        <span className="inline-badge audience-badge">{route.tag}</span>
                        <strong>{route.title}</strong>
                        <p>{route.desc}</p>
                        <ul className="doc-list audience-list">
                            {route.bullets.map((item) => (
                                <li key={item}>{item}</li>
                            ))}
                        </ul>
                    </div>
                ))}
            </div>
        </div>
    )
}

function QuickStart({ primaryTextModel, primaryImageModel }) {
    const textModelId = primaryTextModel?.id || 'opus'
    const imageModelId = primaryImageModel?.id || 'gpt-image-2'
    const quickstartSteps = [
        {
            title: '登录控制台',
            desc: '先登录控制台，后续的余额、日志和密钥都从这里管。'
        },
        {
            title: '生成开发者 Key',
            desc: '在概览页生成给 CLI、SDK、cURL 和客户端使用的开发者 Key。'
        },
        {
            title: '发出第一条请求',
            desc: '先用统一 /v1 入口跑通一条文本请求，再按需切模型和客户端。'
        }
    ]

    return (
        <div className="doc-section animate-fade-in">
            <h2>快速开始</h2>
            <p className="doc-intro">先跑通一条请求，再接客户端。</p>

            <div className="quickstart-rail">
                {quickstartSteps.map((step, index) => (
                    <div key={step.title} className="quickstart-step">
                        <span className="quickstart-step-index">0{index + 1}</span>
                        <div>
                            <strong>{step.title}</strong>
                            <p>{step.desc}</p>
                        </div>
                    </div>
                ))}
            </div>

            <div className="quick-actions-row">
                <Link className="btn btn-primary btn-sm" to="/api-keys">生成 Key</Link>
                <Link className="btn btn-secondary btn-sm" to="/docs?tab=models">看可用模型</Link>
                <Link className="btn btn-ghost btn-sm" to="/usage">请求记录</Link>
            </div>

            <h3>查余额</h3>
            <pre className="code-block">{`curl ${SITE}/v1/balance \\
  -H "Authorization: Bearer sk_cc_xxxxx"`}</pre>

            <h3>发请求</h3>
            <pre className="code-block">{`curl ${SITE}/v1/chat/completions \\
  -H "Authorization: Bearer sk_cc_xxxxx" \\
  -H "Content-Type: application/json" \\
  -d '{
    "model": "${textModelId}",
    "messages": [{"role": "user", "content": "Reply with only: OK"}],
    "stream": false
  }'`}</pre>

            <h3>Gemini 生图</h3>
            <div className="gemini-usage-grid">
                <div className="gemini-usage-card">
                    <span className="inline-badge">文生图</span>
                    <strong>POST /v1/images/generations</strong>
                    <p>把模型设为 <code>{imageModelId}</code>，其余认证和 Base URL 仍然使用同一把开发者 Key。</p>
                </div>
                <div className="gemini-usage-card">
                    <span className="inline-badge">图生图</span>
                    <strong>POST /v1/images/edits</strong>
                    <p>上传 1-2 张参考图时使用同步编辑；3-8 张参考图改用异步 <code>/v1/image-jobs/edits</code>。</p>
                </div>
            </div>
            <pre className="code-block">{`curl ${SITE}/v1/images/generations \\
  -H "Authorization: Bearer sk_cc_xxxxx" \\
  -H "Content-Type: application/json" \\
  -d '{
    "model": "${imageModelId}",
    "prompt": "A polished product poster for an AI gateway",
    "size": "1024x1024",
    "n": 1
  }'`}</pre>

            <h3>Codex</h3>
            <pre className="code-block">{`model = "${textModelId}"
model_provider = "coincoin"
disable_response_storage = true
model_reasoning_effort = "high"
web_search = "live"
personality = "pragmatic"

[model_providers.coincoin]
name = "CoinCoin"
base_url = "${SITE}/v1"
experimental_bearer_token = "sk_cc_xxxxx"
wire_api = "responses"`}</pre>

            <h3>Claude Code</h3>
            <pre className="code-block">{`export ANTHROPIC_BASE_URL="${SITE}"
export ANTHROPIC_AUTH_TOKEN="sk_cc_xxxxx"
claude --model claude-opus-4-7`}</pre>

            <h3>第三方客户端配置</h3>
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
                        <td>Claude Code</td>
                        <td><span className="badge badge-success">一等支持</span></td>
                        <td><code>ANTHROPIC_BASE_URL=root</code></td>
                        <td>官方 Claude CLI 直接走 Anthropic 兼容面，模型可选 <code>claude-opus-4-7</code>、<code>claude-sonnet-4-6</code>、<code>opus</code>、<code>sonnet</code>。</td>
                    </tr>
                    <tr>
                        <td>Codex CLI</td>
                        <td><span className="badge badge-success">一等支持</span></td>
                        <td><code>/v1 + responses</code></td>
                        <td>推荐的命令行接法，直接通过 <code>model</code> 选择模型。</td>
                    </tr>
                    <tr>
                        <td>OpenCode</td>
                        <td><span className="badge badge-success">已实测支持</span></td>
                        <td><code>/v1 + 自定义 provider</code></td>
                        <td>已实测通过 <code>opencode run</code>、模型发现和基础文件读取。默认推荐 <code>clawfather/opus</code>。</td>
                    </tr>
                    <tr>
                        <td>OpenClaw</td>
                        <td><span className="badge badge-success">支持</span></td>
                        <td><code>/v1 + openai-completions</code></td>
                        <td>优先走 <code>chat/completions</code> 兼容面。</td>
                    </tr>
                    <tr>
                        <td>Continue / Aider / ChatBox</td>
                        <td><span className="badge badge-success">支持</span></td>
                        <td><code>/v1 + OpenAI-compatible</code></td>
                        <td>只要能自定义 <code>base_url</code>、<code>api_key</code> 和 <code>model</code>，就按通用接法接。</td>
                    </tr>
                    <tr>
                        <td>Google Gemini CLI</td>
                        <td><span className="badge badge-warning">暂缓</span></td>
                        <td><code>不建议直连 ClawFather</code></td>
                        <td>它仍偏 Google 原生协议面，当前不建议直接接到公共入口。</td>
                    </tr>
                </tbody>
            </table>

            <h3>切换模型时你要改什么？</h3>
            <ul className="doc-list">
                <li>只需要把请求或客户端配置中的 <code>model</code> 改成目标模型，例如 <code>opus</code>、<code>sonnet</code>、<code>haiku</code>、<code>gemini-image</code>。</li>
                <li>Base URL 和 API Key 不需要改，仍然走同一个 ClawFather 入口。</li>
                <li>文本请求推荐走 <code>/v1/chat/completions</code> 或 <code>/v1/responses</code>，图片请求走 <code>/v1/images/generations</code> 或 <code>/v1/images/edits</code>，并使用 <code>{imageModelId}</code> 这类图片模型。</li>
                <li>图片请求统一走 ClawFather 公开入口，不需要终端用户配置额外服务。</li>
            </ul>
        </div>
    )
}

function StatusCard({ label, value, tone = 'neutral', action }) {
    return (
        <div className={`access-status-card ${tone}`}>
            <span>{label}</span>
            <strong>{value}</strong>
            {action}
        </div>
    )
}

function ModelsAndPricing({ textModels, imageModels, defaultTextModel, defaultImageModel }) {
    const { activeDeveloperKeyCount, effectiveApiKey, hasDeveloperKey, isLoggedIn } = useAuth()
    const [balance, setBalance] = useState({ status: 'idle', data: null })
    const [copied, setCopied] = useState('')
    const openaiBaseUrl = `${SITE}/v1`
    const claudeBaseUrl = SITE
    const snippetKey = effectiveApiKey || 'sk_cc_xxxxx'
    const firstCurl = `curl ${openaiBaseUrl}/chat/completions \\
  -H "Authorization: Bearer ${snippetKey}" \\
  -H "Content-Type: application/json" \\
  -d '{"model":"${defaultTextModel?.id || textModels[0]?.id || 'opus'}","messages":[{"role":"user","content":"Reply with only: OK"}]}'`

    useEffect(() => {
        if (!isLoggedIn) {
            setBalance({ status: 'idle', data: null })
            return undefined
        }
        let active = true
        setBalance({ status: 'loading', data: null })
        getBalance()
            .then((data) => {
                if (active) setBalance({ status: 'ready', data })
            })
            .catch(() => {
                if (active) setBalance({ status: 'error', data: null })
            })
        return () => {
            active = false
        }
    }, [isLoggedIn])

    const copy = async (text, label) => {
        await navigator.clipboard.writeText(text)
        setCopied(label)
        setTimeout(() => setCopied(''), 1800)
    }

    return (
        <div className="doc-section animate-fade-in">
            <h2>可用模型</h2>
            <div className="access-status-grid">
                {isLoggedIn && (
                    <StatusCard
                        label="余额"
                        value={balance.status === 'ready' ? `$${Number(balance.data?.balance_usd || 0).toFixed(2)}` : balance.status === 'loading' ? '读取中' : '读取失败'}
                        tone={balance.status === 'ready' && balance.data?.balance_usd > 0 ? 'ok' : 'warn'}
                        action={<Link className="btn btn-ghost btn-sm" to="/recharge?section=recharge">充值</Link>}
                    />
                )}
                {isLoggedIn && (
                    <StatusCard
                        label="开发者 Key"
                        value={hasDeveloperKey ? `${activeDeveloperKeyCount || 1} 把可用` : '未生成'}
                        tone={hasDeveloperKey ? 'ok' : 'warn'}
                        action={<Link className="btn btn-ghost btn-sm" to="/api-keys">管理</Link>}
                    />
                )}
                <StatusCard
                    label="文本模型"
                    value={defaultTextModel?.id || textModels[0]?.id || '未配置'}
                    tone={textModels.length ? 'ok' : 'warn'}
                />
                <StatusCard
                    label="图片模型"
                    value={defaultImageModel?.id || imageModels[0]?.id || '未配置'}
                    tone={imageModels.length ? 'ok' : 'neutral'}
                />
            </div>

            <div className="endpoint-strip">
                <button onClick={() => copy(openaiBaseUrl, 'openai')}><span>OpenAI Base URL</span><code>{openaiBaseUrl}</code><strong>{copied === 'openai' ? '已复制' : '复制'}</strong></button>
                <button onClick={() => copy(claudeBaseUrl, 'claude')}><span>Claude Code Base URL</span><code>{claudeBaseUrl}</code><strong>{copied === 'claude' ? '已复制' : '复制'}</strong></button>
                <button onClick={() => copy(firstCurl, 'curl')}><span>第一条请求</span><code>curl chat/completions</code><strong>{copied === 'curl' ? '已复制' : '复制'}</strong></button>
            </div>

            <h3>文本模型</h3>
            <div className="official-price-callout">
                <div>
                    <span className="docs-shell-kicker">Official Benchmark</span>
                    <strong>Gemini 价格按 Google Gemini API 官方价格写入默认目录</strong>
                    <p>页面展示的价格来自 <code>/v1/models</code>；生产环境如果配置了价格环境变量，会以线上返回值为准。</p>
                </div>
                <a href="https://ai.google.dev/gemini-api/docs/pricing" target="_blank" rel="noreferrer" className="btn btn-ghost btn-sm">查看官方价格</a>
            </div>
            <div className="pricing-table-wrap">
                <table className="data-table pricing-table pricing-table-text">
                    <thead>
                        <tr>
                            <th>模型名称</th>
                            <th>输入价格</th>
                            <th>输出价格</th>
                            <th>缓存读取</th>
                            <th>官方对标</th>
                            <th>描述</th>
                            <th>状态</th>
                        </tr>
                    </thead>
                    <tbody>
                        {textModels.map((model) => {
                            const isDefault = (model.coincoin_default_for || []).includes('text')
                            const official = getOfficialPricing(model)
                            return (
                                <tr key={model.id}>
                                    <td>
                                        <div className="model-name-cell">
                                            <code className="model-tag-sm">{model.id}</code>
                                            {isDefault && <span className="inline-badge">默认文本</span>}
                                        </div>
                                    </td>
                                    <td className="price-cell">{formatUsdPerMillion(model.coincoin_price_input_per_million)}</td>
                                    <td className="price-cell">{formatUsdPerMillion(model.coincoin_price_output_per_million)}</td>
                                    <td className="price-cell">{formatUsdPerMillion(getCachedInputPricePerMillion(model), 3)}</td>
                                    <td>
                                        {official ? (
                                            <div className="official-price-cell">
                                                <strong>{official.providerModel}</strong>
                                                <span>{official.input} · {official.output}</span>
                                                <em>{official.source}</em>
                                            </div>
                                        ) : (
                                            <span className="table-subtle">{getProviderName(model)}</span>
                                        )}
                                    </td>
                                    <td>
                                        <div>{describePublicModel(model)}</div>
                                        <div className="table-subtle">{formatCaps(model)}</div>
                                    </td>
                                    <td><span className={`badge ${model.coincoin_metadata?.tier === 'preview' ? 'badge-warning' : 'badge-success'}`}>{formatTier(model)}</span></td>
                                </tr>
                            )
                        })}
                    </tbody>
                </table>
            </div>

            <h3>图片模型</h3>
            <div className="pricing-table-wrap">
                <table className="data-table pricing-table pricing-table-image">
                    <thead>
                        <tr>
                            <th>模型名称</th>
                            <th>图片价格</th>
                            <th>官方对标</th>
                            <th>描述</th>
                            <th>状态</th>
                        </tr>
                    </thead>
                    <tbody>
                        {imageModels.map((model) => {
                            const official = getOfficialPricing(model)
                            return (
                                <tr key={model.id}>
                                    <td>
                                        <div className="model-name-cell">
                                            <code className="model-tag-sm">{model.id}</code>
                                            {(model.coincoin_default_for || []).includes('image') && <span className="inline-badge">默认图片</span>}
                                        </div>
                                    </td>
                                    <td className="price-cell">{formatImagePrice(model.coincoin_price_per_image_cents)}</td>
                                    <td>
                                        {official ? (
                                            <div className="official-price-cell">
                                                <strong>{official.providerModel}</strong>
                                                <span>{official.image}</span>
                                                <em>{official.source}</em>
                                            </div>
                                        ) : (
                                            <span className="table-subtle">{getProviderName(model)}</span>
                                        )}
                                    </td>
                                    <td>
                                        <div>{describePublicModel(model)}</div>
                                        <div className="table-subtle">{formatCaps(model)}</div>
                                    </td>
                                    <td><span className={`badge ${model.coincoin_metadata?.tier === 'preview' ? 'badge-warning' : 'badge-success'}`}>{formatTier(model)}</span></td>
                                </tr>
                            )
                        })}
                    </tbody>
                </table>
            </div>

            <h3>图片生成怎么用</h3>
            <div className="gemini-usage-grid">
                <div className="gemini-usage-card">
                    <span className="inline-badge">Default</span>
                    <strong><code>{defaultImageModel?.id || imageModels[0]?.id || 'gpt-image-2'}</code></strong>
                    <p>文生图走 <code>/v1/images/generations</code>。不传 <code>model</code> 时自动选择默认图片模型；也可以显式传入这个模型。</p>
                </div>
                <div className="gemini-usage-card">
                    <span className="inline-badge">Gemini</span>
                    <strong><code>gemini-image</code></strong>
                    <p>需要 Gemini 生图或 Gemini 图生图时，在请求里显式传 <code>model: "gemini-image"</code>。多图异步编辑继续用 Gemini 图片模型。</p>
                </div>
            </div>
            <pre className="code-block">{`curl ${openaiBaseUrl}/images/generations \\
  -H "Authorization: Bearer ${snippetKey}" \\
  -H "Content-Type: application/json" \\
  -d '{
    "model": "${defaultImageModel?.id || imageModels[0]?.id || 'gpt-image-2'}",
    "prompt": "A clean product poster for an AI gateway",
    "size": "1024x1024",
    "n": 1
  }'`}</pre>
            <pre className="code-block">{`curl ${openaiBaseUrl}/images/generations \\
  -H "Authorization: Bearer ${snippetKey}" \\
  -H "Content-Type: application/json" \\
  -d '{
    "model": "gemini-image",
    "prompt": "A clean product poster in Gemini image style",
    "size": "1024x1024",
    "n": 1
  }'`}</pre>

            <h3>计费说明</h3>
            <ul className="doc-list">
                <li>文本模型按 Input / Cached Read / Output Token 计费；图片模型按图片张数计费。</li>
                <li>Cached input 表示缓存读取 token，使用模型目录返回的单独缓存读取价格计费，不要按普通 Input 价格估算。</li>
                <li><code>gpt-image-2</code> 默认图片价格按 OpenAI 官方 medium <code>1024x1024</code> 参考价展示；实际成本会随质量、尺寸和上游计费策略变化。</li>
                <li><code>gemini-image</code> 价格对标 Google Gemini API 官方价格，当前按 Gemini 3.1 Flash Image Preview 的 <code>$0.067 / 1K image</code> 展示。</li>
                <li>同一个账户余额同时覆盖文本模型和图片模型，不需要分开充值。</li>
                <li>老客户端不传 <code>model</code> 时，仍然走默认文本模型，以保证兼容。</li>
            </ul>
            <div className="doc-callout">
                <strong>缓存读取价格怎么读</strong>
                <p>例如 <code>Input $0.99 / M · Cached $0.099 / M · Output $6.99 / M</code>，表示命中缓存读取的输入 token 会按目录里的 Cached 价格单独计费。</p>
            </div>
        </div>
    )
}

function ApiReference({ primaryTextModel, primaryImageModel }) {
    const textModelId = primaryTextModel?.id || 'opus'
    const imageModelId = primaryImageModel?.id || 'gpt-image-2'

    return (
        <div className="doc-section animate-fade-in">
            <h2>API 参考</h2>
            <p className="doc-intro">所有接口均兼容 OpenAI API 风格。当前推荐先从 <code>/v1/models</code> 拉取公开目录，再决定请求用哪个模型。</p>

            <h3>认证方式</h3>
            <pre className="code-block">{`Authorization: Bearer sk_cc_xxxxx`}</pre>

            <ul className="doc-list">
                <li>这里要求的是开发者 Key，不是控制台 session key。</li>
                <li>如果你把 session key 拿来请求 API，服务端会返回 <code>403</code>。</li>
                <li>控制台账号负责余额、日志和充值；开发者 Key 负责程序调用。</li>
            </ul>

            <h3>Claude Code 兼容入口</h3>
            <div className="endpoint-block">
                <span className="method get">GET</span>
                <code>/v1/models</code>
            </div>
            <div className="endpoint-block">
                <span className="method post">POST</span>
                <code>/v1/messages</code>
            </div>
            <div className="endpoint-block">
                <span className="method post">POST</span>
                <code>/v1/messages/count_tokens</code>
            </div>
            <ul className="doc-list">
                <li>官方 Claude Code 使用 Anthropic 风格接口，不走 OpenAI 的 <code>chat/completions</code> 或 <code>responses</code>。</li>
                <li>配置时把 <code>ANTHROPIC_BASE_URL</code> 指向站点根地址，例如 <code>{SITE}</code>，不要追加 <code>/v1</code>。</li>
                <li>认证变量是 <code>ANTHROPIC_AUTH_TOKEN</code>，值仍然是同一把 <code>sk_cc_</code> 开发者 Key。</li>
            </ul>

            <div className="doc-callout">
                <strong>线上入口以这里为准</strong>
                <p>当前对外使用的公开文档入口就是这个 ClawFather 站点本身。工作区里的内部文档和实验性 docs portal 不算正式入口。</p>
            </div>

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

            <h3>Balance</h3>
            <div className="endpoint-block">
                <span className="method get">GET</span>
                <code>/v1/balance</code>
            </div>

            <h3>Usage</h3>
            <div className="endpoint-block">
                <span className="method get">GET</span>
                <code>/v1/usage?limit=50&amp;offset=0</code>
            </div>

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

            <h3>Images: 多图异步图生图</h3>
            <div className="endpoint-block">
                <span className="method post">POST</span>
                <code>/v1/image-jobs/edits</code>
            </div>
            <pre className="code-block">{`curl ${SITE}/v1/image-jobs/edits \\
  -H "Authorization: Bearer sk_cc_xxxxx" \\
  -F "model=${imageModelId}" \\
  -F "prompt=Combine these references into one poster illustration" \\
  -F "n=1" \\
  -F "size=1024x1024" \\
  -F "image=@./ref-1.png" \\
  -F "image=@./ref-2.png" \\
  -F "image=@./ref-3.png"`}</pre>

            <div className="endpoint-block">
                <span className="method get">GET</span>
                <code>/v1/image-jobs/{'{job_id}'}</code>
            </div>
            <pre className="code-block">{`curl ${SITE}/v1/image-jobs/job_xxxxx \\
  -H "Authorization: Bearer sk_cc_xxxxx"`}</pre>

            <ul className="doc-list">
                <li>当前图片编辑分为两条公开契约：<code>1-2</code> 张输入图继续走同步 <code>/v1/images/edits</code>，<code>3-8</code> 张输入图改走异步 <code>/v1/image-jobs/edits</code>。</li>
                <li>如果你把 <code>3+</code> 张输入图直接发到 <code>/v1/images/edits</code>，接口会明确返回 <code>image_job_required</code>，而不是随机超时。</li>
                <li>图片模型当前输出候选数只支持 <code>n=1</code>。</li>
                <li>当前图片编辑不支持 <code>mask</code> 上传；如果传了掩码，会返回 <code>mask_not_supported</code>。</li>
                <li>如果平台侧图片运行时暂不可用，图片请求会返回配置错误，而不是偷偷回退到别的模型。</li>
            </ul>

            <h3>默认兼容规则</h3>
            <ul className="doc-list">
                <li>如果文本请求里省略 <code>model</code>，ClawFather 会保持默认文本模型的兼容行为。</li>
                <li>如果图片请求里省略 <code>model</code>，ClawFather 会自动选择默认图片模型 <code>gpt-image-2</code>。</li>
                <li>如果要用 Gemini 生图或 Gemini 图生图，请显式传入 <code>model: "gemini-image"</code>。</li>
                <li>显式指定某个模型后，如果该模型请求失败，不会偷偷回退到另一个模型。</li>
            </ul>

            <h3>错误码</h3>
            <table className="data-table">
                <thead>
                    <tr><th>状态码</th><th>含义</th><th>说明</th></tr>
                </thead>
                <tbody>
                    <tr><td>400</td><td>模型或参数错误</td><td>例如模型不存在、模型不支持该端点</td></tr>
                    <tr><td>400</td><td><code>image_candidate_count_not_supported</code></td><td>图片模型当前只支持 <code>n=1</code></td></tr>
                    <tr><td>400</td><td><code>image_job_required</code></td><td>同步图生图请求里传了 <code>3+</code> 张输入图，请改用 <code>/v1/image-jobs/edits</code></td></tr>
                    <tr><td>400</td><td><code>mask_not_supported</code></td><td>当前图片编辑不支持 <code>mask</code> 上传</td></tr>
                    <tr><td>401</td><td>认证失败</td><td>API Key 缺失或无效</td></tr>
                    <tr><td>402</td><td>余额不足</td><td>请充值后重试</td></tr>
                    <tr><td>403</td><td>禁止访问</td><td>Key 被禁用、用户被封禁，或使用了 session key 访问 API</td></tr>
                    <tr><td>429</td><td>请求过多</td><td>超出速率或额度限制</td></tr>
                    <tr><td>503</td><td>平台图片运行时暂不可用</td><td>请稍后重试或联系支持。</td></tr>
                </tbody>
            </table>

            <div className="doc-callout">
                <strong>公开接口先看这 4 个</strong>
                <p><code>/v1/models</code> 看目录，<code>/v1/balance</code> 看余额，<code>/v1/chat/completions</code> 或 <code>/v1/responses</code> 发文本请求，<code>/v1/usage</code> 查日志。第一次接入先把这 4 个跑通。</p>
            </div>
        </div>
    )
}

function CodeExamples({ primaryTextModel, primaryImageModel }) {
    const textModelId = primaryTextModel?.id || 'opus'
    const imageModelId = primaryImageModel?.id || 'gpt-image-2'

    return (
        <div className="doc-section animate-fade-in">
            <h2>代码示例</h2>
            <p className="doc-intro">Base URL 固定，切模型时优先改 <code>model</code>。</p>

            <div className="doc-callout">
                <strong>示例默认你已经有开发者 Key</strong>
                <p>没有开发者 Key 时，先回概览页生成，不要把控制台登录态直接塞进客户端。</p>
            </div>

            <h3>cURL（直连文本接口）</h3>
            <pre className="code-block">{`curl ${SITE}/v1/chat/completions \\
  -H "Authorization: Bearer sk_cc_xxxxx" \\
  -H "Content-Type: application/json" \\
  -d '{
    "model": "${textModelId}",
    "messages": [{"role": "user", "content": "Hello from ClawFather"}]
  }'`}</pre>

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
model_provider = "clawfather"
disable_response_storage = true
model_reasoning_effort = "high"
web_search = "live"
personality = "pragmatic"

[model_providers.clawfather]
name = "ClawFather"
base_url = "${SITE}/v1"
env_key = "CLAWFATHER_OPENAI_API_KEY"
wire_api = "responses"`}</pre>

            <h3>Claude Code</h3>
            <pre className="code-block">{`export ANTHROPIC_BASE_URL="${SITE}"
export ANTHROPIC_AUTH_TOKEN="sk_cc_xxxxx"
export ANTHROPIC_MODEL="claude-opus-4-7"
export ANTHROPIC_DEFAULT_OPUS_MODEL="claude-opus-4-7"
export ANTHROPIC_DEFAULT_SONNET_MODEL="claude-sonnet-4-6"
export ANTHROPIC_DEFAULT_HAIKU_MODEL="claude-haiku-4-5"

claude --model claude-opus-4-7`}</pre>
            <ul className="doc-list">
                <li>这里的 <code>ANTHROPIC_BASE_URL</code> 必须填站点根地址，不能带 <code>/v1</code>。</li>
                <li>常用文本模型可直接填写 <code>opus</code>、<code>sonnet</code> 或 <code>haiku</code>；需要兼容 Claude Code 默认模型名时，也可以填写 <code>claude-opus-4-7</code>、<code>claude-sonnet-4-6</code>、<code>claude-haiku-4-5</code>。</li>
                <li>如果之前用过 <code>/login</code> 托管登录，先执行一次 <code>/logout</code>，避免本地登录态和环境变量打架。</li>
            </ul>

            <h3>OpenClaw</h3>
            <pre className="code-block">{`{
  "models": {
    "providers": {
      "clawfather": {
        "baseUrl": "${SITE}/v1",
        "apiKey": "sk_cc_xxxxx",
        "api": "openai-completions",
        "models": [{"id": "${textModelId}", "contextWindow": 131072}]
      }
    },
    "defaults": {
      "provider": "clawfather",
      "model": "${textModelId}"
    }
  }
}`}</pre>

            <h3>Continue / Aider / 通用 OpenAI-compatible 客户端</h3>
            <div className="config-table">
                <div className="config-row">
                    <span className="config-label">Base URL</span>
                    <code>${SITE}/v1</code>
                </div>
                <div className="config-row">
                    <span className="config-label">API Key</span>
                    <code>sk_cc_xxxxx</code>
                </div>
                <div className="config-row">
                    <span className="config-label">Model</span>
                    <code>${textModelId}</code>
                </div>
            </div>
            <ul className="doc-list">
                <li>只要客户端允许自定义 OpenAI-compatible 的 <code>base_url</code>、<code>api_key</code> 和 <code>model</code>，就先按这 3 个值接。</li>
                <li>第一次接入建议先用文本模型跑通，再去试图片模型、工具调用或流式输出。</li>
            </ul>

            <h3>什么时候还要看模型官方文档？</h3>
            <ul className="doc-list">
                <li>模型能力、参数支持和生命周期仍以对应模型官方文档为准。</li>
                <li>当你遇到 function calling、图片参数或模型生命周期问题时，先查模型官方文档，再看 ClawFather 文档。</li>
                <li>简化理解：客户端只接 ClawFather，模型能力边界看对应官方文档。</li>
            </ul>
        </div>
    )
}
