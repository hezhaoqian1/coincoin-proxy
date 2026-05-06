import { Link } from 'react-router-dom'
import { PRICING_PLANS } from '../api/client'
import { useAuth } from '../hooks/useAuth'
import './Landing.css'

const ACCESS_STEPS = [
    {
        title: '创建控制台账号',
        body: '账户、余额、请求日志和密钥都从控制台统一管理。',
    },
    {
        title: '生成开发者 Key',
        body: '程序调用使用独立开发者 Key，不和网页登录态混用。',
    },
    {
        title: '查看接入指南',
        body: 'OpenAI 兼容客户端走 /v1，Claude Code 走根地址，平时主要替换 model。',
    },
]

const CLIENTS = [
    'Claude Code',
    'Codex CLI',
    'Continue',
    'Aider',
    'OpenClaw',
    'cURL / SDK',
]

const ENTRY_POINTS = [
    {
        title: '一把开发者 Key',
        detail: '同一把 Key 可用于 OpenAI 兼容客户端、Claude Code、CLI、SDK 和图片接口。',
    },
    {
        title: '统一管理调用',
        detail: '余额、请求日志、开发者 Key 和充值记录都在同一个控制台里。',
    },
    {
        title: '清楚的接入路径',
        detail: 'OpenAI 兼容客户端用 /v1，Claude Code 用根地址，按文档复制配置即可开始。',
    },
]

const HERO_LANES = [
    {
        client: 'Codex / SDK',
        endpoint: '/v1',
        model: 'opus · gpt-5.5',
        tone: 'indigo',
    },
    {
        client: 'Claude Code',
        endpoint: 'root',
        model: 'claude-opus-4-7',
        tone: 'cyan',
    },
    {
        client: 'Images',
        endpoint: '/v1/images',
        model: 'gemini-image',
        tone: 'amber',
    },
]

const HERO_STATS = [
    { label: '余额', value: '统一扣费' },
    { label: '日志', value: '逐条可查' },
    { label: '密钥', value: '独立管理' },
]

const COMMAND_SNIPPET = `# OpenAI-compatible
base_url = "https://your-domain/v1"
api_key = "sk_cc_xxxxx"
model = "opus"

# Claude Code
ANTHROPIC_BASE_URL = "https://your-domain"
ANTHROPIC_AUTH_TOKEN = "sk_cc_xxxxx"
model = "claude-opus-4-7"`

const FAQS = [
    {
        question: '这是 OpenAI 兼容接口吗？',
        answer: '是。常见 SDK、脚本和多数支持 OpenAI 协议的客户端都可以直接替换 Base URL 和 API Key 来接入。',
    },
    {
        question: 'Claude Code 怎么接？',
        answer: 'Claude Code 走根地址，不走 /v1。接入文档里提供了可复制的环境变量配置。',
    },
    {
        question: '余额和计费怎么处理？',
        answer: '充值后按模型用量扣费，文本和图片共用同一个账户余额。控制台会展示余额、请求日志和开发者 Key。',
    },
]

function PricingPreview({ isLoggedIn }) {
    const featuredPlans = [PRICING_PLANS[0], PRICING_PLANS[2], PRICING_PLANS[5]]

    return (
        <section className="landing-band landing-pricing-band">
            <div className="container landing-pricing">
                <div className="landing-section-head">
                    <div>
                        <span className="landing-eyebrow">Pricing</span>
                        <h2>充值后按量扣费</h2>
                    </div>
                    <p>文本和图片共用一个余额，实际调用按模型计费。</p>
                </div>
                <div className="landing-pricing-grid">
                    {featuredPlans.map((plan) => (
                        <div key={plan.name} className={`landing-pricing-card ${plan.highlight ? 'is-highlight' : ''}`}>
                            <div className="landing-pricing-top">
                                <div>
                                    <strong>{plan.name}</strong>
                                    <span>{plan.balanceLabel}</span>
                                </div>
                                {plan.badge ? <span className="landing-plan-badge">{plan.badge}</span> : null}
                            </div>
                            <div className="landing-pricing-price">{plan.price}</div>
                            <ul className="landing-pricing-list">
                                {plan.features.slice(0, 3).map((feature) => (
                                    <li key={feature}>{feature}</li>
                                ))}
                            </ul>
                        </div>
                    ))}
                </div>
                <div className="landing-inline-actions">
                    <Link to="/recharge" className="btn btn-primary">
                        {isLoggedIn ? '去充值' : '查看充值页'}
                    </Link>
                    <Link to="/docs" className="btn btn-secondary">
                        查看接入文档
                    </Link>
                </div>
            </div>
        </section>
    )
}

