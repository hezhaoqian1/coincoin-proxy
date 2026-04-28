import { Link } from 'react-router-dom'
import { PRICING_PLANS } from '../api/client'
import { useAuth } from '../hooks/useAuth'
import './Landing.css'

const ACCESS_STEPS = [
    {
        title: '创建控制台账号',
        body: '账户、余额、日志和密钥都从控制台管理。',
    },
    {
        title: '生成开发者 Key',
        body: '程序调用单独使用开发者 Key，不用网页登录态。',
    },
    {
        title: '复制配置',
        body: 'OpenAI 客户端走 /v1，Claude Code 走根地址。平时主要改 model。',
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
        detail: '一个 OpenAI 兼容地址，接文本和图片请求。',
    },
    {
        title: '控制台管理',
        detail: '充值、请求日志、开发者 Key 和接入配置都在同一个后台里。',
    },
    {
        title: '接入简单',
        detail: '拿到 Key 后直接复制配置，不用维护多套地址。',
    },
]

function PricingPreview({ isLoggedIn }) {
    const plans = PRICING_PLANS

    return (
        <section className="landing-band">
            <div className="container landing-pricing">
                <div className="landing-section-head">
                    <div>
                        <span className="landing-eyebrow">充值</span>
                        <h2>充值后按量扣费</h2>
                    </div>
                    <p>文本和图片请求共用一个余额。</p>
                </div>
                <div className="landing-pricing-grid">
                    {plans.map((plan) => (
                        <div key={plan.name} className={`landing-pricing-card ${plan.highlight ? 'is-highlight' : ''}`}>
                            <div className="landing-pricing-top">
                                <div>
                                    <strong>{plan.name}</strong>
                                    <span>{plan.balanceLabel}</span>
                                </div>
                                {plan.badge && <span className="landing-plan-badge">{plan.badge}</span>}
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
                <div className="container landing-hero-grid">
                    <div className="landing-copy">
                        <span className="landing-kicker">ClawFather</span>
                        <h1>ClawFather：一个入口，接 GPT、Gemini 和生图</h1>
                        <p className="landing-summary">
                            面向开发者的统一入口。
                            登录控制台、生成开发者 Key，然后把配置复制到 CLI、SDK 或常用客户端。
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

                    <div className="landing-console-card">
                        <div className="landing-console-head">
                            <div>
                                <span className="landing-console-label">接入路径</span>
                                <strong>控制台 {'->'} 开发者 Key {'->'} OpenAI / Claude Code</strong>
                            </div>
                            <span className="landing-console-status">常用路径</span>
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
                        <div className="landing-code-card">
                            <span className="landing-code-label">示例配置</span>
                            <pre>{`# OpenAI-compatible
base_url = "https://your-domain/v1"
api_key = "sk_cc_xxxxx"
model = "gpt-5.5"

# Claude Code
ANTHROPIC_BASE_URL = "https://your-domain"
ANTHROPIC_AUTH_TOKEN = "sk_cc_xxxxx"
model = "claude-opus-4-7"`}</pre>
                        </div>
                    </div>
                </div>
            </section>

            <section className="landing-band">
                <div className="container">
                    <div className="landing-section-head">
                        <div>
                            <span className="landing-eyebrow">控制台结构</span>
                            <h2>公开页负责说明，控制台负责操作</h2>
                        </div>
                        <p>接入、计费、日志和密钥管理放在同一个工作台里。</p>
                    </div>
                    <div className="landing-feature-grid">
                        {ENTRY_POINTS.map((item) => (
                            <div key={item.title} className="landing-feature-card">
                                <strong>{item.title}</strong>
                                <p>{item.detail}</p>
                            </div>
                        ))}
                    </div>
                </div>
            </section>

            <section className="landing-band landing-band-alt">
                <div className="container">
                    <div className="landing-section-head">
                        <div>
                            <span className="landing-eyebrow">开始使用</span>
                            <h2>三步完成接入</h2>
                        </div>
                        <p>登录、生成 Key、复制配置。</p>
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

            <PricingPreview isLoggedIn={isLoggedIn} />
        </div>
    )
}
