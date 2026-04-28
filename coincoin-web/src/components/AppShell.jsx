import { useEffect, useMemo, useState } from 'react'
import { Link, NavLink, useLocation, useNavigate } from 'react-router-dom'
import { getStationApplication } from '../api/client'
import { useAuth } from '../hooks/useAuth'
import { useTheme } from '../hooks/useTheme'
import './AppShell.css'

function ShellGroup({ title, items }) {
    return (
        <div className="shell-group">
            <div className="shell-group-title">{title}</div>
            <div className="shell-group-items">
                {items.map((item) => (
                    <NavLink
                        key={item.to}
                        to={item.to}
                        className={({ isActive }) => `shell-link ${isActive ? 'active' : ''}`}
                    >
                        <span className="shell-link-icon" aria-hidden="true">{item.icon}</span>
                        <span className="shell-link-label">{item.label}</span>
                    </NavLink>
                ))}
            </div>
        </div>
    )
}

export default function AppShell({ title, description, actions, children }) {
    const { authMode, hasDeveloperKey, logout, username } = useAuth()
    const { theme, toggleTheme } = useTheme()
    const navigate = useNavigate()
    const location = useLocation()
    const [hasStation, setHasStation] = useState(false)

    useEffect(() => {
        let active = true
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
    }, [])

    const accountLabel = authMode === 'api' ? '开发者 Key 会话' : (username || '控制台账号')
    const accountSub = hasDeveloperKey ? '可直接发请求' : '先生成开发者 Key'

    const navGroups = useMemo(() => {
        const groups = [
            {
                title: '开发者',
                items: [
                    { to: '/dashboard', label: '概览', icon: '◧' },
                    { to: '/settings', label: '接入配置', icon: '⌥' },
                    { to: '/usage', label: '请求日志', icon: '☰' },
                    { to: '/playground', label: '测试请求', icon: '◎' },
                    { to: '/docs', label: '接入文档', icon: '◫' },
                ],
            },
            {
                title: '钱包与订单',
                items: [
                    { to: '/recharge', label: '充值与套餐', icon: '¤' },
                ],
            },
        ]

        if (hasStation) {
            groups.push({
                title: '高级',
                items: [{ to: '/station', label: '站长中心', icon: '◇' }],
            })
        }

        return groups
    }, [hasStation])

    const handleLogout = () => {
        logout()
        navigate('/')
    }

    return (
        <div className="app-shell">
            <aside className="app-sidebar">
                <div className="app-sidebar-top">
                    <Link to="/dashboard" className="app-sidebar-brand">
                        <div className="logo-icon">CF</div>
                        <div className="app-sidebar-brand-copy">
                            <strong>ClawFather</strong>
                            <span>开发者控制台</span>
                        </div>
                    </Link>

                    <div className="app-sidebar-session">
                        <div className="app-sidebar-session-title">{accountLabel}</div>
                        <div className="app-sidebar-session-sub">{accountSub}</div>
                    </div>
                </div>

                <nav className="app-sidebar-nav">
                    {navGroups.map((group) => (
                        <ShellGroup key={group.title} title={group.title} items={group.items} />
                    ))}
                </nav>

                <div className="app-sidebar-footer">
                    <button onClick={toggleTheme} className="shell-action-btn" title={theme === 'dark' ? '切换到浅色' : '切换到深色'}>
                        <span>{theme === 'dark' ? '☀️' : '🌙'}</span>
                        <span>{theme === 'dark' ? '浅色模式' : '深色模式'}</span>
                    </button>
                    <button onClick={handleLogout} className="shell-action-btn shell-action-btn-muted">
                        <span>↩</span>
                        <span>退出登录</span>
                    </button>
                </div>
            </aside>

            <div className="app-main">
                <header className="app-topbar">
                    <div>
                        <div className="app-topbar-eyebrow">
                            {location.pathname === '/dashboard' ? '概览' : title}
                        </div>
                        <h1 className="app-topbar-title">{title}</h1>
                        {description ? <p className="app-topbar-desc">{description}</p> : null}
                    </div>
                    {actions ? <div className="app-topbar-actions">{actions}</div> : null}
                </header>

                <main className="app-main-content">
                    {children}
                </main>
            </div>
        </div>
    )
}
