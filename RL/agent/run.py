'''

@author: Davide Nitti
'''

from . import common
from . import default_params
from . import torchagent
from . import agent_utils

import time
import numpy as np
import gym
import gym.spaces
from multiprocessing import Process
import logging
from vel.rl.vecenv.subproc import SubprocVecEnvWrapper
from vel.rl.vecenv.dummy import DummyVecEnvWrapper
from vel.rl.env.classic_atari import ClassicAtariEnv

import argparse
import os
import json

logger = logging.getLogger(__name__)


def loadparams(filename):
    with open(filename + ".json", "r") as input_file:
        out = json.load(input_file)
    return out


def getparams(params):
    parser = argparse.ArgumentParser()
    parser.add_argument('--name_exp', default="")
    parser.add_argument('--res_dir', default="out_dir")
    parser.add_argument('--target', default="Breakout-v0")  # LunarLander-v2 Breakout-v0
    parser.add_argument('--episodes', type=int, default=1000000)
    parser.add_argument('--plot', action='store_true', default=True, help='plot')
    parser.add_argument('--render', action='store_true', help='render')
    parser.add_argument('--monitor', action='store_true', help='monitor')
    parser.add_argument('--logging', default='INFO')
    parser.add_argument('--no_cuda', action='store_false', dest='use_cuda', default=True, help='disable cuda')
    args = parser.parse_args(params)
    options = vars(args)

    if options["name_exp"] != "":
        if not os.path.exists(args.res_dir):
            os.makedirs(args.res_dir)
        options["path_exp"] = os.path.join(options["res_dir"], options["name_exp"])
    else:
        options["path_exp"] = None

    if options["path_exp"] and os.path.exists(options["path_exp"] + ".json"):
        params = loadparams(options["path_exp"])
        # only this parameters are taken from args
        params['monitor'] = options['monitor']
        params['plot'] = options['plot']
        params['render'] = options['render']
        params['use_cuda'] = options['use_cuda']
    else:
        params = default_params.get_default(options['target'])
        params.update(options)

    return params


def start_process(func, args):
    p = Process(target=func, args=args)
    p.start()
    return p


def upload_res(callback, process_upload=None, upload_checkpoint=False, parallel=False):
    if callback is None:
        return None
    print('uploading')
    if parallel:
        if process_upload is not None:
            process_upload.join()
        process_upload = start_process(callback, (upload_checkpoint,))
    else:
        try:
            callback(upload_checkpoint)
        except Exception as e:
            print(str(e))
    return process_upload


