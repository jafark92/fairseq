# Copyright (c) 2017-present, Facebook, Inc.
# All rights reserved.
#
# This source code is licensed under the license found in the LICENSE file in
# the root directory of this source tree. An additional grant of patent rights
# can be found in the PATENTS file in the same directory.

import pickle

import torch
from torch import distributed
from torch.distributed import group

from fairseq import utils


def is_master(args):
    return args.distributed_rank == 0


_use_c10d = [None]


def distributed_init(args):
    if args.distributed_world_size == 1:
        raise ValueError('Cannot initialize distributed with distributed_world_size=1')

    if _use_c10d[0] is None:
        _use_c10d[0] = not args.no_c10d

    if _use_c10d[0] and not hasattr(torch.nn.parallel, '_DistributedDataParallelC10d'):
        _use_c10d[0] = False
        print('WARNING: cannot find DistributedDataParallelC10d, '
              'falling back to standard DistributedDataParallel')

    print('| distributed init (rank {}): {}'.format(
        args.distributed_rank, args.distributed_init_method), flush=True)

    if _use_c10d[0]:
        distributed.c10d.init_process_group(
            backend=args.distributed_backend,
            init_method=args.distributed_init_method,
            world_size=args.distributed_world_size,
            rank=args.distributed_rank,
        )
    else:
        distributed.init_process_group(
            backend=args.distributed_backend,
            init_method=args.distributed_init_method,
            world_size=args.distributed_world_size,
            rank=args.distributed_rank,
        )

    if not is_master(args):
        suppress_output()

    return args.distributed_rank


def suppress_output():
    """Suppress printing on the current device. Force printing with `force=True`."""
    import builtins as __builtin__
    builtin_print = __builtin__.print

    def print(*args, **kwargs):
        if 'force' in kwargs:
            force = kwargs.pop('force')
            if force:
                builtin_print(*args, **kwargs)

    __builtin__.print = print


def get_rank():
    if _use_c10d[0]:
        return distributed.c10d.get_rank()
    else:
        return distributed.get_rank()


def get_world_size():
    if _use_c10d[0]:
        return distributed.c10d.get_world_size()
    else:
        return distributed.get_world_size()


def get_default_group():
    if _use_c10d[0]:
        return distributed.c10d.group.WORLD
    else:
        return distributed.group.WORLD


def all_reduce(tensor, group=None):
    if group is None:
        group = get_default_group()
    if _use_c10d[0]:
        return distributed.c10d.all_reduce(tensor, group=group)
    else:
        return distributed.all_reduce(tensor, group=group)


def all_gather_list(data, group=None, max_size=16384):
    """Gathers arbitrary data from all nodes into a list.

    Similar to :func:`~torch.distributed.all_gather` but for arbitrary Python
    data. Note that *data* must be picklable.

    Args:
        data (Any): data from the local worker to be gathered on other workers
        group (optional): group of the collective
        max_size (int, optional): maximum size of the data to be gathered
            across workers
    """
    rank = get_rank()
    world_size = get_world_size()

    buffer_size = max_size * world_size
    if not hasattr(all_gather_list, '_buffer') or \
            all_gather_list._buffer.numel() < buffer_size:
        all_gather_list._buffer = torch.cuda.ByteTensor(buffer_size)
    buffer = all_gather_list._buffer
    buffer.zero_()

    enc = pickle.dumps(data)
    enc_size = len(enc)
    if enc_size + 2 > max_size:
        raise ValueError('encoded data exceeds max_size: {}'.format(enc_size + 2))
    assert max_size < 255*256

    buffer_rank = buffer[rank * max_size : (rank + 1) * max_size]
    buffer_rank[0] = enc_size // 255  # this encoding works for max_size < 65k
    buffer_rank[1] = enc_size % 255
    buffer_rank[2:enc_size+2] = torch.ByteTensor(list(enc))

    all_reduce(buffer, group=group)

    result = []
    for i in range(world_size):
        out_buffer = buffer[i * max_size : (i + 1) * max_size]
        size = (255 * utils.item(out_buffer[0])) + utils.item(out_buffer[1])
        if size > 0:
            result.append(
                pickle.loads(bytes(out_buffer[2:size+2].tolist()))
            )
    return result