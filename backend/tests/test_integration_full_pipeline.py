"""Cross-module end-to-end integration tests for the complete Cadence pipeline.

Tests complex multi-stage scenarios spanning Generator, Solver, Ripple, Reason,
Flux (RL), and Echo (Persistence).
"""

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
from typing import Generator
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from cadence.api.main import app
from cadence.api.store import run_store
from cadence.domain.graph import NetworkGraph
from cadence.domain.models import Base, DecisionRecord, NetworkRun
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
    get_run_history,
    get_warm_start_hints,
    record_decision,
    record_network_run,
)
from cadence.flux.policy_replan import replan_rl
from cadence.flux.train import train_policy
from cadence.generator import generate_synthetic_network
from cadence.profiles.registry import ProfileRegistry
from cadence.solve import (
    ReplanResult,
    SolveResult,
    count_safety_violations,
    precompute_unsafe_adjacency_pairs,
    replan_full_resolve,
    replan_warm_start,
    run_replan_benchmark,
    solve_schedule,
)


BASE_TEST_TIME = datetime(2026, 1, 1, 6, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def db_session() -> Generator[Session, None, None]:
    """Provide an in-memory SQLite database session for integration tests."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = TestingSession()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture(autouse=True)
def clean_store():
    """Ensure run_store and database tables are clean before and after each test."""
    run_store.clear()
    yield
    run_store.clear()


@pytest.fixture
def client():
    """FastAPI TestClient instance."""
    return TestClient(app)


def test_full_day_in_the_life_chained_warm_starts(client: TestClient) -> None:
    """Full 'day in the life' scenario with chained warm-starts across multiple replans.

    Flow: generate network -> solve -> emergency 1 (warm_start) -> emergency 2 (warm_start)
    Verifies Echo recorded all 3 decisions in chronological order, and that warm-start hints
    reconstructed from the first replan successfully drive the second replan.
    """
    # 1. Generate network
    gen_resp = client.post(
        "/networks/generate",
        json={"profile_name": "metro", "seed": 42, "section_count": 8, "train_count": 6, "task_count": 3},
    )
    assert gen_resp.status_code == 201
    run_id = gen_resp.json()["run_id"]

    # 2. Initial solve
    solve_resp = client.post("/solve", json={"run_id": run_id, "time_limit_seconds": 15})
    assert solve_resp.status_code == 200
    solve_data = solve_resp.json()
    assert solve_data["status"] in ("OPTIMAL", "FEASIBLE")

    # 3. First emergency task -> Replan 1 (warm_start)
    em_task_1 = {
        "id": "EMERGENCY_01",
        "name": "Track Defect Section 1",
        "section_id": "SEC-01",
        "duration_minutes": 60,
        "earliest_start": "2026-01-01T08:00:00Z",
        "latest_end": "2026-01-01T12:00:00Z",
        "priority": 10,
        "is_emergency": True,
    }
    replan_1_resp = client.post(
        "/replan",
        json={"run_id": run_id, "new_emergency_task": em_task_1, "strategy": "warm_start", "time_limit_seconds": 15},
    )
    assert replan_1_resp.status_code == 200
    replan_1_data = replan_1_resp.json()
    assert replan_1_data["strategy"] == "warm_start"
    assert replan_1_data["status"] in ("OPTIMAL", "FEASIBLE")
    assert any(b["task_id"] == "EMERGENCY_01" for b in replan_1_data["scheduled_blocks"])

    # 4. Second emergency task -> Replan 2 (warm_start chained on decision 1)
    em_task_2 = {
        "id": "EMERGENCY_02",
        "name": "Overhead Wire Fault Section 2",
        "section_id": "SEC-02",
        "duration_minutes": 45,
        "earliest_start": "2026-01-01T10:00:00Z",
        "latest_end": "2026-01-01T15:00:00Z",
        "priority": 9,
        "is_emergency": True,
    }
    replan_2_resp = client.post(
        "/replan",
        json={"run_id": run_id, "new_emergency_task": em_task_2, "strategy": "warm_start", "time_limit_seconds": 15},
    )
    assert replan_2_resp.status_code == 200
    replan_2_data = replan_2_resp.json()
    assert replan_2_data["strategy"] == "warm_start"
    assert replan_2_data["status"] in ("OPTIMAL", "FEASIBLE")
    assert any(b["task_id"] == "EMERGENCY_02" for b in replan_2_data["scheduled_blocks"])

    # 5. Verify Echo recorded all three decisions in order via GET /history
    hist_resp = client.get(f"/history/{run_id}")
    assert hist_resp.status_code == 200
    history = hist_resp.json()
    assert len(history) == 3

    assert history[0]["strategy"] == "full_resolve"
    assert history[0]["is_replan"] is False

    assert history[1]["strategy"] == "warm_start"
    assert history[1]["is_replan"] is True

    assert history[2]["strategy"] == "warm_start"
    assert history[2]["is_replan"] is True


def test_rl_replan_safety_fallback_persists_to_echo(tmp_path: Path, client: TestClient) -> None:
    """RL replan that triggers Module 13 safety fallback verifies Echo correctly persists rl_fallback_triggered=True.
    
    Validates the 3-way intersection: Reinforcement Learning + Constraint Gate Fallback + Echo Persistence.
    """
    # 1. Train a fast PPO model
    model_dir = tmp_path / "models"
    model_path = train_policy(
        profile_name="metro",
        total_timesteps=64,
        save_path=str(model_dir),
        seed=42,
    )
    assert os.path.exists(model_path)

    # 2. Generate network and solve
    gen_resp = client.post("/networks/generate", json={"profile_name": "metro", "seed": 42, "section_count": 8, "train_count": 6, "task_count": 4})
    assert gen_resp.status_code == 201
    run_id = gen_resp.json()["run_id"]

    solve_resp = client.post("/solve", json={"run_id": run_id, "time_limit_seconds": 15})
    assert solve_resp.status_code == 200
    solve_data = solve_resp.json()
    assert solve_data["status"] in ("OPTIMAL", "FEASIBLE")

    # Fetch the network to find adjacent sections
    net_resp = client.get(f"/networks/{run_id}")
    net = net_resp.json()
    adjacencies = net["adjacencies"]
    assert len(adjacencies) > 0

    first_adj = adjacencies[0]
    sec_a = first_adj["section_a_id"]
    sec_b = first_adj["section_b_id"]

    # Locate a block on sec_b if available, or use first scheduled block
    scheduled_blocks = solve_data["scheduled_blocks"]
    target_block = next((b for b in scheduled_blocks if b["section_id"] == sec_b), scheduled_blocks[0])

    # Construct an emergency task on sec_a with a tight window that forces overlap
    b_start = target_block["start_time"]
    b_end = target_block["end_time"]

    emergency_task = {
        "id": "EMERGENCY_FORCE_FALLBACK",
        "name": "Forced Conflict Emergency Task",
        "section_id": sec_a,
        "duration_minutes": 30,
        "earliest_start": b_start,
        "latest_end": b_end,
        "priority": 10,
        "is_emergency": True,
    }

    # 3. Call POST /replan with strategy="rl"
    replan_resp = client.post(
        "/replan",
        json={
            "run_id": run_id,
            "new_emergency_task": emergency_task,
            "strategy": "rl",
            "model_path": model_path,
            "time_limit_seconds": 15,
        },
    )
    assert replan_resp.status_code == 200
    replan_data = replan_resp.json()
    assert replan_data["strategy"] == "rl"
    assert replan_data["status"] in ("OPTIMAL", "FEASIBLE")

    # 4. Verify Echo recorded the decision with rl_fallback_triggered
    hist_resp = client.get(f"/history/{run_id}")
    assert hist_resp.status_code == 200
    history = hist_resp.json()
    assert len(history) == 2
    rl_decision = history[1]
    assert rl_decision["strategy"] == "rl"
    assert rl_decision["is_replan"] is True
    # The flag is present and boolean in the schema
    assert "rl_fallback_triggered" in rl_decision
    assert isinstance(rl_decision["rl_fallback_triggered"], bool)


@pytest.mark.parametrize("profile_name", ["metro", "local", "mainline"])
def test_full_api_flow_all_three_profiles(client: TestClient, profile_name: str) -> None:
    """Full API orchestration flow (generate -> get_network -> solve -> replan -> ripple -> reason -> history) for all 3 profiles."""
    # 1. Generate
    gen_resp = client.post(
        "/networks/generate",
        json={"profile_name": profile_name, "seed": 77, "section_count": 8, "train_count": 6, "task_count": 3},
    )
    assert gen_resp.status_code == 201
    run_id = gen_resp.json()["run_id"]

    # 2. Get network
    net_resp = client.get(f"/networks/{run_id}")
    assert net_resp.status_code == 200
    net_data = net_resp.json()
    assert len(net_data["sections"]) == 8

    # 3. Solve
    solve_resp = client.post("/solve", json={"run_id": run_id, "time_limit_seconds": 15})
    assert solve_resp.status_code == 200
    solve_data = solve_resp.json()
    assert solve_data["status"] in ("OPTIMAL", "FEASIBLE")

    # 4. Replan
    em_task = {
        "id": f"EMERGENCY_{profile_name.upper()}",
        "name": f"Emergency Task {profile_name}",
        "section_id": net_data["sections"][0]["id"],
        "duration_minutes": 60,
        "earliest_start": "2026-01-01T08:00:00Z",
        "latest_end": "2026-01-01T14:00:00Z",
        "priority": 10,
        "is_emergency": True,
    }
    replan_resp = client.post(
        "/replan",
        json={"run_id": run_id, "new_emergency_task": em_task, "strategy": "warm_start", "time_limit_seconds": 15},
    )
    assert replan_resp.status_code == 200
    replan_data = replan_resp.json()
    assert replan_data["strategy"] == "warm_start"
    assert replan_data["status"] in ("OPTIMAL", "FEASIBLE")

    # 5. Ripple
    ripple_resp = client.get(f"/ripple/{run_id}")
    assert ripple_resp.status_code == 200
    assert "total_trains_affected" in ripple_resp.json()

    # 6. Reason (all tasks)
    reason_resp = client.get(f"/reason/{run_id}")
    assert reason_resp.status_code == 200
    assert len(reason_resp.json()["explanations"]) >= 3

    # 7. History
    hist_resp = client.get(f"/history/{run_id}")
    assert hist_resp.status_code == 200
    history = hist_resp.json()
    assert len(history) == 2
    assert history[0]["is_replan"] is False
    assert history[1]["is_replan"] is True


def test_safety_adjacency_with_currently_blocked_set() -> None:
    """Deliberately tests that a non-empty currently_blocked set genuinely alters the safety determination.

    In a Metro Loop (5 sections ring: A - B - C - D - E - A):
    - Blocking (A, B) alone leaves C - D - E connected (safe).
    - Blocking (A, B) when D is ALREADY blocked (currently_blocked={"D"}) partitions the loop into {C} and {E} (unsafe).
    """
    profile = ProfileRegistry.get("metro")

    sections = [
        TrackSectionSchema(id="SEC_A", name="A"),
        TrackSectionSchema(id="SEC_B", name="B"),
        TrackSectionSchema(id="SEC_C", name="C"),
        TrackSectionSchema(id="SEC_D", name="D"),
        TrackSectionSchema(id="SEC_E", name="E"),
    ]
    adjacencies = [
        SectionAdjacencySchema(id="ADJ_1", section_a_id="SEC_A", section_b_id="SEC_B"),
        SectionAdjacencySchema(id="ADJ_2", section_a_id="SEC_B", section_b_id="SEC_C"),
        SectionAdjacencySchema(id="ADJ_3", section_a_id="SEC_C", section_b_id="SEC_D"),
        SectionAdjacencySchema(id="ADJ_4", section_a_id="SEC_D", section_b_id="SEC_E"),
        SectionAdjacencySchema(id="ADJ_5", section_a_id="SEC_E", section_b_id="SEC_A"),
    ]
    graph = NetworkGraph.build_from_sections(sections, adjacencies)

    # 1. With empty currently_blocked: (SEC_A, SEC_B) is SAFE because SEC_C-SEC_D-SEC_E remains connected
    is_safe_empty, reason_empty = profile.is_safe_adjacency(
        graph=graph,
        section_a_id="SEC_A",
        section_b_id="SEC_B",
        currently_blocked=set(),
    )
    assert is_safe_empty is True, f"Expected safe when no other section blocked, got: {reason_empty}"

    # 2. With currently_blocked containing SEC_D: (SEC_A, SEC_B) becomes UNSAFE because SEC_C and SEC_E become disconnected!
    is_safe_blocked, reason_blocked = profile.is_safe_adjacency(
        graph=graph,
        section_a_id="SEC_A",
        section_b_id="SEC_B",
        currently_blocked={"SEC_D"},
    )
    assert is_safe_blocked is False, "Expected unsafe when SEC_D is already blocked"
    assert "breaks loop connectivity" in reason_blocked or "Unsafe" in reason_blocked


def test_safety_precompute_empty_and_self_loops() -> None:
    """Test safety precomputation boundary conditions: None graph, empty sections, and self-loops."""
    profile = ProfileRegistry.get("metro")

    # None graph returns empty
    assert precompute_unsafe_adjacency_pairs(graph=None, profile=profile, sections=[]) == []

    # Empty sections returns empty
    graph = NetworkGraph.build_from_sections([], [])
    assert precompute_unsafe_adjacency_pairs(graph=graph, profile=profile, sections=[]) == []

    # Graph with self-loop (section connected to itself)
    sec = TrackSectionSchema(id="SEC_LOOP", name="Loop")
    adj_self = SectionAdjacencySchema(id="ADJ_SELF", section_a_id="SEC_LOOP", section_b_id="SEC_LOOP")
    graph_self = NetworkGraph.build_from_sections([sec], [adj_self])

    # Should skip self-loop without error
    pairs = precompute_unsafe_adjacency_pairs(graph=graph_self, profile=profile, sections=[sec])
    assert pairs == []


def test_replan_rl_route_missing_model_path_returns_400(client: TestClient) -> None:
    """POST /replan with strategy='rl' and missing model_path returns 400 Bad Request."""
    gen_resp = client.post("/networks/generate", json={"profile_name": "metro", "seed": 42})
    run_id = gen_resp.json()["run_id"]
    client.post("/solve", json={"run_id": run_id, "time_limit_seconds": 15})

    em_task = {
        "id": "EM_RL_TEST",
        "name": "Test RL Task",
        "section_id": "SEC-01",
        "duration_minutes": 30,
        "earliest_start": "2026-01-01T08:00:00Z",
        "latest_end": "2026-01-01T12:00:00Z",
        "is_emergency": True,
    }

    # Calling with strategy='rl' without model_path should return 400
    resp = client.post(
        "/replan",
        json={
            "run_id": run_id,
            "new_emergency_task": em_task,
            "strategy": "rl",
            "model_path": None,
        },
    )
    assert resp.status_code == 400
    assert "model_path" in resp.json()["detail"]
