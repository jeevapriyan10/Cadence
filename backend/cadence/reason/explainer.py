"""Post-hoc explainability engine for Cadence maintenance possession block scheduling."""

from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field

from cadence.domain.graph import NetworkGraph
from cadence.domain.models import MaintenanceTask, TrackSection, TrainSlot
from cadence.domain.schemas import (
    MaintenanceTaskSchema,
    ScheduledBlockSchema,
    TrackSectionSchema,
    TrainSlotSchema,
)
from cadence.profiles.base import NetworkProfile
from cadence.ripple.simulator import _compute_train_section_windows
from cadence.solve.safety import precompute_unsafe_adjacency_pairs
from cadence.solve.solver import SolveResult


class RejectedCandidate(BaseModel):
    """Details of a plausible candidate scheduling slot that was rejected by constraint logic."""

    candidate_start: datetime
    candidate_end: datetime
    rejection_reason: str
    conflicting_entity_id: str
    conflicting_entity_type: Literal["task", "train_slot", "safety_adjacency"]

    model_config = ConfigDict(arbitrary_types_allowed=True)


class Explanation(BaseModel):
    """Post-hoc explanation of why a maintenance task was scheduled in its assigned window."""

    task_id: str
    task_name: str
    scheduled_start: datetime
    scheduled_end: datetime
    chosen_reason: str
    rejected_candidates: list[RejectedCandidate] = Field(default_factory=list)

    model_config = ConfigDict(arbitrary_types_allowed=True)


def generate_candidate_slots(
    task: Union[MaintenanceTaskSchema, MaintenanceTask],
    granularity_minutes: int = 15,
    scheduled_start: Optional[datetime] = None,
) -> list[tuple[datetime, datetime]]:
    """Generate evenly spaced candidate start and end times within a task's permitted window.

    Generates potential slots from task.earliest_start up to task.latest_end - duration,
    stepped by granularity_minutes, filtering out the actually-scheduled slot.

    Args:
        task: Maintenance task with earliest_start, latest_end, and duration_minutes.
        granularity_minutes: Grid interval step size in minutes (default 15).
        scheduled_start: Optional timestamp of the chosen slot to exclude from candidates.

    Returns:
        list[tuple[datetime, datetime]]: List of (candidate_start, candidate_end) intervals.
    """
    task_schema = task if isinstance(task, MaintenanceTaskSchema) else MaintenanceTaskSchema.model_validate(task)
    duration = timedelta(minutes=int(task_schema.duration_minutes))
    step = timedelta(minutes=max(1, granularity_minutes))

    candidates: list[tuple[datetime, datetime]] = []
    curr = task_schema.earliest_start

    # Iterate until candidate_end exceeds latest_end
    while curr + duration <= task_schema.latest_end:
        cand_start = curr
        cand_end = curr + duration

        # Exclude the slot that was actually chosen by the solver
        if scheduled_start is None or abs((cand_start - scheduled_start).total_seconds()) > 30:
            candidates.append((cand_start, cand_end))

        curr += step

    return candidates


