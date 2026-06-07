package ledger

import (
	"context"
	"testing"
	"time"

	"github.com/hezhaoqian1/coincoin-proxy/usage-quota-service/internal/events"
)

func TestMemorySummaryStoreRecordsRollupsIdempotently(t *testing.T) {
	store := NewMemorySummaryStore()
	event := summaryTestEvent("uev_summary_1")

	first, err := store.RecordUsageEvent(context.Background(), event)
	if err != nil {
		t.Fatalf("record first event: %v", err)
	}
	if first.Duplicate {
		t.Fatalf("first record should not be duplicate")
	}
	second, err := store.RecordUsageEvent(context.Background(), event)
	if err != nil {
		t.Fatalf("record duplicate event: %v", err)
	}
	if !second.Duplicate {
		t.Fatalf("second record should be duplicate")
	}

	global := mustSummary(t, store, SummaryQuery{Day: "2026-06-07"})
	if global.Events != 1 || global.UnitCount != 150 || global.CostCents != 42 {
		t.Fatalf("unexpected global summary: %#v", global)
	}
	if global.InputTokens != 100 || global.OutputTokens != 50 || global.CacheReadTokens != 8 {
		t.Fatalf("unexpected token counters: %#v", global)
	}

	byUserModel := mustSummary(t, store, SummaryQuery{Day: "2026-06-07", UserID: "u_1", Model: "gpt-5.4"})
	if byUserModel.Events != 1 || byUserModel.RetailChargeCents != 45 || byUserModel.WholesaleCostCents != 12 {
		t.Fatalf("unexpected user/model summary: %#v", byUserModel)
	}

	empty := mustSummary(t, store, SummaryQuery{Day: "2026-06-07", UserID: "missing"})
	if empty.Events != 0 || empty.Day != "2026-06-07" || empty.UserID != "missing" {
		t.Fatalf("unexpected empty summary: %#v", empty)
	}
}

func TestDryRunWriterRecordsSummaryAndTreatsSummaryDuplicateAsDuplicate(t *testing.T) {
	summaryStore := NewMemorySummaryStore()
	writer := NewDryRunWriterWithSummary(NewInMemoryIdempotencyStore(), summaryStore, nil)
	event := summaryTestEvent("uev_writer_1")

	result, err := writer.WriteUsageEvent(context.Background(), event)
	if err != nil {
		t.Fatalf("write first event: %v", err)
	}
	if result.Duplicate || !result.DryRun {
		t.Fatalf("unexpected first result: %#v", result)
	}

	restartedWriter := NewDryRunWriterWithSummary(NewInMemoryIdempotencyStore(), summaryStore, nil)
	result, err = restartedWriter.WriteUsageEvent(context.Background(), event)
	if err != nil {
		t.Fatalf("write duplicate event after restart: %v", err)
	}
	if !result.Duplicate {
		t.Fatalf("expected summary duplicate after writer restart: %#v", result)
	}

	summary := mustSummary(t, summaryStore, SummaryQuery{Day: "2026-06-07", UserID: "u_1"})
	if summary.Events != 1 || summary.CostCents != 42 {
		t.Fatalf("duplicate should not increment summary: %#v", summary)
	}
}

func TestMemorySummaryStoreRejectsBadQuery(t *testing.T) {
	store := NewMemorySummaryStore()
	if _, err := store.GetSummary(context.Background(), SummaryQuery{}); err == nil {
		t.Fatalf("missing day should fail")
	}
	if _, err := store.GetSummary(context.Background(), SummaryQuery{Day: "06-07-2026"}); err == nil {
		t.Fatalf("bad day should fail")
	}
}

func mustSummary(t *testing.T, store SummaryStore, query SummaryQuery) UsageSummary {
	t.Helper()
	summary, err := store.GetSummary(context.Background(), query)
	if err != nil {
		t.Fatal(err)
	}
	return summary
}

func summaryTestEvent(eventID string) events.UsageEvent {
	return events.UsageEvent{
		SchemaVersion: events.CurrentSchemaVersion,
		EventID:       eventID,
		EventType:     "usage.recorded",
		Status:        "received",
		UserID:        "u_1",
		APIKeyID:      "key_1",
		RequestID:     "req_1",
		CreatedAt:     time.Date(2026, 6, 7, 10, 11, 12, 0, time.UTC).Format(time.RFC3339Nano),
		Usage: events.UsageFields{
			UnitType:            "tokens",
			UnitCount:           150,
			InputTokens:         100,
			OutputTokens:        50,
			CacheReadTokens:     8,
			CacheCreationTokens: 3,
			ImageCount:          2,
			VideoCount:          1,
		},
		Cost: events.CostFields{
			CostCents:          42,
			RetailChargeCents:  45,
			WholesaleCostCents: 12,
			PricingMode:        "catalog",
			PriceVersion:       7,
		},
		RequestLog: map[string]interface{}{
			"model":                 "gpt-5.4",
			"resolved_public_model": "gpt-5.4",
		},
	}
}
