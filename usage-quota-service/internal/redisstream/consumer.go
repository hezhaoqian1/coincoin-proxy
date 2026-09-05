package redisstream

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"sync/atomic"
	"time"

	"github.com/hezhaoqian1/coincoin-proxy/usage-quota-service/internal/events"
	"github.com/hezhaoqian1/coincoin-proxy/usage-quota-service/internal/ledger"
)

type Message struct {
	ID     string
	Values map[string]string
}

type PendingMessage struct {
	ID            string
	Consumer      string
	Idle          time.Duration
	DeliveryCount int64
}

type Client interface {
	CreateGroup(ctx context.Context, stream string, group string) error
	ReadGroup(ctx context.Context, stream string, group string, consumer string, start string, count int64, block time.Duration) ([]Message, error)
	Pending(ctx context.Context, stream string, group string, count int64) ([]PendingMessage, error)
	Claim(ctx context.Context, stream string, group string, consumer string, minIdle time.Duration, ids []string) ([]Message, error)
	Ack(ctx context.Context, stream string, group string, ids ...string) error
	Add(ctx context.Context, stream string, values map[string]interface{}) (string, error)
	Close() error
}

type Config struct {
	Stream           string
	Group            string
	Consumer         string
	DeadLetterStream string
	BatchSize        int64
	BlockTimeout     time.Duration
	ReclaimMinIdle   time.Duration
	MaxAttempts      int64
}

type Stats struct {
	Received      int64 `json:"received"`
	Processed     int64 `json:"processed"`
	Duplicates    int64 `json:"duplicates"`
	ParseErrors   int64 `json:"parse_errors"`
	WriteErrors   int64 `json:"write_errors"`
	DeadLettered  int64 `json:"dead_lettered"`
	AckErrors     int64 `json:"ack_errors"`
	Claimed       int64 `json:"claimed"`
	LastErrorUnix int64 `json:"last_error_unix"`
}

type Consumer struct {
	cfg    Config
	client Client
	writer ledger.Writer
	logger *slog.Logger
	stats  Stats
}

func NewConsumer(cfg Config, client Client, writer ledger.Writer, logger *slog.Logger) (*Consumer, error) {
	if cfg.Stream == "" || cfg.Group == "" || cfg.Consumer == "" || cfg.DeadLetterStream == "" {
		return nil, errors.New("stream, group, consumer, and dead-letter stream are required")
	}
	if cfg.BatchSize <= 0 {
		cfg.BatchSize = 100
	}
	if cfg.BlockTimeout <= 0 {
		cfg.BlockTimeout = 5 * time.Second
	}
	if cfg.MaxAttempts <= 0 {
		cfg.MaxAttempts = 5
	}
	if client == nil {
		return nil, errors.New("redis stream client is required")
	}
	if writer == nil {
		return nil, errors.New("ledger writer is required")
	}
	if logger == nil {
		logger = slog.Default()
	}
	return &Consumer{cfg: cfg, client: client, writer: writer, logger: logger}, nil
}

func (consumer *Consumer) EnsureGroup(ctx context.Context) error {
	return consumer.client.CreateGroup(ctx, consumer.cfg.Stream, consumer.cfg.Group)
}

func (consumer *Consumer) Run(ctx context.Context) error {
	if err := consumer.EnsureGroup(ctx); err != nil {
		return err
	}
	for {
		if err := ctx.Err(); err != nil {
			return err
		}
		if err := consumer.reclaimPending(ctx); err != nil {
			consumer.rememberError(err)
			consumer.logger.Warn("pending usage events reclaim failed", "error", err)
		}
		messages, err := consumer.client.ReadGroup(
			ctx,
			consumer.cfg.Stream,
			consumer.cfg.Group,
			consumer.cfg.Consumer,
			">",
			consumer.cfg.BatchSize,
			consumer.cfg.BlockTimeout,
		)
		if err != nil {
			if ctx.Err() != nil {
				return ctx.Err()
			}
			consumer.rememberError(err)
			consumer.logger.Warn("usage events read failed", "error", err)
			continue
		}
		consumer.ProcessBatch(ctx, messages)
	}
}

func (consumer *Consumer) ProcessBatch(ctx context.Context, messages []Message) {
	for _, message := range messages {
		if err := consumer.ProcessMessage(ctx, message); err != nil {
			consumer.rememberError(err)
			consumer.logger.Warn("usage event processing failed", "stream_id", message.ID, "error", err)
		}
	}
}

