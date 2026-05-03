import { useState, useEffect } from 'react'
import { Link } from 'react-router-dom'
import { Line } from 'react-chartjs-2'
import { Chart as ChartJS, CategoryScale, LinearScale, PointElement, LineElement, Title, Tooltip, Legend, Filler } from 'chart.js'
import { MOCK_BALANCE, MOCK_USAGE, getBalance, getUsageLogs, getAnnouncements, activateKey, getStationApplication, applyForStation, setGeneratedKey as storeGeneratedKey } from '../api/client'
import useOrderConfirm from '../hooks/useOrderConfirm'
import { useAuth } from '../hooks/useAuth'
import { usePublicModels } from '../hooks/usePublicModels'
import AppShell from '../components/AppShell'
import { formatLocalTime, getLocalDateRangeIso, getLocalIsoDate, getLocalTodayRangeIso, getRecentLocalIsoDates } from '../utils/time'
import './Dashboard.css'

ChartJS.register(CategoryScale, LinearScale, PointElement, LineElement, Title, Tooltip, Legend, Filler)

async function getLocalDailyUsage(days = 7) {
    const dates = getRecentLocalIsoDates(days)
    const rows = await Promise.all(dates.map(async (day) => {
        const range = getLocalDateRangeIso(day)
        if (!range) {
            return {
                day,
                input_tokens: 0,
                output_tokens: 0,
                tokens_total: 0,
                images_total: 0,
                cost_usd: 0,
                requests_total: 0,
            }
        }

        const usage = await getUsageLogs(1, 0, {
            start_date: range.start,
            end_date: range.end,
            end_exclusive: 'true',
        })
        const summary = usage.summary || {}
        return {
            day,
            input_tokens: summary.input_tokens || 0,
            output_tokens: summary.output_tokens || 0,
            tokens_total: summary.total_tokens || 0,
            images_total: summary.image_count || 0,
            cost_usd: summary.cost_usd || 0,
            requests_total: usage.total || 0,
        }
    }))
    return rows
}

function ReadinessCard({ authMode, username, hasDeveloperKey }) {
    const contentMap = {
        session_only: {
            tone: 'warning',
            eyebrow: '还差一步',
            title: '接口调用还未开通',
            description: `${username || '当前账户'} 已进入控制台。右侧生成调用凭证后，CLI、SDK 和常见客户端才能发起请求。`,
            statusItems: [
                { label: '当前会话', value: '控制台账号' },
                { label: '接口调用', value: '等待开通' },
                { label: '下一步', value: '在右侧生成' },
            ],
        },
        session_with_api: {
            tone: 'success',
            eyebrow: '已就绪',
            title: '接口状态正常',
            description: `${username || '当前账户'} 已具备调用凭证。右侧处理复制、重新生成和接入配置。`,
            statusItems: [
                { label: '当前会话', value: '控制台账号' },
                { label: '接口调用', value: '可直接请求' },
                { label: '常用动作', value: '复制配置' },
            ],
        },
        api: {
            tone: 'info',
            eyebrow: '当前会话',
            title: '接口状态正常',
            description: '当前浏览器已通过接口凭证进入控制台。请求可以继续使用当前凭证；充值、轮换和账户设置需要切回控制台账号。',
            statusItems: [
                { label: '当前会话', value: 'Key 会话' },
                { label: '接口调用', value: '可直接请求' },
                { label: '账户操作', value: '切回控制台账号' },
            ],
        },
    }
    const content = contentMap[authMode] || contentMap.api

    return (
        <div className={`readiness-card readiness-${content.tone}`}>
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
            </div>
        </div>
    )
}

