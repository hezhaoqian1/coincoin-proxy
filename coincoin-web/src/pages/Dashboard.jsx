import { useState, useEffect } from 'react'
import { Link } from 'react-router-dom'
import { Line } from 'react-chartjs-2'
import { Chart as ChartJS, CategoryScale, LinearScale, PointElement, LineElement, Title, Tooltip, Legend, Filler } from 'chart.js'
import { MOCK_BALANCE, MOCK_USAGE, getBalance, getUsageLogs, getDailyUsage, getAnnouncements, activateKey, getReferralInfo, setGeneratedKey as storeGeneratedKey } from '../api/client'
import useOrderConfirm from '../hooks/useOrderConfirm'
import { useAuth } from '../hooks/useAuth'
import { usePublicModels } from '../hooks/usePublicModels'
import './Dashboard.css'

ChartJS.register(CategoryScale, LinearScale, PointElement, LineElement, Title, Tooltip, Legend, Filler)

function ReadinessCard({ authMode, username, hasDeveloperKey }) {
    const contentMap = {
        session_only: {
            tone: 'warning',
            eyebrow: '开发接入',
            title: '还差一步：生成开发者 API Key',
            description: `${username || '当前账户'} 已进入控制台。现在可以看余额、充值和用量；真正给 Codex CLI、Continue、Aider 或 cURL 用的开发者 API Key 还没有生成。`,
            checklist: [
                '控制台登录已完成',
                '开发者 API Key 尚未生成',
                '生成后即可直接复制到客户端配置',
            ],
            actions: [
                { href: '#developer-key', label: '去生成 API Key', style: 'btn btn-primary btn-sm' },
                { to: '/docs', label: '阅读接入文档', style: 'btn btn-secondary btn-sm' },
            ],
        },
        session_with_api: {
            tone: 'success',
            eyebrow: '开发接入',
            title: '控制台账号和开发者密钥都已准备好',
            description: `${username || '当前账户'} 已生成开发者 API Key。余额、日志和接入配置都可以在当前页面继续完成。`,
            checklist: [
                '控制台登录已完成',
                '开发者 API Key 已就绪',
                '可以直接复制配置片段接入客户端',
            ],
            actions: [
                { to: '/settings', label: '复制配置片段', style: 'btn btn-primary btn-sm' },
                { to: '/playground', label: '在线测试模型', style: 'btn btn-secondary btn-sm' },
            ],
        },
        api: {
            tone: 'info',
            eyebrow: '会话模式',
            title: '当前正在用开发者 API Key 直接登录',
            description: '这种方式适合验证开发者 Key 是否可用，但它不等同于控制台账号登录。如果你还要做账户管理，建议改用用户名密码登录控制台。',
            checklist: [
                '当前会话可直接调用 API',
                '不会自动拥有控制台管理能力',
                '需要站内管理时请改用用户名密码登录',
            ],
            actions: [
                { to: '/settings', label: '查看接入配置', style: 'btn btn-primary btn-sm' },
                { to: '/docs', label: '查看客户端示例', style: 'btn btn-secondary btn-sm' },
            ],
        },
        demo: {
            tone: 'muted',
            eyebrow: '演示模式',
            title: '你当前看到的是演示数据',
            description: '可以先熟悉余额、日志、模型和充值入口。真正接入客户端时，请注册控制台账号并生成自己的开发者 API Key。',
            checklist: [
                '当前内容仅用于体验界面',
                '不会分配真实开发者 API Key',
                '注册正式账号后才能接入客户端',
            ],
            actions: [
                { to: '/register', label: '创建正式账号', style: 'btn btn-primary btn-sm' },
                { to: '/docs', label: '先看文档', style: 'btn btn-secondary btn-sm' },
            ],
        },
    }
    const content = contentMap[authMode] || contentMap.api

    return (
        <div className={`readiness-card glass-card readiness-${content.tone} animate-fade-in-up`}>
            <div className="readiness-copy">
                <span className="readiness-eyebrow">{content.eyebrow}</span>
                <h2>{content.title}</h2>
                <p>{content.description}</p>
                <ul className="readiness-list">
                    {content.checklist.map((item) => (
                        <li key={item}>{item}</li>
                    ))}
                </ul>
                <div className="readiness-tags">
                    <span className="readiness-tag">{authMode === 'session_only' ? '当前为控制台会话' : '站内状态正常'}</span>
                    <span className="readiness-tag">{hasDeveloperKey ? '开发者 Key 已就绪' : '尚未生成开发者 Key'}</span>
                </div>
            </div>
            <div className="readiness-actions">
                {content.actions.map((action) => (
                    action.to ? (
                        <Link key={action.to + action.label} to={action.to} className={action.style}>
                            {action.label}
                        </Link>
                    ) : (
                        <a key={action.href + action.label} href={action.href} className={action.style}>
                            {action.label}
                        </a>
                    )
                ))}
            </div>
        </div>
    )
}

