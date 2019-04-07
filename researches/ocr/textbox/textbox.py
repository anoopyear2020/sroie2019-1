import os, time, sys, math, random, glob
sys.path.append(os.path.expanduser("~/Documents/sroie2019"))
import cv2, torch
import numpy as np
import torch.backends.cudnn as cudnn
import omni_torch.utils as util
import researches.ocr.textbox as init
import researches.ocr.textbox.tb_data as data
import researches.ocr.textbox.tb_preset as preset
import researches.ocr.textbox.tb_model as model
from researches.ocr.textbox.tb_loss import MultiBoxLoss
from researches.ocr.textbox.tb_utils import *
from researches.ocr.textbox.tb_preprocess import *
from researches.ocr.textbox.tb_augment import *
from researches.ocr.textbox.tb_vis import visualize_bbox, print_box
from omni_torch.networks.optimizer.adabound import AdaBound
import omni_torch.visualize.basic as vb


TMPJPG = os.path.expanduser("~/Pictures/tmp.jpg")
cfg = model.cfg
args = util.get_args(preset.PRESET)
if not torch.cuda.is_available():
    raise RuntimeError("Need cuda devices")

def fit(args, cfg, net, dataset, optimizer, is_train):
    def avg(list):
        return sum(list) / len(list)
    if is_train:
        net.train()
    else:
        net.eval()
    Loss_L, Loss_C = [], []
    accuracy, precision, recall, f1_score = [], [], [], []
    for epoch in range(args.epoches_per_phase):
        start_time = time.time()
        criterion = MultiBoxLoss(cfg, neg_pos=3)
        # Update variance and balance of loc_loss and conf_loss
        cfg['variance'] = [var * cfg['var_updater'] if var <= 0.95 else 1 for var in cfg['variance']]
        cfg['alpha'] *= cfg['alpha_updater']
        for batch_idx, (images, targets) in enumerate(dataset):
            #if not net.fix_size:
                #assert images.size(0) == 1, "batch size for dynamic input shape can only be 1 for 1 GPU RIGHT NOW!"
            images = images.cuda()
            ratios = images.size(3) / images.size(2)
            if ratios != 1.0:
                print(ratios)
            targets = [ann.cuda() for ann in targets]
            out = net(images, is_train)
            if args.curr_epoch == 0 and batch_idx == 0:
                visualize_bbox(args, cfg, images, targets, net.module.prior, batch_idx)
            if is_train:
                loss_l, loss_c = criterion(out, targets, ratios)
                loss = loss_l + loss_c
                Loss_L.append(float(loss_l.data))
                Loss_C.append(float(loss_c.data))
                optimizer.zero_grad()
                loss.backward()
                optimizer.step()
            else:
                if len(targets) == 0:
                    continue
                visualize = False
                if args.curr_epoch != 0 and args.curr_epoch % 10 == 0 and epoch==0:
                    visualize = True
                _accuracy, _precision, _recall, _f1_score = evaluate(images, out.data, targets, batch_idx,
                                                             visualize=visualize)
                accuracy.append(_accuracy)
                precision.append(_precision)
                recall.append(_recall)
                f1_score.append(_f1_score)
        if is_train:
            args.curr_epoch += 1
            print(" --- loc loss: %.4f, conf loss: %.4f, at epoch %04d, cost %.2f seconds ---" %
                  (avg(Loss_L), avg(Loss_C), args.curr_epoch + 1, time.time() - start_time))
    if not is_train:
        print(" --- accuracy: %.4f, precision: %.4f, recall %.4f, f1-score: %.4f  ---\n" %
              (avg(accuracy), avg(precision), avg(recall), avg(f1_score)))
        return avg(accuracy), avg(precision), avg(recall), avg(f1_score)
    else:
        return avg(Loss_L), avg(Loss_C)

def val(args, cfg, net, dataset, optimizer, prior):
    with torch.no_grad():
        fit(args, cfg, net, dataset, optimizer, prior, False)


def evaluate(img, detections, targets, batch_idx, visualize=False):
    idx = detections[0, 1, :, 0] >= 0.3
    text_boxes = detections[0, 1, idx, 1:]
    gt_boxes = targets[0][:, :-1].data
    accuracy, precision, recall = measure(text_boxes, gt_boxes)
    if (recall + precision) < 1e-3:
        f1_score = 0
    else:
        f1_score = 2 * (recall * precision) / (recall + precision)
    if visualize:
        pred = [[float(coor) for coor in area] for area in text_boxes]
        gt = [[float(coor) for coor in area] for area in gt_boxes]
        print_box(pred, green_boxes=gt, img=vb.plot_tensor(args, img, margin=0), idx=batch_idx)
    return accuracy, precision, recall, f1_score


