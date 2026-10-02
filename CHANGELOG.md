# Changelog

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
