import argparse
import torch
import os
import numpy as np
from gym.spaces import Box
from pathlib import Path
from torch.autograd import Variable
from tensorboardX import SummaryWriter
from utils.make_env import make_env
from utils.buffer import ReplayBuffer
from utils.env_wrappers import SubprocVecEnv
from algorithms.attention_sac import AttentionSAC


def make_parallel_env(env_id, n_rollout_threads, seed):
    def get_env_fn(rank):
        def init_env():
            env = make_env(env_id, discrete_action=True)
            env.seed(seed + rank * 1000)
            np.random.seed(seed + rank * 1000)
            return env
        return init_env
    return SubprocVecEnv([get_env_fn(i) for i in range(n_rollout_threads)])


def run(config):
    MODEL_DIR = Path('./models') / config.env_id / config.model_name
    run_num = 1
    if MODEL_DIR.exists():
        exst_run_nums = [int(str(folder.name).split('run')[1])
                         for folder in MODEL_DIR.iterdir() if str(folder.name).startswith('run')]
        run_num = 1 if len(exst_run_nums) == 0 else (max(exst_run_nums) + 1)
    CURR_RUN = f"run{run_num}"
    RUN_DIR = MODEL_DIR / CURR_RUN
    LOG_DIR = RUN_DIR / 'logs'
    os.makedirs(LOG_DIR)
    logger = SummaryWriter(str(LOG_DIR))

    torch.manual_seed(run_num)
    np.random.seed(run_num)
    env = make_parallel_env(
        config.env_id, max(config.n_rollout_threads, 2),
        run_num
    )
    model = AttentionSAC.init_from_env(
        env,
        tau=config.tau,
        pi_lr=config.pi_lr,
        q_lr=config.q_lr,
        gamma=config.gamma,
        pol_hidden_dim=config.pol_hidden_dim,
        critic_hidden_dim=config.critic_hidden_dim,
        attend_heads=config.attend_heads,
        reward_scale=config.reward_scale
    )
    # model = AttentionSAC.init_from_save(
    #     filename="models/fullobs_collect_treasure/test/run3/incremental/model_ep27001.pt",
    #     load_critic=True
    # )
    replay_buffer = ReplayBuffer(
        config.buffer_length,
        model.nagents,
        [obsp.shape[0] for obsp in env.observation_space],
        [acsp.shape[0] if isinstance(
            acsp, Box) else acsp.n for acsp in env.action_space]
    )

    t = 0
    for ep_i in range(0, config.n_episodes, config.n_rollout_threads):
        print(
            f"Episodes {ep_i + 1}-{ep_i + 1 + config.n_rollout_threads} of {config.n_episodes}")
        obs = env.reset()
        model.prep_rollouts()

        for _ in range(config.episode_length):
            # rearrange observations to be per agent, and convert to torch Variable
            torch_obs = [Variable(torch.Tensor(np.vstack(obs[:, i])), requires_grad=False)
                         for i in range(model.nagents)]
            # get actions as torch Variables
            torch_agent_actions = model.step(torch_obs, explore=True)
            # convert actions to numpy arrays
            agent_actions = [ac.data.numpy() for ac in torch_agent_actions]
            # rearrange actions to be per environment
            actions = [[ac[i] for ac in agent_actions]
                       for i in range(config.n_rollout_threads)]
            next_obs, rewards, dones, infos = env.step(actions)
            replay_buffer.push(obs, agent_actions, rewards, next_obs, dones)
            obs = next_obs
            t += config.n_rollout_threads

            if (len(replay_buffer) >= config.batch_size and
                    (t % config.steps_per_update) < config.n_rollout_threads):
                model.prep_training()
                for _ in range(config.num_updates):
                    sample = replay_buffer.sample(config.batch_size)
                    model.update_critic(sample, logger=logger)
                    model.update_policies(sample, logger=logger)
                    model.update_all_targets()
                model.prep_rollouts()

        ep_rews = replay_buffer.get_average_rewards(
            config.episode_length * config.n_rollout_threads)
        for a_i, a_ep_rew in enumerate(ep_rews):
            logger.add_scalar(
                f"agent{a_i}/mean_episode_rewards", a_ep_rew * config.episode_length, ep_i)

        if ep_i % config.save_interval < config.n_rollout_threads:
            model.prep_rollouts()
            os.makedirs(RUN_DIR / 'incremental', exist_ok=True)
            model.save(RUN_DIR / 'incremental' / f"model_ep{ep_i + 1}.pt")
            model.save(RUN_DIR / 'model.pt')

    model.save(RUN_DIR / 'model.pt')
    env.close()
    logger.export_scalars_to_json(str(LOG_DIR / 'summary.json'))
    logger.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument("env_id", help="Name of environment")
    parser.add_argument("model_name",
                        help="Name of directory to store " +
                             "model/training contents")
    parser.add_argument("--n_rollout_threads", default=12, type=int)
    parser.add_argument("--buffer_length", default=int(1e6), type=int)
    parser.add_argument("--n_episodes", default=50000, type=int)
    parser.add_argument("--episode_length", default=25, type=int)
    parser.add_argument("--steps_per_update", default=100, type=int)
    parser.add_argument("--num_updates", default=4, type=int,
                        help="Number of updates per update cycle")
    parser.add_argument("--batch_size",
                        default=1024, type=int,
                        help="Batch size for training")
    parser.add_argument("--save_interval", default=1000, type=int)
    parser.add_argument("--pol_hidden_dim", default=128, type=int)
    parser.add_argument("--critic_hidden_dim", default=128, type=int)
    parser.add_argument("--attend_heads", default=4, type=int)
    parser.add_argument("--pi_lr", default=0.001, type=float)
    parser.add_argument("--q_lr", default=0.001, type=float)
    parser.add_argument("--tau", default=0.001, type=float)
    parser.add_argument("--gamma", default=0.99, type=float)
    parser.add_argument("--reward_scale", default=100., type=float)

    config = parser.parse_args()

    run(config)