function KeyManagement({ copied, copy, username, generatedApiKey, hasLocalDeveloperKey, latestDeveloperKey, activeDeveloperKeyCount, authMode, effectiveApiKey }) {
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
        <div id="developer-key" className="key-management-card">
            <div className="key-management-head">
                <span className="key-management-eyebrow">凭证操作</span>
                <h3>开发者 Key</h3>
            </div>
            {!username ? (
                <div className="key-panel-copy">
                    <p>复制当前会话 Key，或查看可直接粘贴到客户端里的配置片段。</p>
                    <div className="action-grid key-action-grid">
                        <button className="action-item key-copy-row" type="button" onClick={() => copy(effectiveApiKey, 'key')}>
                            <span className="action-icon">&#128273;</span>
                            <span className="key-copy-body">
                                <strong>当前开发者 Key</strong>
                                <code>{effectiveApiKey.substring(0, 12)}...</code>
                            </span>
                            <span className="action-btn">{copied === 'key' ? '&#10003; 已复制' : '复制'}</span>
                        </button>
                    </div>
                    <div className="action-links">
                        <Link to="/settings" className="btn btn-primary btn-sm">查看接入配置</Link>
                        <Link to="/docs" className="btn btn-ghost btn-sm">查看文档</Link>
                    </div>
                </div>
            ) : generatedKey && !showKey ? (
                <div>
                    <p className="key-panel-copy">
                        这把 Key 已可使用。复制 Key 或直接查看客户端配置。
                    </p>
                    <div className="action-grid key-action-grid">
                        <button className="action-item key-copy-row" type="button" onClick={() => copy(generatedKey, 'apikey')}>
                            <span className="action-icon">&#128273;</span>
                            <span className="key-copy-body">
                                <strong>开发者 API Key</strong>
                                <code>{maskedKey}</code>
                            </span>
                            <span className="action-btn">{copied === 'apikey' ? '&#10003; 已复制' : '复制'}</span>
                        </button>
                    </div>
                    <div className="key-secondary-actions">
                        <Link to="/settings" className="btn btn-primary btn-sm">查看接入配置</Link>
                        <button className="btn btn-secondary btn-sm" onClick={handleGenerate} disabled={generating}>
                            {generating ? '生成中...' : '重新生成 Key'}
                        </button>
                    </div>
                    {genError && <p style={{ color: 'var(--accent-rose)', fontSize: '0.85rem', marginTop: 'var(--space-sm)' }}>{genError}</p>}
                </div>
            ) : username && !hasLocalDeveloperKey && latestDeveloperKey ? (
                <div>
                    <p className="key-panel-copy">
                        当前账户已经有开发者 Key，但明文不会跨浏览器恢复。
                        如果你没有保存原值，需要重新生成一把新的开发者 Key。
                    </p>
                    <div className="action-grid key-action-grid">
                        <div className="action-item key-copy-row key-copy-row-static">
                            <span className="action-icon">&#128273;</span>
                            <span className="key-copy-body">
                                <strong>最近一把开发者 Key</strong>
                                <code>{latestDeveloperKey.masked_key}</code>
                            </span>
                            <span className="action-btn">{activeDeveloperKeyCount} 把有效 Key</span>
                        </div>
                    </div>
                    <div className="key-secondary-actions">
                        <button className="btn btn-primary btn-sm" onClick={handleGenerate} disabled={generating}>
                            {generating ? '生成中...' : '重新生成 Key'}
                        </button>
                        <Link to="/settings" className="btn btn-ghost btn-sm">查看配置说明</Link>
                    </div>
                    {genError && <p style={{ color: 'var(--accent-rose)', fontSize: '0.85rem', marginTop: 'var(--space-sm)' }}>{genError}</p>}
                </div>
            ) : showKey ? (
                <div>
                    <div className="key-warning-box">
                        <span className="key-warning-eyebrow">这次会显示完整值</span>
                        <p>这是新生成的完整 Key。明文只显示这一次。</p>
                    </div>
                    <div className="key-secret-panel">
                        <div className="key-secret-meta">
                            <span className="meta-pill">开发者 Key</span>
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
                        给当前账户生成一把开发者 Key，生成后会显示一次完整值。
                    </p>
                    <button className="btn btn-primary btn-sm" onClick={handleGenerate} disabled={generating}>
                        {generating ? '生成中...' : '生成开发者 Key'}
                    </button>
                    {genError && <p style={{ color: 'var(--accent-rose)', fontSize: '0.85rem', marginTop: 'var(--space-sm)' }}>{genError}</p>}
                </div>
            )}
        </div>
    )
}

