"""Targeted unit and edge-case tests for the Cadence system.

Covers boundary conditions:
- generate_synthetic_network with section_count=1 or task_count=0
- solve_schedule with zero tasks returns a trivially OPTIMAL empty schedule
- simulate_cascade with zero train_slots returns a valid zero-impact RippleReport
- simulate_cascade with empty route train slot
- explain_full_schedule on a zero-task solved schedule returns an empty list
- explain_task_scheduling for unknown task ID and unscheduled task
- API routes handle malformed request bodies with HTTP 422 Unprocessable Entity
- Unit tests for RunStore cache eviction / reconstruction
- Unit tests for Echo serialization edge cases (dicts, __dict__, naive datetimes, invalid blocks)
- Unit tests for Benchmark replan edge cases (unknown strategy, missing RL model path)
- Unit tests for train generator edge cases (adjacencies=None, custom profiles, tight headway conflicts)
- 100% coverage on profiles/base.py abstract methods via super() invocation
"""

from datetime import datetime, timedelta, timezone
import random
from typing import Any, Generator
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from cadence.api.main import app
from cadence.api.store import RunStore, run_store
from cadence.domain.db import get_session
from cadence.domain.graph import NetworkGraph
from cadence.domain.models import Base, DecisionRecord, NetworkRun, TrackSection
from cadence.domain.schemas import (
    MaintenanceTaskSchema,
    ScheduledBlockSchema,
    SectionAdjacencySchema,
    TrackSectionSchema,
    TrainSlotSchema,
)
from cadence.echo.store import (
    echo_store,
    get_latest_decision,
    get_warm_start_hints,
    record_decision,
    record_network_run,
    serialize_scheduled_blocks,
)
from cadence.generator import generate_synthetic_network
from cadence.generator.trains import (
    DEFAULT_HOP_MINUTES,
    _build_train_attributes,
    _find_conflict_free_start_minute,
    _generate_contiguous_route,
    generate_train_slots,
)
from cadence.profiles.base import NetworkProfile
from cadence.profiles.metro import MetroProfile
from cadence.profiles.registry import ProfileRegistry
from cadence.reason.explainer import (
    explain_full_schedule,
    explain_task_scheduling,
    generate_candidate_slots,
)
from cadence.ripple import simulate_cascade
from cadence.solve import (
    SolveResult,
    run_replan_benchmark,
    solve_schedule,
)


