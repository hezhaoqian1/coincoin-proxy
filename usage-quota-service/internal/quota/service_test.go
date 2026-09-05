package quota

import (
	"context"
	"testing"
	"time"
)

func TestServiceReserveBlocksRPMAcrossRequests(t *testing.T) {
	store := NewMemoryStore()
	service, err := NewService(store)
	if err != nil {
		t.Fatal(err)
	}
	req := ReservationRequest{
		UserID: "u_1",
		RPMLimits: []Limit{{
			Dimension:     "user",
			ID:            "u_1",
			Limit:         1,
			WindowSeconds: 60,
		}},
	}

	first, err := service.Reserve(context.Background(), req)
	if err != nil {
		t.Fatal(err)
	}
	if !first.Allowed {
		t.Fatalf("expected first reservation allowed: %#v", first)
	}
	second, err := service.Reserve(context.Background(), req)
	if err != nil {
		t.Fatal(err)
	}
	if second.Allowed || second.Reason != "rpm_exceeded:user" {
		t.Fatalf("expected rpm rejection, got %#v", second)
	}
}

func TestServiceReserveReleaseFreesConcurrencyAndBalance(t *testing.T) {
	store := NewMemoryStore()
	service, err := NewService(store)
	if err != nil {
		t.Fatal(err)
	}
	req := ReservationRequest{
		ReservationID:         "qres_a",
		UserID:                "u_1",
		EstimatedCostCents:    80,
		AvailableBalanceCents: 100,
		ConcurrencyLimits: []Limit{{
			Dimension: "user",
			ID:        "u_1",
			Limit:     1,
		}},
	}

	first, err := service.Reserve(context.Background(), req)
	if err != nil {
		t.Fatal(err)
	}
	if !first.Allowed {
		t.Fatalf("expected first reservation allowed: %#v", first)
	}
	blocked, err := service.Reserve(context.Background(), ReservationRequest{
		ReservationID:         "qres_b",
		UserID:                "u_1",
		EstimatedCostCents:    80,
		AvailableBalanceCents: 100,
		ConcurrencyLimits:     req.ConcurrencyLimits,
	})
	if err != nil {
		t.Fatal(err)
	}
	if blocked.Allowed || blocked.Reason != "concurrency_exceeded:user" {
		t.Fatalf("expected concurrency rejection, got %#v", blocked)
	}
	released, err := service.Release(context.Background(), first.ReservationID)
	if err != nil {
		t.Fatal(err)
	}
	if !released.Allowed || released.Reason != "released" {
		t.Fatalf("expected release success, got %#v", released)
	}
	afterRelease, err := service.Reserve(context.Background(), ReservationRequest{
		ReservationID:         "qres_c",
		UserID:                "u_1",
		EstimatedCostCents:    80,
		AvailableBalanceCents: 100,
		ConcurrencyLimits:     req.ConcurrencyLimits,
	})
	if err != nil {
		t.Fatal(err)
	}
	if !afterRelease.Allowed {
		t.Fatalf("expected reservation after release: %#v", afterRelease)
	}
}

func TestServiceReserveDoesNotReuseFinishedReservationID(t *testing.T) {
	store := NewMemoryStore()
	service, err := NewService(store)
	if err != nil {
		t.Fatal(err)
	}
	first, err := service.Reserve(context.Background(), ReservationRequest{
		ReservationID: "qres_done",
		UserID:        "u_1",
	})
	if err != nil {
		t.Fatal(err)
	}
	if !first.Allowed {
		t.Fatalf("expected first reservation allowed: %#v", first)
	}
	if _, err := service.Commit(context.Background(), ReservationUpdate{ReservationID: "qres_done"}); err != nil {
		t.Fatal(err)
	}
	second, err := service.Reserve(context.Background(), ReservationRequest{
		ReservationID: "qres_done",
		UserID:        "u_1",
	})
	if err != nil {
		t.Fatal(err)
	}
	if !second.Allowed || second.Reason != "committed" {
		t.Fatalf("expected completed reservation idempotency, got %#v", second)
	}
}

func TestServiceReserveBlocksBalanceReservedAcrossReservations(t *testing.T) {
	store := NewMemoryStore()
	service, err := NewService(store)
	if err != nil {
		t.Fatal(err)
	}
	req := ReservationRequest{
		UserID:                "u_1",
		EstimatedCostCents:    60,
		AvailableBalanceCents: 100,
	}
	first, err := service.Reserve(context.Background(), req)
	if err != nil {
		t.Fatal(err)
	}
	if !first.Allowed {
		t.Fatalf("expected first reservation allowed: %#v", first)
	}
	second, err := service.Reserve(context.Background(), req)
	if err != nil {
		t.Fatal(err)
	}
	if second.Allowed || second.Reason != "balance_reserved_exceeded" {
		t.Fatalf("expected balance reservation rejection, got %#v", second)
	}
}

func TestServiceNormalizesTTLAndRejectsBadRequests(t *testing.T) {
	store := NewMemoryStore()
	service, err := NewService(store)
	if err != nil {
		t.Fatal(err)
	}
	missingUser, err := service.Reserve(context.Background(), ReservationRequest{})
	if err != nil {
		t.Fatal(err)
	}
	if missingUser.Allowed || missingUser.Reason != "user_id_required" {
		t.Fatalf("expected user_id_required, got %#v", missingUser)
	}
	allowed, err := service.Reserve(context.Background(), ReservationRequest{UserID: "u_1", TTLSeconds: 1})
	if err != nil {
		t.Fatal(err)
	}
	if !allowed.Allowed || time.Until(allowed.ExpiresAt) < 4*time.Second {
		t.Fatalf("expected normalized ttl reservation, got %#v", allowed)
	}
}
