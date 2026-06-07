package ledger

import (
	"context"
	"testing"
	"time"

	"github.com/hezhaoqian1/coincoin-proxy/usage-quota-service/internal/events"
)

func TestDryRunWriterIsIdempotent(t *testing.T) {
	writer := NewDryRunWriter(NewInMemoryIdempotencyStore(), nil)
	event := events.UsageEvent{
		SchemaVersion: events.CurrentSchemaVersion,
		EventID:       "uev_duplicate",
		EventType:     "usage.recorded",
		UserID:        "u_1",
		CreatedAt:     time.Now().UTC().Format(time.RFC3339Nano),
		Usage:         events.UsageFields{UnitType: "tokens", UnitCount: 1},
		Cost:          events.CostFields{CostCents: 1},
		RequestLog:    map[string]interface{}{},
	}

	first, err := writer.WriteUsageEvent(context.Background(), event)
	if err != nil {
		t.Fatalf("first write returned error: %v", err)
	}
	if first.Duplicate || !first.DryRun {
		t.Fatalf("unexpected first result: %#v", first)
	}

	second, err := writer.WriteUsageEvent(context.Background(), event)
	if err != nil {
		t.Fatalf("second write returned error: %v", err)
	}
	if !second.Duplicate {
		t.Fatalf("expected duplicate on second write: %#v", second)
	}
}