func (consumer *Consumer) ProcessMessage(ctx context.Context, message Message) error {
	atomic.AddInt64(&consumer.stats.Received, 1)
	envelope, err := events.ParseStreamFields(message.ID, message.Values)
	if err != nil {
		atomic.AddInt64(&consumer.stats.ParseErrors, 1)
		return consumer.deadLetterAndAck(ctx, message, "parse_error", err)
	}

	result, err := consumer.writer.WriteUsageEvent(ctx, envelope.Event)
	if err != nil {
		atomic.AddInt64(&consumer.stats.WriteErrors, 1)
		attempts := envelope.Attempts + 1
		if attempts >= consumer.cfg.MaxAttempts {
			return consumer.deadLetterAndAck(ctx, withAttempts(message, attempts), "write_error", err)
		}
		_, addErr := consumer.client.Add(ctx, consumer.cfg.Stream, valuesWithAttempts(message.Values, attempts))
		if addErr != nil {
			return fmt.Errorf("requeue failed after writer error %v: %w", err, addErr)
		}
		if ackErr := consumer.client.Ack(ctx, consumer.cfg.Stream, consumer.cfg.Group, message.ID); ackErr != nil {
			atomic.AddInt64(&consumer.stats.AckErrors, 1)
			return ackErr
		}
		return nil
	}
	if result.Duplicate {
		atomic.AddInt64(&consumer.stats.Duplicates, 1)
	} else {
		atomic.AddInt64(&consumer.stats.Processed, 1)
	}
	if err := consumer.client.Ack(ctx, consumer.cfg.Stream, consumer.cfg.Group, message.ID); err != nil {
		atomic.AddInt64(&consumer.stats.AckErrors, 1)
		return err
	}
	return nil
}

func (consumer *Consumer) reclaimPending(ctx context.Context) error {
	if consumer.cfg.ReclaimMinIdle <= 0 {
		return nil
	}
	pending, err := consumer.client.Pending(ctx, consumer.cfg.Stream, consumer.cfg.Group, consumer.cfg.BatchSize)
	if err != nil {
		return err
	}
	var ids []string
	for _, item := range pending {
		if item.Consumer == consumer.cfg.Consumer {
			continue
		}
		if item.Idle >= consumer.cfg.ReclaimMinIdle {
			ids = append(ids, item.ID)
		}
	}
	if len(ids) == 0 {
		return nil
	}
	messages, err := consumer.client.Claim(ctx, consumer.cfg.Stream, consumer.cfg.Group, consumer.cfg.Consumer, consumer.cfg.ReclaimMinIdle, ids)
	if err != nil {
		return err
	}
	atomic.AddInt64(&consumer.stats.Claimed, int64(len(messages)))
	consumer.ProcessBatch(ctx, messages)
	return nil
}

func (consumer *Consumer) deadLetterAndAck(ctx context.Context, message Message, reason string, cause error) error {
	payloadBytes, _ := json.Marshal(message.Values)
	values := map[string]interface{}{
		"source_stream": consumer.cfg.Stream,
		"source_group":  consumer.cfg.Group,
		"source_id":     message.ID,
		"reason":        reason,
		"error":         cause.Error(),
		"payload":       string(payloadBytes),
		"created_at":    time.Now().UTC().Format(time.RFC3339Nano),
	}
	if _, err := consumer.client.Add(ctx, consumer.cfg.DeadLetterStream, values); err != nil {
		return fmt.Errorf("dead-letter add failed: %w", err)
	}
	atomic.AddInt64(&consumer.stats.DeadLettered, 1)
	if err := consumer.client.Ack(ctx, consumer.cfg.Stream, consumer.cfg.Group, message.ID); err != nil {
		atomic.AddInt64(&consumer.stats.AckErrors, 1)
		return err
	}
	return nil
}

func (consumer *Consumer) Stats() Stats {
	return Stats{
		Received:      atomic.LoadInt64(&consumer.stats.Received),
		Processed:     atomic.LoadInt64(&consumer.stats.Processed),
		Duplicates:    atomic.LoadInt64(&consumer.stats.Duplicates),
		ParseErrors:   atomic.LoadInt64(&consumer.stats.ParseErrors),
		WriteErrors:   atomic.LoadInt64(&consumer.stats.WriteErrors),
		DeadLettered:  atomic.LoadInt64(&consumer.stats.DeadLettered),
		AckErrors:     atomic.LoadInt64(&consumer.stats.AckErrors),
		Claimed:       atomic.LoadInt64(&consumer.stats.Claimed),
		LastErrorUnix: atomic.LoadInt64(&consumer.stats.LastErrorUnix),
	}
}

func (consumer *Consumer) rememberError(err error) {
	if err == nil {
		return
	}
	atomic.StoreInt64(&consumer.stats.LastErrorUnix, time.Now().Unix())
}

func valuesWithAttempts(values map[string]string, attempts int64) map[string]interface{} {
	next := make(map[string]interface{}, len(values)+1)
	for key, value := range values {
		next[key] = value
	}
	next["attempts"] = fmt.Sprintf("%d", attempts)
	return next
}

func withAttempts(message Message, attempts int64) Message {
	next := make(map[string]string, len(message.Values)+1)
	for key, value := range message.Values {
		next[key] = value
	}
	next["attempts"] = fmt.Sprintf("%d", attempts)
	return Message{ID: message.ID, Values: next}
}
