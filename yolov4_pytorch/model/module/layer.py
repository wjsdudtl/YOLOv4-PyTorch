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

import torch
import torch.nn as nn
import yaml

from .common import Concat
from .common import Focus
from .conv import Conv
from .conv import ConvBNReLU
from .conv import CrossConv
from .conv import DWConv
from .conv import MixConv2d
from .conv import MobileNetConv
from .head import SPP
from .neck import Bottleneck
from .neck import BottleneckCSP
from .neck import ResNetBottleneck
from .pooling import Maxpool
from ..common import model_info
from ..fuse import fuse_conv_and_bn
from ...data.image import scale_image
from ...utils.common import make_divisible
from ...utils.device import time_synchronized
from ...utils.weights import initialize_weights


class Detect(nn.Module):
    def __init__(self, num_classes=80, anchors=()):  # detection layer
        super(Detect, self).__init__()
        self.stride = None  # strides computed during build
        self.num_classes = num_classes  # number of classes
        self.no = num_classes + 5  # number of outputs per anchor
        self.nl = len(anchors)  # number of detection layers
        self.num_anchors = len(anchors[0]) // 2  # number of anchors
        self.grid = [torch.zeros(1)] * self.nl  # init grid
        a = torch.tensor(anchors).float().view(self.nl, -1, 2)
        self.register_buffer('anchors', a)  # shape(nl,na,2)
        self.register_buffer('anchor_grid', a.clone().view(self.nl, 1, -1, 1, 1, 2))  # shape(nl,1,na,1,1,2)
        self.export = False  # onnx export

    def forward(self, x):
        # x = x.copy()  # for profiling
        z = []  # inference output
        self.training |= self.export
        for i in range(self.nl):
            bs, _, ny, nx = x[i].shape  # x(bs,255,20,20) to x(bs,3,20,20,85)
            x[i] = x[i].view(bs, self.num_anchors, self.no, ny, nx).permute(0, 1, 3, 4, 2).contiguous()

            if not self.training:  # inference
                if self.grid[i].shape[2:4] != x[i].shape[2:4]:
                    self.grid[i] = self._make_grid(nx, ny).to(x[i].device)

                y = x[i].sigmoid()
                y[..., 0:2] = (y[..., 0:2] * 2. - 0.5 + self.grid[i].to(x[i].device)) * self.stride[i]  # xy
                y[..., 2:4] = (y[..., 2:4] * 2) ** 2 * self.anchor_grid[i]  # wh
                z.append(y.view(bs, -1, self.no))

        return x if self.training else (torch.cat(z, 1), x)

    @staticmethod
    def _make_grid(nx=20, ny=20):
        yv, xv = torch.meshgrid([torch.arange(ny), torch.arange(nx)])
        return torch.stack((xv, yv), 2).view((1, 1, ny, nx, 2)).float()


class YOLO(nn.Module):
    def __init__(self, config_file='configs/COCO-Detection/yolov5s.yaml', channels=3, classes=None):
        super(YOLO, self).__init__()
        if type(config_file) is dict:
            self.config_file = config_file  # model dict
        else:  # is *.yaml
            with open(config_file) as f:
                self.config_file = yaml.load(f, Loader=yaml.FullLoader)  # model dict

        # Define model
        if classes:
            self.config_file['classes'] = classes  # override yaml value
        self.model, self.save = parse_model(self.config_file, channels=[channels])  # model, savelist, ch_out
        # print([x.shape for x in self.forward(torch.zeros(1, ch, 64, 64))])

        # Build strides, anchors
        m = self.model[-1]  # Detect()
        m.stride = torch.tensor([64 / x.shape[-2] for x in self.forward(torch.zeros(1, channels, 64, 64))])  # forward
        m.anchors /= m.stride.view(-1, 1, 1)
        self.stride = m.stride

        # Init weights, biases
        initialize_weights(self)
        self._initialize_biases()  # only run once
        model_info(self)
        print('')

    def forward(self, x, augment=False, profile=False):
        if augment:
            image_size = x.shape[-2:]  # height, width
            s = [0.83, 0.67]  # scales
            y = []
            for i, xi in enumerate((x,
                                    scale_image(x.flip(3), s[0]),  # flip-lr and scale
                                    scale_image(x, s[1]),  # scale
                                    )):
                # cv2.imwrite('img%g.jpg' % i, 255 * xi[0].numpy().transpose((1, 2, 0))[:, :, ::-1])
                y.append(self.forward_once(xi)[0])

            y[1][..., :4] /= s[0]  # scale
            y[1][..., 0] = image_size[1] - y[1][..., 0]  # flip lr
            y[2][..., :4] /= s[1]  # scale
            return torch.cat(y, 1), None  # augmented inference, train
        else:
            return self.forward_once(x, profile)  # single-scale inference, train

    def forward_once(self, x, profile=False):
        y, dt = [], []  # outputs
        for m in self.model:
            if m.f != -1:  # if not from previous layer
                x = y[m.f] if isinstance(m.f, int) else [x if j == -1 else y[j] for j in m.f]  # from earlier layers

            if profile:
                try:
                    import thop
                    o = thop.profile(m, inputs=(x,), verbose=False)[0] / 1E9 * 2  # FLOPS
                except:
                    o = 0
                t = time_synchronized()
                for _ in range(10):
                    _ = m(x)
                dt.append((time_synchronized() - t) * 100)
                print('%10.1f%10.0f%10.1fms %-40s' % (o, m.np, dt[-1], m.type))

            x = m(x)  # run
            y.append(x if m.i in self.save else None)  # save output

        if profile:
            print('%.1fms total' % sum(dt))
        return x

    def _initialize_biases(self, cf=None):  # initialize biases into Detect(), cf is class frequency
        # cf = torch.bincount(torch.tensor(np.concatenate(dataset.labels, 0)[:, 0]).long(), minlength=nc) + 1.
        module = self.model[-1]  # Detect() module
        for f, s in zip(module.f, module.stride):  #  from
            mi = self.model[f % module.i]
            b = mi.bias.view(module.num_anchors, -1)  # conv.bias(255) to (3,85)
            b[:, 4] += math.log(8 / (640 / s) ** 2)  # obj (8 objects per 640 image)
            b[:, 5:] += math.log(0.6 / (module.num_classes - 0.99)) if cf is None else torch.log(cf / cf.sum())  # cls
            mi.bias = torch.nn.Parameter(b.view(-1), requires_grad=True)

    def _print_biases(self):
        m = self.model[-1]  # Detect() module
        for f in sorted([x % m.i for x in m.f]):  #  from
            b = self.model[f].bias.detach().view(m.num_anchors, -1).T  # conv.bias(255) to (3,85)
            print(('%g Conv2d.bias:' + '%10.3g' * 6) % (f, *b[:5].mean(1).tolist(), b[5:].mean()))

    # def _print_weights(self):
    #     for m in self.model.modules():
    #         if type(m) is Bottleneck:
    #             print('%10.3g' % (m.w.detach().sigmoid() * 2))  # shortcut weights

    def fuse(self):  # fuse model Conv2d() + BatchNorm2d() layers
        print('Fusing layers...')
        for m in self.model.modules():
            if type(m) is Conv:
                m.conv = fuse_conv_and_bn(m.conv, m.bn)  # update conv
                m.bn = None  # remove batchnorm
                m.forward = m.fuseforward  # update forward
        model_info(self)