def check_candidate_feasibility(
    candidate_start: datetime,
    candidate_end: datetime,
    task: Union[MaintenanceTaskSchema, MaintenanceTask],
    other_scheduled_blocks: list[Union[ScheduledBlockSchema, Any]],
    unsafe_adjacency_pairs: list[tuple[str, str, str]],
    train_slots: list[Union[TrainSlotSchema, Any]],
    sections: list[Union[TrackSectionSchema, Any]],
    profile: Optional[NetworkProfile] = None,
) -> Optional[RejectedCandidate]:
    """Check whether a candidate time window for a task violates physical or operational constraints.

    Evaluates potential conflicts in strict priority order:
        1. Same-section temporal overlap against other scheduled maintenance tasks.
        2. Safety-adjacency violations against maintenance blocks on adjacent sections flagged unsafe.
        3. Train slot disruptions traversing this section during the candidate window.

    Args:
        candidate_start: Proposed start time.
        candidate_end: Proposed end time.
        task: Maintenance task being evaluated.
        other_scheduled_blocks: Scheduled possession blocks for all other tasks.
        unsafe_adjacency_pairs: Precomputed list of (sec_a, sec_b, reason) flagged unsafe.
        train_slots: Timetable train slots traversing the network.
        sections: Track sections in the network.
        profile: Optional active NetworkProfile for disruption penalty context.

    Returns:
        Optional[RejectedCandidate]: Details of the first encountered conflict, or None if feasible.
    """
    task_schema = task if isinstance(task, MaintenanceTaskSchema) else MaintenanceTaskSchema.model_validate(task)
    sec_id = task_schema.section_id

    # -------------------------------------------------------------------------
    # 1. Priority 1: Same-section time overlap against other scheduled tasks
    # -------------------------------------------------------------------------
    for block in other_scheduled_blocks:
        b_sec = block.section_id if hasattr(block, "section_id") else block["section_id"]
        if b_sec != sec_id:
            continue

        b_start = block.start_time if hasattr(block, "start_time") else block["start_time"]
        b_end = block.end_time if hasattr(block, "end_time") else block["end_time"]
        b_task_id = block.task_id if hasattr(block, "task_id") else block["task_id"]

        # Check temporal overlap
        if max(candidate_start, b_start) < min(candidate_end, b_end):
            return RejectedCandidate(
                candidate_start=candidate_start,
                candidate_end=candidate_end,
                rejection_reason=(
                    f"Section '{sec_id}' is occupied by scheduled maintenance task '{b_task_id}' "
                    f"from {b_start.strftime('%H:%M')} to {b_end.strftime('%H:%M')}."
                ),
                conflicting_entity_id=b_task_id,
                conflicting_entity_type="task",
            )

    # -------------------------------------------------------------------------
    # 2. Priority 2: Safety-adjacency pairs (cannot block both sections simultaneously)
    # -------------------------------------------------------------------------
    # Build fast lookup mapping sec_id -> dict of {partner_sec_id: reason}
    unsafe_partners: dict[str, str] = {}
    for a, b, reason in unsafe_adjacency_pairs:
        if sec_id == a:
            unsafe_partners[b] = reason
        elif sec_id == b:
            unsafe_partners[a] = reason

    if unsafe_partners:
        for block in other_scheduled_blocks:
            b_sec = block.section_id if hasattr(block, "section_id") else block["section_id"]
            if b_sec in unsafe_partners:
                b_start = block.start_time if hasattr(block, "start_time") else block["start_time"]
                b_end = block.end_time if hasattr(block, "end_time") else block["end_time"]
                b_task_id = block.task_id if hasattr(block, "task_id") else block["task_id"]

                if max(candidate_start, b_start) < min(candidate_end, b_end):
                    return RejectedCandidate(
                        candidate_start=candidate_start,
                        candidate_end=candidate_end,
                        rejection_reason=unsafe_partners[b_sec],
                        conflicting_entity_id=b_sec,
                        conflicting_entity_type="safety_adjacency",
                    )

    # -------------------------------------------------------------------------
    # 3. Priority 3: Train slot disruption
    # -------------------------------------------------------------------------
    for slot in train_slots:
        slot_schema = slot if isinstance(slot, TrainSlotSchema) else TrainSlotSchema.model_validate(slot)
        if sec_id not in slot_schema.route:
            continue

        windows = _compute_train_section_windows(slot_schema)
        sec_window = next((w for w in windows if w[0] == sec_id), None)
        if not sec_window:
            continue

        _, t_start, t_end = sec_window
        if max(candidate_start, t_start) < min(candidate_end, t_end):
            penalty_info = ""
            if profile is not None:
                penalty = profile.disruption_penalty(slot_schema)
                penalty_info = f" with disruption penalty {penalty:.1f}"

            return RejectedCandidate(
                candidate_start=candidate_start,
                candidate_end=candidate_end,
                rejection_reason=(
                    f"Disrupts scheduled train slot '{slot_schema.name}' ({slot_schema.id}) on section '{sec_id}'"
                    f"{penalty_info}."
                ),
                conflicting_entity_id=slot_schema.id,
                conflicting_entity_type="train_slot",
            )

    return None