def measure(pred_boxes, gt_boxes):
    if pred_boxes.size(0) == 0:
        return 0.0, 0.0, 0.0
    inter = intersect(pred_boxes, gt_boxes)
    text_area = get_box_size(pred_boxes)
    gt_area = get_box_size(gt_boxes)
    num_sample = max(text_area.size(0),  gt_area.size(0))
    accuracy = torch.sum(jaccard(pred_boxes, gt_boxes).max(0)[0]) / num_sample
    precision = torch.sum(inter.max(1)[0] / text_area) / num_sample
    recall = torch.sum(inter.max(0)[0] / gt_area) / num_sample
    return float(accuracy), float(precision), float(recall)


def test_rotation():
    def return_aug(transform_det, height_ori, width_ori, height, width):
        aug_list = []
        aug_list.append(
            augmenters.Resize(size={"height": height, "width": width})
        )
        if "crop" in transform_det:
            top_crop, right_crop, bottom, left = transform_det["crop"]
            left = (left + width/2) / width_ori
            bottom = (bottom + height/2) / height_ori
            aug_list.append(
                augmenters.PadToFixedSize(width=width_ori, height=height_ori, position=(left, bottom)),
            )
        if "rotation" in transform_det:
            aug_list.append(
                augmenters.Affine(rotate=-transform_det["rotation"], cval=args.aug_bg_color, fit_output=True),
            )
        aug = augmenters.Sequential(aug_list, random_order=False)
        return aug
    def extract_boxes(bboxes, h, w, box_label):
        coords = []
        #h, w = image.shape[0], image.shape[1]
        for i, bbox in enumerate(bboxes.bounding_boxes):
            condition_1 = bbox.x1 <= 0 and bbox.x2 <= 0
            condition_2 = bbox.y1 <= 0 and bbox.y2 <= 0
            condition_3 = bbox.x1 >= w - 1 and bbox.x2 >= w - 1
            condition_4 = bbox.y1 >= h - 1 and bbox.y2 >= h - 1
            if condition_1 or condition_2 or condition_3 or condition_4:
                # Eliminate bboxes outside the image
                continue
            horizontal_constrain = lambda x: max(min(w, x), 0)
            vertival_constrain = lambda y: max(min(h, y), 0)
            coords.append([horizontal_constrain(bbox.x1) / w, vertival_constrain(bbox.y1) / h,
                           horizontal_constrain(bbox.x2) / w, vertival_constrain(bbox.y2) / h, box_label[i]])
        return coords
    import imgaug
    from imgaug import augmenters
    net = model.SSD(cfg, connect_loc_to_conf=True, fix_size=False)
    net = net.cuda()
    net_dict = net.state_dict()
    weight_dict = util.load_latest_model(args, net, prefix="cv_1", return_state_dict=True)
    for key in weight_dict.keys():
        net_dict[key[7:]] = weight_dict[key]
    net.load_state_dict(net_dict)
    img_list = glob.glob(os.path.expanduser("~/Pictures/sroie_new/*.jpg"))
    for i, img_file in enumerate(sorted(img_list)):
        # Get img and bbox infomation from local file
        img, bbox, box_label = data.extract_bbox(args, [img_file, img_file[:-4] + ".txt"], None, None)
        height_ori, width_ori = img.shape[0], img.shape[1]
        # detect rotation and crop area and save it for returning the image back
        img, transform_det = estimate_angle_and_crop_area(img, args, None, None, None)
        if "rotation" in transform_det:
            rot_aug = augmenters.Affine(rotate=transform_det["rotation"],
                                         cval=args.aug_bg_color, fit_output=True)
        else:
            rot_aug = None
        if "crop" in transform_det:
            crop_aug = [augmenters.Crop(px=transform_det["crop"], keep_size=False)]
        else:
            crop_aug = None
        # Augment img and bbox, even if rotation exists, we only rotate img not bbox
        if rot_aug:
            rot_aug = augmenters.Sequential(rot_aug, random_order=False)
            rot_bbox = rot_aug.augment_bounding_boxes([bbox])[0]
            image = rot_aug.augment_image(img)
        else:
            image = img
            rot_bbox = None
        if crop_aug:
            crop_aug = augmenters.Sequential(crop_aug, random_order=False)
            crop_aug = crop_aug.to_deterministic()
            image = crop_aug.augment_image(image)
            bbox = crop_aug.augment_bounding_boxes([bbox])[0]
            if rot_bbox:
                rot_bbox = crop_aug.augment_bounding_boxes([rot_bbox])[0]
        height, width = image.shape[0], image.shape[1]
        # Resize the image to a number divideable by GCD
        # So as to estimate the feature map size
        gcd = 32
        height_resize = round(height / gcd) * gcd
        width_resize = round(width / gcd) * gcd
        resize_aug =augmenters.Sequential([
            augmenters.Resize(size={"height": 1472, "width": 512}),
            augmenters.CropToFixedSize(height=128, width=512),
        ])
        resize_aug = resize_aug.to_deterministic()
        image = resize_aug.augment_image(image)
        bbox = resize_aug.augment_bounding_boxes([bbox])[0]
        if rot_bbox:
            rot_bbox = resize_aug.augment_bounding_boxes([rot_bbox])[0]
        # Get the final size of resized input image
        height_final, width_final = image.shape[0], image.shape[1]
        # Generate prior boxes according to the input image size
        cfg["feature_map_sizes"] = [[height_final/8, width_final/8], [height_final/16, width_final/16],
                                     [height_final/32, width_final/32]]
        net.prior = net.create_prior(input_size=(height_final, width_final)).cuda()
        # Collect bboxes inside the image
        coord = extract_boxes(bbox, height_final, width_final, box_label)
        rot_coord = extract_boxes(rot_bbox, height_final, width_final, box_label)

        # Prepare image tensor and test
        image = torch.Tensor(util.normalize_image(args, image)).unsqueeze(0)
        image = image.permute(0, 3, 1, 2).cuda()
        visualize_bbox(args, cfg, image, [torch.Tensor(rot_coord).cuda()], net.prior, height_final/width_final)
        b, c, h, w = image.shape
        out = net(image, is_train=False)

        # Extract the predicted bboxes
        idx = out.data[0, 1, :, 0] >= 0.4
        text_boxes = out.data[0, 1, idx, 1:]
        scale = torch.Tensor([h, w, h, w]).unsqueeze(0).repeat(text_boxes.size(0), 1)
        text_boxes = text_boxes.cpu() * scale

        r_aug = return_aug(transform_det, height_ori, width_ori, height, width)
        r_aug = r_aug.to_deterministic()
        image = r_aug.augment_image(image)
        bbox = r_aug.augment_bounding_boxes([bbox])[0]

        pred_bbox = [imgaug.imgaug.BoundingBox([float(coor) for coor in area]) for area in text_boxes]
        BBox = imgaug.imgaug.BoundingBoxesOnImage(BBox, shape=img.shape)
        bbox_aug = crop_aug.augment_bounding_boxes(bbox)

        #print_box(pred, img=img, idx=i)


