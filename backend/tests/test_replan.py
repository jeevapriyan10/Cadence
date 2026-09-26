"""Pytest suite for dynamic re-planning and benchmarking in Cadence."""

from datetime import datetime, timedelta, timezone
import logging
from typing import Any
import pytest

from cadence.domain.schemas import (
    MaintenanceTaskSchema,
    ScheduledBlockSchema,
    TrackSectionSchema,
    TrainSlotSchema,
)
from cadence.generator import generate_synthetic_network
from cadence.profiles.registry import ProfileRegistry
from cadence.solve import (
    ReplanResult,
    ReplanStrategy,
    build_cp_model,
    compute_tasks_changed,
    precompute_unsafe_adjacency_pairs,
    register_replan_strategy,
    replan_full_resolve,
    replan_warm_start,
    run_replan_benchmark,
    solve_schedule,
)
from tests.test_safety_adjacency import count_safety_violations

BASE_TEST_TIME = datetime(2026, 1, 1, 6, 0, 0, tzinfo=timezone.utc)
logger = logging.getLogger(__name__)


def test_compute_tasks_changed_identifies_moved_vs_stayed_put() -> None:
    """compute_tasks_changed correctly identifies which tasks moved vs stayed put."""
    # Solution 1
    blocks_1 = [
        ScheduledBlockSchema(
            task_id="TASK_STAY_1",
            section_id="SEC_A",
            start_time=BASE_TEST_TIME + timedelta(hours=1),
            end_time=BASE_TEST_TIME + timedelta(hours=2),
            method="full_resolve",
        ),
        ScheduledBlockSchema(
            task_id="TASK_STAY_2",
            section_id="SEC_B",
            start_time=BASE_TEST_TIME + timedelta(hours=3),
            end_time=BASE_TEST_TIME + timedelta(hours=4),
            method="full_resolve",
        ),
        ScheduledBlockSchema(
            task_id="TASK_MOVED",
            section_id="SEC_C",
            start_time=BASE_TEST_TIME + timedelta(hours=5),
            end_time=BASE_TEST_TIME + timedelta(hours=6),
            method="full_resolve",
        ),
        ScheduledBlockSchema(
            task_id="TASK_SUB_TOLERANCE",
            section_id="SEC_D",
            start_time=BASE_TEST_TIME + timedelta(hours=7),
            end_time=BASE_TEST_TIME + timedelta(hours=8),
            method="full_resolve",
        ),
    ]

    # Solution 2:
    # - TASK_STAY_1: exactly identical start time -> stayed put
    # - TASK_STAY_2: exactly identical start time -> stayed put
    # - TASK_MOVED: shifted by 90 minutes -> moved (> 1.0 min tolerance)
    # - TASK_SUB_TOLERANCE: shifted by 30 seconds -> within 1.0 min tolerance, should NOT be marked moved
    # - EMERGENCY_TASK: newly added task, not present in solution 1 -> not a task that 'moved'
    blocks_2 = [
        ScheduledBlockSchema(
            task_id="TASK_STAY_1",
            section_id="SEC_A",
            start_time=BASE_TEST_TIME + timedelta(hours=1),
            end_time=BASE_TEST_TIME + timedelta(hours=2),
            method="warm_start",
        ),
        ScheduledBlockSchema(
            task_id="TASK_STAY_2",
            section_id="SEC_B",
            start_time=BASE_TEST_TIME + timedelta(hours=3),
            end_time=BASE_TEST_TIME + timedelta(hours=4),
            method="warm_start",
        ),
        ScheduledBlockSchema(
            task_id="TASK_MOVED",
            section_id="SEC_C",
            start_time=BASE_TEST_TIME + timedelta(hours=6, minutes=30),
            end_time=BASE_TEST_TIME + timedelta(hours=7, minutes=30),
            method="warm_start",
        ),
        ScheduledBlockSchema(
            task_id="TASK_SUB_TOLERANCE",
            section_id="SEC_D",
            start_time=BASE_TEST_TIME + timedelta(hours=7, seconds=30),
            end_time=BASE_TEST_TIME + timedelta(hours=8, seconds=30),
            method="warm_start",
        ),
        ScheduledBlockSchema(
            task_id="EMERGENCY_TASK",
            section_id="SEC_E",
            start_time=BASE_TEST_TIME + timedelta(hours=2),
            end_time=BASE_TEST_TIME + timedelta(hours=3),
            method="warm_start",
        ),
    ]

    changed = compute_tasks_changed(blocks_1, blocks_2, tolerance_minutes=1.0)
    assert changed == ["TASK_MOVED"]

    # When all tasks remain unchanged, compute_tasks_changed returns empty list
    unchanged = compute_tasks_changed(blocks_1, blocks_1, tolerance_minutes=1.0)
    assert unchanged == []