function KeyManagement({ copied, copy, username, generatedApiKey, authMode, effectiveApiKey }) {
    const [generatedKey, setGeneratedKey] = useState(generatedApiKey || '')
    const [showKey, setShowKey] = useState(false)
    const [generating, setGenerating] = useState(false)
    const [genError, setGenError] = useState('')

    useEffect(() => {
        setGeneratedKey(generatedApiKey || '')
    }, [generatedApiKey])

    const handleGenerate = async () => {
        if (!username) return
        setGenerating(true)
        setGenError('')
        try {
            const data = await activateKey(username)
            if (data.api_key) {
                setGeneratedKey(data.api_key)
                storeGeneratedKey(data.api_key)
                setShowKey(true)
            } else {
                setGenError('生成失败，请重试')
            }
        } catch {
            setGenError('网络错误，请重试')
        } finally {
            setGenerating(false)
        }
    }

    const maskedKey = generatedKey
        ? generatedKey.substring(0, 10) + '...' + generatedKey.substring(generatedKey.length - 4)
        : ''

    return (
        <div id="developer-key" className="quick-actions glass-card key-management-card animate-fade-in-up" style={{ animationDelay: '200ms' }}>
            <h3>开发者 Key 管理</h3>
            {authMode === 'demo' ? (
                <div className="key-panel-copy">
                    <p>
                        Demo 模式不会分配真实的开发者 API Key。想要接入客户端，请先注册控制台账号，
                        然后在仪表盘里生成你自己的开发者 Key。
                    </p>
                    <div className="action-links">
                        <Link to="/register" className="btn btn-primary btn-sm">创建正式账号</Link>
                        <Link to="/docs" className="btn btn-secondary btn-sm">查看接入步骤</Link>
                    </div>
                </div>
            ) : !username ? (
                <div className="key-panel-copy">
                    <p>
                        当前会话是通过开发者 API Key 直接登录的。你可以复制并继续使用这个 Key，
                        但如果要在站内重新生成或管理 Key，需要改用用户名密码登录控制台。
                    </p>
                    <div className="action-grid" style={{ marginTop: 'var(--space-md)' }}>
                        <div className="action-item" onClick={() => copy(effectiveApiKey, 'key')}>
                            <div className="action-icon">&#128273;</div>
                            <div>
                                <strong>当前开发者 Key</strong>
                                <code>{effectiveApiKey.substring(0, 12)}...</code>
                            </div>
                            <span className="action-btn">{copied === 'key' ? '&#10003; 已复制' : '复制'}</span>
                        </div>
                    </div>
                    <div className="action-links">
                        <Link to="/settings" className="btn btn-secondary btn-sm">查看配置片段</Link>
                    </div>
                </div>
            ) : generatedKey && !showKey ? (
                <div>
                    <p className="key-panel-copy">
                        你已经生成过开发者 API Key，可直接用于 Codex CLI、Continue、Aider 和其他 OpenAI 兼容客户端。
                        当前站内登录依然走控制台 session，两者职责不同。
                    </p>
                    <div className="action-grid">
                        <div className="action-item" onClick={() => copy(generatedKey, 'apikey')}>
                            <div className="action-icon">&#128273;</div>
                            <div>
                                <strong>开发者 API Key</strong>
                                <code>{maskedKey}</code>
                            </div>
                            <span className="action-btn">{copied === 'apikey' ? '&#10003; 已复制' : '复制'}</span>
                        </div>
                    </div>
                    <div style={{ marginTop: 'var(--space-md)', display: 'flex', gap: 'var(--space-sm)' }}>
                        <button className="btn btn-secondary btn-sm" onClick={handleGenerate} disabled={generating}>
                            {generating ? '生成中...' : '重新生成开发者 Key'}
                        </button>
                        <Link to="/settings" className="btn btn-ghost btn-sm">查看完整配置</Link>
                    </div>
                    {genError && <p style={{ color: 'var(--accent-rose)', fontSize: '0.85rem', marginTop: 'var(--space-sm)' }}>{genError}</p>}
                </div>
            ) : showKey ? (
                <div>
                    <div style={{
                        background: 'rgba(245, 158, 11, 0.1)',
                        border: '1px solid rgba(245, 158, 11, 0.2)',
                        borderRadius: 'var(--radius-sm)',
                        padding: 'var(--space-md)',
                        fontSize: '0.88rem',
                        color: 'var(--accent-amber)',
                        marginBottom: 'var(--space-md)'
                    }}>
                        请务必保存此开发者 API Key。完整值只会在重新生成后的这一刻明确展示。
                    </div>
                    <div className="key-display" style={{
                        display: 'flex', alignItems: 'center', gap: 'var(--space-sm)',
                        background: 'rgba(0,0,0,0.3)', border: '1px solid var(--border-color)',
                        borderRadius: 'var(--radius-md)', padding: 'var(--space-md)', marginBottom: 'var(--space-md)'
                    }}>
                        <code style={{ flex: 1, fontSize: '0.85rem', color: 'var(--accent-cyan)', wordBreak: 'break-all' }}>
                            {generatedKey}
                        </code>
                        <button className="btn btn-ghost btn-sm" onClick={() => copy(generatedKey, 'newkey')}>
                            {copied === 'newkey' ? '&#10003; 已复制' : '复制'}
                        </button>
                    </div>
                    <button className="btn btn-secondary btn-sm" onClick={() => setShowKey(false)}>
                        我已保存，隐藏完整 Key
                    </button>
                </div>
            ) : (
                <div>
                    <p className="key-panel-copy">
                        为你的控制台账户生成一个开发者 API Key，用于第三方客户端。
                        当前这次登录拿到的是 session key，只能访问 Dashboard、充值和设置页面，不能直接调用 API。
                    </p>
                    <button className="btn btn-primary btn-sm" onClick={handleGenerate} disabled={generating}>
                        {generating ? '生成中...' : '生成开发者 API Key'}
                    </button>
                    {genError && <p style={{ color: 'var(--accent-rose)', fontSize: '0.85rem', marginTop: 'var(--space-sm)' }}>{genError}</p>}
                </div>
            )}
        </div>
    )
}

