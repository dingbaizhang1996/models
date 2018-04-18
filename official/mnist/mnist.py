#  Copyright 2017 The TensorFlow Authors. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
"""Convolutional Neural Network Estimator for MNIST, built with tf.layers."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import argparse
import sys

import tensorflow as tf  # pylint: disable=g-bad-import-order

from official.datasets.image import mnist_dataset
from official.utils.arg_parsers import accelerator
from official.utils.arg_parsers import base
from official.utils.arg_parsers import parsers
from official.utils.logs import hooks_helper
from official.utils.misc import model_helpers

LEARNING_RATE = 1e-4


def create_model(data_format, image_size=mnist_dataset.IMAGE_SIZE):
  """Model to recognize digits in the MNIST dataset.

  Network structure is equivalent to:
  https://github.com/tensorflow/tensorflow/blob/r1.5/tensorflow/examples/tutorials/mnist/mnist_deep.py
  and
  https://github.com/tensorflow/models/blob/master/tutorials/image/mnist/convolutional.py

  But uses the tf.keras API.

  Args:
    data_format: Either 'channels_first' or 'channels_last'. 'channels_first' is
      typically faster on GPUs while 'channels_last' is typically faster on
      CPUs. See
      https://www.tensorflow.org/performance/performance_guide#data_formats
    image_size: The side length of an image. Images are assumed to be
      image_size x image_size square images. MNIST images are 28x28 pixels.

  Returns:
    A tf.keras.Model.
  """
  if data_format == 'channels_first':
    input_shape = [1, image_size, image_size]
  else:
    assert data_format == 'channels_last'
    input_shape = [image_size, image_size, 1]

  l = tf.keras.layers
  max_pool = l.MaxPooling2D(
      (2, 2), (2, 2), padding='same', data_format=data_format)
  # The model consists of a sequential chain of layers, so tf.keras.Sequential
  # (a subclass of tf.keras.Model) makes for a compact description.
  return tf.keras.Sequential(
      [
          l.Reshape(input_shape),
          l.Conv2D(
              32,
              5,
              padding='same',
              data_format=data_format,
              activation=tf.nn.relu),
          max_pool,
          l.Conv2D(
              64,
              5,
              padding='same',
              data_format=data_format,
              activation=tf.nn.relu),
          max_pool,
          l.Flatten(),
          l.Dense(1024, activation=tf.nn.relu),
          l.Dropout(0.4),
          l.Dense(10)
      ])


def metric_fn(labels, logits):
  accuracy = (
    tf.no_op() if tf.contrib.distribute.has_distribution_strategy()
    else tf.metrics.accuracy(
        labels=labels, predictions=tf.argmax(logits, axis=1))
  )
  return {"accuracy": accuracy}


def model_fn(features, labels, mode, params):
  """The model_fn argument for creating an Estimator."""
  use_tpu = params["use_tpu"]  # type: bool
  if use_tpu and mode == tf.estimator.ModeKeys.PREDICT:
    raise RuntimeError("mode PREDICT is not yet supported for TPUs.")

  model = create_model(params['data_format'])  # type: tf.keras.Sequential
  image = features
  if isinstance(image, dict):
    image = features['image']

  if mode == tf.estimator.ModeKeys.PREDICT:
    if use_tpu:
      raise RuntimeError("mode PREDICT is not yet supported for TPUs.")

    logits = model(image, training=False)
    predictions = {
      'classes': tf.argmax(logits, axis=1),
      'probabilities': tf.nn.softmax(logits),
    }
    return tf.estimator.EstimatorSpec(
        mode=mode,
        predictions=predictions,
        export_outputs={
          'classify': tf.estimator.export.PredictOutput(predictions)
        })
  if mode == tf.estimator.ModeKeys.TRAIN:
    optimizer = tf.train.AdamOptimizer(learning_rate=LEARNING_RATE)
    logits = model(image, training=True)
    loss = tf.losses.sparse_softmax_cross_entropy(labels=labels, logits=logits)

    # TODO(robieta@): Reconcile with TPU code.
    # # Name tensors to be logged with LoggingTensorHook.
    # tf.identity(LEARNING_RATE, 'learning_rate')
    # tf.identity(loss, 'cross_entropy')
    # tf.identity(accuracy[1], name='train_accuracy')
    #
    # # Save accuracy scalar to Tensorboard output.
    # tf.summary.scalar('train_accuracy', accuracy[1])

    spec_args = dict(
        mode=mode,
        loss=loss,
        train_op=optimizer.minimize(loss, tf.train.get_or_create_global_step())
    )
    if use_tpu:
      return tf.contrib.tpu.TPUEstimatorSpec(**spec_args)
    return tf.estimator.EstimatorSpec(**spec_args)

  if mode == tf.estimator.ModeKeys.EVAL:
    logits = model(image, training=False)
    loss = tf.losses.sparse_softmax_cross_entropy(labels=labels, logits=logits)
    if use_tpu:
      return tf.contrib.tpu.TPUEstimatorSpec(
          mode=mode,
          loss=loss,
          eval_metrics=(metric_fn, [labels, logits])
      )
    return tf.estimator.EstimatorSpec(
        mode=mode,
        loss=loss,
        eval_metric_ops=metric_fn(labels=labels, logits=logits)
    )


def construct_estimator(flags, use_tpu):
  use_gpu = flags.num_gpus > 0

  data_format = flags.data_format
  if data_format is None:
    data_format = ('channels_first' if use_gpu or use_tpu else 'channels_last')

  params = {"use_tpu": use_tpu, "data_format": data_format}
  session_config=tf.ConfigProto(
      inter_op_parallelism_threads=flags.inter_op_parallelism_threads,
      intra_op_parallelism_threads=flags.intra_op_parallelism_threads,
      allow_soft_placement=True,
      log_device_placement=True
  )

  if use_tpu:
    tpu_cluster_resolver = tf.contrib.cluster_resolver.TPUClusterResolver(
        flags.tpu,
        zone=flags.tpu_zone,
        project=flags.tpu_gcp_project
    )
    run_config = tf.contrib.tpu.RunConfig(
        cluster=tpu_cluster_resolver,
        model_dir=flags.model_dir,
        session_config=session_config,
        tpu_config=tf.contrib.tpu.TPUConfig(
            iterations_per_loop=flags.iterations_per_loop,
            num_shards=flags.num_tpu_shards)
    )
    return tf.contrib.tpu.TPUEstimator(
        model_fn=model_fn,
        use_tpu=True,
        train_batch_size=flags.batch_size,
        eval_batch_size=flags.batch_size,
        params=params,
        config=run_config)

  if flags.num_gpus == 0:
    distribution = tf.contrib.distribute.OneDeviceStrategy('device:CPU:0')
  elif flags.num_gpus == 1:
    distribution = tf.contrib.distribute.OneDeviceStrategy('device:GPU:0')
  else:
    distribution = tf.contrib.distribute.MirroredStrategy(
        num_gpus=flags.num_gpus
    )
  run_config = tf.estimator.RunConfig(
      model_dir=flags.model_dir,
      train_distribute=distribution,
      session_config=session_config
  )
  return tf.estimator.Estimator(
      model_fn=model_fn,
      config=run_config,
      params=params
  )

def main(argv):
  parser = MNISTArgParser()
  flags = parser.parse_args(args=argv[1:])
  use_tpu = flags.tpu is not None

  mnist_classifier = construct_estimator(flags=flags, use_tpu=use_tpu)

  # Set up hook that outputs training logs every 100 steps.
  train_hooks = hooks_helper.get_train_hooks(
      flags.hooks, batch_size=flags.batch_size)

  # Train and evaluate model.
  for _ in range(flags.train_epochs // flags.epochs_between_evals):
    mnist_classifier.train(input_fn=train_input_fn, hooks=train_hooks)
    eval_results = mnist_classifier.evaluate(input_fn=eval_input_fn)
    print('\nEvaluation results:\n\t%s\n' % eval_results)

    if model_helpers.past_stop_threshold(flags.stop_threshold,
                                         eval_results['accuracy']):
      break

  # Export the model
  if flags.export_dir is not None:
    image = tf.placeholder(tf.float32, [None, 28, 28])
    input_fn = tf.estimator.export.build_raw_serving_input_receiver_fn({
        'image': image,
    })
    mnist_classifier.export_savedmodel(flags.export_dir, input_fn)


class MNISTArgParser(base.Parser):
  """Argument parser for running MNIST model."""

  def __init__(self, simple_help=True):
    super(MNISTArgParser, self).__init__(parents=[
        parsers.BaseParser(multi_gpu=False, num_gpu=False),
        accelerator.Parser(simple_help=simple_help, num_gpus=True, tpu=True),
        parsers.ImageModelParser(),
        parsers.ExportParser()],
        simple_help=simple_help
    )

    self.set_defaults(
        data_dir='/tmp/mnist_data',
        model_dir='/tmp/mnist_model',
        batch_size=100,
        train_epochs=40)


if __name__ == '__main__':
  tf.logging.set_verbosity(tf.logging.INFO)
  main(argv=sys.argv)
