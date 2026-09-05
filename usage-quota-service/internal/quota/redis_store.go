package quota

import (
	"context"
	"errors"
	"fmt"
	"strconv"
	"strings"
	"time"

	"github.com/redis/go-redis/v9"
)

const reserveLua = `
local reservation = redis.call("HGET", KEYS[1], "status")
if reservation == "pending" then
  return {1, "duplicate"}
end
if reservation then
  return {1, reservation}
end

local rpm_count = tonumber(ARGV[7])
local concurrency_count = tonumber(ARGV[8])
local key_index = 3
local arg_index = 9

for i = 1, rpm_count do
  local key = KEYS[key_index]
  local limit = tonumber(ARGV[arg_index])
  local ttl = tonumber(ARGV[arg_index + 1])
  local current = tonumber(redis.call("GET", key) or "0")
  if current + 1 > limit then
    return {0, "rpm_exceeded:" .. ARGV[arg_index + 2]}
  end
  key_index = key_index + 1
  arg_index = arg_index + 3
end

for i = 1, concurrency_count do
  local key = KEYS[key_index]
  local limit = tonumber(ARGV[arg_index])
  local current = tonumber(redis.call("GET", key) or "0")
  if current + 1 > limit then
    return {0, "concurrency_exceeded:" .. ARGV[arg_index + 1]}
  end
  key_index = key_index + 1
  arg_index = arg_index + 2
end

local estimated_cost = tonumber(ARGV[2])
local available_balance = tonumber(ARGV[3])
if estimated_cost > 0 then
  local reserved = tonumber(redis.call("GET", KEYS[2]) or "0")
  if reserved + estimated_cost > available_balance then
    return {0, "balance_reserved_exceeded"}
  end
end

key_index = 3
arg_index = 9
for i = 1, rpm_count do
  local key = KEYS[key_index]
  local ttl = tonumber(ARGV[arg_index + 1])
  local current = redis.call("INCR", key)
  if current == 1 then
    redis.call("EXPIRE", key, ttl)
  end
  key_index = key_index + 1
  arg_index = arg_index + 3
end

local concurrency_keys = {}
for i = 1, concurrency_count do
  local key = KEYS[key_index]
  redis.call("INCR", key)
  redis.call("EXPIRE", key, tonumber(ARGV[4]) + 60)
  table.insert(concurrency_keys, key)
  key_index = key_index + 1
  arg_index = arg_index + 2
end

if estimated_cost > 0 then
  redis.call("INCRBY", KEYS[2], estimated_cost)
  redis.call("EXPIRE", KEYS[2], tonumber(ARGV[4]) + 60)
end

redis.call("HSET", KEYS[1],
  "status", "pending",
  "user_id", ARGV[1],
  "estimated_cost_cents", ARGV[2],
  "balance_key", KEYS[2],
  "concurrency_keys", table.concat(concurrency_keys, ","),
  "created_at_unix", ARGV[5],
  "expires_at_unix", ARGV[6])
redis.call("EXPIRE", KEYS[1], tonumber(ARGV[4]) + 86400)
return {1, "reserved"}
`

const finishLua = `
local status = redis.call("HGET", KEYS[1], "status")
if not status then
  return {0, "reservation_missing"}
end
if status ~= "pending" then
  return {1, status}
end

local estimated_cost = tonumber(redis.call("HGET", KEYS[1], "estimated_cost_cents") or "0")
local balance_key = redis.call("HGET", KEYS[1], "balance_key") or ""
if estimated_cost > 0 and balance_key ~= "" then
  local current = tonumber(redis.call("GET", balance_key) or "0")
  if current <= estimated_cost then
    redis.call("DEL", balance_key)
  else
    redis.call("DECRBY", balance_key, estimated_cost)
  end
end

local concurrency_keys = redis.call("HGET", KEYS[1], "concurrency_keys") or ""
for key in string.gmatch(concurrency_keys, "([^,]+)") do
  local current = tonumber(redis.call("GET", key) or "0")
  if current > 0 then
    redis.call("DECR", key)
  end
end

redis.call("HSET", KEYS[1], "status", ARGV[1], "finished_at_unix", ARGV[2], "actual_cost_cents", ARGV[3])
redis.call("EXPIRE", KEYS[1], 86400)
return {1, ARGV[1]}
`

