"""Dynamic re-planning support for railway maintenance scheduling."""

from datetime import datetime, timedelta, timezone
from enum import Enum
import time
from typing import Any, Literal, Optional, Union

from ortools.sat.python import cp_model
from pydantic import BaseModel, ConfigDict, Field

from cadence.domain.graph import NetworkGraph
from cadence.domain.models import MaintenanceTask, SectionAdjacency, TrackSection, TrainSlot
from cadence.domain.schemas import (
    MaintenanceTaskSchema,
    ScheduledBlockSchema,
    SectionAdjacencySchema,
    TrackSectionSchema,
    TrainSlotSchema,
)
from cadence.profiles.base import NetworkProfile
from cadence.solve.model import DEFAULT_BASE_TIME, DEFAULT_SCALE_FACTOR, build_cp_model
from cadence.solve.solver import SolveResult, solve_schedule


class ReplanStrategy(str, Enum):
    """Enumeration of supported re-planning strategies."""

    FULL_RESOLVE = "full_resolve"
    WARM_START = "warm_start"


ReplanStrategyType = Literal["full_resolve", "warm_start"]


class ReplanResult(BaseModel):
    """Result of a dynamic re-planning operation."""

    strategy: str
    status: str
    scheduled_blocks: list[ScheduledBlockSchema] = Field(default_factory=list)
    objective_value: Optional[float] = None
    wall_time_seconds: float = 0.0
    previous_objective_value: Optional[float] = None
    tasks_changed: list[str] = Field(default_factory=list)

    model_config = ConfigDict(arbitrary_types_allowed=True)


def compute_tasks_changed(
    previous_blocks: list[Union[ScheduledBlockSchema, Any]],
    new_blocks: list[Union[ScheduledBlockSchema, Any]],
    tolerance_minutes: float = 1.0,
) -> list[str]:
    """Diff scheduled start times between previous and new solutions for matching tasks.

    Compares start times for tasks present in both the previous and newly replanned schedules.
    Returns the task IDs whose start time changed by more than tolerance_minutes.

    Args:
        previous_blocks: List of ScheduledBlockSchema from the previous schedule.
        new_blocks: List of ScheduledBlockSchema from the newly replanned schedule.
        tolerance_minutes: Minimum start time difference in minutes to be considered changed (default 1.0).

    Returns:
        list[str]: Sorted list of task IDs whose scheduled start time changed by more than tolerance_minutes.
    """
    prev_starts: dict[str, datetime] = {}
    for b in previous_blocks:
        tid = b.task_id if hasattr(b, "task_id") else b["task_id"]
        st = b.start_time if hasattr(b, "start_time") else b["start_time"]
        prev_starts[tid] = st

    new_starts: dict[str, datetime] = {}
    for b in new_blocks:
        tid = b.task_id if hasattr(b, "task_id") else b["task_id"]
        st = b.start_time if hasattr(b, "start_time") else b["start_time"]
        new_starts[tid] = st

    changed: list[str] = []
    for task_id, prev_start in prev_starts.items():
        if task_id in new_starts:
            new_start = new_starts[task_id]
            diff_mins = abs((new_start - prev_start).total_seconds()) / 60.0
            if diff_mins > tolerance_minutes:
                changed.append(task_id)

    return sorted(changed)


