import random
from readline import parse_and_bind
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

import utils
from encoder import make_encoder
import data_augs as rad

LOG_FREQ = 10000


def gaussian_logprob(noise, log_std):
    """Compute Gaussian log probability."""
    residual = (-0.5 * noise.pow(2) - log_std).sum(-1, keepdim=True)
    return residual - 0.5 * np.log(2 * np.pi) * noise.size(-1)


def squash(mu, pi, log_pi):
    """Apply squashing function.
    See appendix C from https://arxiv.org/pdf/1812.05905.pdf.
    """
    mu = torch.tanh(mu)
    if pi is not None:
        pi = torch.tanh(pi)
    if log_pi is not None:
        log_pi -= torch.log(F.relu(1 - pi.pow(2)) + 1e-6).sum(-1, keepdim=True)
    return mu, pi, log_pi


def weight_init(m):
    """Custom weight init for Conv2D and Linear layers."""
    if isinstance(m, nn.Linear):
        nn.init.orthogonal_(m.weight.data)
        m.bias.data.fill_(0.0)
    elif isinstance(m, nn.Conv2d) or isinstance(m, nn.ConvTranspose2d):
        # delta-orthogonal init from https://arxiv.org/pdf/1806.05393.pdf
        assert m.weight.size(2) == m.weight.size(3)
        m.weight.data.fill_(0.0)
        m.bias.data.fill_(0.0)
        mid = m.weight.size(2) // 2
        gain = nn.init.calculate_gain("relu")
        nn.init.orthogonal_(m.weight.data[:, :, mid, mid], gain)


class Actor(nn.Module):
    """MLP actor network."""

    def __init__(
        self,
        obs_shape,
        action_shape,
        hidden_dim,
        encoder_type,
        encoder_feature_dim,
        log_std_min,
        log_std_max,
        num_layers,
        num_filters,
    ):
        super().__init__()

        self.encoder = make_encoder(
            encoder_type,
            obs_shape,
            encoder_feature_dim,
            num_layers,
            num_filters,
            output_logits=True,
        )

        self.log_std_min = log_std_min
        self.log_std_max = log_std_max

        self.trunk = nn.Sequential(
            nn.Linear(self.encoder.feature_dim, hidden_dim),
            nn.ReLU(),
            nn.Linear(hidden_dim, hidden_dim),
            nn.ReLU(),
            nn.Linear(hidden_dim, 2 * action_shape[0]),
        )

        self.outputs = dict()
        self.apply(weight_init)

    def forward(self, obs, compute_pi=True, compute_log_pi=True, detach_encoder=False):
        obs = self.encoder(obs, detach=detach_encoder)

        mu, log_std = self.trunk(obs).chunk(2, dim=-1)

        # constrain log_std inside [log_std_min, log_std_max]
        log_std = torch.tanh(log_std)
        log_std = self.log_std_min + 0.5 * (self.log_std_max - self.log_std_min) * (
            log_std + 1
        )

        self.outputs["mu"] = mu
        self.outputs["std"] = log_std.exp()

        if compute_pi:
            std = log_std.exp()
            noise = torch.randn_like(mu)
            pi = mu + noise * std
        else:
            pi = None
            entropy = None

        if compute_log_pi:
            log_pi = gaussian_logprob(noise, log_std)
        else:
            log_pi = None

        mu, pi, log_pi = squash(mu, pi, log_pi)

        return mu, pi, log_pi, log_std

    def log(self, L, step, log_freq=LOG_FREQ):
        if step % log_freq != 0:
            return

        for k, v in self.outputs.items():
            L.log_histogram("train_actor/%s_hist" % k, v, step)

        L.log_param("train_actor/fc1", self.trunk[0], step)
        L.log_param("train_actor/fc2", self.trunk[2], step)
        L.log_param("train_actor/fc3", self.trunk[4], step)


class QFunction(nn.Module):
    """MLP for q-function."""

    def __init__(self, obs_dim, action_dim, hidden_dim):
        super().__init__()

        self.trunk = nn.Sequential(
            nn.Linear(obs_dim + action_dim, hidden_dim),
            nn.ReLU(),
            nn.Linear(hidden_dim, hidden_dim),
            nn.ReLU(),
            nn.Linear(hidden_dim, 1),
        )

    def forward(self, obs, action):
        assert obs.size(0) == action.size(0)

        obs_action = torch.cat([obs, action], dim=1)
        return self.trunk(obs_action)


