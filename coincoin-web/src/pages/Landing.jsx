import { Link } from 'react-router-dom'
import { PRICING_PLANS } from '../api/client'
import { useAuth } from '../hooks/useAuth'
import './Landing.css'

const ACCESS_STEPS = [
    {
        title: '注册控制台账号',
        body: '先进入控制台，拿到余额、日志和账户管理入口。',
    },
    {
        title: '生成开发者 Key',
        body: 'Codex CLI、SDK、cURL 和第三方客户端统一使用这把开发者 Key。',
    },
    {
        title: '复制配置开始用',
        body: 'Base URL 固定为同一个 /v1。平时主要改 model，不要手改内部链路。',
    },
]

const CLIENTS = [
    'Codex CLI',
    'Continue',
    'Aider',
    'OpenClaw',
    'cURL / SDK',
]

const ENTRY_POINTS = [
    {
        title: '统一入口',
        detail: '一个 OpenAI 兼容地址，覆盖 GPT 文本、Gemini 文本和 Gemini 生图。',
    },
    {
        title: '控制台优先',
        detail: '充值、请求日志、开发者 Key 和接入配置都在一处完成。',
    },
    {
        title: '接入路径短',
        detail: '拿到 Key 后直接复制配置片段，不需要维护多套脚本。',
    },
]

function PricingPreview({ isLoggedIn }) {
    const plans = PRICING_PLANS.slice(0, 3)

    return (
        <section className="landing-band">
            <div className="container landing-pricing">
                <div className="landing-section-head">
                    <div>
                        <span className="landing-eyebrow">充值</span>
                        <h2>先开通账户，再按量使用</h2>
                    </div>
                    <p>常用档位放在首页。更多套餐和自定义充值放到充值页处理。</p>
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
                        先看接入文档
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
                        <span className="landing-kicker">OpenAI Compatible Relay</span>
                        <h1>一个入口接 GPT、Gemini 和生图</h1>
                        <p className="landing-summary">
                            这是一个给开发者用的中转站控制台。先注册控制台账号，再生成开发者 Key，
                            然后把配置直接复制到你的 CLI、SDK 或客户端里。
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
                                <strong>控制台 {'->'} 开发者 Key {'->'} /v1</strong>
                            </div>
                            <span className="landing-console-status">推荐流程</span>
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
                            <span className="landing-code-label">最短配置</span>
                            <pre>{`base_url = "your-domain/v1"
api_key = "sk_cc_xxxxx"
model = "gpt-5.2-codex"`}</pre>
                        </div>
                    </div>
                </div>
            </section>

            <section className="landing-band">
                <div className="container">
                    <div className="landing-section-head">
                        <div>
                            <span className="landing-eyebrow">控制台结构</span>
                            <h2>首页给入口，控制台管接入</h2>
                        </div>
                        <p>接入、计费、日志和密钥管理都收在一个工作流里。</p>
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
                            <h2>三步就够了</h2>
                        </div>
                        <p>先注册，再生成 Key，最后复制配置。</p>
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
