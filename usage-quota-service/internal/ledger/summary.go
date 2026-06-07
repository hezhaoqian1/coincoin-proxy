package ledger

import (
	"context"
	"errors"
	"fmt"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/hezhaoqian1/coincoin-proxy/usage-quota-service/internal/events"
	"github.com/redis/go-redis/v9"
)

const shadowSummaryLua = `
local added = redis.call("SET", KEYS[1], "1", "NX", "EX", ARGV[1])
if not added then
  return {0, "duplicate"}
end

local field_count = tonumber(ARGV[2])
for key_index = 2, #KEYS do
  for i = 1, field_count do
    local name = ARGV[3 + ((i - 1) * 2)]
    local amount = tonumber(ARGV[4 + ((i - 1) * 2)])
    if amount ~= 0 then
      redis.call("HINCRBY", KEYS[key_index], name, amount)
    end
  end
  redis.call("EXPIRE", KEYS[key_index], ARGV[1])
end

return {1, "accepted"}
`

type SummaryQuery struct {
	Day      string `json:"day"`
	UserID   string `json:"user_id,omitempty"`
	APIKeyID string `json:"api_key_id,omitempty"`
	Model    string `json:"model,omitempty"`
}

type UsageSummary struct {
	SummaryQuery
	Events              int64 `json:"events"`
	UnitCount           int64 `json:"unit_count"`
	InputTokens         int64 `json:"input_tokens"`
	OutputTokens        int64 `json:"output_tokens"`
	CacheReadTokens     int64 `json:"cache_read_tokens"`
	CacheCreationTokens int64 `json:"cache_creation_tokens"`
	ImageCount          int64 `json:"image_count"`
	VideoCount          int64 `json:"video_count"`
	CostCents           int64 `json:"cost_cents"`
	RetailChargeCents   int64 `json:"retail_charge_cents"`
	WholesaleCostCents  int64 `json:"wholesale_cost_cents"`
}

type SummaryRecordResult struct {
	Duplicate bool
}

type SummaryStore interface {
	RecordUsageEvent(ctx context.Context, event events.UsageEvent) (SummaryRecordResult, error)
	GetSummary(ctx context.Context, query SummaryQuery) (UsageSummary, error)
}

type MemorySummaryStore struct {
	mu       sync.Mutex
	seen     map[string]struct{}
	counters map[string]UsageSummary
}

func NewMemorySummaryStore() *MemorySummaryStore {
	return &MemorySummaryStore{
		seen:     make(map[string]struct{}),
		counters: make(map[string]UsageSummary),
	}
}

func (store *MemorySummaryStore) RecordUsageEvent(_ context.Context, event events.UsageEvent) (SummaryRecordResult, error) {
	contribution, err := contributionFromEvent(event)
	if err != nil {
		return SummaryRecordResult{}, err
	}

	store.mu.Lock()
	defer store.mu.Unlock()
	if _, ok := store.seen[event.EventID]; ok {
		return SummaryRecordResult{Duplicate: true}, nil
	}
	store.seen[event.EventID] = struct{}{}
	for _, query := range rollupQueries(contribution.SummaryQuery) {
		key := summaryKey("memory", query)
		existing := store.counters[key]
		if existing.Day == "" {
			existing.SummaryQuery = query
		}
		store.counters[key] = addSummary(existing, contribution)
	}
	return SummaryRecordResult{}, nil
}

func (store *MemorySummaryStore) GetSummary(_ context.Context, query SummaryQuery) (UsageSummary, error) {
	if err := validateSummaryQuery(query); err != nil {
		return UsageSummary{}, err
	}
	store.mu.Lock()
	defer store.mu.Unlock()
	summary := store.counters[summaryKey("memory", query)]
	if summary.Day == "" {
		summary.SummaryQuery = query
	}
	return summary, nil
}

type RedisSummaryStore struct {
	client *redis.Client
	prefix string
	ttl    time.Duration
}

func NewRedisSummaryStore(redisURL string, prefix string, ttl time.Duration) (*RedisSummaryStore, error) {
	if redisURL == "" {
		return nil, errors.New("COINCOIN_REDIS_URL is required")
	}
	options, err := redis.ParseURL(redisURL)
	if err != nil {
		return nil, err
	}
	if prefix == "" {
		prefix = "coincoin"
	}
	if ttl <= 0 {
		ttl = 90 * 24 * time.Hour
	}
	return &RedisSummaryStore{client: redis.NewClient(options), prefix: prefix, ttl: ttl}, nil
}

