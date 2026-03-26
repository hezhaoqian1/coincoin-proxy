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

export function useAuth() {
    const [apiKey, setApiKeyState] = useState(getApiKey())
    const [isLoggedIn, setIsLoggedIn] = useState(!!getApiKey())
    const [username, setUsernameState] = useState(getUsername())
    const [generatedApiKey, setGeneratedApiKeyState] = useState(getGeneratedKey())
    const [loading, setLoading] = useState(false)

    useEffect(() => {
        const sync = () => {
            const k = getApiKey()
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
            if (key === 'sk_cc_demo_key') {
                setApiKeyState(key)
                setIsLoggedIn(true)
                setUsernameState('')
                return { success: true, data: {} }
            }
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

    const loginDemo = useCallback(() => {
        storeApiKey('sk_cc_demo_key')
        storeUsername('')
        clearGeneratedKey()
        setApiKeyState('sk_cc_demo_key')
        setIsLoggedIn(true)
        setUsernameState('')
        setGeneratedApiKeyState('')
    }, [])

    const isDemo = apiKey === 'sk_cc_demo_key'
    const isConsoleSession = !!username && !isDemo
    const hasDeveloperKey = !!generatedApiKey || (!!apiKey && !isConsoleSession && !isDemo)
    const effectiveApiKey = generatedApiKey || (hasDeveloperKey ? apiKey : '')
    const authMode = isDemo
        ? 'demo'
        : !apiKey
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
        isDemo,
        isLoggedIn,
        loading,
        login,
        loginWithPassword,
        logout,
        loginDemo,
        username,
    }
}
