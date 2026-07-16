import { useEffect, useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { getCachedInputPricePerMillion, hasModelPricingMultiplier } from '../api/client'
import AppShell from '../components/AppShell'
import { useAuth } from '../hooks/useAuth'
import { usePublicModels } from '../hooks/usePublicModels'
import './Docs.css'

const SITE = typeof window !== 'undefined' ? window.location.origin : ''
const CODEX_MODEL_ID = 'gpt-5.4'
const CLAUDE_DEFAULT_ALIAS = 'sonnet'
const CLAUDE_DEFAULT_MODEL_ID = 'claude-sonnet-4-6'
const CLAUDE_OPUS_OPTIONAL_MODEL_ID = 'claude-opus-4-8'
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
        intro: '看端点、认证方式和图片/视频接口边界。'
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
const MODEL_CATEGORY_KEYS = ['all', 'openai', 'xai', 'claude', 'gemini', 'image', 'video']
const MODEL_CATEGORY_META = {
    all: { label: '全部', icon: 'grid' },
    openai: { label: 'OpenAI', icon: 'openai' },
    xai: { label: 'xAI', icon: 'xai' },
    claude: { label: 'Anthropic', icon: 'anthropic' },
    gemini: { label: 'Gemini', icon: 'google' },
    image: { label: '图片', icon: 'image' },
    video: { label: '视频', icon: 'video' },
}

function CategoryIcon({ category, compact = false }) {
    const meta = MODEL_CATEGORY_META[category] || MODEL_CATEGORY_META.all
    const className = `model-category-icon model-category-icon-${meta.icon} ${compact ? 'compact' : ''}`
    if (meta.icon === 'openai') {
        return (
            <span className={className} aria-hidden="true">
                <svg viewBox="0 0 24 24" fill="currentColor">
                    <path d="M9.205 8.658v-2.26c0-.19.072-.333.238-.428l4.543-2.616c.619-.357 1.356-.523 2.117-.523 2.854 0 4.662 2.212 4.662 4.566 0 .167 0 .357-.024.547l-4.71-2.759a.797.797 0 00-.856 0l-5.97 3.473zm10.609 8.8V12.06c0-.333-.143-.57-.429-.737l-5.97-3.473 1.95-1.118a.433.433 0 01.476 0l4.543 2.617c1.309.76 2.189 2.378 2.189 3.948 0 1.808-1.07 3.473-2.76 4.163zM7.802 12.703l-1.95-1.142c-.167-.095-.239-.238-.239-.428V5.899c0-2.545 1.95-4.472 4.591-4.472 1 0 1.927.333 2.712.928L8.23 5.067c-.285.166-.428.404-.428.737v6.898zM12 15.128l-2.795-1.57v-3.33L12 8.658l2.795 1.57v3.33L12 15.128zm1.796 7.23c-1 0-1.927-.332-2.712-.927l4.686-2.712c.285-.166.428-.404.428-.737v-6.898l1.974 1.142c.167.095.238.238.238.428v5.233c0 2.545-1.974 4.472-4.614 4.472zm-5.637-5.303l-4.544-2.617c-1.308-.761-2.188-2.378-2.188-3.948A4.482 4.482 0 014.21 6.327v5.423c0 .333.143.571.428.738l5.947 3.449-1.95 1.118a.432.432 0 01-.476 0zm-.262 3.9c-2.688 0-4.662-2.021-4.662-4.519 0-.19.024-.38.047-.57l4.686 2.71c.286.167.571.167.856 0l5.97-3.448v2.26c0 .19-.07.333-.237.428l-4.543 2.616c-.619.357-1.356.523-2.117.523zm5.899 2.83a5.947 5.947 0 005.827-4.756C22.287 18.339 24 15.84 24 13.296c0-1.665-.713-3.282-1.998-4.448.119-.5.19-.999.19-1.498 0-3.401-2.759-5.947-5.946-5.947-.642 0-1.26.095-1.88.31A5.962 5.962 0 0010.205 0a5.947 5.947 0 00-5.827 4.757C1.713 5.447 0 7.945 0 10.49c0 1.666.713 3.283 1.998 4.448-.119.5-.19 1-.19 1.499 0 3.401 2.759 5.946 5.946 5.946.642 0 1.26-.095 1.88-.309a5.96 5.96 0 004.162 1.713z" />
                </svg>
            </span>
        )
    }
    if (meta.icon === 'anthropic') {
        return (
            <span className={className} aria-hidden="true">
                <svg viewBox="0 0 24 24" fill="currentColor">
                    <path d="M17.3041 3.541h-3.6718l6.696 16.918H24Zm-10.6082 0L0 20.459h3.7442l1.3693-3.5527h7.0052l1.3693 3.5528h3.7442L10.5363 3.5409Zm-.3712 10.2232 2.2914-5.9456 2.2914 5.9456Z" />
                </svg>
            </span>
        )
    }
    if (meta.icon === 'google') {
        return (
            <span className={className} aria-hidden="true">
                <svg viewBox="0 0 24 24" fill="currentColor">
                    <path d="M12.48 10.92v3.28h7.84c-.24 1.84-.853 3.187-1.787 4.133-1.147 1.147-2.933 2.4-6.053 2.4-4.827 0-8.6-3.893-8.6-8.72s3.773-8.72 8.6-8.72c2.6 0 4.507 1.027 5.907 2.347l2.307-2.307C18.747 1.44 16.133 0 12.48 0 5.867 0 .307 5.387.307 12s5.56 12 12.173 12c3.573 0 6.267-1.173 8.373-3.36 2.16-2.16 2.84-5.213 2.84-7.667 0-.76-.053-1.467-.173-2.053H12.48z" />
                </svg>
            </span>
        )
    }
    if (meta.icon === 'xai') {
        return (
            <span className={className} aria-hidden="true">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round">
                    <path d="M5 5l14 14M19 5L5 19" />
                </svg>
            </span>
        )
    }
    if (meta.icon === 'image') {
        return (
            <span className={className} aria-hidden="true">
                <svg viewBox="0 0 20 20" fill="none">
                    <rect x="3" y="4" width="14" height="12" rx="3" />
                    <circle cx="7.5" cy="8" r="1.4" />
                    <path d="M5.5 14l3.2-3.2 2.1 2.1 1.4-1.4 2.3 2.5" />
                </svg>
            </span>
        )
    }
    if (meta.icon === 'video') {
        return (
            <span className={className} aria-hidden="true">
                <svg viewBox="0 0 20 20" fill="none">
                    <rect x="3" y="5" width="10" height="10" rx="3" />
                    <path d="M13 8l4-2.2v8.4L13 12z" />
                </svg>
            </span>
        )
    }
    return (
        <span className={className} aria-hidden="true">
            <svg viewBox="0 0 20 20" fill="none">
                <path d="M4 4h5v5H4zM11 4h5v5h-5zM4 11h5v5H4zM11 11h5v5h-5z" />
            </svg>
        </span>
    )
}

function getModelCategory(model, type) {
    const id = model.id || ''
    if (type === 'video') return 'video'
    if (type === 'image') return 'image'
    if (id.startsWith('claude-') || ['opus', 'sonnet', 'haiku'].includes(id)) return 'claude'
    if (id.startsWith('grok-') || model.owned_by === 'xai') return 'xai'
    if (id.includes('gemini') || model.owned_by === 'google') return 'gemini'
    return 'openai'
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

function formatVideoPrice(cents) {
    const value = Number(cents || 0)
    if (!value) return '后台配置'
    return `$${(value / 100).toFixed(3)} / video`
}

function formatMultiplier(model) {
    if (!hasModelPricingMultiplier(model)) return '默认'
    const modelRatio = Number(model.coincoin_model_multiplier || 1).toFixed(2)
    const outputRatio = Number(model.coincoin_output_multiplier || 1).toFixed(2)
    const mediaRatio = Number(model.coincoin_video_multiplier || model.coincoin_image_multiplier || 1).toFixed(2)
    return `${modelRatio}x · out ${outputRatio}x · media ${mediaRatio}x`
}

export default function Docs() {
    const { isLoggedIn } = useAuth()
    const [searchParams, setSearchParams] = useSearchParams()
    const requestedTab = searchParams.get('tab')
    const [activeTab, setActiveTab] = useState(TAB_INDEX_BY_KEY[requestedTab] ?? 0)
    const { models, textModels, imageModels, videoModels, defaultTextModel, defaultImageModel, defaultVideoModel } = usePublicModels()
    const recommendedTextModel = textModels.find((model) => model.id === CODEX_MODEL_ID)
        || textModels.find((model) => model.id === 'gpt-5.5')
        || defaultTextModel
        || textModels[0]
        || models[0]
    const primaryImageModel = defaultImageModel || imageModels[0] || null
    const primaryVideoModel = defaultVideoModel || videoModels[0] || null
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
                        {activeTab === 0 && <QuickStart primaryTextModel={recommendedTextModel} primaryImageModel={primaryImageModel} />}
                        {activeTab === 1 && <ModelsAndPricing textModels={textModels} imageModels={imageModels} videoModels={videoModels} defaultTextModel={defaultTextModel} defaultImageModel={defaultImageModel} />}
                        {activeTab === 2 && <ApiReference primaryTextModel={recommendedTextModel} primaryImageModel={primaryImageModel} primaryVideoModel={primaryVideoModel} />}
                        {activeTab === 3 && <CodeExamples primaryTextModel={recommendedTextModel} primaryImageModel={primaryImageModel} primaryVideoModel={primaryVideoModel} />}
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
                            <ModelsAndPricing textModels={textModels} imageModels={imageModels} videoModels={videoModels} defaultTextModel={defaultTextModel} defaultImageModel={defaultImageModel} />
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
                            {activeTab === 0 && <QuickStart primaryTextModel={recommendedTextModel} primaryImageModel={primaryImageModel} />}
                            {activeTab === 1 && <ModelsAndPricing textModels={textModels} imageModels={imageModels} videoModels={videoModels} defaultTextModel={defaultTextModel} defaultImageModel={defaultImageModel} />}
                            {activeTab === 2 && <ApiReference primaryTextModel={recommendedTextModel} primaryImageModel={primaryImageModel} primaryVideoModel={primaryVideoModel} />}
                            {activeTab === 3 && <CodeExamples primaryTextModel={recommendedTextModel} primaryImageModel={primaryImageModel} primaryVideoModel={primaryVideoModel} />}
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
            title: 'Grok Build',
            tag: '已实测',
            desc: '官方 Grok Build CLI 通过 Responses 接入 CoinCoin。',
            bullets: ['模型固定为 grok-build', 'Base URL 使用统一 /v1', '推理、流式和函数工具回路已验证']
        },
        {
            title: 'OpenCode',
            tag: '已实测',
            desc: '本地 coding agent 工作流，已验证基础可用。',
            bullets: ['先看 OpenCode quickstart', '默认先用 clawfather/gpt-5.4', '需要 Claude 风格模型时再试 sonnet']
        },
        {
            title: 'Continue / Aider',
            tag: '常见客户端',
            desc: '只要支持 OpenAI-compatible 配置，基本都能按这套接。',
            bullets: ['填 Base URL + API Key + model', '默认先用 gpt-5.4', '接不上时先排查 key 类型']
        },
        {
            title: 'Claude Code',
            tag: '推荐',
            desc: 'Claude Code 走 Anthropic 兼容入口。',
            bullets: ['官方推荐用 ~/.claude/settings.json', 'ANTHROPIC_BASE_URL 填根域名', '默认先用 sonnet，重任务再试 claude-opus-4-8']
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
    const textModelId = primaryTextModel?.id || CODEX_MODEL_ID
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
                    <p><code>gpt-image-2</code> 单图编辑使用同步接口；Gemini 的 3-8 张参考图使用异步 <code>/v1/image-jobs/edits</code>。</p>
                    <p><Link to="/guides/images">打开图生图教程</Link></p>
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
            <pre className="code-block">{`model = "${CODEX_MODEL_ID}"
model_provider = "clawfather"
disable_response_storage = true
model_reasoning_effort = "high"
web_search = "live"
personality = "pragmatic"

[model_providers.clawfather]
name = "ClawFather"
base_url = "${SITE}/v1"
experimental_bearer_token = "sk_cc_xxxxx"
wire_api = "responses"`}</pre>

            <h3>Grok Build</h3>
            <p>Grok Build 必须使用 Responses 后端和 <code>grok-build</code> 公开模型。</p>
            <p><Link className="btn btn-secondary btn-sm" to="/guides/grok-build">打开 Grok Build 一键配置教程</Link></p>

            <h3>Claude Code</h3>
            <pre className="code-block">{`mkdir -p ~/.claude && cat > ~/.claude/settings.json <<'EOF'
{
  "$schema": "https://json.schemastore.org/claude-code-settings.json",
  "env": {
    "ANTHROPIC_BASE_URL": "${SITE}",
    "ANTHROPIC_AUTH_TOKEN": "sk_cc_xxxxx"
  }
}
EOF

claude`}</pre>

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
                        <td>官方 Claude CLI 直接走 Anthropic 兼容面，默认推荐 <code>sonnet</code>；需要更强模型时再显式切到 <code>claude-opus-4-8</code> 或 <code>opus</code>。</td>
                    </tr>
                    <tr>
                        <td>Codex CLI</td>
                        <td><span className="badge badge-success">一等支持</span></td>
                        <td><code>/v1 + responses</code></td>
                        <td>推荐的命令行接法，默认建议直接固定到 <code>gpt-5.4</code>。</td>
                    </tr>
                    <tr>
                        <td>Grok Build</td>
                        <td><span className="badge badge-success">已实测支持</span></td>
                        <td><code>/v1 + responses</code></td>
                        <td>官方 CLI 用 <code>grok-build</code> 公开别名，支持推理、流式输出和文件工具多轮回路。</td>
                    </tr>
                    <tr>
                        <td>OpenCode</td>
                        <td><span className="badge badge-success">已实测支持</span></td>
                        <td><code>/v1 + 自定义 provider</code></td>
                        <td>已实测通过 <code>opencode run</code>、模型发现和基础文件读取。默认推荐 <code>clawfather/gpt-5.4</code>。</td>
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
                <li>只需要把请求或客户端配置中的 <code>model</code> 改成目标模型，例如 <code>gpt-5.4</code>、<code>grok-build</code>、<code>sonnet</code>、<code>claude-opus-4-8</code>、<code>gemini-image</code>。</li>
                <li>Base URL 和 API Key 不需要改，仍然走同一个 ClawFather 入口。</li>
                <li>文本请求推荐走 <code>/v1/chat/completions</code> 或 <code>/v1/responses</code>，图片请求走 <code>/v1/images/generations</code> 或 <code>/v1/images/edits</code>，并使用 <code>{imageModelId}</code> 这类图片模型。</li>
                <li>图片请求统一走 ClawFather 公开入口，不需要终端用户配置额外服务。</li>
            </ul>
        </div>
    )
}

function ModelsAndPricing({ textModels, imageModels, videoModels, defaultTextModel, defaultImageModel }) {
    const { effectiveApiKey } = useAuth({ loadRecoverableKey: true })
    const [activeCategory, setActiveCategory] = useState('all')
    const [copied, setCopied] = useState('')
    const openaiBaseUrl = `${SITE}/v1`
    const snippetKey = effectiveApiKey || 'sk_cc_xxxxx'
    const firstRequestModelId = textModels.find((model) => model.id === CODEX_MODEL_ID)?.id
        || defaultTextModel?.id
        || textModels[0]?.id
        || CODEX_MODEL_ID
    const firstCurl = `curl ${openaiBaseUrl}/chat/completions \\
  -H "Authorization: Bearer ${snippetKey}" \\
  -H "Content-Type: application/json" \\
  -d '{"model":"${firstRequestModelId}","messages":[{"role":"user","content":"Reply with only: OK"}]}'`

    const modelRows = useMemo(() => {
        const rows = [
            ...textModels.map((model) => ({
                model,
                category: getModelCategory(model, 'text'),
                defaultFor: (model.coincoin_default_for || []).includes('text') ? '默认文本' : '',
                primaryPrice: formatUsdPerMillion(model.coincoin_price_input_per_million),
                outputPrice: formatUsdPerMillion(model.coincoin_price_output_per_million),
                cachedPrice: formatUsdPerMillion(getCachedInputPricePerMillion(model), 3),
            })),
            ...imageModels.map((model) => ({
                model,
                category: 'image',
                defaultFor: (model.coincoin_default_for || []).includes('image') ? '默认图片' : '',
                primaryPrice: formatImagePrice(model.coincoin_price_per_image_cents),
                outputPrice: '不适用',
                cachedPrice: '不适用',
            })),
            ...videoModels.map((model) => ({
                model,
                category: 'video',
                defaultFor: (model.coincoin_default_for || []).includes('video') ? '默认视频' : '',
                primaryPrice: formatVideoPrice(model.coincoin_price_per_video_cents),
                outputPrice: '不适用',
                cachedPrice: '不适用',
            })),
        ]
        return rows
    }, [textModels, imageModels, videoModels])

    const categories = MODEL_CATEGORY_KEYS.map((key) => ({
        key,
        label: MODEL_CATEGORY_META[key].label,
        count: key === 'all' ? modelRows.length : modelRows.filter((row) => row.category === key).length,
    }))

    const visibleRows = activeCategory === 'all'
        ? modelRows
        : modelRows.filter((row) => row.category === activeCategory)

    const copy = async (text, label) => {
        await navigator.clipboard.writeText(text)
        setCopied(label)
        setTimeout(() => setCopied(''), 1800)
    }

    return (
        <div className="doc-section animate-fade-in">
            <h2>可用模型</h2>
            <div className="endpoint-strip">
                <button onClick={() => copy(openaiBaseUrl, 'openai')}><span>OpenAI Base URL</span><code>{openaiBaseUrl}</code><strong>{copied === 'openai' ? '已复制' : '复制'}</strong></button>
                <button onClick={() => copy(firstCurl, 'curl')}><span>第一条请求</span><code>curl chat/completions</code><strong>{copied === 'curl' ? '已复制' : '复制'}</strong></button>
            </div>

            <div className="model-catalog-head">
                <h3>模型目录</h3>
                <div className="model-category-tabs" role="tablist" aria-label="模型分类">
                    {categories.map((category) => (
                        <button
                            key={category.key}
                            type="button"
                            className={`model-category-tab ${activeCategory === category.key ? 'active' : ''}`}
                            onClick={() => setActiveCategory(category.key)}
                            aria-selected={activeCategory === category.key}
                        >
                            <CategoryIcon category={category.key} />
                            <span>{category.label}</span>
                            <strong>{category.count}</strong>
                        </button>
                    ))}
                </div>
            </div>
            <div className="pricing-table-wrap">
                <table className="data-table pricing-table model-catalog-table">
                    <thead>
                        <tr>
                            <th>模型名称</th>
                            <th>分类</th>
                            <th>输入/媒体价格</th>
                            <th>输出价格</th>
                            <th>缓存读取</th>
                            <th>倍率</th>
                            <th>状态</th>
                        </tr>
                    </thead>
                    <tbody>
                        {visibleRows.map(({ model, category, defaultFor, primaryPrice, outputPrice, cachedPrice }) => {
                            return (
                                <tr key={model.id}>
                                    <td>
                                        <div className="model-name-cell">
                                            <code className="model-tag-sm">{model.id}</code>
                                            {defaultFor && <span className="inline-badge">{defaultFor}</span>}
                                        </div>
                                    </td>
                                    <td>
                                        <span className="model-category-pill">
                                            <CategoryIcon category={category} compact />
                                            {MODEL_CATEGORY_META[category].label}
                                        </span>
                                    </td>
                                    <td className="price-cell">{primaryPrice}</td>
                                    <td className={`price-cell ${outputPrice === '不适用' ? 'muted' : ''}`}>{outputPrice}</td>
                                    <td className={`price-cell ${cachedPrice === '不适用' ? 'muted' : ''}`}>{cachedPrice}</td>
                                    <td className="price-cell">
                                        <span>{formatMultiplier(model)}</span>
                                        {hasModelPricingMultiplier(model) && <small>基础 {formatUsdPerMillion(model.coincoin_base_price_input_per_million)}</small>}
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
                    <span className="inline-badge">Image</span>
                    <strong><code>{defaultImageModel?.id || imageModels[0]?.id || 'gpt-image-2'}</code></strong>
                    <p>文生图走 <code>/v1/images/generations</code>，请求里显式传入图片模型。</p>
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
                <li>文本模型按 Input / Cached Read / Output Token 计费；图片模型按图片张数计费；视频模型按任务次数计费。</li>
                <li>同一个账户余额同时覆盖文本模型、图片模型和视频模型，不需要分开充值。</li>
            </ul>
        </div>
    )
}

function ApiReference({ primaryTextModel, primaryImageModel, primaryVideoModel }) {
    const textModelId = primaryTextModel?.id || CODEX_MODEL_ID
    const imageModelId = primaryImageModel?.id || 'gpt-image-2'
    const videoModelId = primaryVideoModel?.id || 'seedance-v2-720p'

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
            <div className="doc-callout">
                <strong>慢请求会保持同步连接</strong>
                <p>图片上游较慢时，接口会定期发送 JSON 合法空白，避免连接在等待最终结果时触发 CDN 524。客户端应继续读取到完整 JSON；这不会缩短实际生图时间。</p>
                <p>首个保活字节发出后 HTTP 状态会固定为 <code>200</code>，晚到错误请读取最终 JSON 的 <code>error</code>。需要短请求或严格错误状态时使用 <code>/v1/image-jobs/generations</code>。</p>
            </div>

            <h3>Images: 异步生成</h3>
            <div className="endpoint-block">
                <span className="method post">POST</span>
                <code>/v1/image-jobs/generations</code>
            </div>
            <pre className="code-block">{`{
  "model": "${imageModelId}",
  "prompt": "A futuristic coin mascot in a glass city",
  "size": "1024x1024",
  "n": 1
}`}</pre>
            <p>创建成功返回 <code>202</code> 和任务 <code>id</code>，随后通过 <code>/v1/image-jobs/{'{job_id}'}</code> 查询。异步只缩短单次网络请求，不会加快上游生成。</p>

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
  -F "model=gemini-image" \\
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

            <h3>Videos: Seedance 生成</h3>
            <div className="endpoint-block">
                <span className="method post">POST</span>
                <code>/v1/videos/generations</code>
            </div>
            <pre className="code-block">{`{
  "model": "${videoModelId}",
  "prompt": "镜头缓慢推进，人物转身微笑，电影感光影",
  "params": {
    "ratio": "16:9",
    "images": ["https://images.unsplash.com/photo-1500530855697-b586d89ba3ee?w=1280&q=80"]
  }
}`}</pre>
            <pre className="code-block">{`// 创建成功返回异步任务，先用 id 查询状态。
{
  "id": "job_xxxxx",
  "object": "video.generation",
  "task_id": "job_xxxxx",
  "upstream_task_id": "task_xxxxx",
  "status": "queued",
  "model": "${videoModelId}",
  "charged_cents": 98,
  "refunded_cents": 0
}`}</pre>

            <div className="endpoint-block">
                <span className="method get">GET</span>
                <code>/v1/videos/generations/{'{job_id}'}</code>
            </div>
            <pre className="code-block">{`curl ${SITE}/v1/videos/generations/job_xxxxx \\
  -H "Authorization: Bearer sk_cc_xxxxx"`}</pre>
            <pre className="code-block">{`// status 变成 completed 后，从 output.url 取视频地址。
{
  "id": "job_xxxxx",
  "object": "video.generation",
  "status": "completed",
  "model": "${videoModelId}",
  "charged_cents": 98,
  "refunded_cents": 0,
  "output": {
    "type": "video",
    "url": "https://..."
  }
}`}</pre>

            <ul className="doc-list">
                <li><code>gpt-image-2</code> 单图图生图走同步 <code>/v1/images/edits</code>；Gemini 的 <code>3-8</code> 张输入图走异步 <code>/v1/image-jobs/edits</code>。</li>
                <li>如果你把 <code>3+</code> 张 Gemini 输入图直接发到 <code>/v1/images/edits</code>，接口会明确返回 <code>image_job_required</code>。</li>
                <li>图片模型当前输出候选数只支持 <code>n=1</code>。</li>
                <li>当前图片编辑不支持 <code>mask</code> 上传；如果传了掩码，会返回 <code>mask_not_supported</code>。</li>
                <li>如果平台侧图片运行时暂不可用，图片请求会返回配置错误，而不是偷偷回退到别的模型。</li>
                <li>视频生成是异步任务；创建成功后会先扣费，失败任务会按本地记录退款一次。</li>
                <li>视频参考图请使用可直接访问的 <code>200 image/*</code> 直链，避免跳转、防盗链或需要登录的地址。</li>
                <li>Seedance 纯文本视频请求会被拒绝；普通模型不能传 <code>reference_video</code> 或 <code>reference_audio</code>，需要时使用 <code>-video</code> 模型。</li>
            </ul>

            <h3>错误码</h3>
            <table className="data-table">
                <thead>
                    <tr><th>状态码</th><th>含义</th><th>说明</th></tr>
                </thead>
                <tbody>
                    <tr><td>400</td><td>模型或参数错误</td><td>例如模型不存在、模型不支持该端点</td></tr>
                    <tr><td>400</td><td><code>image_candidate_count_not_supported</code></td><td>图片模型当前只支持 <code>n=1</code></td></tr>
                    <tr><td>400</td><td><code>image_job_required</code></td><td>同步 Gemini 图生图请求里传了 <code>3+</code> 张输入图，或超过同步等待预算，请改用 <code>/v1/image-jobs/edits</code></td></tr>
                    <tr><td>400</td><td><code>mask_not_supported</code></td><td>当前图片编辑不支持 <code>mask</code> 上传</td></tr>
                    <tr><td>400</td><td><code>missing_reference_media</code></td><td>Seedance 视频请求缺少图片、首帧、首尾帧或多模态参考</td></tr>
                    <tr><td>400</td><td><code>video_reference_requires_video_model</code></td><td>视频/音频参考只能使用带 <code>-video</code> 后缀的 Seedance 模型</td></tr>
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

function CodeExamples({ primaryTextModel, primaryImageModel, primaryVideoModel }) {
    const textModelId = primaryTextModel?.id || CODEX_MODEL_ID
    const imageModelId = primaryImageModel?.id || 'gpt-image-2'
    const videoModelId = primaryVideoModel?.id || 'seedance-v2-720p'

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

            <h3>JavaScript (fetch, Seedance 视频任务)</h3>
            <pre className="code-block">{`const createRes = await fetch('${SITE}/v1/videos/generations', {
  method: 'POST',
  headers: {
    'Authorization': 'Bearer sk_cc_xxxxx',
    'Content-Type': 'application/json'
  },
  body: JSON.stringify({
    model: '${videoModelId}',
    prompt: '镜头缓慢推进，人物转身微笑，电影感光影',
    params: {
      ratio: '16:9',
      images: ['https://images.unsplash.com/photo-1500530855697-b586d89ba3ee?w=1280&q=80']
    }
  })
});

const job = await createRes.json();
let result = job;

while (!['completed', 'failed'].includes(result.status)) {
  await new Promise(resolve => setTimeout(resolve, 10000));
  const resultRes = await fetch('${SITE}/v1/videos/generations/' + job.id, {
    headers: { Authorization: 'Bearer sk_cc_xxxxx' }
  });
  result = await resultRes.json();
}

console.log(result.output?.url || result);`}</pre>

            <h3>Codex CLI</h3>
            <pre className="code-block">{`model = "${CODEX_MODEL_ID}"
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

            <h3>Grok Build</h3>
            <pre className="code-block">{`[models]
default = "grok-build"

[model.grok-build]
model = "grok-build"
base_url = "${SITE}/v1"
api_key = "sk_cc_xxxxx"
api_backend = "responses"
context_window = 500000`}</pre>
            <p><Link to="/guides/grok-build">查看完整安装、备份、配置和工具回路验证教程</Link></p>

            <h3>Claude Code</h3>
            <pre className="code-block">{`mkdir -p ~/.claude && cat > ~/.claude/settings.json <<'EOF'
{
  "$schema": "https://json.schemastore.org/claude-code-settings.json",
  "env": {
    "ANTHROPIC_BASE_URL": "${SITE}",
    "ANTHROPIC_AUTH_TOKEN": "sk_cc_xxxxx"
  }
}
EOF

claude`}</pre>
            <ul className="doc-list">
                <li>官方用户级配置文件路径是 <code>~/.claude/settings.json</code>；Windows 对应 <code>%USERPROFILE%\.claude\settings.json</code>。</li>
                <li>这里的 <code>ANTHROPIC_BASE_URL</code> 必须填站点根地址，不能带 <code>/v1</code>。</li>
                <li>默认模型交给 Claude Code 自己选择；通常会走系统默认的 sonnet，不需要额外指定。</li>
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
