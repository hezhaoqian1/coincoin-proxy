import { Link } from 'react-router-dom'
import { PRICING_PLANS } from '../api/client'
import { useAuth } from '../hooks/useAuth'
import './Landing.css'

export default function Landing() {
    const { isLoggedIn } = useAuth()
    const startTarget = isLoggedIn ? '/dashboard' : '/register'
    const purchaseTarget = isLoggedIn ? '/recharge' : '/register'
    const heroHighlights = [
        '一个入口统一接 GPT 文本、Gemini 文本和 Gemini 生图',
        '控制台账号、余额、请求日志、开发者 Key 全在一处管理',
        '兼容 Codex CLI、Continue、Aider、OpenClaw 等主流客户端',
    ]
    const featuredPlans = PRICING_PLANS.slice(0, 3)

    return (
        <div className="landing">
            {/* Hero */}
            <section className="hero">
                <div className="hero-bg">
                    <div className="hero-orb hero-orb-1"></div>
                    <div className="hero-orb hero-orb-2"></div>
                    <div className="hero-orb hero-orb-3"></div>
                    <div className="hero-grid"></div>
                </div>
                <div className="container hero-content">
                    <div className="hero-badge animate-fade-in">
                        <span className="hero-badge-dot"></span>
                        GPT + Gemini 文本与生图已上线
                    </div>
                    <h1 className="hero-title animate-fade-in-up">
                        一个入口接入<br />
                        <span className="hero-gradient">GPT 与 Gemini</span>
                    </h1>
                    <p className="hero-desc animate-fade-in-up" style={{ animationDelay: '100ms' }}>
                        OpenAI 兼容入口，统一管理模型、余额、日志与开发者 Key。
                        先进入控制台，再把配置直接复制到你的客户端里。
                    </p>
                    <div className="hero-proof animate-fade-in-up" style={{ animationDelay: '160ms' }}>
                        {heroHighlights.map((item) => (
                            <div key={item} className="hero-proof-item">
                                <span className="hero-proof-dot"></span>
                                <span>{item}</span>
                            </div>
                        ))}
                    </div>
                    <div className="hero-actions animate-fade-in-up" style={{ animationDelay: '200ms' }}>
                        <Link to={startTarget} className="btn btn-primary btn-lg">
                            {isLoggedIn ? '进入控制台' : '免费开始使用'}
                            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><path d="M5 12h14M12 5l7 7-7 7" /></svg>
                        </Link>
                        <Link to="/docs" className="btn btn-secondary btn-lg">
                            查看文档
                        </Link>
                    </div>
                    <div className="hero-stats animate-fade-in-up" style={{ animationDelay: '350ms' }}>
                        <div className="hero-stat">
                            <span className="hero-stat-value">99.9%</span>
                            <span className="hero-stat-label">在线率</span>
                        </div>
                        <div className="hero-stat-divider"></div>
                        <div className="hero-stat">
                            <span className="hero-stat-value">&lt;100ms</span>
                            <span className="hero-stat-label">平均延迟</span>
                        </div>
                        <div className="hero-stat-divider"></div>
                        <div className="hero-stat">
                            <span className="hero-stat-value">10K+</span>
                            <span className="hero-stat-label">开发者</span>
                        </div>
                    </div>
                </div>
            </section>

            {/* Features */}
            <section className="section" id="features">
                <div className="container">
                    <h2 className="section-title">为什么团队用 CoinCoin 做统一入口</h2>
                    <p className="section-subtitle">不是单纯的代理站，而是把控制台、计费和开发者接入合在一起的控制面</p>
                    <div className="features-grid features-grid-compact stagger-children">
                        <div className="feature-card glass-card animate-fade-in-up">
                            <div className="feature-icon" style={{ background: 'rgba(99,102,241,0.12)', color: 'var(--accent-indigo)' }}>
                                <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><circle cx="12" cy="12" r="10" /><path d="M2 12h20M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z" /></svg>
                            </div>
                            <h3>统一模型入口</h3>
                            <p>Base URL 不变，只通过 <code>model</code> 切换 GPT 文本、Gemini 文本和 Gemini 生图。</p>
                        </div>
                        <div className="feature-card glass-card animate-fade-in-up">
                            <div className="feature-icon" style={{ background: 'rgba(16,185,129,0.12)', color: 'var(--accent-emerald)' }}>
                                <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M12 2v20M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6" /></svg>
                            </div>
                            <h3>控制台即管理台</h3>
                            <p>余额、充值、请求日志和开发者 Key 都在一个控制台里完成，不再分散在文档和脚本里。</p>
                        </div>
                        <div className="feature-card glass-card animate-fade-in-up">
                            <div className="feature-icon" style={{ background: 'rgba(6,182,212,0.12)', color: 'var(--accent-cyan)' }}>
                                <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z" /></svg>
                            </div>
                            <h3>开发者友好</h3>
                            <p>兼容 Codex CLI、Continue、Aider、OpenClaw 等主流 OpenAI 风格客户端，配置路径尽量短。</p>
                        </div>
                    </div>
                </div>
            </section>

            {/* How it works */}
            <section className="section section-alt">
                <div className="container">
                    <h2 className="section-title">三步快速接入</h2>
                    <p className="section-subtitle">无需复杂配置，几分钟即可开始使用</p>
                    <div className="steps-grid stagger-children">
                        <div className="step-card animate-fade-in-up">
                            <div className="step-number">01</div>
                            <h3>创建控制台账号</h3>
                            <p>先注册并进入控制台，完成余额和权限管理</p>
                            <div className="step-code">
                                <code>dashboard.coincoin.ai</code>
                            </div>
                        </div>
                        <div className="step-connector">
                            <svg width="40" height="2" viewBox="0 0 40 2"><line x1="0" y1="1" x2="40" y2="1" stroke="var(--accent-indigo)" strokeWidth="2" strokeDasharray="6,4" /></svg>
                        </div>
                        <div className="step-card animate-fade-in-up">
                            <div className="step-number">02</div>
                            <h3>生成开发者 Key</h3>
                            <p>在仪表盘生成真正给客户端使用的 API Key</p>
                            <div className="step-code">
                                <code>sk_cc_xxxxxxxxxxxxx</code>
                            </div>
                        </div>
                        <div className="step-connector">
                            <svg width="40" height="2" viewBox="0 0 40 2"><line x1="0" y1="1" x2="40" y2="1" stroke="var(--accent-indigo)" strokeWidth="2" strokeDasharray="6,4" /></svg>
                        </div>
                        <div className="step-card animate-fade-in-up">
                            <div className="step-number">03</div>
                            <h3>配置并开始使用</h3>
                            <p>Base URL 不变，只改 model 就能切模型</p>
                            <div className="step-code">
                                <code>base_url = "your-domain/v1"</code>
                            </div>
                        </div>
                    </div>
                </div>
            </section>

            {/* Pricing */}
            <section className="section" id="pricing">
                <div className="container">
                    <h2 className="section-title">先把账户开通，再按量充值</h2>
                    <p className="section-subtitle">对大多数用户来说，真正需要做的只有两步：创建控制台账号，然后按你的使用规模充值</p>
                    <div className="pricing-grid stagger-children">
                        {featuredPlans.map((plan, i) => (
                            <div key={i} className={`pricing-card glass-card animate-fade-in-up ${plan.highlight ? 'pricing-highlight' : ''}`}>
                                {plan.badge && <div className="pricing-badge">{plan.badge}</div>}
                                <h3 className="pricing-name">{plan.name}</h3>
                                <div className="pricing-price">
                                    <span className="pricing-amount">{plan.price}</span>
                                    {plan.priceNote && <span className="pricing-note">{plan.priceNote}</span>}
                                </div>
                                <ul className="pricing-features">
                                    {plan.features.map((f, j) => (
                                        <li key={j}>
                                            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="var(--accent-emerald)" strokeWidth="2.5"><polyline points="20 6 9 17 4 12" /></svg>
                                            {f}
                                        </li>
                                    ))}
                                </ul>
                                <Link to={purchaseTarget} className={`btn ${plan.highlight ? 'btn-primary' : 'btn-secondary'}`} style={{ width: '100%' }}>
                                    {plan.price === '免费' ? (isLoggedIn ? '进入控制台' : '创建账号') : (isLoggedIn ? '去充值' : '创建账号后充值')}
                                </Link>
                            </div>
                        ))}
                    </div>
                </div>
            </section>

            {/* FAQ */}
            <section className="section section-alt" id="faq">
                <div className="container">
                    <h2 className="section-title">常见问题</h2>
                    <p className="section-subtitle">关于 CoinCoin 的一些常见疑问</p>
                    <div className="faq-list">
                        {[
                            { q: '支持哪些模型？', a: '当前支持默认 GPT 文本模型、多个 Gemini 文本 alias，以及 Gemini 生图模型。公开目录可通过 /v1/models 查询。' },
                            { q: '如何计费？', a: '文本模型按 Token 计费，图片模型按张数计费。所有费用统一从账户余额扣除。' },
                            { q: '支持哪些客户端？', a: '当前优先支持 Codex CLI、Continue、Aider、ChatBox 等主流 OpenAI 兼容客户端，OpenClaw 也可接入但建议走 openai-completions 模式。Gemini CLI 暂不作为一等公共接入方式。' },
                            { q: 'API Key 丢失怎么办？', a: '请联系管理员，我们可以为你生成新的 API Key。' },
                            { q: '余额用完会怎样？', a: '余额用完后请求会返回 HTTP 402 错误，充值后即可恢复使用。已有的余额不会过期。' },
                            { q: '支持哪些支付方式？', a: '目前支持支付宝。充值后余额实时到账。' },
                        ].map((item, i) => (
                            <details key={i} className="faq-item glass-card">
                                <summary className="faq-question">
                                    {item.q}
                                    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><polyline points="6 9 12 15 18 9" /></svg>
                                </summary>
                                <p className="faq-answer">{item.a}</p>
                            </details>
                        ))}
                    </div>
                </div>
            </section>

            {/* CTA */}
            <section className="section cta-section">
                <div className="container cta-content">
                    <h2 className="cta-title">准备好开始了吗？</h2>
                    <p className="cta-desc">{isLoggedIn ? '直接进入控制台，继续充值、配置和接入' : '注册即送测试额度，3 分钟完成接入'}</p>
                    <Link to={startTarget} className="btn btn-primary btn-lg">
                        {isLoggedIn ? '进入控制台' : '免费创建账号'}
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><path d="M5 12h14M12 5l7 7-7 7" /></svg>
                    </Link>
                </div>
            </section>
        </div>
    )
}
