package main

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/hezhaoqian1/coincoin-proxy/usage-quota-service/internal/config"
	"github.com/hezhaoqian1/coincoin-proxy/usage-quota-service/internal/events"
	"github.com/hezhaoqian1/coincoin-proxy/usage-quota-service/internal/ledger"
	"github.com/hezhaoqian1/coincoin-proxy/usage-quota-service/internal/quota"
	"github.com/hezhaoqian1/coincoin-proxy/usage-quota-service/internal/redisstream"
)

type httpFakeStreamClient struct{}

func (httpFakeStreamClient) CreateGroup(context.Context, string, string) error { return nil }
func (httpFakeStreamClient) ReadGroup(context.Context, string, string, string, string, int64, time.Duration) ([]redisstream.Message, error) {
	return nil, nil
}
func (httpFakeStreamClient) Pending(context.Context, string, string, int64) ([]redisstream.PendingMessage, error) {
	return nil, nil
}
func (httpFakeStreamClient) Claim(context.Context, string, string, string, time.Duration, []string) ([]redisstream.Message, error) {
	return nil, nil
}
func (httpFakeStreamClient) Ack(context.Context, string, string, ...string) error { return nil }
func (httpFakeStreamClient) Add(context.Context, string, map[string]interface{}) (string, error) {
	return "1-0", nil
}
func (httpFakeStreamClient) Close() error { return nil }

func TestQuotaHTTPReserveReleaseAndCommit(t *testing.T) {
	server := newTestServer(t)

	response := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodPost, "/v1/quota/reserve", strings.NewReader(`{
		"reservation_id": "qres_http",
		"user_id": "u_http",
		"estimated_cost_cents": 50,
		"available_balance_cents": 100,
		"concurrency_limits": [{"dimension": "user", "id": "u_http", "limit": 1}]
	}`))
	server.Handler.ServeHTTP(response, request)
	if response.Code != http.StatusOK {
		t.Fatalf("reserve status=%d body=%s", response.Code, response.Body.String())
	}
	var decision quota.ReservationDecision
	if err := json.Unmarshal(response.Body.Bytes(), &decision); err != nil {
		t.Fatal(err)
	}
	if !decision.Allowed || decision.ReservationID != "qres_http" {
		t.Fatalf("unexpected reserve decision: %#v", decision)
	}

	blocked := httptest.NewRecorder()
	request = httptest.NewRequest(http.MethodPost, "/v1/quota/reserve", strings.NewReader(`{
		"reservation_id": "qres_http_2",
		"user_id": "u_http",
		"estimated_cost_cents": 50,
		"available_balance_cents": 100,
		"concurrency_limits": [{"dimension": "user", "id": "u_http", "limit": 1}]
	}`))
	server.Handler.ServeHTTP(blocked, request)
	if blocked.Code != http.StatusTooManyRequests {
		t.Fatalf("blocked status=%d body=%s", blocked.Code, blocked.Body.String())
	}

	release := httptest.NewRecorder()
	request = httptest.NewRequest(http.MethodPost, "/v1/quota/release", strings.NewReader(`{"reservation_id": "qres_http"}`))
	server.Handler.ServeHTTP(release, request)
	if release.Code != http.StatusOK {
		t.Fatalf("release status=%d body=%s", release.Code, release.Body.String())
	}

	commitMissing := httptest.NewRecorder()
	request = httptest.NewRequest(http.MethodPost, "/v1/quota/commit", strings.NewReader(`{"reservation_id": "missing"}`))
	server.Handler.ServeHTTP(commitMissing, request)
	if commitMissing.Code != http.StatusTooManyRequests {
		t.Fatalf("missing commit status=%d body=%s", commitMissing.Code, commitMissing.Body.String())
	}
}

func TestQuotaHTTPBadRequest(t *testing.T) {
	server := newTestServer(t)
	response := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodPost, "/v1/quota/reserve", strings.NewReader(`{"user_id": ""}`))
	server.Handler.ServeHTTP(response, request)
	if response.Code != http.StatusBadRequest {
		t.Fatalf("expected bad request, got %d body=%s", response.Code, response.Body.String())
	}
}

func TestUsageShadowSummaryHTTP(t *testing.T) {
	summaryStore := ledger.NewMemorySummaryStore()
	_, err := summaryStore.RecordUsageEvent(context.Background(), ledgerTestUsageEvent())
	if err != nil {
		t.Fatal(err)
	}
	server := newTestServerWithSummary(t, summaryStore)

	response := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodGet, "/v1/usage-shadow/summary?day=2026-06-07&user_id=u_http&model=gpt-5.4", nil)
	server.Handler.ServeHTTP(response, request)
	if response.Code != http.StatusOK {
		t.Fatalf("summary status=%d body=%s", response.Code, response.Body.String())
	}
	var summary ledger.UsageSummary
	if err := json.Unmarshal(response.Body.Bytes(), &summary); err != nil {
		t.Fatal(err)
	}
	if summary.Events != 1 || summary.UnitCount != 30 || summary.CostCents != 9 {
		t.Fatalf("unexpected summary: %#v", summary)
	}
}

func TestUsageShadowSummaryHTTPBadDay(t *testing.T) {
	server := newTestServer(t)
	response := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodGet, "/v1/usage-shadow/summary?day=bad-date", nil)
	server.Handler.ServeHTTP(response, request)
	if response.Code != http.StatusBadRequest {
		t.Fatalf("expected bad request, got %d body=%s", response.Code, response.Body.String())
	}
}

func newTestServer(t *testing.T) *http.Server {
	return newTestServerWithSummary(t, ledger.NewMemorySummaryStore())
}

func newTestServerWithSummary(t *testing.T, summaryStore ledger.SummaryStore) *http.Server {
	t.Helper()
	consumer, err := redisstream.NewConsumer(
		redisstream.Config{
			Stream:           "coincoin:usage:events",
			Group:            "test",
			Consumer:         "test",
			DeadLetterStream: "coincoin:usage:events:dlq",
		},
		httpFakeStreamClient{},
		ledger.NewDryRunWriter(ledger.NewInMemoryIdempotencyStore(), nil),
		nil,
	)
	if err != nil {
		t.Fatal(err)
	}
	quotaService, err := quota.NewService(quota.NewMemoryStore())
	if err != nil {
		t.Fatal(err)
	}
	return healthServer(":0", consumer, quotaService, summaryStore, config.Config{DryRun: true})
}

func ledgerTestUsageEvent() events.UsageEvent {
	return events.UsageEvent{
		SchemaVersion: events.CurrentSchemaVersion,
		EventID:       "uev_http_summary",
		EventType:     "usage.recorded",
		Status:        "received",
		UserID:        "u_http",
		APIKeyID:      "key_http",
		RequestID:     "req_http",
		CreatedAt:     time.Date(2026, 6, 7, 9, 0, 0, 0, time.UTC).Format(time.RFC3339Nano),
		Usage:         events.UsageFields{UnitType: "tokens", UnitCount: 30, InputTokens: 10, OutputTokens: 20},
		Cost:          events.CostFields{CostCents: 9, RetailChargeCents: 10, WholesaleCostCents: 3},
		RequestLog:    map[string]interface{}{"model": "gpt-5.4"},
	}
}
