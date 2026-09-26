"""Tests for Flux PPO reinforcement learning training, evaluation, and re-planning."""

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from cadence.domain.graph import NetworkGraph
from cadence.domain.schemas import MaintenanceTaskSchema, ScheduledBlockSchema
from cadence.flux.policy_replan import clear_model_cache, replan_rl
from cadence.flux.train import evaluate_policy_quick, train_policy
from cadence.generator import generate_synthetic_network
from cadence.profiles.registry import ProfileRegistry
from cadence.solve.benchmark import run_replan_benchmark
from cadence.solve.model import build_cp_model
from cadence.solve.replan import ReplanResult, replan_full_resolve
from cadence.solve.safety import count_safety_violations, precompute_unsafe_adjacency_pairs
from cadence.solve.solver import solve_schedule

BASE_TEST_TIME = datetime(2026, 1, 1, 6, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def quick_trained_model(tmp_path_factory: pytest.TempPathFactory) -> str:
    """Fixture producing a fast-trained PPO model file for test reuse."""
    tmp_dir = tmp_path_factory.mktemp("models")
    model_path = train_policy(
        profile_name="metro",
        total_timesteps=64,
        save_path=str(tmp_dir),
        seed=42,
    )
    return model_path


def test_train_policy_success(tmp_path: Path) -> None:
    """train_policy with small total_timesteps completes without error and saves model to disk."""
    save_dir = tmp_path / "models"
    model_path = train_policy(
        profile_name="metro",
        total_timesteps=64,
        save_path=str(save_dir),
        seed=101,
    )

    assert isinstance(model_path, str)
    assert os.path.exists(model_path), f"Saved model file does not exist at {model_path}"
    assert model_path.endswith(".zip")
    assert "metro" in model_path
    assert "64steps" in model_path


def test_evaluate_policy_quick(quick_trained_model: str) -> None:
    """evaluate_policy_quick runs against a trained model and returns well-formed stats in sane ranges."""
    stats = evaluate_policy_quick(
        model_path=quick_trained_model,
        profile_name="metro",
        n_eval_episodes=5,
    )

    assert isinstance(stats, dict)
    assert "mean_reward" in stats
    assert isinstance(stats["mean_reward"], float)

    assert "safety_violation_rate" in stats
    assert isinstance(stats["safety_violation_rate"], float)
    assert 0.0 <= stats["safety_violation_rate"] <= 1.0

    assert "mean_disruption_penalty" in stats
    assert isinstance(stats["mean_disruption_penalty"], float)
    assert stats["mean_disruption_penalty"] >= 0.0

    assert stats.get("n_eval_episodes") == 5


def test_replan_rl_fallback_on_safety_violation(quick_trained_model: str) -> None:
    """replan_rl falls back to warm_start with rl_fallback_triggered=True on unsafe proposals.

    Verifies the final returned schedule has zero safety violations using Module 5's
    count_safety_violations as ground truth.
    """
    profile = ProfileRegistry.get("metro")
    net = generate_synthetic_network("metro", seed=42, section_count=3, train_count=2, task_count=1)
    sections = net["sections"]
    tasks = net["maintenance_tasks"]
    train_slots = net["train_slots"]
    adjacencies = net["adjacencies"]

    graph = NetworkGraph.build_from_sections(sections, adjacencies)
    unsafe_pairs = precompute_unsafe_adjacency_pairs(
        graph=graph,
        profile=profile,
        sections=sections,
        train_slots=train_slots,
    )
    assert len(unsafe_pairs) > 0, "Network should have at least one unsafe adjacency pair"

    sec_a, sec_b, reason = unsafe_pairs[0]
    task1 = MaintenanceTaskSchema(
        id="TASK_ON_B",
        name="Task on B",
        section_id=sec_b,
        duration_minutes=60,
        earliest_start=BASE_TEST_TIME,
        latest_end=BASE_TEST_TIME + timedelta(hours=6),
        priority=5,
    )
    tasks = [task1]

    prev_solve = solve_schedule(
        sections=sections,
        tasks=tasks,
        train_slots=train_slots,
        profile=profile,
        time_horizon_minutes=720,
        base_time=BASE_TEST_TIME,
        graph=graph,
        adjacencies=adjacencies,
    )

    block_b = next(b for b in prev_solve.scheduled_blocks if b.section_id == sec_b)

    # Construct emergency task on sec_a whose window directly overlaps block_b on unsafe neighbor sec_b
    duration = max(30, int((block_b.end_time - block_b.start_time).total_seconds() // 60))
    emergency_task = MaintenanceTaskSchema(
        id="EMERGENCY_UNSAFE_FORCE",
        name="Emergency Track Repair",
        section_id=sec_a,
        duration_minutes=duration,
        earliest_start=block_b.start_time,
        latest_end=block_b.end_time,
        priority=9,
        is_emergency=True,
    )

    # replan_rl will evaluate candidate slots for emergency_task on sec_a.
    # Any candidate slot chosen will overlap block_b on adjacent section sec_b,
    # which violates Module 5 hard safety adjacency.
    result = replan_rl(
        model_path=quick_trained_model,
        previous_solve_result=prev_solve,
        new_emergency_task=emergency_task,
        all_existing_tasks=tasks,
        sections=sections,
        train_slots=train_slots,
        profile=profile,
        unsafe_adjacency_pairs=unsafe_pairs,
        time_horizon_minutes=1440,
        base_time=BASE_TEST_TIME,
        graph=graph,
        adjacencies=adjacencies,
    )

    assert isinstance(result, ReplanResult)
    assert result.strategy == "rl"
    assert result.rl_fallback_triggered is True, "rl_fallback_triggered should be True when unsafe proposal detected"

    # Module 5 ground truth check: Final schedule must have ZERO safety violations
    final_violations = count_safety_violations(result.scheduled_blocks, unsafe_pairs)
    assert final_violations == 0, f"Expected 0 safety violations in fallback schedule, got {final_violations}"


def test_replan_rl_wall_time_faster_than_full_resolve(quick_trained_model: str) -> None:
    """replan_rl's wall_time_seconds is measurably lower than replan_full_resolve's on the same scenario."""
    profile = ProfileRegistry.get("metro")
    net = generate_synthetic_network("metro", seed=12, section_count=8, train_count=15, task_count=6)
    sections = net["sections"]
    tasks = net["maintenance_tasks"]
    train_slots = net["train_slots"]
    adjacencies = net["adjacencies"]

    graph = NetworkGraph.build_from_sections(sections, adjacencies)
    unsafe_pairs = precompute_unsafe_adjacency_pairs(
        graph=graph,
        profile=profile,
        sections=sections,
        train_slots=train_slots,
    )

    prev_solve = solve_schedule(
        sections=sections,
        tasks=tasks,
        train_slots=train_slots,
        profile=profile,
        time_horizon_minutes=1440,
        base_time=BASE_TEST_TIME,
        graph=graph,
        adjacencies=adjacencies,
    )

    # Find a section that is free or safe
    sec_id = sections[-1].id if hasattr(sections[-1], "id") else sections[-1]["id"]
    emergency_task = MaintenanceTaskSchema(
        id="EMERGENCY_FAST_SPEED",
        name="Fast Speed Emergency Task",
        section_id=sec_id,
        duration_minutes=30,
        earliest_start=BASE_TEST_TIME + timedelta(hours=10),
        latest_end=BASE_TEST_TIME + timedelta(hours=20),
        priority=8,
        is_emergency=True,
    )

    # Run full resolve
    full_result = replan_full_resolve(
        previous_solve_result=prev_solve,
        new_emergency_task=emergency_task,
        all_existing_tasks=tasks,
        sections=sections,
        train_slots=train_slots,
        profile=profile,
        time_horizon_minutes=1440,
        base_time=BASE_TEST_TIME,
        graph=graph,
        adjacencies=adjacencies,
    )

    # Run repeated RL inference runs
    rl_times = []
    for _ in range(3):
        rl_res = replan_rl(
            model_path=quick_trained_model,
            previous_solve_result=prev_solve,
            new_emergency_task=emergency_task,
            all_existing_tasks=tasks,
            sections=sections,
            train_slots=train_slots,
            profile=profile,
            unsafe_adjacency_pairs=unsafe_pairs,
            time_horizon_minutes=1440,
            base_time=BASE_TEST_TIME,
            graph=graph,
            adjacencies=adjacencies,
        )
        rl_times.append(rl_res.wall_time_seconds)

    avg_rl_time = sum(rl_times) / len(rl_times)
    # Neural forward pass + validation is sub-second (usually < 0.05s)
    assert avg_rl_time < full_result.wall_time_seconds, (
        f"RL average time ({avg_rl_time:.4f}s) should be lower than full resolve ({full_result.wall_time_seconds:.4f}s)"
    )


def test_run_replan_benchmark_with_and_without_rl(quick_trained_model: str) -> None:
    """run_replan_benchmark returns all 3 strategies when rl_model_path given, and 2 strategies when omitted."""
    profile = ProfileRegistry.get("metro")
    net = generate_synthetic_network("metro", seed=44, section_count=8, train_count=12, task_count=5)
    sections = net["sections"]
    tasks = net["maintenance_tasks"]
    train_slots = net["train_slots"]
    adjacencies = net["adjacencies"]

    graph = NetworkGraph.build_from_sections(sections, adjacencies)
    unsafe_pairs = precompute_unsafe_adjacency_pairs(
        graph=graph,
        profile=profile,
        sections=sections,
        train_slots=train_slots,
    )

    prev_solve = solve_schedule(
        sections=sections,
        tasks=tasks,
        train_slots=train_slots,
        profile=profile,
        time_horizon_minutes=1440,
        base_time=BASE_TEST_TIME,
        graph=graph,
        adjacencies=adjacencies,
    )

    sec_id = sections[1].id if hasattr(sections[1], "id") else sections[1]["id"]
    emergency_task = MaintenanceTaskSchema(
        id="EMERGENCY_BENCH_TEST",
        name="Emergency Bench Task",
        section_id=sec_id,
        duration_minutes=45,
        earliest_start=BASE_TEST_TIME + timedelta(hours=2),
        latest_end=BASE_TEST_TIME + timedelta(hours=10),
        priority=8,
        is_emergency=True,
    )

    # 1. When rl_model_path is omitted: Regression check against Module 11 behavior
    bench_without_rl = run_replan_benchmark(
        previous_solve_result=prev_solve,
        previous_model_context={"base_time": BASE_TEST_TIME, "unsafe_adjacency_pairs": unsafe_pairs},
        new_emergency_task=emergency_task,
        all_existing_tasks=tasks,
        sections=sections,
        train_slots=train_slots,
        profile=profile,
        time_horizon_minutes=1440,
        base_time=BASE_TEST_TIME,
        graph=graph,
        adjacencies=adjacencies,
    )

    assert "full_resolve" in bench_without_rl
    assert "warm_start" in bench_without_rl
    assert "rl" not in bench_without_rl
    assert "speedup_factor" in bench_without_rl
    assert "objective_delta" in bench_without_rl
    assert "rl" not in bench_without_rl["strategies"]

    # 2. When rl_model_path is provided: Returns all three strategies
    bench_with_rl = run_replan_benchmark(
        previous_solve_result=prev_solve,
        previous_model_context={"base_time": BASE_TEST_TIME, "unsafe_adjacency_pairs": unsafe_pairs},
        new_emergency_task=emergency_task,
        all_existing_tasks=tasks,
        sections=sections,
        train_slots=train_slots,
        profile=profile,
        time_horizon_minutes=1440,
        base_time=BASE_TEST_TIME,
        graph=graph,
        adjacencies=adjacencies,
        rl_model_path=quick_trained_model,
    )

    assert "full_resolve" in bench_with_rl
    assert "warm_start" in bench_with_rl
    assert "rl" in bench_with_rl
    assert isinstance(bench_with_rl["rl"], ReplanResult)
    assert bench_with_rl["rl"].strategy == "rl"

    assert "rl_fallback_triggered" in bench_with_rl
    assert isinstance(bench_with_rl["rl_fallback_triggered"], bool)

    assert "rl" in bench_with_rl["strategies"]
    assert "rl" in bench_with_rl["comparisons"]

    rl_comp = bench_with_rl["comparisons"]["rl"]
    assert "speedup_factor" in rl_comp
    assert rl_comp["speedup_factor"] > 0.0
    assert "wall_time_seconds" in rl_comp
    assert "tasks_changed_count" in rl_comp


def test_model_caching_behavior(quick_trained_model: str) -> None:
    """Verifies that replan_rl caches loaded PPO models by path."""
    clear_model_cache()

    from cadence.flux.policy_replan import _MODEL_CACHE

    assert len(_MODEL_CACHE) == 0

    profile = ProfileRegistry.get("metro")
    net = generate_synthetic_network("metro", seed=1, section_count=5, train_count=5, task_count=2)
    sections = net["sections"]
    tasks = net["maintenance_tasks"]
    train_slots = net["train_slots"]
    adjacencies = net["adjacencies"]
    graph = NetworkGraph.build_from_sections(sections, adjacencies)

    prev_solve = solve_schedule(
        sections=sections,
        tasks=tasks,
        train_slots=train_slots,
        profile=profile,
        time_horizon_minutes=1440,
        base_time=BASE_TEST_TIME,
        graph=graph,
        adjacencies=adjacencies,
    )

    sec_id = sections[0].id if hasattr(sections[0], "id") else sections[0]["id"]
    emergency_task = MaintenanceTaskSchema(
        id="EMERGENCY_CACHE_TEST",
        name="Cache Test Emergency",
        section_id=sec_id,
        duration_minutes=30,
        earliest_start=BASE_TEST_TIME + timedelta(hours=1),
        latest_end=BASE_TEST_TIME + timedelta(hours=5),
        priority=5,
        is_emergency=True,
    )

    replan_rl(
        model_path=quick_trained_model,
        previous_solve_result=prev_solve,
        new_emergency_task=emergency_task,
        all_existing_tasks=tasks,
        sections=sections,
        train_slots=train_slots,
        profile=profile,
        unsafe_adjacency_pairs=[],
        time_horizon_minutes=1440,
        base_time=BASE_TEST_TIME,
        graph=graph,
        adjacencies=adjacencies,
    )

    assert len(_MODEL_CACHE) == 1
    cached_instance = list(_MODEL_CACHE.values())[0]

    # Calling again uses the exact same model instance in memory
    replan_rl(
        model_path=quick_trained_model,
        previous_solve_result=prev_solve,
        new_emergency_task=emergency_task,
        all_existing_tasks=tasks,
        sections=sections,
        train_slots=train_slots,
        profile=profile,
        unsafe_adjacency_pairs=[],
        time_horizon_minutes=1440,
        base_time=BASE_TEST_TIME,
        graph=graph,
        adjacencies=adjacencies,
    )

    assert list(_MODEL_CACHE.values())[0] is cached_instance
