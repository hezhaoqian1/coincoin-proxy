import { useState, useRef, useEffect, useCallback } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useSearchParams } from 'react-router-dom'
import { CREDIT_PRODUCTS, redeemCode, getApiKey, confirmOrder, listOrders, getBillingState } from '../api/client'
import useOrderConfirm from '../hooks/useOrderConfirm'
import { useAuth } from '../hooks/useAuth'
import AppShell from '../components/AppShell'
import './Recharge.css'

const POLL_INTERVAL = 3000
const MAX_POLL_ATTEMPTS = 200

function formatUsd(cents) {
    return `$${((Number(cents) || 0) / 100).toFixed(2)}`
}

function formatDateTime(value) {
    if (!value) return '-'
    const date = new Date(value)
    if (Number.isNaN(date.getTime())) return '-'
    return date.toLocaleString('zh-CN', {
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
    })
}

function isUsableLegacyPack(pack, nowMs = Date.now()) {
    if (!pack) return false
    const status = String(pack.status || '').trim().toLowerCase()
    if (status && status !== 'active') return false
    if (Number(pack.remaining_cents || 0) <= 0) return false
    const expiresAtMs = new Date(pack.expires_at).getTime()
    return Number.isFinite(expiresAtMs) && expiresAtMs > nowMs
}

function trimMoney(value) {
    const numberValue = Number(value)
    if (!Number.isFinite(numberValue)) return value || ''
    return numberValue.toFixed(2).replace(/\.00$/, '').replace(/(\.\d)0$/, '$1')
}

function mergeProduct(apiProduct, fallbackProduct) {
    if (!apiProduct) return fallbackProduct
    const payMoney = apiProduct.money || fallbackProduct.money
    const creditCents = apiProduct.promised_credit_cents ?? fallbackProduct.creditCents
    return {
        ...fallbackProduct,
        id: apiProduct.id,
        kind: 'credit',
        name: apiProduct.name || fallbackProduct.name,
        money: payMoney,
        price: `¥${trimMoney(payMoney)}`,
        creditCents,
        balanceLabel: `${formatUsd(creditCents)} 美金额度`,
        unitLabel: fallbackProduct.unitLabel,
        features: fallbackProduct.features,
    }
}

function formatOrderTime(value) {
    if (!value) return '-'
    const date = new Date(value)
    if (Number.isNaN(date.getTime())) return '-'
    return date.toLocaleString('zh-CN', {
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
    })
}