class Critic(nn.Module):
    """Critic network, employes two q-functions."""

    def __init__(
        self,
        obs_shape,
        action_shape,
        hidden_dim,
        encoder_type,
        encoder_feature_dim,
        num_layers,
        num_filters,
    ):
        super().__init__()

        self.encoder = make_encoder(
            encoder_type,
            obs_shape,
            encoder_feature_dim,
            num_layers,
            num_filters,
            output_logits=True,
        )

        self.Q1 = QFunction(self.encoder.feature_dim, action_shape[0], hidden_dim)
        self.Q2 = QFunction(self.encoder.feature_dim, action_shape[0], hidden_dim)

        self.outputs = dict()
        self.apply(weight_init)

    def forward(self, obs, action, detach_encoder=False):
        # detach_encoder allows to stop gradient propogation to encoder
        obs = self.encoder(obs, detach=detach_encoder)

        q1 = self.Q1(obs, action)
        q2 = self.Q2(obs, action)

        self.outputs["q1"] = q1
        self.outputs["q2"] = q2

        return q1, q2

    def log(self, L, step, log_freq=LOG_FREQ):
        if step % log_freq != 0:
            return

        self.encoder.log(L, step, log_freq)

        for k, v in self.outputs.items():
            L.log_histogram("train_critic/%s_hist" % k, v, step)

        for i in range(3):
            L.log_param("train_critic/q1_fc%d" % i, self.Q1.trunk[i * 2], step)
            L.log_param("train_critic/q2_fc%d" % i, self.Q2.trunk[i * 2], step)


class CURL(nn.Module):
    """
    CURL
    """

    def __init__(
        self,
        obs_shape,
        z_dim,
        batch_size,
        critic,
        critic_target,
        output_type="continuous",
    ):
        super(CURL, self).__init__()
        self.batch_size = batch_size

        self.encoder = critic.encoder

        self.encoder_target = critic_target.encoder

        self.W = nn.Parameter(torch.rand(z_dim, z_dim))
        self.output_type = output_type

    def encode(self, x, detach=False, ema=False):
        """
        Encoder: z_t = e(x_t)
        :param x: x_t, x y coordinates
        :return: z_t, value in r2
        """
        if ema:
            with torch.no_grad():
                z_out = self.encoder_target(x)
        else:
            z_out = self.encoder(x)

        if detach:
            z_out = z_out.detach()
        return z_out

    # def update_target(self):
    #    utils.soft_update_params(self.encoder, self.encoder_target, 0.05)

    def compute_logits(self, z_a, z_pos):
        """
        Uses logits trick for CURL:
        - compute (B,B) matrix z_a (W z_pos.T)
        - positives are all diagonal elements
        - negatives are all other elements
        - to compute loss use multiclass cross entropy with identity matrix for labels
        """
        Wz = torch.matmul(self.W, z_pos.T)  # (z_dim,B)
        logits = torch.matmul(z_a, Wz)  # (B,B)
        logits = logits - torch.max(logits, 1)[0][:, None]
        return logits


