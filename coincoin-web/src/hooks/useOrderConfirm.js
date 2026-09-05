import { useState, useEffect, useRef } from 'react'
import { confirmOrder, getApiKey } from '../api/client'

const POLL_INTERVAL = 2000
// Keep trying long enough for users who pay in a separate tab and come back later.
const MAX_ATTEMPTS = 300

export default function useOrderConfirm() {
    const [pendingOrder, setPendingOrder] = useState(null)
    const [confirmResult, setConfirmResult] = useState(null)
    const [dismissed, setDismissed] = useState(false)
    const attemptsRef = useRef(0)
    const timerRef = useRef(null)
    const cancelledRef = useRef(false)

    useEffect(() => {
        cancelledRef.current = false
        const stored = localStorage.getItem('coincoin_last_order')
        if (!stored) return

        // Can't confirm without auth; rely on backend webhook/reconcile to credit and let UI refresh normally.
        if (!getApiKey()) return

        const order = JSON.parse(stored)
        setPendingOrder(order)

        const tryConfirm = async () => {
            if (cancelledRef.current) return
            attemptsRef.current++
            try {
                const result = await confirmOrder(order.orderNo)
                if (result.success || result.message === 'order already confirmed' || result.detail === 'order already confirmed') {
                    setConfirmResult(result)
                    localStorage.removeItem('coincoin_last_order')
                    return
                }
            } catch {
                // 402 = not paid yet, keep trying
            }
            if (attemptsRef.current < MAX_ATTEMPTS) {
                timerRef.current = setTimeout(tryConfirm, POLL_INTERVAL)
            }
        }

        tryConfirm()
        return () => {
            cancelledRef.current = true
            clearTimeout(timerRef.current)
        }
    }, [])

    const dismiss = () => {
        setDismissed(true)
        setConfirmResult(null)
        setPendingOrder(null)
    }

    return { pendingOrder, confirmResult, dismissed, dismiss }
}
