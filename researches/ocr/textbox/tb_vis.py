import os, torch
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.patches as patches
import omni_torch.visualize.basic as vb
from matplotlib import gridspec
from researches.ocr.textbox.tb_utils import *

def print_box(red_boxes, shape=0, green_boxes=(), blue_boxes=(),
              img=None, idx=None, title=None):
    """
    Print three kind of boxes in different color on a canvas of shape: shape
    :param red_boxes:
    :param green_boxes:
    :param blue_boxes:
    :param shape:
    :return:
    """
    if type(shape) is tuple:
        h, w = shape[0], shape[1]
    else:
        h, w = shape, shape
    # img as white background image
    if img is None:
        img = np.zeros((h, w, 3)).astype(np.uint8) + 254
    else:
        img = img.astype(np.uint8)
        h, w, c = img.shape
    fig, ax = plt.subplots(figsize=(round(w / 100), round(h / 100)))
    ax.imshow(img)
    for box in red_boxes:
        x1, y1, x2, y2 = box[0], box[1], box[2] - box[0], box[3] - box[1]
        rect = patches.Rectangle((x1 * h, y1 * w), x2 * h, y2 * w, linewidth=1,
                                       edgecolor='r', facecolor='none', alpha=1)
        ax.add_patch(rect)
    for box in green_boxes:
        x1, y1, x2, y2 = box[0], box[1], box[2] - box[0], box[3] - box[1]
        rect = patches.Rectangle((x1 * h, y1 * w), x2 * h, y2 * w, linewidth=1,
                                       edgecolor='g', facecolor='none', alpha=0.7)
        ax.add_patch(rect)
    for box in blue_boxes:
        x1, y1, x2, y2 = box[0], box[1], box[2] - box[0], box[3] - box[1]
        rect = patches.Rectangle((x1 * h, y1 * w), x2 * h, y2 * w, linewidth=2,
                                       edgecolor='b', facecolor='none', alpha=0.7)
        ax.add_patch(rect)
    if title:
        plt.title(title)
    if idx is not None:
        plt.savefig(os.path.expanduser("~/Pictures/batch_%s_pred.jpg" % (idx)))
    else:
        plt.savefig(os.path.expanduser("~/Pictures/tmp.jpg"))
    plt.close()
    
def visualize_overlaps(cfg, target, label, prior, idx):
    images, subtitle, coords = [], [], []

    # conf中的1代表所有当前设置下与ground truth匹配的default box及其相应的index
    overlaps, conf = match(cfg, cfg['overlap_thresh'], target, prior,
                           None, label, None, None, 0, visualize=True)
    summary = "%s of %s positive samples"%(int(torch.sum(conf)), prior.size(0))
    crop_start = 0

    for k in range(len(cfg['conv_output'])):
        shape = cfg['feature_map_sizes'][k]
        if type(shape) is list or type(shape) is tuple:
            assert len(shape) == 2, "feature map shape shoud be either scalar or 2d list or tuple"
            h, w = shape[0], shape[1]
        else:
            h, w = shape, shape
        stride = cfg['stride'][k]
        if type(stride) is list or type(stride) is tuple:
            assert len(stride) == 2, "stride shape shoud be either scalar or 2d list or tuple"
            h_stride, w_stride = stride[0], stride[1]
        else:
            h_stride, w_stride = stride, stride
        anchor_num = calculate_anchor_number(cfg, k)
        feature_num = len(range(0, h, h_stride)) * len(range(0, w, w_stride)) * anchor_num
        #overlap = overlaps[:, crop_start: crop_start + feature_num]
        _conf = conf[crop_start: crop_start + feature_num]
        effective_sample = int(torch.sum(_conf))
        idx = _conf == 1
        idx = list(np.where(idx.cpu().numpy() == 1)[0])
        for i in idx:
            coords.append(point_form(prior[crop_start+i:crop_start+i+1, :]).clamp_(max=1, min=0).squeeze())
        #overlap = torch.max(overlap, 0)[0]
        _conf = _conf.view(len(range(0, h, h_stride)), len(range(0, w, w_stride)), anchor_num)
        _conf = _conf.permute(2, 0, 1)
        subs = ["ratio: %s"%(r) for r in cfg['box_ratios'][k]]
        subtitle.append("box height: %s\neffective samle: %s"
                        %(cfg['box_height'][k], effective_sample))
        if cfg['big_box']:
            subs += ["ratio: %s"%(r) for r in cfg['box_ratios_large'][k]]
            subtitle[-1] = "box height: %s and %s\neffective samle: %s" \
                           %(cfg['box_height'][k], cfg['box_height_large'][k], effective_sample)
        image = vb.plot_tensor(None, _conf.unsqueeze_(1) * 254, deNormalize=False,
                               sub_title=subs)
        images.append(image.astype(np.uint8))
        crop_start += feature_num
    return images, summary, subtitle, coords


def visualize_bbox(args, cfg, images, targets, prior=None, idx=0):
    print("Visualizing bound box...")
    batch = images.size(0)
    height, width = round(images.size(2) / 100) + 1, round(images.size(3) / 100)  * 2 + 1
    for i in range(batch):
        image = images[i:i+1, :, :, :]
        bbox = targets[i]

        image = vb.plot_tensor(args, image, deNormalize=True, margin=0).astype("uint8")
        h, w = image.shape[0], image.shape[1]
        # Create a Rectangle patch
        rects = []
        for point in bbox:
            x1, y1, x2, y2 = point[0], point[1], point[2] - point[0], point[3] - point[1]
            rects.append(patches.Rectangle((x1 * h, y1 * w), x2 * h, y2 * w, linewidth=1,
                                           edgecolor='r', facecolor='none'))
        if prior is not None:
            overlaps, summary, subtitle, coords = visualize_overlaps(cfg, bbox[:, :-1].data, bbox[:, -1].data, prior, i)
            for coord in coords:
                x1, y1, x2, y2 = coord[0], coord[1], coord[2] - coord[0], coord[3] - coord[1]
                rects.append(patches.Rectangle((x1 * h, y1 * w), x2 * h, y2 * w, linewidth=1,
                                               edgecolor='b', facecolor='none'))
        else:
            overlaps = []
            summary = ""
        fig, ax = plt.subplots(figsize=(width + len(overlaps), height))
        width_ratio = [2] + [1] * len(overlaps)
        gs = gridspec.GridSpec(1, 1+len(overlaps), width_ratios=width_ratio)
        ax0 = plt.subplot(gs[0])
        ax0.imshow(image)
        ax0.set_title(summary)
        for j in range(len(overlaps)):
            ax = plt.subplot(gs[j + 1])
            ax.imshow(overlaps[j])
            ax.set_title(subtitle[j])
        for rect in rects:
            ax0.add_patch(rect)
        plt.grid(False)
        plt.tight_layout()
        plt.savefig(os.path.expanduser("~/Pictures/batch_%s_sample_%s.jpg"%(idx, i)))
        plt.close()