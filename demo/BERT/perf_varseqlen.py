#
# SPDX-FileCopyrightText: Copyright (c) 1993-2023 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import argparse
import ctypes
import time
import numpy as np
import tensorrt as trt
import pycuda.driver as cuda
import pycuda.autoinit

import numpy as np

TRT_LOGGER = trt.Logger(trt.Logger.ERROR)

class DeviceBuffer(object):
    def __init__(self, shape, dtype=trt.int32):
        self.buf = cuda.mem_alloc(trt.volume(shape) * dtype.itemsize)

    def binding(self):
        return int(self.buf)

    def free(self):
        self.buf.free()


def main():
    parser = argparse.ArgumentParser(description='BERT Inference Benchmark')
    parser.add_argument("-e", "--engine", help='Path to BERT TensorRT engine')
    parser.add_argument('-b', '--batch-size', default=[], action="append", help='Batch size(s) to benchmark. Can be specified multiple times for more than one batch size. This script assumes that the engine has been built with one optimization profile for each batch size, and that these profiles are in order of increasing batch size.', type=int)
    parser.add_argument('-s', '--sequence-length', default=128, help='Sequence length of the BERT model', type=int)
    parser.add_argument('-i', '--iterations', default=200, help='Number of iterations to run when benchmarking each batch size.', type=int)
    parser.add_argument('-w', '--warm-up-runs', default=10, help='Number of iterations to run prior to benchmarking.', type=int)
    parser.add_argument('-d', '--duration', default=0.0, help='Minimal number of seconds to run when benchmarking each batch size.', type=float)
    parser.add_argument('-r', '--random-seed', required=False, default=12345, help='Random seed.', type=int)
    args, _ = parser.parse_known_args()
    args.batch_size = args.batch_size or [1]

    # Import necessary plugins for BERT TensorRT
    ctypes.CDLL("libnvinfer_plugin.so", mode=ctypes.RTLD_GLOBAL)

    with open(args.engine, 'rb') as f, trt.Runtime(TRT_LOGGER) as runtime, runtime.deserialize_cuda_engine(f.read()) as engine, engine.create_execution_context() as context:
        # Allocate buffers large enough to store the largest batch size
        max_input_shape = (args.sequence_length * max(args.batch_size), )
        max_output_shape = (args.sequence_length * max(args.batch_size), 2, 1, 1)
        buffers = [
            DeviceBuffer(max_input_shape),
            DeviceBuffer(max_input_shape),
            DeviceBuffer((max(args.batch_size) + 1, )),
            DeviceBuffer((args.sequence_length, )),
            DeviceBuffer(max_output_shape)
        ]

        # Prepare random input
        pseudo_vocab_size = 30522
        pseudo_type_vocab_size = 2
        np.random.seed(args.random_seed)
        test_word_ids = np.random.randint(0, pseudo_vocab_size, (args.sequence_length * max(args.batch_size)), dtype=np.int32)
        test_segment_ids = np.random.randint(0, pseudo_type_vocab_size, (args.sequence_length * max(args.batch_size)), dtype=np.int32)
        test_cu_seq_lens = np.arange(0, args.sequence_length * max(args.batch_size) + 1, args.sequence_length, dtype=np.int32)

        # Copy input h2d
        cuda.memcpy_htod(buffers[0].buf, test_word_ids.ravel())
        cuda.memcpy_htod(buffers[1].buf, test_segment_ids.ravel())
        cuda.memcpy_htod(buffers[2].buf, test_cu_seq_lens.ravel())

        bench_times = {}

        for idx, batch_size in enumerate(sorted(args.batch_size)):
            stream = cuda.Stream()
            context.set_optimization_profile_async(0, stream.handle)

            # Each profile has unique bindings
            bindings = [buf.binding() for buf in buffers]

            shapes = {
                "input_ids": (args.sequence_length * batch_size, ),
                "segment_ids": (args.sequence_length * batch_size, ),
                "cu_seqlens": (batch_size + 1, ),
                "max_seqlen": (args.sequence_length, ),
            }

            for binding, shape in shapes.items():
                context.set_input_shape(binding, shape)
            assert len(context.infer_shapes()) == 0

            # Inference
            total_time = 0
            start = cuda.Event()
            end = cuda.Event()

            # Warmup
            for _ in range(args.warm_up_runs):
                context.execute_async_v2(bindings=bindings, stream_handle=stream.handle)
                stream.synchronize()

            # Timing loop
            times = []
            actual_iterations = 0
            start_time = time.time()
            while actual_iterations < args.iterations or (time.time() - start_time) < args.duration:
                start.record(stream)
                context.execute_async_v2(bindings=bindings, stream_handle=stream.handle)
                end.record(stream)
                stream.synchronize()
                times.append(end.time_since(start))
                actual_iterations += 1

            # Compute average time, 95th percentile time and 99th percentile time.
            bench_times[batch_size] = times

        [b.free() for b in buffers]

        for batch_size, times in bench_times.items():
            total_time = sum(times)
            avg_time = total_time / float(actual_iterations)
            times.sort()
            percentile95 = times[int(actual_iterations * 0.95)]
            percentile99 = times[int(actual_iterations * 0.99)]
            print("Running {:} iterations with Batch Size: {:}\n\tTotal Time: {:} ms \tAverage Time: {:} ms\t95th Percentile Time: {:} ms\t99th Percentile Time: {:}".format(actual_iterations, batch_size, total_time, avg_time, percentile95, percentile99))


if __name__ == '__main__':
    main()