export default function Dashboard() {
    const { defaultTextModel, defaultImageModel } = usePublicModels()
    const { authMode, effectiveApiKey, generatedApiKey, hasDeveloperKey, isDemo, username } = useAuth()
    const [balance, setBalance] = useState(null)
    const [usage, setUsage] = useState(null)
    const [dailyData, setDailyData] = useState(null)
    const [announcements, setAnnouncements] = useState([])
    const [dismissedAnns, setDismissedAnns] = useState(() => {
        try { return JSON.parse(localStorage.getItem('coincoin_dismissed_anns') || '[]') } catch { return [] }
    })
    const [copied, setCopied] = useState('')
    const [chartMode, setChartMode] = useState('cost')
    const [referral, setReferral] = useState(null)
    const [refCopied, setRefCopied] = useState(false)
    const { confirmResult: orderConfirmed, dismiss: dismissOrder } = useOrderConfirm()

    useEffect(() => {
        if (orderConfirmed) {
            // refresh balance after auto-confirm
            getBalance().then(setBalance).catch(() => {})
        }
    }, [orderConfirmed])

    useEffect(() => {
        async function load() {
            if (isDemo) {
                setBalance(MOCK_BALANCE)
                setUsage(MOCK_USAGE)
            } else {
                try {
                    const [b, u] = await Promise.all([getBalance(), getUsageLogs(20)])
                    setBalance(b)
                    setUsage(u)
                } catch {
                    setBalance(MOCK_BALANCE)
                    setUsage(MOCK_USAGE)
                }
                try { setDailyData(await getDailyUsage(7)) } catch { /* ignore */ }
                try { setAnnouncements(await getAnnouncements()) } catch { /* ignore */ }
                try { setReferral(await getReferralInfo()) } catch { /* ignore */ }
            }
        }
        load()
    }, [isDemo])

    const copy = (text, label) => {
        navigator.clipboard.writeText(text)
        setCopied(label)
        setTimeout(() => setCopied(''), 2000)
    }

    const dismissAnn = (id) => {
        const next = [...dismissedAnns, id]
        setDismissedAnns(next)
        localStorage.setItem('coincoin_dismissed_anns', JSON.stringify(next))
    }

    if (!balance) {
        return (
            <div className="page-wrapper">
                <div className="container">
                    <div className="loading-state">
                        <div className="loading-spinner"></div>
                        <p>加载中...</p>
                    </div>
                </div>
            </div>
        )
    }

    const activeAnns = announcements.filter(a => !dismissedAnns.includes(a.id))

    const todayStr = new Date().toISOString().slice(0, 10)
    const todayUsage = usage?.data?.filter(d => d.created_at?.startsWith(todayStr)) || []
    const todayCost = todayUsage.reduce((sum, d) => sum + d.cost_cents, 0) / 100
    const todayTokens = todayUsage.reduce((sum, d) => sum + (d.total_tokens || d.input_tokens + d.output_tokens), 0)
    const todayImages = todayUsage.reduce((sum, d) => sum + (d.image_count || 0), 0)
    const todayRequests = todayUsage.length

    const chartData = dailyData && dailyData.length > 0 ? {
        labels: dailyData.map(d => d.day.slice(5)),
        datasets: chartMode === 'cost' ? [{
            label: '花费 ($)',
            data: dailyData.map(d => d.cost_usd),
            borderColor: 'rgb(16, 185, 129)',
            backgroundColor: 'rgba(16, 185, 129, 0.1)',
            fill: true,
            tension: 0.3,
        }] : chartMode === 'tokens' ? [{
            label: 'Input Tokens',
            data: dailyData.map(d => d.input_tokens),
            borderColor: 'rgb(99, 102, 241)',
            backgroundColor: 'rgba(99, 102, 241, 0.1)',
            fill: true,
            tension: 0.3,
        }, {
            label: 'Output Tokens',
            data: dailyData.map(d => d.output_tokens),
            borderColor: 'rgb(245, 158, 11)',
            backgroundColor: 'rgba(245, 158, 11, 0.1)',
            fill: true,
            tension: 0.3,
        }] : [{
            label: '请求数',
            data: dailyData.map(d => d.requests_total),
            borderColor: 'rgb(6, 182, 212)',
            backgroundColor: 'rgba(6, 182, 212, 0.1)',
            fill: true,
            tension: 0.3,
        }]
    } : null

    const chartOptions = {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: chartMode === 'tokens' } },
        scales: {
            x: { grid: { display: false } },
            y: { beginAtZero: true, grid: { color: 'rgba(128,128,128,0.1)' } }
        }
    }

    return (
        <div className="page-wrapper dashboard">
            <div className="container">
                <div className="page-header">
                    <h1 className="page-title">仪表盘</h1>
                    <p className="page-desc">余额、调用记录、开发者接入状态都集中在这里</p>
                </div>

                {/* Announcements */}
                {activeAnns.map(a => (
                    <div key={a.id} className={`announcement-banner ann-${a.priority} animate-fade-in`}>
                        <div className="ann-content">
                            <strong>{a.title}</strong>
                            <span>{a.content}</span>
                        </div>
                        <button className="ann-close" onClick={() => dismissAnn(a.id)}>&times;</button>
                    </div>
                ))}

                {/* Low balance warning */}
                {balance.balance_usd < 0.10 && (
                    <div className="low-balance-banner critical animate-fade-in">
                        <span>&#9888; 余额即将耗尽 (${balance.balance_usd.toFixed(2)})，请立即充值以免服务中断</span>
                        <Link to="/recharge" className="btn btn-sm btn-primary">立即充值</Link>
                    </div>
                )}
                {balance.balance_usd >= 0.10 && balance.balance_usd < 1.00 && (
                    <div className="low-balance-banner warning animate-fade-in">
                        <span>&#9888; 余额不足 $1.00 (${balance.balance_usd.toFixed(2)})，建议及时充值</span>
                        <Link to="/recharge" className="btn btn-sm btn-secondary">去充值</Link>
                    </div>
                )}

                {/* Auto-confirmed order banner */}
                {orderConfirmed && (
                    <div className="low-balance-banner animate-fade-in" style={{ background: 'rgba(16,185,129,0.1)', borderColor: 'rgba(16,185,129,0.3)', color: 'var(--accent-emerald)' }}>
                        <span>&#10003; 充值到账！+${(orderConfirmed.added_cents / 100).toFixed(2)}，当前余额 ${orderConfirmed.new_balance_usd?.toFixed(2)}</span>
                        <button className="btn btn-sm btn-secondary" onClick={dismissOrder}>知道了</button>
                    </div>
                )}

                {/* Stats Cards */}
                <div className="stats-grid stagger-children">
                    <div className="stat-card glass-card animate-fade-in-up">
                        <div className="stat-icon" style={{ background: 'rgba(16,185,129,0.12)' }}>
                            <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="var(--accent-emerald)" strokeWidth="2"><path d="M12 2v20M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6" /></svg>
                        </div>
                        <div className="stat-info">
                            <span className="stat-label">账户余额</span>
                            <span className="stat-value">${balance.balance_usd.toFixed(2)}</span>
                            <span className="stat-sub">{balance.balance} 分</span>
                        </div>
                    </div>

                    <div className="stat-card glass-card animate-fade-in-up">
                        <div className="stat-icon" style={{ background: 'rgba(99,102,241,0.12)' }}>
                            <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="var(--accent-indigo)" strokeWidth="2"><path d="M22 12h-4l-3 9L9 3l-3 9H2" /></svg>
                        </div>
                        <div className="stat-info">
                            <span className="stat-label">已用 Token</span>
                            <span className="stat-value">{(balance.token_used).toLocaleString()}</span>
                            <span className="stat-sub">Input: {balance.input_tokens_used.toLocaleString()} &middot; Output: {balance.output_tokens_used.toLocaleString()}</span>
                        </div>
                    </div>

                    <div className="stat-card glass-card animate-fade-in-up">
                        <div className="stat-icon" style={{ background: 'rgba(245,158,11,0.12)' }}>
                            <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="var(--accent-amber)" strokeWidth="2"><circle cx="12" cy="12" r="10" /><polyline points="12 6 12 12 16 14" /></svg>
                        </div>
                        <div className="stat-info">
                            <span className="stat-label">今日消费</span>
                            <span className="stat-value">${todayCost.toFixed(2)}</span>
                            <span className="stat-sub">{todayRequests} 次请求 &middot; {todayTokens.toLocaleString()} Tokens &middot; {todayImages} Images</span>
                        </div>
                    </div>
                </div>

                <div className="developer-setup-grid">
                    <ReadinessCard authMode={authMode} username={username} hasDeveloperKey={hasDeveloperKey} />

                    <KeyManagement
                        copied={copied}
                        copy={copy}
                        username={username}
                        generatedApiKey={generatedApiKey}
                        authMode={authMode}
                        effectiveApiKey={effectiveApiKey}
                    />
                </div>

                {/* Trend Chart */}
                {chartData && (
                    <div className="trend-card glass-card animate-fade-in-up" style={{ animationDelay: '150ms' }}>
                        <div className="section-row">
                            <h3>近 7 天趋势</h3>
                            <div className="chart-tabs">
                                {[['cost', '花费'], ['tokens', 'Tokens'], ['requests', '请求']].map(([k, label]) => (
                                    <button key={k} className={`chart-tab ${chartMode === k ? 'active' : ''}`} onClick={() => setChartMode(k)}>{label}</button>
                                ))}
                            </div>
                        </div>
                        <div className="chart-container">
                            <Line data={chartData} options={chartOptions} />
                        </div>
                    </div>
                )}

                {/* Quick Links */}
                <div className="quick-actions glass-card animate-fade-in-up" style={{ animationDelay: '250ms' }}>
                    <h3>快速操作</h3>
                    <div className="action-grid">
                        <div className="action-item" onClick={() => copy(window.location.origin + '/v1', 'url')}>
                            <div className="action-icon">&#127760;</div>
                            <div>
                                <strong>复制 Base URL</strong>
                                <code>{window.location.origin}/v1</code>
                            </div>
                            <span className="action-btn">{copied === 'url' ? '&#10003; 已复制' : '复制'}</span>
                        </div>
                    </div>
                    <div className="action-links">
                        <Link to="/recharge" className="btn btn-primary btn-sm">&#128176; 充值</Link>
                        <Link to="/usage" className="btn btn-secondary btn-sm">&#128202; 查看详情</Link>
                        <Link to="/settings" className="btn btn-secondary btn-sm">&#128736; 接入配置</Link>
                        <Link to="/docs" className="btn btn-secondary btn-sm">&#128214; 接入文档</Link>
                        <Link to="/playground" className="btn btn-secondary btn-sm">&#9881; 在线测试</Link>
                    </div>
                </div>

                {/* Referral */}
                {referral && (
                    <div className="referral-card glass-card animate-fade-in-up" style={{ animationDelay: '275ms' }}>
                        <h3>邀请返佣</h3>
                        <p style={{ color: 'var(--text-secondary)', fontSize: '0.85rem', marginBottom: 'var(--space-md)' }}>
                            好友前 {referral.max_rewards_per_user || 3} 次充值你拿 <strong style={{ color: 'var(--accent-emerald)' }}>{Math.round(referral.commission_rate * 100)}%</strong> 佣金，好友首充额外得 <strong style={{ color: 'var(--accent-emerald)' }}>${referral.new_user_bonus_usd || 3}</strong>
                        </p>
                        <div className="referral-stats">
                            <div className="referral-stat">
                                <span className="stat-num">{referral.invited_count}</span>
                                <span className="stat-label">邀请人数</span>
                            </div>
                            <div className="referral-stat">
                                <span className="stat-num" style={{ color: 'var(--accent-emerald)' }}>${referral.total_reward_usd.toFixed(2)}</span>
                                <span className="stat-label">累计佣金</span>
                            </div>
                        </div>
                        <div className="referral-code-row">
                            <div className="referral-code-display">
                                <span style={{ color: 'var(--text-secondary)', fontSize: '0.8rem' }}>你的邀请码</span>
                                <code style={{ fontSize: '1.2rem', letterSpacing: '0.15em', fontWeight: 700 }}>{referral.referral_code}</code>
                            </div>
                            <button
                                className="btn btn-primary btn-sm"
                                onClick={() => {
                                    navigator.clipboard.writeText(`${window.location.origin}/register?ref=${referral.referral_code}`)
                                    setRefCopied(true)
                                    setTimeout(() => setRefCopied(false), 2000)
                                }}
                            >
                                {refCopied ? '已复制' : '复制邀请链接'}
                            </button>
                        </div>
                    </div>
                )}

                {/* Pricing Info */}
                <div className="pricing-info glass-card animate-fade-in-up" style={{ animationDelay: '300ms' }}>
                    <h3>当前价格</h3>
                    <div className="price-row">
                        <div className="price-item">
                            <span className="price-label">Input Token</span>
                            <span className="price-val">${balance.price_input_per_million} <small>/ 百万</small></span>
                        </div>
                        <div className="price-item">
                            <span className="price-label">Output Token</span>
                            <span className="price-val">${balance.price_output_per_million} <small>/ 百万</small></span>
                        </div>
                        <div className="price-item">
                            <span className="price-label">默认文本</span>
                            <span className="price-val model-tag">{defaultTextModel?.id || 'gpt-5.2-codex'}</span>
                        </div>
                        <div className="price-item">
                            <span className="price-label">默认图片</span>
                            <span className="price-val model-tag">{defaultImageModel?.id || 'gemini-image'}</span>
                        </div>
                    </div>
                </div>

                {/* Recent usage */}
                {usage && usage.data.length > 0 && (
                    <div className="recent-usage glass-card animate-fade-in-up" style={{ animationDelay: '400ms' }}>
                        <div className="section-row">
                            <h3>最近请求</h3>
                            <Link to="/usage" className="btn btn-ghost btn-sm">查看全部 &rarr;</Link>
                        </div>
                        <div className="table-wrapper">
                            <table className="data-table">
                                <thead>
                                    <tr>
                                        <th>时间</th>
                                        <th>端点</th>
                                        <th>模型</th>
                                        <th>计量</th>
                                        <th>花费</th>
                                        <th>耗时</th>
                                        <th>状态</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {usage.data.slice(0, 5).map((log, i) => (
                                        <tr key={i}>
                                            <td>{new Date(log.created_at).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })}</td>
                                            <td><code className="endpoint-tag">{log.endpoint}</code></td>
                                            <td><span className="model-tag-sm">{log.model}</span></td>
                                            <td>{log.usage_unit_type === 'images' ? `${log.image_count || log.usage_unit_count || 0} images` : `${(log.total_tokens || 0).toLocaleString()} tokens`}</td>
                                            <td className="cost-cell">${log.cost_usd.toFixed(2)}</td>
                                            <td>{(log.duration_ms / 1000).toFixed(1)}s</td>
                                            <td><span className={`badge ${log.status_code === 200 ? 'badge-success' : 'badge-error'}`}>{log.status_code}</span></td>
                                        </tr>
                                    ))}
                                </tbody>
                            </table>
                        </div>
                    </div>
                )}
            </div>
        </div>
    )
}
