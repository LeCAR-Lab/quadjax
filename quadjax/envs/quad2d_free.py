import jax
import jax.numpy as jnp
from jax import lax
from gymnax.environments import environment, spaces
from typing import Tuple, Optional
import chex
from functools import partial
from dataclasses import dataclass as pydataclass
import tyro
import pickle
import time as time_module
from matplotlib.animation import FuncAnimation
import matplotlib.pyplot as plt
import numpy as np

import quadjax
from quadjax import controllers
from quadjax.dynamics import utils
from quadjax.dynamics.free import get_free_bodyrate_dynamics_2d
from quadjax.dynamics.dataclass import EnvParams2D, EnvState2D, Action2D

# for debug purpose
from icecream import install
install()


class Quad2D(environment.Environment):
    """
    JAX Compatible version of Quad2D-v0 OpenAI gym environment. 
    """

    def __init__(self, task: str = "tracking", dynamics: str = 'bodyrate'):
        super().__init__()
        self.task = task
        # reference trajectory function
        if task == "tracking":
            self.generate_traj = partial(utils.generate_lissa_traj, self.default_params.max_steps_in_episode, self.default_params.dt)
            self.reward_fn = utils.tracking_reward_fn
        elif task == "tracking_zigzag":
            self.generate_traj = partial(utils.generate_zigzag_traj, self.default_params.max_steps_in_episode, self.default_params.dt)
            self.reward_fn = utils.tracking_reward_fn
        else:
            raise NotImplementedError
        # dynamics function
        if dynamics == 'bodyrate':
            self.step_fn, self.dynamics_fn = get_free_bodyrate_dynamics_2d()
            self.get_obs = self.get_obs_quadonly
        else:
            raise NotImplementedError
        # equibrium point
        self.equib = jnp.zeros(5)
        # RL parameters
        self.action_dim = 2
        self.obs_dim = 29 + self.default_params.traj_obs_len * 4


    '''
    environment properties
    '''
    @property
    def default_params(self) -> EnvParams2D:
        """Default environment parameters for Quad2D-v0."""
        return EnvParams2D()

    '''
    key methods
    '''
    def step_env(
        self,
        key: chex.PRNGKey,
        state: EnvState2D,
        action: jnp.ndarray,
        params: EnvParams2D,
    ) -> Tuple[chex.Array, EnvState2D, float, bool, dict]:
        thrust = (action[0] + 1.0) / 2.0 * params.max_thrust
        roll_dot = action[2] * params.max_bodyrate
        env_action = Action2D(thrust=thrust, roll_dot=roll_dot)

        reward = self.reward_fn(state)

        next_state = self.step_fn(params, state, env_action)

        done = self.is_terminal(state, params)
        return (
            lax.stop_gradient(self.get_obs(next_state, params)),
            lax.stop_gradient(next_state),
            reward,
            done,
            {
                "discount": self.discount(next_state, params),
                "err_pos": jnp.linalg.norm(state.pos_tar - state.pos),
                "err_vel": jnp.linalg.norm(state.vel_tar - state.vel),
            },
        )

    def reset_env(
        self, key: chex.PRNGKey, params: EnvParams2D
    ) -> Tuple[chex.Array, EnvState2D]:
        """Reset environment state by sampling theta, theta_dot."""
        traj_key, pos_key, key = jax.random.split(key, 3)
        # generate reference trajectory by adding a few sinusoids together
        pos_traj, vel_traj = self.generate_traj(traj_key)
        pos_traj = pos_traj[..., :2]
        vel_traj = vel_traj[..., :2]
        zeros2 = jnp.zeros(2)
        state = EnvState2D(
            # drone
            pos=zeros2, vel=zeros2,
            roll=0.0, roll_dot=0.0, 
            # trajectory
            pos_tar=pos_traj[0],vel_tar=vel_traj[0],
            pos_traj=pos_traj,vel_traj=vel_traj,
            # debug value
            last_thrust=0.0,last_roll_dot=0.0,
            # step
            time=0,
        )
        return self.get_obs(state, params), state

    @partial(jax.jit, static_argnums=(0,))
    def sample_params(self, key: chex.PRNGKey) -> EnvParams2D:
        """Sample environment parameters."""
        # NOTE domain randomization disabled here

        # param_key = jax.random.split(key)[0]
        # rand_val = jax.random.uniform(param_key, shape=(9,), minval=0.0, maxval=1.0)

        m = 0.03
        I = 2.0e-5

        return EnvParams2D(m=m, I=I)
    
    @partial(jax.jit, static_argnums=(0,))
    def get_obs_quadonly(self, state: EnvState2D, params: EnvParams2D) -> chex.Array:
        """Return angle in polar coordinates and change."""
        # future trajectory observation
        traj_obs_len = self.default_params.traj_obs_len
        traj_obs_gap = self.default_params.traj_obs_gap
        # Generate the indices
        indices = state.time + 1 + jnp.arange(traj_obs_len) * traj_obs_gap
        obs_elements = [
            # drone
            *state.pos,
            *(state.vel / 4.0),
            state.roll,
            state.roll_dot / 40.0,  # 3*3+4=13
            # trajectory
            *(state.pos_tar),
            *(state.vel_tar / 4.0),  # 3*2=6
            *state.pos_traj[indices].flatten(), 
            *(state.vel_traj[indices].flatten() / 4.0), 
        ]  # 13+6=19
        obs = jnp.asarray(obs_elements)

        return obs

    @partial(jax.jit, static_argnums=(0,))
    def is_terminal(self, state: EnvState2D, params: EnvParams2D) -> bool:
        """Check whether state is terminal."""
        # Check number of steps in episode termination condition
        done = (state.time >= params.max_steps_in_episode) \
            | (jnp.abs(state.pos) > 3.0).any()
        return done


