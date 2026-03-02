import { useState, useEffect } from 'react'
import { Link } from 'react-router-dom'
import { Line } from 'react-chartjs-2'
import { Chart as ChartJS, CategoryScale, LinearScale, PointElement, LineElement, Title, Tooltip, Legend, Filler } from 'chart.js'
import { MOCK_BALANCE, MOCK_USAGE, getApiKey, getBalance, getUsageLogs, getDailyUsage, getAnnouncements, getUsername, activateKey, getReferralInfo } from '../api/client'
import './Dashboard.css'

ChartJS.register(CategoryScale, LinearScale, PointElement, LineElement, Title, Tooltip, Legend, Filler)

function KeyManagement({ copied, copy }) {
    const username = getUsername()
    const [generatedKey, setGeneratedKey] = useState(() => localStorage.getItem('coincoin_generated_key') || '')
    const [showKey, setShowKey] = useState(false)
    const [generating, setGenerating] = useState(false)
    const [genError, setGenError] = useState('')

    const handleGenerate = async () => {
        if (!username) return
        setGenerating(true)
        setGenError('')
        try {
            const data = await activateKey(username)
            if (data.api_key) {
                setGeneratedKey(data.api_key)
                localStorage.setItem('coincoin_generated_key', data.api_key)
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
        <div className="quick-actions glass-card animate-fade-in-up" style={{ animationDelay: '200ms' }}>
            <h3>API Key 管理</h3>
            {!username ? (
                <div style={{ color: 'var(--text-secondary)', fontSize: '0.9rem' }}>
                    当前使用 API Key 直接登录。如需管理 Key，请使用用户名密码注册/登录。
                    <div className="action-grid" style={{ marginTop: 'var(--space-md)' }}>
                        <div className="action-item" onClick={() => copy(getApiKey(), 'key')}>
                            <div className="action-icon">&#128273;</div>
                            <div>
                                <strong>当前 Key</strong>
                                <code>{getApiKey().substring(0, 12)}...</code>
                            </div>
                            <span className="action-btn">{copied === 'key' ? '&#10003; 已复制' : '复制'}</span>
                        </div>
                    </div>
                </div>
            ) : generatedKey && !showKey ? (
                <div>
                    <p style={{ fontSize: '0.9rem', color: 'var(--text-secondary)', marginBottom: 'var(--space-md)' }}>
                        你已生成过 API Key，用于第三方客户端（Codex CLI、Continue 等）。
                    </p>
                    <div className="action-grid">
                        <div className="action-item" onClick={() => copy(generatedKey, 'apikey')}>
                            <div className="action-icon">&#128273;</div>
                            <div>
                                <strong>API Key</strong>
                                <code>{maskedKey}</code>
                            </div>
                            <span className="action-btn">{copied === 'apikey' ? '&#10003; 已复制' : '复制'}</span>
                        </div>
                    </div>
                    <div style={{ marginTop: 'var(--space-md)', display: 'flex', gap: 'var(--space-sm)' }}>
                        <button className="btn btn-secondary btn-sm" onClick={handleGenerate} disabled={generating}>
                            {generating ? '生成中...' : '重新生成'}
                        </button>
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
                        请务必保存此 API Key，它只会显示一次！
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
                        我已保存，隐藏 Key
                    </button>
                </div>
            ) : (
                <div>
                    <p style={{ fontSize: '0.9rem', color: 'var(--text-secondary)', marginBottom: 'var(--space-md)' }}>
                        生成一个 API Key 用于第三方客户端（Codex CLI、Continue、Aider 等）。
                        当前登录使用的 session key 只能访问 Dashboard，不能调用 API。
                    </p>
                    <button className="btn btn-primary btn-sm" onClick={handleGenerate} disabled={generating}>
                        {generating ? '生成中...' : '生成 API Key'}
                    </button>
                    {genError && <p style={{ color: 'var(--accent-rose)', fontSize: '0.85rem', marginTop: 'var(--space-sm)' }}>{genError}</p>}
                </div>
            )}
        </div>
    )
}

export default function Dashboard() {
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
    const isDemo = getApiKey() === 'sk_cc_demo_key'

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
                    <p className="page-desc">你的 API 使用概览</p>
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
                            <span className="stat-sub">{todayRequests} 次请求 &middot; {todayTokens.toLocaleString()} Tokens</span>
                        </div>
                    </div>
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

                {/* API Key Management */}
                <KeyManagement copied={copied} copy={copy} />

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
                            <span className="price-label">模型</span>
                            <span className="price-val model-tag">gpt-5.2-codex</span>
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
                                        <th>Input</th>
                                        <th>Output</th>
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
                                            <td>{log.input_tokens.toLocaleString()}</td>
                                            <td>{log.output_tokens.toLocaleString()}</td>
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
