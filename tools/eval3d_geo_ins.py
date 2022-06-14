# This file is derived from [Atlas](https://github.com/magicleap/Atlas).
# Originating Author: Zak Murez (zak.murez.com)
# Modified for [PlanarRecon](https://github.com/neu-vi/PlanarRecon) by Yiming Xie.

# Original header:
# Copyright 2020 Magic Leap, Inc.

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import sys

sys.path.append('.')
import argparse
import json
import os

import numpy as np
import torch
import trimesh
from tools.simple_loader import *
from tools.evaluate_utils import *
import ray


torch.multiprocessing.set_sharing_strategy('file_system')


def parse_args():
    parser = argparse.ArgumentParser(description="NeuralRecon ScanNet Testing")
    parser.add_argument("--model", required=True, metavar="FILE",
                        help="path to checkpoint")
    parser.add_argument("--gt_path", metavar="DIR",
                        help="path to raw dataset", default='./data/scannet/planes_9')

    # ray config
    parser.add_argument('--n_proc', type=int, default=64, help='#processes launched to process scenes.')
    parser.add_argument('--n_gpu', type=int, default=1, help='#number of gpus')
    parser.add_argument('--num_workers', type=int, default=8)
    parser.add_argument('--loader_num_workers', type=int, default=8)
    return parser.parse_args()


args = parse_args()



def process(scene, total_scenes_index, total_scenes_count):
    save_path = args.model

    mesh_file = os.path.join(save_path, scene, 'planes_mesh.ply')
    mesh_file_eval = os.path.join(save_path, scene, 'planes_mesh_eval.ply')

    file_mesh_trgt = os.path.join(args.gt_path, scene, 'annotation', 'planes_mesh.ply')
    print(mesh_file)
    print(file_mesh_trgt)
    # eval 3d geometry
    metrics_mesh = eval_mesh(mesh_file_eval, file_mesh_trgt, error_map=False)
    metrics = {**metrics_mesh}

    rslt_file = os.path.join(save_path, '%s_metrics.json' % scene.replace('/', '-'))
    json.dump(metrics, open(rslt_file, 'w'))

    # prepare files for instance evaluation
    mesh_trgt = trimesh.load(file_mesh_trgt, process=False)
    mesh_pred = trimesh.load(mesh_file, process=False)
    
    pred_ins = np.load(os.path.join(save_path, scene, 'indices.npy'), allow_pickle=True)
    # _pred_ins = np.load(save_path + '/{}_planeIns.npy'.format(scene), allow_pickle=True)
    # pred_ins = _pred_ins.item().get('plane_ins')
    # conf = _pred_ins.item().get('conf')
    conf = None

    mesh_planeIns_transfer = project_to_mesh(mesh_pred, mesh_trgt, pred_ins, 'plane_ins')
    planeIns = mesh_planeIns_transfer.vertex_attributes['plane_ins']

    plnIns_save_pth = os.path.join(save_path, 'plane_ins')
    if not os.path.isdir(plnIns_save_pth):
        os.makedirs(plnIns_save_pth)
    
    mesh_planeIns_transfer.export(os.path.join(plnIns_save_pth, '%s_planeIns_transfer.ply' % scene))
    np.savetxt(plnIns_save_pth + '/%s.txt'%scene, planeIns, fmt='%d')
    # For RANSAC output, we have conf = None, and during eval we use all confidence = 1
    # export_instance_ids_for_eval(os.path.join(plnIns_save_pth, '%s.txt' % scene),
    #                              label_ids= np.ones_like(planeIns),
    #                              instance_ids= planeIns,
    #                              conf=conf
    #                              )

    return scene, metrics


@ray.remote(num_cpus=args.num_workers + 1, num_gpus=(1 / args.n_proc))
def process_with_single_worker(info_files):
    metrics = {}
    for i, info_file in enumerate(info_files):
        # if info_file == 'scene0704_00' or info_file == 'scene0702_01' or info_file == 'scene0702_00'or info_file == 'scene0702_02':
        #     continue
        # if not os.path.exists(os.path.join(args.model, info_file, 'planes_mesh.ply')):
        #     continue
        # if info_file.split('_')[1] != '00':
            # continue
        # if not os.path.exists(os.path.join(args.model, '{}_plane_only.ply'.format(info_file))):
            # continue

        scene, temp = process(info_file, i, len(info_files))
        if temp is not None:
            metrics[scene] = temp
    return metrics


def split_list(_list, n):
    assert len(_list) >= n
    ret = [[] for _ in range(n)]
    for idx, item in enumerate(_list):
        ret[idx % n].append(item)
    return ret


def main():
    all_proc = args.n_proc * args.n_gpu

    ray.init(num_cpus=all_proc * (args.num_workers + 1), num_gpus=args.n_gpu)

    val_file = open('/work/vig/Datasets/ScanNet/ScanNet/Tasks/Benchmark/scannetv2_val.txt', 'r')
    # val_file = open('./tools/scannetv1_val.txt', 'r')
    info_files = sorted(val_file.read().splitlines())
    # info_files = ['scene0100_00', 'scene0277_00', 'scene0559_00']

    info_files = split_list(info_files, all_proc)

    ray_worker_ids = []
    for w_idx in range(all_proc):
        ray_worker_ids.append(process_with_single_worker.remote(info_files[w_idx]))

    results = ray.get(ray_worker_ids)

    # results = process_with_single_worker(info_files)

    metrics = {}
    for r in results:
       metrics.update(r)

    met = {}
    for key,value in metrics.items():
        if len(met) == 0:
            for key2, value2 in value.items():
                met[key2] = value2
        else:
            for key2, value2 in value.items():
                met[key2] += value2
    for key,value in met.items():
        if type(value) != type([1,2]):
            met[key] = value / len(metrics)
    metrics = met


    # rslt_file = os.path.join(args.model, 'metrics_planenet.json')
    rslt_file = os.path.join(args.model, 'metrics.json')

    json.dump(str(metrics), open(rslt_file, 'w'))


if __name__ == "__main__":
    main()