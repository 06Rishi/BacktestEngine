# Phase 4–5 progress

One line per mini-checkpoint. Spec: `docs/phase45_amendments.md`.

| Checkpoint | Tests | New | Commit | Notes |
|---|---|---|---|---|
| M0 spec | 79/79 | +0 | 9583f75 | plan.md rewritten; amendments in docs/; holdout seed (momentum, breakout viewed; legacy reports not in repo); 40 CA rows lack effective_date (spec said 9) |
| M1.1 account | 107/107 | +28 | 0a460ef | capital capped at ₹1,000,000,000; config.yaml edited in place, comments kept; no account saved yet, run `nsebt account` |
| M1.2 costs | 115/115 | +8 | ec24aff | ₹12k round trip = ₹42.0377; no ipft key; costs block added to config.yaml |
| M1.3 sim core | 130/130 | +15 | d7ec5ea | order value fixed at decision close (A6); fixture bars given a range so they aren't read as locked circuits |
| M1.4 execution rules | 142/142 | +12 | ae1207a | shared test helpers moved to tests/_simkit.py; unfilled sells always retry (unused max_unfilled_days_sell removed) |
| M1.5 run record | 147/147 | +5 | e51ae68 | Run schema v1 frozen; store version = hash of build_report.json |
