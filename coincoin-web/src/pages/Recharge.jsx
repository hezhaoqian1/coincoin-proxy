import { useState, useRef, useEffect, useCallback } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { PRICING_PLANS, redeemCode, getApiKey, confirmOrder } from '../api/client'
import useOrderConfirm from '../hooks/useOrderConfirm'
import './Recharge.css'

const POLL_INTERVAL = 3000
const MAX_POLL_ATTEMPTS = 200

export default function Recharge() {
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

    const startPolling = useCallback((orderNo, planName, money) => {
        setPolling(true)
        setPollInfo({ planName, money })
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
        const plan = useCustom ? null : PRICING_PLANS[selectedPlan]
        const planMoney = useCustom ? parseFloat(customAmount).toFixed(2) : plan.money
        if (!planMoney || parseFloat(planMoney) <= 0) return

        const planName = useCustom ? `自定义充值 ¥${customAmount}` : `${plan.name} 套餐`

        setLoading(true)
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
                return
            }
            if (res.pay_url && res.order_no) {
                localStorage.setItem('coincoin_last_order', JSON.stringify({
                    orderNo: res.order_no,
                    planName,
                    money: planMoney
                }))
                window.open(res.pay_url, '_blank')
                startPolling(res.order_no, planName, planMoney)
            } else {
                alert(res.detail || '创建订单失败，请重试')
            }
        } catch (e) {
            alert('网络错误: ' + (e.message || '请检查网络连接后重试'))
        } finally {
            setLoading(false)
        }
    }

    const handleRedeem = async () => {
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
                    <p className="page-desc">选择套餐或自定义金额充值，按量计费用多少扣多少</p>
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
                        <button className="btn btn-secondary btn-sm" onClick={() => { pollingRef.current = false; setPolling(false) }}>取消等待</button>
                    </div>
                ) : (
                    <div className="pay-action animate-fade-in-up" style={{ animationDelay: '400ms' }}>
                        <button
                            className="btn btn-primary btn-lg pay-btn"
                            onClick={handlePay}
                            disabled={loading || (useCustom && (!customAmount || parseFloat(customAmount) <= 0))}
                        >
                            {loading ? '创建订单中...' : (
                                <>
                                    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><rect x="1" y="4" width="22" height="16" rx="2" ry="2" /><line x1="1" y1="10" x2="23" y2="10" /></svg>
                                    {useCustom
                                        ? `支付宝支付 ¥${customAmount || '0'}`
                                        : `支付宝支付 ${PRICING_PLANS[selectedPlan].price}`
                                    }
                                </>
                            )}
                        </button>
                        <p className="pay-note">点击后将在新窗口打开支付宝，支付完成后自动到账</p>
                    </div>
                )}

                <div className="redeem-section glass-card animate-fade-in-up" style={{ animationDelay: '500ms' }}>
                    <h3>兑换码充值</h3>
                    <p className="redeem-desc">输入兑换码立即充值到账</p>
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
                </div>
            </div>
        </div>
    )
}
