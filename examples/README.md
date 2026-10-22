# Synthetic replay data

`synthetic-replay.jsonl` contains six invented, deterministic observations for `demo:SYNTH`.
These records are test fixtures, not real market data, executed transactions or historical backtest results.
Each record includes `dataOrigin=synthetic`, its scenario, timestamps, feed thresholds, prior reference, quotes and expected decision.

| Scenario | Expected result |
| --- | --- |
| `normal` | Median `100.10`, accepted |
| `outlier` | Reject `bad=500.00`, retain median `100.10` |
| `stale_quorum` | Only 2 fresh sources remain; requires 3 |
| `weight_concentration` | One group exceeds the 40% share cap |
| `market_closed` | Aggregate exists, market-closed circuit breaker blocks |
| `price_jump` | `50.00` reference to `100.10` exceeds the configured jump limit |

Run without network, signing or state mutation:

```bash
hip3-oracle replay --journal examples/synthetic-replay.jsonl
```

Expected JSON: `{"matches": true, "cycles": 6, "mismatchedLines": []}` (key ordering may differ).
The fixture's observation time is deliberately fixed; replay uses that recorded time instead of the current clock.
`tests/test_replay_data.py` separately asserts the expected prices and rejection reasons.

The release validation record is in `docs/validation/0.3.0.md`. Production observations, runtime state,
credentials and licensed upstream response bodies are not part of these samples.
