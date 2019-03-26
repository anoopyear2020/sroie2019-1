import torch, sys, os, math
import torch.nn as nn
from torch.autograd import Function
import numpy as np
from torchvision.models import vgg16_bn
import omni_torch.networks.blocks as omth_blocks
import researches.ocr.textbox as init
from researches.ocr.textbox.tb_utils import *

cfg_300 = {
    'num_classes': 2,
    'feature_map_sizes': [38, 19, 10],
    'input_img_size': 300,
    'zoom_level': [8, 16],
    'box_height': [15, 25],
    'box_ratios': [[1, 2, 4, 7, 11, 16, 20], [1, 2, 5, 9, 14]],
    'box_height_large': [20, 32],
    'box_ratios_large': [[1, 2, 4, 7, 11, 15], [1, 3, 5, 8]],
    'stride': [1, 1],
    'loc_and_conf': [512, 512],
    'conv_output': ["conv_4", "conv_5"],
    'big_box': True,
    'variance': [0.1, 0.2],
    'var_updater': 1,
    'alpha': 1,
    'alpha_updater': 1,
    'overlap_thresh': 0.65,
    'clip': True,
    'name': 'VOC',
}

cfg = {
    'num_classes': 2,
    'feature_map_sizes': [64, 32, 32],
    'input_img_size': 512,
    'zoom_level': [8, 16, 32],
    'box_height': [16, 26, 36],
    'box_ratios': [[2, 4, 7, 11, 16, 20, 26], [1, 2, 5, 9, 14, 20], [1, 2, 5, 9, 12]],
    'box_height_large': [20, 34, 42],
    'box_ratios_large': [[1, 2, 4, 7, 11, 15, 20], [0.5, 1, 3, 6, 10, 15], [1, 3, 5, 9]],
    'stride': [1, 1, 1],
    'loc_and_conf': [512, 512, 1024],
    'conv_output': ["conv_4", "conv_5"],
    'big_box': True,
    'variance': [0.1, 0.2],
    'var_updater': 1,
    'alpha': 1,
    'alpha_updater': 1,
    'overlap_thresh': 0.6,
    'clip': True,
    'name': 'VOC',
}

class Detect(Function):
    """At test time, Detect is the final layer of SSD.  Decode location preds,
    apply non-maximum suppression to location predictions based on conf
    scores and threshold to a top_k number of output predictions for both
    confidence score and locations.
    """
    def __init__(self, num_classes, bkg_label, top_k, conf_thresh, nms_thresh):
        self.num_classes = num_classes
        self.background_label = bkg_label
        self.top_k = top_k
        # Parameters used in nms.
        self.nms_thresh = nms_thresh
        if nms_thresh <= 0:
            raise ValueError('nms_threshold must be non negative.')
        self.conf_thresh = conf_thresh
        self.variance = cfg['variance']

    def forward(self, loc_data, conf_data, prior_data):
        """
        Args:
            loc_data: (tensor) Loc preds from loc layers
                Shape: [batch,num_priors*4]
            conf_data: (tensor) Shape: Conf preds from conf layers
                Shape: [batch*num_priors,num_classes]
            prior_data: (tensor) Prior boxes and variances from priorbox layers
                Shape: [1,num_priors,4]
        """
        num = loc_data.size(0)  # batch size
        num_priors = prior_data.size(0)
        output = torch.zeros(num, self.num_classes, self.top_k, 5)
        conf_preds = conf_data.view(num, num_priors,
                                    self.num_classes).transpose(2, 1)

        # Decode predictions into bboxes.
        for i in range(num):
            decoded_boxes = decode(loc_data[i], prior_data, self.variance)
            # For each class, perform nms
            conf_scores = conf_preds[i].clone()

            for cl in range(1, self.num_classes):
                c_mask = conf_scores[cl].gt(self.conf_thresh)
                scores = conf_scores[cl][c_mask]
                if scores.size(0) == 0:
                    continue
                l_mask = c_mask.unsqueeze(1).expand_as(decoded_boxes)
                boxes = decoded_boxes[l_mask].view(-1, 4)
                # idx of highest scoring and non-overlapping boxes per class
                ids, count = nms(boxes, scores, self.nms_thresh, self.top_k)
                output[i, cl, :count] = \
                    torch.cat((scores[ids[:count]].unsqueeze(1),
                               boxes[ids[:count]]), 1)
        flt = output.contiguous().view(num, -1, 5)
        _, idx = flt[:, :, 0].sort(1, descending=True)
        _, rank = idx.sort(1)
        flt[(rank < self.top_k).unsqueeze(-1).expand_as(flt)].fill_(0)
        return output.cuda()


