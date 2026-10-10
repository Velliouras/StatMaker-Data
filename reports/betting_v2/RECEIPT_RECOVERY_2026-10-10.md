# Betting V2 — Verified receipt recovery (2026-10-10)

Official **no-provider-call** GitHub Actions run: [38084468346](https://github.com/Velliouras/StatMaker-Data/actions/runs/38084468346). Input SHA: `9685f9b061647d9ec05100b5fdf0d69f2e6ee366`.

**Result:** 136/136 tests passed. The pilot completed. A genuine offline replay of the **13 existing** Brazil research receipts found:

| Measurement | Prior strict all-or-nothing validator | New hash-checked research reindex |
|---|---:|---:|
| Accepted snapshot-integrity receipts | 2 | 12 |
| Full parser coverage | 2 | 2 |
| Explicitly partial but hash-integrity checked | 0 | 10 |
| Invalid / empty | 11 | 1 |
| Pre-kickoff bookmaker market price entries | 542 | 22,876 |
| Distinct provider event IDs | 1 | 11 |

New quote rows by bookmaker: Bet365 18,267; Bet365 (no latency) 70; Unibet 4,539. Some rows are alternate markets, player props, handicaps and repeated price captures. **These counts are not unique qualified bets, are not historical net profits and must not be considered STRONG.**

Root cause confirmed by independent offline diagnosis: all 11 previous rejections were due to `marketSnapshotComplete=false`, not checksum corruption. Among these 11, ten contained stored market snapshots and one had no stored snapshot bytes. The fix preserves the strict full-market-completeness flag, requires a matching SHA-256 and valid client timestamp for **partial research** evidence, and rejects tampering.

Read-only QA reports are archived as a downloadable artifact in the linked run. The canonical `quote_receipt_index.json` on Data/main was last produced by the preceding live refresh and **will be regenerated on the next normal odds cycle**; don't mistake its older values for a failed offline fix.

**Safety:** No provider API calls were used to recover these existing price entries; no Android PROD change; `certifiedStrong=0`. Client receipt time does not prove bookmaker price-update time. No verified full-universe historical EV or ROI, individual quote freshness, or exact API-Football fixture crosswalk yet. Do not authorize Betting V2 production from these receipt counts.
