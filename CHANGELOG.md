# Changelog

## 1.3.0-rc.1 (2026-10-21)

Release candidate; real signed-in macOS/Ubuntu validation is required before release.

- On-demand adaptive collaboration framing and a 140-line core skill.
- Versioned task/context contracts and current-byte Git/non-Git capture.
- Independent per-role scratch copies, observed-stability checks and explicit freshness.
- Read-only defaults, authorized workspace experiments, fail-closed capability inspection.
- Model discovery in the actual execution cwd; per-role snapshots and explicit continuity.
- Raw per-attempt evidence, candidate artifact inventories and separated interpretations.
- Required-coverage semantics for prepared runs; no automatic application or merge.
- Replay safety for ordinary transient failures as well as stalls.
- Existing supervisor/liveness mechanisms retained; preparation publishes progress.
- Ubuntu/macOS CI, new integration and opt-in real-host tests, local validation guide.
- Mechanical documentation contracts replace obsolete exact-prose/PDF pins.
- Added MIT license text; generated PDFs no longer committed.

Migration: skill contract 5 requires prepare before discovery. Update/reload the skill
and runner together. Legacy direct CLI is now read-only and lacks captured-evidence
guarantees. Source agent config is not copied: inspect scratch-native model settings.
No live-model or real-sandbox claim is implied by fake-protocol CI success.
