package quota

import "time"

type Limit struct {
	Dimension     string `json:"dimension"`
	ID            string `json:"id"`
	Limit         int64  `json:"limit"`
	WindowSeconds int64  `json:"window_seconds,omitempty"`
}

type ReservationRequest struct {
	ReservationID         string  `json:"reservation_id,omitempty"`
	UserID                string  `json:"user_id"`
	APIKeyID              string  `json:"api_key_id,omitempty"`
	StationID             string  `json:"station_id,omitempty"`
	ChannelID             string  `json:"channel_id,omitempty"`
	EstimatedCostCents    int64   `json:"estimated_cost_cents,omitempty"`
	AvailableBalanceCents int64   `json:"available_balance_cents,omitempty"`
	RPMLimits             []Limit `json:"rpm_limits,omitempty"`
	ConcurrencyLimits     []Limit `json:"concurrency_limits,omitempty"`
	TTLSeconds            int64   `json:"ttl_seconds,omitempty"`
}

type ReservationDecision struct {
	Allowed       bool      `json:"allowed"`
	ReservationID string    `json:"reservation_id,omitempty"`
	Reason        string    `json:"reason,omitempty"`
	RetryAfterMS  int64     `json:"retry_after_ms,omitempty"`
	DryRun        bool      `json:"dry_run,omitempty"`
	ExpiresAt     time.Time `json:"expires_at,omitempty"`
}

type ReservationUpdate struct {
	ReservationID   string `json:"reservation_id"`
	ActualCostCents int64  `json:"actual_cost_cents,omitempty"`
}

type Store interface {
	Reserve(ctx Context, request ReservationRequest) (ReservationDecision, error)
	Release(ctx Context, reservationID string) (ReservationDecision, error)
	Commit(ctx Context, update ReservationUpdate) (ReservationDecision, error)
	Close() error
}

type Context interface {
	Done() <-chan struct{}
	Err() error
}
