import torch
import torch.nn as nn
from src.deprecated.network_baselines import MLPCategoricalActor, MLPCritic, MLPGaussianActor, mlp, SquashedGaussianMLPActor
from enum import Enum
from gym.spaces import Box, Discrete

def resnet18(pretrained=True):
    model = torch.hub.load("pytorch/vision:v0.6.0", "resnet18", pretrained=pretrained)
    for param in model.parameters():
        param.requires_grad = False
    model.fc = nn.Identity()
    return model


class Qfunction(nn.Module):
    """
    Modified from the core MLPQFunction and MLPActorCritic to include a speed encoder
    """

    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        # pdb.set_trace()
        self.speed_encoder = mlp([1] + [8, 8])
        self.regressor = mlp([32 + 8 + 2] + [32, 64, 64, 32, 32] + [1])
        # self.lr = cfg['resnet']['LR']

    def forward(self, obs_feat, action):
        # if obs_feat.ndimension() == 1:
        #    obs_feat = obs_feat.unsqueeze(0)
        img_embed = obs_feat[..., :32]  # n x latent_dims
        speed = obs_feat[..., 32:]  # n x 1
        spd_embed = self.speed_encoder(speed)  # n x 16
        out = self.regressor(torch.cat([img_embed, spd_embed, action], dim=-1))  # n x 1
        # pdb.set_trace()
        return out.view(-1)


class DuelingNetwork(nn.Module):
    """
    Further modify from Qfunction to
        - Add an action_encoder
        - Separate state-dependent value and advantage
            Q(s, a) = V(s) + A(s, a)
    """

    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg

        self.speed_encoder = mlp([1] + [8, 8])
        self.action_encoder = mlp([2] + [64, 64, 32])

        n_obs = 32 + [8, 8][-1]
        # self.V_network = mlp([n_obs] + [32,64,64,32,32] + [1])
        self.A_network = mlp([n_obs + [64, 64, 32][-1]] + [32, 64, 64, 32, 32] + [1])
        # self.lr = cfg['resnet']['LR']

    def forward(self, obs_feat, action, advantage_only=False):
        # if obs_feat.ndimension() == 1:
        #    obs_feat = obs_feat.unsqueeze(0)
        img_embed = obs_feat[..., :32]  # n x latent_dims
        speed = obs_feat[..., 32:]  # n x 1
        spd_embed = self.speed_encoder(speed)  # n x 16
        action_embed = self.action_encoder(action)

        out = self.A_network(torch.cat([img_embed, spd_embed, action_embed], dim=-1))
        """
        if advantage_only == False:
            V = self.V_network(torch.cat([img_embed, spd_embed], dim = -1)) # n x 1
            out += V
        """
        return out.view(-1)

class CriticType(Enum):
    Q = 0
    Safety = 1
    Value = 2

class ActorCritic(nn.Module):
    def __init__(
        self,
        observation_space,
        action_space,
        cfg,
        activation=nn.ReLU,
        latent_dims=None,
        device="cpu",
        critic_type=CriticType.Value,  ## Flag to indicate architecture for Safety_actor_critic
    ):
        super().__init__()
        self.cfg = cfg
        obs_dim = observation_space.shape[0] if latent_dims is None else latent_dims
        act_dim = action_space.shape[0]
        act_limit = action_space.high[0]

        # build policy and value functions
        self.speed_encoder = mlp([1] + [8, 8])
        self.policy = SquashedGaussianMLPActor(
            obs_dim, act_dim, [64, 64, 32], activation, act_limit
        )
        if critic_type == CriticType.Safety:
            self.q1 = DuelingNetwork(cfg)
        elif critic_type == CriticType.Q:
            self.q1 = Qfunction(cfg)
            self.q2 = Qfunction(cfg)
        elif critic_type == CriticType.Value:
            self.v = Vfunction(cfg)
        
        self.device = device
        self.to(device)

    def pi(self, obs_feat, deterministic=False):
        # if obs_feat.ndimension() == 1:
        #    obs_feat = obs_feat.unsqueeze(0)
        img_embed = obs_feat[..., :32]  # n x latent_dims
        # speed = obs_feat[..., 32:]  # n x 1
        # spd_embed = self.speed_encoder(speed)  # n x 8
        feat = torch.cat(
            [
                img_embed,
            ],
            dim=-1,
        )
        return self.policy(feat, deterministic, True)

    def act(self, obs_feat, deterministic=False):
        # if obs_feat.ndimension() == 1:
        #    obs_feat = obs_feat.unsqueeze(0)
        with torch.no_grad():
            img_embed = obs_feat[..., :32]  # n x latent_dims
            # speed = obs_feat[..., 32:] # n x 1
            # raise ValueError(obs_feat.shape, img_embed.shape, speed.shape)
            # pdb.set_trace()
            # spd_embed = self.speed_encoder(speed) # n x 8
            feat = img_embed
            a, _ = self.policy(feat, deterministic, False)
            a = a.squeeze(0)
        return a.numpy() if self.device == "cpu" else a.cpu().numpy()


class Vfunction(nn.Module):
    """Modified from Qfunction."""
    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        # pdb.set_trace()
        self.speed_encoder = mlp([1] + [8, 8])
        self.regressor = mlp([32] + [32, 64, 64, 32, 32] + [1])

    def forward(self, obs_feat):
        # if obs_feat.ndimension() == 1:
        #    obs_feat = obs_feat.unsqueeze(0)
        img_embed = obs_feat[..., :32]  # n x latent_dims
        #speed = obs_feat[..., 32:]  # n x 1
        #spd_embed = self.speed_encoder(speed)  # n x 16
        out = self.regressor(torch.cat([img_embed], dim=-1))  # n x 1
        # pdb.set_trace()
        return out.view(-1)


class PPOMLPActorCritic(nn.Module):
    def __init__(self, observation_space, action_space,
                 hidden_sizes=(64,64), activation=nn.Tanh, device="cpu"):
        super().__init__()

        obs_dim = observation_space

        
        # obs_dim += self.cfg[self.cfg["use_encoder_type"]]["speed_hiddens"][-1]
        # policy builder depends on action space
        if isinstance(action_space, Box):
            self.pi = SquashedGaussianMLPActor(obs_dim, action_space.shape[0], [64, 64, 32], activation, action_space.high[0])
        # Discrete might not work
        elif isinstance(action_space, Discrete):
            self.pi = MLPCategoricalActor(obs_dim, action_space.n, hidden_sizes, activation)

        # build value function
        self.v  = MLPCritic(obs_dim, hidden_sizes, activation)

        self.to(device)
        self.device = device

    def step(self, obs):
        with torch.no_grad():
            pi = self.pi._distribution(obs)
            a = pi.sample()
            logp_a = self.pi._log_prob_from_distribution(pi, a)
            v = self.v(obs)
        return a.cpu().numpy(), v.cpu().numpy(), logp_a.cpu().numpy()

    def act(self, obs):
        return self.step(obs)[0]