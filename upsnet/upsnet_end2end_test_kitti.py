# ---------------------------------------------------------------------------
# Unified Panoptic Segmentation Network
#
# Copyright (c) 2018-2019 Uber Technologies, Inc.
#
# Licensed under the Uber Non-Commercial License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at the root directory of this project. 
#
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Written by Yuwen Xiong
# ---------------------------------------------------------------------------

from __future__ import print_function, division
import os
import sys
import logging
import pprint
import time
import numpy as np
import pickle
import torch
import torch.nn as nn
import torch.utils.data
import torch.backends.cudnn as cudnn
import cv2
from PIL import Image

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from upsnet.config.config import config
from upsnet.config.parse_args import parse_args
from lib.utils.logging import create_logger
from lib.utils.timer import Timer

## Load config and create log
args = parse_args()
logger, final_output_path = create_logger(config.output_path, args.cfg, config.dataset.test_image_set)

from upsnet.dataset import *
from upsnet.models import *
from upsnet.bbox.bbox_transform import bbox_transform, clip_boxes, expand_boxes
from lib.utils.callback import Speedometer
from lib.utils.data_parallel import DataParallel
from pycocotools.mask import encode as mask_encode

cv2.ocl.setUseOpenCL(False)

cudnn.enabled = True
cudnn.benchmark = False

def im_detect(output_all, data, im_infos):
    # Program to convert output of network to what to be saved
    scores_all = []
    pred_boxes_all = []
    pred_masks_all = []
    pred_ssegs_all = []
    pred_panos_all = []
    pred_pano_cls_inds_all = []
    cls_inds_all = []

    if len(data) == 1:
        output_all = [output_all]

    output_all = [{k: v.data.cpu().numpy() for k, v in output.items()} for output in output_all]

    for i in range(len(data)):
        im_info = im_infos[i]
        # Modify here?
        #print("score = ", output_all[i]['cls_probs'])
        #print("pred_bbox = ", output_all[i]['pred_boxes'][:, 1:] / im_info[2])
        #print("cls_ind =", output_all[i]['cls_inds'])
        #if output_all[i]['cls_probs'] > 0.9:
        scores_all.append(output_all[i]['cls_probs'])
        pred_boxes_all.append(output_all[i]['pred_boxes'][:, 1:] / im_info[2])
        cls_inds_all.append(output_all[i]['cls_inds'])

        if config.network.has_mask_head:
            #print("pred_mask =", output_all[i]['mask_probs'].shape)
            pred_masks_all.append(output_all[i]['mask_probs'])
        if config.network.has_fcn_head:
            #print("ssegs =", output_all[i]['fcn_outputs'].shape)
            pred_ssegs_all.append(output_all[i]['fcn_outputs'])
        if config.network.has_panoptic_head:
            #print("panoptic = ", output_all[i]['panoptic_outputs'].shape)
            pred_panos_all.append(output_all[i]['panoptic_outputs'])
            #print("panoptic cls =  ", len(output_all[i]['panoptic_cls_inds']))
            pred_pano_cls_inds_all.append(output_all[i]['panoptic_cls_inds'])

    return {
        'scores': scores_all,
        'boxes': pred_boxes_all,
        'masks': pred_masks_all,
        'ssegs': pred_ssegs_all,
        'panos': pred_panos_all,
        'cls_inds': cls_inds_all,
        'pano_cls_inds': pred_pano_cls_inds_all,
    }


def failure_removal(boxes, masks, ref_box):
    
    if boxes.shape[0] == 0:
        return boxes, np.zeros((0,9,28,28)), np.zeros((0,4))
    else:
        tmp = np.zeros((0,5))
        valid_idx = [False]*len(boxes)
        for j in range(len(boxes)):
            cnt_ = boxes[j,:]
            if cnt_[-1] > 0.9:
                valid_idx[j] = True
                if tmp.shape[0] == 0:
                    tmp = boxes[j:j+1,...]
                else:
                    tmp = np.concatenate([tmp, boxes[j:j+1,...]], axis=0)

        masks_ret = np.zeros((0,9,28,28))       
        ref_box_ret = np.zeros((0,4))
        for j in range(len(boxes)):
            if valid_idx[j] == True:
                if masks_ret.shape[0] == 0:
                    masks_ret = masks[j:j+1,...]
                else:
                    masks_ret = np.concatenate([masks_ret, masks[j:j+1,...]], axis=0)
                if ref_box_ret.shape[0] == 0:
                    ref_box_ret = ref_box[j:j+1,...]
                else:
                    ref_box_ret = np.concatenate([ref_box_ret, ref_box[j:j+1,...]], axis=0)

    return tmp, masks_ret, ref_box_ret

