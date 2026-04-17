import { useEffect, useState } from 'react'
import { Link, useLocation, useNavigate } from 'react-router-dom'
import { useAuth } from '../hooks/useAuth'
import { useTheme } from '../hooks/useTheme'
import { getStationApplication } from '../api/client'
import './Navbar.css'

export default function Navbar() {
    const { authMode, hasDeveloperKey, isLoggedIn, logout, username } = useAuth()
    const { theme, toggleTheme } = useTheme()
    const location = useLocation()
    const navigate = useNavigate()
    const pricingTarget = '/recharge'
    const [hasStation, setHasStation] = useState(false)

    useEffect(() => {
        let active = true
        if (!isLoggedIn) {
            setHasStation(false)
            return undefined
        }
        getStationApplication()
            .then((data) => {
                if (!active) return
                setHasStation(Boolean(data?.station && data.station.status === 'active'))
            })
            .catch(() => {
                if (!active) return
                setHasStation(false)
            })
        return () => {
            active = false
        }
    }, [isLoggedIn, username])

    const handleLogout = () => {
        logout()
        navigate('/')
    }

    const isActive = (path) => location.pathname === path
    const isOverviewActive = ['/dashboard', '/usage'].includes(location.pathname)
    const accountLabel = authMode === 'api'
        ? 'API Key 会话'
        : username || '控制台'

    return (
        <nav className="navbar">
            <div className="navbar-inner container">
                <Link to="/" className="navbar-logo">
                    <div className="logo-icon">CC</div>
                    <span className="logo-text">CoinCoin</span>
                </Link>

                <div className="navbar-links">
                    {isLoggedIn ? (
                        <>
                            <div className="nav-session-badge">
                                <span className="nav-session-title">{accountLabel}</span>
                                <span className="nav-session-sub">{hasDeveloperKey ? '开发者 Key 已就绪' : '仅控制台会话'}</span>
                            </div>
                            <Link to="/dashboard" className={`nav-link ${isOverviewActive ? 'active' : ''}`}>
                                概览
                            </Link>
                            <Link to="/recharge" className={`nav-link ${isActive('/recharge') ? 'active' : ''}`}>
                                充值
                            </Link>
                            <Link to="/docs" className={`nav-link ${isActive('/docs') ? 'active' : ''}`}>
                                文档
                            </Link>
                            <div className="nav-secondary-links">
                                <Link to="/usage" className={`nav-link nav-link-secondary ${isActive('/usage') ? 'active' : ''}`}>
                                请求日志
                                </Link>
                                {hasStation && (
                                    <Link to="/station" className={`nav-link nav-link-secondary ${isActive('/station') ? 'active' : ''}`}>
                                        站长中心
                                    </Link>
                                )}
                                <Link to="/settings" className={`nav-link nav-link-secondary ${isActive('/settings') ? 'active' : ''}`}>
                                    接入配置
                                </Link>
                                <Link to="/playground" className={`nav-link nav-link-secondary ${isActive('/playground') ? 'active' : ''}`}>
                                    Playground
                                </Link>
                            </div>
                            <button onClick={toggleTheme} className="theme-toggle" title={theme === 'dark' ? '切换到浅色' : '切换到深色'}>
                                {theme === 'dark' ? '☀️' : '🌙'}
                            </button>
                            <button onClick={handleLogout} className="btn btn-ghost btn-sm">登出</button>
                        </>
                    ) : (
                        <>
                            <Link to="/docs" className={`nav-link ${isActive('/docs') ? 'active' : ''}`}>
                                文档
                            </Link>
                            <Link to={pricingTarget} className="nav-link">定价</Link>
                            <button onClick={toggleTheme} className="theme-toggle" title={theme === 'dark' ? '切换到浅色' : '切换到深色'}>
                                {theme === 'dark' ? '☀️' : '🌙'}
                            </button>
                            <Link to="/login" className="btn btn-secondary btn-sm">登录</Link>
                            <Link to="/register" className="btn btn-primary btn-sm">注册</Link>
                        </>
                    )}
                </div>
            </div>
        </nav>
    )
}
