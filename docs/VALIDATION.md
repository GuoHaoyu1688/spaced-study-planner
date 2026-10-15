# Validation record

Measured on 2026-10-15 with Python 3.12.13 on macOS. Examples and screenshots contain original fictional material only. Remote CI is a separate result.

| Check | Observed result |
| --- | --- |
| Ruff lint and formatting | Passed |
| Pytest | 67 passed; 93% measured overall coverage |
| FSRS reference sequences | Two five-rating sequences at each of 0.8, 0.9, 0.95 retention; exact card-state and due-date agreement with official fsrs 6.3.2 configured identically |
| SM-2 hand arithmetic | Good intervals 1, 6, 15 days at ease 2.5; Again resets interval to 1 and ease to 1.96; Easy adjusts ease to 2.06 |
| Full 50-question mock | 40 correct and 10 incorrect = 80%; subject times sum to 1,500 seconds |
| Time-limited partial mock | Unanswered selections remain in denominator; late answers are rejected |
| Capacity | No day exceeds its available seconds; dated travel overrides, already-spent time, stable phases, and explicit shortfall verified |
| Answer integrity | Identical retry returns once; changed retry, stale revision, duplicate session answer, and reversed time are rejected; concurrent duplicate retry exercised |
| Import and restore | JSON/CSV library state and local image bytes round-trip; conflicting batches roll back; legacy APKG, Markdown, MathText, and local images exercised |
| Safety boundaries | Unsupported modern APKG rejected; image escape and corrupted asset archives rejected; raw HTML escaped; loopback host/origin checks exercised |
| Independent audit regressions | 502-item selection and exact offline plan caching; 31 malformed snapshot variants rejected before commit; full FSRS/SM-2 history roundtrips; reference-style remote images rejected |
| MCP | All five tools via official SDK plus actual stdio client initialization and calls |
| CLI quick start | Fresh seed gives 50 questions and 3 cards; today/review/answer/stats/forecast/availability/plan/mock/export/import/backup exercised |
| Browser functional check | Today's 29 selected items cached; offline reload succeeded; one offline answer synced exactly once; repeating sync did not add a review |
| Browser scoring | Two-question mock with one correct and one incorrect produced 50% and subject breakdown |
| Browser layout | Fresh desktop 1440 px and mobile 390 px contexts, no horizontal overflow; mock curve and daily-time rows rendered |
| Browser network and console | No external requests or JavaScript errors in the functional acceptance flow |
| Package | Wheel built; static HTML/CSS/JS/service-worker resources inspected |

The upstream FastAPI TestClient emitted one Starlette deprecation warning about its HTTP client dependency; tests completed successfully. This is recorded, not silenced.

Screenshots: [desktop](desktop.png), [mobile](mobile.png). The captured study time rounds to zero because browser test answers were submitted quickly; those screenshots are not evidence of actual studying. Manual elapsed-time assertions use independent 30-second answers.

The forecast assumes Good ratings and estimated durations. Observed practice accuracy and the score chart do not establish exam readiness or guaranteed retention. Offline sync is supported for a single active review tab on one browser origin; simultaneous offline tabs are not supported.
