"""This is OpenAI' Spinning Up PyTorch implementation of Soft-Actor-Critic with
minor adjustments.
For the official documentation, see below:
https://spinningup.openai.com/en/latest/algorithms/sac.html#documentation-pytorch-version
Source:
https://github.com/openai/spinningup/blob/master/spinup/algos/pytorch/sac/sac.py
"""
import itertools
import queue, threading
from copy import deepcopy

import torch
import numpy as np
from gym.spaces import Box
from torch.optim import Adam

from agents.base import BaseAgent
from l2r.common.models.network import ActorCritic
from l2r.common.models.vae import VAE
from l2r.common.utils import RecordExperience
from l2r.common.utils import setup_logging

from ruamel.yaml import YAML

from buffers.replay_buffer import ReplayBuffer

from constants import DEVICE

from base.envwrapper import EnvContainer



class SACAgent(BaseAgent):
    """Adopted from https://github.com/learn-to-race/l2r/blob/main/l2r/baselines/rl/sac.py"""

    def __init__(self):
        super(SACAgent, self).__init__()

        self.cfg = self.load_model_config("models/sac/params-sac.yaml")
        self.file_logger, self.tb_logger = self.setup_loggers()

        if self.cfg["record_experience"]:
            self.setup_experience_recorder()

        # Action limit for clamping: critically, assumes all dimensions share the same bound!
        # self.act_limit = self.action_space.high[0]

        self.setup_vision_encoder()
        self.set_params()

    def select_action(self, obs, encode=True):
        # Until start_steps have elapsed, randomly sample actions
        # from a uniform distribution for better exploration. Afterwards,
        # use the learned policy.
        if encode:
            obs = self._encode(obs)
        if self.t > self.cfg["start_steps"]:
            a = self.actor_critic.act(obs.to(DEVICE), self.deterministic)
            a = a  # numpy array...
            self.record["transition_actor"] = "learner"
        else:
            a = self.action_space.sample()
            self.record["transition_actor"] = "random"
        self.t = self.t + 1
        return a

    def register_reset(self, obs) -> np.array:
        """
        Same input/output as select_action, except this method is called at episodal reset.
        """
        # camera, features, state = obs
        self.deterministic = True
        self.t = 1e6

    def load_model(self, path):
        self.actor_critic.load_state_dict(torch.load(path))

    def save_model(self, path):
        torch.save(self.actor_critic.state_dict(), path)

    def setup_experience_recorder(self):
        self.save_queue = queue.Queue()
        self.save_batch_size = 256
        self.record_experience = RecordExperience(
            self.cfg["record_dir"],
            self.cfg["track_name"],
            self.cfg["experiment_name"],
            self.file_logger,
            self,
        )
        self.save_thread = threading.Thread(target=self.record_experience.save_thread)
        self.save_thread.start()

    def setup_vision_encoder(self):
        assert self.cfg["use_encoder_type"] in [
            "vae"
        ], "Specified encoder type must be in ['vae']"
        speed_hiddens = self.cfg[self.cfg["use_encoder_type"]]["speed_hiddens"]
        self.feat_dim = self.cfg[self.cfg["use_encoder_type"]]["latent_dims"] + 1
        self.obs_dim = (
            self.cfg[self.cfg["use_encoder_type"]]["latent_dims"] + speed_hiddens[-1]
            if self.cfg["encoder_switch"]
            else None
        )

        if self.cfg["use_encoder_type"] == "vae":
            self.backbone = VAE(
                im_c=self.cfg["vae"]["im_c"],
                im_h=self.cfg["vae"]["im_h"],
                im_w=self.cfg["vae"]["im_w"],
                z_dim=self.cfg["vae"]["latent_dims"],
            )
            self.backbone.load_state_dict(
                torch.load(self.cfg["vae"]["vae_chkpt_statedict"], map_location=DEVICE)
            )
        else:
            raise NotImplementedError

        self.backbone.to(DEVICE)

    def set_params(self):
        self.save_episodes = True
        self.episode_num = 0
        self.best_ret = 0
        self.t = 0
        self.deterministic = False
        self.atol = 1e-3
        self.store_from_safe = False
        self.pi_scheduler = None
        self.t_start = 0
        self.best_pct = 0

        # This is important: it allows child classes (that extend this one) to "push up" information
        # that this parent class should log
        self.metadata = {}
        self.record = {"transition_actor": ""}

        self.action_space = Box(-1, 1, (2,))
        self.act_dim = self.action_space.shape[0]

        # Experience buffer
        self.replay_buffer = ReplayBuffer(
            obs_dim=self.feat_dim, act_dim=self.act_dim, size=self.cfg["replay_size"]
        )

        self.actor_critic = ActorCritic(
            self.obs_dim,
            self.action_space,
            self.cfg,
            latent_dims=self.obs_dim,
            device=DEVICE,
        )

        if self.cfg["checkpoint"] and self.cfg["load_checkpoint"]:
            self.load_model(self.cfg["checkpoint"])

        self.actor_critic_target = deepcopy(self.actor_critic)

    @staticmethod
    def load_model_config(path):
        yaml = YAML()
        params = yaml.load(open(path))
        sac_kwargs = params["agent_kwargs"]
        return sac_kwargs

    def setup_loggers(self):
        save_path = self.cfg["model_save_path"]
        loggers = setup_logging(save_path, self.cfg["experiment_name"], True)
        loggers[0]("Using random seed: {}".format(0))
        return loggers

    def compute_loss_q(self, data):
        """Set up function for computing SAC Q-losses."""
        o, a, r, o2, d = (
            data["obs"],
            data["act"],
            data["rew"],
            data["obs2"],
            data["done"],
        )

        q1 = self.actor_critic.q1(o, a)
        q2 = self.actor_critic.q2(o, a)

        # Bellman backup for Q functions
        with torch.no_grad():
            # Target actions come from *current* policy
            a2, logp_a2 = self.actor_critic.pi(o2)

            # Target Q-values
            q1_pi_targ = self.actor_critic_target.q1(o2, a2)
            q2_pi_targ = self.actor_critic_target.q2(o2, a2)
            q_pi_targ = torch.min(q1_pi_targ, q2_pi_targ)
            backup = r + self.cfg["gamma"] * (1 - d) * (
                q_pi_targ - self.cfg["alpha"] * logp_a2
            )

        # MSE loss against Bellman backup
        loss_q1 = (self.replay_buffer.weights * (q1 - backup) ** 2).mean()
        loss_q2 = (self.replay_buffer.weights * (q2 - backup) ** 2).mean()
        loss_q = loss_q1 + loss_q2

        # Useful info for logging
        q_info = dict(
            Q1Vals=q1.detach().cpu().numpy(), Q2Vals=q2.detach().cpu().numpy()
        )

        return loss_q, q_info

    def compute_loss_pi(self, data):
        """Set up function for computing SAC pi loss."""
        o = data["obs"]
        pi, logp_pi = self.actor_critic.pi(o)
        q1_pi = self.actor_critic.q1(o, pi)
        q2_pi = self.actor_critic.q2(o, pi)
        q_pi = torch.min(q1_pi, q2_pi)

        # Entropy-regularized policy loss
        loss_pi = (self.cfg["alpha"] * logp_pi - q_pi).mean()

        # Useful info for logging
        pi_info = dict(LogPi=logp_pi.detach().cpu().numpy())

        return loss_pi, pi_info

    def update(self, data):
        # First run one gradient descent step for Q1 and Q2
        self.q_optimizer.zero_grad()
        loss_q, q_info = self.compute_loss_q(data)
        loss_q.backward()
        self.q_optimizer.step()

        # Freeze Q-networks so you don't waste computational effort
        # computing gradients for them during the policy learning step.
        for p in self.q_params:
            p.requires_grad = False

        # Next run one gradient descent step for pi.
        self.pi_optimizer.zero_grad()
        loss_pi, pi_info = self.compute_loss_pi(data)
        loss_pi.backward()
        self.pi_optimizer.step()

        # Unfreeze Q-networks so you can optimize it at next DDPG step.
        for p in self.q_params:
            p.requires_grad = True

        # Finally, update target networks by polyak averaging.
        with torch.no_grad():
            for p, p_targ in zip(
                self.actor_critic.parameters(), self.actor_critic_target.parameters()
            ):
                # NB: We use an in-place operations "mul_", "add_" to update target
                # params, as opposed to "mul" and "add", which would make new tensors.
                p_targ.data.mul_(self.cfg["polyak"])
                p_targ.data.add_((1 - self.cfg["polyak"]) * p.data)


