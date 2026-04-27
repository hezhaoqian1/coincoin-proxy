import { useState, useEffect } from 'react'
import { Link } from 'react-router-dom'
import { Line } from 'react-chartjs-2'
import { Chart as ChartJS, CategoryScale, LinearScale, PointElement, LineElement, Title, Tooltip, Legend, Filler } from 'chart.js'
import { MOCK_BALANCE, MOCK_USAGE, getBalance, getUsageLogs, getDailyUsage, getAnnouncements, activateKey, getReferralInfo, getStationApplication, applyForStation, setGeneratedKey as storeGeneratedKey } from '../api/client'
import useOrderConfirm from '../hooks/useOrderConfirm'
import { useAuth } from '../hooks/useAuth'
import { usePublicModels } from '../hooks/usePublicModels'
import './Dashboard.css'

ChartJS.register(CategoryScale, LinearScale, PointElement, LineElement, Title, Tooltip, Legend, Filler)

function ReadinessCard({ authMode, username, hasDeveloperKey }) {
    const contentMap = {
        session_only: {
            tone: 'warning',
            eyebrow: '下一步',
            title: '先生成开发者 API Key',
            description: `${username || '当前账户'} 已经登录控制台。还差一把开发者 Key，生成后就可以直接复制到 CLI、SDK 和客户端。`,
            statusItems: [
                { label: '登录方式', value: '控制台账号' },
                { label: 'API 调用', value: '尚未开通' },
                { label: '下一步', value: '生成开发者 Key' },
            ],
            actions: [
                { href: '#developer-key', label: '去生成 API Key', style: 'btn btn-primary btn-sm' },
                { to: '/settings', label: '打开接入配置', style: 'btn btn-secondary btn-sm' },
            ],
        },
        session_with_api: {
            tone: 'success',
            eyebrow: '已就绪',
            title: '账户和开发者 Key 都已准备好',
            description: `${username || '当前账户'} 已经可以直接接入。常用下一步通常只有两件事：复制配置，或者发一条真实请求测试。`,
            statusItems: [
                { label: '登录方式', value: '控制台账号' },
                { label: 'API 调用', value: '可直接请求' },
                { label: '常用动作', value: '复制配置 / 发请求' },
            ],
            actions: [
                { to: '/settings', label: '复制配置片段', style: 'btn btn-primary btn-sm' },
                { to: '/playground', label: '发起测试请求', style: 'btn btn-secondary btn-sm' },
            ],
        },
        api: {
            tone: 'info',
            eyebrow: '当前状态',
            title: '你正在用开发者 API Key 直登',
            description: '这种模式可以直接测试调用和复制配置。需要账户管理、充值或重新生成密钥时，再切回控制台账号登录。',
            statusItems: [
                { label: '登录方式', value: '开发者 Key 直登' },
                { label: 'API 调用', value: '可直接请求' },
                { label: '账户管理', value: '回控制台处理' },
            ],
            actions: [
                { to: '/settings', label: '查看接入配置', style: 'btn btn-primary btn-sm' },
                { to: '/playground', label: '开始测试', style: 'btn btn-secondary btn-sm' },
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
                <div className="readiness-matrix">
                    {content.statusItems.map((item) => (
                        <div key={item.label} className="readiness-metric">
                            <span className="readiness-metric-label">{item.label}</span>
                            <strong>{item.value}</strong>
                        </div>
                    ))}
                </div>
                <div className="readiness-tags">
                    <span className="readiness-tag">{authMode === 'session_only' ? '控制台会话' : '当前可继续接入'}</span>
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
            {!username ? (
                <div className="key-panel-copy">
                    <p>
                        当前会话使用的是开发者 API Key。可以继续复制和使用它；
                        需要重新生成、轮换或做账户管理时，再回到控制台账号登录。
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
                        你已经生成过开发者 API Key。平时直接复制这个 Key 去接入即可，
                        不需要每次回到这里重新理解一遍登录方式。
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
                    <div className="key-warning-box">
                        <span className="key-warning-eyebrow">请立即保存</span>
                        <p>这是重新生成后的完整开发者 API Key。完整值只会显示这一次，保存后再继续配置客户端。</p>
                    </div>
                    <div className="key-secret-panel">
                        <div className="key-secret-meta">
                            <span className="meta-pill">开发者 API Key</span>
                            <span className="meta-pill">仅本次明文展示</span>
                        </div>
                        <code className="key-secret-value">{generatedKey}</code>
                        <div className="key-secret-actions">
                            <button className="btn btn-primary btn-sm" onClick={() => copy(generatedKey, 'newkey')}>
                                {copied === 'newkey' ? '&#10003; 已复制' : '复制完整 Key'}
                            </button>
                            <button className="btn btn-ghost btn-sm" onClick={() => setShowKey(false)}>
                                我已保存，隐藏完整 Key
                            </button>
                        </div>
                    </div>
                </div>
            ) : (
                <div>
                    <p className="key-panel-copy">
                        给当前控制台账户生成一把开发者 API Key。生成后就能直接接 SDK、CLI 和第三方客户端。
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

function StationCard({ stationState, onSubmitted }) {
    const [submitting, setSubmitting] = useState(false)
    const [error, setError] = useState('')
    const [form, setForm] = useState({
        station_name: '',
        contact_handle: '',
        traffic_source: '',
        audience_note: '',
        settlement_payee_name: '',
        settlement_payee_account: '',
        settlement_qr_url: '',
    })

    const app = stationState?.application
    const station = stationState?.station

    const handleChange = (key, value) => {
        setForm((prev) => ({ ...prev, [key]: value }))
    }

    const handleSubmit = async (e) => {
        e.preventDefault()
        setSubmitting(true)
        setError('')
        try {
            await applyForStation({
                station_name: form.station_name,
                contact_handle: form.contact_handle,
                traffic_source: form.traffic_source,
                audience_note: form.audience_note,
                settlement_method: 'alipay_manual',
                settlement_payee_name: form.settlement_payee_name,
                settlement_payee_account: form.settlement_payee_account,
                settlement_qr_url: form.settlement_qr_url,
            })
            onSubmitted?.()
        } catch (err) {
            setError(err.message || '申请失败')
        } finally {
            setSubmitting(false)
        }
    }

    if (station) {
        return (
            <div className="station-card glass-card animate-fade-in-up" style={{ animationDelay: '275ms' }}>
                <div className="station-card-header">
                    <div>
                        <h3>站长中心</h3>
                        <p className="station-card-desc">你的站长资格已开通。后续我们会在这里继续接入下游用户、分润和人工结算。</p>
                    </div>
                    <span className={`badge ${station.status === 'active' ? 'badge-success' : 'badge-warning'}`}>
                        {station.status === 'active' ? '已开通' : station.status}
                    </span>
                </div>
                <div className="station-summary-grid">
                    <div className="station-summary-item">
                        <span className="station-summary-label">站点名称</span>
                        <strong>{station.display_name}</strong>
                    </div>
                    <div className="station-summary-item">
                        <span className="station-summary-label">站点标识</span>
                        <code>{station.slug}</code>
                    </div>
                    <div className="station-summary-item">
                        <span className="station-summary-label">结算方式</span>
                        <strong>{station.settlement_method === 'alipay_manual' ? '支付宝人工打款' : station.settlement_method}</strong>
                    </div>
                    <div className="station-summary-item">
                        <span className="station-summary-label">收款账户</span>
                        <strong>{station.settlement_payee_account || '待补充'}</strong>
                    </div>
                </div>
            </div>
        )
    }

    if (app) {
        return (
            <div className="station-card glass-card animate-fade-in-up" style={{ animationDelay: '275ms' }}>
                <div className="station-card-header">
                    <div>
                        <h3>站长申请</h3>
                        <p className="station-card-desc">申请已经提交。当前先走审核制，避免影响现有用户链路和支付流程。</p>
                    </div>
                    <span className={`badge ${app.status === 'pending' ? 'badge-warning' : app.status === 'approved' ? 'badge-success' : 'badge-error'}`}>
                        {app.status === 'pending' ? '审核中' : app.status === 'approved' ? '已通过' : '已驳回'}
                    </span>
                </div>
                <div className="station-summary-grid">
                    <div className="station-summary-item">
                        <span className="station-summary-label">申请站点</span>
                        <strong>{app.station_name}</strong>
                    </div>
                    <div className="station-summary-item">
                        <span className="station-summary-label">联系方式</span>
                        <strong>{app.contact_handle || '未填写'}</strong>
                    </div>
                    <div className="station-summary-item station-summary-item-wide">
                        <span className="station-summary-label">流量来源 / 受众说明</span>
                        <p>{app.traffic_source || '未填写流量来源'}</p>
                        <p>{app.audience_note}</p>
                    </div>
                    {app.review_note && (
                        <div className="station-summary-item station-summary-item-wide">
                            <span className="station-summary-label">审核备注</span>
                            <p>{app.review_note}</p>
                        </div>
                    )}
                </div>
            </div>
        )
    }

    return (
        <div className="station-card glass-card animate-fade-in-up" style={{ animationDelay: '275ms' }}>
            <div className="station-card-header">
                <div>
                    <h3>申请成为站长</h3>
                    <p className="station-card-desc">想做自己的站长，就先提交这份申请。第一期只做审核制开通，不会影响你当前的使用和支付体验。</p>
                </div>
                <span className="station-badge">站长内测</span>
            </div>
            <form className="station-form" onSubmit={handleSubmit}>
                <div className="station-form-grid">
                    <label className="station-field">
                        <span>站点名称</span>
                        <input value={form.station_name} onChange={(e) => handleChange('station_name', e.target.value)} placeholder="例如：AI 工具分发站" required />
                    </label>
                    <label className="station-field">
                        <span>联系方式</span>
                        <input value={form.contact_handle} onChange={(e) => handleChange('contact_handle', e.target.value)} placeholder="微信 / TG / 邮箱" />
                    </label>
                    <label className="station-field station-field-wide">
                        <span>流量来源</span>
                        <input value={form.traffic_source} onChange={(e) => handleChange('traffic_source', e.target.value)} placeholder="公众号、社群、私域、B 站、客户资源等" />
                    </label>
                    <label className="station-field station-field-wide">
                        <span>受众说明</span>
                        <textarea value={form.audience_note} onChange={(e) => handleChange('audience_note', e.target.value)} placeholder="说清楚你准备服务谁、预估规模和你的运营方式" rows={4} required />
                    </label>
                    <label className="station-field">
                        <span>支付宝姓名</span>
                        <input value={form.settlement_payee_name} onChange={(e) => handleChange('settlement_payee_name', e.target.value)} placeholder="人工打款收款人" />
                    </label>
                    <label className="station-field">
                        <span>支付宝账号</span>
                        <input value={form.settlement_payee_account} onChange={(e) => handleChange('settlement_payee_account', e.target.value)} placeholder="手机号 / 邮箱 / UID" />
                    </label>
                    <label className="station-field station-field-wide">
                        <span>收款码图片地址</span>
                        <input value={form.settlement_qr_url} onChange={(e) => handleChange('settlement_qr_url', e.target.value)} placeholder="先填可访问的图片 URL，后续再做上传" />
                    </label>
                </div>
                {error && <p className="station-error">{error}</p>}
                <div className="station-actions">
                    <button className="btn btn-primary btn-sm" type="submit" disabled={submitting}>
                        {submitting ? '提交中...' : '提交站长申请'}
                    </button>
                    <span className="station-hint">现阶段为审核制。通过后会开通站长资格和人工结算信息。</span>
                </div>
            </form>
        </div>
    )
}

export default function Dashboard() {
    const { defaultTextModel, defaultImageModel } = usePublicModels()
    const { authMode, effectiveApiKey, generatedApiKey, hasDeveloperKey, username } = useAuth()
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
    const [stationState, setStationState] = useState(null)
    const { confirmResult: orderConfirmed, dismiss: dismissOrder } = useOrderConfirm()

    useEffect(() => {
        if (orderConfirmed) {
            // refresh balance after auto-confirm
            getBalance().then(setBalance).catch(() => {})
        }
    }, [orderConfirmed])

    useEffect(() => {
        async function load() {
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
            try { setStationState(await getStationApplication()) } catch { /* ignore */ }
        }
        load()
    }, [])

    const reloadStationState = async () => {
        try {
            setStationState(await getStationApplication())
        } catch {
            // ignore
        }
    }

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
                    <p className="page-desc">先看余额和接入状态，再决定是复制配置、充值还是排查请求。</p>
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
                    <div className="quick-actions-header">
                        <div>
                            <h3>快速操作</h3>
                            <p className="quick-actions-desc">把常用动作收在这里。先复制入口和配置，再按需去充值或查日志。</p>
                        </div>
                        <Link to="/recharge" className="btn btn-primary btn-sm">进入充值中心</Link>
                    </div>
                    <div className="base-url-card" onClick={() => copy(window.location.origin + '/v1', 'url')}>
                        <div className="base-url-icon">&#127760;</div>
                        <div className="base-url-copy">
                            <span className="base-url-label">统一 Base URL</span>
                            <code>{window.location.origin}/v1</code>
                        </div>
                        <span className="action-btn">{copied === 'url' ? '&#10003; 已复制' : '复制'}</span>
                    </div>
                    <div className="shortcut-grid">
                        <Link to="/recharge" className="shortcut-card shortcut-card-primary">
                            <span className="shortcut-icon">&#128176;</span>
                            <div>
                                <strong>充值</strong>
                                <p>补余额，继续跑请求。</p>
                            </div>
                        </Link>
                        <Link to="/usage" className="shortcut-card">
                            <span className="shortcut-icon">&#128202;</span>
                            <div>
                                <strong>请求日志</strong>
                                <p>看模型、耗时、扣费和状态码。</p>
                            </div>
                        </Link>
                        <Link to="/settings" className="shortcut-card">
                            <span className="shortcut-icon">&#128736;</span>
                            <div>
                                <strong>接入配置</strong>
                                <p>复制 SDK、CLI 和常用客户端片段。</p>
                            </div>
                        </Link>
                        <Link to="/docs" className="shortcut-card">
                            <span className="shortcut-icon">&#128214;</span>
                            <div>
                                <strong>接入文档</strong>
                                <p>查协议、模型目录和图片接口规则。</p>
                            </div>
                        </Link>
                        <Link to="/playground" className="shortcut-card">
                            <span className="shortcut-icon">&#9881;</span>
                            <div>
                                <strong>测试请求</strong>
                                <p>直接发一条真实请求，验证模型和 Key。</p>
                            </div>
                        </Link>
                        <a href="#developer-key" className="shortcut-card">
                            <span className="shortcut-icon">&#128273;</span>
                            <div>
                                <strong>开发者 Key</strong>
                                <p>生成、复制或轮换开发者 API Key。</p>
                            </div>
                        </a>
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

                <StationCard stationState={stationState} onSubmitted={reloadStationState} />

                {/* Pricing Info */}
                <div className="pricing-info glass-card animate-fade-in-up" style={{ animationDelay: '300ms' }}>
                    <h3>当前价格</h3>
                    <p className="pricing-note-row">价格和默认模型只做简表展示。更细的模型说明和完整接法放到接入配置页。</p>
                    <div className="price-row">
                        <div className="price-item">
                            <span className="price-label">Input Token</span>
                            <span className="price-val">${balance.price_input_per_million} <small>/ 百万</small></span>
                        </div>
                        <div className="price-item">
                            <span className="price-label">Cached Input</span>
                            <span className="price-val">${balance.price_cached_input_per_million} <small>/ 百万</small></span>
                        </div>
                        <div className="price-item">
                            <span className="price-label">Output Token</span>
                            <span className="price-val">${balance.price_output_per_million} <small>/ 百万</small></span>
                        </div>
                    </div>
                    <div className="price-row">
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
