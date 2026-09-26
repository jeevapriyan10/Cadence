"""Policy-driven fast re-planning using trained reinforcement learning models."""

from datetime import datetime, timezone
import logging
import os
from pathlib import Path
import time
from typing import Any, Optional, Union

import numpy as np
from stable_baselines3 import PPO

from cadence.domain.graph import NetworkGraph
from cadence.domain.models import MaintenanceTask, SectionAdjacency, TrackSection, TrainSlot
from cadence.domain.schemas import (
    MaintenanceTaskSchema,
    ScheduledBlockSchema,
    SectionAdjacencySchema,
    TrackSectionSchema,
    TrainSlotSchema,
)
from cadence.flux.env import DEFAULT_HORIZON_MINUTES, generate_candidate_windows
from cadence.profiles.base import NetworkProfile
from cadence.solve.model import DEFAULT_BASE_TIME, DEFAULT_SCALE_FACTOR
from cadence.solve.replan import ReplanResult, compute_tasks_changed, replan_warm_start
from cadence.solve.safety import count_safety_violations
from cadence.solve.solver import SolveResult

logger = logging.getLogger(__name__)

# Global cache for loaded SB3 models: path -> PPO instance
_MODEL_CACHE: dict[str, PPO] = {}


def clear_model_cache() -> None:
    """Clear cached PPO models."""
    _MODEL_CACHE.clear()


def construct_scenario_observation(
    emergency_task: MaintenanceTaskSchema,
    base_scheduled_blocks: list[Union[ScheduledBlockSchema, Any]],
    unsafe_adjacency_pairs: list[tuple[str, str, str]],
    candidate_windows: list[tuple[datetime, datetime]],
    base_time: datetime,
    time_horizon_minutes: int = 1440,
) -> np.ndarray:
    """Construct observation vector matching CadenceReplanEnv for a specific scenario.

    Encodes normalized emergency task duration, priority, earliest start and latest end bounds,
    section occupancy context, blocked unsafe neighbor sections, and per-slot occupancy indicators.

    Args:
        emergency_task: The emergency MaintenanceTask to schedule.
        base_scheduled_blocks: Pre-existing scheduled possession blocks.
        unsafe_adjacency_pairs: Flagged unsafe adjacent section pairs.
        candidate_windows: List of candidate (start, end) datetimes.
        base_time: Scenario reference datetime.
        time_horizon_minutes: Planning horizon in minutes (default 1440).

    Returns:
        np.ndarray: Continuous float32 observation vector.
    """
    duration_norm = float(emergency_task.duration_minutes) / float(time_horizon_minutes)
    priority_norm = float(emergency_task.priority) / 10.0

    es_offset_min = (emergency_task.earliest_start - base_time).total_seconds() / 60.0
    le_offset_min = (emergency_task.latest_end - base_time).total_seconds() / 60.0
    window_start_norm = float(np.clip(es_offset_min / float(time_horizon_minutes), 0.0, 1.0))
    window_end_norm = float(np.clip(le_offset_min / float(time_horizon_minutes), 0.0, 1.0))

    total_base_blocks = max(1, len(base_scheduled_blocks))
    em_sec = emergency_task.section_id
    target_sec_blocks = sum(
        1
        for b in base_scheduled_blocks
        if (b.section_id if hasattr(b, "section_id") else b["section_id"]) == em_sec
    )
    target_sec_task_count_norm = float(np.clip(float(target_sec_blocks) / float(total_base_blocks), 0.0, 1.0))

    unsafe_neighbor_sections = {
        pair[1] if pair[0] == em_sec else pair[0]
        for pair in unsafe_adjacency_pairs
        if em_sec in (pair[0], pair[1])
    }
    unsafe_neighbor_blocks = sum(
        1
        for b in base_scheduled_blocks
        if (b.section_id if hasattr(b, "section_id") else b["section_id"]) in unsafe_neighbor_sections
    )
    unsafe_neighbors_blocked_norm = float(
        np.clip(float(unsafe_neighbor_blocks) / float(total_base_blocks), 0.0, 1.0)
    )

    slot_occupancy: list[float] = []
    for c_start, c_end in candidate_windows:
        overlapping_count = sum(
            1
            for b in base_scheduled_blocks
            if (
                ((b.section_id if hasattr(b, "section_id") else b["section_id"]) == em_sec)
                or ((b.section_id if hasattr(b, "section_id") else b["section_id"]) in unsafe_neighbor_sections)
            )
            and max(b.start_time if hasattr(b, "start_time") else b["start_time"], c_start)
            < min(b.end_time if hasattr(b, "end_time") else b["end_time"], c_end)
        )
        slot_occupancy.append(float(np.clip(float(overlapping_count) / 5.0, 0.0, 1.0)))

    features = [
        float(np.clip(duration_norm, 0.0, 1.0)),
        float(np.clip(priority_norm, 0.0, 1.0)),
        window_start_norm,
        window_end_norm,
        target_sec_task_count_norm,
        unsafe_neighbors_blocked_norm,
    ] + slot_occupancy

    return np.array(features, dtype=np.float32)


