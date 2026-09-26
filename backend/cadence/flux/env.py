"""Gymnasium-compatible reinforcement learning environment for emergency possession re-planning."""

from datetime import datetime, timedelta, timezone
import logging
from typing import Any, Optional, Union
import uuid

import gymnasium as gym
import numpy as np

from cadence.domain.graph import NetworkGraph
from cadence.domain.schemas import (
    MaintenanceTaskSchema,
    ScheduledBlockSchema,
    TrackSectionSchema,
    TrainSlotSchema,
)
from cadence.flux.spaces import NUM_BASE_OBS_FEATURES, build_action_space, build_observation_space
from cadence.generator import generate_synthetic_network
from cadence.profiles.base import NetworkProfile
from cadence.profiles.registry import ProfileRegistry
from cadence.ripple.simulator import simulate_cascade
from cadence.solve import precompute_unsafe_adjacency_pairs, solve_schedule

logger = logging.getLogger(__name__)

# =============================================================================
# FLUX REINFORCEMENT LEARNING REWARD WEIGHTING SPECIFICATION
# -----------------------------------------------------------------------------
# Module 13's PPO policy optimizes against this reward function to learn fast,
# safe, low-disruption emergency possession placement decisions.
#
# Components:
# 1. Decision Speed Proxy Bonus (BASE_SPEED_BONUS = 10.0):
#    Guaranteed positive baseline reward for producing a valid sub-second decision.
#
# 2. Safety-Adjacency Hard Constraint Penalty (SAFETY_VIOLATION_PENALTY = 500.0):
#    Dominant negative penalty applied if the placement violates Module 5's
#    precomputed safety-adjacency hard constraints or creates an overlapping
#    possession on the same track section. This term heavily dominates the reward.
#
# 3. Train Service Disruption Penalty (DISRUPTION_PENALTY_WEIGHT = 1.0):
#    Negative penalty scaled by the active profile's disruption_penalty() for all
#    train slots whose routes intersect the emergency block (via Ripple simulation).
#
# 4. Priority-Weighted Earliness Bonus (EARLINESS_BONUS_WEIGHT = 5.0):
#    Small positive reward encouraging earlier task placement within its window,
#    scaled by normalized task priority (mirroring Solve's delay minimization term).
#    Formulation: (priority / 10.0) * earliness_ratio * EARLINESS_BONUS_WEIGHT
# =============================================================================
BASE_SPEED_BONUS = 10.0
SAFETY_VIOLATION_PENALTY = 500.0
DISRUPTION_PENALTY_WEIGHT = 1.0
EARLINESS_BONUS_WEIGHT = 5.0
DEFAULT_HORIZON_MINUTES = 1440


def generate_candidate_windows(
    task: MaintenanceTaskSchema,
    candidate_slots: int,
) -> list[tuple[datetime, datetime]]:
    """Generate candidate_slots evenly-spaced candidate start and end times within a task window.

    Follows the Reason (Module 7) candidate slot generation pattern: divides the span
    between earliest_start and (latest_end - duration) into candidate_slots evenly spaced
    candidate start times.

    Args:
        task: MaintenanceTaskSchema with earliest_start, latest_end, and duration_minutes.
        candidate_slots: Number of candidate slots to produce (>= 1).

    Returns:
        list[tuple[datetime, datetime]]: List of (candidate_start, candidate_end) intervals.
    """
    duration = timedelta(minutes=max(1, int(task.duration_minutes)))
    latest_start = task.latest_end - duration
    earliest_start = task.earliest_start

    num_slots = max(1, candidate_slots)
    windows: list[tuple[datetime, datetime]] = []

    if latest_start <= earliest_start or num_slots == 1:
        # Window cannot be subdivided or only 1 slot requested
        for _ in range(num_slots):
            windows.append((earliest_start, earliest_start + duration))
        return windows

    total_span_sec = (latest_start - earliest_start).total_seconds()
    step_sec = total_span_sec / float(num_slots - 1)

    for i in range(num_slots):
        slot_start = earliest_start + timedelta(seconds=i * step_sec)
        slot_end = slot_start + duration
        windows.append((slot_start, slot_end))

    return windows