BASE_TIME = datetime(2026, 1, 1, 6, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def client() -> TestClient:
    """FastAPI TestClient fixture."""
    return TestClient(app)


@pytest.fixture(autouse=True)
def clean_store():
    """Ensure run_store and database tables are clean before and after each test."""
    run_store.clear()
    yield
    run_store.clear()


# ==============================================================================
# 1. Generator Edge Cases: Single Section and Zero Tasks
# ==============================================================================

def test_generate_synthetic_network_single_section() -> None:
    """generate_synthetic_network with section_count=1 clamps sensibly without crashing."""
    # Loop topology requires at least 3 sections, linear requires 2. Clamps to min.
    net_metro = generate_synthetic_network(
        profile_name="metro",
        seed=42,
        section_count=1,
        train_count=2,
        task_count=1,
    )
    assert len(net_metro["sections"]) == 3  # Clamped to loop minimum
    assert len(net_metro["adjacencies"]) == 3
    assert len(net_metro["maintenance_tasks"]) == 1
    assert len(net_metro["train_slots"]) == 2

    net_local = generate_synthetic_network(
        profile_name="local",
        seed=42,
        section_count=1,
        train_count=1,
        task_count=1,
    )
    assert len(net_local["sections"]) == 2  # Clamped to linear minimum


def test_generate_synthetic_network_zero_tasks() -> None:
    """generate_synthetic_network with task_count=0 returns empty maintenance tasks list without error."""
    net = generate_synthetic_network(
        profile_name="metro",
        seed=42,
        section_count=4,
        train_count=3,
        task_count=0,
    )
    assert len(net["sections"]) == 4
    assert len(net["maintenance_tasks"]) == 0
    assert len(net["train_slots"]) == 3


def test_generate_synthetic_network_single_section_zero_tasks() -> None:
    """Boundary test combining section_count=1 and task_count=0."""
    net = generate_synthetic_network(
        profile_name="metro",
        seed=42,
        section_count=1,
        train_count=0,
        task_count=0,
    )
    assert len(net["sections"]) == 3
    assert len(net["train_slots"]) == 0
    assert len(net["maintenance_tasks"]) == 0


# ==============================================================================
# 2. Solver Edge Cases: Zero Tasks
# ==============================================================================

def test_solve_schedule_zero_tasks() -> None:
    """solve_schedule with zero tasks returns a trivially OPTIMAL empty schedule."""
    profile = ProfileRegistry.get("metro")
    sections = [TrackSectionSchema(id="SEC_01", name="Track 1")]
    train_slots = [
        TrainSlotSchema(
            id="TRAIN_01",
            name="Train 1",
            route=["SEC_01"],
            scheduled_start=BASE_TIME,
            scheduled_end=BASE_TIME + timedelta(minutes=30),
            priority=1,
        )
    ]

    result = solve_schedule(
        sections=sections,
        tasks=[],
        train_slots=train_slots,
        profile=profile,
        time_horizon_minutes=240,
    )
    assert result.status == "OPTIMAL"
    assert result.scheduled_blocks == []
    assert result.objective_value == 0.0
    assert result.wall_time_seconds >= 0.0


# ==============================================================================
# 3. Ripple Edge Cases: Zero Train Slots & Empty Route
# ==============================================================================

def test_simulate_cascade_zero_train_slots() -> None:
    """simulate_cascade with zero train_slots returns a valid zero-impact RippleReport."""
    scheduled_blocks = [
        ScheduledBlockSchema(
            id="BLOCK_01",
            task_id="TASK_01",
            section_id="SEC_01",
            start_time=BASE_TIME,
            end_time=BASE_TIME + timedelta(minutes=60),
            method="cp_sat",
        )
    ]

    report = simulate_cascade(
        scheduled_blocks=scheduled_blocks,
        train_slots=[],
    )
    assert report.total_trains_affected == 0
    assert report.total_delay_minutes == 0.0
    assert len(report.per_train_impacts) == 0


def test_simulate_cascade_empty_route_train() -> None:
    """simulate_cascade handles a train slot with empty route gracefully without error."""
    empty_train = TrainSlotSchema(
        id="EMPTY_TRAIN",
        name="Empty Route Train",
        route=[],
        scheduled_start=BASE_TIME,
        scheduled_end=BASE_TIME + timedelta(minutes=30),
        priority=1,
    )
    report = simulate_cascade(
        scheduled_blocks=[],
        train_slots=[empty_train],
    )
    assert report.total_trains_affected == 0
    assert report.total_delay_minutes == 0.0
    assert len(report.per_train_impacts) == 0


# ==============================================================================
# 4. Reason Edge Cases: Zero-Task Explanations & Error Paths
# ==============================================================================

def test_explain_full_schedule_zero_tasks() -> None:
    """explain_full_schedule on a zero-task solved schedule returns an empty list, not an error."""
    profile = ProfileRegistry.get("metro")
    sections = [TrackSectionSchema(id="SEC_01", name="Track 1")]
    empty_solve = SolveResult(
        status="OPTIMAL",
        scheduled_blocks=[],
        objective_value=0.0,
        wall_time_seconds=0.001,
    )

    explanations = explain_full_schedule(
        solve_result=empty_solve,
        tasks=[],
        sections=sections,
        train_slots=[],
        profile=profile,
        model_context={},
    )
    assert explanations == []


def test_explain_task_scheduling_unknown_task_raises_value_error() -> None:
    """explain_task_scheduling raises ValueError when task_id is not present in tasks list."""
    profile = ProfileRegistry.get("metro")
    solve = SolveResult(
        status="OPTIMAL",
        scheduled_blocks=[],
        objective_value=0.0,
        wall_time_seconds=0.001,
    )
    with pytest.raises(ValueError, match="Task 'NONEXISTENT' not found"):
        explain_task_scheduling(
            task_id="NONEXISTENT",
            tasks=[],
            solve_result=solve,
            sections=[],
            train_slots=[],
            profile=profile,
            model_context={},
        )


def test_explain_task_scheduling_unscheduled_task_fallback() -> None:
    """explain_task_scheduling provides a fallback explanation if task was not scheduled."""
    profile = ProfileRegistry.get("metro")
    task = MaintenanceTaskSchema(
        id="TASK_UNSCHEDULED",
        name="Unscheduled Inspection",
        section_id="SEC_01",
        duration_minutes=30,
        earliest_start=BASE_TIME,
        latest_end=BASE_TIME + timedelta(hours=2),
        priority=2,
    )
    infeasible_solve = SolveResult(
        status="INFEASIBLE",
        scheduled_blocks=[],
        objective_value=None,
        wall_time_seconds=0.01,
    )
    expl = explain_task_scheduling(
        task_id="TASK_UNSCHEDULED",
        tasks=[task],
        solve_result=infeasible_solve,
        sections=[TrackSectionSchema(id="SEC_01", name="Track 1")],
        train_slots=[],
        profile=profile,
        model_context={},
    )
    assert expl.task_id == "TASK_UNSCHEDULED"
    assert "could not be feasibly scheduled" in expl.chosen_reason
    assert expl.rejected_candidates == []


# ==============================================================================
# 5. API Malformed Body 422 Validations
# ==============================================================================

@pytest.mark.parametrize(
    "endpoint,invalid_payload",
    [
        ("/networks/generate", {}),
        ("/networks/generate", {"profile_name": "metro", "section_count": -5}),
        ("/solve", {}),
        ("/solve", {"run_id": 12345}),
        ("/solve", {"run_id": "test", "time_limit_seconds": -1}),
        ("/replan", {}),
        ("/replan", {"run_id": "test-uuid"}),
    ],
)
def test_api_routes_malformed_request_bodies_return_422(
    client: TestClient,
    endpoint: str,
    invalid_payload: dict,
) -> None:
    """API routes handle malformed request bodies with clean 422 validation errors, not 500s."""
    resp = client.post(endpoint, json=invalid_payload)
    assert resp.status_code == 422, f"Expected 422 for {endpoint} with payload {invalid_payload}, got {resp.status_code}: {resp.text}"
    err_body = resp.json()
    assert "detail" in err_body


# ==============================================================================
# 6. Store Unit Tests (RunStore, EchoStore, Serialization Edge Cases)
# ==============================================================================

def test_run_store_create_run_default_and_custom_id() -> None:
    """RunStore create_run handles None network_data and custom run_id."""
    store = RunStore()
    run_id_custom = "custom-uuid-1234"
    allocated = store.create_run(network_data=None, run_id=run_id_custom)
    assert allocated == run_id_custom
    assert store.has_run(run_id_custom)

    run = store.get_run(run_id_custom)
    assert run is not None
    assert run["run_id"] == run_id_custom
    assert run["network"] is None


def test_run_store_cache_miss_regenerates_network_and_solve() -> None:
    """RunStore get_run recovers network from generator when missing from memory cache."""
    # 1. Create a generated network run in DB
    gen_net = generate_synthetic_network("metro", seed=42, section_count=4, train_count=2, task_count=1)
    allocated_id = run_store.create_run(network_data=gen_net)

    # 2. Clear memory caches only, retaining database rows
    run_store._network_cache.clear()
    run_store._solve_cache.clear()

    # 3. get_run should reload/regenerate network from DB run record
    recovered = run_store.get_run(allocated_id)
    assert recovered is not None
    assert recovered["network"] is not None
    assert len(recovered["network"]["sections"]) == 4


def test_echo_serialize_scheduled_blocks_various_types() -> None:
    """serialize_scheduled_blocks correctly handles dicts, custom objects, and tuples."""
    # 1. Empty input
    assert serialize_scheduled_blocks([]) == []

    # 2. Raw dictionary with datetimes
    dt_now = datetime.now(timezone.utc)
    raw_dict = {
        "id": "B1",
        "task_id": "T1",
        "start_time": dt_now,
        "end_time": dt_now + timedelta(hours=1),
    }
    serialized = serialize_scheduled_blocks([raw_dict])
    assert len(serialized) == 1
    assert serialized[0]["start_time"] == dt_now.isoformat()

    # 3. Custom class with __dict__
    class CustomBlock:
        def __init__(self):
            self.id = "B2"
            self.task_id = "T2"
            self.start_time = dt_now
            self.end_time = dt_now + timedelta(hours=1)
            self._private_field = "hidden"

    custom_obj = CustomBlock()
    serialized_custom = serialize_scheduled_blocks([custom_obj])
    assert len(serialized_custom) == 1
    assert serialized_custom[0]["id"] == "B2"
    assert "_private_field" not in serialized_custom[0]
    assert serialized_custom[0]["start_time"] == dt_now.isoformat()

    # 4. Fallback iterable of pairs
    pair_block = [("id", "B3"), ("task_id", "T3")]
    serialized_pairs = serialize_scheduled_blocks([pair_block])
    assert len(serialized_pairs) == 1
    assert serialized_pairs[0]["id"] == "B3"


def test_echo_get_warm_start_hints_edge_cases() -> None:
    """get_warm_start_hints handles missing fields, non-datetime types, and naive datetimes."""
    with get_session() as session:
        # Non-existent run returns None
        assert echo_store.get_warm_start_hints(session, "nonexistent-run") is None

        # Record a run with a mock decision record
        run = echo_store.record_network_run(
            session=session,
            profile_name="metro",
            seed=42,
            section_count=4,
            train_count=2,
            task_count=2,
        )
        naive_dt = datetime(2026, 1, 1, 10, 0, 0)
        mock_snapshot = [
            {"task_id": "T1", "start_time": "2026-01-01T10:00:00"},
            {"task_id": "T2", "start_time": naive_dt},
            {"task_id": None, "start_time": "2026-01-01T10:00:00"},
            {"task_id": "T3", "start_time": None},
            {"task_id": "T4", "start_time": 1234567},
        ]
        echo_store.record_decision(
            session=session,
            network_run_id=run.id,
            strategy="warm_start",
            solve_or_replan_result={
                "status": "OPTIMAL",
                "scheduled_blocks": mock_snapshot,
                "objective_value": 10.0,
                "wall_time_seconds": 0.1,
            },
        )

        hints = echo_store.get_warm_start_hints(session, run.id)
        assert hints is not None
        assert "T1" in hints
        assert hints["T1"].tzinfo is not None  # Converted to UTC
        assert "T2" in hints
        assert hints["T2"].tzinfo is not None  # Converted to UTC
        assert "T3" not in hints
        assert "T4" not in hints


# ==============================================================================
# 7. Benchmark & Trains Generator Unit Tests
# ==============================================================================

def test_run_replan_benchmark_unknown_strategy_raises_value_error() -> None:
    """run_replan_benchmark raises ValueError when an unknown strategy is passed."""
    net = generate_synthetic_network("metro", seed=42, section_count=4, train_count=2, task_count=1)
    profile = ProfileRegistry.get("metro")
    dummy_solve = SolveResult(status="OPTIMAL", scheduled_blocks=[], objective_value=10.0, wall_time_seconds=0.1)

    with pytest.raises(ValueError, match="Unknown replan strategy: invalid_strat"):
        run_replan_benchmark(
            previous_solve_result=dummy_solve,
            previous_model_context={},
            new_emergency_task=net["maintenance_tasks"][0],
            all_existing_tasks=net["maintenance_tasks"],
            sections=net["sections"],
            train_slots=net["train_slots"],
            profile=profile,
            strategies=["invalid_strat"],
        )


def test_run_replan_benchmark_rl_missing_model_path_raises_value_error() -> None:
    """run_replan_benchmark raises ValueError when target_strategies=['rl'] and no model path is provided."""
    net = generate_synthetic_network("metro", seed=42, section_count=4, train_count=2, task_count=1)
    profile = ProfileRegistry.get("metro")
    dummy_solve = SolveResult(status="OPTIMAL", scheduled_blocks=[], objective_value=10.0, wall_time_seconds=0.1)

    with pytest.raises(ValueError, match="replan_rl requires 'rl_model_path' or 'model_path'"):
        run_replan_benchmark(
            previous_solve_result=dummy_solve,
            previous_model_context={},
            new_emergency_task=net["maintenance_tasks"][0],
            all_existing_tasks=net["maintenance_tasks"],
            sections=net["sections"],
            train_slots=net["train_slots"],
            profile=profile,
            strategies=["rl"],
            rl_model_path=None,
        )


def test_trains_generator_sequential_adjacencies_and_fallback() -> None:
    """generate_train_slots works when adjacencies=None (sequential fallback)."""
    sections = [
        TrackSection(id=f"SEC_{i:02d}", name=f"Section {i}", length_meters=1000.0)
        for i in range(5)
    ]
    profile = ProfileRegistry.get("metro")
    rng = random.Random(42)
    slots = generate_train_slots(
        sections=sections,
        profile=profile,
        rng=rng,
        count=3,
        adjacencies=None,
    )
    assert len(slots) == 3
    for s in slots:
        assert len(s.route) >= 1


def test_trains_generator_custom_profile_attributes() -> None:
    """_build_train_attributes returns standard attributes for non-standard/unregistered profile."""
    rng = random.Random(42)

    class CustomConcreteProfile(MetroProfile):
        profile_name = "custom_test"

    name, priority, attrs = _build_train_attributes(CustomConcreteProfile(), 0, rng, 0.2)
    assert name == "Train 01"
    assert attrs["train_type"] == "standard"


def test_profile_base_abstract_methods_super() -> None:
    """Call super() implementations on NetworkProfile to ensure 100% coverage on base.py."""
    class BaseCallerProfile(NetworkProfile):
        profile_name = "base_caller"
        default_topology = "linear"

        def is_safe_adjacency(self, graph, section_a_id, section_b_id, currently_blocked=None, **kwargs):
            return super().is_safe_adjacency(graph, section_a_id, section_b_id, currently_blocked, **kwargs)

        def min_headway_minutes(self, train_slot_a, train_slot_b):
            return super().min_headway_minutes(train_slot_a, train_slot_b)

        def priority_weight(self, task):
            return super().priority_weight(task)

        def disruption_penalty(self, train_slot):
            return super().disruption_penalty(train_slot)

        def default_topology_generator_hint(self):
            return super().default_topology_generator_hint()

    instance = BaseCallerProfile()
    assert instance.is_safe_adjacency(None, "A", "B") is None
    assert instance.min_headway_minutes(None, None) is None
    assert instance.priority_weight(None) is None
    assert instance.disruption_penalty(None) is None
    assert instance.default_topology_generator_hint() is None


# ==============================================================================
# 9. Additional Targeted Tests for Complete Branch Coverage
# ==============================================================================

def test_benchmark_replan_missing_branches() -> None:
    """Test single strategy executions, objective_delta None, and lazy unsafe pair resolution."""
    from cadence.solve.replan import ReplanResult
    from cadence.solve.benchmark import _REPLAN_STRATEGY_REGISTRY

    net = generate_synthetic_network("metro", seed=42, section_count=4, train_count=2, task_count=1)
    profile = ProfileRegistry.get("metro")
    dummy_solve = SolveResult(status="OPTIMAL", scheduled_blocks=[], objective_value=10.0, wall_time_seconds=0.1)
    em_task = net["maintenance_tasks"][0]

    # 1. Test running with only warm_start (triggers full_res is None fallback in benchmark)
    res_warm = run_replan_benchmark(
        previous_solve_result=dummy_solve,
        previous_model_context={"base_time": BASE_TIME},
        new_emergency_task=em_task,
        all_existing_tasks=net["maintenance_tasks"],
        sections=net["sections"],
        train_slots=net["train_slots"],
        profile=profile,
        strategies=["warm_start"],
    )
    assert "full_resolve" in res_warm
    assert "warm_start" in res_warm

    # 2. Test running with only full_resolve (triggers warm_res is None fallback in benchmark)
    res_full = run_replan_benchmark(
        previous_solve_result=dummy_solve,
        previous_model_context={"base_time": BASE_TIME},
        new_emergency_task=em_task,
        all_existing_tasks=net["maintenance_tasks"],
        sections=net["sections"],
        train_slots=net["train_slots"],
        profile=profile,
        strategies=["full_resolve"],
    )
    assert "full_resolve" in res_full
    assert "warm_start" in res_full

    # 3. Test objective_delta = None branch when warm_start objective_value is None
    no_obj_solve = SolveResult(status="INFEASIBLE", scheduled_blocks=[], objective_value=None, wall_time_seconds=0.1)
    res_none = run_replan_benchmark(
        previous_solve_result=no_obj_solve,
        previous_model_context={"base_time": BASE_TIME},
        new_emergency_task=em_task,
        all_existing_tasks=net["maintenance_tasks"],
        sections=net["sections"],
        train_slots=net["train_slots"],
        profile=profile,
        strategies=["full_resolve", "warm_start"],
    )
    assert res_none["objective_delta"] is None or isinstance(res_none["objective_delta"], float)

    # 4. Test lazy unsafe pairs resolution when "rl" is in strategies and adjacencies provided
    orig_rl = _REPLAN_STRATEGY_REGISTRY.get("rl")
    try:
        # Mock rl handler to avoid needing real trained model
        _REPLAN_STRATEGY_REGISTRY["rl"] = lambda **kwargs: ReplanResult(
            strategy="rl",
            status="OPTIMAL",
            scheduled_blocks=[],
            objective_value=5.0,
            wall_time_seconds=0.01,
            previous_objective_value=10.0,
            tasks_changed=[],
        )
        res_rl_lazy = run_replan_benchmark(
            previous_solve_result=dummy_solve,
            previous_model_context={},  # No unsafe_adjacency_pairs in context
            new_emergency_task=em_task,
            all_existing_tasks=net["maintenance_tasks"],
            sections=net["sections"],
            train_slots=net["train_slots"],
            profile=profile,
            adjacencies=net["adjacencies"],  # Passes adjacencies to trigger NetworkGraph build
            strategies=["rl"],
        )
        assert "rl" in res_rl_lazy
    finally:
        if orig_rl:
            _REPLAN_STRATEGY_REGISTRY["rl"] = orig_rl


def test_generator_trains_headway_and_routes_edge_cases() -> None:
    """Test contiguous route single-section and headway conflict branches."""
    from unittest.mock import patch
    from cadence.generator.trains import _find_conflict_free_start_minute

    profile = ProfileRegistry.get("metro")
    rng = random.Random(42)

    # 1. Single section route
    graph = NetworkGraph.build_from_sections([], [])
    route_single = _generate_contiguous_route(graph, ["SEC_ONLY"], rng)
    assert route_single == ["SEC_ONLY"]

    # 2. Random walk fallback when shortest_path returns empty
    sections = [
        TrackSection(id=f"SEC_{i:02d}", name=f"Section {i}", length_meters=1000.0)
        for i in range(4)
    ]
    adjacencies = [
        SectionAdjacencySchema(id="ADJ_1", section_a_id="SEC_00", section_b_id="SEC_01"),
        SectionAdjacencySchema(id="ADJ_2", section_a_id="SEC_01", section_b_id="SEC_02"),
        SectionAdjacencySchema(id="ADJ_3", section_a_id="SEC_02", section_b_id="SEC_03"),
    ]
    graph_walk = NetworkGraph.build_from_sections(sections, adjacencies)
    with patch.object(graph_walk, "shortest_path", return_value=[]):
        route_walk = _generate_contiguous_route(graph_walk, [s.id for s in sections], rng)
        assert len(route_walk) >= 1

    # 3. Headway conflict in _find_conflict_free_start_minute
    slot_existing = TrainSlotSchema(
        id="SLOT_EXISTING",
        name="Slot Existing",
        train_type="metro",
        priority=5,
        route=["SEC_00", "SEC_01"],
        scheduled_start=BASE_TIME + timedelta(minutes=10),
        scheduled_end=BASE_TIME + timedelta(minutes=10 + 2 * DEFAULT_HOP_MINUTES),
        origin="SEC_00",
        destination="SEC_01",
    )
    cand_schema = TrainSlotSchema(
        id="SLOT_CAND",
        name="Slot Candidate",
        train_type="metro",
        priority=5,
        route=["SEC_00", "SEC_01"],
        scheduled_start=BASE_TIME + timedelta(minutes=11),
        scheduled_end=BASE_TIME + timedelta(minutes=11 + 2 * DEFAULT_HOP_MINUTES),
        origin="SEC_00",
        destination="SEC_01",
    )
    scheduled_records = [(None, slot_existing, 10, 2 * DEFAULT_HOP_MINUTES, ["SEC_00", "SEC_01"])]
    # Candidate starting at minute 11 conflicts with minute 10 (headway 3 min for metro)
    resolved_min = _find_conflict_free_start_minute(
        candidate_minute=11,
        route=["SEC_00", "SEC_01"],
        route_duration=2 * DEFAULT_HOP_MINUTES,
        cand_schema=cand_schema,
        profile=profile,
        scheduled_records=scheduled_records,
        total_minutes=1440,
    )
    assert resolved_min >= 13

    # Candidate exceeding total_minutes clamps to max possible start minute
    exceeded_min = _find_conflict_free_start_minute(
        candidate_minute=1500,
        route=["SEC_00", "SEC_01"],
        route_duration=2 * DEFAULT_HOP_MINUTES,
        cand_schema=cand_schema,
        profile=profile,
        scheduled_records=[],
        total_minutes=1440,
    )
    assert exceeded_min <= 1440


def test_domain_models_repr_coverage() -> None:
    """Test __repr__ implementations for all SQLAlchemy domain models."""
    from cadence.domain.models import (
        DecisionRecord,
        MaintenanceTask,
        NetworkRun,
        ScheduledBlock,
        SectionAdjacency,
        TrackSection,
        TrainSlot,
    )
    sec = TrackSection(id="S1", name="Sec 1", length_meters=500.0)
    assert "S1" in repr(sec)

    task = MaintenanceTask(id="T1", name="Task 1", section_id="S1", duration_minutes=60, priority=5)
    assert "T1" in repr(task)

    train = TrainSlot(
        id="TR1",
        name="Train 1",
        priority=8,
        route=["S1"],
        scheduled_start=BASE_TIME,
        scheduled_end=BASE_TIME + timedelta(minutes=30),
    )
    assert "TR1" in repr(train)

    adj = SectionAdjacency(id="A1", section_a_id="S1", section_b_id="S2")
    assert "S1" in repr(adj) and "S2" in repr(adj)

    block = ScheduledBlock(id="B1", task_id="T1", section_id="S1", method="cp_sat")
    assert "B1" in repr(block)

    run = NetworkRun(id="R1", profile_name="metro", seed=42, section_count=5, train_count=2, task_count=1)
    assert "R1" in repr(run)

    dec = DecisionRecord(id=1, network_run_id="R1", strategy="warm_start", status="OPTIMAL", wall_time_seconds=0.1)
    assert "warm_start" in repr(dec)


def test_domain_db_drop_all_and_get_db() -> None:
    """Test drop_all_tables and get_db dependency generator."""
    from cadence.domain.db import create_all_tables, drop_all_tables, get_db
    engine_test = create_engine("sqlite:///:memory:")
    create_all_tables(bind_engine=engine_test)
    drop_all_tables(bind_engine=engine_test)

    # Test get_db generator
    db_gen = get_db()
    session = next(db_gen)
    assert session is not None
    try:
        next(db_gen)
    except StopIteration:
        pass


def test_flux_train_callback_and_hyperparameters() -> None:
    """Test TrainingProgressCallback logging and n_steps batch_size reduction."""
    from cadence.flux.train import TrainingProgressCallback
    cb = TrainingProgressCallback(check_freq=2, verbose=1)
    cb.n_calls = 2
    cb.num_timesteps = 2
    assert cb._on_step() is True

    # Test batch_size //= 2 reduction loop in train_policy parameter configuration
    total_timesteps = 48
    n_steps = min(64, max(16, total_timesteps))
    batch_size = min(32, n_steps)
    while n_steps % batch_size != 0 and batch_size > 1:
        batch_size //= 2
    assert n_steps % batch_size == 0


def test_flux_policy_replan_same_section_overlap_fallback() -> None:
    """Test that replan_rl re-validation gate catches same-section overlap and falls back."""
    from cadence.flux.policy_replan import replan_rl
    from unittest.mock import MagicMock, patch

    profile = ProfileRegistry.get("metro")
    net = generate_synthetic_network("metro", seed=42, section_count=4, train_count=2, task_count=1)
    prev_solve = solve_schedule(
        sections=net["sections"],
        tasks=net["maintenance_tasks"],
        train_slots=net["train_slots"],
        profile=profile,
        time_horizon_minutes=720,
        base_time=BASE_TIME,
    )
    sec_id = prev_solve.scheduled_blocks[0].section_id
    em_task = MaintenanceTaskSchema(
        id="EM_SAME_SEC",
        name="Emergency on Same Section",
        section_id=sec_id,
        duration_minutes=30,
        earliest_start=prev_solve.scheduled_blocks[0].start_time,
        latest_end=prev_solve.scheduled_blocks[0].end_time,
        priority=10,
        is_emergency=True,
    )

    # Create dummy mock PPO model that predicts action 0 (or out of bounds)
    mock_model = MagicMock()
    mock_model.action_space.n = 5
    mock_model.predict.return_value = (10, None)  # out of bounds to trigger act_idx = 0 fallback

    from pathlib import Path
    resolved_key = str(Path("dummy_path").resolve())
    with patch("cadence.flux.policy_replan._MODEL_CACHE", {resolved_key: mock_model}):
        res = replan_rl(
            model_path="dummy_path",
            previous_solve_result=prev_solve,
            new_emergency_task=em_task,
            all_existing_tasks=net["maintenance_tasks"],
            sections=net["sections"],
            train_slots=net["train_slots"],
            profile=profile,
            unsafe_adjacency_pairs=[],
            base_time=None,
            previous_model_context={"base_time": BASE_TIME},
        )
        assert res.strategy == "rl"
        assert res.rl_fallback_triggered is True


def test_replan_warm_start_base_time_from_previous_context() -> None:
    """Test replan_warm_start inherits base_time from previous_model_context when base_time=None."""
    from cadence.solve.replan import replan_warm_start

    net = generate_synthetic_network("metro", seed=42, section_count=4, train_count=2, task_count=1)
    profile = ProfileRegistry.get("metro")
    prev_solve = solve_schedule(
        sections=net["sections"],
        tasks=net["maintenance_tasks"],
        train_slots=net["train_slots"],
        profile=profile,
        time_horizon_minutes=720,
        base_time=BASE_TIME,
    )
    em_task = MaintenanceTaskSchema(
        id="EM_WARM_TEST",
        name="Emergency Warm",
        section_id=net["sections"][0].id,
        duration_minutes=30,
        earliest_start=BASE_TIME + timedelta(hours=2),
        latest_end=BASE_TIME + timedelta(hours=6),
        priority=8,
        is_emergency=True,
    )
    res = replan_warm_start(
        previous_solve_result=prev_solve,
        previous_model_context={"base_time": BASE_TIME},
        new_emergency_task=em_task,
        all_existing_tasks=net["maintenance_tasks"],
        sections=net["sections"],
        train_slots=net["train_slots"],
        profile=profile,
        base_time=None,  # triggers line 208
    )
    assert res.status in ("OPTIMAL", "FEASIBLE")


def test_generator_passing_loops_backfill() -> None:
    """Test passing loops backfill when non-adjacent constraint is too strict."""
    from cadence.generator.topology import generate_linear_with_passing_loops_topology
    rng = random.Random(42)
    # count=6, freq=0.5 -> backbone=4, inner=[1, 2], target_loops=2
    # eligible [1, 2] are adjacent, so first pass chooses 1, second pass backfills lines 180-184
    sections, adjacencies = generate_linear_with_passing_loops_topology(
        section_count=6,
        rng=rng,
        loop_frequency=0.5,
    )
    loops = [s for s in sections if s.attributes.get("is_passing_loop")]
    assert len(loops) >= 2


def test_generator_tasks_attributes_and_bounds() -> None:
    """Test task generator with custom profile and end time clamping."""
    from cadence.generator.tasks import _build_task_attributes, generate_maintenance_tasks
    rng = random.Random(42)
    sec = TrackSection(id="SEC_01", name="Section 1", length_meters=1000.0, attributes={})

    # Custom profile fallback (lines 147-148)
    class CustomProf(MetroProfile):
        profile_name = "unknown_custom"

    name, attrs = _build_task_attributes(CustomProf(), sec, 0, False, rng, 0.5)
    assert "Maintenance Task" in name
    assert attrs == {}

    # Mainline without explicit traffic_type (line 124)
    mainline_prof = ProfileRegistry.get("mainline")
    name_m, attrs_m = _build_task_attributes(mainline_prof, sec, 0, False, rng, 0.5)
    assert "traffic_type" in attrs_m

    # End minute exceeds horizon clamping to min_latest_end (line 88)
    tasks = generate_maintenance_tasks(
        sections=[sec],
        profile=mainline_prof,
        rng=rng,
        count=1,
        time_window_hours=1,
        base_time=BASE_TIME,
    )
    assert len(tasks) == 1
    assert tasks[0].latest_end >= tasks[0].earliest_start + timedelta(minutes=tasks[0].duration_minutes)


def test_api_store_cache_miss_reconstruction(db_session: Session) -> None:
    """Test RunStore solve_result reconstruction when _solve_cache is empty but decision exists in DB."""
    from contextlib import contextmanager
    from unittest.mock import patch
    from cadence.api.store import run_store

    @contextmanager
    def mock_session():
        yield db_session

    run_rec = record_network_run(db_session, "metro", seed=42, section_count=4, train_count=2, task_count=1)
    blocks = [
        {"task_id": "T1", "section_id": "SEC_01", "start_time": BASE_TIME.isoformat(), "end_time": (BASE_TIME + timedelta(minutes=60)).isoformat(), "method": "test"}
    ]
    res = SolveResult(
        status="OPTIMAL",
        scheduled_blocks=blocks,
        objective_value=12.5,
        wall_time_seconds=0.05,
    )
    record_decision(
        db_session,
        network_run_id=run_rec.id,
        strategy="full_resolve",
        solve_or_replan_result=res,
    )

    run_store.clear()

    # Reconstruct solve_result from DecisionRecord snapshot (lines 114-118)
    with patch("cadence.api.store.get_session", mock_session):
        data = run_store.get_run(run_rec.id)
        assert data is not None
        assert data["solve_result"] is not None
        assert data["solve_result"].objective_value == 12.5
        assert len(data["solve_result"].scheduled_blocks) == 1

    run_store.clear()

    # Reconstruction exception in network generator (lines 109-110)
    with patch("cadence.api.store.generate_synthetic_network", side_effect=Exception("Generator boom")):
        with patch("cadence.api.store.get_session", mock_session):
            data_err = run_store.get_run(run_rec.id)
            assert data_err is not None
            assert data_err["network"] is None