def test_env(env: Quad2D, controller, control_params, repeat_times = 1):
    # running environment
    rng = jax.random.PRNGKey(1)
    rng, rng_params = jax.random.split(rng)
    env_params = env.sample_params(rng_params)
    env_params = env.default_params # DEBUG

    state_seq, obs_seq, reward_seq = [], [], []
    rng, rng_reset = jax.random.split(rng)
    obs, env_state = env.reset(rng_reset, env_params)

    # DEBUG set iniiial state here
    # env_state = env_state.replace(quat = jnp.array([jnp.sin(jnp.pi/4), 0.0, 0.0, jnp.cos(jnp.pi/4)]))
                                  
    control_params = controller.update_params(env_params, control_params)
    n_dones = 0

    t0 = time_module.time()
    while n_dones < repeat_times:
        state_seq.append(env_state)
        rng, rng_act, rng_step = jax.random.split(rng, 3)
        action = controller(obs, env_state, env_params, rng_act, control_params)
        next_obs, next_env_state, reward, done, info = env.step(
            rng_step, env_state, action, env_params)
        if done:
            rng, rng_params = jax.random.split(rng)
            env_params = env.sample_params(rng_params)
            control_params = controller.update_params(env_params, control_params)
            n_dones += 1

        reward_seq.append(reward)
        obs_seq.append(obs)
        obs = next_obs
        env_state = next_env_state
    print(f"env running time: {time_module.time()-t0:.2f}s")

    t0 = time_module.time()
    utils.plot_states(state_seq, obs_seq, reward_seq, env_params)
    print(f"plotting time: {time_module.time()-t0:.2f}s")

    # save state_seq (which is a list of EnvState2D:flax.struct.dataclass)
    # get package quadjax path

    # plot animation
    def update_plot(i):
        # i = frame
        plt.gca().clear()
        pos_array = np.asarray([s.pos for s in state_seq])
        tar_array = np.asarray([s.pos_tar for s in state_seq])
        plt.plot(pos_array[:, 0], pos_array[:, 1], "b", alpha=0.5)
        plt.plot(tar_array[:, 0], tar_array[:, 1], "r--", alpha = 0.3)

        # quadrotor 0 with blue arrow
        plt.arrow(
            state_seq[i].pos[0],
            state_seq[i].pos[1],
            -0.1 * jnp.sin(state_seq[i].roll),
            0.1 * jnp.cos(state_seq[i].roll),
            width=0.01,
            color="g",
        )
        # plot y_tar and z_tar with red dot
        plt.plot(state_seq[i].pos_tar[0], state_seq[i].pos_tar[1], "ro")
        plt.xlabel("y")
        plt.ylabel("z")
        plt.xlim([-2, 2])
        plt.ylim([-2, 2])

    plt.figure(figsize=(4, 4))
    anim = FuncAnimation(plt.gcf(), update_plot, frames=len(state_seq), interval=1)
    anim.save(filename=f"{quadjax.get_package_path()}/../results/anim.gif", writer="imagemagick", fps=int(1.0/env_params.dt))
    
    with open(f"{quadjax.get_package_path()}/../results/state_seq.pkl", "wb") as f:
        pickle.dump(state_seq, f)

'''
reward function here. 
'''

@pydataclass
class Args:
    task: str = "tracking"
    dynamics: str = 'bodyrate'
    controller: str = 'lqr'

def main(args: Args):
    env = Quad2D(task=args.task, dynamics=args.dynamics)

    print("starting test...")
    # enable NaN value detection
    # from jax import config
    # config.update("jax_debug_nans", True)
    # with jax.disable_jit():
    if args.controller == 'lqr':
        control_params = controllers.LQRParams(
            Q = jnp.diag(jnp.ones(5)),
            R = 0.03 * jnp.diag(jnp.ones(2)),
            K = jnp.zeros((2, 5)),
        )
        controller = controllers.LQRController2D(env)
    elif args.controller == 'fixed':
        control_params = controllers.FixedParams(
            u = jnp.asarray([0.8, 0.0, 0.0, 0.0]),
        )
        controller = controllers.FixedController(env)
    else:
        raise NotImplementedError
    test_env(env, controller=controller, control_params=control_params, repeat_times=1)


if __name__ == "__main__":
    main(tyro.cli(Args))