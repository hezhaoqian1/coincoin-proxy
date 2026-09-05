package config

import (
	"strings"
	"testing"
)

func TestLoadDefaultsToDryRun(t *testing.T) {
	t.Setenv("COINCOIN_USAGE_QUOTA_DRY_RUN", "")

	cfg, err := Load()
	if err != nil {
		t.Fatalf("Load returned error: %v", err)
	}
	if !cfg.DryRun {
		t.Fatal("expected dry-run to default to true")
	}
	if cfg.Stream != "coincoin:usage:events" {
		t.Fatalf("unexpected stream default: %q", cfg.Stream)
	}
	if cfg.DeadLetterStream != "coincoin:usage:events:dlq" {
		t.Fatalf("unexpected DLQ default: %q", cfg.DeadLetterStream)
	}
}

func TestLoadRejectsNonDryRunUntilLedgerWriterShips(t *testing.T) {
	t.Setenv("COINCOIN_USAGE_QUOTA_DRY_RUN", "false")

	_, err := Load()
	if err == nil {
		t.Fatal("expected non-dry-run config to fail")
	}
	if !strings.Contains(err.Error(), "not supported") {
		t.Fatalf("unexpected error: %v", err)
	}
}
