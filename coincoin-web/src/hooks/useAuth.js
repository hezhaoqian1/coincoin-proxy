import { useState, useEffect, useCallback } from 'react'
import { getApiKey, setApiKey as storeApiKey, clearApiKey, getBalance, setUserId, loginUser, setUsername as storeUsername } from '../api/client'

export function useAuth() {
    const [apiKey, setApiKeyState] = useState(getApiKey())
    const [isLoggedIn, setIsLoggedIn] = useState(!!getApiKey())
    const [loading, setLoading] = useState(false)

    useEffect(() => {
        const sync = () => {
            const k = getApiKey()
            setApiKeyState(k)
            setIsLoggedIn(!!k)
        }
        window.addEventListener('storage', sync)
        const id = setInterval(sync, 1000)
        return () => {
            window.removeEventListener('storage', sync)
            clearInterval(id)
        }
    }, [])

    const login = useCallback(async (key) => {
        setLoading(true)
        try {
            storeApiKey(key)
            const balance = await getBalance()
            if (balance.user_id) {
                setUserId(balance.user_id)
            }
            setApiKeyState(key)
            setIsLoggedIn(true)
            return { success: true, data: balance }
        } catch (err) {
            if (key === 'sk_cc_demo_key') {
                setApiKeyState(key)
                setIsLoggedIn(true)
                return { success: true, data: {} }
            }
            clearApiKey()
            setApiKeyState('')
            setIsLoggedIn(false)
            return { success: false, error: 'API Key 无效或已过期' }
        } finally {
            setLoading(false)
        }
    }, [])

    const logout = useCallback(() => {
        clearApiKey()
        localStorage.removeItem('coincoin_username')
        localStorage.removeItem('coincoin_generated_key')
        setApiKeyState('')
        setIsLoggedIn(false)
    }, [])

    const loginWithPassword = useCallback(async (username, password) => {
        setLoading(true)
        try {
            const data = await loginUser(username, password)
            storeApiKey(data.session_key)
            setUserId(data.user_id)
            storeUsername(data.username)
            setApiKeyState(data.session_key)
            setIsLoggedIn(true)
            return { success: true, data }
        } catch (err) {
            return { success: false, error: err.message || '登录失败' }
        } finally {
            setLoading(false)
        }
    }, [])

    const loginDemo = useCallback(() => {
        storeApiKey('sk_cc_demo_key')
        setApiKeyState('sk_cc_demo_key')
        setIsLoggedIn(true)
    }, [])

    return { apiKey, isLoggedIn, loading, login, loginWithPassword, logout, loginDemo }
}
