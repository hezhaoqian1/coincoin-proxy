import { useState, useRef, useEffect, useCallback } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { PRICING_PLANS, redeemCode, getApiKey, confirmOrder } from '../api/client'
import useOrderConfirm from '../hooks/useOrderConfirm'
import { useAuth } from '../hooks/useAuth'
import './Recharge.css'

const POLL_INTERVAL = 3000
const MAX_POLL_ATTEMPTS = 200

export default function Recharge() {
    const { isLoggedIn } = useAuth()
    const [selectedPlan, setSelectedPlan] = useState(2)
    const [customAmount, setCustomAmount] = useState('')
    const [useCustom, setUseCustom] = useState(false)
    const [loading, setLoading] = useState(false)

    const [redeemInput, setRedeemInput] = useState('')
    const [redeemLoading, setRedeemLoading] = useState(false)
    const [redeemMsg, setRedeemMsg] = useState(null)
    const { confirmResult: orderConfirmed, dismiss: dismissOrder } = useOrderConfirm()

    const [polling, setPolling] = useState(false)
    const [pollInfo, setPollInfo] = useState(null)
    const [payResult, setPayResult] = useState(null)
    const pollingRef = useRef(false)
    const [autoRedirect, setAutoRedirect] = useState(5)
    const [popupBlocked, setPopupBlocked] = useState(false)
    const navigate = useNavigate()

    useEffect(() => {
        return () => { pollingRef.current = false }
    }, [])

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
                    document.title = '\u2705 \u5145\u503c\u6210\u529f\uff01'
                    setTimeout(() => { document.title = 'CoinCoin' }, 8000)
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
    }, [])

    const handlePay = async () => {
        if (!isLoggedIn) {
            navigate('/login')
            return
        }

        const plan = useCustom ? null : PRICING_PLANS[selectedPlan]
        const planMoney = useCustom ? parseFloat(customAmount).toFixed(2) : plan.money
        if (!planMoney || parseFloat(planMoney) <= 0) return

        const planName = useCustom ? `自定义充值 ¥${customAmount}` : `${plan.name} 套餐`

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
                body: JSON.stringify({ name: planName, money: planMoney, pay_type: 'alipay' })
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
                setRedeemMsg({ type: 'success', text: `兑换成功！充值 $${(res.added_cents / 100).toFixed(2)}，当前余额 $${res.new_balance_usd.toFixed(2)}` })
                setRedeemInput('')
            } else {
                setRedeemMsg({ type: 'error', text: res.detail || res.message || '兑换失败' })
            }
        } catch {
            setRedeemMsg({ type: 'error', text: '网络错误，请重试' })
        } finally {
            setRedeemLoading(false)
        }
    }

    return (
        <div className="page-wrapper">
            <div className="container">
                <div className="page-header">
                    <h1 className="page-title">充值中心</h1>
                    <p className="page-desc">给账户充值，之后所有文本和图片请求都从这里统一扣费。</p>
                </div>

                {!isLoggedIn && (
                    <div className="glass-card animate-fade-in" style={{ padding: 'var(--space-lg)', marginBottom: 'var(--space-lg)', background: 'rgba(99,102,241,0.08)', border: '1px solid rgba(99,102,241,0.2)' }}>
                        <div style={{ display: 'flex', justifyContent: 'space-between', gap: 'var(--space-md)', alignItems: 'center', flexWrap: 'wrap' }}>
                            <div>
                                <strong style={{ display: 'block', marginBottom: 'var(--space-xs)' }}>可以先看套餐，支付前再登录</strong>
                                <span style={{ color: 'var(--text-secondary)' }}>未登录也能查看充值页；创建订单和兑换码到账仍需要先登录，这样余额才会进到正确账户。</span>
                            </div>
                            <div style={{ display: 'flex', gap: 'var(--space-sm)', flexWrap: 'wrap' }}>
                                <Link to="/login" className="btn btn-primary btn-sm">登录后充值</Link>
                                <Link to="/register" className="btn btn-secondary btn-sm">注册账号</Link>
                            </div>
                        </div>
                    </div>
                )}

                <div className="recharge-overview glass-card animate-fade-in-up">
                    <div className="recharge-overview-copy">
                        <span className="recharge-kicker">Billing</span>
                        <h2>一个余额，覆盖全部公开模型</h2>
                        <p>GPT 文本、Gemini 文本和 Gemini 生图统一从 CoinCoin 余额扣费，不需要分开维护。</p>
                    </div>
                    <div className="recharge-overview-points">
                        <div className="recharge-point">
                            <strong>先选套餐</strong>
                            <p>大多数情况直接选预设套餐就够了，到账额度也更直观。</p>
                        </div>
                        <div className="recharge-point">
                            <strong>再去支付</strong>
                            <p>点击后会在新标签打开支付宝，本页自动轮询到账结果。</p>
                        </div>
                        <div className="recharge-point">
                            <strong>到账后继续操作</strong>
                            <p>支付完成后可以直接回仪表盘、请求日志或接入配置页。</p>
                        </div>
                    </div>
                </div>

                <div className="recharge-path-grid">
                    <div className="recharge-path-card glass-card animate-fade-in-up">
                        <span className="recharge-path-label">套餐充值</span>
                        <strong>选择套餐充值</strong>
                        <p>直接选金额档位并支付，适合第一次充值或快速补余额。</p>
                    </div>
                    <div className="recharge-path-card glass-card animate-fade-in-up" style={{ animationDelay: '80ms' }}>
                        <span className="recharge-path-label">兑换码</span>
                        <strong>兑换码到账</strong>
                        <p>适合活动码、管理员发放额度或内部测试。无需支付，兑换后即时到账。</p>
                    </div>
                </div>

                {orderConfirmed && (
                    <div className="glass-card animate-fade-in" style={{ padding: 'var(--space-lg)', marginBottom: 'var(--space-lg)', background: 'rgba(16,185,129,0.08)', border: '1px solid rgba(16,185,129,0.25)' }}>
                        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                            <span style={{ color: 'var(--accent-emerald)' }}>&#10003; 上笔充值已到账！+${(orderConfirmed.added_cents / 100).toFixed(2)}，当前余额 ${orderConfirmed.new_balance_usd?.toFixed(2)}</span>
                            <button className="btn btn-sm btn-secondary" onClick={dismissOrder}>知道了</button>
                        </div>
                    </div>
                )}

                <div className="recharge-plans stagger-children">
                    {PRICING_PLANS.map((plan, i) => (
                        <div
                            key={i}
                            className={`recharge-plan glass-card animate-fade-in-up ${selectedPlan === i && !useCustom ? 'plan-selected' : ''} ${plan.highlight ? 'plan-popular' : ''}`}
                            onClick={() => { setSelectedPlan(i); setUseCustom(false) }}
                        >
                            {plan.badge && <div className="pricing-badge">{plan.badge}</div>}
                            <div className="plan-header">
                                <h3>{plan.name}</h3>
                                <div className="plan-price">
                                    <span className="plan-amount">{plan.price}</span>
                                    {plan.priceNote && <span className="plan-note">{plan.priceNote}</span>}
                                </div>
                                <div className="plan-balance-label">{plan.balanceLabel}</div>
                            </div>
                            <ul className="plan-features">
                                {plan.features.map((f, j) => (
                                    <li key={j}>&#10003; {f}</li>
                                ))}
                            </ul>
                            {selectedPlan === i && !useCustom && (
                                <div className="plan-check">
                                    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="var(--accent-emerald)" strokeWidth="3"><polyline points="20 6 9 17 4 12" /></svg>
                                </div>
                            )}
                        </div>
                    ))}
                </div>

                <div className="custom-amount glass-card animate-fade-in-up" style={{ animationDelay: '300ms' }}>
                    <div className="custom-header">
                        <label className="custom-toggle">
                            <input type="checkbox" checked={useCustom} onChange={(e) => setUseCustom(e.target.checked)} />
                            <span className="toggle-slider"></span>
                        </label>
                        <span>自定义充值金额</span>
                    </div>
                    {useCustom && (
                        <div className="custom-input-row animate-fade-in">
                            <span className="currency-sign">&#165;</span>
                            <input
                                type="number"
                                className="input-field custom-input"
                                placeholder="输入金额"
                                value={customAmount}
                                onChange={(e) => setCustomAmount(e.target.value)}
                                min="1"
                                step="0.01"
                            />
                            {customAmount && parseFloat(customAmount) > 0 && (
                                <span className="custom-hint">&#8776; ${(parseFloat(customAmount) * 0.14).toFixed(2)} 余额（选套餐更划算！）</span>
                            )}
                        </div>
                    )}
                </div>

                {payResult ? (
                    <div className="glass-card animate-fade-in" style={{ padding: 'var(--space-xl)', textAlign: 'center', marginBottom: 'var(--space-lg)' }}>
                        <div style={{ fontSize: '3rem', marginBottom: 'var(--space-md)' }}>&#10003;</div>
                        <h2 style={{ color: 'var(--accent-emerald)', marginBottom: 'var(--space-sm)' }}>充值成功！</h2>
                        <p style={{ fontSize: '1.1rem', marginBottom: 'var(--space-md)' }}>
                            +${(payResult.added_cents / 100).toFixed(2)} 已到账，当前余额 ${payResult.new_balance_usd?.toFixed(2)}
                        </p>
                        <p style={{ color: 'var(--text-secondary)', fontSize: '0.9rem', marginBottom: 'var(--space-md)' }}>
                            {autoRedirect} 秒后自动跳转到仪表盘...
                        </p>
                        <div style={{ display: 'flex', gap: 'var(--space-md)', justifyContent: 'center' }}>
                            <button className="btn btn-primary" onClick={() => navigate('/dashboard')}>立即前往仪表盘</button>
                            <button className="btn btn-secondary" onClick={() => { setPayResult(null); setAutoRedirect(5) }}>继续充值</button>
                        </div>
                    </div>
                ) : polling ? (
                    <div className="glass-card animate-fade-in" style={{ padding: 'var(--space-xl)', textAlign: 'center', marginBottom: 'var(--space-lg)' }}>
                        <div className="loading-spinner" style={{ width: 48, height: 48, margin: '0 auto var(--space-md)' }}></div>
                        <h2 style={{ marginBottom: 'var(--space-sm)' }}>等待支付完成...</h2>
                        <p style={{ color: 'var(--text-secondary)', marginBottom: 'var(--space-sm)' }}>
                            {pollInfo?.planName} ¥{pollInfo?.money}
                        </p>
                        <p style={{ color: 'var(--accent-amber)', fontSize: '0.9rem', marginBottom: 'var(--space-md)', background: 'rgba(245,158,11,0.08)', padding: 'var(--space-sm) var(--space-md)', borderRadius: 'var(--radius-sm)', border: '1px solid rgba(245,158,11,0.2)' }}>
                            请在新打开的标签页完成支付宝付款，支付成功后<strong>无需操作</strong>，本页面会自动检测到账并跳转
                        </p>
                        {popupBlocked && pollInfo?.payUrl && (
                            <div style={{ marginBottom: 'var(--space-md)' }}>
                                <p style={{ color: 'var(--accent-amber)', marginBottom: 'var(--space-sm)' }}>
                                    浏览器可能拦截了新窗口。请点击下方按钮在新标签打开支付页面，并保持此页面不要关闭。
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
                            disabled={(isLoggedIn && loading) || (useCustom && (!customAmount || parseFloat(customAmount) <= 0))}
                        >
                            {isLoggedIn && loading ? '创建订单中...' : (
                                <>
                                    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><rect x="1" y="4" width="22" height="16" rx="2" ry="2" /><line x1="1" y1="10" x2="23" y2="10" /></svg>
                                    {!isLoggedIn
                                        ? '登录后充值'
                                        : useCustom
                                        ? `支付宝支付 ¥${customAmount || '0'}`
                                        : `支付宝支付 ${PRICING_PLANS[selectedPlan].price}`
                                    }
                                </>
                            )}
                        </button>
                        <p className="pay-note">{isLoggedIn ? '点击后会在新标签打开支付页，到账后本页自动更新。' : '当前可以先看套餐；登录后才能创建订单并把余额充到你的账户。'}</p>
                        <div className="recharge-next-links">
                            {isLoggedIn ? (
                                <>
                                    <Link to="/dashboard" className="btn btn-ghost btn-sm">返回仪表盘</Link>
                                    <Link to="/usage" className="btn btn-ghost btn-sm">查看请求日志</Link>
                                    <Link to="/settings" className="btn btn-ghost btn-sm">去接入配置</Link>
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

                <div className="redeem-section glass-card animate-fade-in-up" style={{ animationDelay: '500ms' }}>
                    <h3>兑换码充值</h3>
                    <p className="redeem-desc">{isLoggedIn ? '输入兑换码后立即到账。' : '兑换码也需要先登录，避免充到错误账户。'}</p>
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
        </div>
    )
}
