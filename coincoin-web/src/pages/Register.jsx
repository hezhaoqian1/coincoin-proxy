import { useEffect, useMemo, useState } from 'react'
import { useNavigate, Link } from 'react-router-dom'
import {
    checkRegisterEmailCode,
    clearGeneratedKey,
    getStationContext,
    registerUser,
    sendRegisterEmailCode,
    setApiKey,
    setStationContext,
    setUserId,
    setUsername as storeUsername,
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
    const [stationContext] = useState(() => {
        const params = new URLSearchParams(window.location.search)
        const stationParam = (params.get('station') || '').trim().toLowerCase()
        const stored = getStationContext()
        return {
            slug: stationParam || stored.slug || '',
            displayName: stored.displayName || stationParam || '',
        }
    })
    const [code, setCode] = useState('')
    const [verificationId, setVerificationId] = useState('')
    const [verifiedEmail, setVerifiedEmail] = useState('')
    const [emailSent, setEmailSent] = useState(false)
    const [emailVerified, setEmailVerified] = useState(false)
    const [message, setMessage] = useState('')
    const [error, setError] = useState('')
    const [loading, setLoading] = useState(false)
    const [sendingCode, setSendingCode] = useState(false)
    const [verifyingCode, setVerifyingCode] = useState(false)
    const [resendCountdown, setResendCountdown] = useState(0)
    const navigate = useNavigate()

    const normalizedEmail = useMemo(() => email.trim().toLowerCase(), [email])
    const emailLocked = emailVerified && verifiedEmail === normalizedEmail
    const hasCodeReady = Boolean(verificationId && code.trim().length >= 4 && normalizedEmail)

    useEffect(() => {
        if (!stationContext.slug) return
        setStationContext({ slug: stationContext.slug, display_name: stationContext.displayName || stationContext.slug })
    }, [stationContext.slug, stationContext.displayName])

    const completeLogin = (data) => {
        clearGeneratedKey()
        setApiKey(data.session_key)
        setUserId(data.user_id)
        storeUsername(data.username)
        if (referralCode.trim()) {
            try {
                localStorage.setItem('coincoin_signup_bonus_message', '$10 API 额度已到账。充值后，还可以再拿 $20。')
            } catch { /* ignore */ }
        }
        try {
            localStorage.setItem('coincoin_recent_signup', '1')
        } catch { /* ignore */ }
        navigate('/dashboard')
    }

    const resetVerificationState = (nextEmail = '') => {
        const normalizedNextEmail = nextEmail.trim().toLowerCase()
        if (normalizedNextEmail === verifiedEmail && emailVerified) {
            return
        }
        setVerificationId('')
        setVerifiedEmail('')
        setEmailSent(false)
        setEmailVerified(false)
        setCode('')
        setResendCountdown(0)
    }

    useEffect(() => {
        if (resendCountdown <= 0) return undefined
        const timer = window.setTimeout(() => {
            setResendCountdown((value) => Math.max(0, value - 1))
        }, 1000)
        return () => window.clearTimeout(timer)
    }, [resendCountdown])

    const validateBeforeCode = () => {
        if (!normalizedEmail) {
            setError('请输入邮箱')
            return false
        }
        return true
    }

    const validateUsername = () => {
        const trimmed = username.trim()
        if (!trimmed) {
            setError('请输入用户名')
            return false
        }
        if (trimmed.length < 2 || trimmed.length > 64) {
            setError('用户名长度需为 2-64 位')
            return false
        }
        if (!/^[a-zA-Z0-9_.-]+$/.test(trimmed)) {
            setError('用户名只支持字母、数字、点、下划线、连字符')
            return false
        }
        return true
    }

    const handleSendCode = async () => {
        if (!validateBeforeCode()) return

        setSendingCode(true)
        setError('')
        setMessage('')
        try {
            const data = await sendRegisterEmailCode(normalizedEmail)
            setVerificationId(data.verification_id)
            setVerifiedEmail('')
            setEmailSent(true)
            setEmailVerified(false)
            setCode('')
            setResendCountdown(60)
            setMessage('验证码已发送')
        } catch (err) {
            setError(err.message || '发送失败')
        } finally {
            setSendingCode(false)
        }
    }

    const handleVerifyCode = async () => {
        if (!verificationId) {
            setError('请先发送验证码')
            return
        }
        if (!code.trim()) {
            setError('请输入验证码')
            return
        }

        setVerifyingCode(true)
        setError('')
        setMessage('')
        try {
            const data = await checkRegisterEmailCode(verificationId, code.trim())
            setVerificationId(data.verification_id)
            setVerifiedEmail(data.email)
            setEmailVerified(true)
            setEmailSent(true)
            setMessage('邮箱已验证')
        } catch (err) {
            setError(err.message || '验证失败')
        } finally {
            setVerifyingCode(false)
        }
    }

    const handleSubmit = async (e) => {
        e.preventDefault()
        if (!validateUsername()) return
        if (!normalizedEmail) { setError('请输入邮箱'); return }
        if (!verificationId) { setError('请先发送验证码'); return }
        if (!code.trim()) { setError('请输入验证码'); return }
        if (password.length < 6) { setError('密码至少 6 位'); return }
        if (password !== confirmPw) { setError('两次密码不一致'); return }

        setLoading(true)
        setError('')
        setMessage('')
        try {
            const data = await registerUser(
                username.trim(),
                normalizedEmail,
                password,
                referralCode.trim() || undefined,
                verificationId,
                code.trim(),
                stationContext.slug || undefined,
            )
            setVerifiedEmail(normalizedEmail)
            setEmailVerified(true)
            completeLogin(data)
        } catch (err) {
            setError(err.message || '注册失败，请重试')
        } finally {
            setLoading(false)
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
                    <h1>创建控制台账号</h1>
                    {stationContext.slug && <p>注册后会绑定到 {stationContext.displayName || stationContext.slug}。</p>}
                </div>

                <form onSubmit={handleSubmit} className="auth-form">
                    <div className="input-group">
                        <label>用户名</label>
                        <input
                            type="text"
                            className="input-field"
                            placeholder="字母、数字、点、下划线、连字符"
                            value={username}
                            onChange={(e) => { setUsername(e.target.value); setError('') }}
                            autoFocus
                        />
                    </div>

                    <div className="input-group">
                        <label>邮箱</label>
                        <div className="input-inline-group">
                            <div className="input-with-badge">
                                <input
                                    type="email"
                                    className="input-field"
                                    placeholder="name@example.com"
                                    value={email}
                                    onChange={(e) => {
                                        setEmail(e.target.value)
                                        setError('')
                                        setMessage('')
                                        resetVerificationState(e.target.value)
                                    }}
                                    disabled={emailLocked}
                                />
                                {emailLocked && <span className="verified-badge">已验证</span>}
                            </div>
                            <button
                                type="button"
                                className="btn btn-secondary auth-inline-button"
                                onClick={handleSendCode}
                                disabled={sendingCode || emailLocked || resendCountdown > 0}
                            >
                                {emailLocked
                                    ? '已验证'
                                    : sendingCode
                                        ? '发送中...'
                                        : resendCountdown > 0
                                            ? `${resendCountdown}s 后重发`
                                            : (emailSent ? '重新发送' : '发送验证码')}
                            </button>
                        </div>
                        {emailSent && !emailLocked && (
                            <span className="input-hint">
                                {resendCountdown > 0 ? `${resendCountdown} 秒后可重新发送` : '如果没收到，可以重新发送验证码'}
                            </span>
                        )}
                    </div>

                    <div className="input-group">
                        <label>验证码</label>
                        <div className="input-inline-group">
                            <input
                                type="text"
                                inputMode="numeric"
                                className="input-field verification-code-input"
                                placeholder="6 位数字"
                                value={code}
                                onChange={(e) => {
                                    setCode(e.target.value.replace(/\D/g, '').slice(0, 6))
                                    setError('')
                                    setMessage('')
                                }}
                                disabled={emailLocked}
                            />
                            <button
                                type="button"
                                className="btn btn-secondary auth-inline-button"
                                onClick={handleVerifyCode}
                                disabled={verifyingCode || emailLocked || !emailSent}
                            >
                                {emailLocked ? '已通过' : verifyingCode ? '验证中...' : '验证'}
                            </button>
                        </div>
                        {emailSent && !emailLocked && (
                            <span className="input-hint">填好验证码后，直接创建账号即可。</span>
                        )}
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
                        {referralCode.trim() && (
                            <span className="input-hint">你正在使用好友的邀请码。注册成功后，可获得 $10 API 额度。</span>
                        )}
                    </div>

                    {(error || message) && (
                        <div className="auth-status-stack">
                            {error && <span className="input-error">{error}</span>}
                            {message && <span className="input-success">{message}</span>}
                        </div>
                    )}

                    <button type="submit" className="btn btn-primary" style={{ width: '100%' }} disabled={loading || !hasCodeReady}>
                        {loading ? '创建中...' : '创建账号'}
                    </button>

                    <p className="auth-footer-text">
                        已有账号？<Link to="/login">去登录</Link>
                    </p>
                </form>
            </div>
        </div>
    )
}