export default function Recharge() {
    const [searchParams, setSearchParams] = useSearchParams()
    const { isLoggedIn } = useAuth()
    const [selectedProductId, setSelectedProductId] = useState('credit_standard')
    const productRefs = useRef([])
    const [loading, setLoading] = useState(false)
    const [billingState, setBillingState] = useState(null)
    const [billingLoading, setBillingLoading] = useState(false)
    const [billingError, setBillingError] = useState('')

    const [redeemInput, setRedeemInput] = useState('')
    const [redeemLoading, setRedeemLoading] = useState(false)
    const [redeemMsg, setRedeemMsg] = useState(null)
    const { confirmResult: orderConfirmed, dismiss: dismissOrder } = useOrderConfirm()

    const [polling, setPolling] = useState(false)
    const [pollInfo, setPollInfo] = useState(null)
    const [payResult, setPayResult] = useState(null)
    const [orders, setOrders] = useState([])
    const [ordersLoading, setOrdersLoading] = useState(false)
    const [ordersError, setOrdersError] = useState('')
    const pollingRef = useRef(false)
    const [autoRedirect, setAutoRedirect] = useState(5)
    const [popupBlocked, setPopupBlocked] = useState(false)
    const navigate = useNavigate()
    const activeSection = searchParams.get('section') || 'recharge'

    const loadBillingState = useCallback(async () => {
        if (!isLoggedIn) {
            setBillingState(null)
            setBillingError('')
            return
        }
        setBillingLoading(true)
        setBillingError('')
        try {
            const data = await getBillingState()
            setBillingState(data)
        } catch {
            setBillingError('额度信息加载失败，请刷新后重试。')
        } finally {
            setBillingLoading(false)
        }
    }, [isLoggedIn])

    const products = CREDIT_PRODUCTS.map(product => (
        mergeProduct(
            billingState?.products?.credits?.find(item => item.id === product.id),
            product,
        )
    ))
    const selectedProduct = products.find(item => item.id === selectedProductId) || products[1] || products[0]

    const loadOrders = useCallback(async () => {
        if (!isLoggedIn) return
        setOrdersLoading(true)
        setOrdersError('')
        try {
            const data = await listOrders(20)
            setOrders(Array.isArray(data) ? data : [])
        } catch {
            setOrdersError('订单记录加载失败，请稍后刷新。')
        } finally {
            setOrdersLoading(false)
        }
    }, [isLoggedIn])

    useEffect(() => {
        return () => { pollingRef.current = false }
    }, [])

    useEffect(() => {
        if (orderConfirmed) loadOrders()
    }, [orderConfirmed, loadOrders])

    useEffect(() => {
        loadOrders()
    }, [loadOrders])

    useEffect(() => {
        loadBillingState()
    }, [loadBillingState])

    useEffect(() => {
        if (!payResult) return
        if (autoRedirect <= 0) { navigate('/dashboard'); return }
        const t = setTimeout(() => setAutoRedirect(prev => prev - 1), 1000)
        return () => clearTimeout(t)
    }, [payResult, autoRedirect, navigate])

    const startPolling = useCallback((orderNo, planName, money, payUrl) => {
        setPolling(true)
        setPollInfo({ orderNo, planName, money, payUrl })
        pollingRef.current = true
        let attempts = 0

        const poll = async () => {
            if (!pollingRef.current) return
            attempts++
            try {
                const result = await confirmOrder(orderNo)
                if (result.success || result.message === 'order already confirmed') {
                    pollingRef.current = false
                    setPolling(false)
                    setPayResult(result)
                    localStorage.removeItem('coincoin_last_order')
                    loadOrders()
                    loadBillingState()
                    document.title = '\u2705 \u5145\u503c\u6210\u529f\uff01'
                    setTimeout(() => { document.title = 'ClawFather' }, 8000)
                    return
                }
            } catch {
                // 402 = not paid yet
            }
            if (attempts < MAX_POLL_ATTEMPTS && pollingRef.current) {
                setTimeout(poll, POLL_INTERVAL)
            } else if (pollingRef.current) {
                pollingRef.current = false
                setPolling(false)
            }
        }
        poll()
    }, [loadOrders, loadBillingState])

    const handlePay = async () => {
        if (!isLoggedIn) {
            navigate('/login')
            return
        }

        const plan = selectedProduct || CREDIT_PRODUCTS[1] || products[0]
        const planMoney = plan.money
        if (!planMoney || parseFloat(planMoney) <= 0) return
        const planName = plan.name

        // IMPORTANT:
        // Open a blank tab synchronously on the click gesture. If we wait until after `await fetch`,
        // many browsers treat it as a popup and block it, causing the payment to hijack the same tab.
        const payWin = window.open('about:blank', '_blank')
        if (!payWin) setPopupBlocked(true)
        else {
            try {
                payWin.document.title = '正在打开支付页面...'
            } catch { /* ignore */ }
        }

        setLoading(true)
        setPopupBlocked(false)
        try {
            const raw = await fetch('/v1/orders/create', {
                method: 'POST',
                headers: {
                    'Authorization': `Bearer ${getApiKey()}`,
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({ name: planName, money: planMoney, pay_type: 'alipay', product_id: plan.id })
            })
            const res = await raw.json()
            if (!raw.ok) {
                if (raw.status === 402) {
                    alert('余额不足，请先通过兑换码充值后再试')
                } else {
                    alert(res.detail || `创建订单失败 (${raw.status})`)
                }
                if (payWin) payWin.close()
                return
            }
            if (res.pay_url && res.order_no) {
                localStorage.setItem('coincoin_last_order', JSON.stringify({
                    orderNo: res.order_no,
                    planName,
                    money: planMoney
                }))
                // Navigate the blank tab to the payment URL (if popup wasn't blocked).
                if (payWin) {
                    try { payWin.opener = null } catch { /* ignore */ }
                    try { payWin.location.href = res.pay_url } catch { /* ignore */ }
                    try { payWin.focus() } catch { /* ignore */ }
                } else {
                    setPopupBlocked(true)
                }
                startPolling(res.order_no, planName, planMoney, res.pay_url)
                loadOrders()
                loadBillingState()
            } else {
                alert(res.detail || '创建订单失败，请重试')
                if (payWin) payWin.close()
            }
        } catch (e) {
            alert('网络错误: ' + (e.message || '请检查网络连接后重试'))
            if (payWin) payWin.close()
        } finally {
            setLoading(false)
        }
    }

    const handleRedeem = async () => {
        if (!isLoggedIn) {
            navigate('/login')
            return
        }

        if (!redeemInput.trim()) return
        setRedeemLoading(true)
        setRedeemMsg(null)
        try {
            const res = await redeemCode(redeemInput.trim())
            if (res.success) {
                setRedeemMsg({ type: 'success', text: `兑换成功！获得 $${(res.added_cents / 100).toFixed(2)} 美金额度，当前余额 $${res.new_balance_usd.toFixed(2)}` })
                setRedeemInput('')
                loadBillingState()
            } else {
                setRedeemMsg({ type: 'error', text: res.detail || res.message || '兑换失败' })
            }
        } catch {
            setRedeemMsg({ type: 'error', text: '网络错误，请重试' })
        } finally {
            setRedeemLoading(false)
        }
    }

    useEffect(() => {
        if (!isLoggedIn) return
        const target = searchParams.get('section')
        if (!target) return
        const sectionEl = document.getElementById(`recharge-section-${target}`)
        if (sectionEl) {
            sectionEl.scrollIntoView({ behavior: 'smooth', block: 'start' })
        }
    }, [isLoggedIn, searchParams])

    const handleProductKeyDown = (event, index) => {
        let nextIndex = null
        if (event.key === 'ArrowRight' || event.key === 'ArrowDown') {
            nextIndex = (index + 1) % products.length
        } else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') {
            nextIndex = (index - 1 + products.length) % products.length
        } else if (event.key === 'Home') {
            nextIndex = 0
        } else if (event.key === 'End') {
            nextIndex = products.length - 1
        }
        if (nextIndex === null) return
        event.preventDefault()
        setSelectedProductId(products[nextIndex].id)
        productRefs.current[nextIndex]?.focus()
    }

    const pageContent = (
        <div className="recharge-page">
            {isLoggedIn && (
                <div className="recharge-local-nav glass-card animate-fade-in-up">
                    {[
                        ['recharge', '充值'],
                        ['orders', '我的订单'],
                        ['redeem', '兑换'],
                    ].map(([key, label]) => (
                        <button
                            key={key}
                            className={`recharge-local-nav-item ${activeSection === key ? 'active' : ''}`}
                            onClick={() => {
                                const next = new URLSearchParams(searchParams)
                                next.set('section', key)
                                setSearchParams(next, { replace: true })
                                const sectionEl = document.getElementById(`recharge-section-${key}`)
                                if (sectionEl) sectionEl.scrollIntoView({ behavior: 'smooth', block: 'start' })
                            }}
                        >
                            {label}
                        </button>
                    ))}
                </div>
            )}

                {isLoggedIn && (
                    <BillingSnapshot
                        billingState={billingState}
                        loading={billingLoading}
                        error={billingError}
                        onRefresh={loadBillingState}
                    />
                )}

                {orderConfirmed && (
                    <div className="glass-card animate-fade-in" style={{ padding: 'var(--space-lg)', marginBottom: 'var(--space-lg)', background: 'rgba(16,185,129,0.08)', border: '1px solid rgba(16,185,129,0.25)' }}>
                        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                            <span style={{ color: 'var(--accent-emerald)' }}>&#10003; 上一笔支付已到账，获得 {formatUsd(orderConfirmed.added_cents)} 美金额度。</span>
                            <button className="btn btn-sm btn-secondary" onClick={dismissOrder}>知道了</button>
                        </div>
                    </div>
                )}

                <div id="recharge-section-recharge" className="recharge-anchor"></div>

                <div className="pricing-section-head">
                    <div>
                        <span className="recharge-kicker">Permanent USD Credit</span>
                        <h3>永久美金额度</h3>
                        <p>购买后永久有效、不会过期，可多次购买并叠加到当前美金额度。</p>
                    </div>
                </div>
                <div
                    className="recharge-plans recharge-credit-plans stagger-children"
                    role="radiogroup"
                    aria-label="选择美金额度档位"
                >
                    {products.map((plan, i) => (
                        <ProductCard
                            key={plan.id}
                            product={plan}
                            selected={selectedProductId === plan.id}
                            onSelect={() => setSelectedProductId(plan.id)}
                            buttonRef={(node) => { productRefs.current[i] = node }}
                            tabIndex={selectedProductId === plan.id ? 0 : -1}
                            onKeyDown={(event) => handleProductKeyDown(event, i)}
                            delay={i * 80}
                        />
                    ))}
                </div>

                <div id="recharge-section-orders" className="recharge-anchor"></div>

                {payResult ? (
                    <div className="glass-card animate-fade-in" style={{ padding: 'var(--space-xl)', textAlign: 'center', marginBottom: 'var(--space-lg)' }}>
                        <div style={{ fontSize: '3rem', marginBottom: 'var(--space-md)' }}>&#10003;</div>
                        <h2 style={{ color: 'var(--accent-emerald)', marginBottom: 'var(--space-sm)' }}>充值成功！</h2>
                        <p style={{ fontSize: '1.1rem', marginBottom: 'var(--space-md)' }}>
                            {formatUsd(payResult.added_cents)} 美金额度已到账。
                        </p>
                        <p style={{ color: 'var(--text-secondary)', fontSize: '0.95rem', marginBottom: 'var(--space-sm)' }}>
                            当前可用美金额度 {formatUsd(payResult.available_cents ?? payResult.new_balance)}
                        </p>
                        <p style={{ color: 'var(--text-secondary)', fontSize: '0.9rem', marginBottom: 'var(--space-md)' }}>
                            {autoRedirect} 秒后自动跳转到概览页...
                        </p>
                        <div style={{ display: 'flex', gap: 'var(--space-md)', justifyContent: 'center' }}>
                            <button className="btn btn-primary" onClick={() => navigate('/dashboard')}>立即前往仪表盘</button>
                            <button className="btn btn-secondary" onClick={() => { setPayResult(null); setAutoRedirect(5) }}>继续充值</button>
                        </div>
                    </div>
                ) : polling ? (
                    <div className="glass-card animate-fade-in" style={{ padding: 'var(--space-xl)', textAlign: 'center', marginBottom: 'var(--space-lg)' }}>
                        <div className="loading-spinner" style={{ width: 48, height: 48, margin: '0 auto var(--space-md)' }}></div>
                        <h2 style={{ marginBottom: 'var(--space-sm)' }}>等待支付完成</h2>
                        <p style={{ color: 'var(--text-secondary)', marginBottom: 'var(--space-sm)' }}>
                            {pollInfo?.planName} ¥{pollInfo?.money}
                        </p>
                        <p style={{ color: 'var(--accent-amber)', fontSize: '0.9rem', marginBottom: 'var(--space-md)', background: 'rgba(245,158,11,0.08)', padding: 'var(--space-sm) var(--space-md)', borderRadius: 'var(--radius-sm)', border: '1px solid rgba(245,158,11,0.2)' }}>
                            请在新标签完成支付。成功后本页会自动更新。
                        </p>
                        {popupBlocked && pollInfo?.payUrl && (
                            <div style={{ marginBottom: 'var(--space-md)' }}>
                                <p style={{ color: 'var(--accent-amber)', marginBottom: 'var(--space-sm)' }}>
                                    浏览器可能拦截了新窗口。点下面的按钮重新打开支付页即可。
                                </p>
                                <div style={{ display: 'flex', gap: 'var(--space-md)', justifyContent: 'center', flexWrap: 'wrap' }}>
                                    <a className="btn btn-primary" href={pollInfo.payUrl} target="_blank" rel="noopener noreferrer">
                                        打开支付页面（新标签）
                                    </a>
                                    <button
                                        className="btn btn-secondary"
                                        onClick={() => navigator.clipboard.writeText(pollInfo.payUrl).catch(() => {})}
                                    >
                                        复制支付链接
                                    </button>
                                </div>
                            </div>
                        )}
                        <button className="btn btn-secondary btn-sm" onClick={() => { pollingRef.current = false; setPolling(false) }}>取消等待</button>
                    </div>
                ) : (
                    <div className="pay-action animate-fade-in-up" style={{ animationDelay: '400ms' }}>
                        <button
                            className="btn btn-primary btn-lg pay-btn"
                            onClick={handlePay}
                            disabled={isLoggedIn && loading}
                        >
                            {isLoggedIn && loading ? '创建订单中...' : (
                                <>
                                    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><rect x="1" y="4" width="22" height="16" rx="2" ry="2" /><line x1="1" y1="10" x2="23" y2="10" /></svg>
                                    {!isLoggedIn
                                        ? '登录后充值'
                                        : `支付宝支付 ${selectedProduct?.price || ''}`
                                    }
                                </>
                            )}
                        </button>
                        <p className="pay-note">{isLoggedIn ? '支付页会在新标签打开，到账后本页自动更新。' : '登录后才能创建订单和入账。'}</p>
                        <div className="recharge-next-links">
                            {isLoggedIn ? (
                                <>
                                    <Link to="/dashboard" className="btn btn-ghost btn-sm">返回仪表盘</Link>
                                    <Link to="/usage" className="btn btn-ghost btn-sm">查看请求日志</Link>
                                    <Link to="/guides/api-quickstart" className="btn btn-ghost btn-sm">去接入指南</Link>
                                </>
                            ) : (
                                <>
                                    <Link to="/login" className="btn btn-ghost btn-sm">登录</Link>
                                    <Link to="/register" className="btn btn-ghost btn-sm">注册</Link>
                                    <Link to="/docs" className="btn btn-ghost btn-sm">查看文档</Link>
                                </>
                            )}
                        </div>
                    </div>
                )}

                <div className="glass-card animate-fade-in-up" style={{ animationDelay: '460ms', padding: 'var(--space-lg)', marginBottom: 'var(--space-lg)' }}>
                    <div className="settings-section-head" style={{ marginBottom: 'var(--space-md)' }}>
                        <div>
                            <h3>我的订单</h3>
                        </div>
                        {isLoggedIn && <button className="btn btn-secondary btn-sm" onClick={loadOrders} disabled={ordersLoading}>刷新</button>}
                    </div>
                    {pollInfo?.orderNo && (
                        <div className="config-table recharge-live-order">
                            <div className="config-row">
                                <span className="config-label">当前状态</span>
                                <code>{payResult ? '已到账' : polling ? '等待支付' : '待确认'}</code>
                            </div>
                            <div className="config-row">
                                <span className="config-label">订单号</span>
                                <code>{pollInfo.orderNo}</code>
                            </div>
                            <div className="config-row">
                                <span className="config-label">充值项目</span>
                                <code>{pollInfo.planName || '-'}</code>
                            </div>
                            <div className="config-row">
                                <span className="config-label">金额</span>
                                <code>{pollInfo.money ? `¥${pollInfo.money}` : '-'}</code>
                            </div>
                        </div>
                    )}
                    {!isLoggedIn ? (
                        <div className="orders-empty">登录后可以查看自己的支付订单。</div>
                    ) : ordersLoading && orders.length === 0 ? (
                        <div className="orders-empty">正在加载订单...</div>
                    ) : ordersError ? (
                        <div className="orders-empty orders-error">{ordersError}</div>
                    ) : orders.length === 0 ? (
                        <div className="orders-empty">暂无支付订单</div>
                    ) : (
                        <div className="orders-list">
                            {orders.map(order => (
                                <div className="order-history-row" key={order.order_no || order.id}>
                                    <div className="order-history-main">
                                        <code>{order.order_no || order.id || '-'}</code>
                                        <span>{formatOrderTime(order.confirmed_at || order.created_at)}</span>
                                    </div>
                                    <div className="order-history-meta">
                                        <span>¥{order.amount_rmb || order.money || '-'}</span>
                                        <span>+${((order.add_balance_cents ?? order.added_cents ?? 0) / 100).toFixed(2)}</span>
                                        <span className={`order-status order-status-${order.status}`}>
                                            {order.status === 'confirmed' ? '已到账' : '待确认'}
                                        </span>
                                    </div>
                                </div>
                            ))}
                        </div>
                    )}
                </div>

                <div id="recharge-section-redeem" className="recharge-anchor"></div>

                <div className="redeem-section glass-card animate-fade-in-up" style={{ animationDelay: '500ms' }}>
                    <h3>兑换码充值</h3>
                    {isLoggedIn ? (
                        <>
                            <div className="redeem-row">
                                <input
                                    type="text"
                                    className="input-field redeem-input"
                                    placeholder="请输入兑换码"
                                    value={redeemInput}
                                    onChange={(e) => setRedeemInput(e.target.value)}
                                    onKeyDown={(e) => e.key === 'Enter' && handleRedeem()}
                                />
                                <button
                                    className="btn btn-primary"
                                    onClick={handleRedeem}
                                    disabled={redeemLoading || !redeemInput.trim()}
                                >
                                    {redeemLoading ? '兑换中...' : '兑换'}
                                </button>
                            </div>
                            {redeemMsg && (
                                <div className={`redeem-msg ${redeemMsg.type}`}>{redeemMsg.text}</div>
                            )}
                        </>
                    ) : (
                        <div style={{ display: 'flex', gap: 'var(--space-sm)', flexWrap: 'wrap' }}>
                            <Link to="/login" className="btn btn-primary">登录后兑换</Link>
                            <Link to="/register" className="btn btn-secondary">先注册</Link>
                        </div>
                    )}
                </div>
        </div>
    )

    if (isLoggedIn) {
        return (
            <AppShell title="美金额度充值">
                {pageContent}
            </AppShell>
        )
    }

    return (
        <div className="page-wrapper">
            <div className="container">
                <div className="page-header">
                    <h1 className="page-title">美金额度充值</h1>
                </div>
                {pageContent}
            </div>
        </div>
    )
}

