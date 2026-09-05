package ledger

import (
	"context"
	"log/slog"
	"sync"

	"github.com/hezhaoqian1/coincoin-proxy/usage-quota-service/internal/events"
)

type WriteResult struct {
	Duplicate bool
	DryRun    bool
	Status    string
}

type Writer interface {
	WriteUsageEvent(ctx context.Context, event events.UsageEvent) (WriteResult, error)
}

type IdempotencyStore interface {
	Begin(ctx context.Context, eventID string) (bool, error)
	Complete(ctx context.Context, eventID string) error
	Fail(ctx context.Context, eventID string) error
}

type InMemoryIdempotencyStore struct {
	mu     sync.Mutex
	states map[string]string
}

func NewInMemoryIdempotencyStore() *InMemoryIdempotencyStore {
	return &InMemoryIdempotencyStore{states: make(map[string]string)}
}

func (store *InMemoryIdempotencyStore) Begin(_ context.Context, eventID string) (bool, error) {
	store.mu.Lock()
	defer store.mu.Unlock()
	if store.states[eventID] == "completed" || store.states[eventID] == "processing" {
		return false, nil
	}
	store.states[eventID] = "processing"
	return true, nil
}

func (store *InMemoryIdempotencyStore) Complete(_ context.Context, eventID string) error {
	store.mu.Lock()
	defer store.mu.Unlock()
	store.states[eventID] = "completed"
	return nil
}

func (store *InMemoryIdempotencyStore) Fail(_ context.Context, eventID string) error {
	store.mu.Lock()
	defer store.mu.Unlock()
	delete(store.states, eventID)
	return nil
}

type DryRunWriter struct {
	store        IdempotencyStore
	summaryStore SummaryStore
	logger       *slog.Logger
}

func NewDryRunWriter(store IdempotencyStore, logger *slog.Logger) *DryRunWriter {
	return NewDryRunWriterWithSummary(store, nil, logger)
}

func NewDryRunWriterWithSummary(store IdempotencyStore, summaryStore SummaryStore, logger *slog.Logger) *DryRunWriter {
	if store == nil {
		store = NewInMemoryIdempotencyStore()
	}
	if logger == nil {
		logger = slog.Default()
	}
	return &DryRunWriter{store: store, summaryStore: summaryStore, logger: logger}
}

func (writer *DryRunWriter) WriteUsageEvent(ctx context.Context, event events.UsageEvent) (WriteResult, error) {
	ok, err := writer.store.Begin(ctx, event.EventID)
	if err != nil {
		return WriteResult{}, err
	}
	if !ok {
		return WriteResult{Duplicate: true, DryRun: true, Status: "duplicate"}, nil
	}
	if writer.summaryStore != nil {
		result, err := writer.summaryStore.RecordUsageEvent(ctx, event)
		if err != nil {
			_ = writer.store.Fail(ctx, event.EventID)
			return WriteResult{}, err
		}
		if result.Duplicate {
			if err := writer.store.Complete(ctx, event.EventID); err != nil {
				return WriteResult{}, err
			}
			return WriteResult{Duplicate: true, DryRun: true, Status: "duplicate"}, nil
		}
	}
	if err := writer.store.Complete(ctx, event.EventID); err != nil {
		return WriteResult{}, err
	}
	writer.logger.Info(
		"usage event dry-run accepted",
		"event_id", event.EventID,
		"user_id", event.UserID,
		"api_key_id", event.APIKeyID,
		"unit_type", event.Usage.UnitType,
		"unit_count", event.Usage.UnitCount,
		"cost_cents", event.Cost.CostCents,
	)
	return WriteResult{DryRun: true, Status: "accepted"}, nil
}