def compute_schedule_objective(
    scheduled_blocks: list[Union[ScheduledBlockSchema, Any]],
    tasks: list[Union[MaintenanceTaskSchema, MaintenanceTask]],
    train_slots: list[Union[TrainSlotSchema, TrainSlot]],
    profile: NetworkProfile,
) -> float:
    """Compute exact CP-SAT objective value for a given schedule assignment.

    Mirrors build_cp_model's objective function:
    sum(priority_weight * delay) + sum(disruption_penalty * overlap).

    Args:
        scheduled_blocks: Scheduled blocks in the schedule.
        tasks: All maintenance tasks included in the schedule.
        train_slots: Timetable train slots.
        profile: Active NetworkProfile.

    Returns:
        float: Normalized objective value (scaled by DEFAULT_SCALE_FACTOR).
    """
    task_map = {
        (t.id if hasattr(t, "id") else t["id"]): t for t in tasks
    }
    block_map = {
        (b.task_id if hasattr(b, "task_id") else b["task_id"]): b for b in scheduled_blocks
    }
    total_scaled_cost = 0

    # 1. Delay terms: priority_weight * delay_minutes
    for tid, task in task_map.items():
        if tid in block_map:
            block = block_map[tid]
            weight = profile.priority_weight(task)
            scaled_weight = max(1, int(round(weight * DEFAULT_SCALE_FACTOR)))
            es = task.earliest_start if hasattr(task, "earliest_start") else task["earliest_start"]
            b_start = block.start_time if hasattr(block, "start_time") else block["start_time"]
            delay_min = max(0, int(round((b_start - es).total_seconds() / 60.0)))
            total_scaled_cost += scaled_weight * delay_min

    # 2. Disruption terms for train slots traversing blocked sections
    for slot in train_slots:
        slot_schema = slot if isinstance(slot, TrainSlotSchema) else TrainSlotSchema.model_validate(slot)
        slot_route_set = set(slot_schema.route)
        penalty = profile.disruption_penalty(slot_schema)
        scaled_penalty = max(1, int(round(penalty * DEFAULT_SCALE_FACTOR)))

        for block in scheduled_blocks:
            b_sec = block.section_id if hasattr(block, "section_id") else block["section_id"]
            if b_sec in slot_route_set:
                b_start = block.start_time if hasattr(block, "start_time") else block["start_time"]
                b_end = block.end_time if hasattr(block, "end_time") else block["end_time"]
                if max(b_start, slot_schema.scheduled_start) < min(b_end, slot_schema.scheduled_end):
                    total_scaled_cost += scaled_penalty

    return float(total_scaled_cost) / float(DEFAULT_SCALE_FACTOR)