class SSD(nn.Module):
    def __init__(self, cfg, in_channel=512, batch_norm=nn.BatchNorm2d, test_phase=False):
        super().__init__()
        self.cfg = cfg
        self.num_classes = cfg['num_classes']
        self.output_list = cfg['conv_output']
        self.img_size = cfg['input_img_size']
        self.conv_module = nn.ModuleList([])
        self.loc_layers = nn.ModuleList([])
        self.conf_layers = nn.ModuleList([])
        self.conf_concate = nn.ModuleList([])
        self.conv_module_name = []
        self.softmax = nn.Softmax(dim=-1)
        self.detect = Detect(self.num_classes, 0, 200, 0.01, 0.45)
        self.test = test_phase
        self.prior = self.prior().cuda()

        # Prepare VGG-16 net with batch normalization
        vgg16_model = vgg16_bn(pretrained=True)
        net = list(vgg16_model.children())[0]
        # Replace the maxout with ceil in vanilla vgg16 net
        ceil_maxout = nn.MaxPool2d(kernel_size=2, stride=2, padding=0, dilation=1, ceil_mode=True)
        net = [ceil_maxout if type(n) is nn.MaxPool2d else n for n in net]

        # Basic VGG Layers
        self.conv_module_name.append("conv_1")
        self.conv_module.append(nn.Sequential(*net[:6]))
        self.conv_module_name.append("conv_2")
        self.conv_module.append(nn.Sequential(*net[6:13]))
        self.conv_module_name.append("conv_3")
        self.conv_module.append(nn.Sequential(*net[13:23]))
        self.conv_module_name.append("conv_4")
        self.conv_module.append(nn.Sequential(*net[23:33]))
        self.conv_module_name.append("conv_5")
        self.conv_module.append(nn.Sequential(*net[33:43]))

        # Extra Layers
        self.conv_module_name.append("extra_1")
        self.conv_module.append(omth_blocks.conv_block(in_channel, [1024, 1024],
                                                       kernel_sizes=[3, 1], stride=[1, 1],padding=[3, 0],
                                                       dilation=[3, 1], batch_norm=batch_norm))
        self.conv_module_name.append("extra_2")
        self.conv_module.append(omth_blocks.conv_block(1024, [256, 512], kernel_sizes=[1, 3],
                                                       stride=[1, 2], padding=[0, 1], batch_norm=batch_norm))

        # Location and Confidence Layer
        for i, in_channel in enumerate(cfg['loc_and_conf']):
            anchor = calculate_anchor_number(cfg, i)

            loc_layer = omth_blocks.conv_block(in_channel, filters=[in_channel, int(in_channel / 2), anchor * 4],
                                               kernel_sizes=[1, 3, 3], stride=[1, 1, cfg['stride'][i]], padding=[0, 1, 1], 
                                               activation=None)
            loc_layer.apply(init.init_cnn)
            self.loc_layers.append(loc_layer)
            conf_layer = omth_blocks.conv_block(in_channel, filters=[in_channel, int(in_channel / 2), anchor * 2],
                                               kernel_sizes=[1, 3, 3], stride=[1, 1, cfg['stride'][i]], padding=[0, 1, 1],
                                               activation=None)
            conf_layer.apply(init.init_cnn)
            self.conf_layers.append(conf_layer)
            """
            # produce the confidence after location offset was provided
            conf_concat = omth_blocks.conv_block(int(in_channel / 4) + anchor * 4,
                                                 filters=[int(in_channel / 4), anchor * 2], kernel_sizes=[3, 3],
                                                 stride=[1, cfg['stride'][i]], padding=[1, 1], activation=None)
            conf_concat.apply(init.init_cnn)
            self.conf_concate.append(conf_concat)
            
            self.loc_layers.append(nn.Conv2d(in_channel, anchor * 4, kernel_size=3,
                                             stride=cfg['stride'][i], padding=1))
            self.conf_layers.append(nn.Conv2d(in_channel, anchor * self.num_classes, kernel_size=3,
                                              stride=cfg['stride'][i], padding=1))
            """


    def parallel_prior(self):
        def generate_grid(h, w, f_k, n):
            x = np.expand_dims(np.linspace(0, h - 1, h), 0)
            y = np.expand_dims(np.linspace(0, w - 1, w), 0)
            x = np.repeat(x, w, axis=0).reshape((1, -1))
            y = np.repeat(y, h, axis=1)
            grid = np.concatenate([x, y], 0).transpose()
            grid = (grid + 0.5) / f_k
            grid = np.repeat(grid, n, axis=0)
            return grid

        priors = []
        for k, f in enumerate(self.cfg['feature_map_sizes']):
            n = (len(self.cfg['box_ratios'][k])) * (1, 2)[self.cfg['bidirection']] + \
                1 + (0, 1)[self.cfg['big_box']]
            f_k = self.img_size / self.cfg['zoom_level'][k]
            s_k = self.cfg['box_height'][k] / self.img_size
            s_k_big = math.sqrt(s_k * (self.cfg['box_height_large'][k] / self.img_size))
            if type(f) is list or type(f) is tuple:
                h, w = f[0], f[1]
            else:
                h, w = f, f
            center_grid = generate_grid(h, w, f_k, n)
            prior = np.tile(np.asarray([[s_k, s_k]]), (h * w * n, 1))
            ratios = [[1.0, 1.0]]
            if self.cfg['big_box']:
                ratios += [[s_k_big / s_k, s_k_big / s_k]]
            ratios += [[math.sqrt(ar), math.sqrt(1 / ar)] for ar in self.cfg['box_ratios'][k]]
            if self.cfg['bidirection']:
                ratios += [[math.sqrt(1 / ar), math.sqrt(ar)] for ar in self.cfg['box_ratios'][k]]
            ratios = np.tile(np.asarray(ratios), (h * w, 1))
            prior *= ratios
            priors.append(np.concatenate([center_grid, prior], axis=1))
            output = torch.from_numpy(np.concatenate(priors, axis=0)).float()
            if self.cfg['clip']:
                output.clamp_(max=1, min=0)
        return output

    def prior(self):
        from itertools import product as product
        mean = []
        big_box = self.cfg['big_box']
        for k in range(len(self.cfg['conv_output'])):
            shape = self.cfg['feature_map_sizes'][k]
            if type(shape) is list or type(shape) is tuple:
                assert len(shape) == 2, "feature map shape shoud be either scalar or 2d list or tuple"
                h, w = shape[0], shape[1]
            else:
                h, w = shape, shape
            if type(self.cfg['stride'][k]) is list or type(self.cfg['stride'][k]) is tuple:
                assert len(self.cfg['stride']) == 2, "feature map shape shoud be either scalar or 2d list or tuple"
                h_stride, w_stride = self.cfg['stride'][k][0], self.cfg['stride'][k][1]
            else:
                h_stride, w_stride = self.cfg['stride'][k], self.cfg['stride'][k]
            f_k = self.img_size / self.cfg['zoom_level'][k]
            s_k = self.cfg['box_height'][k] / self.img_size
            s_k_big = self.cfg['box_height_large'][k] / self.img_size
            for i, j in product(range(0, h, h_stride), range(0, w, w_stride)):
                cx = (j + 0.5) / f_k
                cy = (i + 0.5) / f_k
                # Apply different aspect ratio for small boxes
                for ar in self.cfg['box_ratios'][k]:
                    mean += [cx, cy, s_k * ar, s_k]
                if big_box:
                    for ar in self.cfg['box_ratios_large'][k]:
                        mean += [cx, cy, s_k_big * ar, s_k_big]
        # back to torch land
        output = torch.Tensor(mean).view(-1, 4)
        if self.cfg['clip']:
            output.clamp_(max=1, min=0)
        return output

    def forward(self, x, is_train=True, debug=False):
        locations, confidences, conv_output = [], [], []
        for i, conv_layer in enumerate(self.conv_module):
            x = conv_layer(x)
            if self.conv_module_name[i] in self.output_list:
                conv_output.append(x)
                if debug:
                    print("CNN output shape: %s" % (str(x.shape)))
                if len(conv_output) == len(self.output_list):
                    # Doesn't need any further conv operation
                    break
        for i, x in enumerate(conv_output):
            loc = self.loc_layers[i](x)
            locations.append(loc.permute(0, 2, 3, 1).contiguous().view(loc.size(0), -1, 4))
            #_loc = loc.detach()
            conf = self.conf_layers[i](x)
            #conf = torch.cat([conf, _loc], dim=1)
            #conf = self.conf_concate[i](conf)
            confidences.append(conf.permute(0, 2, 3, 1).contiguous().view(conf.size(0), -1, self.num_classes))
            if debug:
                print("Loc output shape: %s\nConf output shape: %s" % (str(loc.shape), str(conf.shape)))
        locations = torch.cat(locations, dim=1)
        confidences = torch.cat(confidences, dim=1)
        if is_train:
            output = [locations, confidences, self.prior]
        else:
            output = self.detect(locations, self.softmax(confidences), self.prior)
        return output


if __name__ == "__main__":
    x = torch.randn(2, 3, 512, 512).to("cuda")
    print(cfg)
    ssd = SSD(cfg).to("cuda")
    loc, conf, prior = ssd(x, debug=True)
    print(loc.shape)
    print(conf.shape)
    print(prior.shape)
