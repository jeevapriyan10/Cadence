"""Comprehensive test suite for Echo historical decision memory and API routes."""

from datetime import datetime, timedelta, timezone
from typing import Generator
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from cadence.api.main import app
from cadence.api.schemas import HistoryResponseSchema, ReplanRequestSchema
from cadence.api.store import run_store
from cadence.domain.models import Base, DecisionRecord, NetworkRun
from cadence.domain.schemas import MaintenanceTaskSchema, ScheduledBlockSchema, TrackSectionSchema
from cadence.echo.store import (
    EchoStore,
    echo_store,
    get_latest_decision,
    get_run_history,
    get_warm_start_hints,
    record_decision,
    record_network_run,
)
from cadence.profiles.registry import ProfileRegistry
from cadence.solve.replan import ReplanResult, replan_warm_start
from cadence.solve.solver import SolveResult, solve_schedule


BASE_TEST_TIME = datetime(2026, 1, 1, 6, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def db_session() -> Generator[Session, None, None]:
    """Provide an in-memory SQLite database session for Echo unit tests."""
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


def test_record_network_run_and_decision_roundtrip(db_session: Session) -> None:
    """record_network_run followed by record_decision correctly persists and round-trips both records."""
    # 1. Record network run
    net_run = record_network_run(
        session=db_session,
        profile_name="metro",
        seed=42,
        section_count=10,
        train_count=15,
        task_count=8,
    )
    db_session.commit()

    assert net_run.id is not None
    assert len(net_run.id) > 0
    assert net_run.profile_name == "metro"
    assert net_run.seed == 42
    assert net_run.section_count == 10
    assert net_run.train_count == 15
    assert net_run.task_count == 8
    assert isinstance(net_run.created_at, datetime)

    # 2. Record decision
    block = ScheduledBlockSchema(
        task_id="TASK_01",
        section_id="SEC_A",
        start_time=BASE_TEST_TIME + timedelta(hours=1),
        end_time=BASE_TEST_TIME + timedelta(hours=2),
        method="full_resolve",
    )
    solve_res = SolveResult(
        status="OPTIMAL",
        scheduled_blocks=[block],
        objective_value=123.45,
        wall_time_seconds=0.456,
    )

    dec_record = record_decision(
        session=db_session,
        network_run_id=net_run.id,
        strategy="full_resolve",
        solve_or_replan_result=solve_res,
        is_replan=False,
    )
    db_session.commit()

    # 3. Round-trip verification from clean DB query
    queried_run = db_session.get(NetworkRun, net_run.id)
    assert queried_run is not None
    assert queried_run.id == net_run.id
    assert queried_run.profile_name == "metro"
    assert queried_run.seed == 42
    assert queried_run.section_count == 10

    queried_dec = db_session.query(DecisionRecord).filter_by(network_run_id=net_run.id).first()
    assert queried_dec is not None
    assert queried_dec.id == dec_record.id
    assert queried_dec.network_run_id == net_run.id
    assert queried_dec.strategy == "full_resolve"
    assert queried_dec.status == "OPTIMAL"
    assert queried_dec.objective_value == pytest.approx(123.45)
    assert queried_dec.wall_time_seconds == pytest.approx(0.456)
    assert queried_dec.is_replan is False
    assert queried_dec.rl_fallback_triggered is False
    assert isinstance(queried_dec.scheduled_blocks_snapshot, list)
    assert len(queried_dec.scheduled_blocks_snapshot) == 1
    assert queried_dec.scheduled_blocks_snapshot[0]["task_id"] == "TASK_01"


def test_get_run_history_chronological_order(db_session: Session) -> None:
    """get_run_history returns decisions in correct chronological order for a run with multiple recorded decisions."""
    net_run = record_network_run(
        session=db_session,
        profile_name="local",
        seed=10,
        section_count=5,
        train_count=4,
        task_count=2,
    )
    db_session.commit()

    block_1 = ScheduledBlockSchema(
        task_id="T1",
        section_id="S1",
        start_time=BASE_TEST_TIME + timedelta(hours=1),
        end_time=BASE_TEST_TIME + timedelta(hours=2),
        method="initial",
    )
    block_2 = ScheduledBlockSchema(
        task_id="T1",
        section_id="S1",
        start_time=BASE_TEST_TIME + timedelta(hours=2),
        end_time=BASE_TEST_TIME + timedelta(hours=3),
        method="replan_1",
    )
    block_3 = ScheduledBlockSchema(
        task_id="T1",
        section_id="S1",
        start_time=BASE_TEST_TIME + timedelta(hours=3),
        end_time=BASE_TEST_TIME + timedelta(hours=4),
        method="replan_2",
    )

    # 1. Initial solve
    initial_res = SolveResult(status="OPTIMAL", scheduled_blocks=[block_1], objective_value=10.0, wall_time_seconds=0.1)
    record_decision(db_session, net_run.id, strategy="full_resolve", solve_or_replan_result=initial_res, is_replan=False)
    db_session.commit()

    # 2. Replan 1 (warm_start)
    replan_1 = ReplanResult(strategy="warm_start", status="OPTIMAL", scheduled_blocks=[block_2], objective_value=12.0, wall_time_seconds=0.05)
    record_decision(db_session, net_run.id, strategy="warm_start", solve_or_replan_result=replan_1, is_replan=True)
    db_session.commit()

    # 3. Replan 2 (rl)
    replan_2 = ReplanResult(strategy="rl", status="OPTIMAL", scheduled_blocks=[block_3], objective_value=14.0, wall_time_seconds=0.02, rl_fallback_triggered=False)
    record_decision(db_session, net_run.id, strategy="rl", solve_or_replan_result=replan_2, is_replan=True)
    db_session.commit()

    # Query history
    history = get_run_history(db_session, net_run.id)
    assert len(history) == 3

    assert history[0].strategy == "full_resolve"
    assert history[0].is_replan is False
    assert history[0].objective_value == 10.0

    assert history[1].strategy == "warm_start"
    assert history[1].is_replan is True
    assert history[1].objective_value == 12.0

    assert history[2].strategy == "rl"
    assert history[2].is_replan is True
    assert history[2].objective_value == 14.0

    # Strict chronological order check
    assert history[0].created_at <= history[1].created_at <= history[2].created_at


def test_get_latest_decision_returns_most_recent(db_session: Session) -> None:
    """get_latest_decision correctly returns the most recent one, not the first."""
    net_run = record_network_run(
        session=db_session,
        profile_name="mainline",
        seed=99,
        section_count=8,
        train_count=6,
        task_count=3,
    )
    db_session.commit()

    # Initial solve
    record_decision(
        db_session,
        net_run.id,
        strategy="full_resolve",
        solve_or_replan_result=SolveResult(status="FEASIBLE", scheduled_blocks=[], objective_value=100.0, wall_time_seconds=0.5),
        is_replan=False,
    )
    db_session.commit()

    # Replan 1
    record_decision(
        db_session,
        net_run.id,
        strategy="warm_start",
        solve_or_replan_result=ReplanResult(strategy="warm_start", status="OPTIMAL", scheduled_blocks=[], objective_value=85.0, wall_time_seconds=0.2),
        is_replan=True,
    )
    db_session.commit()

    # Replan 2 (latest)
    record_decision(
        db_session,
        net_run.id,
        strategy="rl",
        solve_or_replan_result=ReplanResult(strategy="rl", status="OPTIMAL", scheduled_blocks=[], objective_value=80.0, wall_time_seconds=0.01),
        is_replan=True,
    )
    db_session.commit()

    latest = get_latest_decision(db_session, net_run.id)
    assert latest is not None
    assert latest.strategy == "rl"
    assert latest.objective_value == 80.0
    assert latest.is_replan is True


def test_get_warm_start_hints_integration_with_replan_warm_start(db_session: Session) -> None:
    """get_warm_start_hints reconstructs usable {task_id: start_time} dict that works in replan_warm_start."""
    profile = ProfileRegistry.get("mainline")
    section_1 = TrackSectionSchema(id="SEC_1", name="Track 1")
    section_2 = TrackSectionSchema(id="SEC_2", name="Track 2")

    existing_task = MaintenanceTaskSchema(
        id="TASK_BASE",
        name="Routine S1",
        section_id="SEC_1",
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

    # Persist in Echo
    net_run = record_network_run(
        session=db_session,
        profile_name="mainline",
        seed=42,
        section_count=2,
        train_count=0,
        task_count=1,
    )
    record_decision(
        session=db_session,
        network_run_id=net_run.id,
        strategy="full_resolve",
        solve_or_replan_result=prev_solve,
        is_replan=False,
    )
    db_session.commit()

    # Reconstruct warm-start hints via get_warm_start_hints
    hints = get_warm_start_hints(db_session, net_run.id)
    assert hints is not None
    assert "TASK_BASE" in hints
    assert isinstance(hints["TASK_BASE"], datetime)

    # Emergency task to be inserted
    emergency_task = MaintenanceTaskSchema(
        id="EMERGENCY_FAULT",
        name="Urgent Joint Defect",
        section_id="SEC_2",
        duration_minutes=60,
        earliest_start=BASE_TEST_TIME + timedelta(hours=2),
        latest_end=BASE_TEST_TIME + timedelta(hours=5),
        priority=10,
        is_emergency=True,
    )

    # Reconstruct prior solve result using the hints
    reconstructed_prior_solve = SolveResult(
        status="OPTIMAL",
        scheduled_blocks=[
            ScheduledBlockSchema(
                task_id=task_id,
                section_id="SEC_1",
                start_time=st_time,
                end_time=st_time + timedelta(minutes=60),
                method="warm_hinted",
            )
            for task_id, st_time in hints.items()
        ],
        objective_value=prev_solve.objective_value,
        wall_time_seconds=0.05,
    )

    # Feed into Module 11's replan_warm_start
    warm_result = replan_warm_start(
        previous_solve_result=reconstructed_prior_solve,
        previous_model_context={"base_time": BASE_TEST_TIME},
        new_emergency_task=emergency_task,
        all_existing_tasks=[existing_task],
        sections=[section_1, section_2],
        train_slots=[],
        profile=profile,
        time_horizon_minutes=720,
        base_time=BASE_TEST_TIME,
    )

    assert warm_result.strategy == "warm_start"
    assert warm_result.status in ("OPTIMAL", "FEASIBLE")
    assert any(b.task_id == "EMERGENCY_FAULT" for b in warm_result.scheduled_blocks)
    assert any(b.task_id == "TASK_BASE" for b in warm_result.scheduled_blocks)


def test_full_api_integration_generate_solve_replan_history(client: TestClient) -> None:
    """Full API integration test: POST /networks/generate -> POST /solve -> POST /replan -> GET /history/{run_id}.
    
    Verifies all three events are returned with correct strategy and is_replan values.
    """
    # 1. POST /networks/generate
    gen_payload = {
        "profile_name": "metro",
        "seed": 42,
        "section_count": 8,
        "train_count": 6,
        "task_count": 3,
    }
    gen_resp = client.post("/networks/generate", json=gen_payload)
    assert gen_resp.status_code == 201
    run_id = gen_resp.json()["run_id"]
    assert len(run_id) > 0

    # 2. POST /solve
    solve_resp = client.post("/solve", json={"run_id": run_id, "time_limit_seconds": 15})
    assert solve_resp.status_code == 200
    solve_data = solve_resp.json()
    assert solve_data["status"] in ("OPTIMAL", "FEASIBLE")

    # 3. POST /replan with full_resolve
    em_task_1 = {
        "id": "EMERGENCY_E1",
        "name": "Track Buckling Emergency",
        "section_id": "SEC_1",
        "duration_minutes": 60,
        "earliest_start": "2026-01-01T08:00:00Z",
        "latest_end": "2026-01-01T14:00:00Z",
        "priority": 10,
        "is_emergency": True,
    }
    replan_1_resp = client.post(
        "/replan",
        json={
            "run_id": run_id,
            "new_emergency_task": em_task_1,
            "strategy": "full_resolve",
            "time_limit_seconds": 15,
        },
    )
    assert replan_1_resp.status_code == 200
    replan_1_data = replan_1_resp.json()
    assert replan_1_data["strategy"] == "full_resolve"
    assert replan_1_data["status"] in ("OPTIMAL", "FEASIBLE")

    # 4. POST /replan with warm_start
    em_task_2 = {
        "id": "EMERGENCY_E2",
        "name": "Signal Failure Emergency",
        "section_id": "SEC_2",
        "duration_minutes": 45,
        "earliest_start": "2026-01-01T10:00:00Z",
        "latest_end": "2026-01-01T16:00:00Z",
        "priority": 9,
        "is_emergency": True,
    }
    replan_2_resp = client.post(
        "/replan",
        json={
            "run_id": run_id,
            "new_emergency_task": em_task_2,
            "strategy": "warm_start",
            "time_limit_seconds": 15,
        },
    )
    assert replan_2_resp.status_code == 200
    replan_2_data = replan_2_resp.json()
    assert replan_2_data["strategy"] == "warm_start"
    assert replan_2_data["status"] in ("OPTIMAL", "FEASIBLE")

    # 5. GET /history/{run_id}
    hist_resp = client.get(f"/history/{run_id}")
    assert hist_resp.status_code == 200
    history = hist_resp.json()
    assert isinstance(history, list)
    assert len(history) == 3

    # Event 0: Initial solve
    assert history[0]["strategy"] == "full_resolve"
    assert history[0]["status"] in ("OPTIMAL", "FEASIBLE")
    assert history[0]["is_replan"] is False
    assert "wall_time_seconds" in history[0]
    assert "created_at" in history[0]

    # Event 1: First replan (full_resolve)
    assert history[1]["strategy"] == "full_resolve"
    assert history[1]["status"] in ("OPTIMAL", "FEASIBLE")
    assert history[1]["is_replan"] is True

    # Event 2: Second replan (warm_start)
    assert history[2]["strategy"] == "warm_start"
    assert history[2]["status"] in ("OPTIMAL", "FEASIBLE")
    assert history[2]["is_replan"] is True


def test_echo_store_class_and_empty_edge_cases(db_session: Session) -> None:
    """Verify EchoStore class wrappers and empty/nonexistent edge cases."""
    store = EchoStore()

    # Empty cases
    assert store.get_run_history(db_session, "nonexistent-id") == []
    assert store.get_latest_decision(db_session, "nonexistent-id") is None
    assert store.get_warm_start_hints(db_session, "nonexistent-id") is None

    # Persist via class instance
    run = store.record_network_run(db_session, "metro", 42, 5, 5, 2)
    db_session.commit()
    assert run.id is not None

    dec = store.record_decision(
        db_session,
        network_run_id=run.id,
        strategy="full_resolve",
        solve_or_replan_result={"status": "OPTIMAL", "scheduled_blocks": [], "objective_value": 5.0, "wall_time_seconds": 0.01},
        is_replan=False,
    )
    db_session.commit()
    assert dec.id is not None

    history = store.get_run_history(db_session, run.id)
    assert len(history) == 1
    assert history[0].id == dec.id

    latest = store.get_latest_decision(db_session, run.id)
    assert latest is not None
    assert latest.id == dec.id


def test_replan_api_missing_run_or_decision(client: TestClient) -> None:
    """POST /replan returns 404 for missing run and 400 if /solve has not run yet."""
    em_task = {
        "id": "EM_1",
        "name": "Fault",
        "section_id": "SEC_1",
        "duration_minutes": 30,
        "earliest_start": "2026-01-01T08:00:00Z",
        "latest_end": "2026-01-01T12:00:00Z",
        "is_emergency": True,
    }

    # 404 on nonexistent run
    resp_404 = client.post("/replan", json={"run_id": "nonexistent-xyz", "new_emergency_task": em_task, "strategy": "warm_start"})
    assert resp_404.status_code == 404

    # Generate but don't solve -> 400
    gen_resp = client.post("/networks/generate", json={"profile_name": "metro", "seed": 42})
    run_id = gen_resp.json()["run_id"]

    resp_400 = client.post("/replan", json={"run_id": run_id, "new_emergency_task": em_task, "strategy": "warm_start"})
    assert resp_400.status_code == 400
    assert "No prior decision found" in resp_400.json()["detail"]


def test_history_api_not_found(client: TestClient) -> None:
    """GET /history/{nonexistent_id} returns 404."""
    resp = client.get("/history/nonexistent-uuid-12345")
    assert resp.status_code == 404
    assert "detail" in resp.json()
