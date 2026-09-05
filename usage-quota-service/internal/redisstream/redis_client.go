package redisstream

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/redis/go-redis/v9"
)

type RedisClient struct {
	client *redis.Client
}

func NewRedisClient(redisURL string) (*RedisClient, error) {
	if redisURL == "" {
		return nil, errors.New("COINCOIN_REDIS_URL is required")
	}
	options, err := redis.ParseURL(redisURL)
	if err != nil {
		return nil, err
	}
	return &RedisClient{client: redis.NewClient(options)}, nil
}

func (client *RedisClient) CreateGroup(ctx context.Context, stream string, group string) error {
	err := client.client.XGroupCreateMkStream(ctx, stream, group, "0").Err()
	if err == nil || isBusyGroup(err) {
		return nil
	}
	return err
}

func (client *RedisClient) ReadGroup(ctx context.Context, stream string, group string, consumer string, start string, count int64, block time.Duration) ([]Message, error) {
	streams, err := client.client.XReadGroup(ctx, &redis.XReadGroupArgs{
		Group:    group,
		Consumer: consumer,
		Streams:  []string{stream, start},
		Count:    count,
		Block:    block,
	}).Result()
	if err != nil {
		if err == redis.Nil {
			return nil, nil
		}
		return nil, err
	}
	return convertStreams(streams), nil
}

func (client *RedisClient) Pending(ctx context.Context, stream string, group string, count int64) ([]PendingMessage, error) {
	result, err := client.client.XPendingExt(ctx, &redis.XPendingExtArgs{
		Stream: stream,
		Group:  group,
		Start:  "-",
		End:    "+",
		Count:  count,
	}).Result()
	if err != nil {
		if err == redis.Nil {
			return nil, nil
		}
		return nil, err
	}
	pending := make([]PendingMessage, 0, len(result))
	for _, item := range result {
		pending = append(pending, PendingMessage{
			ID:            item.ID,
			Consumer:      item.Consumer,
			Idle:          item.Idle,
			DeliveryCount: item.RetryCount,
		})
	}
	return pending, nil
}

func (client *RedisClient) Claim(ctx context.Context, stream string, group string, consumer string, minIdle time.Duration, ids []string) ([]Message, error) {
	if len(ids) == 0 {
		return nil, nil
	}
	result, err := client.client.XClaim(ctx, &redis.XClaimArgs{
		Stream:   stream,
		Group:    group,
		Consumer: consumer,
		MinIdle:  minIdle,
		Messages: ids,
	}).Result()
	if err != nil {
		if err == redis.Nil {
			return nil, nil
		}
		return nil, err
	}
	return convertMessages(result), nil
}

func (client *RedisClient) Ack(ctx context.Context, stream string, group string, ids ...string) error {
	if len(ids) == 0 {
		return nil
	}
	return client.client.XAck(ctx, stream, group, ids...).Err()
}

func (client *RedisClient) Add(ctx context.Context, stream string, values map[string]interface{}) (string, error) {
	return client.client.XAdd(ctx, &redis.XAddArgs{Stream: stream, Values: values}).Result()
}

func (client *RedisClient) Close() error {
	return client.client.Close()
}

func convertStreams(streams []redis.XStream) []Message {
	var messages []Message
	for _, stream := range streams {
		messages = append(messages, convertMessages(stream.Messages)...)
	}
	return messages
}

func convertMessages(redisMessages []redis.XMessage) []Message {
	messages := make([]Message, 0, len(redisMessages))
	for _, message := range redisMessages {
		values := make(map[string]string, len(message.Values))
		for key, value := range message.Values {
			values[key] = fmt.Sprint(value)
		}
		messages = append(messages, Message{ID: message.ID, Values: values})
	}
	return messages
}

func isBusyGroup(err error) bool {
	return err != nil && strings.HasPrefix(err.Error(), "BUSYGROUP")
}