def im_post(boxes_all, masks_all, scores, pred_boxes, pred_masks, cls_inds, num_classes, im_info):

    cls_segms = [[] for _ in range(num_classes)]
    mask_ind = 0

    M = config.network.mask_size

    scale = (M + 2.0) / M


    ref_boxes = expand_boxes(pred_boxes, scale)
    ref_boxes = ref_boxes.astype(np.int32)
    padded_mask = np.zeros((M + 2, M + 2), dtype=np.float32)
    # Idx classes idx
    for idx in range(1, num_classes):
        segms = []
        # BBox for classes idx
        cls_boxes = np.hstack([pred_boxes[idx == cls_inds, :], scores.reshape(-1, 1)[idx == cls_inds]])
        #print("boxes", cls_boxes)
        #cls_boxes = 
        cls_pred_masks = pred_masks[idx == cls_inds]
        #print("cls_pred_masks = ", cls_pred_masks.shape)
        cls_ref_boxes = ref_boxes[idx == cls_inds]
        #print("cls_ref_boxes = ", cls_ref_boxes.shape)
        cls_boxes, cls_pred_masks, cls_ref_boxes = failure_removal(cls_boxes, cls_pred_masks, cls_ref_boxes)
        #print("remove boxes", cls_boxes)
        #print("remove cls_pred_masks = ", cls_pred_masks.shape)
        #print("remove cls_ref_boxes = ", cls_ref_boxes.shape)
        for _ in range(cls_boxes.shape[0]):

            if pred_masks.shape[1] > 1:
                padded_mask[1:-1, 1:-1] = cls_pred_masks[_, idx, :, :]
            else:
                padded_mask[1:-1, 1:-1] = cls_pred_masks[_, 0, :, :]
            ref_box = cls_ref_boxes[_, :]

            w = ref_box[2] - ref_box[0] + 1
            h = ref_box[3] - ref_box[1] + 1
            w = np.maximum(w, 1)
            h = np.maximum(h, 1)

            mask = cv2.resize(padded_mask, (w, h))
            mask = np.array(mask > 0.5, dtype=np.uint8)
            im_mask = np.zeros((im_info[0], im_info[1]), dtype=np.uint8)

            x_0 = max(ref_box[0], 0)
            x_1 = min(ref_box[2] + 1, im_info[1])
            y_0 = max(ref_box[1], 0)
            y_1 = min(ref_box[3] + 1, im_info[0])

            im_mask[y_0:y_1, x_0:x_1] = mask[
                                        (y_0 - ref_box[1]):(y_1 - ref_box[1]),
                                        (x_0 - ref_box[0]):(x_1 - ref_box[0])
                                        ]

            # Get RLE encoding used by the COCO evaluation API
            rle = mask_encode(
                np.array(im_mask[:, :, np.newaxis], order='F')
            )[0]
            rle['counts'] = rle['counts'].decode()
            # Updata segs
            segms.append(rle)

            mask_ind += 1

        cls_segms[idx] = segms
        boxes_all[idx].append(cls_boxes)
        masks_all[idx].append(segms)