def parse_model(model_dict, channels):
    print('\n%3s%15s%3s%10s  %-40s%-30s' % ('', 'from', 'n', 'params', 'module', 'arguments'))
    anchors = model_dict["anchors"]
    num_classes = model_dict["num_classes"]
    depth_multiple = model_dict["depth_multiple"]
    width_multiple = model_dict["width_multiple"]
    num_anchors = (len(anchors[0]) // 2)  # number of anchors
    num_outputs = num_anchors * (num_classes + 5)  # number of outputs = anchors * (classes + 5)

    layers, save, out_channels = [], [], channels[-1]  # layers, savelist, out channels
    for i, (f, number, module, args) in enumerate(model_dict['backbone'] + model_dict['head']):
        module = eval(module) if isinstance(module, str) else module  # eval strings
        for j, a in enumerate(args):
            try:
                args[j] = eval(a) if isinstance(a, str) else a  # eval strings
            except:
                pass

        number = max(round(number * depth_multiple), 1) if number > 1 else number  # depth gain
        if module in [nn.Conv2d, Conv, Bottleneck, SPP, DWConv, MixConv2d, Focus, BottleneckCSP,
                      ResNetBottleneck, MobileNetConv, ConvBNReLU, CrossConv]:
            in_channels, out_channels = channels[f], args[0]

            # Normal
            # if i > 0 and args[0] != no:  # channel expansion factor
            #     ex = 1.75  # exponential (default 2.0)
            #     e = math.log(c2 / ch[1]) / math.log(2)
            #     c2 = int(ch[1] * ex ** e)
            # if m != Focus:
            out_channels = make_divisible(out_channels * width_multiple,
                                          8) if out_channels != num_outputs else out_channels

            # Experimental
            # if i > 0 and args[0] != no:  # channel expansion factor
            #     ex = 1 + gw  # exponential (default 2.0)
            #     ch1 = 32  # ch[1]
            #     e = math.log(c2 / ch1) / math.log(2)  # level 1-n
            #     c2 = int(ch1 * ex ** e)
            # if m != Focus:
            #     c2 = make_divisible(c2, 8) if c2 != no else c2

            args = [in_channels, out_channels, *args[1:]]
            if module is BottleneckCSP:
                args.insert(2, number)
                number = 1
        elif module is nn.BatchNorm2d:
            args = [channels[f]]
        elif module is Concat:
            out_channels = sum([channels[-1 if x == -1 else x + 1] for x in f])
        elif module is Detect:
            f = f or list(reversed([(-1 if j == i else j - 1) for j, x in enumerate(channels) if x == num_outputs]))
        elif module is Maxpool:
            kernel_size, strides = args[0], args[1]
            args = [kernel_size, strides]
        else:
            out_channels = channels[f]

        m_ = nn.Sequential(*[module(*args) for _ in range(number)]) if number > 1 else module(*args)  # module
        t = str(module)[8:-2].replace('__main__.', '')  # module type
        np = sum([x.numel() for x in m_.parameters()])  # number params
        m_.i, m_.f, m_.type, m_.np = i, f, t, np  # attach index, 'from' index, type, number params
        print('%3s%15s%3s%10.0f  %-40s%-30s' % (i, f, number, np, t, args))  # print
        save.extend(x % i for x in ([f] if isinstance(f, int) else f) if x != -1)  # append to savelist
        layers.append(m_)
        channels.append(out_channels)
    return nn.Sequential(*layers), sorted(save)