function BillingSnapshot({ billingState, loading, error, onRefresh }) {
    const subscription = billingState?.subscription || {}
    const trafficPacks = billingState?.traffic_packs || {}
    const legacyBalance = billingState?.legacy_balance || {}
    const creditWallet = billingState?.credit_wallet || billingState?.credit_balance || {}
    const available = billingState?.available || {}
    const packItems = (trafficPacks.items || []).filter((pack) => isUsableLegacyPack(pack))
    const hasLegacyRights = subscription.active || packItems.length > 0 || Number(legacyBalance.remaining_cents || 0) !== 0

    return (
        <div className="billing-snapshot glass-card animate-fade-in-up">
            <div className="billing-snapshot-head">
                <div>
                    <span className="recharge-kicker">Account</span>
                    <h3>当前额度</h3>
                </div>
                <button className="btn btn-secondary btn-sm" onClick={onRefresh} disabled={loading}>
                    {loading ? '刷新中...' : '刷新'}
                </button>
            </div>
            {error ? (
                <div className="billing-snapshot-error">{error}</div>
            ) : (
                <>
                    <div className="billing-pool-grid billing-primary-grid">
                        <div className="billing-pool">
                            <span>永久美金额度</span>
                            <strong>{formatUsd(creditWallet.remaining_cents)}</strong>
                            <small>永久有效，不过期；再次购买会自动叠加</small>
                        </div>
                        <div className="billing-pool billing-pool-total">
                            <span>当前可用美金额度</span>
                            <strong>{formatUsd(available.remaining_cents)}</strong>
                            <small>已包含仍可用的历史权益与余额调整</small>
                        </div>
                    </div>
                    {hasLegacyRights && (
                        <div className="legacy-rights">
                            <div className="legacy-rights-head">
                                <div>
                                    <span className="recharge-kicker">Existing Benefits</span>
                                    <h4>已有权益</h4>
                                </div>
                                <p>仅展示已拥有的历史权益；用完或到期后不再售卖，本区没有购买按钮。</p>
                            </div>
                            <div className="legacy-rights-list">
                                {subscription.active && (
                                    <div className="legacy-right-item">
                                        <div>
                                            <strong>{subscription.plan_name || '原月卡权益'}</strong>
                                            <small>到期 {formatDateTime(subscription.paid_until)}</small>
                                        </div>
                                        <span>{formatUsd(subscription.remaining_cents)}</span>
                                    </div>
                                )}
                                {packItems.map((pack) => (
                                    <div className="legacy-right-item" key={pack.id || `${pack.product_id}-${pack.created_at}`}>
                                        <div>
                                            <strong>{pack.product_name || pack.product_id || '历史流量包'}</strong>
                                            <small>{pack.expires_at ? `到期 ${formatDateTime(pack.expires_at)}` : (pack.status || '历史记录')}</small>
                                        </div>
                                        <span>{formatUsd(pack.remaining_cents)}</span>
                                    </div>
                                ))}
                                {Number(legacyBalance.remaining_cents || 0) !== 0 && (
                                    <div className="legacy-right-item">
                                        <div>
                                            <strong>历史余额调整</strong>
                                            <small>保留原账户记录，并计入当前可用总额</small>
                                        </div>
                                        <span>{formatUsd(legacyBalance.remaining_cents)}</span>
                                    </div>
                                )}
                            </div>
                        </div>
                    )}
                </>
            )}
        </div>
    )
}

