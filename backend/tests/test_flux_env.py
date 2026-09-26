"""Pytest test suite for CadenceReplanEnv Gymnasium RL environment."""

from datetime import datetime, timedelta, timezone
from typing import Any
import gymnasium as gym
from gymnasium.utils.env_checker import check_env
import numpy as np
import pytest

from cadence.domain.schemas import MaintenanceTaskSchema, ScheduledBlockSchema
from cadence.flux import CadenceReplanEnv, build_action_space, build_observation_space

BASE_TEST_TIME = datetime(2026, 1, 1, 6, 0, 0, tzinfo=timezone.utc)


def test_spaces_helpers_independent() -> None:
    """build_observation_space and build_action_space work independently without instantiating env."""
    for slots in [5, 10, 20]:
        obs_space = build_observation_space(slots)
        act_space = build_action_space(slots)

        assert isinstance(obs_space, gym.spaces.Box)
        assert isinstance(act_space, gym.spaces.Discrete)
        assert obs_space.shape == (6 + slots,)
        assert obs_space.dtype == np.float32
        assert act_space.n == slots


@pytest.mark.parametrize("profile_name", ["metro", "mainline", "local"])
def test_env_instantiation_all_profiles(profile_name: str) -> None:
    """CadenceReplanEnv instantiates cleanly for all three supported profiles."""
    env = CadenceReplanEnv(
        profile_name=profile_name,
        section_count=8,
        train_count=10,
        task_count=6,
        candidate_slots=10,
    )
    assert env.profile_name == profile_name
    assert env.profile is not None
    assert env.action_space.n == 10
    assert env.observation_space.shape == (16,)
    env.close()


def test_env_reset_shape_and_info() -> None:
    """env.reset() returns an observation matching observation_space shape and a valid info dict."""
    env = CadenceReplanEnv(
        profile_name="metro",
        section_count=8,
        train_count=10,
        task_count=6,
        candidate_slots=10,
    )
    obs, info = env.reset(seed=42)

    assert isinstance(obs, np.ndarray)
    assert obs.shape == env.observation_space.shape
    assert obs.dtype == np.float32
    assert env.observation_space.contains(obs)

    # Info dictionary assertions
    assert isinstance(info, dict)
    assert "emergency_task_id" in info
    assert "emergency_section_id" in info
    assert "candidate_slots_count" in info
    assert info["candidate_slots_count"] == 10
    assert "base_blocks_count" in info
    assert "unsafe_adjacency_pairs_count" in info
    env.close()


def test_env_step_return_types_and_termination() -> None:
    """env.step() returns a valid 5-tuple with terminated=True (single-decision episode)."""
    env = CadenceReplanEnv(
        profile_name="metro",
        section_count=8,
        train_count=10,
        task_count=6,
        candidate_slots=8,
    )
    obs, info = env.reset(seed=42)

    next_obs, reward, terminated, truncated, step_info = env.step(0)

    assert env.observation_space.contains(next_obs)
    assert isinstance(reward, float)
    assert isinstance(terminated, bool)
    assert terminated is True  # Always True per single-decision framing
    assert isinstance(truncated, bool)
    assert truncated is False

    assert isinstance(step_info, dict)
    assert "safety_violation" in step_info
    assert isinstance(step_info["safety_violation"], bool)
    assert "disruption_penalty" in step_info
    assert isinstance(step_info["disruption_penalty"], (int, float))
    assert "scheduled_block" in step_info
    assert isinstance(step_info["scheduled_block"], ScheduledBlockSchema)
    assert "reward_breakdown" in step_info
    env.close()


def test_unsafe_vs_safe_reward_contrast() -> None:
    """A step resulting in an unsafe placement produces a reward significantly lower than a safe slot."""
    env = CadenceReplanEnv(
        profile_name="metro",
        section_count=8,
        train_count=10,
        task_count=6,
        candidate_slots=5,
    )
    env.reset(seed=123)

    # Identify a base block from the schedule
    base_block = env.base_scheduled_blocks[0]
    conflict_section = base_block.section_id
    conflict_start = base_block.start_time
    conflict_end = base_block.end_time

    # Construct an emergency task whose window spans the conflicting block
    # and also extends into a safe, non-overlapping time
    custom_emergency_task = MaintenanceTaskSchema(
        id="EMERGENCY_CONTRAST",
        name="Emergency Contrast Task",
        section_id=conflict_section,
        duration_minutes=30,
        earliest_start=conflict_start,
        latest_end=conflict_end + timedelta(hours=6),
        priority=5,
        is_emergency=True,
    )

    env.reset(seed=123, options={"emergency_task": custom_emergency_task})

    # Action 0 starts at conflict_start, overlapping directly with base_block on the same section -> Unsafe
    _, unsafe_reward, _, _, unsafe_info = env.step(0)

    # Reset with same task and test a later slot (e.g. slot 4) well after conflict_end -> Safe
    env.reset(seed=123, options={"emergency_task": custom_emergency_task})
    _, safe_reward, _, _, safe_info = env.step(4)

    assert unsafe_info["safety_violation"] is True
    # The safety violation penalty (-500.0) dominates the reward
    assert unsafe_reward < safe_reward - 100.0
    env.close()


def test_action_space_sample_reliability() -> None:
    """action_space.sample() produces valid actions that step() handles reliably across random seeds."""
    env = CadenceReplanEnv(
        profile_name="local",
        section_count=8,
        train_count=8,
        task_count=5,
        candidate_slots=6,
    )

    for seed in range(5):
        obs, _ = env.reset(seed=seed)
        action = env.action_space.sample()
        next_obs, reward, terminated, truncated, info = env.step(action)

        assert env.observation_space.contains(next_obs)
        assert isinstance(reward, float)
        assert terminated is True
        assert truncated is False
        assert "safety_violation" in info
    env.close()


def test_gym_env_checker_conformance() -> None:
    """Validate CadenceReplanEnv conforms to the Gymnasium API using check_env."""
    env = CadenceReplanEnv(
        profile_name="metro",
        section_count=8,
        train_count=10,
        task_count=6,
        candidate_slots=10,
        seed=42,
    )
    # Gymnasium's check_env verifies reset, step, observation/action space containment, determinism
    check_env(env)
    env.close()


def test_render_runs_without_error() -> None:
    """render() executes cleanly before and after a step."""
    env = CadenceReplanEnv(
        profile_name="mainline",
        section_count=8,
        train_count=10,
        task_count=6,
        candidate_slots=5,
    )
    env.reset(seed=42)

    # Render initial state
    out_init = env.render()
    assert isinstance(out_init, str)
    assert "Pending Emergency Task" in out_init

    # Render after step
    env.step(1)
    out_step = env.render()
    assert isinstance(out_step, str)
    assert "Step Summary" in out_step
    assert "Assigned Window" in out_step
    env.close()