def replan_full_resolve(
    previous_solve_result: SolveResult,
    new_emergency_task: MaintenanceTaskSchema,
    all_existing_tasks: list[Union[MaintenanceTaskSchema, MaintenanceTask]],
    sections: list[Union[TrackSectionSchema, TrackSection]],
    train_slots: list[Union[TrainSlotSchema, TrainSlot]],
    profile: NetworkProfile,
    time_horizon_minutes: int = 1440,
    time_limit_seconds: int = 30,
    base_time: Optional[datetime] = None,
    graph: Optional[NetworkGraph] = None,
    adjacencies: Optional[list[Union[SectionAdjacencySchema, SectionAdjacency]]] = None,
    **kwargs: Any,
) -> ReplanResult:
    """Perform dynamic replanning by executing a full resolve from scratch.

    Appends the new emergency task to the existing task set and invokes solve_schedule()
    fresh without any solution hints.

    Args:
        previous_solve_result: The prior SolveResult before emergency task insertion.
        new_emergency_task: The emergency MaintenanceTask to schedule.
        all_existing_tasks: Pre-existing maintenance tasks in the schedule.
        sections: Track sections in the network.
        train_slots: Scheduled train slots traversing the network.
        profile: NetworkProfile governing priority and disruption constraints.
        time_horizon_minutes: Maximum scheduling horizon in integer minutes (default 1440).
        time_limit_seconds: Maximum wall time allowed for the solver (default 30).
        base_time: Reference datetime for minute 0.
        graph: Optional NetworkGraph representation of topology.
        adjacencies: Optional list of SectionAdjacency connections.
        **kwargs: Additional parameters passed to solve_schedule.

    Returns:
        ReplanResult: Re-planning outcome containing new blocks, metrics, and tasks_changed.
    """
    em_id = new_emergency_task.id if hasattr(new_emergency_task, "id") else new_emergency_task["id"]
    combined_tasks = [t for t in all_existing_tasks if (t.id if hasattr(t, "id") else t["id"]) != em_id]
    combined_tasks.append(new_emergency_task)

    solve_result = solve_schedule(
        sections=sections,
        tasks=combined_tasks,
        train_slots=train_slots,
        profile=profile,
        time_horizon_minutes=time_horizon_minutes,
        time_limit_seconds=time_limit_seconds,
        base_time=base_time,
        graph=graph,
        adjacencies=adjacencies,
        **kwargs,
    )

    tasks_changed = compute_tasks_changed(
        previous_blocks=previous_solve_result.scheduled_blocks,
        new_blocks=solve_result.scheduled_blocks,
    )

    return ReplanResult(
        strategy=ReplanStrategy.FULL_RESOLVE.value,
        status=solve_result.status,
        scheduled_blocks=solve_result.scheduled_blocks,
        objective_value=solve_result.objective_value,
        wall_time_seconds=solve_result.wall_time_seconds,
        previous_objective_value=previous_solve_result.objective_value,
        tasks_changed=tasks_changed,
    )