function ProductCard({ product, selected, onSelect, buttonRef, tabIndex, onKeyDown, delay = 0 }) {
    return (
        <button
            type="button"
            role="radio"
            aria-checked={selected}
            ref={buttonRef}
            tabIndex={tabIndex}
            className={`recharge-plan glass-card animate-fade-in-up ${selected ? 'plan-selected' : ''} ${product.highlight ? 'plan-popular' : ''}`}
            style={{ animationDelay: `${delay}ms` }}
            onClick={onSelect}
            onKeyDown={onKeyDown}
        >
            {product.badge && <div className="pricing-badge">{product.badge}</div>}
            <div className="plan-header">
                <h3>{product.name}</h3>
                <div className="plan-price">
                    <span className="plan-amount">{product.price}</span>
                    {product.priceNote && <span className="plan-note">{product.priceNote}</span>}
                </div>
                <div className="plan-balance-label">{product.balanceLabel}</div>
                <div className="plan-unit-label">{product.unitLabel}</div>
            </div>
            <ul className="plan-features">
                {product.features.map((feature) => (
                    <li key={feature}>&#10003; {feature}</li>
                ))}
            </ul>
            {selected && (
                <div className="plan-check">
                    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="var(--accent-emerald)" strokeWidth="3"><polyline points="20 6 9 17 4 12" /></svg>
                </div>
            )}
        </button>
    )
}