def main(params=[], callback=None, upload_ckp=False, numavg=100, sleep=0.0):
    params = getparams(params)
    logger.info('params' + str(params))
    if params['plot'] != True:
        import matplotlib
        matplotlib.use('pdf')
    else:
        import matplotlib
        # matplotlib.use('Agg')
        # matplotlib.use("Qt5agg")
        import matplotlib.pyplot as plt

        plt.rcParams['image.interpolation'] = 'nearest'

    nameenv = params['target']

    # vec_env = DummyVecEnvWrapper(
    #     ClassicAtariEnv('BreakoutNoFrameskip-v4'), frame_history=4
    # ).instantiate(parallel_envs=1, seed=params["seed"])

    reward_threshold = gym.envs.registry.spec(nameenv).reward_threshold
    env = gym.make(nameenv)

    if params['monitor'] == True:  # store performance and video
        from gym import wrappers
        env = wrappers.Monitor(env, os.path.join(params['res_dir'], 'video'), force=True)

    if params["path_exp"]:
        log_file = params["path_exp"] + '.log'
    else:
        log_file = None

    common.init_logger(log_file, params['logging'])

    logger.info('params ' + str(params))
    logger.info(str(
        (env.observation_space, env.action_space, 'max_episode_steps', env.spec.max_episode_steps, env.reward_range)))
    for p in params:
        logger.debug(p + " " + str(params[p]))

    if params["seed"] > 0:
        env.seed(params["seed"])
        np.random.seed(params["seed"])
        logger.debug("seed " + str(params["seed"]))
    try:
        agent = torchagent.deepQconv(env.observation_space, env.action_space, env.reward_range, params)
        num_steps = env.spec.max_episode_steps
        avg = None
        process_upload = None
        if params['plot']:
            plt.ion()

        totrewlist = []
        greedyrewlist = [[], []]
        totrewavglist = []
        total_rew_discountlist = []
        testevery = 25
        useConv = agent.config['conv']
        max_total_rew_discount = float("-inf")
        max_abs_rew_discount = float("-inf")

        total_steps = 0
        start_updates = agent.config['num_updates']
        print(agent.config)
        start_episode = agent.config['start_episode']

        for episode in range(start_episode, params['episodes']):
            # to upload results (used for cloud)
            if episode > 1 and episode % 50 == 0:
                process_upload = upload_res(callback, process_upload, upload_ckp)
            if (episode + 1) % testevery == 0 or episode >= params['episodes'] - numavg:
                is_test = True
            else:
                is_test = False
            if is_test:
                render = (params['render'])
                eps = -1
                learn = False
                print(agent.config["path_exp"], 'episode', episode, 'l rate', agent.getlearnrate(), 'lambda',
                      agent.config['lambda'])
            else:
                render = False
                learn = True
                eps = episode
            startt = time.time()
            total_rew, steps, total_rew_discount, max_qval = agent_utils.do_rollout(agent, env, eps,
                                                                                    num_steps=num_steps,
                                                                                    render=render, useConv=useConv,
                                                                                    discount=agent.config["discount"],
                                                                                    sleep=sleep, learn=learn)
            stopt = time.time()
            max_total_rew_discount = max(max_total_rew_discount, total_rew_discount)
            max_abs_rew_discount = max(max_abs_rew_discount, abs(total_rew_discount))
            total_steps += steps

            if ((max_qval - max_total_rew_discount) / max_abs_rew_discount > 0.9):
                logger.warning("Q function too high: max rew disc  {:.3f}"
                               " max Q {:.3f} rel error {:.3f}".format(
                    max_total_rew_discount, max_qval,
                    (max_qval - max_total_rew_discount) / max_abs_rew_discount))

            if avg is None:
                avg = total_rew
            if is_test:
                greedyrewlist[0].append(total_rew / agent.config['scalereward'])
                greedyrewlist[1].append(episode)
                inc = max(0.2, 0.05 + 1. / (episode + 1.) ** 0.5)
                avg = avg * (1 - inc) + inc * total_rew
                totrewavglist.append(avg / agent.config['scalereward'])

            if episode % 10 == 0:
                print(agent.config)
            if (episode + 1 - start_episode) % 20 == 0:
                if agent.config["path_exp"] is not None:
                    print("saving...")
                    agent.config['start_episode'] = episode
                    try:
                        agent.save()
                    except KeyboardInterrupt:
                        agent.save()
                        exit()
            if episode % 1 == 0:
                totrewlist.append(total_rew / agent.config['scalereward'])

                total_rew_discountlist.append(total_rew_discount / agent.config['scalereward'])
            # print(avg,agent.config['scalereward'])
            logger.info(
                "episode {} t {:.2f}=100 steps {:6} reward {:.2f} disc_rew {:.2f} avg {:.2f}, avg100 {:.2f}, eps {:.3f} " \
                "updates {:8} tot-steps {:8} epoch {:.1f} lr {:.5f}".format(episode,
                                                                            (stopt - startt) / steps * 100., \
                                                                            steps,
                                                                            total_rew / agent.config['scalereward'],
                                                                            total_rew_discount / agent.config[
                                                                                'scalereward'],
                                                                            avg / agent.config['scalereward'],
                                                                            np.mean(np.array(totrewlist[-100:])), \
                                                                            agent.epsilon(eps),
                                                                            agent.config['num_updates'],
                                                                            total_steps,
                                                                            agent.config['num_updates'] / 50000,
                                                                            agent.getlearnrate()))
            if is_test and params['plot']:
                agent.plot([], (totrewlist, totrewavglist, greedyrewlist), reward_threshold, plt, plot=params['plot'],
                           numplot=1, start_episode=start_episode)

        print(agent.config)
    except Exception as e:
        print('Exception', e)
        env.close()
        raise e
    except KeyboardInterrupt:
        pass
    finally:
        env.close()
    return np.mean(totrewlist[-numavg:]), agent.config, totrewlist, totrewavglist, greedyrewlist, reward_threshold
