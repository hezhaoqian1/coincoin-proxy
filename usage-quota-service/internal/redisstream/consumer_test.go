package redisstream

import (
	"context"
	"encoding/json"
	"errors"
	"testing"
	"time"

	"github.com/hezhaoqian1/coincoin-proxy/usage-quota-service/internal/events"
	"github.com/hezhaoqian1/coincoin-proxy/usage-quota-service/internal/ledger"
)

type fakeClient struct {
	acked       []string
	addedStream []string
	addedValues []map[string]interface{}
	pending     []PendingMessage
	claimed     []Message
}

func (client *fakeClient) CreateGroup(context.Context, string, string) error { return nil }
func (client *fakeClient) ReadGroup(context.Context, string, string, string, string, int64, time.Duration) ([]Message, error) {
	return nil, nil
}
func (client *fakeClient) Pending(context.Context, string, string, int64) ([]PendingMessage, error) {
	return client.pending, nil
}
func (client *fakeClient) Claim(context.Context, string, string, string, time.Duration, []string) ([]Message, error) {
	return client.claimed, nil
}
func (client *fakeClient) Ack(_ context.Context, _ string, _ string, ids ...string) error {
	client.acked = append(client.acked, ids...)
	return nil
}
func (client *fakeClient) Add(_ context.Context, stream string, values map[string]interface{}) (string, error) {
	client.addedStream = append(client.addedStream, stream)
	client.addedValues = append(client.addedValues, values)
	return "2-0", nil
}
func (client *fakeClient) Close() error { return nil }

type failingWriter struct{}

func (failingWriter) WriteUsageEvent(context.Context, events.UsageEvent) (ledger.WriteResult, error) {
	return ledger.WriteResult{}, errors.New("writer down")
}

func TestConsumerProcessesValidMessageAndAcks(t *testing.T) {
	client := &fakeClient{}
	consumer := newTestConsumer(t, client, ledger.NewDryRunWriter(ledger.NewInMemoryIdempotencyStore(), nil), 5)

	if err := consumer.ProcessMessage(context.Background(), validMessage("1-0", "uev_1", "")); err != nil {
		t.Fatalf("ProcessMessage returned error: %v", err)
	}
	if len(client.acked) != 1 || client.acked[0] != "1-0" {
		t.Fatalf("expected ack for processed message, got %#v", client.acked)
	}
	stats := consumer.Stats()
	if stats.Processed != 1 || stats.DeadLettered != 0 {
		t.Fatalf("unexpected stats: %#v", stats)
	}
}

func TestConsumerDeadLettersMalformedMessage(t *testing.T) {
	client := &fakeClient{}
	consumer := newTestConsumer(t, client, ledger.NewDryRunWriter(ledger.NewInMemoryIdempotencyStore(), nil), 5)

	err := consumer.ProcessMessage(context.Background(), Message{ID: "bad-0", Values: map[string]string{"payload": "{bad"}})
	if err != nil {
		t.Fatalf("ProcessMessage returned error: %v", err)
	}
	if len(client.acked) != 1 || client.acked[0] != "bad-0" {
		t.Fatalf("expected malformed message ack after DLQ, got %#v", client.acked)
	}
	if len(client.addedStream) != 1 || client.addedStream[0] != "coincoin:usage:events:dlq" {
		t.Fatalf("expected DLQ add, got streams=%#v values=%#v", client.addedStream, client.addedValues)
	}
	stats := consumer.Stats()
	if stats.ParseErrors != 1 || stats.DeadLettered != 1 {
		t.Fatalf("unexpected stats: %#v", stats)
	}
}

func TestConsumerRequeuesWriterFailureBeforeMaxAttempts(t *testing.T) {
	client := &fakeClient{}
	consumer := newTestConsumer(t, client, failingWriter{}, 5)

	err := consumer.ProcessMessage(context.Background(), validMessage("1-0", "uev_retry", "3"))
	if err != nil {
		t.Fatalf("ProcessMessage returned error: %v", err)
	}
	if len(client.acked) != 1 || client.acked[0] != "1-0" {
		t.Fatalf("expected original message ack after requeue, got %#v", client.acked)
	}
	if len(client.addedStream) != 1 || client.addedStream[0] != "coincoin:usage:events" {
		t.Fatalf("expected requeue to source stream, got %#v", client.addedStream)
	}
	if client.addedValues[0]["attempts"] != "4" {
		t.Fatalf("expected attempts to increment to 4, got %#v", client.addedValues[0])
	}
}

func TestConsumerDeadLettersWriterFailureAtMaxAttempts(t *testing.T) {
	client := &fakeClient{}
	consumer := newTestConsumer(t, client, failingWriter{}, 5)

	err := consumer.ProcessMessage(context.Background(), validMessage("1-0", "uev_dlq", "4"))
	if err != nil {
		t.Fatalf("ProcessMessage returned error: %v", err)
	}
	if len(client.addedStream) != 1 || client.addedStream[0] != "coincoin:usage:events:dlq" {
		t.Fatalf("expected writer failure DLQ, got %#v", client.addedStream)
	}
	stats := consumer.Stats()
	if stats.WriteErrors != 1 || stats.DeadLettered != 1 {
		t.Fatalf("unexpected stats: %#v", stats)
	}
}

func newTestConsumer(t *testing.T, client Client, writer ledger.Writer, maxAttempts int64) *Consumer {
	t.Helper()
	consumer, err := NewConsumer(Config{
		Stream:           "coincoin:usage:events",
		Group:            "test-group",
		Consumer:         "test-consumer",
		DeadLetterStream: "coincoin:usage:events:dlq",
		BatchSize:        10,
		BlockTimeout:     time.Millisecond,
		ReclaimMinIdle:   time.Minute,
		MaxAttempts:      maxAttempts,
	}, client, writer, nil)
	if err != nil {
		t.Fatal(err)
	}
	return consumer
}

func validMessage(id string, eventID string, attempts string) Message {
	event := events.UsageEvent{
		SchemaVersion: events.CurrentSchemaVersion,
		EventID:       eventID,
		EventType:     "usage.recorded",
		Status:        "received",
		UserID:        "u_1",
		CreatedAt:     time.Now().UTC().Format(time.RFC3339Nano),
		Usage:         events.UsageFields{UnitType: "tokens", UnitCount: 2, InputTokens: 1, OutputTokens: 1},
		Cost:          events.CostFields{CostCents: 1, RetailChargeCents: 1},
		RequestLog:    map[string]interface{}{"model": "gpt-5.4"},
	}
	payload, _ := json.Marshal(event)
	values := map[string]string{"payload": string(payload)}
	if attempts != "" {
		values["attempts"] = attempts
	}
	return Message{ID: id, Values: values}
}
