"""PPO policy training infrastructure for Cadence emergency re-planning."""

import logging
import os
from pathlib import Path
from typing import Any, Optional

import numpy as np
from stable_baselines3 import PPO
from stable_baselines3.common.callbacks import BaseCallback
from stable_baselines3.common.monitor import Monitor

from cadence.flux.env import CadenceReplanEnv

logger = logging.getLogger(__name__)


class TrainingProgressCallback(BaseCallback):
    """Simple callback to log training progress every N rollout steps."""

    def __init__(self, check_freq: int = 500, verbose: int = 1) -> None:
        super().__init__(verbose)
        self.check_freq = check_freq

    def _on_step(self) -> bool:
        if self.n_calls % self.check_freq == 0:
            if self.verbose > 0:
                logger.info(
                    f"Training progress: {self.num_timesteps} timesteps completed "
                    f"({self.n_calls} env steps)."
                )
        return True


def train_policy(
    profile_name: str = "metro",
    total_timesteps: int = 50000,
    save_path: str = "backend/cadence/flux/models/",
    seed: Optional[int] = None,
) -> str:
    """Train a PPO policy on CadenceReplanEnv for emergency possession re-planning.

    Args:
        profile_name: Name of the registered NetworkProfile ('metro', 'mainline', 'local').
        total_timesteps: Total environment timesteps to train for (default 50,000).
        save_path: Directory path where the trained model .zip will be saved.
        seed: Optional random seed for environment and policy initialization.

    Returns:
        str: File path to the saved model (.zip).
    """
    # 1. Instantiate CadenceReplanEnv wrapped with Monitor per SB3 convention
    raw_env = CadenceReplanEnv(profile_name=profile_name, seed=seed)
    env = Monitor(raw_env)

    # 2. PPO Hyperparameter Configuration & Rationale:
    # -------------------------------------------------------------------------
    # - policy="MlpPolicy": Multi-Layer Perceptron policy mapping continuous
    #   observation vectors (task features, occupancy, neighbor status) to discrete
    #   candidate time slot probabilities.
    # - learning_rate=3e-4: Standard Adam optimizer learning rate providing stable
    #   policy updates without aggressive divergence.
    # - n_steps: Number of steps to collect per rollout buffer. In CadenceReplanEnv,
    #   episodes are 1-step decisions. We adapt n_steps so that even for small
    #   test runs (e.g. total_timesteps=500), multiple updates occur.
    # - batch_size: Minibatch size for SGD updates. Configured to evenly divide
    #   n_steps (e.g. 32 or 64).
    # - n_epochs=10: Number of gradient passes over the collected rollout buffer.
    # - gamma=0.99: Standard discount factor.
    # - gae_lambda=0.95: Generalized advantage estimation factor.
    # - clip_range=0.2: Standard PPO surrogate objective clipping range.
    # - ent_coef=0.01: Small entropy regularization bonus to encourage exploration
    #   over multiple candidate windows.
    # - verbose=1: SB3 built-in rollout and loss logging.
    # -------------------------------------------------------------------------
    n_steps = min(64, max(16, total_timesteps))
    batch_size = min(32, n_steps)
    while n_steps % batch_size != 0 and batch_size > 1:
        batch_size //= 2
    batch_size = max(1, batch_size)

    model = PPO(
        policy="MlpPolicy",
        env=env,
        learning_rate=3e-4,
        n_steps=n_steps,
        batch_size=batch_size,
        n_epochs=10,
        gamma=0.99,
        gae_lambda=0.95,
        clip_range=0.2,
        ent_coef=0.01,
        verbose=1,
        seed=seed,
    )

    # 3. Train policy
    progress_callback = TrainingProgressCallback(
        check_freq=max(100, total_timesteps // 10),
        verbose=1,
    )
    model.learn(
        total_timesteps=total_timesteps,
        callback=progress_callback,
    )

    # 4. Save model to save_path encoding profile_name and total_timesteps
    save_path_obj = Path(save_path)
    if not save_path_obj.is_absolute():
        cwd = Path.cwd()
        if save_path in ("backend/cadence/flux/models/", "backend/cadence/flux/models"):
            save_dir = (Path(__file__).parent / "models").resolve()
        elif (cwd.name == "backend") and (save_path.startswith("backend/") or save_path.startswith("backend\\")):
            save_dir = (cwd / save_path[len("backend/"):]).resolve()
        else:
            save_dir = (cwd / save_path).resolve()
    else:
        save_dir = save_path_obj.resolve()

    save_dir.mkdir(parents=True, exist_ok=True)
    filename = f"ppo_{profile_name}_{total_timesteps}steps"
    save_file_path = str((save_dir / filename).resolve())
    model.save(save_file_path)

    # SB3 appends .zip to the saved model file
    final_model_path = save_file_path if save_file_path.endswith(".zip") else f"{save_file_path}.zip"
    logger.info(f"Model saved successfully to: {final_model_path}")

    env.close()
    return final_model_path


def evaluate_policy_quick(
    model_path: str,
    profile_name: str = "metro",
    n_eval_episodes: int = 20,
) -> dict[str, Any]:
    """Lightweight evaluation of a saved PPO model against fresh CadenceReplanEnv episodes.

    Computes quick sanity check statistics: mean reward, safety constraint violation rate,
    and mean disruption penalty.

    Args:
        model_path: Path to the saved SB3 PPO model (.zip).
        profile_name: Profile name to evaluate against.
        n_eval_episodes: Number of fresh evaluation episodes to execute (default 20).

    Returns:
        dict[str, Any]: Summary stats dictionary containing:
            - 'mean_reward': float
            - 'safety_violation_rate': float (fraction in [0.0, 1.0])
            - 'mean_disruption_penalty': float
            - 'n_eval_episodes': int
    """
    model = PPO.load(model_path)
    env = CadenceReplanEnv(profile_name=profile_name)

    rewards: list[float] = []
    safety_violations = 0
    disruptions: list[float] = []

    for _ in range(n_eval_episodes):
        obs, _ = env.reset()
        action, _ = model.predict(obs, deterministic=True)
        _, reward, _, _, step_info = env.step(int(action))

        rewards.append(float(reward))
        is_violating = bool(step_info.get("safety_violation", False) or step_info.get("is_safety_violating", False))
        if is_violating:
            safety_violations += 1
        disruptions.append(float(step_info.get("disruption_penalty", 0.0)))

    env.close()

    total_episodes = max(1, n_eval_episodes)
    return {
        "mean_reward": float(np.mean(rewards)) if rewards else 0.0,
        "safety_violation_rate": float(safety_violations / total_episodes),
        "mean_disruption_penalty": float(np.mean(disruptions)) if disruptions else 0.0,
        "n_eval_episodes": n_eval_episodes,
    }
