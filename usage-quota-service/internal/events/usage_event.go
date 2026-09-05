package events

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"strconv"
	"time"
)

const CurrentSchemaVersion = 1

type UsageFields struct {
	UnitType            string `json:"unit_type"`
	UnitCount           int64  `json:"unit_count"`
	InputTokens         int64  `json:"input_tokens"`
	OutputTokens        int64  `json:"output_tokens"`
	CacheReadTokens     int64  `json:"cache_read_tokens"`
	CacheCreationTokens int64  `json:"cache_creation_tokens"`
	ImageCount          int64  `json:"image_count"`
	VideoCount          int64  `json:"video_count"`
}

type CostFields struct {
	CostCents          int64  `json:"cost_cents"`
	RetailChargeCents  int64  `json:"retail_charge_cents"`
	WholesaleCostCents int64  `json:"wholesale_cost_cents"`
	PricingMode        string `json:"pricing_mode"`
	PriceVersion       int64  `json:"price_version"`
}

type UsageEvent struct {
	SchemaVersion int                    `json:"schema_version"`
	EventID       string                 `json:"event_id"`
	EventType     string                 `json:"event_type"`
	Status        string                 `json:"status"`
	UserID        string                 `json:"user_id"`
	APIKeyID      string                 `json:"api_key_id"`
	RequestID     string                 `json:"request_id"`
	ReservationID string                 `json:"reservation_id"`
	CreatedAt     string                 `json:"created_at"`
	Usage         UsageFields            `json:"usage"`
	Cost          CostFields             `json:"cost"`
	RequestLog    map[string]interface{} `json:"request_log"`
}

type StreamEnvelope struct {
	StreamID string
	Event    UsageEvent
	Attempts int64
}

func ParseStreamFields(streamID string, fields map[string]string) (StreamEnvelope, error) {
	payload := fields["payload"]
	if payload == "" {
		return StreamEnvelope{}, errors.New("usage event is missing payload")
	}

	var event UsageEvent
	decoder := json.NewDecoder(bytes.NewBufferString(payload))
	decoder.UseNumber()
	if err := decoder.Decode(&event); err != nil {
		return StreamEnvelope{}, fmt.Errorf("decode usage event payload: %w", err)
	}
	if err := event.Validate(); err != nil {
		return StreamEnvelope{}, err
	}

	attempts, _ := strconv.ParseInt(fields["attempts"], 10, 64)
	return StreamEnvelope{StreamID: streamID, Event: event, Attempts: attempts}, nil
}

func (event UsageEvent) Validate() error {
	if event.SchemaVersion != CurrentSchemaVersion {
		return fmt.Errorf("unsupported usage event schema_version %d", event.SchemaVersion)
	}
	if event.EventID == "" {
		return errors.New("usage event event_id is required")
	}
	if event.EventType != "usage.recorded" {
		return fmt.Errorf("unsupported usage event type %q", event.EventType)
	}
	if event.UserID == "" {
		return errors.New("usage event user_id is required")
	}
	if event.CreatedAt == "" {
		return errors.New("usage event created_at is required")
	}
	if _, err := time.Parse(time.RFC3339Nano, event.CreatedAt); err != nil {
		return fmt.Errorf("usage event created_at must be RFC3339: %w", err)
	}
	if event.Usage.UnitType == "" {
		return errors.New("usage event usage.unit_type is required")
	}
	if event.Usage.UnitCount < 0 ||
		event.Usage.InputTokens < 0 ||
		event.Usage.OutputTokens < 0 ||
		event.Usage.CacheReadTokens < 0 ||
		event.Usage.CacheCreationTokens < 0 ||
		event.Usage.ImageCount < 0 ||
		event.Usage.VideoCount < 0 {
		return errors.New("usage event usage counters must not be negative")
	}
	if event.Cost.CostCents < 0 || event.Cost.RetailChargeCents < 0 || event.Cost.WholesaleCostCents < 0 {
		return errors.New("usage event cost counters must not be negative")
	}
	if event.RequestLog == nil {
		return errors.New("usage event request_log is required")
	}
	return nil
}
