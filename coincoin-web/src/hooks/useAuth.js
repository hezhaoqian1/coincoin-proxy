import { useState, useEffect, useCallback } from 'react'
import {
    getApiKey,
    setApiKey as storeApiKey,
    clearApiKey,
    clearGeneratedKey,
    getBalance,
    getDeveloperKeyState,
    getGeneratedKey,
    getUsername,
    listDeveloperKeys,
    setUserId,
    getStationContext,
    loginUser,
    setUsername as storeUsername,
} from '../api/client'

const LEGACY_DEMO_KEY = 'sk_cc_demo_key'

export function useAuth(options = {}) {
    const shouldLoadRecoverableKey = !!options.loadRecoverableKey
    const initialKey = getApiKey()
    const [apiKey, setApiKeyState] = useState(initialKey === LEGACY_DEMO_KEY ? '' : initialKey)
    const [isLoggedIn, setIsLoggedIn] = useState(!!initialKey && initialKey !== LEGACY_DEMO_KEY)
    const [username, setUsernameState] = useState(getUsername())
    const [generatedApiKey, setGeneratedApiKeyState] = useState(getGeneratedKey())
    const [recoverableApiKey, setRecoverableApiKey] = useState('')
    const [developerKeyState, setDeveloperKeyState] = useState({ hasActiveKey: false, activeKeyCount: 0, latestKey: null })
    const [developerKeyLoading, setDeveloperKeyLoading] = useState(!!initialKey && !!getUsername() && shouldLoadRecoverableKey)
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
                setRecoverableApiKey('')
                setDeveloperKeyState({ hasActiveKey: false, activeKeyCount: 0, latestKey: null })
                setDeveloperKeyLoading(false)
                return
            }
            setApiKeyState(k)
            setIsLoggedIn(!!k)
            setUsernameState(getUsername())
            setGeneratedApiKeyState(getGeneratedKey())
            setRecoverableApiKey('')
        }
        window.addEventListener('storage', sync)
        window.addEventListener('coincoin-auth-changed', sync)
        sync()
        return () => {
            window.removeEventListener('storage', sync)
            window.removeEventListener('coincoin-auth-changed', sync)
        }
    }, [])

    useEffect(() => {
        let active = true

        const syncDeveloperKeyState = async () => {
            const currentKey = getApiKey()
            const currentUsername = getUsername()
            if (!currentKey || !currentUsername) {
                if (!active) return
                setDeveloperKeyState({ hasActiveKey: false, activeKeyCount: 0, latestKey: null })
                setRecoverableApiKey('')
                setDeveloperKeyLoading(false)
                return
            }

            if (active) {
                setDeveloperKeyLoading(shouldLoadRecoverableKey)
            }

            try {
                const state = await getDeveloperKeyState()
                let recoveredKey = ''
                if (state?.has_active_key && shouldLoadRecoverableKey && !getGeneratedKey()) {
                    try {
                        const keys = await listDeveloperKeys()
                        const activeKey = (keys?.data || []).find((item) => item.status === 'active' && item.api_key)
                        recoveredKey = activeKey?.api_key || ''
                    } catch {
                        recoveredKey = ''
                    }
                }
                if (!active) return
                setDeveloperKeyState({
                    hasActiveKey: !!state?.has_active_key,
                    activeKeyCount: state?.active_key_count || 0,
                    latestKey: state?.latest_key || null,
                })
                setRecoverableApiKey(recoveredKey)
            } catch {
                if (!active) return
                setDeveloperKeyState({ hasActiveKey: false, activeKeyCount: 0, latestKey: null })
                setRecoverableApiKey('')
            } finally {
                if (active) {
                    setDeveloperKeyLoading(false)
                }
            }
        }

        syncDeveloperKeyState()
        window.addEventListener('coincoin-auth-changed', syncDeveloperKeyState)
        return () => {
            active = false
            window.removeEventListener('coincoin-auth-changed', syncDeveloperKeyState)
        }
    }, [apiKey, username, shouldLoadRecoverableKey])

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
            setRecoverableApiKey('')
            setDeveloperKeyState({ hasActiveKey: true, activeKeyCount: 1, latestKey: null })
            setDeveloperKeyLoading(false)
            return { success: true, data: balance }
        } catch (err) {
            clearApiKey()
            setApiKeyState('')
            setIsLoggedIn(false)
            setUsernameState('')
            setRecoverableApiKey('')
            setDeveloperKeyState({ hasActiveKey: false, activeKeyCount: 0, latestKey: null })
            setDeveloperKeyLoading(false)
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
        setRecoverableApiKey('')
        setDeveloperKeyState({ hasActiveKey: false, activeKeyCount: 0, latestKey: null })
        setDeveloperKeyLoading(false)
    }, [])

    const loginWithPassword = useCallback(async (username, password, stationSlug) => {
        setLoading(true)
        try {
            const context = getStationContext()
            const data = await loginUser(username, password, stationSlug || context.slug || undefined)
            clearGeneratedKey()
            storeApiKey(data.session_key)
            setUserId(data.user_id)
            storeUsername(data.username)
            setApiKeyState(data.session_key)
            setIsLoggedIn(true)
            setUsernameState(data.username)
            setGeneratedApiKeyState('')
            setRecoverableApiKey('')
            setDeveloperKeyState({ hasActiveKey: false, activeKeyCount: 0, latestKey: null })
            setDeveloperKeyLoading(shouldLoadRecoverableKey)
            return { success: true, data }
        } catch (err) {
            return { success: false, error: err.message || '登录失败' }
        } finally {
            setLoading(false)
        }
    }, [])

    const isConsoleSession = !!username
    const hasLocalDeveloperKey = !!generatedApiKey || (!!apiKey && !isConsoleSession)
    const effectiveApiKey = generatedApiKey || recoverableApiKey || (!isConsoleSession && apiKey ? apiKey : '')
    const hasCopyableDeveloperKey = !!effectiveApiKey
    const hasDeveloperKey = hasLocalDeveloperKey || !!recoverableApiKey || (isConsoleSession && developerKeyState.hasActiveKey)
    const workbenchApiKey = effectiveApiKey || (isConsoleSession && hasDeveloperKey ? apiKey : '')
    const canUseWorkbench = !!workbenchApiKey && hasDeveloperKey
    const authMode = !apiKey
        ? 'anonymous'
        : isConsoleSession
            ? (hasDeveloperKey ? 'session_with_api' : 'session_only')
            : 'api'

    return {
        activeDeveloperKeyCount: developerKeyState.activeKeyCount,
        apiKey,
        authMode,
        canUseWorkbench,
        developerKeyLoading,
        effectiveApiKey,
        generatedApiKey,
        hasCopyableDeveloperKey,
        hasDeveloperKey,
        hasLocalDeveloperKey,
        isConsoleSession,
        isLoggedIn,
        latestDeveloperKey: developerKeyState.latestKey,
        loading,
        login,
        loginWithPassword,
        logout,
        workbenchApiKey,
        username,
    }
}