class CadenceReplanEnv(gym.Env):
    """Gymnasium environment framing emergency-task possession re-planning as an RL episode."""

    metadata = {"render_modes": ["human"]}

    def __init__(
        self,
        profile_name: str = "metro",
        section_count: Optional[int] = None,
        train_count: int = 20,
        task_count: int = 15,
        candidate_slots: int = 10,
        seed: Optional[int] = None,
    ) -> None:
        """Initialize the CadenceReplanEnv environment.

        Args:
            profile_name: Name of the registered NetworkProfile ('metro', 'mainline', 'local').
            section_count: Optional section count for synthetic network generation.
            train_count: Number of train slots to generate per episode scenario.
            task_count: Number of pre-existing maintenance tasks per scenario.
            candidate_slots: Number of discrete candidate time slots for the emergency task.
            seed: Optional integer seed for environment initialization.
        """
        super().__init__()
        self.profile_name = profile_name
        self.profile: NetworkProfile = ProfileRegistry.get(profile_name)
        self.section_count = section_count
        self.train_count = train_count
        self.task_count = task_count
        self.candidate_slots = candidate_slots

        # Initialize spaces
        self.action_space = build_action_space(candidate_slots)
        self.observation_space = build_observation_space(candidate_slots)

        # Scenario state placeholders
        self.sections: list[TrackSectionSchema] = []
        self.tasks: list[MaintenanceTaskSchema] = []
        self.train_slots: list[TrainSlotSchema] = []
        self.adjacencies: list[Any] = []
        self.graph: Optional[NetworkGraph] = None
        self.base_time: datetime = datetime(2026, 1, 1, 6, 0, 0, tzinfo=timezone.utc)
        self.base_scheduled_blocks: list[ScheduledBlockSchema] = []
        self.unsafe_adjacency_pairs: list[tuple[str, str, str]] = []

        self.emergency_task: Optional[MaintenanceTaskSchema] = None
        self.candidate_windows: list[tuple[datetime, datetime]] = []

        self.last_action: Optional[int] = None
        self.last_scheduled_block: Optional[ScheduledBlockSchema] = None
        self.last_reward: Optional[float] = None
        self.last_info: dict[str, Any] = {}

        if seed is not None:
            self.reset(seed=seed)

    def _get_obs(self) -> np.ndarray:
        """Construct continuous observation vector for current emergency task and schedule state.

        Returns:
            np.ndarray: Float32 vector of shape (NUM_BASE_OBS_FEATURES + candidate_slots,).
        """
        assert self.emergency_task is not None, "Environment must be reset before getting observation."

        # 1. Normalized emergency task attributes
        duration_norm = float(self.emergency_task.duration_minutes) / float(DEFAULT_HORIZON_MINUTES)
        priority_norm = float(self.emergency_task.priority) / 10.0

        es_offset_min = (self.emergency_task.earliest_start - self.base_time).total_seconds() / 60.0
        le_offset_min = (self.emergency_task.latest_end - self.base_time).total_seconds() / 60.0
        window_start_norm = float(np.clip(es_offset_min / float(DEFAULT_HORIZON_MINUTES), 0.0, 1.0))
        window_end_norm = float(np.clip(le_offset_min / float(DEFAULT_HORIZON_MINUTES), 0.0, 1.0))

        # 2. Schedule occupancy summary on/near target section
        total_base_blocks = max(1, len(self.base_scheduled_blocks))
        target_sec_blocks = sum(1 for b in self.base_scheduled_blocks if b.section_id == self.emergency_task.section_id)
        target_sec_task_count_norm = float(np.clip(float(target_sec_blocks) / float(total_base_blocks), 0.0, 1.0))

        # 3. Count of unsafe neighbor sections currently blocked
        unsafe_neighbor_sections = {
            pair[1] if pair[0] == self.emergency_task.section_id else pair[0]
            for pair in self.unsafe_adjacency_pairs
            if self.emergency_task.section_id in (pair[0], pair[1])
        }
        unsafe_neighbor_blocks = sum(
            1 for b in self.base_scheduled_blocks if b.section_id in unsafe_neighbor_sections
        )
        unsafe_neighbors_blocked_norm = float(
            np.clip(float(unsafe_neighbor_blocks) / float(total_base_blocks), 0.0, 1.0)
        )

        # 4. Per-candidate-slot occupancy indicators
        slot_occupancy: list[float] = []
        for c_start, c_end in self.candidate_windows:
            overlapping_count = sum(
                1
                for b in self.base_scheduled_blocks
                if (b.section_id == self.emergency_task.section_id or b.section_id in unsafe_neighbor_sections)
                and max(b.start_time, c_start) < min(b.end_time, c_end)
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

        obs = np.array(features, dtype=np.float32)
        return obs

    def reset(
        self,
        seed: Optional[int] = None,
        options: Optional[dict[str, Any]] = None,
    ) -> tuple[np.ndarray, dict[str, Any]]:
        """Reset the environment, generating a fresh synthetic scenario and emergency task.

        Args:
            seed: Optional integer seed for reproducible scenario generation.
            options: Optional dictionary allowing test overrides (e.g. 'emergency_task').

        Returns:
            tuple[np.ndarray, dict[str, Any]]: Initial observation and scenario info dictionary.
        """
        super().reset(seed=seed)

        # Determine generation seed from Gymnasium RNG
        gen_seed = int(self.np_random.integers(0, 1_000_000)) if seed is None else int(seed)

        # Generate synthetic network scenario
        net = generate_synthetic_network(
            profile_name=self.profile_name,
            seed=gen_seed,
            section_count=self.section_count,
            train_count=self.train_count,
            task_count=self.task_count,
        )
        self.sections = net["sections"]
        self.tasks = net["maintenance_tasks"]
        self.train_slots = net["train_slots"]
        self.adjacencies = net["adjacencies"]

        # Determine horizon base time
        earliest_task_dt = min(
            (t.earliest_start for t in self.tasks if hasattr(t, "earliest_start") and t.earliest_start),
            default=None,
        )
        earliest_train_dt = min(
            (s.scheduled_start for s in self.train_slots if hasattr(s, "scheduled_start") and s.scheduled_start),
            default=None,
        )
        candidates = [dt for dt in (earliest_task_dt, earliest_train_dt) if dt is not None]
        self.base_time = min(candidates) if candidates else datetime(2026, 1, 1, 6, 0, 0, tzinfo=timezone.utc)

        # Solve base schedule
        base_solve = solve_schedule(
            sections=self.sections,
            tasks=self.tasks,
            train_slots=self.train_slots,
            profile=self.profile,
            time_horizon_minutes=DEFAULT_HORIZON_MINUTES,
            base_time=self.base_time,
            adjacencies=self.adjacencies,
        )
        self.base_scheduled_blocks = base_solve.scheduled_blocks

        # Build graph and precompute Module 5 unsafe adjacency pairs
        self.graph = NetworkGraph.build_from_sections(self.sections, self.adjacencies)
        self.unsafe_adjacency_pairs = precompute_unsafe_adjacency_pairs(
            graph=self.graph,
            profile=self.profile,
            sections=self.sections,
            train_slots=self.train_slots,
        )

        # Inject emergency task (from options or dynamically generated)
        if options and "emergency_task" in options:
            self.emergency_task = options["emergency_task"]
        else:
            # Pick a section from available sections
            sec_idx = int(self.np_random.integers(0, len(self.sections)))
            chosen_section = self.sections[sec_idx]
            sec_id = chosen_section.id if hasattr(chosen_section, "id") else chosen_section["id"]

            duration_min = int(self.np_random.integers(45, 90))
            start_hour = int(self.np_random.integers(1, 4))
            span_hours = int(self.np_random.integers(6, 12))

            em_start = self.base_time + timedelta(hours=start_hour)
            em_end = em_start + timedelta(hours=span_hours)
            em_priority = int(self.np_random.integers(5, 11))

            self.emergency_task = MaintenanceTaskSchema(
                id=f"EMERGENCY_{gen_seed}",
                name="Emergency Track Repair",
                section_id=sec_id,
                duration_minutes=duration_min,
                earliest_start=em_start,
                latest_end=em_end,
                priority=em_priority,
                is_emergency=True,
            )

        # Generate candidate slot intervals
        self.candidate_windows = generate_candidate_windows(
            task=self.emergency_task,
            candidate_slots=self.candidate_slots,
        )

        self.last_action = None
        self.last_scheduled_block = None
        self.last_reward = None
        self.last_info = {}

        obs = self._get_obs()
        info: dict[str, Any] = {
            "emergency_task_id": self.emergency_task.id,
            "emergency_section_id": self.emergency_task.section_id,
            "candidate_slots_count": len(self.candidate_windows),
            "base_blocks_count": len(self.base_scheduled_blocks),
            "unsafe_adjacency_pairs_count": len(self.unsafe_adjacency_pairs),
        }
        return obs, info

    def step(self, action: int) -> tuple[np.ndarray, float, bool, bool, dict[str, Any]]:
        """Execute one re-planning step, placing the emergency task into the chosen candidate slot.

        Per the buildplan's single-decision emergency replanning framing, each episode
        consists of exactly one placement decision and immediately terminates.

        Args:
            action: Discrete index of the candidate start slot to select.

        Returns:
            tuple[np.ndarray, float, bool, bool, dict[str, Any]]:
                (observation, reward, terminated=True, truncated=False, info)
        """
        assert self.emergency_task is not None, "Environment must be reset before calling step."
        act_idx = int(action)
        if not (0 <= act_idx < len(self.candidate_windows)):
            raise ValueError(f"Action {act_idx} out of bounds for candidate_slots {len(self.candidate_windows)}")

        cand_start, cand_end = self.candidate_windows[act_idx]

        # Construct candidate ScheduledBlockSchema
        emergency_block = ScheduledBlockSchema(
            id=f"BLOCK_{self.emergency_task.id}_{act_idx}",
            task_id=self.emergency_task.id,
            section_id=self.emergency_task.section_id,
            start_time=cand_start,
            end_time=cand_end,
            method="rl_placement",
            created_at=self.base_time,
        )
        self.last_action = act_idx
        self.last_scheduled_block = emergency_block

        # 1. Check Module 5 safety-adjacency hard constraint & same-section overlap
        same_section_overlap = any(
            b.section_id == emergency_block.section_id
            and max(b.start_time, emergency_block.start_time) < min(b.end_time, emergency_block.end_time)
            for b in self.base_scheduled_blocks
        )

        unsafe_adjacent_violations = 0
        unsafe_set = {
            tuple(sorted((p[0], p[1]))) for p in self.unsafe_adjacency_pairs
        }
        for b in self.base_scheduled_blocks:
            pair = tuple(sorted((emergency_block.section_id, b.section_id)))
            if pair in unsafe_set:
                if max(emergency_block.start_time, b.start_time) < min(emergency_block.end_time, b.end_time):
                    unsafe_adjacent_violations += 1

        is_safety_violating = same_section_overlap or (unsafe_adjacent_violations > 0)

        # 2. Check train disruption penalty via Ripple direct impact logic
        train_map = {
            (t.id if hasattr(t, "id") else t["id"]): t for t in self.train_slots
        }
        ripple_report = simulate_cascade(
            scheduled_blocks=[emergency_block],
            train_slots=self.train_slots,
            graph=self.graph,
            sections=self.sections,
        )

        total_disruption_penalty = 0.0
        for impact in ripple_report.per_train_impacts:
            slot_obj = train_map.get(impact.train_slot_id)
            if slot_obj is not None:
                slot_schema = (
                    slot_obj if isinstance(slot_obj, TrainSlotSchema) else TrainSlotSchema.model_validate(slot_obj)
                )
                total_disruption_penalty += float(self.profile.disruption_penalty(slot_schema))

        # 3. Compute early placement reward (mirroring Solve's delay minimization)
        duration = timedelta(minutes=int(self.emergency_task.duration_minutes))
        latest_possible_start = self.emergency_task.latest_end - duration
        total_window_span_sec = max(
            1.0, (latest_possible_start - self.emergency_task.earliest_start).total_seconds()
        )
        delay_sec = max(0.0, (cand_start - self.emergency_task.earliest_start).total_seconds())
        earliness_ratio = float(np.clip(1.0 - (delay_sec / total_window_span_sec), 0.0, 1.0))
        normalized_priority = float(self.emergency_task.priority) / 10.0
        earliness_bonus = float(normalized_priority * earliness_ratio * EARLINESS_BONUS_WEIGHT)

        # 4. Compute composite reward
        safety_penalty = -SAFETY_VIOLATION_PENALTY if is_safety_violating else 0.0
        disruption_penalty_term = -DISRUPTION_PENALTY_WEIGHT * total_disruption_penalty

        reward = float(BASE_SPEED_BONUS + safety_penalty + disruption_penalty_term + earliness_bonus)
        self.last_reward = reward

        # Single-decision episode: terminated is always True
        terminated = True
        truncated = False

        obs = self._get_obs()
        info: dict[str, Any] = {
            "action": act_idx,
            "safety_violation": is_safety_violating,
            "is_safety_violating": is_safety_violating,
            "same_section_overlap": same_section_overlap,
            "unsafe_adjacent_violations": unsafe_adjacent_violations,
            "disruption_penalty": total_disruption_penalty,
            "affected_train_count": len(ripple_report.per_train_impacts),
            "earliness_ratio": earliness_ratio,
            "scheduled_block": emergency_block,
            "candidate_start": cand_start.isoformat(),
            "candidate_end": cand_end.isoformat(),
            "reward_breakdown": {
                "base_speed_bonus": BASE_SPEED_BONUS,
                "safety_penalty": safety_penalty,
                "disruption_penalty": disruption_penalty_term,
                "earliness_bonus": earliness_bonus,
                "total_reward": reward,
            },
        }
        self.last_info = info

        return obs, reward, terminated, truncated, info

    def render(self, mode: str = "human") -> Optional[str]:
        """Render a text summary of the emergency possession decision."""
        if self.last_scheduled_block is None:
            summary = (
                f"CadenceReplanEnv [Profile: {self.profile_name}]\n"
                f"  Pending Emergency Task: {self.emergency_task.id if self.emergency_task else 'None'}\n"
                f"  Available Candidate Slots: {len(self.candidate_windows)}"
            )
        else:
            block = self.last_scheduled_block
            info = self.last_info
            summary = (
                f"CadenceReplanEnv Step Summary [Profile: {self.profile_name}]\n"
                f"  Emergency Task: {block.task_id} on Section: {block.section_id}\n"
                f"  Assigned Window: {block.start_time.strftime('%H:%M')} - {block.end_time.strftime('%H:%M')}\n"
                f"  Safety Violation: {info.get('safety_violation', False)}\n"
                f"  Disruption Penalty: {info.get('disruption_penalty', 0.0):.2f}\n"
                f"  Reward: {self.last_reward:.2f}"
            )
        print(summary)
        return summary

    def close(self) -> None:
        """Clean up environment resources."""
        pass
