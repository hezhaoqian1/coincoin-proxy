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
    const { login, loginWithPassword, loginDemo, loading } = useAuth()
    const navigate = useNavigate()

    const handlePasswordLogin = async (e) => {
        e.preventDefault()
        if (!username.trim()) { setError('请输入用户名'); return }
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

    const handleDemo = () => {
        loginDemo()
        navigate('/dashboard')
    }

    const clearError = () => setError('')

    return (
        <div className="auth-page page-wrapper">
            <div className="auth-bg">
                <div className="hero-orb hero-orb-1"></div>
                <div className="hero-orb hero-orb-2"></div>
            </div>
            <div className="auth-card glass-card animate-fade-in-up">
                <div className="auth-header">
                    <div className="logo-icon" style={{ width: 48, height: 48, fontSize: '1.1rem', borderRadius: 14 }}>CC</div>
                    <h1>欢迎回来</h1>
                    <p>先决定你要进入控制台，还是只验证一个开发者 API Key</p>
                </div>

                <div className="auth-tabs">
                    <button
                        className={`auth-tab ${tab === 'password' ? 'active' : ''}`}
                        onClick={() => { setTab('password'); clearError() }}
                    >控制台登录</button>
                    <button
                        className={`auth-tab ${tab === 'key' ? 'active' : ''}`}
                        onClick={() => { setTab('key'); clearError() }}
                    >开发者 Key 直登</button>
                </div>

                {tab === 'password' ? (
                    <form onSubmit={handlePasswordLogin} className="auth-form">
                        <div className="auth-callout">
                            <strong>适合需要站内管理的人</strong>
                            <p>控制台登录可以看余额、充值、查看用量，并在仪表盘生成开发者 API Key。</p>
                        </div>
                        <div className="input-group">
                            <label>用户名</label>
                            <input
                                type="text"
                                className="input-field"
                                placeholder="输入控制台用户名"
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
                            <strong>适合已经拿到开发者 Key 的人</strong>
                            <p>这个入口只验证你的开发者 API Key 是否可用，不会自动拥有控制台账号管理能力。</p>
                        </div>
                        <div className="input-group">
                            <label>开发者 API Key</label>
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
                    </form>
                )}

                <div className="auth-divider">
                    <span>或者</span>
                </div>

                <button onClick={handleDemo} className="btn btn-secondary" style={{ width: '100%' }}>
                    先看 Demo
                </button>

                <p className="auth-footer-text">
                    还没有控制台账号？<Link to="/register">立即注册</Link>
                </p>
            </div>
        </div>
    )
}
