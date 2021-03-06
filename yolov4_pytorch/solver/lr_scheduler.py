# Copyright 2020 Lorna Authors. All Rights Reserved.
# Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
import math
from bisect import bisect_right
from copy import deepcopy
from typing import List

import torch
from torchvision.models.resnet import ResNet

from ..utils import is_parallel


def copy_attr(a, b, include=(), exclude=()):
    # Copy attributes from b to a, options to only include [...] and to exclude [...]
    for k, v in b.__dict__.items():
        if (len(include) and k not in include) or k.startswith('_') or k in exclude:
            continue
        else:
            setattr(a, k, v)


class CosineDecayLR(object):
    def __init__(self, optimizer, max_batches, lr, warmup):
        """ Cosine decay scheduler about all batches training.
        Args:
            optimizer (torch.optim): Stochastic gradient descent (optionally with momentum).
            max_batches (int): The maximum number of steps in the training process.
            lr (float): Learning rate.
            warmup (int): In the training begin, the lr is smoothly increase from 0 to lr_init,
                which means "warmup", this means warmup steps, if 0 that means don't use lr warmup.
        Example:
            >>> from torchvision.models.resnet import ResNet
            >>> optimizer = torch.optim.SGD(ResNet.parameters(), lr=0.1, momentum=0.9)
            >>> scheduler = CosineDecayLR(optimizer, 10000, 0.1, 0.0001, 400)
            >>> for epoch in range(50):
            >>>     for iters in range(200):
            >>>        scheduler.step(200 * epoch + iters)
        """
        super(CosineDecayLR, self).__init__()
        self.optimizer = optimizer
        self.max_bacthes = max_batches
        self.lr = lr
        self.lr_end = self.lr * 0.01
        self.warmup = warmup

    def step(self, iters):
        if self.warmup and iters < self.warmup:
            lr = self.lr / self.warmup * iters
        else:
            max_bacthes = self.max_bacthes - self.warmup
            iters = iters - self.warmup
            lr = self.lr_end + 0.5 * (self.lr - self.lr_end) * (
                    1 + math.cos(iters / max_bacthes * math.pi))
        for param_group in self.optimizer.param_groups:
            param_group["lr"] = lr


class ModelEMA:
    """ Model Exponential Moving Average from https://github.com/rwightman/pytorch-image-models
    Keep a moving average of everything in the model state_dict (parameters and buffers).
    This is intended to allow functionality like
    https://www.tensorflow.org/api_docs/python/tf/train/ExponentialMovingAverage
    A smoothed version of the weights is necessary for some training schemes to perform well.
    This class is sensitive where it is initialized in the sequence of model init,
    GPU assignment and distributed training wrappers.
    """

    def __init__(self, model, decay=0.9999, updates=0):
        # Create EMA
        self.ema = deepcopy(model.module if is_parallel(model) else model).eval()  # FP32 EMA
        # if next(model.parameters()).device.type != 'cpu':
        #     self.ema.half()  # FP16 EMA
        self.updates = updates  # number of EMA updates
        self.decay = lambda x: decay * (1 - math.exp(-x / 2000))  # decay exponential ramp (to help early epochs)
        for p in self.ema.parameters():
            p.requires_grad_(False)

    def update(self, model):
        # Update EMA parameters
        with torch.no_grad():
            self.updates += 1
            d = self.decay(self.updates)

            msd = model.module.state_dict() if is_parallel(model) else model.state_dict()  # model state_dict
            for k, v in self.ema.state_dict().items():
                if v.dtype.is_floating_point:
                    v *= d
                    v += (1. - d) * msd[k].detach()

    def update_attr(self, model, include=(), exclude=('process_group', 'reducer')):
        # Update EMA attributes
        copy_attr(self.ema, model, include, exclude)