export default function Landing() {
    const { isLoggedIn } = useAuth()
    const startTarget = isLoggedIn ? '/dashboard' : '/register'

    return (
        <div className="landing-shell">
            <section className="landing-hero">
                <div className="landing-hero-noise" />
                <div className="container">
                    <div className="landing-hero-grid">
                        <div className="landing-copy landing-copy-centered">
                            <div className="landing-announcement">
                                <span className="landing-announcement-dot" />
                                <span>统一 Key，统一计费，统一控制台</span>
                            </div>
                            <span className="landing-kicker">ClawFather API</span>
                            <h1>
                                <span className="landing-title-brand">CoinCoin.ai</span>
                                <span className="landing-title-main">打开 AI 时代的模型网关</span>
                            </h1>
                            <p className="landing-summary">
                                不用在多个平台之间来回切换。生成开发者 Key 后，把配置复制到
                                Codex、Claude Code、SDK 或常用客户端里，就能开始调用文本和图片接口。
                            </p>

                            <div className="landing-inline-actions">
                                <Link to={startTarget} className="btn btn-primary btn-lg">
                                    {isLoggedIn ? '进入控制台' : '开始接入'}
                                </Link>
                                <Link to="/docs" className="btn btn-secondary btn-lg">
                                    查看文档
                                </Link>
                            </div>

                            <div className="landing-client-row">
                                <span className="landing-client-label">常见接法</span>
                                <div className="landing-client-list">
                                    {CLIENTS.map((client) => (
                                        <span key={client} className="landing-client-chip">{client}</span>
                                    ))}
                                </div>
                            </div>
                        </div>

                        <div className="landing-scroll-hint" aria-hidden="true">
                            <span />
                        </div>
                    </div>
                </div>
            </section>

            <section className="landing-console-showcase" aria-label="ClawFather routing overview">
                <div className="container">
                    <div className="landing-command-center">
                        <div className="landing-orbit landing-orbit-one" />
                        <div className="landing-orbit landing-orbit-two" />
                        <div className="landing-panel-head">
                            <div>
                                <span className="landing-console-label">Routing Console</span>
                                <strong>一把 Key，三类接入路径</strong>
                            </div>
                            <span className="landing-console-status">Live</span>
                        </div>

                        <div className="landing-routing-surface">
                            <div className="landing-key-strip">
                                <div>
                                    <span>Developer Key</span>
                                    <strong>sk_cc_••••••••••</strong>
                                </div>
                                <span className="landing-key-badge">ready</span>
                            </div>

                            <div className="landing-flow-map" aria-hidden="true">
                                <span className="landing-flow-line landing-flow-line-one" />
                                <span className="landing-flow-line landing-flow-line-two" />
                                <span className="landing-flow-line landing-flow-line-three" />
                            </div>

                            <div className="landing-lane-list">
                                {HERO_LANES.map((lane, index) => (
                                    <div
                                        key={lane.client}
                                        className={`landing-lane landing-lane-${lane.tone}`}
                                        style={{ '--lane-index': index }}
                                    >
                                        <div className="landing-lane-pulse" />
                                        <div>
                                            <strong>{lane.client}</strong>
                                            <span>{lane.model}</span>
                                        </div>
                                        <code>{lane.endpoint}</code>
                                    </div>
                                ))}
                            </div>

                            <div className="landing-stat-grid">
                                {HERO_STATS.map((stat) => (
                                    <div key={stat.label} className="landing-stat">
                                        <span>{stat.label}</span>
                                        <strong>{stat.value}</strong>
                                    </div>
                                ))}
                            </div>
                        </div>

                        <div className="landing-endpoint-note">
                            <span>OpenAI-compatible 用 <code>/v1</code></span>
                            <span>Claude Code 用根地址</span>
                        </div>
                    </div>
                </div>
            </section>

            <section className="landing-band">
                <div className="container">
                    <div className="landing-section-head">
                        <div>
                            <span className="landing-eyebrow">Platform</span>
                            <h2>从拿到 Key 到发出请求，路径保持清楚</h2>
                        </div>
                        <p>先创建账号并生成开发者 Key，再按客户端选择 Base URL 和模型名；余额、日志和密钥都在控制台管理。</p>
                    </div>
                    <div className="landing-feature-grid">
                        {ENTRY_POINTS.map((item) => (
                            <div key={item.title} className="landing-feature-card">
                                <span className="landing-feature-mark" />
                                <strong>{item.title}</strong>
                                <p>{item.detail}</p>
                            </div>
                        ))}
                    </div>
                </div>
            </section>

            <section className="landing-band landing-band-alt">
                <div className="container landing-setup-layout">
                    <div className="landing-section-head landing-section-head-compact">
                        <div>
                            <span className="landing-eyebrow">Quick Start</span>
                            <h2>三步完成接入</h2>
                        </div>
                        <p>少讲概念，直接交付开发者最常走的路径。</p>
                    </div>
                    <div className="landing-step-grid">
                        {ACCESS_STEPS.map((step, index) => (
                            <div key={step.title} className="landing-step-card">
                                <span className="landing-step-number">0{index + 1}</span>
                                <strong>{step.title}</strong>
                                <p>{step.body}</p>
                            </div>
                        ))}
                    </div>
                    <div className="landing-code-card landing-code-card-wide">
                        <span className="landing-code-label">Recommended Setup</span>
                        <pre>{COMMAND_SNIPPET}</pre>
                    </div>
                </div>
            </section>

            <section className="landing-band" id="faq">
                <div className="container">
                    <div className="landing-section-head">
                        <div>
                            <span className="landing-eyebrow">FAQ</span>
                            <h2>开发者最常问的三个问题</h2>
                        </div>
                        <p>先确认协议、Claude Code 接法和计费方式，再开始接入。</p>
                    </div>
                    <div className="landing-faq-grid">
                        {FAQS.map((item, index) => (
                            <article key={item.question} className="landing-faq-card">
                                <span className="landing-faq-index">0{index + 1}</span>
                                <strong>{item.question}</strong>
                                <p>{item.answer}</p>
                            </article>
                        ))}
                    </div>
                </div>
            </section>

            <PricingPreview isLoggedIn={isLoggedIn} />
        </div>
    )
}
