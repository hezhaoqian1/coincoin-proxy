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
        title: '统一入口',
        detail: '一个 OpenAI 兼容地址，覆盖文本、图片和常见开发工具接入。',
    },
    {
        title: '控制台管理',
        detail: '充值、请求日志和开发者 Key 都在同一个后台里。',
    },
    {
        title: '低心智负担',
        detail: '拿到 Key 后直接替换 Base URL 和模型名，不需要维护多套线路。',
    },
]

const PROVIDERS = [
    { name: 'Text', vendor: 'compatible' },
    { name: 'Claude', vendor: 'Anthropic' },
    { name: 'Image', vendor: 'generation' },
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
        answer: 'Claude Code 走根地址，不走 /v1。欢迎页里已经给了对应配置片段，控制台里也会提供可复制的接入说明。',
    },
    {
        question: '余额和计费怎么处理？',
        answer: '公开页展示充值入口和套餐说明，实际调用时由控制台统一管理余额、请求日志和开发者 Key。',
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
                    <p>文本和图片共用一个余额，公开页展示套餐，实际调用按模型计费。</p>
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
                        <div className="landing-copy">
                            <div className="landing-announcement">
                                <span className="landing-announcement-dot" />
                                <span>统一入口，统一计费，统一控制台</span>
                            </div>
                            <span className="landing-kicker">ClawFather API</span>
                            <h1>把文本、图片和 Claude Code 收进同一个开发者入口</h1>
                            <p className="landing-summary">
                                不用在多个平台之间来回切换。登录控制台、生成开发者 Key，再把一套配置复制到
                                CLI、SDK 和常用客户端里，就能开始调用。
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

                        <div className="landing-hero-stack">
                            <div className="landing-console-card">
                                <div className="landing-console-head">
                                    <div>
                                        <span className="landing-console-label">Access Path</span>
                                        <strong>控制台 {'->'} 开发者 Key {'->'} 你的 IDE / 应用</strong>
                                    </div>
                                    <span className="landing-console-status">推荐路径</span>
                                </div>

                                <div className="landing-bridge-card">
                                    <div className="landing-provider-column">
                                        {PROVIDERS.map((provider) => (
                                            <div key={provider.name} className="landing-provider-node">
                                                <strong>{provider.name}</strong>
                                                <span>{provider.vendor}</span>
                                            </div>
                                        ))}
                                    </div>
                                    <div className="landing-bridge-center">
                                        <div className="landing-bridge-line" />
                                        <div className="landing-bridge-hub">CF</div>
                                        <div className="landing-bridge-line" />
                                    </div>
                                    <div className="landing-user-node">
                                        <strong>Your Stack</strong>
                                        <span>CLI / SDK / App</span>
                                    </div>
                                </div>

                                <div className="landing-console-body">
                                    {ACCESS_STEPS.map((step, index) => (
                                        <div key={step.title} className="landing-console-step">
                                            <span className="landing-step-index">0{index + 1}</span>
                                            <div>
                                                <strong>{step.title}</strong>
                                                <p>{step.body}</p>
                                            </div>
                                        </div>
                                    ))}
                                </div>
                            </div>

                            <div className="landing-code-card landing-code-card-wide">
                                <span className="landing-code-label">Recommended Setup</span>
                                <pre>{COMMAND_SNIPPET}</pre>
                            </div>
                        </div>
                    </div>
                </div>
            </section>

            <section className="landing-band">
                <div className="container">
                    <div className="landing-section-head">
                        <div>
                            <span className="landing-eyebrow">Platform</span>
                            <h2>公开页负责说明，控制台负责操作</h2>
                        </div>
                        <p>欢迎页负责建立信任和说明接入方式，真正的充值、日志、密钥管理在控制台里完成。</p>
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
                </div>
            </section>

            <section className="landing-band" id="faq">
                <div className="container">
                    <div className="landing-section-head">
                        <div>
                            <span className="landing-eyebrow">FAQ</span>
                            <h2>开发者最常问的三个问题</h2>
                        </div>
                        <p>把最影响接入决策的问题提前回答掉，减少第一次打开页面时的信息摩擦。</p>
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
