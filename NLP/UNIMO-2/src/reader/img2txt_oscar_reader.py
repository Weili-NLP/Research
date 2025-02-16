#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Copyright (c) 2021 PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
File: img2txt_oscar_reader.py
Author: liwei(liwei85@baidu.com)
Date: 2021-10-25 16:07
Desc: img2txt reader
"""


import csv
csv.field_size_limit(1024 * 1024)
import numpy as np
from collections import namedtuple
import base64
import os
import gzip
import six
import time
import sys

import paddle.fluid as fluid
import functools
import model.roberta_tokenization as tokenization
from reader.unimo_grounded_batching import pad_batch_data
from utils.image_utils import process_image


def get_time():
    res = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(time.time()))
    return res


class Img2TxtReader(object):
    def __init__(self, tokenizer, args, image_size=224, resolution=16):
        self.tokenizer = tokenizer
        self.pad_id = tokenizer.pad_token_id
        self.cls_id = tokenizer.cls_token_id
        self.sep_id = tokenizer.sep_token_id
        self.mask_id = tokenizer.mask_token_id

        self.max_obj_len = args.max_obj_len
        self.tgt_type_id = args.tgt_type_id
        self.max_tgt_len = args.max_tgt_len
        self.max_out_len = args.max_out_len
        self.obj_dict = self.load_obj_file(args.object_file)

        # random_seed must be set for data slicing when using multi-gpu
        if args.random_seed:
            np.random.seed(args.random_seed)
        else:
            np.random.seed(0)

        self.trainer_id = 0
        self.trainer_nums = 1
        if os.getenv("PADDLE_TRAINER_ID"):
            self.trainer_id = int(os.getenv("PADDLE_TRAINER_ID"))
        if os.getenv("PADDLE_TRAINERS_NUM"):
            self.trainer_nums = int(os.getenv("PADDLE_TRAINERS_NUM"))

        self.current_example = 0
        self.current_epoch = 0
        self.num_examples = 0

        self.features = {}

        self.image_size = image_size
        assert resolution in {14, 16}
        self.patch_seq_len = self.image_size * self.image_size // (resolution * resolution)
        self.patch_emb_size = resolution * resolution * 3

    def get_progress(self):
        """return current progress of traning data
        """
        return self.current_epoch, self.current_file_index, self.total_file, self.current_file

    def get_num_examples(self, filelist):
        num_exp = 0
        files = open(filelist).readlines()
        for index, file_ in enumerate(files):
            print("%s - reading %d/%d" % (get_time(), index, len(files)))
            file_ = file_.strip()
            if file_.endswith('.gz'):
                with gzip.open(file_, "rt") as f:
                    for line in f:
                        if line is None:
                            continue
                        num_exp += 1
            else:
                with open(file_, "r") as f:
                    for line in f:
                        if line is None:
                            continue
                        num_exp += 1
        return num_exp

    def parse_line(self, line):
        """ parse one line to token_ids, sentence_ids, pos_ids, label
        """
        line = line.strip('\r\n').split(";")

        if len(line) == 8:
            image_id, image_path, caption_id, token_ids, sent_ids, pos_ids, seg_labels, label = line
            image_id = image_id[9:]
        else:
            raise ValueError("One sample have %d fields!" % len(line))

        image_id, image_pixel = process_image(image_id=image_id,
                                              image_path=image_path,
                                              target_shape_h=self.image_size,
                                              target_shape_w=self.image_size,
                                              data_format="channels_last",
                                              dtype='float32')
        h, w, c = image_pixel.shape
        assert h == w == self.image_size and c == 3

        obj_token_ids, obj_sent_ids, obj_pos_ids = self.obj_dict[image_id]
        obj_token_ids = [int(token) for token in obj_token_ids.split(" ")]
        obj_sent_ids = [int(token) for token in obj_sent_ids.split(" ")]
        obj_pos_ids = [int(token) for token in obj_pos_ids.split(" ")]
        assert len(obj_token_ids) == len(obj_sent_ids) == len(obj_pos_ids), \
            "[Must be true]len(obj_token_ids) == len(obj_sent_ids) == len(obj_pos_ids)"

        if len(obj_token_ids) > self.max_obj_len:
            obj_token_ids = obj_token_ids[:self.max_obj_len]
            obj_sent_ids = obj_sent_ids[:self.max_obj_len]
            obj_pos_ids = obj_pos_ids[:self.max_obj_len]

        if token_ids != '':
            token_ids = [int(token) for token in token_ids.split(" ")]
            sent_ids = [int(token) for token in sent_ids.split(" ")]
            pos_ids = [int(token) for token in pos_ids.split(" ")]
            seg_labels = [int(seg_label) for seg_label in seg_labels.split(" ")]

            if len(token_ids) > self.max_tgt_len:
                token_ids = token_ids[:self.max_tgt_len - 1] + [self.sep_id]
                sent_ids = sent_ids[:self.max_tgt_len]
                pos_ids = pos_ids[:self.max_tgt_len]
                seg_labels = seg_labels[:self.max_tgt_len - 1] + [-1]

            assert len(token_ids) == len(sent_ids) == len(pos_ids) == len(seg_labels), \
                "[Must be true]len(token_ids) == len(sent_ids) == len(pos_ids) == len(seg_labels)"

            Record = namedtuple(
                'Record',
                ['image_pixel', 'image_id', 'obj_token_ids', 'obj_sent_ids', 'obj_pos_ids',
                 'token_ids', 'sent_ids', 'pos_ids', 'seg_labels'])

            record = Record(
                image_pixel=image_pixel,
                image_id=int(image_id),
                obj_token_ids=obj_token_ids,
                obj_sent_ids=obj_sent_ids,
                obj_pos_ids=obj_pos_ids,
                token_ids=token_ids,
                sent_ids=sent_ids,
                pos_ids=pos_ids,
                seg_labels=seg_labels)
        else:
            Record = namedtuple(
                'Record',
                ['image_pixel', 'image_id', 'obj_token_ids', 'obj_sent_ids', 'obj_pos_ids'])

            record = Record(
                image_pixel=image_pixel,
                image_id=int(image_id),
                obj_token_ids=obj_token_ids,
                obj_sent_ids=obj_sent_ids,
                obj_pos_ids=obj_pos_ids)

        return record

    def load_obj_file(self, obj_file):
        if not obj_file:
            print("obj_file is None")
            return None
        _dict = {}
        for line in open(obj_file):
            line = line.strip('\r\n').split(';')
            assert len(line) == 4, "the object file should only contain 4 fields!!!"
            image_id, obj_token_ids, obj_sent_ids, obj_pos_ids = line
            _dict[image_id] = [obj_token_ids, obj_sent_ids, obj_pos_ids]
        print('obj_dict size is ', len(_dict))
        return _dict

    def read_file(self, file):
        if file.endswith('.gz'):
            with gzip.open(file, "rt") as f:
                for line in f:
                    parsed_line = self.parse_line(line)
                    if parsed_line is None:
                        continue
                    yield parsed_line
        else:
            with open(file, "r") as f:
                for line in f:
                    parsed_line = self.parse_line(line)
                    if parsed_line is None:
                        continue
                    yield parsed_line

    def shuffle_samples(self, sample_generator, buffer=1000):
        samples = []
        try:
            while True:
                while len(samples) < buffer:
                    sample = next(sample_generator)
                    samples.append(sample)
                np.random.shuffle(samples)
                for sample in samples:
                    yield sample
                samples = []
        except StopIteration:
            print("stopiteration: reach end of file")
            if len(samples) == 0:
                yield None
            else:
                np.random.shuffle(samples)
                for sample in samples:
                    yield sample

    def _prepare_batch_data(self, sample_generator, batch_size, phase=None, do_decode=False, place=None):
        """generate batch records"""
        batch, index = [], 0
        for sample in sample_generator:
            if sample is None:
                continue
            self.current_example = index
            index += 1

            to_append = len(batch) < batch_size
            if to_append:
                batch.append(sample)
            else:
                yield self._pad_batch_records(batch, do_decode, place)
                batch = [sample]

        if batch:
            yield self._pad_batch_records(batch, do_decode, place)

    def data_generator(self,
                       filelist,
                       batch_size,
                       epoch,
                       dev_count=1,
                       shuffle=True,
                       phase=None,
                       do_decode=False,
                       place=None):
        files = open(filelist).readlines()
        self.total_file = len(files)
        print("total_file: ", self.total_file)

        def wrapper():
            all_dev_batches = []
            trainer_id = self.trainer_id
            for epoch_index in range(epoch):
                self.current_file_index = 0
                self.current_epoch = epoch_index

                if phase == "train":  # shuffle file list
                    np.random.shuffle(files)

                for index, file_ in enumerate(files):
                    file_ = file_.strip()
                    self.current_file_index = index + 1
                    self.current_file = file_

                    sample_generator = self.read_file(file_)
                    if phase == "train":  # shuffle buffered sample
                        sample_generator = self.shuffle_samples(sample_generator)

                    for batch_data in self._prepare_batch_data(
                            sample_generator, batch_size, phase=phase, do_decode=do_decode, place=place):
                        if len(all_dev_batches) < dev_count:
                            all_dev_batches.append(batch_data)
                        if len(all_dev_batches) == dev_count:
                            yield all_dev_batches[trainer_id]
                            all_dev_batches = []

            if phase != "train":
                if trainer_id < len(all_dev_batches):
                    yield all_dev_batches[trainer_id]

        return wrapper

    def _to_lodtensor(self, data, place, lod=None):
        data_tensor = fluid.LoDTensor()
        data_tensor.set(data, place)
        if lod is not None:
            data_tensor.set_lod(lod)
        return data_tensor

    def _pad_batch_records(self, batch_records, do_decode, place):
        # visual image part
        batch_image_pixel = [record.image_pixel for record in batch_records]

        # image pixels, include the global image token
        image_mask = np.ones(shape=[len(batch_records), 1, self.patch_seq_len + 1], dtype="float32")
        image_pixel_input = np.array(batch_image_pixel, dtype='float32')

        batch_obj_token_ids = [record.obj_token_ids for record in batch_records]
        batch_obj_sent_ids = [record.obj_sent_ids for record in batch_records]
        batch_obj_pos_ids = [record.obj_pos_ids for record in batch_records]

        batch_size = len(batch_image_pixel)
        if do_decode:
            batch_image_id = [record.image_id for record in batch_records]
            image_id = np.array(batch_image_id, dtype='int32').reshape((-1, 1))

            tgt_word = np.array([[self.cls_id]] * batch_size,
                                dtype="int64").reshape([-1, 1, 1])
            tgt_pos_id = np.full_like(tgt_word, 2, dtype="int64").reshape(
                [-1, 1, 1])  ####################### pos start from 2
            init_score = np.zeros_like(tgt_word, dtype="float32").reshape([-1, 1])

            lods = [range(tgt_word.shape[0] + 1)] * 2
            init_score = self._to_lodtensor(init_score, place, lods)
            tgt_word = self._to_lodtensor(tgt_word, place, lods)
            tgt_pos_id = self._to_lodtensor(tgt_pos_id, place, lods)
            init_idx = np.array(range(batch_size), dtype="int32")

            padded_obj_token_ids, obj_token_mask = pad_batch_data(
                batch_obj_token_ids, pretraining_task='nlu', pad_idx=self.pad_id, return_input_mask=True)
            padded_obj_sent_ids = pad_batch_data(
                batch_obj_sent_ids, pretraining_task='nlu', pad_idx=self.pad_id)
            padded_obj_pos_ids = pad_batch_data(
                batch_obj_pos_ids, pretraining_task='nlu', pad_idx=self.pad_id)

            # (batch_size, max_obj_len, max_obj_len)
            obj_mask = np.matmul(np.transpose(obj_token_mask, (0, 2, 1)), obj_token_mask)

            return_list = [image_pixel_input, image_mask, image_id,
                           padded_obj_token_ids, padded_obj_sent_ids, padded_obj_pos_ids, obj_mask,
                           tgt_word, tgt_pos_id, init_score, init_idx, obj_token_mask]

        else:
            batch_token_ids = [record.token_ids for record in batch_records]
            batch_sent_ids = [record.sent_ids for record in batch_records]
            batch_position_ids = [record.pos_ids for record in batch_records]

            input_token_ids = []
            input_sent_ids = []
            input_pos_ids = []
            for obj_token_ids, token_ids in zip(batch_obj_token_ids, batch_token_ids):
                input_token_ids.append(obj_token_ids + token_ids)

            for obj_sent_ids, sent_ids in zip(batch_obj_sent_ids, batch_sent_ids):
                input_sent_ids.append(obj_sent_ids + sent_ids)

            for obj_pos_ids, pos_ids in zip(batch_obj_pos_ids, batch_position_ids):
                input_pos_ids.append(obj_pos_ids + pos_ids)

            token_ids = pad_batch_data(input_token_ids, pad_idx=self.pad_id)
            sent_ids = pad_batch_data(input_sent_ids, pad_idx=self.pad_id)
            position_ids = pad_batch_data(input_pos_ids, pad_idx=self.pad_id)

            max_len = token_ids.shape[1]
            tgt_label = []
            for i in range(len(batch_token_ids)):
                tgt_idxs = range(1, len(batch_token_ids[i]))
                tgt_label.extend(batch_token_ids[i][idx] for idx in tgt_idxs)
            tgt_label = np.array(tgt_label).astype("int64").reshape([-1, 1])

            tgt_pos = sum(list(map(lambda i: list(range(max_len * i + len(batch_obj_token_ids[i]),
                                                        max_len * i + len(batch_obj_token_ids[i])
                                                        + len(batch_token_ids[i]) - 1)),
                                   range(batch_size))), [])
            tgt_pos = np.array(tgt_pos).reshape([-1, 1]).astype('int64')

            # This is used to avoid attention on paddings and subsequent words.
            token_seq_mask_data = np.zeros((batch_size, max_len, max_len))
            for index, mask_data in enumerate(token_seq_mask_data):
                start = len(batch_obj_token_ids[index])
                end = len(input_token_ids[index])
                mask_data[:end, :start] = 1

                # Generate the lower triangular matrix using the slice of matrix
                b = np.tril(np.ones([end - start, end - start]), 0)
                mask_data[start:end, start:end] = b
            token_seq_mask = token_seq_mask_data.astype("float32")

            return_list = [image_pixel_input, image_mask,
                           token_ids, sent_ids, position_ids, token_seq_mask, tgt_label, tgt_pos]

        return return_list


if __name__ == '__main__':
    pass
