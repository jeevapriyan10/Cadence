# Changelog

All notable changes to Cadence are documented in this file.

The project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [v1.0.0] - 2026-10-08

### Added
- **Echo Persisted Decision History**:
  - Replaced transient in-memory run caching with database-backed SQLAlchemy 2.0 persistence and Alembic schema migrations.
  - Added `network_runs` and `decision_records` relational schema tracking complete chronological run histories and scheduled block snapshots.
  - Implemented warm-start assignment hint generation from persisted historical runs.
  - Added `GET /history/{run_id}` API endpoint.
- **Docker Compose Orchestration**:
  - Multi-container architecture orchestrating PostgreSQL 16, FastAPI backend, and Nginx reverse proxy serving the React SPA.
  - Automatic database migrations on backend boot via `docker-entrypoint.sh`.
  - Production Nginx reverse proxy routing `/api/*` to the backend service.
- **CI & Quality Gates**:
  - GitHub Actions CI pipeline running linting (`ruff`), type checks (`tsc`), and test coverage enforcement (`pytest --cov-fail-under=90`).
  - Added Docker build smoke test job verifying service health on clean spin-up.
- **Test Suite Hardening**:
  - Expanded test suite to 229 tests, elevating backend coverage from 92% to 98% (97.80%).
  - Added comprehensive edge case suites and multi-step "day-in-the-life" chained warm-start integration tests.
- **Portfolio & System Documentation**:
  - Complete `README.md` revamp with architecture diagrams, profile comparisons, benchmark results, and honest limitations.
  - Created `docs/PROFILES.md` and `docs/ARCHITECTURE.md` reference manuals.

---

## [v0.8.0] - 2026-09-26

### Added
- **Board Web Dashboard**:
  - React 18 + TypeScript single-page application built with Vite.
  - Interactive Gantt timeline visualization (`vis-timeline`) displaying sections, train slots, and maintenance possession blocks.
  - Integrated Explanation Panel displaying post-hoc counterfactuals from Reason.
  - Integrated Ripple Panel displaying downstream cascading train delays.
- **Dynamic Re-planning Engine**:
  - Implemented `replan_full_resolve` for complete schedule re-optimization upon emergency task insertion.
  - Implemented `replan_warm_start` utilizing CP-SAT variable assignment hints from previous solves for faster convergence.
  - Added `POST /replan` API endpoint.
- **Flux Reinforcement Learning Engine**:
  - Created `CadenceReplanEnv` Gymnasium environment modeling single-decision emergency possessions.
  - Integrated Stable-Baselines3 PPO policy training pipeline with observation normalization and candidate window discretization.
  - Implemented strict Hard-Constraint Re-Validation Gate verifying same-section overlaps and ground-truth safety invariants, with automatic fallback to warm-start.
- **Three-Way Benchmark Harness**:
  - Benchmarking utility (`run_replan_benchmark`) comparing full-resolve, warm-start, and RL inference across wall time, objective value, safety violation rates, and fallback frequency.

---

## [v0.4.0] - 2026-09-17

### Added
- **Safety-Hardened CP-SAT Solve Core**:
  - Implemented structural safety-adjacency hard constraints in CP-SAT solver, forbidding simultaneous possession of vulnerable adjacent track sections.
  - Randomized-seed test suite validating safety invariant satisfaction across random topologies.
  - Ground-truth independent safety violation counter (`count_safety_violations`).
- **Ripple Cascade Simulator**:
  - Deterministic downstream delay propagation simulator modeling train schedule impacts caused by active maintenance blocks.
  - Formatted cascade reporting calculating total affected trains and aggregate delay minutes.
  - Added `GET /ripple/{run_id}` API endpoint.
- **Reason Explainability Layer**:
  - Counterfactual explanation generator analyzing why maintenance tasks were scheduled in their assigned window and why alternative slots were rejected.
  - Formatted human-readable summaries detailing train conflicts, track occupancy, and safety-adjacency restrictions.
  - Added `GET /reason/{run_id}/{task_id}` and `GET /reason/{run_id}` API endpoints.
- **FastAPI REST Service**:
  - Production REST API with structured Pydantic v2 schemas, automated OpenAPI documentation (`/docs`), and CORS middleware.
  - Added `POST /solve` and `POST /networks/generate` routes.

---

## [v0.1.0] - 2026-09-08

### Added
- **Core Domain & Graph Modeling**:
  - Strongly typed domain schemas: `TrackSection`, `SectionAdjacency`, `TrainSlot`, `MaintenanceTask`.
  - Network topology graph representation built on NetworkX (`NetworkGraph`).
- **Typed Network Profiles**:
  - Abstract base profile `NetworkProfile` defining interface for safety adjacency, headways, priority weights, and disruption penalties.
  - Initial implementations of `MetroProfile`, `LocalProfile`, and `MainlineProfile`.
  - Dynamic `ProfileRegistry` for discovering and retrieving profiles.
- **Seedable Synthetic Network Generator**:
  - Deterministic procedural generator creating reproducible rail topologies, timetable train slots, and maintenance tasks based on random seed and profile.
- **Initial CP-SAT Scheduling Core**:
  - Integer programming scheduling formulation using Google OR-Tools CP-SAT.
  - Enforced no-overlap interval constraints on shared track sections and bounded earliest-start/latest-end task windows.
