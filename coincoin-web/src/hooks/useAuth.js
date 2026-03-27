import { useState, useEffect, useCallback } from 'react'
import {
    getApiKey,
    setApiKey as storeApiKey,
    clearApiKey,
    clearGeneratedKey,
    getBalance,
    getGeneratedKey,
    getUsername,
    setUserId,
    loginUser,
    setUsername as storeUsername,
} from '../api/client'

const LEGACY_DEMO_KEY = 'sk_cc_demo_key'

export function useAuth() {
    const initialKey = getApiKey()
    const [apiKey, setApiKeyState] = useState(initialKey === LEGACY_DEMO_KEY ? '' : initialKey)
    const [isLoggedIn, setIsLoggedIn] = useState(!!initialKey && initialKey !== LEGACY_DEMO_KEY)
    const [username, setUsernameState] = useState(getUsername())
    const [generatedApiKey, setGeneratedApiKeyState] = useState(getGeneratedKey())
    const [loading, setLoading] = useState(false)

    useEffect(() => {
        const sync = () => {
            const k = getApiKey()
            if (k === LEGACY_DEMO_KEY) {
                clearApiKey()
                storeUsername('')
                clearGeneratedKey()
                setApiKeyState('')
                setIsLoggedIn(false)
                setUsernameState('')
                setGeneratedApiKeyState('')
                return
            }
            setApiKeyState(k)
            setIsLoggedIn(!!k)
            setUsernameState(getUsername())
            setGeneratedApiKeyState(getGeneratedKey())
        }
        window.addEventListener('storage', sync)
        window.addEventListener('coincoin-auth-changed', sync)
        sync()
        return () => {
            window.removeEventListener('storage', sync)
            window.removeEventListener('coincoin-auth-changed', sync)
        }
    }, [])

    const login = useCallback(async (key) => {
        setLoading(true)
        try {
            storeApiKey(key)
            storeUsername('')
            clearGeneratedKey()
            const balance = await getBalance()
            if (balance.user_id) {
                setUserId(balance.user_id)
            }
            setApiKeyState(key)
            setIsLoggedIn(true)
            setUsernameState('')
            setGeneratedApiKeyState('')
            return { success: true, data: balance }
        } catch (err) {
            clearApiKey()
            setApiKeyState('')
            setIsLoggedIn(false)
            setUsernameState('')
            return { success: false, error: 'API Key 无效或已过期' }
        } finally {
            setLoading(false)
        }
    }, [])

    const logout = useCallback(() => {
        clearApiKey()
        storeUsername('')
        clearGeneratedKey()
        setApiKeyState('')
        setIsLoggedIn(false)
        setUsernameState('')
        setGeneratedApiKeyState('')
    }, [])

    const loginWithPassword = useCallback(async (username, password) => {
        setLoading(true)
        try {
            const data = await loginUser(username, password)
            clearGeneratedKey()
            storeApiKey(data.session_key)
            setUserId(data.user_id)
            storeUsername(data.username)
            setApiKeyState(data.session_key)
            setIsLoggedIn(true)
            setUsernameState(data.username)
            setGeneratedApiKeyState('')
            return { success: true, data }
        } catch (err) {
            return { success: false, error: err.message || '登录失败' }
        } finally {
            setLoading(false)
        }
    }, [])

    const isConsoleSession = !!username
    const hasDeveloperKey = !!generatedApiKey || (!!apiKey && !isConsoleSession)
    const effectiveApiKey = generatedApiKey || (hasDeveloperKey ? apiKey : '')
    const authMode = !apiKey
        ? 'anonymous'
        : isConsoleSession
            ? (generatedApiKey ? 'session_with_api' : 'session_only')
            : 'api'

    return {
        apiKey,
        authMode,
        effectiveApiKey,
        generatedApiKey,
        hasDeveloperKey,
        isConsoleSession,
        isLoggedIn,
        loading,
        login,
        loginWithPassword,
        logout,
        username,
    }
}
