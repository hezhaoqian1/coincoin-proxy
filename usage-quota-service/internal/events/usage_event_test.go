package events

import (
	"encoding/json"
	"testing"
	"time"
)

func TestParseStreamFieldsValidatesStableUsageEvent(t *testing.T) {
	event := UsageEvent{
		SchemaVersion: CurrentSchemaVersion,
		EventID:       "uev_req_123",
		EventType:     "usage.recorded",
		Status:        "received",
		UserID:        "u_123",
		APIKeyID:      "k_123",
		RequestID:     "req_123",
		CreatedAt:     time.Now().UTC().Format(time.RFC3339Nano),
		Usage: UsageFields{
			UnitType:     "tokens",
			UnitCount:    15,
			InputTokens:  10,
			OutputTokens: 5,
		},
		Cost: CostFields{
			CostCents:         2,
			RetailChargeCents: 2,
			PriceVersion:      7,
		},
		RequestLog: map[string]interface{}{"model": "gpt-5.4"},
	}
	payload, err := json.Marshal(event)
	if err != nil {
		t.Fatal(err)
	}

	envelope, err := ParseStreamFields("1710000000000-0", map[string]string{
		"payload":  string(payload),
		"attempts": "2",
	})
	if err != nil {
		t.Fatalf("ParseStreamFields returned error: %v", err)
	}

	if envelope.StreamID != "1710000000000-0" {
		t.Fatalf("stream id mismatch: %q", envelope.StreamID)
	}
	if envelope.Attempts != 2 {
		t.Fatalf("attempts mismatch: %d", envelope.Attempts)
	}
	if envelope.Event.EventID != event.EventID || envelope.Event.UserID != event.UserID {
		t.Fatalf("event mismatch: %#v", envelope.Event)
	}
}

func TestParseStreamFieldsRejectsMalformedPayload(t *testing.T) {
	_, err := ParseStreamFields("1-0", map[string]string{"payload": "{bad json"})
	if err == nil {
		t.Fatal("expected malformed JSON to fail")
	}
}

func TestUsageEventValidateRejectsMissingUser(t *testing.T) {
	event := UsageEvent{
		SchemaVersion: CurrentSchemaVersion,
		EventID:       "uev_req_123",
		EventType:     "usage.recorded",
		CreatedAt:     time.Now().UTC().Format(time.RFC3339Nano),
		Usage:         UsageFields{UnitType: "tokens"},
		Cost:          CostFields{},
		RequestLog:    map[string]interface{}{},
	}
	if err := event.Validate(); err == nil {
		t.Fatal("expected missing user_id to fail")
	}
}