def replan_rl(
    model_path: str,
    previous_solve_result: SolveResult,
    new_emergency_task: MaintenanceTaskSchema,
    all_existing_tasks: list[Union[MaintenanceTaskSchema, MaintenanceTask]],
    sections: list[Union[TrackSectionSchema, TrackSection]],
    train_slots: list[Union[TrainSlotSchema, TrainSlot]],
    profile: NetworkProfile,
    unsafe_adjacency_pairs: list[tuple[str, str, str]],
    time_horizon_minutes: int = 1440,
    base_time: Optional[datetime] = None,
    graph: Optional[NetworkGraph] = None,
    adjacencies: Optional[list[Union[SectionAdjacencySchema, SectionAdjacency]]] = None,
    previous_model_context: Optional[dict] = None,
    time_limit_seconds: int = 30,
    candidate_slots: int = 10,
    **kwargs: Any,
) -> ReplanResult:
    """Perform dynamic emergency re-planning via trained reinforcement learning policy.

    Executes sub-second neural inference followed by a strict hard-constraint re-validation gate.
    If the RL-proposed placement violates Module 5 safety-adjacency constraints or causes
    a same-section overlap, the re-validation gate rejects it and automatically falls back to
    replan_warm_start, tagging the result with rl_fallback_triggered=True.

    Args:
        model_path: Path to the trained SB3 PPO model file (.zip).
        previous_solve_result: The prior SolveResult before emergency task insertion.
        new_emergency_task: The emergency MaintenanceTask to schedule.
        all_existing_tasks: Pre-existing maintenance tasks in the schedule.
        sections: Track sections in the network.
        train_slots: Scheduled train slots traversing the network.
        profile: NetworkProfile governing priority and disruption constraints.
        unsafe_adjacency_pairs: Precomputed (sec_a, sec_b, reason) flagged unsafe pairs.
        time_horizon_minutes: Scheduling horizon in integer minutes (default 1440).
        base_time: Reference base datetime for scheduling window.
        graph: Optional NetworkGraph representation of topology.
        adjacencies: Optional list of SectionAdjacency connections.
        previous_model_context: Optional model context from prior build_cp_model.
        time_limit_seconds: Solver time limit if warm-start fallback is triggered.
        candidate_slots: Default number of candidate discrete time slots (default 10).
        **kwargs: Additional parameters passed to fallback solvers.

    Returns:
        ReplanResult: Re-planning outcome with strategy="rl", wall_time_seconds, and
            rl_fallback_triggered flag.
    """
    # 1. Load model with caching
    resolved_path = str(Path(model_path).resolve())
    if resolved_path not in _MODEL_CACHE:
        _MODEL_CACHE[resolved_path] = PPO.load(model_path)
    model = _MODEL_CACHE[resolved_path]

    # Determine reference base time
    if base_time is not None:
        ref_time = base_time
    elif previous_model_context and previous_model_context.get("base_time") is not None:
        ref_time = previous_model_context["base_time"]
    else:
        earliest_task_dt = min(
            (t.earliest_start for t in all_existing_tasks if hasattr(t, "earliest_start") and t.earliest_start),
            default=None,
        )
        earliest_train_dt = min(
            (s.scheduled_start for s in train_slots if hasattr(s, "scheduled_start") and s.scheduled_start),
            default=None,
        )
        candidates = [dt for dt in (earliest_task_dt, earliest_train_dt) if dt is not None]
        ref_time = min(candidates) if candidates else DEFAULT_BASE_TIME

    # Determine candidate slot count from model action space
    slots_count = candidate_slots
    if hasattr(model.action_space, "n"):
        slots_count = int(model.action_space.n)

    candidate_windows = generate_candidate_windows(
        task=new_emergency_task,
        candidate_slots=slots_count,
    )

    # 2. Timing boundary: Measure inference + validation wall time
    t0 = time.perf_counter()

    # Construct observation for this specific scenario
    obs = construct_scenario_observation(
        emergency_task=new_emergency_task,
        base_scheduled_blocks=previous_solve_result.scheduled_blocks,
        unsafe_adjacency_pairs=unsafe_adjacency_pairs,
        candidate_windows=candidate_windows,
        base_time=ref_time,
        time_horizon_minutes=time_horizon_minutes,
    )

    # Get RL model predicted action
    action, _ = model.predict(obs, deterministic=True)
    act_idx = int(action)
    if not (0 <= act_idx < len(candidate_windows)):
        act_idx = 0
    cand_start, cand_end = candidate_windows[act_idx]

    # Construct candidate ScheduledBlockSchema
    emergency_block = ScheduledBlockSchema(
        id=f"BLOCK_{new_emergency_task.id}_{act_idx}",
        task_id=new_emergency_task.id,
        section_id=new_emergency_task.section_id,
        start_time=cand_start,
        end_time=cand_end,
        method="rl_placement",
        created_at=ref_time,
    )

    # 3. CRITICAL: The Re-Validation Gate
    # Check 1: Module 5 actual hard safety-adjacency constraints (using count_safety_violations ground truth)
    candidate_blocks = list(previous_solve_result.scheduled_blocks) + [emergency_block]
    safety_violations = count_safety_violations(candidate_blocks, unsafe_adjacency_pairs)
    safety_violation_detected = safety_violations > 0

    # Check 2: Same-section temporal no-overlap conflict
    same_section_overlap = False
    for b in previous_solve_result.scheduled_blocks:
        b_sec = b.section_id if hasattr(b, "section_id") else b["section_id"]
        if b_sec == emergency_block.section_id:
            b_start = b.start_time if hasattr(b, "start_time") else b["start_time"]
            b_end = b.end_time if hasattr(b, "end_time") else b["end_time"]
            if max(cand_start, b_start) < min(cand_end, b_end):
                same_section_overlap = True
                break

    t1 = time.perf_counter()
    inference_and_validation_time = max(0.0001, t1 - t0)

    # 4. Fallback execution if re-validation gate fails
    if safety_violation_detected or same_section_overlap:
        logger.warning(
            f"RL placement failed re-validation gate "
            f"(safety_violations={safety_violations}, same_section_overlap={same_section_overlap}). "
            f"Triggering fallback to replan_warm_start."
        )
        warm_context = previous_model_context or ({"base_time": ref_time} if ref_time else {})
        warm_result = replan_warm_start(
            previous_solve_result=previous_solve_result,
            previous_model_context=warm_context,
            new_emergency_task=new_emergency_task,
            all_existing_tasks=all_existing_tasks,
            sections=sections,
            train_slots=train_slots,
            profile=profile,
            time_horizon_minutes=time_horizon_minutes,
            time_limit_seconds=time_limit_seconds,
            base_time=ref_time,
            graph=graph,
            adjacencies=adjacencies,
            **kwargs,
        )

        return ReplanResult(
            strategy="rl",
            status=warm_result.status,
            scheduled_blocks=warm_result.scheduled_blocks,
            objective_value=warm_result.objective_value,
            wall_time_seconds=inference_and_validation_time + warm_result.wall_time_seconds,
            previous_objective_value=previous_solve_result.objective_value,
            tasks_changed=warm_result.tasks_changed,
            rl_fallback_triggered=True,
        )

    # 5. RL placement passed re-validation gate: Commit solution
    em_id = new_emergency_task.id if hasattr(new_emergency_task, "id") else new_emergency_task["id"]
    combined_tasks = [t for t in all_existing_tasks if (t.id if hasattr(t, "id") else t["id"]) != em_id]
    combined_tasks.append(new_emergency_task)

    objective_val = compute_schedule_objective(
        scheduled_blocks=candidate_blocks,
        tasks=combined_tasks,
        train_slots=train_slots,
        profile=profile,
    )

    tasks_changed = compute_tasks_changed(
        previous_blocks=previous_solve_result.scheduled_blocks,
        new_blocks=candidate_blocks,
    )

    sorted_blocks = sorted(
        candidate_blocks,
        key=lambda b: (
            b.start_time if hasattr(b, "start_time") else b["start_time"],
            b.section_id if hasattr(b, "section_id") else b["section_id"],
            b.task_id if hasattr(b, "task_id") else b["task_id"],
        ),
    )

    return ReplanResult(
        strategy="rl",
        status="FEASIBLE",
        scheduled_blocks=sorted_blocks,
        objective_value=objective_val,
        wall_time_seconds=inference_and_validation_time,
        previous_objective_value=previous_solve_result.objective_value,
        tasks_changed=tasks_changed,
        rl_fallback_triggered=False,
    )
