import jax
import chex
from flax import struct
from functools import partial
from jax import lax
from jax import numpy as jnp
import pickle
from jaxopt import ProjectedGradient
from jaxopt.projection import projection_hyperplane

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

class MPPIController(controllers.BaseController):
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
    def __call__(self, obs:jnp.ndarray, state, env_params, rng_act: chex.PRNGKey, control_params: MPPIParams, info = None) -> jnp.ndarray:
        # shift operator
        a_mean_old = control_params.a_mean
        a_cov_old = control_params.a_cov

        # jax.debug.print('a cov old {cov}', cov=a_cov_old)

        control_params = control_params.replace(a_mean=jnp.concatenate([a_mean_old[1:], a_mean_old[-1:]]),
                                                 a_cov=jnp.concatenate([a_cov_old[1:], a_cov_old[-1:]]))
        
        # sample action with mean and covariance, repeat for N times to get N samples with shape (N, H, action_dim)
        # a_mean shape (H, action_dim), a_cov shape (H, action_dim, action_dim)
        rng_act, act_key = jax.random.split(rng_act)
        act_keys = jax.random.split(act_key, self.N)

        def single_sample(key, traj_mean, traj_cov):
            keys = jax.random.split(key, self.H)
            return jax.vmap(lambda key, mean, cov: jax.random.multivariate_normal(key, mean, cov))(keys, traj_mean, traj_cov)
        # repeat single_sample N times to get N samples
        a_sampled = jax.vmap(single_sample, in_axes=(0, None, None))(act_keys, control_params.a_mean, control_params.a_cov)
        a_sampled = jnp.clip(a_sampled, -1.0, 1.0) # (N, H, action_dim)

        # def single_sample(key):
        #     return jax.random.multivariate_normal(key, control_params.a_mean.flatten(), jnp.eye(2*self.H)*0.04)
        # a_sampled_flattened = jax.vmap(single_sample)(act_keys)
        # # calculate the mean and covariance of the sampled actions
        # a_sampled = jnp.reshape(a_sampled_flattened, (self.N, self.H, 2))
        # a_sampled = jnp.clip(a_sampled, -1.0, 1.0) # (N, H, action_dim)

        # rollout to get reward with lax.scan
        rng_act, step_key = jax.random.split(rng_act)
        def rollout_fn(carry, action):
            state, params, reward_before, done_before = carry
            obs, state, reward, done, info = jax.vmap(lambda s, a, p: self.env.step_env_wocontroller(step_key, s, a, p))(state, action, params)
            reward = jnp.where(done_before, reward_before, reward)
            return (state, params, reward, done | done_before), (reward, state.pos)
        # repeat state each element to match the sample size N
        state_repeat = jax.tree_map(lambda x: jnp.repeat(jnp.asarray(x)[None, ...], self.N, axis=0), state)
        env_params_repeat = jax.tree_map(lambda x: jnp.repeat(jnp.asarray(x)[None, ...], self.N, axis=0), env_params)
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