def replan_warm_start(
    previous_solve_result: SolveResult,
    previous_model_context: dict,
    new_emergency_task: MaintenanceTaskSchema,
    all_existing_tasks: list[Union[MaintenanceTaskSchema, MaintenanceTask]],
    sections: list[Union[TrackSectionSchema, TrackSection]],
    train_slots: list[Union[TrainSlotSchema, TrainSlot]],
    profile: NetworkProfile,
    time_horizon_minutes: int = 1440,
    time_limit_seconds: int = 30,
    base_time: Optional[datetime] = None,
    graph: Optional[NetworkGraph] = None,
    adjacencies: Optional[list[Union[SectionAdjacencySchema, SectionAdjacency]]] = None,
    **kwargs: Any,
) -> ReplanResult:
    """Perform dynamic replanning by warm-starting CP-SAT with previous schedule hints.

    Builds a new CP-SAT model incorporating the emergency task, hints the start variables
    of all pre-existing tasks using their actual scheduled start times from previous_solve_result,
    and solves.

    Args:
        previous_solve_result: The prior SolveResult before emergency task insertion.
        previous_model_context: Context dictionary or BuildModelResult from prior model build.
        new_emergency_task: The emergency MaintenanceTask to schedule.
        all_existing_tasks: Pre-existing maintenance tasks in the schedule.
        sections: Track sections in the network.
        train_slots: Scheduled train slots traversing the network.
        profile: NetworkProfile governing priority and disruption constraints.
        time_horizon_minutes: Maximum scheduling horizon in integer minutes (default 1440).
        time_limit_seconds: Maximum wall time allowed for the solver (default 30).
        base_time: Reference datetime for minute 0.
        graph: Optional NetworkGraph representation of topology.
        adjacencies: Optional list of SectionAdjacency connections.
        **kwargs: Additional parameters passed to solver/builder.

    Returns:
        ReplanResult: Re-planning outcome containing warm-started blocks, metrics, and tasks_changed.
    """
    em_id = new_emergency_task.id if hasattr(new_emergency_task, "id") else new_emergency_task["id"]
    combined_tasks = [t for t in all_existing_tasks if (t.id if hasattr(t, "id") else t["id"]) != em_id]
    combined_tasks.append(new_emergency_task)

    # Determine reference base datetime for integer minute conversion
    if base_time is not None:
        ref_time = base_time
    elif previous_model_context and previous_model_context.get("base_time") is not None:
        ref_time = previous_model_context["base_time"]
    else:
        earliest_task_dt = min(
            (t.earliest_start for t in combined_tasks if hasattr(t, "earliest_start") and t.earliest_start),
            default=None,
        )
        earliest_train_dt = min(
            (s.scheduled_start for s in train_slots if hasattr(s, "scheduled_start") and s.scheduled_start),
            default=None,
        )
        candidates = [dt for dt in (earliest_task_dt, earliest_train_dt) if dt is not None]
        ref_time = min(candidates) if candidates else DEFAULT_BASE_TIME

    # Build CP-SAT model with combined tasks including the emergency task
    build_output = build_cp_model(
        sections=sections,
        tasks=combined_tasks,
        train_slots=train_slots,
        profile=profile,
        time_horizon_minutes=time_horizon_minutes,
        base_time=ref_time,
        graph=graph,
        adjacencies=adjacencies,
    )
    model = build_output["model"]
    interval_vars = build_output["interval_vars"]

    # Inject warm-start hints for all tasks that existed in previous solution
    for block in previous_solve_result.scheduled_blocks:
        tid = block.task_id if hasattr(block, "task_id") else block["task_id"]
        if tid in interval_vars:
            iv = interval_vars[tid]
            start_expr = iv.StartExpr()
            b_start = block.start_time if hasattr(block, "start_time") else block["start_time"]
            prev_start_val = int(round((b_start - ref_time).total_seconds() / 60))
            if 0 <= prev_start_val <= time_horizon_minutes:
                model.AddHint(start_expr, prev_start_val)

    # Configure CP-SAT solver
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = float(time_limit_seconds)

    t0 = time.perf_counter()
    status_code = solver.Solve(model)
    t1 = time.perf_counter()

    measured_wall_time = t1 - t0
    solver_wall_time = solver.WallTime()
    wall_time_seconds = max(0.001, solver_wall_time if solver_wall_time > 0.0 else measured_wall_time)

    status_map = {
        cp_model.OPTIMAL: "OPTIMAL",
        cp_model.FEASIBLE: "FEASIBLE",
        cp_model.INFEASIBLE: "INFEASIBLE",
        cp_model.MODEL_INVALID: "INFEASIBLE",
        cp_model.UNKNOWN: "UNKNOWN",
    }
    status_str = status_map.get(status_code, "UNKNOWN")

    scheduled_blocks: list[ScheduledBlockSchema] = []
    objective_value: Optional[float] = None

    if status_str in ("OPTIMAL", "FEASIBLE"):
        task_lookup = {
            (t.id if hasattr(t, "id") else t["id"]): t for t in combined_tasks
        }

        for task_id, iv in interval_vars.items():
            start_min = solver.Value(iv.StartExpr())
            end_min = solver.Value(iv.EndExpr())

            start_dt = ref_time + timedelta(minutes=start_min)
            end_dt = ref_time + timedelta(minutes=end_min)

            task_obj = task_lookup[task_id]
            sec_id = task_obj.section_id if hasattr(task_obj, "section_id") else task_obj["section_id"]

            block = ScheduledBlockSchema(
                task_id=task_id,
                section_id=sec_id,
                start_time=start_dt,
                end_time=end_dt,
                method="warm_start",
            )
            scheduled_blocks.append(block)

        # Sort blocks by start time for consistent readability
        scheduled_blocks.sort(key=lambda b: (b.start_time, b.section_id, b.task_id))
        objective_value = float(solver.ObjectiveValue()) / float(DEFAULT_SCALE_FACTOR)

    tasks_changed = compute_tasks_changed(
        previous_blocks=previous_solve_result.scheduled_blocks,
        new_blocks=scheduled_blocks,
    )

    return ReplanResult(
        strategy=ReplanStrategy.WARM_START.value,
        status=status_str,
        scheduled_blocks=scheduled_blocks,
        objective_value=objective_value,
        wall_time_seconds=wall_time_seconds,
        previous_objective_value=previous_solve_result.objective_value,
        tasks_changed=tasks_changed,
    )
