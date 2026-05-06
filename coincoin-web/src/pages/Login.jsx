import { useState } from 'react'
import { useNavigate, Link } from 'react-router-dom'
import { useAuth } from '../hooks/useAuth'
import './Auth.css'

export default function Login() {
    const [tab, setTab] = useState('password')
    const [username, setUsername] = useState('')
    const [password, setPassword] = useState('')
    const [key, setKey] = useState('')
    const [error, setError] = useState('')
    const { login, loginWithPassword, loading } = useAuth()
    const navigate = useNavigate()

    const handlePasswordLogin = async (e) => {
        e.preventDefault()
        if (!username.trim()) { setError('请输入邮箱或用户名'); return }
        if (!password) { setError('请输入密码'); return }
        const result = await loginWithPassword(username.trim(), password)
        if (result.success) {
            navigate('/dashboard')
        } else {
            setError(result.error)
        }
    }

    const handleKeyLogin = async (e) => {
        e.preventDefault()
        if (!key.trim()) { setError('请输入 API Key'); return }
        const result = await login(key.trim())
        if (result.success) {
            navigate('/dashboard')
        } else {
            setError(result.error)
        }
    }

    const clearError = () => setError('')

    return (
        <div className="auth-page page-wrapper">
            <div className="auth-bg">
                <div className="auth-grid"></div>
            </div>
            <div className="auth-card glass-card animate-fade-in-up">
                <div className="auth-header">
                    <div className="logo-icon" style={{ width: 48, height: 48, fontSize: '1.1rem', borderRadius: 14 }}>CF</div>
                    <h1>登录控制台</h1>
                </div>

                <div className="auth-tabs">
                    <button
                        className={`auth-tab ${tab === 'password' ? 'active' : ''}`}
                        onClick={() => { setTab('password'); clearError() }}
                    >控制台登录</button>
                    <button
                        className={`auth-tab ${tab === 'key' ? 'active' : ''}`}
                        onClick={() => { setTab('key'); clearError() }}
                    >已有开发者 Key</button>
                </div>

                {tab === 'password' ? (
                    <form onSubmit={handlePasswordLogin} className="auth-form">
                        <div className="input-group">
                            <label>邮箱或用户名</label>
                            <input
                                type="text"
                                className="input-field"
                                placeholder="输入邮箱或控制台用户名"
                                value={username}
                                onChange={(e) => { setUsername(e.target.value); clearError() }}
                                autoFocus
                            />
                        </div>
                        <div className="input-group">
                            <label>密码</label>
                            <input
                                type="password"
                                className="input-field"
                                placeholder="输入密码"
                                value={password}
                                onChange={(e) => { setPassword(e.target.value); clearError() }}
                            />
                            {error && <span className="input-error">{error}</span>}
                        </div>
                        <button type="submit" className="btn btn-primary" style={{ width: '100%' }} disabled={loading}>
                            {loading ? '登录中...' : '进入控制台'}
                        </button>
                    </form>
                ) : (
                    <form onSubmit={handleKeyLogin} className="auth-form">
                        <div className="auth-callout auth-callout-muted">
                            <strong>这个入口只做 Key 验证</strong>
                            <p>适合已经拿到开发者 Key 的用户。第一次来站，先注册或登录控制台账号。</p>
                        </div>
                        <div className="input-group">
                            <label>开发者 Key</label>
                            <input
                                type="password"
                                className="input-field"
                                placeholder="sk_cc_xxxxxxxxxx"
                                value={key}
                                onChange={(e) => { setKey(e.target.value); clearError() }}
                                autoFocus
                            />
                            {error && <span className="input-error">{error}</span>}
                        </div>
                        <button type="submit" className="btn btn-primary" style={{ width: '100%' }} disabled={loading}>
                            {loading ? '验证中...' : '验证并进入'}
                        </button>
                        <p className="auth-helper-text">
                            没有开发者 Key 时，用控制台账号登录。
                        </p>
                    </form>
                )}

                <p className="auth-footer-text">
                    还没有控制台账号？<Link to="/register">立即注册</Link>
                </p>
            </div>
        </div>
    )
}
