package main

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/hezhaoqian1/coincoin-proxy/usage-quota-service/internal/config"
	"github.com/hezhaoqian1/coincoin-proxy/usage-quota-service/internal/ledger"
	"github.com/hezhaoqian1/coincoin-proxy/usage-quota-service/internal/quota"
	"github.com/hezhaoqian1/coincoin-proxy/usage-quota-service/internal/redisstream"
)

func main() {
	logger := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: slog.LevelInfo}))
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	cfg, err := config.Load()
	if err != nil {
		logger.Error("usage quota service config invalid", "error", err)
		os.Exit(1)
	}

	redisClient, err := redisstream.NewRedisClient(cfg.RedisURL)
	if err != nil {
		logger.Error("redis client init failed", "error", err)
		os.Exit(1)
	}
	defer redisClient.Close()
	quotaStore, err := quota.NewRedisStore(cfg.RedisURL, cfg.RedisKeyPrefix)
	if err != nil {
		logger.Error("quota store init failed", "error", err)
		os.Exit(1)
	}
	defer quotaStore.Close()
	quotaService, err := quota.NewService(quotaStore)
	if err != nil {
		logger.Error("quota service init failed", "error", err)
		os.Exit(1)
	}
	summaryStore, err := ledger.NewRedisSummaryStore(cfg.RedisURL, cfg.RedisKeyPrefix, cfg.ShadowSummaryTTL)
	if err != nil {
		logger.Error("usage shadow summary store init failed", "error", err)
		os.Exit(1)
	}
	defer summaryStore.Close()

	consumer, err := redisstream.NewConsumer(redisstream.Config{
		Stream:           cfg.Stream,
		Group:            cfg.Group,
		Consumer:         cfg.Consumer,
		DeadLetterStream: cfg.DeadLetterStream,
		BatchSize:        int64(cfg.BatchSize),
		BlockTimeout:     cfg.BlockTimeout,
		ReclaimMinIdle:   cfg.ReclaimMinIdle,
		MaxAttempts:      int64(cfg.MaxAttempts),
	}, redisClient, ledger.NewDryRunWriterWithSummary(ledger.NewInMemoryIdempotencyStore(), summaryStore, logger), logger)
	if err != nil {
		logger.Error("usage stream consumer init failed", "error", err)
		os.Exit(1)
	}

	server := healthServer(cfg.HTTPAddr, consumer, quotaService, summaryStore, cfg)
	go func() {
		logger.Info("usage quota health server starting", "addr", cfg.HTTPAddr)
		if err := server.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			logger.Error("usage quota health server failed", "error", err)
			stop()
		}
	}()

	errCh := make(chan error, 1)
	go func() {
		logger.Info(
			"usage quota service starting",
			"stream", cfg.Stream,
			"group", cfg.Group,
			"consumer", cfg.Consumer,
			"dry_run", cfg.DryRun,
			"dlq_stream", cfg.DeadLetterStream,
		)
		errCh <- consumer.Run(ctx)
	}()

	select {
	case <-ctx.Done():
	case err := <-errCh:
		if err != nil && !errors.Is(err, context.Canceled) {
			logger.Error("usage quota service stopped with error", "error", err)
		}
	}

	shutdownCtx, cancel := context.WithTimeout(context.Background(), cfg.ShutdownGracePeriod)
	defer cancel()
	if err := server.Shutdown(shutdownCtx); err != nil {
		logger.Warn("usage quota health server shutdown failed", "error", err)
	}
	logger.Info("usage quota service stopped")
}