def upsnet_test():
    # Print config
    pprint.pprint(config)
    logger.info('test config:{}\n'.format(pprint.pformat(config)))

    # create models
    gpus = [int(_) for _ in config.gpus.split(',')]
    test_model = eval(config.symbol)().cuda(device=gpus[0])

    # create data loader
    test_dataset = eval(config.dataset.dataset)(image_sets=config.dataset.test_image_set.split('+'), flip=False,
                                                result_path=final_output_path, phase='test')
    print("image_sets = ", config.dataset.test_image_set.split('+'))
    print("result_path = ", final_output_path)
    print("test_dataset = ", test_dataset)
    test_loader = torch.utils.data.DataLoader(test_dataset, batch_size=config.test.batch_size, shuffle=False,
                                              num_workers=0, drop_last=False, pin_memory=False, collate_fn=test_dataset.collate)

    if args.eval_only:
        # If only evaluate existing result
        results = pickle.load(open(os.path.join(final_output_path, 'results', 'results_list.pkl'), 'rb'))
        if config.test.vis_mask:
            test_dataset.vis_all_mask(results['all_boxes'], results['all_masks'], os.path.join(final_output_path, 'results', 'vis'))
        if config.network.has_rcnn:
            test_dataset.evaluate_boxes(results['all_boxes'], os.path.join(final_output_path, 'results'))
        if config.network.has_mask_head:
            test_dataset.evaluate_masks(results['all_boxes'], results['all_masks'], os.path.join(final_output_path, 'results'))
        if config.network.has_fcn_head:
            test_dataset.evaluate_ssegs(results['all_ssegs'], os.path.join(final_output_path, 'results', 'ssegs'))
            # logging.info('combined pano result:')
            # test_dataset.evaluate_panoptic(test_dataset.get_combined_pan_result(results['all_ssegs'], results['all_boxes'], results['all_masks'], stuff_area_limit=config.test.panoptic_stuff_area_limit), os.path.join(final_output_path, 'results', 'pans_combined'))
        if config.network.has_panoptic_head:
            logging.info('unified pano result:')
            test_dataset.evaluate_panoptic(test_dataset.get_unified_pan_result(results['all_ssegs'], results['all_panos'], results['all_pano_cls_inds'], stuff_area_limit=config.test.panoptic_stuff_area_limit), os.path.join(final_output_path, 'results', 'pans_unified'))
        sys.exit()

    # preparing
    curr_iter = config.test.test_iteration
    if args.weight_path == '':
        test_model.load_state_dict(torch.load(os.path.join(os.path.join(os.path.join(config.output_path, os.path.basename(args.cfg).split('.')[0]),
                                   '_'.join(config.dataset.image_set.split('+')), config.model_prefix+str(curr_iter)+'.pth'))), resume=True)
    else:
        test_model.load_state_dict(torch.load(args.weight_path), resume=True)


    for p in test_model.parameters():
        p.requires_grad = False

    test_model = DataParallel(test_model, device_ids=gpus, gather_output=False).to(gpus[0])

    # start training
    test_model.eval()

    i_iter = 0
    idx = 0
    test_iter = test_loader.__iter__()
    all_boxes = [[] for _ in range(test_dataset.num_classes)]
    if config.network.has_mask_head:
        all_masks = [[] for _ in range(test_dataset.num_classes)]
    if config.network.has_fcn_head:
        all_ssegs = []
    if config.network.has_panoptic_head:
        all_panos = []
        all_pano_cls_inds = []
        panos = []


    data_timer = Timer()
    net_timer = Timer()
    post_timer = Timer()

    while i_iter < len(test_loader):
        data_timer.tic()
        batch = []
        labels = []
        #Collect a batch of data
        for gpu_id in gpus:
            try:
                data, label, _ = test_iter.next()
                if label is not None:
                    data['roidb'] = label['roidb']
                for k, v in data.items():
                    data[k] = v.pin_memory().to(gpu_id, non_blocking=True) if torch.is_tensor(v) else v
            except StopIteration:
                data = data.copy()
                for k, v in data.items():
                    data[k] = v.pin_memory().to(gpu_id, non_blocking=True) if torch.is_tensor(v) else v
            batch.append((data, None))
            labels.append(label)
            i_iter += 1

        im_infos = [_[0]['im_info'][0] for _ in batch]

        data_time = data_timer.toc()
        if i_iter > 10:
            net_timer.tic()
        with torch.no_grad():
            output = test_model(*batch)
            torch.cuda.synchronize()
            if i_iter > 10:
                net_time = net_timer.toc()
            else:
                net_time = 0
            output = im_detect(output, batch, im_infos)
        
        post_timer.tic()
        for score, box, mask, cls_idx, im_info in zip(output['scores'], output['boxes'], output['masks'], output['cls_inds'], im_infos):
            im_post(all_boxes, all_masks, score, box, mask, cls_idx, test_dataset.num_classes, np.round(im_info[:2] / im_info[2]).astype(np.int32))
            idx += 1
        if config.network.has_fcn_head:
            for i, sseg in enumerate(output['ssegs']):
                sseg = sseg.squeeze(0).astype('uint8')[:int(im_infos[i][0]), :int(im_infos[i][1])]
                all_ssegs.append(cv2.resize(sseg, None, None, fx=1/im_infos[i][2], fy=1/im_infos[i][2], interpolation=cv2.INTER_NEAREST))
        if config.network.has_panoptic_head:
            pano_cls_inds = []
            for i, (pano, cls_ind) in enumerate(zip(output['panos'], output['pano_cls_inds'])):
                pano = pano.squeeze(0).astype('uint8')[:int(im_infos[i][0]), :int(im_infos[i][1])]
                panos.append(cv2.resize(pano, None, None, fx=1/im_infos[i][2], fy=1/im_infos[i][2], interpolation=cv2.INTER_NEAREST))
                pano_cls_inds.append(cls_ind)

            all_panos.extend(panos)
            panos = []
            all_pano_cls_inds.extend(pano_cls_inds)
        post_time = post_timer.toc()
        s = 'Batch %d/%d, data_time:%.3f, net_time:%.3f, post_time:%.3f' % (idx, len(test_dataset), data_time, net_time, post_time)
        logging.info(s)

    results = []

    # trim redundant predictions
    for i in range(1, test_dataset.num_classes):
        all_boxes[i] = all_boxes[i][:len(test_loader)]
        if config.network.has_mask_head:
            all_masks[i] = all_masks[i][:len(test_loader)]
    if config.network.has_fcn_head:
        all_ssegs = all_ssegs[:len(test_loader)]
    if config.network.has_panoptic_head:
        all_panos = all_panos[:len(test_loader)]

    os.makedirs(os.path.join(final_output_path, 'results'), exist_ok=True)

    results = {'all_boxes': all_boxes,
               'all_masks': all_masks if config.network.has_mask_head else None,
               'all_ssegs': all_ssegs if config.network.has_fcn_head else None,
               'all_panos': all_panos if config.network.has_panoptic_head else None,
               'all_pano_cls_inds': all_pano_cls_inds if config.network.has_panoptic_head else None,
               }

    #with open(os.path.join(final_output_path, 'results', 'results_list.pkl'), 'wb') as f:
    #    pickle.dump(results, f, protocol=2)

    if config.test.vis_mask:
        test_dataset.vis_all_mask(all_boxes, all_masks, os.path.join(final_output_path, 'results', 'vis'))
    else:
        #test_dataset.evaluate_boxes(all_boxes, os.path.join(final_output_path, 'results'))
        if config.network.has_mask_head:
            test_dataset.evaluate_masks(all_boxes, all_masks, os.path.join(final_output_path, 'results'))
        if config.network.has_panoptic_head:
            logging.info('unified pano result:')
            test_dataset.evaluate_panoptic(test_dataset.get_unified_pan_result(all_ssegs, all_panos, all_pano_cls_inds, stuff_area_limit=config.test.panoptic_stuff_area_limit), os.path.join(final_output_path, 'results', 'pans_unified'))
        if config.network.has_fcn_head:
            test_dataset.evaluate_ssegs(all_ssegs, os.path.join(final_output_path, 'results', 'ssegs'))
            # logging.info('combined pano result:')
            # test_dataset.evaluate_panoptic(test_dataset.get_combined_pan_result(all_ssegs, all_boxes, all_masks, stuff_area_limit=config.test.panoptic_stuff_area_limit), os.path.join(final_output_path, 'results', 'pans_combined'))


if __name__ == "__main__":
    upsnet_test()