type RedisStore struct {
	client *redis.Client
	prefix string
	now    func() time.Time
}

func NewRedisStore(redisURL string, prefix string) (*RedisStore, error) {
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
	return &RedisStore{client: redis.NewClient(options), prefix: prefix, now: time.Now}, nil
}

func (store *RedisStore) Reserve(ctx Context, request ReservationRequest) (ReservationDecision, error) {
	goCtx := contextFrom(ctx)
	now := store.now().UTC()
	expiresAt := now.Add(time.Duration(request.TTLSeconds) * time.Second)
	keys := []string{
		store.reservationKey(request.ReservationID),
		store.balanceKey(request.UserID),
	}
	args := []interface{}{
		request.UserID,
		request.EstimatedCostCents,
		request.AvailableBalanceCents,
		request.TTLSeconds,
		now.Unix(),
		expiresAt.Unix(),
		len(request.RPMLimits),
		len(request.ConcurrencyLimits),
	}
	for _, limit := range request.RPMLimits {
		keys = append(keys, store.rpmKey(limit, now))
		args = append(args, limit.Limit, limit.WindowSeconds+5, limit.Dimension)
	}
	for _, limit := range request.ConcurrencyLimits {
		keys = append(keys, store.concurrencyKey(limit))
		args = append(args, limit.Limit, limit.Dimension)
	}

	result, err := store.client.Eval(goCtx, reserveLua, keys, args...).Slice()
	if err != nil {
		return ReservationDecision{}, err
	}
	allowed := redisInt(result, 0) == 1
	reason := redisString(result, 1)
	return ReservationDecision{
		Allowed:       allowed,
		ReservationID: request.ReservationID,
		Reason:        reason,
		ExpiresAt:     expiresAt,
	}, nil
}

func (store *RedisStore) Release(ctx Context, reservationID string) (ReservationDecision, error) {
	return store.finish(ctx, reservationID, "released", 0)
}

func (store *RedisStore) Commit(ctx Context, update ReservationUpdate) (ReservationDecision, error) {
	return store.finish(ctx, update.ReservationID, "committed", update.ActualCostCents)
}

func (store *RedisStore) Close() error {
	return store.client.Close()
}

func (store *RedisStore) finish(ctx Context, reservationID string, status string, actualCostCents int64) (ReservationDecision, error) {
	result, err := store.client.Eval(
		contextFrom(ctx),
		finishLua,
		[]string{store.reservationKey(reservationID)},
		status,
		store.now().UTC().Unix(),
		actualCostCents,
	).Slice()
	if err != nil {
		return ReservationDecision{}, err
	}
	allowed := redisInt(result, 0) == 1
	reason := redisString(result, 1)
	return ReservationDecision{Allowed: allowed, ReservationID: reservationID, Reason: reason}, nil
}

func (store *RedisStore) reservationKey(reservationID string) string {
	return store.prefix + ":quota:reservation:" + reservationID
}

func (store *RedisStore) balanceKey(userID string) string {
	return store.prefix + ":quota:reserved_balance:" + userID
}

func (store *RedisStore) rpmKey(limit Limit, now time.Time) string {
	bucket := now.Unix() / maxInt64(1, limit.WindowSeconds)
	return fmt.Sprintf("%s:quota:rpm:%s:%s:%d", store.prefix, sanitize(limit.Dimension), sanitize(limit.ID), bucket)
}

func (store *RedisStore) concurrencyKey(limit Limit) string {
	return fmt.Sprintf("%s:quota:concurrency:%s:%s", store.prefix, sanitize(limit.Dimension), sanitize(limit.ID))
}

func contextFrom(ctx Context) context.Context {
	if realCtx, ok := ctx.(context.Context); ok {
		return realCtx
	}
	return context.Background()
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

func redisString(result []interface{}, index int) string {
	if index >= len(result) {
		return ""
	}
	switch value := result[index].(type) {
	case string:
		return value
	case []byte:
		return string(value)
	default:
		return fmt.Sprint(value)
	}
}

func sanitize(value string) string {
	replacer := strings.NewReplacer(" ", "_", ":", "_", "/", "_", "\n", "_", "\r", "_", "\t", "_")
	return replacer.Replace(value)
}