####

    def update_best_pct_complete(self, info):
        if self.best_pct < info["metrics"]["pct_complete"]:
            for cutoff in [93, 100]:
                if (self.best_pct < cutoff) & (
                    info["metrics"]["pct_complete"] >= cutoff
                ):
                    self.pi_scheduler.step()
            self.best_pct = info["metrics"]["pct_complete"]

    def checkpoint_model(self, ep_ret, n_eps):
        # Save if best (or periodically)
        if ep_ret > self.best_ret:  # and ep_ret > 100):
            path_name = f"{self.cfg['model_save_path']}/best_{self.cfg['experiment_name']}_episode_{n_eps}.statedict"
            self.file_logger(
                f"New best episode reward of {round(ep_ret, 1)}! Saving: {path_name}"
            )
            self.best_ret = ep_ret
            torch.save(self.actor_critic.state_dict(), path_name)
            path_name = f"{self.cfg['model_save_path']}/best_{self.cfg['experiment_name']}_episode_{n_eps}.statedict"
            try:
                # Try to save Safety Actor-Critic, if present
                torch.save(self.safety_actor_critic.state_dict(), path_name)
            except:
                pass

        elif self.cfg['save_freq'] > 0 and (n_eps + 1 % self.cfg["save_freq"] == 0):
            path_name = f"{self.cfg['model_save_path']}/{self.cfg['experiment_name']}_episode_{n_eps}.statedict"
            self.file_logger(
                f"Periodic save (save_freq of {self.cfg['save_freq']}) to {path_name}"
            )
            torch.save(self.actor_critic.state_dict(), path_name)
            path_name = f"{self.cfg['model_save_path']}/{self.cfg['experiment_name']}_episode_{n_eps}.statedict"
            try:
                # Try to save Safety Actor-Critic, if present
                torch.save(self.safety_actor_critic.state_dict(), path_name)
            except:
                pass


    def add_experience(
        self,
        action,
        camera,
        next_camera,
        done,
        env,
        feature,
        next_feature,
        info,
        reward,
        state,
        next_state,
        step,
    ):
        self.recording = {
            "step": step,
            "nearest_idx": env.nearest_idx,
            "camera": camera,
            "feature": feature.detach().cpu().numpy(),
            "state": state,
            "action_taken": action,
            "next_camera": next_camera,
            "next_feature": next_feature.detach().cpu().numpy(),
            "next_state": next_state,
            "reward": reward,
            "episode": self.episode_num,
            "stage": "training",
            "done": done,
            "transition_actor": self.record["transition_actor"],
            "metadata": info,
        }
        return self.recording

    def log_val_metrics_to_tensorboard(self, info, ep_ret, n_eps, n_val_steps):
        self.tb_logger.add_scalar("val/episodic_return", ep_ret, n_eps)
        self.tb_logger.add_scalar("val/ep_n_steps", n_val_steps, n_eps)

        try:
            self.tb_logger.add_scalar(
                "val/ep_pct_complete", info["metrics"]["pct_complete"], n_eps
            )
            self.tb_logger.add_scalar(
                "val/ep_total_time", info["metrics"]["total_time"], n_eps
            )
            self.tb_logger.add_scalar(
                "val/ep_total_distance", info["metrics"]["total_distance"], n_eps
            )
            self.tb_logger.add_scalar(
                "val/ep_avg_speed", info["metrics"]["average_speed_kph"], n_eps
            )
            self.tb_logger.add_scalar(
                "val/ep_avg_disp_err",
                info["metrics"]["average_displacement_error"],
                n_eps,
            )
            self.tb_logger.add_scalar(
                "val/ep_traj_efficiency",
                info["metrics"]["trajectory_efficiency"],
                n_eps,
            )
            self.tb_logger.add_scalar(
                "val/ep_traj_admissibility",
                info["metrics"]["trajectory_admissibility"],
                n_eps,
            )
            self.tb_logger.add_scalar(
                "val/movement_smoothness",
                info["metrics"]["movement_smoothness"],
                n_eps,
            )
        except:
            pass

        # TODO: Find a better way: requires knowledge of child class API :(
        if "safety_info" in self.metadata:
            self.tb_logger.add_scalar(
                "val/ep_interventions",
                self.metadata["safety_info"]["ep_interventions"],
                n_eps,
            )

    def log_train_metrics_to_tensorboard(self, ep_ret, t, t_start):
        self.tb_logger.add_scalar("train/episodic_return", ep_ret, self.episode_num)
        self.tb_logger.add_scalar(
            "train/ep_total_time",
            self.metadata["info"]["metrics"]["total_time"],
            self.episode_num,
        )
        self.tb_logger.add_scalar(
            "train/ep_total_distance",
            self.metadata["info"]["metrics"]["total_distance"],
            self.episode_num,
        )
        self.tb_logger.add_scalar(
            "train/ep_avg_speed",
            self.metadata["info"]["metrics"]["average_speed_kph"],
            self.episode_num,
        )
        self.tb_logger.add_scalar(
            "train/ep_avg_disp_err",
            self.metadata["info"]["metrics"]["average_displacement_error"],
            self.episode_num,
        )
        self.tb_logger.add_scalar(
            "train/ep_traj_efficiency",
            self.metadata["info"]["metrics"]["trajectory_efficiency"],
            self.episode_num,
        )
        self.tb_logger.add_scalar(
            "train/ep_traj_admissibility",
            self.metadata["info"]["metrics"]["trajectory_admissibility"],
            self.episode_num,
        )
        self.tb_logger.add_scalar(
            "train/movement_smoothness",
            self.metadata["info"]["metrics"]["movement_smoothness"],
            self.episode_num,
        )
        self.tb_logger.add_scalar("train/ep_n_steps", t - t_start, self.episode_num)