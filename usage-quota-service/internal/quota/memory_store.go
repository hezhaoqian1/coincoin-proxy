package quota

import (
	"fmt"
	"sync"
	"time"
)

type memoryReservation struct {
	request   ReservationRequest
	status    string
	expiresAt time.Time
}

type MemoryStore struct {
	mu           sync.Mutex
	rpm          map[string][]time.Time
	concurrency  map[string]int64
	reservedCost map[string]int64
	reservations map[string]memoryReservation
	now          func() time.Time
}

func NewMemoryStore() *MemoryStore {
	return &MemoryStore{
		rpm:          make(map[string][]time.Time),
		concurrency:  make(map[string]int64),
		reservedCost: make(map[string]int64),
		reservations: make(map[string]memoryReservation),
		now:          time.Now,
	}
}

func (store *MemoryStore) Reserve(_ Context, request ReservationRequest) (ReservationDecision, error) {
	store.mu.Lock()
	defer store.mu.Unlock()

	now := store.now().UTC()
	expiresAt := now.Add(time.Duration(request.TTLSeconds) * time.Second)
	if existing, ok := store.reservations[request.ReservationID]; ok {
		if existing.status == "pending" && existing.expiresAt.After(now) {
			return ReservationDecision{Allowed: true, ReservationID: request.ReservationID, Reason: "duplicate", ExpiresAt: existing.expiresAt}, nil
		}
		if existing.status != "pending" {
			return ReservationDecision{Allowed: true, ReservationID: request.ReservationID, Reason: existing.status, ExpiresAt: existing.expiresAt}, nil
		}
	}
	for _, limit := range request.RPMLimits {
		key := rpmKey(limit, now)
		windowStart := now.Add(-time.Duration(limit.WindowSeconds) * time.Second)
		kept := store.rpm[key][:0]
		for _, item := range store.rpm[key] {
			if item.After(windowStart) {
				kept = append(kept, item)
			}
		}
		store.rpm[key] = kept
		if int64(len(kept))+1 > limit.Limit {
			return ReservationDecision{Allowed: false, Reason: "rpm_exceeded:" + limit.Dimension, RetryAfterMS: 1000}, nil
		}
	}
	for _, limit := range request.ConcurrencyLimits {
		key := concurrencyKey(limit)
		if store.concurrency[key]+1 > limit.Limit {
			return ReservationDecision{Allowed: false, Reason: "concurrency_exceeded:" + limit.Dimension}, nil
		}
	}
	balanceKey := balanceKey(request.UserID)
	if request.EstimatedCostCents > 0 && store.reservedCost[balanceKey]+request.EstimatedCostCents > request.AvailableBalanceCents {
		return ReservationDecision{Allowed: false, Reason: "balance_reserved_exceeded"}, nil
	}

	for _, limit := range request.RPMLimits {
		key := rpmKey(limit, now)
		store.rpm[key] = append(store.rpm[key], now)
	}
	for _, limit := range request.ConcurrencyLimits {
		store.concurrency[concurrencyKey(limit)]++
	}
	if request.EstimatedCostCents > 0 {
		store.reservedCost[balanceKey] += request.EstimatedCostCents
	}
	store.reservations[request.ReservationID] = memoryReservation{request: request, status: "pending", expiresAt: expiresAt}
	return ReservationDecision{Allowed: true, ReservationID: request.ReservationID, Reason: "reserved", ExpiresAt: expiresAt}, nil
}

func (store *MemoryStore) Release(_ Context, reservationID string) (ReservationDecision, error) {
	return store.finish(reservationID, "released")
}

func (store *MemoryStore) Commit(_ Context, update ReservationUpdate) (ReservationDecision, error) {
	return store.finish(update.ReservationID, "committed")
}

func (store *MemoryStore) Close() error { return nil }

func (store *MemoryStore) finish(reservationID string, status string) (ReservationDecision, error) {
	store.mu.Lock()
	defer store.mu.Unlock()

	reservation, ok := store.reservations[reservationID]
	if !ok {
		return ReservationDecision{Allowed: false, ReservationID: reservationID, Reason: "reservation_missing"}, nil
	}
	if reservation.status != "pending" {
		return ReservationDecision{Allowed: true, ReservationID: reservationID, Reason: reservation.status}, nil
	}
	for _, limit := range reservation.request.ConcurrencyLimits {
		key := concurrencyKey(limit)
		if store.concurrency[key] > 0 {
			store.concurrency[key]--
		}
	}
	if reservation.request.EstimatedCostCents > 0 {
		key := balanceKey(reservation.request.UserID)
		store.reservedCost[key] -= reservation.request.EstimatedCostCents
		if store.reservedCost[key] < 0 {
			store.reservedCost[key] = 0
		}
	}
	reservation.status = status
	store.reservations[reservationID] = reservation
	return ReservationDecision{Allowed: true, ReservationID: reservationID, Reason: status}, nil
}

func rpmKey(limit Limit, now time.Time) string {
	bucket := now.Unix() / maxInt64(1, limit.WindowSeconds)
	return fmt.Sprintf("rpm:%s:%s:%d", limit.Dimension, limit.ID, bucket)
}

func concurrencyKey(limit Limit) string {
	return "concurrency:" + limit.Dimension + ":" + limit.ID
}

func balanceKey(userID string) string {
	return "balance:" + userID
}

func maxInt64(a int64, b int64) int64 {
	if a > b {
		return a
	}
	return b
}