func (store *RedisSummaryStore) RecordUsageEvent(ctx context.Context, event events.UsageEvent) (SummaryRecordResult, error) {
	contribution, err := contributionFromEvent(event)
	if err != nil {
		return SummaryRecordResult{}, err
	}

	keys := []string{store.eventKey(event.EventID)}
	for _, query := range rollupQueries(contribution.SummaryQuery) {
		keys = append(keys, summaryKey(store.prefix, query))
	}
	args := []interface{}{int64(store.ttl.Seconds()), len(summaryFields(contribution))}
	for _, field := range summaryFields(contribution) {
		args = append(args, field.name, field.value)
	}
	result, err := store.client.Eval(ctx, shadowSummaryLua, keys, args...).Slice()
	if err != nil {
		return SummaryRecordResult{}, err
	}
	return SummaryRecordResult{Duplicate: redisInt(result, 0) == 0}, nil
}

func (store *RedisSummaryStore) GetSummary(ctx context.Context, query SummaryQuery) (UsageSummary, error) {
	if err := validateSummaryQuery(query); err != nil {
		return UsageSummary{}, err
	}
	values, err := store.client.HGetAll(ctx, summaryKey(store.prefix, query)).Result()
	if err != nil {
		return UsageSummary{}, err
	}
	return summaryFromFields(query, values), nil
}

func (store *RedisSummaryStore) Close() error {
	return store.client.Close()
}

func (store *RedisSummaryStore) eventKey(eventID string) string {
	return store.prefix + ":usage:shadow:event:" + sanitizeKeyPart(eventID)
}

func contributionFromEvent(event events.UsageEvent) (UsageSummary, error) {
	createdAt, err := time.Parse(time.RFC3339Nano, event.CreatedAt)
	if err != nil {
		return UsageSummary{}, fmt.Errorf("parse usage event created_at: %w", err)
	}
	return UsageSummary{
		SummaryQuery: SummaryQuery{
			Day:      createdAt.UTC().Format(time.DateOnly),
			UserID:   event.UserID,
			APIKeyID: event.APIKeyID,
			Model:    eventModel(event),
		},
		Events:              1,
		UnitCount:           event.Usage.UnitCount,
		InputTokens:         event.Usage.InputTokens,
		OutputTokens:        event.Usage.OutputTokens,
		CacheReadTokens:     event.Usage.CacheReadTokens,
		CacheCreationTokens: event.Usage.CacheCreationTokens,
		ImageCount:          event.Usage.ImageCount,
		VideoCount:          event.Usage.VideoCount,
		CostCents:           event.Cost.CostCents,
		RetailChargeCents:   event.Cost.RetailChargeCents,
		WholesaleCostCents:  event.Cost.WholesaleCostCents,
	}, nil
}

func eventModel(event events.UsageEvent) string {
	for _, key := range []string{"resolved_public_model", "model", "customer_model_alias", "provider_model"} {
		if value, ok := event.RequestLog[key]; ok {
			text := strings.TrimSpace(fmt.Sprint(value))
			if text != "" {
				return text
			}
		}
	}
	return "_unknown"
}

func rollupQueries(base SummaryQuery) []SummaryQuery {
	queries := []SummaryQuery{{Day: base.Day}}
	if base.UserID != "" {
		queries = append(queries, SummaryQuery{Day: base.Day, UserID: base.UserID})
	}
	if base.APIKeyID != "" {
		queries = append(queries, SummaryQuery{Day: base.Day, APIKeyID: base.APIKeyID})
	}
	if base.Model != "" {
		queries = append(queries, SummaryQuery{Day: base.Day, Model: base.Model})
	}
	if base.UserID != "" && base.APIKeyID != "" {
		queries = append(queries, SummaryQuery{Day: base.Day, UserID: base.UserID, APIKeyID: base.APIKeyID})
	}
	if base.UserID != "" && base.Model != "" {
		queries = append(queries, SummaryQuery{Day: base.Day, UserID: base.UserID, Model: base.Model})
	}
	if base.APIKeyID != "" && base.Model != "" {
		queries = append(queries, SummaryQuery{Day: base.Day, APIKeyID: base.APIKeyID, Model: base.Model})
	}
	if base.UserID != "" && base.APIKeyID != "" && base.Model != "" {
		queries = append(queries, base)
	}
	return queries
}