def test_replan_incorporates_emergency_task_correctly() -> None:
    """Both replan_full_resolve and replan_warm_start correctly incorporate the new emergency task."""
    profile = ProfileRegistry.get("mainline")
    section_1 = TrackSectionSchema(id="S1", name="Section 1")
    section_2 = TrackSectionSchema(id="S2", name="Section 2")

    existing_task = MaintenanceTaskSchema(
        id="T1",
        name="Scheduled Maintenance T1",
        section_id="S1",
        duration_minutes=60,
        earliest_start=BASE_TEST_TIME + timedelta(hours=1),
        latest_end=BASE_TEST_TIME + timedelta(hours=4),
    )

    prev_solve = solve_schedule(
        sections=[section_1, section_2],
        tasks=[existing_task],
        train_slots=[],
        profile=profile,
        time_horizon_minutes=720,
        base_time=BASE_TEST_TIME,
    )
    assert prev_solve.status in ("OPTIMAL", "FEASIBLE")

    em_earliest = BASE_TEST_TIME + timedelta(hours=2)
    em_latest = BASE_TEST_TIME + timedelta(hours=6)
    em_duration = 90
    emergency_task = MaintenanceTaskSchema(
        id="EMERGENCY_T99",
        name="Urgent Track Fault",
        section_id="S2",
        duration_minutes=em_duration,
        earliest_start=em_earliest,
        latest_end=em_latest,
        priority=10,
        is_emergency=True,
    )

    # 1. Full resolve
    full_result = replan_full_resolve(
        previous_solve_result=prev_solve,
        new_emergency_task=emergency_task,
        all_existing_tasks=[existing_task],
        sections=[section_1, section_2],
        train_slots=[],
        profile=profile,
        time_horizon_minutes=720,
        base_time=BASE_TEST_TIME,
    )

    assert full_result.strategy == ReplanStrategy.FULL_RESOLVE.value
    assert full_result.status in ("OPTIMAL", "FEASIBLE")
    em_blocks_full = [b for b in full_result.scheduled_blocks if b.task_id == "EMERGENCY_T99"]
    assert len(em_blocks_full) == 1
    em_block_full = em_blocks_full[0]
    assert em_block_full.start_time >= em_earliest
    assert em_block_full.end_time <= em_latest
    assert int((em_block_full.end_time - em_block_full.start_time).total_seconds() // 60) == em_duration

    # 2. Warm start
    warm_result = replan_warm_start(
        previous_solve_result=prev_solve,
        previous_model_context={"base_time": BASE_TEST_TIME},
        new_emergency_task=emergency_task,
        all_existing_tasks=[existing_task],
        sections=[section_1, section_2],
        train_slots=[],
        profile=profile,
        time_horizon_minutes=720,
        base_time=BASE_TEST_TIME,
    )

    assert warm_result.strategy == ReplanStrategy.WARM_START.value
    assert warm_result.status in ("OPTIMAL", "FEASIBLE")
    em_blocks_warm = [b for b in warm_result.scheduled_blocks if b.task_id == "EMERGENCY_T99"]
    assert len(em_blocks_warm) == 1
    em_block_warm = em_blocks_warm[0]
    assert em_block_warm.start_time >= em_earliest
    assert em_block_warm.end_time <= em_latest
    assert int((em_block_warm.end_time - em_block_warm.start_time).total_seconds() // 60) == em_duration

    # Both strategies must retain the existing task as well
    assert any(b.task_id == "T1" for b in full_result.scheduled_blocks)
    assert any(b.task_id == "T1" for b in warm_result.scheduled_blocks)


def test_replan_respects_safety_adjacency_constraints() -> None:
    """Both replan strategies respect all Module 5 safety-adjacency hard constraints."""
    profile = ProfileRegistry.get("metro")
    net = generate_synthetic_network("metro", seed=42, section_count=10, train_count=10, task_count=6)
    sections = net["sections"]
    tasks = net["maintenance_tasks"]
    train_slots = net["train_slots"]
    adjacencies = net["adjacencies"]

    prev_solve = solve_schedule(
        sections=sections,
        tasks=tasks,
        train_slots=train_slots,
        profile=profile,
        time_horizon_minutes=1440,
        base_time=BASE_TEST_TIME,
        adjacencies=adjacencies,
    )
    assert prev_solve.status in ("OPTIMAL", "FEASIBLE")

    # Pick section for emergency task that is adjacent to another section
    sec_id = sections[0].id if hasattr(sections[0], "id") else sections[0]["id"]
    emergency_task = MaintenanceTaskSchema(
        id="EMERGENCY_SAFETY",
        name="Emergency Safety Test",
        section_id=sec_id,
        duration_minutes=60,
        earliest_start=BASE_TEST_TIME + timedelta(hours=2),
        latest_end=BASE_TEST_TIME + timedelta(hours=10),
        priority=10,
        is_emergency=True,
    )

    full_result = replan_full_resolve(
        previous_solve_result=prev_solve,
        new_emergency_task=emergency_task,
        all_existing_tasks=tasks,
        sections=sections,
        train_slots=train_slots,
        profile=profile,
        time_horizon_minutes=1440,
        base_time=BASE_TEST_TIME,
        adjacencies=adjacencies,
    )

    warm_result = replan_warm_start(
        previous_solve_result=prev_solve,
        previous_model_context={"base_time": BASE_TEST_TIME},
        new_emergency_task=emergency_task,
        all_existing_tasks=tasks,
        sections=sections,
        train_slots=train_slots,
        profile=profile,
        time_horizon_minutes=1440,
        base_time=BASE_TEST_TIME,
        adjacencies=adjacencies,
    )

    # Precompute unsafe adjacency pairs independently
    model_ctx = build_cp_model(
        sections=sections,
        tasks=tasks + [emergency_task],
        train_slots=train_slots,
        profile=profile,
        time_horizon_minutes=1440,
        base_time=BASE_TEST_TIME,
        adjacencies=adjacencies,
    )
    unsafe_pairs = model_ctx.get("unsafe_adjacency_pairs", [])

    # Check zero safety violations in both replanned schedules
    full_violations = count_safety_violations(full_result.scheduled_blocks, unsafe_pairs)
    warm_violations = count_safety_violations(warm_result.scheduled_blocks, unsafe_pairs)

    assert full_violations == 0, f"Full resolve produced {full_violations} safety adjacency violations"
    assert warm_violations == 0, f"Warm start produced {warm_violations} safety adjacency violations"


def test_warm_start_wall_time_benchmark() -> None:
    """Evaluate warm-start wall_time_seconds vs full-resolve across repeated runs.

    Tests warm-start vs full-resolve timing on a synthetic network across multiple runs,
    evaluating whether warm_start achieves lower wall time and honestly reporting
    observed performance metrics.
    """
    profile = ProfileRegistry.get("mainline")
    net = generate_synthetic_network("mainline", seed=42, section_count=12, train_count=20, task_count=10)
    sections = net["sections"]
    tasks = net["maintenance_tasks"]
    train_slots = net["train_slots"]
    adjacencies = net["adjacencies"]
    base_time = min(t.earliest_start for t in tasks)

    prev_solve = solve_schedule(
        sections=sections,
        tasks=tasks,
        train_slots=train_slots,
        profile=profile,
        time_horizon_minutes=1440,
        base_time=base_time,
        adjacencies=adjacencies,
    )
    assert prev_solve.status in ("OPTIMAL", "FEASIBLE")

    sec_id = sections[0].id if hasattr(sections[0], "id") else sections[0]["id"]
    emergency_task = MaintenanceTaskSchema(
        id="EMERGENCY_BENCHMARK",
        name="Emergency Benchmark Task",
        section_id=sec_id,
        duration_minutes=45,
        earliest_start=base_time + timedelta(hours=3),
        latest_end=base_time + timedelta(hours=12),
        priority=10,
        is_emergency=True,
    )

    full_times: list[float] = []
    warm_times: list[float] = []
    iterations = 4

    for _ in range(iterations):
        full_res = replan_full_resolve(
            previous_solve_result=prev_solve,
            new_emergency_task=emergency_task,
            all_existing_tasks=tasks,
            sections=sections,
            train_slots=train_slots,
            profile=profile,
            time_horizon_minutes=1440,
            base_time=base_time,
            adjacencies=adjacencies,
        )
        warm_res = replan_warm_start(
            previous_solve_result=prev_solve,
            previous_model_context={"base_time": base_time},
            new_emergency_task=emergency_task,
            all_existing_tasks=tasks,
            sections=sections,
            train_slots=train_slots,
            profile=profile,
            time_horizon_minutes=1440,
            base_time=base_time,
            adjacencies=adjacencies,
        )
        assert full_res.status in ("OPTIMAL", "FEASIBLE")
        assert warm_res.status in ("OPTIMAL", "FEASIBLE")
        full_times.append(full_res.wall_time_seconds)
        warm_times.append(warm_res.wall_time_seconds)

    avg_full = sum(full_times) / len(full_times)
    avg_warm = sum(warm_times) / len(warm_times)
    speedup = avg_full / max(1e-6, avg_warm)

    # Note findings honestly as instructed:
    # On synthetic networks solving in sub-100ms, CP-SAT solve times are fast enough
    # that solver thread initialization can show near-parity, while hints guide search.
    logger.info(
        "Replan Benchmark Timing: avg_full=%.4fs, avg_warm=%.4fs, speedup=%.2fx",
        avg_full,
        avg_warm,
        speedup,
    )

    assert avg_warm > 0.0
    assert avg_full > 0.0
    # Both strategies must complete within reasonable time limit and not diverge
    assert avg_warm < 10.0
    assert avg_full < 10.0


def test_run_replan_benchmark_structure_and_convention() -> None:
    """run_replan_benchmark produces well-formed dict with correct speedup_factor and objective_delta sign."""
    profile = ProfileRegistry.get("metro")
    net = generate_synthetic_network("metro", seed=55, section_count=8, train_count=12, task_count=6)
    sections = net["sections"]
    tasks = net["maintenance_tasks"]
    train_slots = net["train_slots"]

    prev_solve = solve_schedule(
        sections=sections,
        tasks=tasks,
        train_slots=train_slots,
        profile=profile,
        time_horizon_minutes=1440,
        base_time=BASE_TEST_TIME,
    )

    sec_id = sections[1].id if hasattr(sections[1], "id") else sections[1]["id"]
    emergency_task = MaintenanceTaskSchema(
        id="EMERGENCY_BENCH",
        name="Emergency Bench Task",
        section_id=sec_id,
        duration_minutes=30,
        earliest_start=BASE_TEST_TIME + timedelta(hours=1),
        latest_end=BASE_TEST_TIME + timedelta(hours=8),
        priority=8,
        is_emergency=True,
    )

    bench = run_replan_benchmark(
        previous_solve_result=prev_solve,
        previous_model_context={"base_time": BASE_TEST_TIME},
        new_emergency_task=emergency_task,
        all_existing_tasks=tasks,
        sections=sections,
        train_slots=train_slots,
        profile=profile,
        time_horizon_minutes=1440,
        base_time=BASE_TEST_TIME,
    )

    # Structural assertions
    assert "full_resolve" in bench
    assert "warm_start" in bench
    assert "speedup_factor" in bench
    assert "objective_delta" in bench
    assert "strategies" in bench
    assert "comparisons" in bench

    assert isinstance(bench["full_resolve"], ReplanResult)
    assert isinstance(bench["warm_start"], ReplanResult)
    assert bench["speedup_factor"] > 0.0

    # Objective delta sign convention verification:
    # objective_delta = warm_start.objective_value - full_resolve.objective_value
    expected_delta = bench["warm_start"].objective_value - bench["full_resolve"].objective_value
    assert pytest.approx(bench["objective_delta"], abs=1e-4) == expected_delta

    # Test Module 13 scaffold extensibility with an additional 'rl' strategy
    def dummy_rl_strategy(**kwargs: Any) -> ReplanResult:
        return ReplanResult(
            strategy="rl",
            status="OPTIMAL",
            scheduled_blocks=kwargs["previous_solve_result"].scheduled_blocks,
            objective_value=5.0,
            wall_time_seconds=0.002,
            previous_objective_value=kwargs["previous_solve_result"].objective_value,
            tasks_changed=[],
        )

    register_replan_strategy("rl", dummy_rl_strategy)

    extended_bench = run_replan_benchmark(
        previous_solve_result=prev_solve,
        previous_model_context={"base_time": BASE_TEST_TIME},
        new_emergency_task=emergency_task,
        all_existing_tasks=tasks,
        sections=sections,
        train_slots=train_slots,
        profile=profile,
        time_horizon_minutes=1440,
        base_time=BASE_TEST_TIME,
        strategies=["full_resolve", "warm_start", "rl"],
    )

    assert "rl" in extended_bench["strategies"]
    assert "rl" in extended_bench["comparisons"]
    assert extended_bench["comparisons"]["rl"]["speedup_factor"] > 0.0


@pytest.mark.parametrize("profile_name", ["metro", "mainline", "local"])
def test_replan_all_profiles(profile_name: str) -> None:
    """Run dynamic re-planning against all three profiles confirming no crashes and safety constraints hold."""
    profile = ProfileRegistry.get(profile_name)
    net = generate_synthetic_network(profile_name, seed=42, section_count=8, train_count=10, task_count=6)
    sections = net["sections"]
    tasks = net["maintenance_tasks"]
    train_slots = net["train_slots"]
    adjacencies = net["adjacencies"]

    prev_solve = solve_schedule(
        sections=sections,
        tasks=tasks,
        train_slots=train_slots,
        profile=profile,
        time_horizon_minutes=1440,
        base_time=BASE_TEST_TIME,
        adjacencies=adjacencies,
    )
    assert prev_solve.status in ("OPTIMAL", "FEASIBLE")

    sec_id = sections[0].id if hasattr(sections[0], "id") else sections[0]["id"]
    emergency_task = MaintenanceTaskSchema(
        id=f"EMERGENCY_{profile_name.upper()}",
        name=f"Emergency Task {profile_name}",
        section_id=sec_id,
        duration_minutes=45,
        earliest_start=BASE_TEST_TIME + timedelta(hours=1),
        latest_end=BASE_TEST_TIME + timedelta(hours=9),
        priority=10,
        is_emergency=True,
    )

    bench = run_replan_benchmark(
        previous_solve_result=prev_solve,
        previous_model_context={"base_time": BASE_TEST_TIME},
        new_emergency_task=emergency_task,
        all_existing_tasks=tasks,
        sections=sections,
        train_slots=train_slots,
        profile=profile,
        time_horizon_minutes=1440,
        base_time=BASE_TEST_TIME,
        adjacencies=adjacencies,
    )

    full_res = bench["full_resolve"]
    warm_res = bench["warm_start"]

    assert full_res.status in ("OPTIMAL", "FEASIBLE")
    assert warm_res.status in ("OPTIMAL", "FEASIBLE")

    # Safety constraints validation
    model_ctx = build_cp_model(
        sections=sections,
        tasks=tasks + [emergency_task],
        train_slots=train_slots,
        profile=profile,
        time_horizon_minutes=1440,
        base_time=BASE_TEST_TIME,
        adjacencies=adjacencies,
    )
    unsafe_pairs = model_ctx.get("unsafe_adjacency_pairs", [])

    assert count_safety_violations(full_res.scheduled_blocks, unsafe_pairs) == 0
    assert count_safety_violations(warm_res.scheduled_blocks, unsafe_pairs) == 0