def main():
    if args.fix_size:
        aug = aug_sroie()
    else:
        aug = aug_sroie_dynamic_2()
        args.batch_size = torch.cuda.device_count()
    datasets = data.fetch_detection_data(args, sources=args.train_sources, k_fold=1,
                                         batch_size=args.batch_size, batch_size_val=1,
                                         auxiliary_info=args.train_aux, split_val=0.2,
                                         pre_process=None, aug=aug)
    for idx, (train_set, val_set) in enumerate(datasets):
        loc_loss, conf_loss = [], []
        accuracy, precision, recall, f1_score = [], [], [], []
        print("\n =============== Cross Validation: %s/%s ================ " %
              (idx + 1, len(datasets)))
        net = model.SSD(cfg, connect_loc_to_conf=True, fix_size=args.fix_size)
        net = torch.nn.DataParallel(net)
        # Input dimension of bbox is different in each step
        cudnn.benchmark = False
        net = net.cuda()
        if args.finetune:
            net = util.load_latest_model(args, net, prefix="cv_1")
        # Using the latest optimizer, better than Adam and SGD
        optimizer = AdaBound(net.parameters(), lr=args.learning_rate, weight_decay=args.weight_decay,)

        for epoch in range(args.epoch_num):
            loc_avg, conf_abg = fit(args, cfg, net, train_set, optimizer, is_train=True)
            loc_loss.append(loc_avg)
            conf_loss.append(conf_abg)
            train_losses = [np.asarray(loc_loss), np.asarray(conf_loss)]
            if val_set is not None:
                accu, pre, rec, f1 = fit(args, cfg, net, val_set, optimizer, is_train=False)
                accuracy.append(accu)
                precision.append(pre)
                recall.append(rec)
                f1_score.append(f1)
                val_losses = [np.asarray(accuracy), np.asarray(precision),
                              np.asarray(recall), np.asarray(f1_score)]
            if epoch != 0 and epoch % 20 == 0:
                util.save_model(args, args.curr_epoch, net.state_dict(), prefix="cv_%s" % (idx + 1),
                                keep_latest=20)
            if epoch > 5:
                # Visualize the graph change of train losses and val metrics
                # Train losses
                vb.plot_loss_distribution(train_losses, ["location", "confidence"],
                                          args.log_dir, "Loc_and_Conf", window=5, epoch=idx)
                # Val metrics
                vb.plot_loss_distribution(val_losses, ["Accuracy", "Precision", "Recall", "F1-Score"],
                                          args.log_dir, "Validation_Measure", window=5, epoch=idx)
        # Clean the data for next cross validation
        del net, optimizer
        args.curr_epoch = 0


if __name__ == "__main__":
    #test_rotation()
    main()


