"""Echo persistence store for historical network runs and decision memory."""

from datetime import datetime, timezone
from typing import Any, Optional, Union

from sqlalchemy import select
from sqlalchemy.orm import Session

from cadence.domain.models import DecisionRecord, NetworkRun, generate_uuid, utc_now
from cadence.domain.schemas import ScheduledBlockSchema
from cadence.solve.replan import ReplanResult
from cadence.solve.solver import SolveResult


def serialize_scheduled_blocks(
    scheduled_blocks: Any,
) -> list[dict[str, Any]]:
    """Serialize scheduled blocks into JSON-compatible dictionaries."""
    snapshot: list[dict[str, Any]] = []
    if not scheduled_blocks:
        return snapshot

    for b in scheduled_blocks:
        if isinstance(b, dict):
            d = dict(b)
            for k in ("start_time", "end_time", "created_at"):
                if isinstance(d.get(k), datetime):
                    d[k] = d[k].isoformat()
            snapshot.append(d)
        elif hasattr(b, "model_dump"):
            snapshot.append(b.model_dump(mode="json"))
        elif hasattr(b, "__dict__"):
            d = {k: v for k, v in b.__dict__.items() if not k.startswith("_")}
            for k in ("start_time", "end_time", "created_at"):
                if isinstance(d.get(k), datetime):
                    d[k] = d[k].isoformat()
            snapshot.append(d)
        else:
            snapshot.append(dict(b))
    return snapshot


def record_network_run(
    session: Session,
    profile_name: str,
    seed: int,
    section_count: int,
    train_count: int,
    task_count: int,
    run_id: Optional[str] = None,
) -> NetworkRun:
    """Persist a newly generated synthetic network run record.

    Args:
        session: Active SQLAlchemy Session.
        profile_name: Name of the profile used (e.g. 'metro', 'local', 'mainline').
        seed: Random seed used for deterministic generation.
        section_count: Number of track sections in the network.
        train_count: Number of train slots in the network.
        task_count: Number of maintenance tasks in the network.
        run_id: Optional predefined run UUID. If None, a new UUID is generated.

    Returns:
        NetworkRun: The persisted NetworkRun ORM instance.
    """
    allocated_id = run_id or generate_uuid()
    network_run = NetworkRun(
        id=allocated_id,
        profile_name=profile_name,
        seed=seed,
        section_count=section_count,
        train_count=train_count,
        task_count=task_count,
        created_at=utc_now(),
    )
    session.add(network_run)
    session.flush()
    return network_run


def record_decision(
    session: Session,
    network_run_id: str,
    strategy: Union[str, Any],
    solve_or_replan_result: Union[SolveResult, ReplanResult, dict[str, Any]],
    is_replan: bool = False,
) -> DecisionRecord:
    """Persist a solve or replan decision record.

    Handles both SolveResult and ReplanResult shapes (or dictionary equivalents).

    Args:
        session: Active SQLAlchemy Session.
        network_run_id: Identifier of the parent NetworkRun.
        strategy: Strategy string (e.g. 'full_resolve', 'warm_start', 'rl') or ReplanStrategy enum.
        solve_or_replan_result: The result object from solve_schedule or a replan function.
        is_replan: False for initial solve decisions, True for replanning events.

    Returns:
        DecisionRecord: The persisted DecisionRecord ORM instance.
    """
    strat_str = strategy.value if hasattr(strategy, "value") else str(strategy)

    status = getattr(solve_or_replan_result, "status", None)
    if status is None and isinstance(solve_or_replan_result, dict):
        status = solve_or_replan_result.get("status", "UNKNOWN")
    status_str = str(status or "UNKNOWN")

    obj_val = getattr(solve_or_replan_result, "objective_value", None)
    if obj_val is None and isinstance(solve_or_replan_result, dict):
        obj_val = solve_or_replan_result.get("objective_value")
    if obj_val is not None:
        obj_val = float(obj_val)

    wall_time = getattr(solve_or_replan_result, "wall_time_seconds", None)
    if wall_time is None and isinstance(solve_or_replan_result, dict):
        wall_time = solve_or_replan_result.get("wall_time_seconds", 0.0)
    wall_time = float(wall_time or 0.0)

    rl_fallback = getattr(solve_or_replan_result, "rl_fallback_triggered", None)
    if rl_fallback is None and isinstance(solve_or_replan_result, dict):
        rl_fallback = solve_or_replan_result.get("rl_fallback_triggered", False)
    rl_fallback = bool(rl_fallback or False)

    blocks = getattr(solve_or_replan_result, "scheduled_blocks", None)
    if blocks is None and isinstance(solve_or_replan_result, dict):
        blocks = solve_or_replan_result.get("scheduled_blocks", [])

    snapshot = serialize_scheduled_blocks(blocks)

    decision = DecisionRecord(
        network_run_id=network_run_id,
        strategy=strat_str,
        status=status_str,
        objective_value=obj_val,
        wall_time_seconds=wall_time,
        rl_fallback_triggered=rl_fallback,
        scheduled_blocks_snapshot=snapshot,
        is_replan=is_replan,
        created_at=utc_now(),
    )
    session.add(decision)
    session.flush()
    return decision


