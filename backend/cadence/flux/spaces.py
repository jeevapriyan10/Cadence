"""Observation and action spaces for Flux reinforcement learning environments."""

import gymnasium as gym
import numpy as np

# Number of base features in the observation space before per-candidate slot occupancy
# 1. duration_norm (float in [0, 1])
# 2. priority_norm (float in [0, 1])
# 3. earliest_start_norm (float in [0, 1])
# 4. latest_end_norm (float in [0, 1])
# 5. target_section_task_count_norm (float in [0, 1])
# 6. unsafe_neighbors_blocked_count_norm (float in [0, 1])
NUM_BASE_OBS_FEATURES = 6


def build_observation_space(candidate_slots: int) -> gym.spaces.Box:
    """Build Gymnasium Box observation space for the replan environment.

    Encodes emergency task attributes (duration, priority, window bounds),
    section occupancy context, unsafe adjacent neighbor status, and per-slot
    occupancy indicators.

    Args:
        candidate_slots: Number of candidate start time slots.

    Returns:
        gym.spaces.Box: Continuous observation space of shape (6 + candidate_slots,).
    """
    total_dim = NUM_BASE_OBS_FEATURES + max(1, candidate_slots)
    return gym.spaces.Box(
        low=0.0,
        high=1.0,
        shape=(total_dim,),
        dtype=np.float32,
    )


def build_action_space(candidate_slots: int) -> gym.spaces.Discrete:
    """Build Gymnasium Discrete action space for the replan environment.

    Args:
        candidate_slots: Number of candidate start time slots to select from.

    Returns:
        gym.spaces.Discrete: Discrete action space of size candidate_slots.
    """
    return gym.spaces.Discrete(max(1, candidate_slots))
