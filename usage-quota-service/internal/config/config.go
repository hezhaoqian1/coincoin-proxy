package config

import (
	"fmt"
	"os"
	"strconv"
	"time"
)

type Config struct {
	RedisURL            string
	RedisKeyPrefix      string
	Stream              string
	Group               string
	Consumer            string
	DeadLetterStream    string
	BatchSize           int
	BlockTimeout        time.Duration
	ReclaimMinIdle      time.Duration
	MaxAttempts         int
	DryRun              bool
	HTTPAddr            string
	DatabaseDSN         string
	ShadowSummaryTTL    time.Duration
	ShutdownGracePeriod time.Duration
}

func Load() (Config, error) {
	hostname, _ := os.Hostname()
	if hostname == "" {
		hostname = "local"
	}

	stream := envString("COINCOIN_USAGE_EVENT_STREAM", "coincoin:usage:events")
	cfg := Config{
		RedisURL:            envString("COINCOIN_REDIS_URL", ""),
		RedisKeyPrefix:      envString("COINCOIN_REDIS_KEY_PREFIX", "coincoin"),
		Stream:              stream,
		Group:               envString("COINCOIN_USAGE_QUOTA_GROUP", "coincoin-usage-quota-service"),
		Consumer:            envString("COINCOIN_USAGE_QUOTA_CONSUMER", hostname),
		DeadLetterStream:    envString("COINCOIN_USAGE_QUOTA_DLQ_STREAM", stream+":dlq"),
		BatchSize:           envInt("COINCOIN_USAGE_QUOTA_BATCH_SIZE", 100),
		BlockTimeout:        envDuration("COINCOIN_USAGE_QUOTA_BLOCK_TIMEOUT", 5*time.Second),
		ReclaimMinIdle:      envDuration("COINCOIN_USAGE_QUOTA_RECLAIM_MIN_IDLE", 2*time.Minute),
		MaxAttempts:         envInt("COINCOIN_USAGE_QUOTA_MAX_ATTEMPTS", 5),
		DryRun:              envBool("COINCOIN_USAGE_QUOTA_DRY_RUN", true),
		HTTPAddr:            envString("COINCOIN_USAGE_QUOTA_HTTP_ADDR", ":8091"),
		DatabaseDSN:         envString("COINCOIN_USAGE_QUOTA_DATABASE_DSN", ""),
		ShadowSummaryTTL:    envDuration("COINCOIN_USAGE_QUOTA_SHADOW_SUMMARY_TTL", 90*24*time.Hour),
		ShutdownGracePeriod: envDuration("COINCOIN_USAGE_QUOTA_SHUTDOWN_GRACE_PERIOD", 10*time.Second),
	}
	if cfg.Stream == "" {
		return Config{}, fmt.Errorf("COINCOIN_USAGE_EVENT_STREAM must not be empty")
	}
	if cfg.Group == "" {
		return Config{}, fmt.Errorf("COINCOIN_USAGE_QUOTA_GROUP must not be empty")
	}
	if cfg.Consumer == "" {
		return Config{}, fmt.Errorf("COINCOIN_USAGE_QUOTA_CONSUMER must not be empty")
	}
	if cfg.DeadLetterStream == "" {
		return Config{}, fmt.Errorf("COINCOIN_USAGE_QUOTA_DLQ_STREAM must not be empty")
	}
	if cfg.BatchSize <= 0 {
		return Config{}, fmt.Errorf("COINCOIN_USAGE_QUOTA_BATCH_SIZE must be positive")
	}
	if cfg.MaxAttempts <= 0 {
		return Config{}, fmt.Errorf("COINCOIN_USAGE_QUOTA_MAX_ATTEMPTS must be positive")
	}
	if !cfg.DryRun {
		return Config{}, fmt.Errorf("COINCOIN_USAGE_QUOTA_DRY_RUN=false is not supported until the DB ledger writer ships")
	}
	return cfg, nil
}

func envString(name string, fallback string) string {
	value := os.Getenv(name)
	if value == "" {
		return fallback
	}
	return value
}

func envBool(name string, fallback bool) bool {
	value := os.Getenv(name)
	if value == "" {
		return fallback
	}
	parsed, err := strconv.ParseBool(value)
	if err != nil {
		return fallback
	}
	return parsed
}

func envInt(name string, fallback int) int {
	value := os.Getenv(name)
	if value == "" {
		return fallback
	}
	parsed, err := strconv.Atoi(value)
	if err != nil {
		return fallback
	}
	return parsed
}

func envDuration(name string, fallback time.Duration) time.Duration {
	value := os.Getenv(name)
	if value == "" {
		return fallback
	}
	if parsed, err := time.ParseDuration(value); err == nil {
		return parsed
	}
	if seconds, err := strconv.ParseFloat(value, 64); err == nil {
		return time.Duration(seconds * float64(time.Second))
	}
	return fallback
}
