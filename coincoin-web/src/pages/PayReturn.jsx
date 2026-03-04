import { useState, useEffect, useRef } from 'react'
import { Link, useLocation, useNavigate } from 'react-router-dom'
import { confirmOrder, getApiKey } from '../api/client'
import './PayReturn.css'

const MAX_ATTEMPTS = 300
const POLL_INTERVAL = 1000

export default function PayReturn() {
    const [status, setStatus] = useState('checking')
    const [orderInfo, setOrderInfo] = useState(null)
    const [confirmResult, setConfirmResult] = useState(null)
    const [countdown, setCountdown] = useState(5)
    const [attempt, setAttempt] = useState(0)
    const navigate = useNavigate()
    const location = useLocation()
    const timerRef = useRef(null)
    const cancelledRef = useRef(false)

    useEffect(() => {
        if (status !== 'success') return
        timerRef.current = setInterval(() => {
            setCountdown(prev => {
                if (prev <= 1) {
                    clearInterval(timerRef.current)
                    navigate('/dashboard')
                    return 0
                }
                return prev - 1
            })
        }, 1000)
        return () => clearInterval(timerRef.current)
    }, [status, navigate])

    useEffect(() => {
        const stored = localStorage.getItem('coincoin_last_order')
        const qs = new URLSearchParams(location.search || '')
        const orderNoFromQuery = qs.get('order_no') || qs.get('out_trade_no') || ''

        let order = null
        if (stored) {
            try { order = JSON.parse(stored) } catch { order = null }
        }
        if (!order && orderNoFromQuery) {
            order = { orderNo: orderNoFromQuery, planName: '', money: '' }
            // Save it so Dashboard can also auto-confirm later if needed.
            localStorage.setItem('coincoin_last_order', JSON.stringify(order))
        }

        if (!order) {
            setStatus('failed')
            return
        }

        setOrderInfo(order)

        let attempts = 0

        const poll = async () => {
            if (cancelledRef.current) return
            attempts++
            setAttempt(attempts)

            if (!getApiKey()) {
                setStatus('need_login')
                return
            }

            try {
                const result = await confirmOrder(order.orderNo)

                if (result.success) {
                    setConfirmResult(result)
                    setStatus('success')
                    localStorage.removeItem('coincoin_last_order')
                    return
                }

                if (result.message === 'order already confirmed' || result.detail === 'order already confirmed') {
                    setConfirmResult(result)
                    setStatus('success')
                    localStorage.removeItem('coincoin_last_order')
                    return
                }
            } catch {
                // 402 = not paid yet, other errors = transient, both should retry
            }

            if (attempts < MAX_ATTEMPTS) {
                setTimeout(poll, POLL_INTERVAL)
            } else {
                setStatus('timeout')
            }
        }

        poll()
        return () => { cancelledRef.current = true }
    }, [location.search])

    return (
        <div className="page-wrapper pay-return-page">
            <div className="container">
                <div className="pay-result glass-card animate-fade-in-up">
                    {status === 'checking' && (
                        <div className="result-content">
                            <div className="loading-spinner" style={{ width: 48, height: 48 }}></div>
                            <h2>正在确认支付...</h2>
                            <p>请稍候，正在向支付平台确认你的付款状态（剩余 {MAX_ATTEMPTS - attempt} 秒）</p>
                        </div>
                    )}
                    {status === 'success' && (
                        <div className="result-content">
                            <div className="result-icon success">&#10003;</div>
                            <h2>充值成功！</h2>
                            {confirmResult?.added_cents != null ? (
                                <div className="order-detail">
                                    <span>充值金额：+${(confirmResult.added_cents / 100).toFixed(2)}</span>
                                    <span>当前余额：${confirmResult.new_balance_usd?.toFixed(2)}</span>
                                </div>
                            ) : orderInfo && (
                                <div className="order-detail">
                                    <span>订单：{orderInfo.planName}</span>
                                    <span>金额：&#165;{orderInfo.money}</span>
                                </div>
                            )}
                            <p className="redirect-hint">{countdown} 秒后自动跳转到仪表盘...</p>
                            <div className="result-actions">
                                <Link to="/dashboard" className="btn btn-primary">立即前往仪表盘</Link>
                                <Link to="/usage" className="btn btn-secondary">查看用量</Link>
                            </div>
                        </div>
                    )}
                    {status === 'need_login' && (
                        <div className="result-content">
                            <div className="result-icon failed">!</div>
                            <h2>需要登录确认</h2>
                            <p>检测到支付回跳，但当前浏览器未登录（没有 API Key）。请先登录后再查看余额，或稍后等待系统自动到账。</p>
                            <div className="result-actions">
                                <Link to="/login" className="btn btn-primary">去登录</Link>
                                <Link to="/dashboard" className="btn btn-secondary">查看仪表盘</Link>
                            </div>
                        </div>
                    )}
                    {(status === 'failed' || status === 'timeout') && (
                        <div className="result-content">
                            <div className="result-icon failed">!</div>
                            <h2>{status === 'failed' ? '未找到订单' : '确认超时'}</h2>
                            <p>{status === 'failed'
                                ? '未找到待确认的订单信息'
                                : '未能确认支付结果，余额可能稍后自动到账。你可以在仪表盘查看余额。'
                            }</p>
                            <div className="result-actions">
                                <Link to="/dashboard" className="btn btn-primary">查看余额</Link>
                                <Link to="/recharge" className="btn btn-secondary">返回充值</Link>
                            </div>
                        </div>
                    )}
                </div>
            </div>
        </div>
    )
}