func healthServer(addr string, consumer *redisstream.Consumer, quotaService *quota.Service, summaryStore ledger.SummaryStore, cfg config.Config) *http.Server {
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", func(writer http.ResponseWriter, request *http.Request) {
		writer.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(writer).Encode(map[string]interface{}{
			"ok":       true,
			"service":  "usage-quota-service",
			"dry_run":  cfg.DryRun,
			"stream":   cfg.Stream,
			"group":    cfg.Group,
			"consumer": cfg.Consumer,
		})
	})
	mux.HandleFunc("/metrics", func(writer http.ResponseWriter, request *http.Request) {
		writer.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(writer).Encode(consumer.Stats())
	})
	mux.HandleFunc("/v1/usage-shadow/summary", func(writer http.ResponseWriter, request *http.Request) {
		if request.Method != http.MethodGet {
			http.Error(writer, "method not allowed", http.StatusMethodNotAllowed)
			return
		}
		if summaryStore == nil {
			http.Error(writer, "summary store unavailable", http.StatusServiceUnavailable)
			return
		}
		query := ledger.SummaryQuery{
			Day:      request.URL.Query().Get("day"),
			UserID:   request.URL.Query().Get("user_id"),
			APIKeyID: request.URL.Query().Get("api_key_id"),
			Model:    request.URL.Query().Get("model"),
		}
		if query.Day == "" {
			query.Day = time.Now().UTC().Format(time.DateOnly)
		}
		summary, err := summaryStore.GetSummary(request.Context(), query)
		if err != nil {
			http.Error(writer, err.Error(), http.StatusBadRequest)
			return
		}
		writer.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(writer).Encode(summary)
	})
	mux.HandleFunc("/v1/quota/reserve", func(writer http.ResponseWriter, request *http.Request) {
		if request.Method != http.MethodPost {
			http.Error(writer, "method not allowed", http.StatusMethodNotAllowed)
			return
		}
		var payload quota.ReservationRequest
		if err := json.NewDecoder(request.Body).Decode(&payload); err != nil {
			http.Error(writer, "invalid json", http.StatusBadRequest)
			return
		}
		decision, err := quotaService.Reserve(request.Context(), payload)
		writeDecision(writer, decision, err)
	})
	mux.HandleFunc("/v1/quota/release", func(writer http.ResponseWriter, request *http.Request) {
		if request.Method != http.MethodPost {
			http.Error(writer, "method not allowed", http.StatusMethodNotAllowed)
			return
		}
		var payload quota.ReservationUpdate
		if err := json.NewDecoder(request.Body).Decode(&payload); err != nil {
			http.Error(writer, "invalid json", http.StatusBadRequest)
			return
		}
		decision, err := quotaService.Release(request.Context(), payload.ReservationID)
		writeDecision(writer, decision, err)
	})
	mux.HandleFunc("/v1/quota/commit", func(writer http.ResponseWriter, request *http.Request) {
		if request.Method != http.MethodPost {
			http.Error(writer, "method not allowed", http.StatusMethodNotAllowed)
			return
		}
		var payload quota.ReservationUpdate
		if err := json.NewDecoder(request.Body).Decode(&payload); err != nil {
			http.Error(writer, "invalid json", http.StatusBadRequest)
			return
		}
		decision, err := quotaService.Commit(request.Context(), payload)
		writeDecision(writer, decision, err)
	})
	return &http.Server{
		Addr:              addr,
		Handler:           mux,
		ReadHeaderTimeout: 5 * time.Second,
	}
}

func writeDecision(writer http.ResponseWriter, decision quota.ReservationDecision, err error) {
	writer.Header().Set("Content-Type", "application/json")
	if err != nil {
		writer.WriteHeader(http.StatusInternalServerError)
		_ = json.NewEncoder(writer).Encode(map[string]interface{}{"allowed": false, "reason": "internal_error"})
		return
	}
	if !decision.Allowed {
		if isBadRequestReason(decision.Reason) {
			writer.WriteHeader(http.StatusBadRequest)
		} else if decision.Reason == "balance_reserved_exceeded" {
			writer.WriteHeader(http.StatusPaymentRequired)
		} else {
			writer.WriteHeader(http.StatusTooManyRequests)
		}
	}
	_ = json.NewEncoder(writer).Encode(decision)
}

func isBadRequestReason(reason string) bool {
	return reason == "user_id_required" ||
		reason == "reservation_id_required" ||
		reason == "actual_cost_cents_negative" ||
		strings.Contains(reason, "_required") ||
		strings.Contains(reason, "_negative") ||
		strings.Contains(reason, "_must_be_positive")
}
