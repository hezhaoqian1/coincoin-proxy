import { useEffect } from 'react'
import { BrowserRouter, Routes, Route, Navigate, useLocation, useParams } from 'react-router-dom'
import { useAuth } from './hooks/useAuth'
import Navbar from './components/Navbar'
import Footer from './components/Footer'
import Landing from './pages/Landing'
import Login from './pages/Login'
import Register from './pages/Register'
import Dashboard from './pages/Dashboard'
import Usage from './pages/Usage'
import Recharge from './pages/Recharge'
import Docs from './pages/Docs'
import GuideDetail from './pages/GuideDetail'
import Account from './pages/Account'
import ApiKeys from './pages/ApiKeys'
import Playground from './pages/Playground'
import PayReturn from './pages/PayReturn'
import Station from './pages/Station'
import StationPortal from './pages/StationPortal'
import Referrals from './pages/Referrals'

function PublicShell({ children }) {
    return (
        <>
            <Navbar />
            {children}
            <Footer />
        </>
    )
}

function ProtectedRoute({ children }) {
    const { isLoggedIn } = useAuth()
    const location = useLocation()
    if (!isLoggedIn) return <Navigate to="/login" replace state={{ from: location.pathname + location.search + location.hash }} />
    return children
}

function GuestOnlyRoute({ children }) {
    const { isLoggedIn } = useAuth()
    if (isLoggedIn) return <Navigate to="/dashboard" replace />
    return children
}

function ReferralRedirect() {
    const { code = '' } = useParams()
    const referralCode = code.trim()
    const target = referralCode
        ? `/register?ref=${encodeURIComponent(referralCode)}`
        : '/register'
    return <Navigate to={target} replace />
}

function ScrollManager() {
    const location = useLocation()

    useEffect(() => {
        if (typeof window === 'undefined') return
        window.history.scrollRestoration = 'manual'
    }, [])

    useEffect(() => {
        if (typeof window === 'undefined') return
        if (location.hash) {
            const targetId = decodeURIComponent(location.hash.slice(1))

            requestAnimationFrame(() => {
                const target = document.getElementById(targetId)
                if (target) {
                    target.scrollIntoView({ block: 'start', behavior: 'auto' })
                }
            })
            return
        }

        window.scrollTo({ top: 0, left: 0, behavior: 'auto' })
    }, [location.pathname, location.hash])

    return null
}

export default function App() {
    return (
        <BrowserRouter>
            <ScrollManager />
            <Routes>
                <Route path="/" element={<PublicShell><Landing /></PublicShell>} />
                <Route path="/login" element={
                    <GuestOnlyRoute><PublicShell><Login /></PublicShell></GuestOnlyRoute>
                } />
                <Route path="/register" element={
                    <GuestOnlyRoute><PublicShell><Register /></PublicShell></GuestOnlyRoute>
                } />
                <Route path="/r/:code" element={<ReferralRedirect />} />
                <Route path="/dashboard" element={
                    <ProtectedRoute><Dashboard /></ProtectedRoute>
                } />
                <Route path="/usage" element={
                    <ProtectedRoute><Usage /></ProtectedRoute>
                } />
                <Route path="/recharge" element={<Recharge />} />
                <Route path="/docs" element={<Docs />} />
                <Route path="/guides/:guideId" element={
                    <ProtectedRoute><GuideDetail /></ProtectedRoute>
                } />
                <Route path="/playground" element={
                    <ProtectedRoute><Playground /></ProtectedRoute>
                } />
                <Route path="/account" element={
                    <ProtectedRoute><Account /></ProtectedRoute>
                } />
                <Route path="/settings" element={<Navigate to="/guides/api-quickstart" replace />} />
                <Route path="/api-keys" element={
                    <ProtectedRoute><ApiKeys /></ProtectedRoute>
                } />
                <Route path="/referrals" element={
                    <ProtectedRoute><Referrals /></ProtectedRoute>
                } />
                <Route path="/station" element={
                    <ProtectedRoute><Station /></ProtectedRoute>
                } />
                <Route path="/s/:slug" element={<StationPortal />} />
                <Route path="/pay/return" element={<PublicShell><PayReturn /></PublicShell>} />
                <Route path="*" element={<Navigate to="/" replace />} />
            </Routes>
        </BrowserRouter>
    )
}