function ReferralNudgeCard() {
    return (
        <Link to="/referrals" className="referral-nudge-card animate-fade-in-up">
            <div className="referral-nudge-icon">
                <svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                    <path d="M16 5.5a3 3 0 1 1-6 0 3 3 0 0 1 6 0Z" />
                    <path d="M8 19a5 5 0 0 1 10 0" />
                    <path d="M5.5 9.5v5" />
                    <path d="M3 12h5" />
                </svg>
            </div>
            <div className="referral-nudge-copy">
                <span>邀请朋友</span>
                <strong>朋友注册得 $10，你得 $5</strong>
                <p>朋友开始调用 API 后，你再得 $5；之后充值奖励按到账额度 20% 给你。</p>
            </div>
            <div className="referral-nudge-meta">
                <span>邀请记录</span>
                <strong>查看</strong>
            </div>
        </Link>
    )
}

function DeveloperAccessPanel(props) {
    return (
        <div className="developer-access-panel glass-card animate-fade-in-up">
            <ReadinessCard
                authMode={props.authMode}
                username={props.username}
                hasDeveloperKey={props.hasDeveloperKey}
            />
            <KeyManagement {...props} />
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
                        <p className="station-card-desc">站长资格已经开通。这里继续处理下游用户、分润和人工结算。</p>
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
                        <p className="station-card-desc">申请已经提交。当前还是审核制，避免影响现有支付和用户链路。</p>
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
                    <p className="station-card-desc">想做自己的站长，就先提交申请。第一期先走审核制，不影响你当前使用。</p>
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
                        <textarea value={form.audience_note} onChange={(e) => handleChange('audience_note', e.target.value)} placeholder="写清楚你服务谁、预估规模，以及你准备怎么运营" rows={4} required />
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
                    <span className="station-hint">当前先走审核制。通过后会开通站长资格和结算资料。</span>
                </div>
            </form>
        </div>
    )
}

