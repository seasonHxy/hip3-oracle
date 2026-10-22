# Changelog

## 0.3.0 — 2026-10-22

- Add durable local `pause`/`resume` controls with incident IDs, recorded reasons and protection against stale resume commands.
- Add offline `status` snapshots and key-free `preflight` checks using the same collection, aggregation and risk pipeline as publishing.
- Verify source identity, independence group and weight against configuration before a quote can participate.
- Handle SIGTERM/SIGINT by finishing the current cycle, closing resources and exiting without starting another cycle.
- Add six clearly marked synthetic replay records and CLI, state-migration, control, diagnostic and real-process signal tests.
- Reject empty evaluated events and non-object journal records rather than accepting vacuous replay success.
- Exclude four confirmed credential-theft assets discovered in upstream history; disable their workflows and retain an incident record.
- Write state schema v3 so older v2-only writers reject the new pause state. Scoped v2 references migrate on the next write;
  pending publications and reference prices are preserved. Unscoped v1 still needs explicit migration.

Local pause only controls this publisher's state scope. It does not cancel an already in-flight transaction or halt DEX trading.
All writers must run the upgraded code; no distributed fencing or signing-key permission check is implied by `preflight`.

## 0.2.0 — 2026-10-02

- Add a default 40% independent-group weight-share cap, checked before and after MAD filtering.
- Scope state paths and contents by network, DEX, endpoint and Dry-run/Live. Persist the formatted wire price.
- Require upstream timestamps and prohibit Static sources in Live configurations.
- Normalize HIP-3 names to `dex:coin`, check the complete DEX universe and size decimals before submission.
- Confirm accepted updates by reading every oracle price; persist pending requests before sending and block ambiguous resends.
- Add explicit read-only `bootstrap` and `reconcile` operations with local audit reasons.
- Apply provider retry, deadline, concurrency and rate budgets; refuse redirects and redact transport errors.
- Check quote freshness again immediately before the SDK submits; retain update-spacing protection across restarts.
- Add local writer locking, JSON logs, health/readiness endpoints, Prometheus metrics and alert templates.
- Record normalized observations and decisions in a bounded rotating journal with deterministic offline replay.
- Pin Hyperliquid SDK 0.24.0 and add offline signing compatibility, fault, recovery and HTTP endpoint tests.
- Add Docker/Compose, Testnet templates, an operator runbook, package build checks and container CI.

Migration: v1 state is not silently imported into a live v2 scope. Establish the first live reference using `bootstrap`.
Short config names such as `AAPL` now produce the fully qualified wire name `demo:AAPL`. Core operation requires Linux
or macOS for `fcntl` locking. The templates still need actual licensed provider URLs and a deployed Testnet DEX.

## 0.1.0

- Initial fail-closed multi-source aggregation, circuit breaker, HIP-3 payload and SDK publishing library.
