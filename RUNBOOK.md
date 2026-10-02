# Oracle operator runbook

## Local verification

```bash
python -m pip install -e '.[dev,live]'
ruff check src tests
ruff format --check src tests
python -m unittest discover -s tests -v
hip3-oracle --config config/example.json once
hip3-oracle replay --journal state/oracle-state.testnet.dry-run.demo.audit.jsonl
```

Tests never submit a real transaction. SDK compatibility tests generate ephemeral unfunded wallets and mock transport.
The monitoring HTTP test binds an ephemeral loopback port. Tests without the optional live dependency skip SDK tests.

## First Testnet start

1. Prepare `config/testnet.local.json` with the actual DEX, complete universe, decimals and independently sourced quotes.
2. Confirm provider symbol, price denomination, timestamp semantics, session status and data permission against the contracts.
3. Run `validate`; it constructs adapters but makes no network calls and does not validate provider credentials.
4. Use `bootstrap --reason '...'` to read the current DEX oracle into an empty live state. No key required.
5. Inject updater key and provider tokens through the operational secret manager, then run `once --live`.
6. Inspect `status=published`, `confirmation.verified=true`, pending cleared, readiness and source metrics.
7. Run continuously under supervision; monitor `/readyz`, API connectivity and the independent DEX oracle.

The `.example` provider URLs are templates, not integrations. Registering a DEX, configuring permissions, funding a wallet
or calling `haltTrading` is outside these commands. Testnet and Mainnet use separate state paths and scopes.

## Pending or uncertain publication

A timeout may occur after HyperCore applied the update. Never resend the same journaled request automatically.
The process returns `status=uncertain`, readiness is false, and the persisted pending record blocks restarts too.

1. Ensure only one updater is authorized to write. Stop the local run process before maintenance; the file lock prevents
   overlapping cycles, but it is not distributed fencing and cannot prevent another host or another state directory.
2. Inspect the JSON state and audit journal on the trusted host. Do not paste credentials or licensed quote data in issues.
3. Query the intended DEX and compare every pending formatted oracle price, the universe and decimals.
4. If all values match, run `reconcile --reason 'investigation details...'`. This only reads info and commits local state.
5. If prices differ, reconciliation refuses to clear pending. Investigate rejection, another writer, price clamp or schema
   changes. Keep pending and use the separately approved incident procedure; this release has no force-clear command.
6. Resume and observe. Successful reconciliation refreshes the local restart rate-limit reference.

Readback confirms observable values, not a transaction hash or nonce. With unchanged prices, an old value is indistinguishable
from a newly accepted same-price request. This is why the operator must fence writers and investigate ambiguous outcomes.

## Quorum, weight or jump circuit breaker

Read the failed Feed reason and source errors in structured JSON output. Compare the journaled source timestamps,
independence groups and confidence. Quorum failure after MAD and a weight share above 40% both block the entire batch.
Changing weights to compensate for an outage changes trust assumptions; treat it as a reviewed configuration change.

For legitimate gaps, splits, dividends or contract scaling changes, preserve the state and journal, confirm the contract
specification and establish a new reference through a recorded maintenance procedure. `bootstrap` refuses an existing state.
Do not delete state or repeatedly restart to bypass the jump check. This release does not automate corporate actions or
calendar-based session transitions.

Market-closed blocking only refuses Oracle updates. It does not halt the market. A separate deployer procedure must decide
whether and how to call `haltTrading`, accounting for protocol fallback behavior.

## Disk failure or state corruption

The process blocks writes when audit or state persistence fails. Restore disk capacity, preserve the pending record and
restart after investigation. If a request might have been applied, reconcile it first. Scope mismatches and malformed files
stop startup; the application does not silently reset the reference price.

State v2 stores `scope`, `feeds`, `lastAttemptAtMs` and `pending`. The scoped filename and embedded scope must match the
network, mode, DEX and endpoint. Legacy v1 files remain untouched; initial Live references must come from the current DEX.
Do not copy a Dry-run state into Live or Mainnet.

## Deployment

`docker compose up --build` starts the default static Dry-run on a Linux host, with a non-root process and named volume.
Docker host networking is a Linux deployment assumption; local macOS development can run the CLI directly.
For Live, change the command to the actual `.local.json` config with `run --live` and inject secrets at runtime.
Keep the state volume owned by UID/GID `10001:10001`. Mount provider config read-only.

Healthchecks use `/livez`, so ordinary circuit breakers do not cause restart loops. Alerting uses readiness and metrics.
Prometheus can use `deploy/prometheus.yml` and `deploy/alerts.yml` when running on the same host. Wire Alertmanager receivers
separately. Defaults bind loopback; binding to `0.0.0.0` requires an appropriately restricted network.

File locking and timing gates protect only a single local state path. Multi-host failover needs an external lease, fencing
of the previous writer, shared recovery procedure and minimum update spacing. This release does not provide distributed HA.

## Audit retention

Journal contains normalized quotes, the exact per-feed thresholds, reference price, decision time, accepted/rejected result,
pending/confirmed stages and operational reasons. `replay` runs the same algorithm using the historical time, without fetching
or signing. Rotated files can be replayed individually. Broken/truncated JSONL files fail the replay command.

Local journal rotation bounds disk use; export logs to a governed durable store if long-term audit retention is needed.
It is not a tamper-evident log and does not retain raw licensed API response bodies. Provider authenticity and upstream
independence must be established outside the application.