export default function Dashboard() {
    const { defaultTextModel, defaultImageModel } = usePublicModels()
    const {
        activeDeveloperKeyCount,
        authMode,
        effectiveApiKey,
        generatedApiKey,
        hasDeveloperKey,
        hasLocalDeveloperKey,
        latestDeveloperKey,
        username,
    } = useAuth()
    const [balance, setBalance] = useState(null)
    const [usage, setUsage] = useState(null)
    const [todayUsage, setTodayUsage] = useState(null)
    const [dailyData, setDailyData] = useState(null)
    const [announcements, setAnnouncements] = useState([])
    const [dismissedAnns, setDismissedAnns] = useState(() => {
        try { return JSON.parse(localStorage.getItem('coincoin_dismissed_anns') || '[]') } catch { return [] }
    })
    const [dismissedModalAnns, setDismissedModalAnns] = useState(() => {
        try { return JSON.parse(localStorage.getItem('coincoin_dismissed_modal_anns') || '[]') } catch { return [] }
    })
    const [sessionHiddenModalAnns, setSessionHiddenModalAnns] = useState([])
    const [modalSlotUsed, setModalSlotUsed] = useState(false)
    const [copied, setCopied] = useState('')
    const [chartMode, setChartMode] = useState('cost')
    const [signupBonusMessage, setSignupBonusMessage] = useState(() => {
        try { return localStorage.getItem('coincoin_signup_bonus_message') || '' } catch { return '' }
    })
    const [recentSignup, setRecentSignup] = useState(() => {
        try { return localStorage.getItem('coincoin_recent_signup') === '1' } catch { return false }
    })
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
            const todayRange = getLocalTodayRangeIso()
            try {
                const [b, u, todayU] = await Promise.all([
                    getBalance(),
                    getUsageLogs(20),
                    getUsageLogs(1, 0, {
                        start_date: todayRange?.start,
                        end_date: todayRange?.end,
                        end_exclusive: todayRange ? 'true' : undefined,
                    }),
                ])
                setBalance(b)
                setUsage(u)
                setTodayUsage(todayU)
            } catch {
                setBalance(MOCK_BALANCE)
                setUsage(MOCK_USAGE)
                setTodayUsage(null)
            }
            try { setDailyData(await getLocalDailyUsage(7)) } catch { /* ignore */ }
            try { setAnnouncements(await getAnnouncements()) } catch { /* ignore */ }
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

    const hideModalAnn = (id) => {
        setSessionHiddenModalAnns((current) => current.includes(id) ? current : [...current, id])
        setModalSlotUsed(true)
        if (recentSignup) {
            setRecentSignup(false)
            try { localStorage.removeItem('coincoin_recent_signup') } catch { /* ignore */ }
        }
    }

    const dismissModalAnn = (id) => {
        const next = [...dismissedModalAnns, id]
        setDismissedModalAnns(next)
        localStorage.setItem('coincoin_dismissed_modal_anns', JSON.stringify(next))
        setModalSlotUsed(true)
        if (recentSignup) {
            setRecentSignup(false)
            try { localStorage.removeItem('coincoin_recent_signup') } catch { /* ignore */ }
        }
    }

    const dismissSignupBonus = () => {
        setSignupBonusMessage('')
        try { localStorage.removeItem('coincoin_signup_bonus_message') } catch { /* ignore */ }
    }

    if (!balance) {
        return (
            <AppShell title="概览" description="先看余额、Key 状态和最近请求。">
                <div className="dashboard-page">
                    <div className="loading-state">
                        <div className="loading-spinner"></div>
                        <p>加载中...</p>
                    </div>
                </div>
            </AppShell>
        )
    }

    const activeBannerAnns = announcements.filter(a => (a.display_type || 'banner') !== 'modal' && !dismissedAnns.includes(a.id))
    const firstEligibleModalAnn = announcements.find(a => (
        (a.display_type || 'banner') === 'modal'
        && ((a.audience || 'all') === 'all' || ((a.audience || 'all') === 'signup' && recentSignup))
        && !dismissedModalAnns.includes(a.id)
    ))
    const activeModalAnn = modalSlotUsed || sessionHiddenModalAnns.includes(firstEligibleModalAnn?.id)
        ? null
        : firstEligibleModalAnn

    const todayStr = getLocalIsoDate()
    const todaySummary = dailyData?.find(d => d.day === todayStr) || dailyData?.[dailyData.length - 1] || null
    const todayUsageFallback = usage?.data?.filter(d => getLocalIsoDate(d.created_at) === todayStr) || []
    const todayDetailSummary = todayUsage?.summary || null
    const todayCost = todayDetailSummary
        ? todayDetailSummary.cost_usd
        : todaySummary
            ? todaySummary.cost_usd
            : todayUsageFallback.reduce((sum, d) => sum + d.cost_cents, 0) / 100
    const todayTokens = todayDetailSummary
        ? (todayDetailSummary.total_tokens || 0)
        : todaySummary
            ? (todaySummary.tokens_total || 0)
            : todayUsageFallback.reduce((sum, d) => sum + (d.total_tokens || d.input_tokens + d.output_tokens), 0)
    const todayImages = todayDetailSummary
        ? (todayDetailSummary.image_count || 0)
        : todaySummary
            ? (todaySummary.images_total || 0)
            : todayUsageFallback.reduce((sum, d) => sum + (d.image_count || 0), 0)
    const todayRequests = todayUsage
        ? (todayUsage.total || 0)
        : todaySummary
            ? (todaySummary.requests_total || 0)
            : todayUsageFallback.length

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
            <AppShell
            title="概览"
            description="余额、密钥状态和最近请求都在这里。"
            actions={<Link to="/recharge" className="btn btn-primary btn-sm">充值</Link>}
        >
            <div className="dashboard-page dashboard">

                {/* Announcements */}
                {activeBannerAnns.map(a => (
                    <div key={a.id} className={`announcement-banner ann-${a.priority} animate-fade-in`}>
                        <div className="ann-content">
                            <strong>{a.title}</strong>
                            <span>{a.content}</span>
                        </div>
                        <button className="ann-close" onClick={() => dismissAnn(a.id)}>&times;</button>
                    </div>
                ))}

                {activeModalAnn && (
                    <div className="announcement-modal-backdrop animate-fade-in" role="dialog" aria-modal="true" aria-labelledby="announcement-modal-title">
                        <div className={`announcement-modal ann-${activeModalAnn.priority || 'info'}`}>
                            <button className="ann-modal-close" onClick={() => hideModalAnn(activeModalAnn.id)} aria-label="关闭公告">&times;</button>
                            {activeModalAnn.image_url ? (
                                <img className="ann-modal-image" src={activeModalAnn.image_url} alt="" />
                            ) : null}
                            <div className="ann-modal-copy">
                                <span className="ann-modal-kicker">CoinCoin</span>
                                <h2 id="announcement-modal-title">{activeModalAnn.title}</h2>
                                <p>{activeModalAnn.content}</p>
                            </div>
                            <div className="ann-modal-actions">
                                {activeModalAnn.cta_label && activeModalAnn.cta_value ? (
                                    <button className="btn btn-primary" onClick={() => copy(activeModalAnn.cta_value, `ann-${activeModalAnn.id}`)}>
                                        {copied === `ann-${activeModalAnn.id}` ? '已复制' : activeModalAnn.cta_label}
                                    </button>
                                ) : null}
                                <button className="btn btn-secondary" onClick={() => hideModalAnn(activeModalAnn.id)}>暂时不用</button>
                            </div>
                            <label className="ann-modal-dismiss">
                                <input type="checkbox" onChange={(event) => event.target.checked && dismissModalAnn(activeModalAnn.id)} />
                                <span>不再显示</span>
                            </label>
                        </div>
                    </div>
                )}

                {/* Low balance warning */}
                {balance.balance_usd < 0.10 && (
                    <div className="low-balance-banner critical animate-fade-in">
                        <span>&#9888; 余额只剩 ${balance.balance_usd.toFixed(2)}，再发几次请求就可能扣空。</span>
                        <Link to="/recharge" className="btn btn-sm btn-primary">立即充值</Link>
                    </div>
                )}
                {balance.balance_usd >= 0.10 && balance.balance_usd < 1.00 && (
                    <div className="low-balance-banner warning animate-fade-in">
                        <span>&#9888; 余额低于 $1.00，当前剩余 ${balance.balance_usd.toFixed(2)}。</span>
                        <Link to="/recharge" className="btn btn-sm btn-secondary">去充值</Link>
                    </div>
                )}

                {/* Auto-confirmed order banner */}
                {orderConfirmed && (
                    <div className="low-balance-banner animate-fade-in" style={{ background: 'rgba(16,185,129,0.1)', borderColor: 'rgba(16,185,129,0.3)', color: 'var(--accent-emerald)' }}>
                        <span>&#10003; 充值已到账。+${(orderConfirmed.added_cents / 100).toFixed(2)}，当前余额 ${orderConfirmed.new_balance_usd?.toFixed(2)}</span>
                        <button className="btn btn-sm btn-secondary" onClick={dismissOrder}>知道了</button>
                    </div>
                )}

                {signupBonusMessage && (
                    <div className="low-balance-banner animate-fade-in" style={{ background: 'rgba(16,185,129,0.1)', borderColor: 'rgba(16,185,129,0.3)', color: 'var(--accent-emerald)' }}>
                        <span>&#10003; {signupBonusMessage}</span>
                        <button className="btn btn-sm btn-secondary" onClick={dismissSignupBonus}>知道了</button>
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
                            <span className="stat-label">累计 Token</span>
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
                            <span className="stat-sub">{todayRequests} 次请求 &middot; {todayTokens.toLocaleString()} Tokens &middot; {todayImages} 张图</span>
                        </div>
                    </div>
                </div>

                <ReferralNudgeCard />

                <DeveloperAccessPanel
                    copied={copied}
                    copy={copy}
                    username={username}
                    generatedApiKey={generatedApiKey}
                    hasLocalDeveloperKey={hasLocalDeveloperKey}
                    latestDeveloperKey={latestDeveloperKey}
                    activeDeveloperKeyCount={activeDeveloperKeyCount}
                    authMode={authMode}
                    effectiveApiKey={effectiveApiKey}
                    hasDeveloperKey={hasDeveloperKey}
                />

                {/* Trend Chart */}
                {chartData && (
                    <div className="trend-card glass-card animate-fade-in-up" style={{ animationDelay: '150ms' }}>
                        <div className="section-row">
                            <h3>近 7 天</h3>
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
                            <p className="quick-actions-desc">常用入口都收在这里。</p>
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
                    <div className="base-url-card base-url-card-secondary" onClick={() => copy(window.location.origin, 'anthropic')}>
                        <div className="base-url-icon">&#129302;</div>
                        <div className="base-url-copy">
                            <span className="base-url-label">Claude Code Base URL</span>
                            <code>{window.location.origin}</code>
                        </div>
                        <span className="action-btn">{copied === 'anthropic' ? '&#10003; 已复制' : '复制'}</span>
                    </div>
                    <div className="shortcut-grid">
                        <Link to="/guides/claude-code" className="shortcut-card shortcut-card-highlight">
                            <span className="shortcut-icon">&#129302;</span>
                            <div>
                                <strong>Claude Code</strong>
                                <p>查看 Claude Code 的环境变量配置。</p>
                            </div>
                        </Link>
                        <Link to="/recharge" className="shortcut-card shortcut-card-primary">
                            <span className="shortcut-icon">&#128176;</span>
                            <div>
                                <strong>充值</strong>
                                <p>给账户补余额。</p>
                            </div>
                        </Link>
                        <Link to="/usage" className="shortcut-card">
                            <span className="shortcut-icon">&#128202;</span>
                            <div>
                                <strong>请求日志</strong>
                                <p>查看请求明细和状态码。</p>
                            </div>
                        </Link>
                        <Link to="/settings" className="shortcut-card">
                            <span className="shortcut-icon">&#128736;</span>
                            <div>
                                <strong>接入配置</strong>
                                <p>复制各类客户端配置。</p>
                            </div>
                        </Link>
                        <Link to="/docs" className="shortcut-card">
                            <span className="shortcut-icon">&#128214;</span>
                            <div>
                                <strong>接入文档</strong>
                                <p>查看接口、模型和示例。</p>
                            </div>
                        </Link>
                        <Link to="/playground" className="shortcut-card">
                            <span className="shortcut-icon">&#9881;</span>
                            <div>
                                <strong>测试请求</strong>
                                <p>发起一条测试请求。</p>
                            </div>
                        </Link>
                        <a href="#developer-key" className="shortcut-card">
                            <span className="shortcut-icon">&#128273;</span>
                            <div>
                                <strong>开发者 Key</strong>
                                <p>生成或复制开发者 Key。</p>
                            </div>
                        </a>
                    </div>
                </div>

                <StationCard stationState={stationState} onSubmitted={reloadStationState} />

                {/* Pricing Info */}
                <div className="pricing-info glass-card animate-fade-in-up" style={{ animationDelay: '300ms' }}>
                    <h3>当前价格</h3>
                    <p className="pricing-note-row">这里只展示简表。完整模型目录和接入示例在文档页。</p>
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
                            <span className="price-label">默认文本模型</span>
                            <span className="price-val model-tag">{defaultTextModel?.id || 'opus'}</span>
                        </div>
                        <div className="price-item">
                            <span className="price-label">默认图片模型</span>
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
                                            <td>{formatLocalTime(log.created_at, { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })}</td>
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
        </AppShell>
    )
}