func validateSummaryQuery(query SummaryQuery) error {
	if query.Day == "" {
		return errors.New("day is required")
	}
	if _, err := time.Parse(time.DateOnly, query.Day); err != nil {
		return fmt.Errorf("day must be YYYY-MM-DD: %w", err)
	}
	return nil
}

func summaryKey(prefix string, query SummaryQuery) string {
	parts := []string{prefix, "usage", "shadow", "summary", query.Day}
	if query.UserID != "" {
		parts = append(parts, "user", sanitizeKeyPart(query.UserID))
	}
	if query.APIKeyID != "" {
		parts = append(parts, "api_key", sanitizeKeyPart(query.APIKeyID))
	}
	if query.Model != "" {
		parts = append(parts, "model", sanitizeKeyPart(query.Model))
	}
	if len(parts) == 5 {
		parts = append(parts, "global")
	}
	return strings.Join(parts, ":")
}

type summaryField struct {
	name  string
	value int64
}

func summaryFields(summary UsageSummary) []summaryField {
	return []summaryField{
		{name: "events", value: summary.Events},
		{name: "unit_count", value: summary.UnitCount},
		{name: "input_tokens", value: summary.InputTokens},
		{name: "output_tokens", value: summary.OutputTokens},
		{name: "cache_read_tokens", value: summary.CacheReadTokens},
		{name: "cache_creation_tokens", value: summary.CacheCreationTokens},
		{name: "image_count", value: summary.ImageCount},
		{name: "video_count", value: summary.VideoCount},
		{name: "cost_cents", value: summary.CostCents},
		{name: "retail_charge_cents", value: summary.RetailChargeCents},
		{name: "wholesale_cost_cents", value: summary.WholesaleCostCents},
	}
}

func addSummary(left UsageSummary, right UsageSummary) UsageSummary {
	left.Events += right.Events
	left.UnitCount += right.UnitCount
	left.InputTokens += right.InputTokens
	left.OutputTokens += right.OutputTokens
	left.CacheReadTokens += right.CacheReadTokens
	left.CacheCreationTokens += right.CacheCreationTokens
	left.ImageCount += right.ImageCount
	left.VideoCount += right.VideoCount
	left.CostCents += right.CostCents
	left.RetailChargeCents += right.RetailChargeCents
	left.WholesaleCostCents += right.WholesaleCostCents
	return left
}

func summaryFromFields(query SummaryQuery, fields map[string]string) UsageSummary {
	return UsageSummary{
		SummaryQuery:        query,
		Events:              intField(fields, "events"),
		UnitCount:           intField(fields, "unit_count"),
		InputTokens:         intField(fields, "input_tokens"),
		OutputTokens:        intField(fields, "output_tokens"),
		CacheReadTokens:     intField(fields, "cache_read_tokens"),
		CacheCreationTokens: intField(fields, "cache_creation_tokens"),
		ImageCount:          intField(fields, "image_count"),
		VideoCount:          intField(fields, "video_count"),
		CostCents:           intField(fields, "cost_cents"),
		RetailChargeCents:   intField(fields, "retail_charge_cents"),
		WholesaleCostCents:  intField(fields, "wholesale_cost_cents"),
	}
}

func intField(fields map[string]string, name string) int64 {
	value, _ := strconv.ParseInt(fields[name], 10, 64)
	return value
}

func redisInt(result []interface{}, index int) int64 {
	if index >= len(result) {
		return 0
	}
	switch value := result[index].(type) {
	case int64:
		return value
	case string:
		parsed, _ := strconv.ParseInt(value, 10, 64)
		return parsed
	case []byte:
		parsed, _ := strconv.ParseInt(string(value), 10, 64)
		return parsed
	default:
		return 0
	}
}

func sanitizeKeyPart(value string) string {
	replacer := strings.NewReplacer(" ", "_", ":", "_", "/", "_", "\n", "_", "\r", "_", "\t", "_")
	return replacer.Replace(value)
}
