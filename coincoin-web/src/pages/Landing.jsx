import { Link } from 'react-router-dom'
import { PRICING_PLANS } from '../api/client'
import { useAuth } from '../hooks/useAuth'
import './Landing.css'

const CLIENTS = [
    'Claude Code',
    'Codex CLI',
    'Continue',
    'Aider',
    'OpenClaw',
    'cURL / SDK',
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
        model: 'claude-opus-4-8',
        tone: 'cyan',
    },
    {
        client: 'Images',
        endpoint: '/v1/images',
        model: 'gpt-image-2 · gemini-image',
        tone: 'amber',
    },
]

const HERO_STATS = [
    { label: '余额', value: '统一扣费' },
    { label: '日志', value: '逐条可查' },
    { label: '密钥', value: '独立管理' },
]

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
                                <strong>一把 Key，多类能力入口</strong>
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

                    </div>
                </div>
            </section>

            <section className="landing-plans-section" aria-label="套餐">
                <div className="container">
                    <div className="landing-plans-head">
                        <h2>套餐</h2>
                    </div>
                    <div className="landing-plans-grid">
                        {PRICING_PLANS.map((plan) => (
                            <Link
                                key={plan.id}
                                to="/recharge"
                                className={`landing-plan-card ${plan.highlight ? 'is-highlight' : ''}`}
                            >
                                <div className="landing-plan-top">
                                    <strong>{plan.name}</strong>
                                    {plan.badge ? <span>{plan.badge}</span> : null}
                                </div>
                                <div className="landing-plan-price">
                                    <b>{plan.price}</b>
                                    <small>{plan.priceNote}</small>
                                </div>
                                <div className="landing-plan-balance">{plan.balanceLabel}</div>
                            </Link>
                        ))}
                    </div>
                </div>
            </section>
        </div>
    )
}