class RadSacAgent(object):
    """RAD with SAC."""

    def __init__(
        self,
        obs_shape,
        action_shape,
        device,
        hidden_dim=256,
        discount=0.99,
        init_temperature=0.1,
        alpha_lr=1e-4,
        alpha_beta=0.9,
        actor_lr=1e-3,
        actor_beta=0.9,
        actor_log_std_min=-10,
        actor_log_std_max=2,
        actor_update_freq=2,
        critic_lr=1e-3,
        critic_beta=0.9,
        critic_tau=0.005,
        critic_target_update_freq=2,
        encoder_type="pixel",
        encoder_feature_dim=50,
        encoder_lr=1e-3,
        encoder_tau=0.05,
        num_layers=4,
        num_filters=32,
        cpc_update_freq=1,
        log_interval=100,
        detach_encoder=False,
        latent_dim=128,
        data_augs="",
        mode=None,
        prune_interval=None
    ):
        self.device = device
        self.discount = discount
        self.critic_tau = critic_tau
        self.encoder_tau = encoder_tau
        self.actor_update_freq = actor_update_freq
        self.critic_target_update_freq = critic_target_update_freq
        self.cpc_update_freq = cpc_update_freq
        self.log_interval = log_interval
        self.image_size = obs_shape[-1]
        self.latent_dim = latent_dim
        self.detach_encoder = detach_encoder
        self.encoder_type = encoder_type
        self.data_augs = data_augs
        self.mode = mode

        self.augs_funcs = {}

        if self.mode in ["warm_up", "tune", "prune"]:
            self.aug_to_func = {
                "grayscale": dict(func=rad.random_grayscale, params=dict(p=0.1)),
                "cutout": dict(func=rad.random_cutout, params=dict(min_cut=0, max_cut=10)),
                "cutout_color": dict(func=rad.random_cutout_color, params=dict(min_cut=0, max_cut=10)),
                "flip": dict(func=rad.random_flip, params=dict(p=0.1)),
                "rotate": dict(func=rad.random_rotation, params=dict(p=0.1)),
                "rand_conv": dict(func=rad.random_convolution, params=dict()),
                "color_jitter": dict(func=rad.random_color_jitter, params=dict(bright=0.1, contrast=0.1, satur=0.1, hue=0.1)),
                "center_crop": dict(func=rad.center_random_crop, params=dict(out=self.image_size - 2)),
                "translate_cc": dict(func=rad.translate_center_crop, params=dict(crop_sz=self.image_size - 2)),
                "kornia_jitter": dict(func=rad.kornia_color_jitter, params=dict(bright=0.1, contrast=0.1, satur=0.1, hue=0.1)),
                "in_frame_translate": dict(func=rad.in_frame_translate, params=dict(size=self.image_size + 2)),
                "crop_translate": dict(func=rad.crop_translate, params=dict(out=self.image_size - 2)),
                "center_crop_drac": dict(func=rad.center_crop_DrAC, params=dict(out=110)),
                "no_aug": dict(func=rad.no_aug, params=dict()),
            }
        else:
            self.aug_to_func = {
                "crop": dict(func=rad.random_crop, params=dict(out=84)),
                "grayscale": dict(func=rad.random_grayscale, params=dict(p=0.3)),
                "cutout": dict(func=rad.random_cutout, params=dict(min_cut=10, max_cut=30)),
                "cutout_color": dict(func=rad.random_cutout_color, params=dict(min_cut=10, max_cut=30)),
                "flip": dict(func=rad.random_flip, params=dict(p=0.2)),
                "rotate": dict(func=rad.random_rotation, params=dict(p=0.3)),
                "rand_conv": dict(func=rad.random_convolution, params=dict()),
                "color_jitter": dict(func=rad.random_color_jitter, params=dict(bright=0.4, contrast=0.4, satur=0.4, hue=0.5)),
                "translate": dict(func=rad.random_translate, params=dict(size=108)),
                "center_crop": dict(func=rad.center_random_crop, params=dict(out=96)),
                "translate_cc": dict(func=rad.translate_center_crop, params=dict(crop_sz=92)),
                "kornia_jitter": dict(func=rad.kornia_color_jitter, params=dict(bright=0.4, contrast=0.4, satur=0.4, hue=0.5)),
                "in_frame_translate": dict(func=rad.in_frame_translate, params=dict(size=128)),
                "crop_translate": dict(func=rad.crop_translate, params=dict(out=100)),
                "no_aug": dict(func=rad.no_aug, params=dict()),
                "center_crop_drac": dict(func=rad.center_crop_DrAC, params=dict(out=116))
            }

        if self.mode == "search":
            aug_grid_search_dict = {
                "cutout": [dict(min_cut=0, max_cut=20), dict(min_cut=20, max_cut=40), dict(min_cut=30, max_cut=50), dict(min_cut=40, max_cut=60)],
                "cutout_color": [dict(min_cut=0, max_cut=20), dict(min_cut=20, max_cut=40), dict(min_cut=30, max_cut=50), dict(min_cut=40, max_cut=60)],
                "color_jitter": [dict(bright=0.2, contrast=0.2, satur=0.2, hue=0.3), dict(bright=0.1, contrast=0.1, satur=0.1, hue=0.2),
                    dict(bright=0.5, contrast=0.5, satur=0.5, hue=0.6), dict(bright=0.6, contrast=0.6, satur=0.6, hue=0.7)],
                "center_crop": [dict(out=104), dict(out=80), dict(out=90), dict(out=75)],
                "translate_cc": [dict(out=104), dict(out=80), dict(out=90), dict(out=75)],
                "kornia_jitter": [dict(bright=0.2, contrast=0.2, satur=0.2, hue=0.3), dict(bright=0.1, contrast=0.1, satur=0.1, hue=0.2),
                    dict(bright=0.5, contrast=0.5, satur=0.5, hue=0.6), dict(bright=0.6, contrast=0.6, satur=0.6, hue=0.7)],
            }
        elif self.mode in ["warm_up", "tune", "prune"]:
            aug_grid_search_dict = {
                "flip": [dict(p=float(i/10)) for i in range(2,11)],
                "grayscale": [dict(p=float(i/10)) for i in range(2,11)],
                "rotate": [dict(p=float(i/10)) for i in range(2,11)],
                "cutout": [dict(min_cut=i*10, max_cut=i*10 + 10) for i in range(1, 11)],
                "cutout_color": [dict(min_cut=10*i, max_cut=10*i + 10) for i in range(1, 11)],
                "color_jitter": [dict(bright=i/10, contrast=i/10, satur=i/10, hue=i/10) for i in range(2, 11)],
                "center_crop": [dict(out=self.image_size-2*i) for i in range(2, 11)],
                "translate_cc": [dict(crop_sz=self.image_size-2*i) for i in range(2, 11)],
                "kornia_jitter": [dict(bright=i/10, contrast=i/10, satur=i/10, hue=i/10) for i in range(2, 11)],
                "in_frame_translate": [dict(size=self.image_size+2*i) for i in range(2, 11)],
                "crop_translate": [dict(out=self.image_size-2*i) for i in range(2, 11)],
                "center_crop_drac": [dict(out=self.image_size+2*i) for i in range(2, 11)]
            }

        for aug_name in self.data_augs.split("-"):
            assert aug_name in self.aug_to_func, "invalid data aug string"
            self.augs_funcs[aug_name] = self.aug_to_func[aug_name]
            self.aug_grid_search_dict[aug_name] = aug_grid_search_dict[aug_name]
        
        print(f'Aug set is: {self.data_augs}')

        if self.mode:
            self.prune_interval = prune_interval
            print(f'Prune PBA mode on! Setting: {self.mode}. With prune step: {prune_interval}')
            self.aug_score_dict = dict()
            for key, _ in self.augs_funcs.items():
                self.aug_score_dict[key] = 0
        else:
            print('Not PBA mode off...')
            self.aug_score_dict = None

        self.actor = Actor(
            obs_shape,
            action_shape,
            hidden_dim,
            encoder_type,
            encoder_feature_dim,
            actor_log_std_min,
            actor_log_std_max,
            num_layers,
            num_filters,
        ).to(device)

        self.critic = Critic(
            obs_shape,
            action_shape,
            hidden_dim,
            encoder_type,
            encoder_feature_dim,
            num_layers,
            num_filters,
        ).to(device)

        self.critic_target = Critic(
            obs_shape,
            action_shape,
            hidden_dim,
            encoder_type,
            encoder_feature_dim,
            num_layers,
            num_filters,
        ).to(device)

        self.critic_target.load_state_dict(self.critic.state_dict())

        # tie encoders between actor and critic, and CURL and critic
        self.actor.encoder.copy_conv_weights_from(self.critic.encoder)

        self.log_alpha = torch.tensor(np.log(init_temperature)).to(device)
        self.log_alpha.requires_grad = True
        # set target entropy to -|A|
        self.target_entropy = -np.prod(action_shape)

        # optimizers
        self.actor_optimizer = torch.optim.Adam(
            self.actor.parameters(), lr=actor_lr, betas=(actor_beta, 0.999)
        )

        self.critic_optimizer = torch.optim.Adam(
            self.critic.parameters(), lr=critic_lr, betas=(critic_beta, 0.999)
        )

        self.log_alpha_optimizer = torch.optim.Adam(
            [self.log_alpha], lr=alpha_lr, betas=(alpha_beta, 0.999)
        )

        if self.encoder_type == "pixel":
            # create CURL encoder (the 128 batch size is probably unnecessary)
            self.CURL = CURL(
                obs_shape,
                encoder_feature_dim,
                self.latent_dim,
                self.critic,
                self.critic_target,
                output_type="continuous",
            ).to(self.device)

            # optimizer for critic encoder for reconstruction loss
            self.encoder_optimizer = torch.optim.Adam(
                self.critic.encoder.parameters(), lr=encoder_lr
            )

            self.cpc_optimizer = torch.optim.Adam(self.CURL.parameters(), lr=encoder_lr)
        self.cross_entropy_loss = nn.CrossEntropyLoss()

        self.train()
        self.critic_target.train()

    def train(self, training=True):
        self.training = training
        self.actor.train(training)
        self.critic.train(training)
        if self.encoder_type == "pixel":
            self.CURL.train(training)

    @property
    def alpha(self):
        return self.log_alpha.exp()

    def select_action(self, obs):
        with torch.no_grad():
            obs = torch.FloatTensor(obs).to(self.device)
            obs = obs.unsqueeze(0)
            mu, _, _, _ = self.actor(obs, compute_pi=False, compute_log_pi=False)
            return mu.cpu().data.numpy().flatten()

    def sample_action(self, obs):
        if obs.shape[-1] != self.image_size:
            obs = utils.center_crop_image(obs, self.image_size)

        with torch.no_grad():
            obs = torch.FloatTensor(obs).to(self.device)
            obs = obs.unsqueeze(0)
            mu, pi, _, _ = self.actor(obs, compute_log_pi=False)
            return pi.cpu().data.numpy().flatten()
    

    def calculate_critic_loss(self, obs, action, reward, next_obs, not_done, L, step):
        with torch.no_grad():
            _, policy_action, log_pi, _ = self.actor(next_obs)
            target_Q1, target_Q2 = self.critic_target(next_obs, policy_action)
            target_V = torch.min(target_Q1, target_Q2) - self.alpha.detach() * log_pi
            target_Q = reward + (not_done * self.discount * target_V)

        # get current Q estimates
        current_Q1, current_Q2 = self.critic(
            obs, action, detach_encoder=self.detach_encoder
        )

        Q1_loss = F.mse_loss(current_Q1, target_Q)
        Q2_loss = F.mse_loss(current_Q2, target_Q)

        L.log("train/Q1 loss", Q1_loss, step)
        L.log("train/Q2 loss", Q2_loss, step)
        L.log("train/Mean Target Q", torch.mean(target_Q), step)
        L.log("train/Mean Q1", torch.mean(current_Q1), step)
        L.log('train/Mean Q2', torch.mean(current_Q2), step)

        return Q1_loss + Q2_loss


    def optimize_critic(self, loss, L, step):
        if step % self.log_interval == 0:
            L.log("train_critic/loss", loss, step)
        
        # Optimize the critic
        self.critic_optimizer.zero_grad()
        loss.backward()
        self.critic_optimizer.step()

        # TODD!!!
        # self.critic.log(L, step)


    def update_critic(self, obs, action, reward, next_obs, not_done, L, step):
        critic_loss = self.calculate_critic_loss(obs=obs, action=action, reward=reward, next_obs=next_obs, not_done=not_done, L=L, step=step)
        self.optimize_critic(loss=critic_loss, L=L, step=step)
    

    def update_actor_and_alpha(self, obs, L, step):
        # detach encoder, so we don't update it with the actor loss
        _, pi, log_pi, log_std = self.actor(obs, detach_encoder=True)
        actor_Q1, actor_Q2 = self.critic(obs, pi, detach_encoder=True)

        actor_Q = torch.min(actor_Q1, actor_Q2)
        actor_loss = (self.alpha.detach() * log_pi - actor_Q).mean()

        if step % self.log_interval == 0:
            L.log("train_actor/loss", actor_loss, step)
            L.log("train_actor/target_entropy", self.target_entropy, step)
        entropy = 0.5 * log_std.shape[1] * (1.0 + np.log(2 * np.pi)) + log_std.sum(
            dim=-1
        )
        if step % self.log_interval == 0:
            L.log("train_actor/entropy", entropy.mean(), step)

        # optimize the actor
        self.actor_optimizer.zero_grad()
        actor_loss.backward()
        self.actor_optimizer.step()
        
        # TODO!!!
        # self.actor.log(L, step)

        self.log_alpha_optimizer.zero_grad()
        alpha_loss = (self.alpha * (-log_pi - self.target_entropy).detach()).mean()
        if step % self.log_interval == 0:
            L.log("train_alpha/loss", alpha_loss, step)
            L.log("train_alpha/value", self.alpha, step)
        alpha_loss.backward()
        self.log_alpha_optimizer.step()

    def update_cpc(self, obs_anchor, obs_pos, cpc_kwargs, L, step):

        # time flips
        """
        time_pos = cpc_kwargs["time_pos"]
        time_anchor= cpc_kwargs["time_anchor"]
        obs_anchor = torch.cat((obs_anchor, time_anchor), 0)
        obs_pos = torch.cat((obs_anchor, time_pos), 0)
        """
        z_a = self.CURL.encode(obs_anchor)
        z_pos = self.CURL.encode(obs_pos, ema=True)

        logits = self.CURL.compute_logits(z_a, z_pos)
        labels = torch.arange(logits.shape[0]).long().to(self.device)
        loss = self.cross_entropy_loss(logits, labels)

        self.encoder_optimizer.zero_grad()
        self.cpc_optimizer.zero_grad()
        loss.backward()

        self.encoder_optimizer.step()
        self.cpc_optimizer.step()
        if step % self.log_interval == 0:
            L.log("train/curl_loss", loss, step)
    

    def run_not_PBA(self, replay_buffer, L, step):
        if step > 999:
            is_first = True
            idxs = None
            best_score = float('-inf')
            best_func_key = None
            best_obs = None
            best_next_obs = None
            for key, func in self.augs_funcs.items():
                func_dict = {key: func}
                if is_first:
                    obs, action, reward, next_obs, not_done, idxs = replay_buffer.sample_rad(func_dict, return_idxes=True)
                    is_first = False
                else:
                    obs, action, reward, next_obs, not_done = replay_buffer.sample_rad(func_dict, idxs=idxs)
                    
                score = self.calculate_critic_loss(obs=obs, action=action, reward=reward, next_obs=next_obs, not_done=not_done, L=L, step=step)
                    
                if score > best_score:
                    best_score = score
                    best_func_key = key
                    best_obs = obs
                    best_next_obs = next_obs

            self.optimize_critic(loss=best_score, L=L, step=step)

            if self.mode in ["unused", "search", "warm_up", "tune", "prune"]:
                del_key = None
                for key, val in self.aug_score_dict.items():
                    if key == best_func_key:
                        if self.mode == "prune":
                            self.aug_score_dict[key] += 1
                        else:
                            self.aug_score_dict[key] = 0
                    else:
                        if self.mode == "prune" and step % self.prune_interval == 0 and len(self.augs_funcs) > 1 and step > 1000:
                            worst_score = float('inf')
                            for key, score in self.aug_score_dict.items():
                                if score < worst_score:
                                    worst_score = score
                                    del_key = key
                        elif self.mode in ["unused", "search", "warm_up", "tune"]:
                            if val + 1 > self.prune_interval:
                                if del_key is None:
                                    del_key = key
                            else:
                                self.aug_score_dict[key] += 1

                if del_key is not None:
                    del self.aug_score_dict[del_key]
                    del self.augs_funcs[del_key]
                
                    if self.mode in ["search", "warm_up"]:
                        aug_keys = list()

                        for key in list(self.augs_funcs.keys()):
                            curr_key = key.split('/')[0]
                            
                            if curr_key not in aug_keys:
                                aug_keys.append(curr_key)

                        aug_params = False
                        sample_key = None

                        while not aug_params and len(aug_keys) > 0:
                            sample_key = random.sample(aug_keys, 1)[0]
                            aug_keys.remove(sample_key)
                            aug_params = self.aug_grid_search_dict.get(sample_key, False)

                        if aug_params:
                            if self.mode == "search":
                                sampled_param = random.sample(aug_params, 1)[0]
                            elif self.mode == "warm_up":
                                sampled_param = aug_params[0]

                            new_key = sample_key + '/' + str(sampled_param)
                            self.augs_funcs[new_key] = dict(func=self.aug_to_func[sample_key]['func'], params=sampled_param)

                            count = 0
                            sum = 0

                            for key in list(self.aug_score_dict):
                                if sample_key in key:
                                    count += 1
                                    sum += self.aug_score_dict[key]

                            self.aug_score_dict[new_key] = int(sum/count)
                            aug_params.remove(sampled_param)
                    elif self.mode in ["tune", "prune"]:
                        og_key_of_del = del_key.split('/')[0]
                        aug_params = self.aug_grid_search_dict.get(og_key_of_del, False)

                        if not aug_params and self.aug_grid_search_dict:
                            og_key_of_del = random.sample(list(self.aug_grid_search_dict.keys()), 1)[0]
                            aug_params = self.aug_grid_search_dict.get(og_key_of_del, False)

                        if aug_params:
                            param_selected = aug_params[0]
                            aug_params.remove(param_selected)
                            new_key = og_key_of_del + '/' + str(param_selected)
                            self.augs_funcs[new_key] = dict(func=self.aug_to_func[og_key_of_del]['func'], params=param_selected)

                            if self.mode == "tune":
                                self.aug_score_dict[new_key] = 0
                            else:
                                for key, _ in self.aug_grid_search_dict.items():
                                    self.aug_grid_search_dict[key] = 0
            else:
                self.aug_score_dict[best_func_key] += 1
        else:
            obs, action, reward, next_obs, not_done = replay_buffer.sample_rad(dict(no_aug=dict(func=rad.no_aug, params=dict())))

        return best_obs, action, reward, best_next_obs, not_done


    def update(self, replay_buffer, L, step):
        if self.mode:
            obs, action, reward, next_obs, not_done = self.run_not_PBA(replay_buffer=replay_buffer, L=L, step=step)
        else:
            if self.encoder_type == "pixel":
                obs, action, reward, next_obs, not_done = replay_buffer.sample_rad(
                    self.augs_funcs
                )
            else:
                obs, action, reward, next_obs, not_done = replay_buffer.sample_proprio()

            self.update_critic(obs, action, reward, next_obs, not_done, L, step)

        if step % self.log_interval == 0:
            L.log("train/batch_reward", reward.mean(), step)

        if step % self.actor_update_freq == 0:
            self.update_actor_and_alpha(obs, L, step)

        if step % self.critic_target_update_freq == 0:
            utils.soft_update_params(
                self.critic.Q1, self.critic_target.Q1, self.critic_tau
            )
            utils.soft_update_params(
                self.critic.Q2, self.critic_target.Q2, self.critic_tau
            )
            utils.soft_update_params(
                self.critic.encoder, self.critic_target.encoder, self.encoder_tau
            )

        # if step % self.cpc_update_freq == 0 and self.encoder_type == 'pixel':
        #    obs_anchor, obs_pos = cpc_kwargs["obs_anchor"], cpc_kwargs["obs_pos"]
        #    self.update_cpc(obs_anchor, obs_pos,cpc_kwargs, L, step)

    def save(self, model_dir, step):
        torch.save(self.actor.state_dict(), "%s/actor_%s.pt" % (model_dir, step))
        torch.save(self.critic.state_dict(), "%s/critic_%s.pt" % (model_dir, step))

    def save_curl(self, model_dir, step):
        torch.save(self.CURL.state_dict(), "%s/curl_%s.pt" % (model_dir, step))

    def load(self, model_dir, step):
        self.actor.load_state_dict(torch.load("%s/actor_%s.pt" % (model_dir, step)))
        self.critic.load_state_dict(torch.load("%s/critic_%s.pt" % (model_dir, step)))
