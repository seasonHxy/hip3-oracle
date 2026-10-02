# Security model

## Fail-closed guarantees in this repository

- Invalid, stale, future, non-positive, wide-spread, duplicate and unconfigured quotes are rejected.
- Quorum is checked both before and after outlier filtering.
- Source count and independent upstream group count are separate checks.
- Each independent group's weight must remain below the configured share both before and after filtering.
- Excessive confidence or price jumps block the complete multi-asset update.
- Partial HIP-3 payloads are refused.
- Payload maps are sorted before the official SDK serializes and signs them.
- State corruption stops startup instead of silently discarding the circuit-breaker reference.
- State filenames and embedded scopes isolate network, DEX and Dry-run/Live; legacy state is not silently imported.
- Live sources require upstream timestamps and cannot be Static adapters.
- Live publication checks DEX universe and szDecimals, and requires readback of all formatted oracle prices.
- A durable pending record precedes submission. Unknown outcomes block further publishing, including after restart.
- Audit/state write errors fail closed. The local file lock prevents overlapping writers of one state file.
- Transport errors do not log provider URLs or headers. HTTP redirects are refused for JSON sources.
- Live testnet publishing requires `dryRun=false`, `--live` and an explicit environment acknowledgement; mainnet adds another acknowledgement.
- Private keys are never accepted in the JSON configuration.

## Threats not solved by software alone

- Several providers may license or relay the same underlying market data.
- A fund NAV, reserve attestation or corporate action can be false at its origin.
- The updater host, process environment or hot key can be compromised.
- A valid but economically wrong contract specification may still expose the HIP-3 deployer to losses or slashing.
- Network partitions can leave the publisher unable to update while the market continues trading.
- A legitimate price gap can trigger a persistent circuit breaker and require an authorized human decision.

## Production requirements

Before mainnet, add at minimum:

1. Contractually authorized, independently sourced market data with documented redistribution rights.
2. Two or more publishers in active/passive mode with fencing so they cannot race the 2.5-second limit.
3. HSM/KMS or isolated signing service for the updater key; keep the deployer key cold.
4. Deploy the provided monitoring endpoints and alert rules, connect alert receivers and add independent onchain monitoring.
5. Alerting and a separately authorized `haltTrading` runbook for market closure and oracle incidents.
6. Export normalized observation journals to a durable governed store; establish raw-response retention rights if needed.
7. Run actual Testnet chaos drills for manipulation, outage, clock skew, API rejection and restart recovery; local fault tests
   cover these code paths but do not validate the deployed DEX or providers.
8. Independent security review of configuration, source mappings and operating procedures.

Never include a private key, access token or paid market-data response in an issue or log bundle.

Price readback confirms the visible oracle values, not inclusion of a specific request/nonce. Unchanged prices are ambiguous;
reconciliation is an operator action performed after writer fencing. The SDK's standard HIP-3 action is supported;
HIP-3* star actions are not. KMS/HSM integration, distributed fencing, corporate-action handling and trading calendars
remain market/deployment-specific work.
