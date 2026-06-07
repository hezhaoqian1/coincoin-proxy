package quota

import (
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"strings"
	"time"
)

const DefaultReservationTTL = 2 * time.Minute

type Service struct {
	store Store
	now   func() time.Time
}

func NewService(store Store) (*Service, error) {
	if store == nil {
		return nil, errors.New("quota store is required")
	}
	return &Service{store: store, now: time.Now}, nil
}

func (service *Service) Reserve(ctx Context, request ReservationRequest) (ReservationDecision, error) {
	normalized, err := service.normalizeReservation(request)
	if err != nil {
		return ReservationDecision{Allowed: false, Reason: err.Error()}, nil
	}
	return service.store.Reserve(ctx, normalized)
}

func (service *Service) Release(ctx Context, reservationID string) (ReservationDecision, error) {
	reservationID = strings.TrimSpace(reservationID)
	if reservationID == "" {
		return ReservationDecision{Allowed: false, Reason: "reservation_id_required"}, nil
	}
	return service.store.Release(ctx, reservationID)
}

func (service *Service) Commit(ctx Context, update ReservationUpdate) (ReservationDecision, error) {
	update.ReservationID = strings.TrimSpace(update.ReservationID)
	if update.ReservationID == "" {
		return ReservationDecision{Allowed: false, Reason: "reservation_id_required"}, nil
	}
	if update.ActualCostCents < 0 {
		return ReservationDecision{Allowed: false, Reason: "actual_cost_cents_negative"}, nil
	}
	return service.store.Commit(ctx, update)
}

func (service *Service) normalizeReservation(request ReservationRequest) (ReservationRequest, error) {
	request.UserID = strings.TrimSpace(request.UserID)
	request.APIKeyID = strings.TrimSpace(request.APIKeyID)
	request.StationID = strings.TrimSpace(request.StationID)
	request.ChannelID = strings.TrimSpace(request.ChannelID)
	request.ReservationID = strings.TrimSpace(request.ReservationID)
	if request.UserID == "" {
		return ReservationRequest{}, errors.New("user_id_required")
	}
	if request.ReservationID == "" {
		request.ReservationID = newReservationID()
	}
	if request.EstimatedCostCents < 0 {
		return ReservationRequest{}, errors.New("estimated_cost_cents_negative")
	}
	if request.AvailableBalanceCents < 0 {
		return ReservationRequest{}, errors.New("available_balance_cents_negative")
	}
	if request.TTLSeconds <= 0 {
		request.TTLSeconds = int64(DefaultReservationTTL / time.Second)
	}
	if request.TTLSeconds < 5 {
		request.TTLSeconds = 5
	}
	if request.TTLSeconds > 600 {
		request.TTLSeconds = 600
	}
	for i := range request.RPMLimits {
		if err := normalizeLimit(&request.RPMLimits[i], true); err != nil {
			return ReservationRequest{}, fmt.Errorf("rpm_limits[%d]_%s", i, err.Error())
		}
	}
	for i := range request.ConcurrencyLimits {
		if err := normalizeLimit(&request.ConcurrencyLimits[i], false); err != nil {
			return ReservationRequest{}, fmt.Errorf("concurrency_limits[%d]_%s", i, err.Error())
		}
	}
	return request, nil
}

func normalizeLimit(limit *Limit, needsWindow bool) error {
	limit.Dimension = strings.TrimSpace(limit.Dimension)
	limit.ID = strings.TrimSpace(limit.ID)
	if limit.Dimension == "" {
		return errors.New("dimension_required")
	}
	if limit.ID == "" {
		return errors.New("id_required")
	}
	if limit.Limit <= 0 {
		return errors.New("limit_must_be_positive")
	}
	if needsWindow {
		if limit.WindowSeconds <= 0 {
			limit.WindowSeconds = 60
		}
		if limit.WindowSeconds > 3600 {
			limit.WindowSeconds = 3600
		}
	}
	return nil
}

func newReservationID() string {
	var bytes [12]byte
	if _, err := rand.Read(bytes[:]); err != nil {
		return fmt.Sprintf("qres_%d", time.Now().UnixNano())
	}
	return "qres_" + hex.EncodeToString(bytes[:])
}