def get_run_history(
    session: Session,
    network_run_id: str,
) -> list[DecisionRecord]:
    """Retrieve all decisions for a given run, ordered chronologically by created_at.

    Args:
        session: Active SQLAlchemy Session.
        network_run_id: Unique identifier of the network run.

    Returns:
        list[DecisionRecord]: List of decision records ordered by created_at ascending.
    """
    stmt = (
        select(DecisionRecord)
        .where(DecisionRecord.network_run_id == network_run_id)
        .order_by(DecisionRecord.created_at.asc(), DecisionRecord.id.asc())
    )
    return list(session.scalars(stmt).all())


def get_latest_decision(
    session: Session,
    network_run_id: str,
) -> Optional[DecisionRecord]:
    """Retrieve the most recent decision record for a given run.

    Args:
        session: Active SQLAlchemy Session.
        network_run_id: Unique identifier of the network run.

    Returns:
        Optional[DecisionRecord]: Most recent decision record or None.
    """
    stmt = (
        select(DecisionRecord)
        .where(DecisionRecord.network_run_id == network_run_id)
        .order_by(DecisionRecord.created_at.desc(), DecisionRecord.id.desc())
        .limit(1)
    )
    return session.scalars(stmt).first()


def get_warm_start_hints(
    session: Session,
    network_run_id: str,
) -> Optional[dict[str, datetime]]:
    """Reconstruct {task_id: start_time} mapping from the latest decision's scheduled_blocks_snapshot.

    Returns the mapping in the shape Module 11's replan_warm_start needs for AddHint.

    Args:
        session: Active SQLAlchemy Session.
        network_run_id: Unique identifier of the network run.

    Returns:
        Optional[dict[str, datetime]]: Dictionary mapping task_id to scheduled start_time,
            or None if no decision exists for the given run_id.
    """
    latest = get_latest_decision(session, network_run_id)
    if latest is None:
        return None

    hints: dict[str, datetime] = {}
    for block_data in latest.scheduled_blocks_snapshot:
        tid = block_data.get("task_id") if isinstance(block_data, dict) else getattr(block_data, "task_id", None)
        st = block_data.get("start_time") if isinstance(block_data, dict) else getattr(block_data, "start_time", None)
        if not tid or st is None:
            continue

        if isinstance(st, str):
            dt = datetime.fromisoformat(st)
        elif isinstance(st, datetime):
            dt = st
        else:
            continue

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        hints[tid] = dt

    return hints


class EchoStore:
    """Store interface wrapping historical network runs and decision records."""

    def record_network_run(
        self,
        session: Session,
        profile_name: str,
        seed: int,
        section_count: int,
        train_count: int,
        task_count: int,
        run_id: Optional[str] = None,
    ) -> NetworkRun:
        return record_network_run(
            session=session,
            profile_name=profile_name,
            seed=seed,
            section_count=section_count,
            train_count=train_count,
            task_count=task_count,
            run_id=run_id,
        )

    def record_decision(
        self,
        session: Session,
        network_run_id: str,
        strategy: Union[str, Any],
        solve_or_replan_result: Union[SolveResult, ReplanResult, dict[str, Any]],
        is_replan: bool = False,
    ) -> DecisionRecord:
        return record_decision(
            session=session,
            network_run_id=network_run_id,
            strategy=strategy,
            solve_or_replan_result=solve_or_replan_result,
            is_replan=is_replan,
        )

    def get_run_history(
        self,
        session: Session,
        network_run_id: str,
    ) -> list[DecisionRecord]:
        return get_run_history(session=session, network_run_id=network_run_id)

    def get_latest_decision(
        self,
        session: Session,
        network_run_id: str,
    ) -> Optional[DecisionRecord]:
        return get_latest_decision(session=session, network_run_id=network_run_id)

    def get_warm_start_hints(
        self,
        session: Session,
        network_run_id: str,
    ) -> Optional[dict[str, datetime]]:
        return get_warm_start_hints(session=session, network_run_id=network_run_id)


echo_store = EchoStore()
