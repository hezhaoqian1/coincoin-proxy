import { useState } from 'react'
import { useNavigate, Link } from 'react-router-dom'
import {
    clearGeneratedKey,
    registerUser,
    resendVerification,
    setApiKey,
    setUserId,
    setUsername as storeUsername,
    verifyEmail,
} from '../api/client'
import './Auth.css'

export default function Register() {
    const [username, setUsername] = useState('')
    const [email, setEmail] = useState('')
    const [password, setPassword] = useState('')
    const [confirmPw, setConfirmPw] = useState('')
    const [referralCode, setReferralCode] = useState(() => {
        const params = new URLSearchParams(window.location.search)
        return params.get('ref') || ''
    })
    const [pending, setPending] = useState(null)
    const [code, setCode] = useState('')
    const [message, setMessage] = useState('')
    const [error, setError] = useState('')
    const [loading, setLoading] = useState(false)
    const [resending, setResending] = useState(false)
    const navigate = useNavigate()

    const completeLogin = (data) => {
        clearGeneratedKey()
        setApiKey(data.session_key)
        setUserId(data.user_id)
        storeUsername(data.username)
        navigate('/dashboard')
    }

    const handleSubmit = async (e) => {
        e.preventDefault()
        if (!username.trim()) { setError('请输入用户名'); return }
        if (!email.trim()) { setError('请输入邮箱'); return }
        if (password.length < 6) { setError('密码至少 6 位'); return }
        if (password !== confirmPw) { setError('两次密码不一致'); return }

        setLoading(true)
        setError('')
        setMessage('')
        try {
            const data = await registerUser(username.trim(), email.trim(), password, referralCode.trim() || undefined)
            if (data.session_key) {
                completeLogin(data)
                return
            }
            setPending(data)
            setMessage('验证码已发送')
        } catch (err) {
            setError(err.message || '注册失败，请重试')
        } finally {
            setLoading(false)
        }
    }

    const handleVerify = async (e) => {
        e.preventDefault()
        if (!pending?.user_id) { setError('请先注册账号'); return }
        if (!code.trim()) { setError('请输入验证码'); return }

        setLoading(true)
        setError('')
        setMessage('')
        try {
            const data = await verifyEmail(pending.user_id, code.trim())
            completeLogin(data)
        } catch (err) {
            setError(err.message || '验证失败')
        } finally {
            setLoading(false)
        }
    }

    const handleResend = async () => {
        if (!pending?.user_id) return
        setResending(true)
        setError('')
        setMessage('')
        try {
            const data = await resendVerification(pending.user_id)
            setPending((prev) => ({ ...prev, ...data }))
            setMessage('已重新发送')
        } catch (err) {
            setError(err.message || '发送失败')
        } finally {
            setResending(false)
        }
    }

    return (
        <div className="auth-page page-wrapper">
            <div className="auth-bg">
                <div className="auth-grid"></div>
            </div>
            <div className="auth-card glass-card animate-fade-in-up">
                <div className="auth-header">
                    <div className="logo-icon" style={{ width: 48, height: 48, fontSize: '1.1rem', borderRadius: 14 }}>CF</div>
                    <h1>{pending ? '验证邮箱' : '创建控制台账号'}</h1>
                    <p>{pending ? '输入邮箱里的验证码，验证后进入控制台。' : '注册后先验证邮箱。开发者 Key 在概览页生成。'}</p>
                </div>

                {pending ? (
                    <form onSubmit={handleVerify} className="auth-form">
                        <div className="auth-callout auth-callout-muted">
                            <strong>{pending.email}</strong>
                            <p>验证码 10 分钟内有效。</p>
                        </div>
                        <div className="input-group">
                            <label>验证码</label>
                            <input
                                type="text"
                                inputMode="numeric"
                                className="input-field verification-code-input"
                                placeholder="6 位数字"
                                value={code}
                                onChange={(e) => { setCode(e.target.value.replace(/\D/g, '').slice(0, 6)); setError(''); setMessage('') }}
                                autoFocus
                            />
                            {error && <span className="input-error">{error}</span>}
                            {message && <span className="input-success">{message}</span>}
                        </div>
                        <button type="submit" className="btn btn-primary" style={{ width: '100%' }} disabled={loading}>
                            {loading ? '验证中...' : '验证并进入控制台'}
                        </button>
                        <button type="button" className="btn btn-secondary" style={{ width: '100%' }} onClick={handleResend} disabled={resending || loading}>
                            {resending ? '发送中...' : '重新发送验证码'}
                        </button>
                        <p className="auth-footer-text">
                            邮箱填错了？<button type="button" className="auth-link-button" onClick={() => { setPending(null); setCode(''); setError(''); setMessage('') }}>返回修改</button>
                        </p>
                    </form>
                ) : (
                    <form onSubmit={handleSubmit} className="auth-form">
                        <div className="input-group">
                            <label>用户名</label>
                            <input
                                type="text"
                                className="input-field"
                                placeholder="字母、数字、下划线、连字符"
                                value={username}
                                onChange={(e) => { setUsername(e.target.value); setError('') }}
                                autoFocus
                            />
                        </div>
                        <div className="input-group">
                            <label>邮箱</label>
                            <input
                                type="email"
                                className="input-field"
                                placeholder="name@example.com"
                                value={email}
                                onChange={(e) => { setEmail(e.target.value); setError('') }}
                            />
                        </div>
                        <div className="input-group">
                            <label>密码</label>
                            <input
                                type="password"
                                className="input-field"
                                placeholder="至少 6 位"
                                value={password}
                                onChange={(e) => { setPassword(e.target.value); setError('') }}
                            />
                        </div>
                        <div className="input-group">
                            <label>确认密码</label>
                            <input
                                type="password"
                                className="input-field"
                                placeholder="再输入一次密码"
                                value={confirmPw}
                                onChange={(e) => { setConfirmPw(e.target.value); setError('') }}
                            />
                        </div>
                        <div className="input-group">
                            <label>邀请码 <span style={{ color: 'var(--text-tertiary)', fontWeight: 400 }}>（选填）</span></label>
                            <input
                                type="text"
                                className="input-field"
                                placeholder="有邀请码就填，没有可留空"
                                value={referralCode}
                                onChange={(e) => setReferralCode(e.target.value.toUpperCase())}
                                style={{ textTransform: 'uppercase', letterSpacing: '0.1em' }}
                            />
                            {error && <span className="input-error">{error}</span>}
                            {message && <span className="input-success">{message}</span>}
                        </div>

                        <button type="submit" className="btn btn-primary" style={{ width: '100%' }} disabled={loading}>
                            {loading ? '创建中...' : '创建账号'}
                        </button>

                        <p className="auth-helper-text">
                            使用主流邮箱，后续找回账号和风控都靠它。
                        </p>

                        <p className="auth-footer-text">
                            已有控制台账号？<Link to="/login">去登录</Link>
                        </p>
                    </form>
                )}
            </div>
        </div>
    )
}
