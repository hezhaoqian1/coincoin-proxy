const SIGNED_RETURN_FIELDS = ['out_trade_no', 'trade_no', 'money', 'trade_status', 'sign']

export function resolvePaymentReturnOrder(search, storedValue) {
    const qs = new URLSearchParams(search || '')
    const returnOrderNo = qs.get('out_trade_no') || qs.get('order_no') || ''

    let storedOrder = null
    if (storedValue) {
        try {
            storedOrder = JSON.parse(storedValue)
        } catch {
            storedOrder = null
        }
    }

    if (returnOrderNo) {
        if (storedOrder?.orderNo === returnOrderNo) return storedOrder
        return { orderNo: returnOrderNo, planName: '', money: '' }
    }

    return storedOrder?.orderNo ? storedOrder : null
}

export function hasSignedPaymentReturn(search) {
    const qs = new URLSearchParams(search || '')
    return SIGNED_RETURN_FIELDS.every(field => Boolean(qs.get(field)))
}