def explain_task_scheduling(
    task_id: str,
    solve_result: SolveResult,
    model_context: dict[str, Any],
    sections: list[Union[TrackSectionSchema, Any]],
    tasks: list[Union[MaintenanceTaskSchema, Any]],
    train_slots: list[Union[TrainSlotSchema, Any]],
    profile: NetworkProfile,
) -> Explanation:
    """Reconstruct why a specific maintenance task was scheduled in its slot and why alternative slots were rejected.

    Args:
        task_id: ID of the maintenance task to explain.
        solve_result: SolveResult from solve_schedule.
        model_context: BuildModelResult or dict returned by build_cp_model.
        sections: Track sections in the network.
        tasks: All maintenance tasks in the scenario.
        train_slots: Train slot services.
        profile: Active NetworkProfile.

    Returns:
        Explanation: Structured post-hoc explanation with chosen rationale and rejected candidates.
    """
    # Locate task definition
    task_lookup = {
        (t.id if hasattr(t, "id") else t["id"]): t for t in tasks
    }
    task_obj = task_lookup.get(task_id)
    if task_obj is None:
        raise ValueError(f"Task '{task_id}' not found in task list.")

    task_schema = (
        task_obj if isinstance(task_obj, MaintenanceTaskSchema) else MaintenanceTaskSchema.model_validate(task_obj)
    )

    # Locate scheduled block
    scheduled_block = next((b for b in solve_result.scheduled_blocks if b.task_id == task_id), None)
    if scheduled_block is None:
        return Explanation(
            task_id=task_id,
            task_name=task_schema.name,
            scheduled_start=task_schema.earliest_start,
            scheduled_end=task_schema.earliest_start + timedelta(minutes=int(task_schema.duration_minutes)),
            chosen_reason=f"Task '{task_id}' could not be feasibly scheduled (solver status: {solve_result.status}).",
            rejected_candidates=[],
        )

    # Resolve unsafe adjacency pairs (solve_result can be SolveResult or ReplanResult)
    unsafe_pairs = (
        model_context.get("unsafe_adjacency_pairs")
        or getattr(solve_result, "unsafe_adjacency_pairs", None)
        or []
    )
    if not unsafe_pairs:
        # Precompute fallback if not populated in context
        graph = NetworkGraph.build_from_sections(sections, [])
        unsafe_pairs = precompute_unsafe_adjacency_pairs(graph, profile, sections)

    other_blocks = [b for b in solve_result.scheduled_blocks if b.task_id != task_id]

    # Generate candidate slots excluding the chosen slot
    candidate_intervals = generate_candidate_slots(
        task=task_schema,
        granularity_minutes=15,
        scheduled_start=scheduled_block.start_time,
    )

    rejected_candidates: list[RejectedCandidate] = []
    for c_start, c_end in candidate_intervals:
        rejection = check_candidate_feasibility(
            candidate_start=c_start,
            candidate_end=c_end,
            task=task_schema,
            other_scheduled_blocks=other_blocks,
            unsafe_adjacency_pairs=unsafe_pairs,
            train_slots=train_slots,
            sections=sections,
            profile=profile,
        )
        if rejection is not None:
            rejected_candidates.append(rejection)

    # Formulate chosen_reason
    task_weight = profile.priority_weight(task_schema)
    start_delay_min = int((scheduled_block.start_time - task_schema.earliest_start).total_seconds() // 60)

    # Check competing tasks on the same section or safety-adjacent sections
    competing_tasks: list[str] = []
    for b in other_blocks:
        if b.section_id == task_schema.section_id:
            competing_tasks.append(b.task_id)

    if start_delay_min == 0:
        chosen_reason = (
            f"Scheduled at earliest possible start time ({scheduled_block.start_time.strftime('%H:%M')}) "
            f"with priority weight {task_weight:.1f}, minimizing schedule delay penalty."
        )
    else:
        competitor_info = f" competing with task(s) {competing_tasks}" if competing_tasks else ""
        chosen_reason = (
            f"Scheduled at {scheduled_block.start_time.strftime('%H:%M')} (delayed by {start_delay_min} min "
            f"from earliest start) with priority weight {task_weight:.1f}{competitor_info} "
            f"to resolve constraint feasibility."
        )

    return Explanation(
        task_id=task_id,
        task_name=task_schema.name,
        scheduled_start=scheduled_block.start_time,
        scheduled_end=scheduled_block.end_time,
        chosen_reason=chosen_reason,
        rejected_candidates=rejected_candidates,
    )


def explain_full_schedule(
    solve_result: SolveResult,
    model_context: dict[str, Any],
    sections: list[Union[TrackSectionSchema, Any]],
    tasks: list[Union[MaintenanceTaskSchema, Any]],
    train_slots: list[Union[TrainSlotSchema, Any]],
    profile: NetworkProfile,
) -> list[Explanation]:
    """Generate post-hoc explanations for every scheduled maintenance possession block in a solve result.

    Args:
        solve_result: Result output from solve_schedule.
        model_context: Dictionary context from build_cp_model.
        sections: Track sections in the railway network.
        tasks: Maintenance tasks to explain.
        train_slots: Timetable train slots.
        profile: Active NetworkProfile.

    Returns:
        list[Explanation]: List of Explanation records corresponding to each scheduled task.
    """
    explanations: list[Explanation] = []
    for block in solve_result.scheduled_blocks:
        expl = explain_task_scheduling(
            task_id=block.task_id,
            solve_result=solve_result,
            model_context=model_context,
            sections=sections,
            tasks=tasks,
            train_slots=train_slots,
            profile=profile,
        )
        explanations.append(expl)

    return explanations
