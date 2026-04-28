import { useEffect, useState } from 'react'
import { Link, useLocation, useNavigate } from 'react-router-dom'
import { useAuth } from '../hooks/useAuth'
import { useTheme } from '../hooks/useTheme'
import { getStationApplication } from '../api/client'
import './Navbar.css'

export default function Navbar() {
    const { isLoggedIn } = useAuth()
    const { theme, toggleTheme } = useTheme()
    const location = useLocation()
    const pricingTarget = isLoggedIn ? '/recharge' : '/recharge'

    const isActive = (path) => location.pathname === path
    const isPublicPage = ['/', '/docs', '/login', '/register', '/pay/return'].includes(location.pathname)

    if (!isPublicPage) return null

    return (
        <nav className="navbar">
            <div className="navbar-inner container">
                <Link to="/" className="navbar-logo">
                    <div className="logo-icon">CF</div>
                    <span className="logo-text">ClawFather</span>
                </Link>

                <div className="navbar-links">
                    <Link to="/docs" className={`nav-link ${isActive('/docs') ? 'active' : ''}`}>
                        文档
                    </Link>
                    <Link to={pricingTarget} className={`nav-link ${isActive('/recharge') ? 'active' : ''}`}>
                        定价
                    </Link>
                    <button onClick={toggleTheme} className="theme-toggle" title={theme === 'dark' ? '切换到浅色' : '切换到深色'}>
                        {theme === 'dark' ? '☀️' : '🌙'}
                    </button>
                    {isLoggedIn ? (
                        <Link to="/dashboard" className="btn btn-primary btn-sm">进入控制台</Link>
                    ) : (
                        <>
                            <Link to="/login" className="btn btn-secondary btn-sm">登录</Link>
                            <Link to="/register" className="btn btn-primary btn-sm">注册</Link>
                        </>
                    )}
                </div>
            </div>
        </nav>
    )
}
