import { useEffect, useMemo, useState } from 'react'
import { Link, NavLink, useLocation, useNavigate } from 'react-router-dom'
import { getStationApplication } from '../api/client'
import { useAuth } from '../hooks/useAuth'
import { useTheme } from '../hooks/useTheme'
import './AppShell.css'

function ShellIcon({ kind }) {
    const common = {
        viewBox: '0 0 24 24',
        fill: 'none',
        stroke: 'currentColor',
        strokeWidth: '1.85',
        strokeLinecap: 'round',
        strokeLinejoin: 'round',
        'aria-hidden': 'true',
    }

    switch (kind) {
        case 'access':
            return (
                <svg {...common}>
                    <path d="M12 3v18" />
                    <path d="M7 8h10" />
                    <path d="M7 16h10" />
                    <path d="M5 12h14" />
                </svg>
            )
        case 'logs':
            return (
                <svg {...common}>
                    <path d="M5 6h14" />
                    <path d="M5 12h14" />
                    <path d="M5 18h10" />
                </svg>
            )
        case 'playground':
            return (
                <svg {...common}>
                    <path d="M5 19.5 19.5 12 5 4.5v6l10 1.5L5 13.5z" />
                </svg>
            )
        case 'docs':
            return (
                <svg {...common}>
                    <path d="M7 4.5h8a3 3 0 0 1 3 3v12H10a3 3 0 0 0-3 3z" />
                    <path d="M7 4.5a3 3 0 0 0-3 3v12h6" />
                </svg>
            )
        case 'billing':
            return (
                <svg {...common}>
                    <path d="M12 2v20" />
                    <path d="M17 6.5c0-1.7-2.2-3-5-3s-5 1.3-5 3 1.4 2.5 5 3 5 1.3 5 3-2.2 3-5 3-5-1.3-5-3" />
                </svg>
            )
        case 'station':
            return (
                <svg {...common}>
                    <path d="M4 10.5 12 4l8 6.5" />
                    <path d="M6.5 9.5V19h11V9.5" />
                    <path d="M10 19v-5h4v5" />
                </svg>
            )
        case 'moon':
            return (
                <svg {...common}>
                    <path d="M18 14.5A7.5 7.5 0 1 1 9.5 6 6 6 0 0 0 18 14.5z" />
                </svg>
            )
        case 'sun':
            return (
                <svg {...common}>
                    <circle cx="12" cy="12" r="4" />
                    <path d="M12 2.5v2.5M12 19v2.5M21.5 12H19M5 12H2.5M18.7 5.3l-1.8 1.8M7.1 16.9l-1.8 1.8M18.7 18.7l-1.8-1.8M7.1 7.1 5.3 5.3" />
                </svg>
            )
        case 'logout':
            return (
                <svg {...common}>
                    <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" />
                    <path d="M16 17l5-5-5-5" />
                    <path d="M21 12H9" />
                </svg>
            )
        case 'dashboard':
        default:
            return (
                <svg {...common}>
                    <path d="M4 5h7v6H4z" />
                    <path d="M13 5h7v10h-7z" />
                    <path d="M4 13h7v6H4z" />
                    <path d="M13 17h7v2h-7z" />
                </svg>
            )
    }
}

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
                        <span className="shell-link-icon"><ShellIcon kind={item.icon} /></span>
                        <span className="shell-link-copy">
                            <span className="shell-link-label">{item.label}</span>
                            {item.caption ? <span className="shell-link-caption">{item.caption}</span> : null}
                        </span>
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
    const accountSub = hasDeveloperKey ? '已具备真实调用权限' : '先生成开发者 Key'
    const pathLabel = location.pathname === '/dashboard' ? '控制台总览' : title

    const navGroups = useMemo(() => {
        const groups = [
            {
                title: '控制台',
                items: [
                    { to: '/dashboard', label: '概览', caption: '余额、Key、最近请求', icon: 'dashboard' },
                    { to: '/settings', label: '接入配置', caption: '复制客户端配置', icon: 'access' },
                    { to: '/usage', label: '请求日志', caption: '路由、计量、状态码', icon: 'logs' },
                ],
            },
            {
                title: '工具与支持',
                items: [
                    { to: '/playground', label: '测试请求', caption: '直接发一条真实调用', icon: 'playground' },
                    { to: '/docs', label: '接入文档', caption: 'Claude Code / Codex / SDK', icon: 'docs' },
                ],
            },
            {
                title: '资金与结算',
                items: [
                    { to: '/recharge', label: '充值与套餐', caption: '余额、订单、兑换码', icon: 'billing' },
                ],
            },
        ]

        if (hasStation) {
            groups.push({
                title: '分发与渠道',
                items: [
                    { to: '/station', label: '站长中心', caption: '下游用户、分润、结算', icon: 'station' },
                ],
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
                            <span>中转站控制台</span>
                        </div>
                    </Link>

                    <div className="app-sidebar-badge-row">
                        <span className="app-sidebar-badge">OpenAI / Anthropic 兼容</span>
                    </div>

                    <div className="app-sidebar-session">
                        <div className="app-sidebar-session-eyebrow">当前会话</div>
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
                        <span className="shell-action-icon"><ShellIcon kind={theme === 'dark' ? 'sun' : 'moon'} /></span>
                        <span>{theme === 'dark' ? '浅色模式' : '深色模式'}</span>
                    </button>
                    <button onClick={handleLogout} className="shell-action-btn shell-action-btn-muted">
                        <span className="shell-action-icon"><ShellIcon kind="logout" /></span>
                        <span>退出登录</span>
                    </button>
                </div>
            </aside>

            <div className="app-main">
                <header className="app-topbar">
                    <div className="app-topbar-copy">
                        <div className="app-topbar-eyebrow">{pathLabel}</div>
                        <h1 className="app-topbar-title">{title}</h1>
                        {description ? <p className="app-topbar-desc">{description}</p> : null}
                    </div>

                    <div className="app-topbar-side">
                        <div className="app-topbar-meta">
                            <div className="app-topbar-meta-card">
                                <span className="app-topbar-meta-label">工作区</span>
                                <strong>API 分发</strong>
                            </div>
                            <div className="app-topbar-meta-card">
                                <span className="app-topbar-meta-label">状态</span>
                                <strong>{hasDeveloperKey ? '可调用' : '待配置'}</strong>
                            </div>
                        </div>
                        {actions ? <div className="app-topbar-actions">{actions}</div> : null}
                    </div>
                </header>

                <main className="app-main-content">
                    {children}
                </main>
            </div>
        </div>
    )
}