# ------------------------------------------------------------------------------------------------------------- #
# Source from :https://github.com/facebookresearch/detectron2/blob/master/detectron2/solver/lr_scheduler.py --- #
# Modify by `Lornatang<liuchangyu1111@gmail.com>`
# ------------------------------------------------------------------------------------------------------------- #
class WarmupMultiStepLR(torch.optim.lr_scheduler._LRScheduler):
    def __init__(
            self,
            optimizer: torch.optim,
            milestones: List[int],
            gamma: float = 0.1,
            warmup_factor: float = 0.001,
            warmup_iters: int = 1000,
            warmup_method: str = "linear",
            last_epoch: int = -1,
    ):
        if not list(milestones) == sorted(milestones):
            raise ValueError(
                "Milestones should be a list of" " increasing integers. Got {}", milestones
            )
        self.base_lrs = None
        self.last_epoch = None
        self.milestones = milestones
        self.gamma = gamma
        self.warmup_factor = warmup_factor
        self.warmup_iters = warmup_iters
        self.warmup_method = warmup_method
        super().__init__(optimizer, last_epoch)

    def get_lr(self) -> List[float]:
        warmup_factor = _get_warmup_factor_at_iter(
            self.warmup_method, self.last_epoch, self.warmup_iters, self.warmup_factor
        )
        return [
            base_lr * warmup_factor * self.gamma ** bisect_right(self.milestones, self.last_epoch)
            for base_lr in self.base_lrs
        ]

    def _compute_values(self) -> List[float]:
        # The new interface
        return self.get_lr()


# ------------------------------------------------------------------------------------------------------------- #
# Source from :https://github.com/facebookresearch/detectron2/blob/master/detectron2/solver/lr_scheduler.py --- #
# Modify by `Lornatang<liuchangyu1111@gmail.com>`
# ------------------------------------------------------------------------------------------------------------- #
class WarmupCosineLR(torch.optim.lr_scheduler._LRScheduler):
    def __init__(
            self,
            optimizer: torch.optim,
            max_iters: int,
            warmup_factor: float = 0.001,
            warmup_iters: int = 1000,
            warmup_method: str = "linear",
            last_epoch: int = -1,
    ):
        self.base_lrs = None
        self.last_epoch = None
        self.max_iters = max_iters
        self.warmup_factor = warmup_factor
        self.warmup_iters = warmup_iters
        self.warmup_method = warmup_method
        super().__init__(optimizer, last_epoch)

    def get_lr(self) -> List[float]:
        warmup_factor = _get_warmup_factor_at_iter(
            self.warmup_method, self.last_epoch, self.warmup_iters, self.warmup_factor
        )
        # Different definitions of half-cosine with warmup are possible. For
        # simplicity we multiply the standard half-cosine schedule by the warmup
        # factor. An alternative is to start the period of the cosine at warmup_iters
        # instead of at 0. In the case that warmup_iters << max_iters the two are
        # very close to each other.
        return [
            base_lr
            * warmup_factor
            * 0.5
            * (1.0 + math.cos(math.pi * self.last_epoch / self.max_iters))
            for base_lr in self.base_lrs
        ]

    def _compute_values(self) -> List[float]:
        # The new interface
        return self.get_lr()


def _get_warmup_factor_at_iter(
        method: str, iters: int, warmup_iters: int, warmup_factor: float
) -> float:
    """
    Return the learning rate warmup factor at a specific iteration.
    See https://arxiv.org/abs/1706.02677 for more details.
    Args:
        method (str): warmup method; either "constant" or "linear".
        iters (int): iteration at which to calculate the warmup factor.
        warmup_iters (int): the number of warmup iterations.
        warmup_factor (float): the base warmup factor (the meaning changes according
            to the method used).
    Returns:
        float: the effective warmup factor at the given iteration.
    """
    if iters >= warmup_iters:
        return 1.0

    if method == "constant":
        return warmup_factor
    elif method == "linear":
        alpha = iters / warmup_iters
        return warmup_factor * (1 - alpha) + alpha
    else:
        raise ValueError("Unknown warmup method: {}".format(method))
