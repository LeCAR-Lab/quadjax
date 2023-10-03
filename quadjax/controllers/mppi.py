import jax
import chex
from flax import struct
from functools import partial
from jax import lax
from jax import numpy as jnp
import pickle

from quadjax import controllers
from quadjax.dynamics import EnvParams2D, EnvState2D, geom
from quadjax.train import ActorCritic

@struct.dataclass
class MPPIParams:
    gamma_mean: float # mean of gamma
    gamma_sigma: float # std of gamma
    discount: float # discount factor
    sample_sigma: float # std of sampling

    a_mean: jnp.ndarray # mean of action
    a_cov: jnp.ndarray # covariance matrix of action

class MPPIController2D(controllers.BaseController):
    def __init__(self, env, control_params, N: int, H: int, lam: float) -> None:
        super().__init__(env, control_params)
        self.N = N # NOTE: N is the number of samples, set here as a static number
        self.H = H
        self.lam = lam
        # network = ActorCritic(2, activation='tanh')
        # self.apply_fn = network.apply
        # with open('/home/pcy/Research/quadjax/results/ppo_params_quad2d_free_tracking_zigzag_base.pkl', 'rb') as f:
        #     self.network_params = pickle.load(f)


    @partial(jax.jit, static_argnums=(0,))
    def __call__(self, obs:jnp.ndarray, state: EnvState2D, env_params: EnvParams2D, rng_act: chex.PRNGKey, control_params: MPPIParams) -> jnp.ndarray:
        # shift operator
        a_mean_old = control_params.a_mean
        a_cov_old = control_params.a_cov

        control_params = control_params.replace(a_mean=jnp.concatenate([a_mean_old[1:], a_mean_old[-1:]]),
                                                 a_cov=jnp.concatenate([a_cov_old[1:], a_cov_old[-1:]]))
        
        # # rollout with given controller to get action mean
        # rng_act, step_key = jax.random.split(rng_act)
        # def reference_rollout_fn(carry, action):
        #     obs, state, params = carry
        #     action = self.apply_fn(self.network_params, obs)[0].mean()
        #     obs, state, _, _, _ = self.env.step_env_wocontroller(step_key, state, action, params)
        #     return (obs, state, params), action
        # _, a_mean = lax.scan(reference_rollout_fn, (obs, state, env_params), None, length=self.H)
        # control_params = control_params.replace(a_mean=a_mean,
        #                                          a_cov=jnp.concatenate([a_cov_old[1:], a_cov_old[-1:]]))


        # sample action with mean and covariance, repeat for N times to get N samples with shape (N, H, action_dim)
        # a_mean shape (H, action_dim), a_cov shape (H, action_dim, action_dim)
        rng_act, act_key = jax.random.split(rng_act)
        def single_sample(key, traj_mean, traj_cov):
            return jax.vmap(lambda mean, cov: jax.random.multivariate_normal(key, mean, cov))(traj_mean, traj_cov)
        # repeat single_sample N times to get N samples
        act_keys = jax.random.split(rng_act, self.N)
        a_sampled = jax.vmap(single_sample, in_axes=(0, None, None))(act_keys, control_params.a_mean, control_params.a_cov)
        a_sampled = jnp.clip(a_sampled, -1.0, 1.0) # (N, H, action_dim)
        # rollout to get reward with lax.scan
        rng_act, step_key = jax.random.split(rng_act)
        def rollout_fn(carry, action):
            state, params, reward_before, done_before = carry
            obs, state, reward, done, info = jax.vmap(lambda s, a, p: self.env.step_env_wocontroller(step_key, s, a, p))(state, action, params)
            reward = jnp.where(done_before, reward_before, reward)
            return (state, params, reward, done | done_before), (reward, state.pos)
        # repeat state each element to match the sample size N
        state_repeat = jax.tree_map(lambda x: jnp.repeat(x[None, ...], self.N, axis=0), state)
        env_params_repeat = jax.tree_map(lambda x: jnp.repeat(x[None, ...], self.N, axis=0), env_params)
        done_repeat = jnp.full(self.N, False)
        reward_repeat = jnp.full(self.N, 0.0)

        _, (rewards, poses) = lax.scan(rollout_fn, (state_repeat, env_params_repeat, reward_repeat, done_repeat), a_sampled.transpose(1,0,2), length=self.H)
        # get discounted reward sum over horizon (axis=1)
        rewards = rewards.transpose(1,0) # (H, N) -> (N, H)
        discounted_rewards = jnp.sum(rewards * jnp.power(control_params.discount, jnp.arange(self.H)), axis=1, keepdims=False)
        # get cost
        cost = -discounted_rewards

        # get trajectory weight
        cost_exp = jnp.exp(-(cost-jnp.min(cost)) / self.lam)

        weight = cost_exp / jnp.sum(cost_exp)

        # update trajectory mean and covariance with weight
        a_mean = jnp.sum(weight[:, None, None] * a_sampled, axis=0) * control_params.gamma_mean + control_params.a_mean * (1 - control_params.gamma_mean)
        a_cov = jnp.sum(weight[:, None, None, None] * ((a_sampled - a_mean)[..., None] * (a_sampled - a_mean)[:, :, None, :]), axis=0) * control_params.gamma_sigma + control_params.a_cov * (1 - control_params.gamma_sigma)
        control_params = control_params.replace(a_mean=a_mean, a_cov=a_cov)

        # get action
        u = control_params.a_mean[0]

        # debug values
        info = {
            'pos_mean': jnp.mean(poses, axis=1), 
            'pos_std': jnp.std(poses, axis=1)
        }

        return u, control_params, info