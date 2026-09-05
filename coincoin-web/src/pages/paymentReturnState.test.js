import test from 'node:test'
import assert from 'node:assert/strict'

import { hasSignedPaymentReturn, resolvePaymentReturnOrder } from './paymentReturnState.js'

test('signed return order overrides a stale local order', () => {
    const stored = JSON.stringify({ orderNo: 'CC_old', planName: 'Old plan', money: '1.00' })
    const order = resolvePaymentReturnOrder('?order_no=CC_old&out_trade_no=CC_paid', stored)

    assert.deepEqual(order, { orderNo: 'CC_paid', planName: '', money: '' })
})

test('matching local order keeps display metadata', () => {
    const stored = JSON.stringify({ orderNo: 'CC_paid', planName: 'Credit $100', money: '59.90' })
    const order = resolvePaymentReturnOrder('?order_no=CC_paid&out_trade_no=CC_paid', stored)

    assert.deepEqual(order, { orderNo: 'CC_paid', planName: 'Credit $100', money: '59.90' })
})

test('return query works without local browser state', () => {
    const order = resolvePaymentReturnOrder('?out_trade_no=CC_paid', null)

    assert.equal(order.orderNo, 'CC_paid')
})

test('local order remains a fallback when there is no return order number', () => {
    const stored = JSON.stringify({ orderNo: 'CC_local', planName: 'Plan', money: '9.90' })

    assert.equal(resolvePaymentReturnOrder('', stored).orderNo, 'CC_local')
})

test('signed return requires the complete payment proof', () => {
    const complete = '?out_trade_no=CC_paid&trade_no=T1&money=59.90&trade_status=TRADE_SUCCESS&sign=abc'

    assert.equal(hasSignedPaymentReturn(complete), true)
    assert.equal(hasSignedPaymentReturn('?out_trade_no=CC_paid&trade_no=T1'), false)
})
