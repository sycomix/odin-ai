from __future__ import absolute_import, annotations, division, print_function

import collections
import copy
import dataclasses
import glob
import inspect
import os
import pickle
import types
from functools import partial
from numbers import Number
from typing import (Any, Callable, Dict, Iterator, List, MutableSequence,
                    Optional, Text, Tuple, Union)

import numpy as np
import tensorflow as tf
from odin.backend.alias import (parse_activation, parse_constraint,
                                parse_initializer, parse_regularizer)
from odin.backend.keras_helpers import layer2text
from odin.exp import Callback, Trainer
from odin.networks.util_layers import (Conv1DTranspose, ExpandDims, Identity,
                                       ReshapeMCMC)
from odin.utils import MD5object, as_tuple, classproperty
from scipy import sparse
from six import string_types
from tensorflow import Tensor
from tensorflow.python import keras
from tensorflow.python.data.ops.dataset_ops import DatasetV2
from tensorflow.python.framework import tensor_shape
from tensorflow.python.keras.engine import training_utils
from tensorflow.python.keras.layers import Layer
from tensorflow.python.keras.layers.convolutional import Conv as _Conv
from tensorflow.python.keras.optimizer_v2.optimizer_v2 import OptimizerV2
from tensorflow.python.keras.utils import layer_utils
from tensorflow.python.ops.summary_ops_v2 import SummaryWriter
from tensorflow.python.platform import tf_logging as logging
from tensorflow.python.training.tracking import base as trackable
from tensorflow.python.util import tf_inspect
from typing_extensions import Literal

__all__ = [
    'TensorTypes',
    'TrainStep',
    'Networks',
    'SequentialNetwork',
    'dense_network',
    'conv_network',
    'deconv_network',
    'NetworkConfig',
]


# ===========================================================================
# Helpers
# ===========================================================================
def _shape(shape):
  if shape is not None:
    if not (tf.is_tensor(shape) or isinstance(shape, tf.TensorShape) or
            isinstance(shape, np.ndarray)):
      shape = tf.nest.flatten(shape)
  return shape


def _as_arg_tuples(*args):
  ref = as_tuple(args[0], t=int)
  n = len(ref)
  return [ref] + [as_tuple(i, N=n) for i in args[1:]], n


def _infer_rank_and_input_shape(rank, input_shape):
  if rank is None and input_shape is None:
    raise ValueError(
        "rank or input_shape must be given so the convolution type "
        "can be determined.")
  if rank is not None:
    if input_shape is not None:
      input_shape = _shape(input_shape)
      if rank != (len(input_shape) - 1):
        raise ValueError("rank=%d but given input_shape=%s (rank=%d)" %
                         (rank, str(input_shape), len(input_shape) - 1))
  else:
    rank = len(input_shape) - 1
  return rank, input_shape


# ===========================================================================
# Networks
# ===========================================================================
TensorTypes = Union[sparse.spmatrix, np.ndarray, Tensor]


def _to_optimizer(optimizer, learning_rate, clipnorm):
  optimizer = tf.nest.flatten(optimizer)
  learning_rate = tf.nest.flatten(learning_rate)
  clipnorm = tf.nest.flatten(clipnorm)
  if len(learning_rate) == 1:
    learning_rate = learning_rate * len(optimizer)
  if len(clipnorm) == 1:
    clipnorm = clipnorm * len(clipnorm)
  ## create the optimizer
  all_optimizers = []
  for opt, lr, clip in zip(optimizer, learning_rate, clipnorm):
    # string
    if isinstance(opt, string_types):
      config = dict(learning_rate=float(lr))
      if clip is not None:
        config['clipnorm'] = clip
      opt = tf.optimizers.get({'class_name': opt, 'config': config})
    # the instance
    elif isinstance(opt, OptimizerV2):
      pass
    # type
    elif inspect.isclass(opt) and issubclass(opt, OptimizerV2):
      opt = opt(learning_rate=float(learning_rate)) \
        if clipnorm is None else \
        opt(learning_rate=float(learning_rate), clipnorm=clipnorm)
    # no support
    else:
      raise ValueError("No support for optimizer: %s" % str(opt))
    all_optimizers.append(opt)
  return all_optimizers


def _to_dataset(x, batch_size, dtype):
  # sparse matrix
  if isinstance(x, sparse.spmatrix):
    x = tf.SparseTensor(indices=sorted(zip(*x.nonzero())),
                        values=x.data,
                        dense_shape=x.shape)
    x = tf.data.Dataset.from_tensor_slices(x).batch(batch_size).map(
        lambda y: tf.cast(tf.sparse.to_dense(y), dtype))
  # numpy ndarray
  elif isinstance(x, np.ndarray) or tf.is_tensor(x):
    x = tf.data.Dataset.from_tensor_slices(x).batch(batch_size)
  return x


@dataclasses.dataclass
class TrainStep:
  r""" Encapsulate a training step into a class, when called return
  a tensor for loss value, and a dictionary of metrics for monitoring. """
  inputs: TensorTypes
  training: bool
  mask: Optional[TensorTypes]
  parameters: List[tf.Variable]

  def call(self) -> Tuple[Tensor, Dict[str, Any]]:
    return tf.constant(0., dtype=tf.float32), {}

  def __call__(self) -> Tuple[Tensor, Dict[str, Any]]:
    loss, metrics = self.call()
    assert tf.is_tensor(loss), \
      f"loss must be instance of Tensor but given: {type(loss)}"
    assert isinstance(metrics, dict), \
      f"metrics must be instance of dictionary but given: {type(metrics)}"
    return loss, metrics


class Networks(keras.Model, MD5object):
  r""" A more civilized version of `keras.Model`, a container of multiple
  networks that serve a computational model. """

  def __new__(cls, *args, **kwargs):
    class_tree = [c for c in type.mro(cls) if issubclass(c, keras.Model)][::-1]
    # get default arguments from parents classes
    kw = dict()
    for c in class_tree:
      spec = inspect.getfullargspec(c.__init__)
      if spec.defaults is not None:
        for key, val in zip(spec.args[::-1], spec.defaults[::-1]):
          kw[key] = val
    # update the user provided arguments
    for k, v in zip(spec.args[1:], args):
      kw[k] = v
    kw.update(kwargs)
    # deep copy is necessary here otherwise the init function will modify
    # the arguments
    kw = copy.copy(kw)
    # create the instance
    instance = super().__new__(cls, *args, **kwargs)
    # must make _init_args NonDependency (i.e. nontrackable and won't be
    # saved in save_weights)
    with trackable.no_automatic_dependency_tracking_scope(instance):
      instance._init_args = kw
    return instance

  def __init__(self,
               path: Optional[str] = None,
               step: int = 0,
               *args,
               **kwargs):
    super().__init__(name=kwargs.pop('name',
                                     type(self).__name__),
                     *args,
                     **kwargs)
    self.step = tf.Variable(step,
                            dtype=self.dtype,
                            trainable=False,
                            name="Step")
    self._save_path = path
    self.trainer = None

  @property
  def n_parameters(self) -> int:
    """ Return the total number of trainable parameters (or variables) """
    return sum(np.prod(v.shape) for v in self.trainable_variables)

  @classproperty
  def default_args(cls) -> Dict[str, Any]:
    """Return a dictionary of the default keyword arguments of all subclass start"""
    kw = dict()
    args = []
    for c in type.mro(cls)[::-1]:
      if not issubclass(c, Networks):
        continue
      spec = inspect.getfullargspec(c.__init__)
      args += spec.args
      if spec.defaults is not None:
        for key, val in zip(spec.args[::-1], spec.defaults[::-1]):
          kw[key] = val
    args = [i for i in set(args) if i not in kw and i != 'self']
    return kw

  def load_weights(self,
                   filepath: str,
                   raise_notfound: bool = False,
                   verbose: bool = False) -> Networks:
    r""" Load all the saved weights in tensorflow format at given path """
    if isinstance(filepath, string_types):
      files = glob.glob(filepath + '.*')
      # load weights
      if len(files) > 0 and (filepath + '.index') in files:
        if verbose:
          print(f"Loading weights at path: {filepath}")
        super().load_weights(filepath, by_name=False, skip_mismatch=False)
      elif raise_notfound:
        raise FileNotFoundError(
            f"Cannot find saved weights at path: {filepath}")
      # load trainer
      trainer_path = filepath + '.trainer'
      if os.path.exists(trainer_path):
        if verbose:
          print(f"Loading trainer at path: {trainer_path}")
        with open(trainer_path, 'rb') as f:
          self.trainer = pickle.load(f)
    self._save_path = filepath
    return self

  def save_weights(self,
                   filepath: Optional[str] = None,
                   overwrite: bool = True) -> Networks:
    r""" Just copy this function here to fix the `save_format` to 'tf'

    Since saving 'h5' will drop certain variables.
    """
    if filepath is None:
      filepath = self.save_path
    assert filepath is not None
    with open(filepath + '.trainer', 'wb') as f:
      pickle.dump(self.trainer, f)
    logging.get_logger().disabled = True
    super().save_weights(filepath=filepath,
                         overwrite=overwrite,
                         save_format='tf')
    logging.get_logger().disabled = False
    return self

  def train_steps(self,
                  inputs: TensorTypes,
                  training: bool = True,
                  *args,
                  **kwargs
                 ) -> Iterator[Callable[[], Tuple[Tensor, Dict[str, Any]]]]:
    yield TrainStep(inputs=inputs,
                    training=training,
                    parameters=self.trainable_variables,
                    *args,
                    **kwargs)

  def optimize(self,
               inputs: Union[TensorTypes, List[TensorTypes]],
               training: bool = True,
               optimizer: Optional[Union[List[OptimizerV2],
                                         OptimizerV2]] = None,
               allow_none_gradients: bool = False,
               track_gradients: bool = False,
               *args,
               **kwargs) -> Tuple[Tensor, Dict[str, Any]]:
    """Optimization function, could be used for autograph

    Parameters
    ----------
    inputs : Union[TensorTypes, List[TensorTypes]]
        a single or list of input tensors
    training : bool, optional
        indicating the training mode for call method, by default True
    optimizer : Optional[OptimizerV2], optional
        optimizer, by default None
    allow_none_gradients : bool, optional
        allow variables with None gradients during training, by default False
    track_gradients : bool, optional
        track and return the metrics includes the gradients' L2-norm for each
        trainable variable, by default False

    Returns
    -------
    Tuple[Tensor, Dict[str, Any]]
        loss : a Scalar, the loss Tensor used for optimization
        metrics : a Dictionary, mapping from name to values
    """
    if training:
      self.step.assign_add(1)
    ## prepare the optimizer
    if optimizer is None:
      optimizer = [self.optimizer]
    elif not isinstance(optimizer, (tuple, list)):
      optimizer = [optimizer]
    ## prepare loss and metrics
    all_metrics = {}
    total_loss = 0.
    optimizer = tf.nest.flatten(optimizer)
    n_optimizer = len(optimizer)
    ## start optimizing step-by-step
    iterator = enumerate(
        self.train_steps(inputs=inputs, training=training, *args, **kwargs))
    for step_idx, step in iterator:
      assert isinstance(step, TrainStep) or callable(step), \
        ("method train_steps must return an Iterator of TrainStep or callable, "
         f"but return type: {type(step)}")
      opt = optimizer[step_idx % n_optimizer]
      if isinstance(step, TrainStep):
        parameters = step.parameters
      else:
        parameters = self.trainable_variables
      ## for training
      if training:
        with tf.GradientTape(watch_accessed_variables=False) as tape:
          tape.watch(parameters)
          loss, metrics = step()
        # applying the gradients
        gradients = tape.gradient(loss, parameters)
        # for debugging gradients
        grads_params = [(g, p)
                        for g, p in zip(gradients, parameters)
                        if g is not None or allow_none_gradients]
        opt.apply_gradients(grads_params)
        # tracking the gradient norms for debugging
        if track_gradients:
          for g, p in grads_params:
            metrics[f'grad/{p.name}'] = tf.linalg.norm(g)
      ## for validation
      else:
        tape = None
        loss, metrics = step()
      ## update metrics and loss
      all_metrics.update(metrics)
      total_loss += loss
    ## return
    all_metrics = {i: tf.reduce_mean(j) for i, j in all_metrics.items()}
    return total_loss, all_metrics

  def fit(self,
          train: Union[TensorTypes, DatasetV2],
          valid: Optional[Union[TensorTypes, DatasetV2]] = None,
          valid_freq: int = 500,
          valid_interval: float = 0,
          optimizer: Union[str, List[str], OptimizerV2,
                           List[OptimizerV2]] = 'adam',
          learning_rate: float = 1e-3,
          clipnorm: Optional[float] = None,
          epochs: int = -1,
          max_iter: int = 1000,
          batch_size: int = 32,
          callback: Union[Callback, List[Callback]] = lambda: None,
          compile_graph: bool = True,
          autograph: bool = False,
          logging_interval: float = 3,
          skip_fitted: Union[bool, int] = False,
          terminate_on_nan: bool = True,
          logdir: Optional[str] = None,
          allow_none_gradients: bool = False,
          track_gradients: bool = False) -> Networks:
    """Override the original fit method of keras to provide simplified
    procedure with `Networks.optimize` and `Networks.train_steps`

    Parameters
    ----------
    train : Union[TensorTypes, DatasetV2]
        tensorflow Dataset for training
    valid : Optional[Union[TensorTypes, DatasetV2]], optional
        tensorflow Dataset for validation, by default None
    valid_freq : int, optional
        the frequency, in steps, for performing validation, by default 500
    valid_interval : float, optional
        the interval, in second, for performing validation, by default 0
    optimizer : Union[str, OptimizerV2], optional
        A list of optimizers is accepted in case of multiple steps training.
        If `None`, re-use stored optimizer, raise `RuntimeError` if no
        predefined optimizer found., by default 'adam'
    learning_rate : float, optional
        learning rate for initializing the optimizer, by default 1e-3
    clipnorm : Optional[float], optional
        global L2-norm value for clipping the gradients, by default None
    epochs : int, optional
        maximum number of epochs, by default -1
    max_iter : int, optional
        maximum number of iteration, by default 1000
    batch_size : int, optional
        number of examples for mini-batch, by default 32
    callback : Union[Callback, List[Callback]], optional
        a function or list of functions called every `valid_freq` steps or
        `valid_interval` seconds, by default lambda:None
    compile_graph : bool, optional
        If True, using tensorflow autograph for optimize function (about 2 times
        speed gain), otherwise, run the function in Eager mode (better for
        debugging), by default True
    autograph : bool, optional
        use autograph to compile the function, by default False
    logging_interval : float, optional
        interval, in seconds, for printing logging information, by default 3
    skip_fitted : Union[bool, int], optional
        skip this function if the model if fitted, or fitted for certain amount of
        steps, by default False
    terminate_on_nan : bool, optional
        terminate the training if NaNs returned, by default True
    logdir : Optional[str], optional
        tensorboard logging directory, by default None
    allow_none_gradients : bool, optional
        allow variables with None gradients during training, by default False
    track_gradients : bool, optional
        track and return the metrics includes the gradients' L2-norm for each
        trainable variable, by default False

    Returns
    -------
    Networks
        the network itself for method chaining

    Raises
    ------
    RuntimeError
        if the optimizer is not defined.
    """
    batch_size = int(batch_size)
    # validate the dataset
    train = _to_dataset(train, batch_size, self.dtype)
    if valid is not None:
      valid = _to_dataset(valid, batch_size, self.dtype)
    # skip training if model is fitted or reached a number of iteration
    if self.is_fitted and skip_fitted:
      if isinstance(skip_fitted, bool):
        return self
      skip_fitted = int(skip_fitted)
      if int(self.step.numpy()) >= skip_fitted:
        return self
    # create the trainer
    if self.trainer is None:
      with trackable.no_automatic_dependency_tracking_scope(self):
        trainer = Trainer(logdir=logdir)
        self.trainer = trainer
    else:
      trainer = self.trainer
    ## if already called repeat, then no need to repeat more
    if hasattr(train, 'repeat'):
      train = train.repeat(int(epochs))
    ## create the optimizer, turn off tracking so the optimizer
    # won't be saved in save_weights
    if optimizer is not None and self.optimizer is None:
      with trackable.no_automatic_dependency_tracking_scope(self):
        self.optimizer = _to_optimizer(optimizer, learning_rate, clipnorm)
    if self.optimizer is None:
      raise RuntimeError("No optimizer found!")
    ## run early stop and callback
    self.trainer.fit(
        train_ds=train,
        optimize=partial(self.optimize,
                         allow_none_gradients=allow_none_gradients,
                         track_gradients=track_gradients),
        valid_ds=valid,
        valid_freq=valid_freq,
        valid_interval=valid_interval,
        compile_graph=compile_graph,
        autograph=autograph,
        logging_interval=logging_interval,
        log_tag=self.name,
        max_iter=max_iter,
        terminate_on_nan=terminate_on_nan,
        callback=callback,
    )
    return self

  def plot_learning_curves(self,
                           path="/tmp/tmp.png",
                           summary_steps=[10, 5],
                           show_validation=True,
                           dpi=200,
                           title=None):
    r""" Plot the learning curves on train and validation sets. """
    assert self.trainer is not None, \
      "fit method must be called before plotting learning curves"
    fig = self.trainer.plot_learning_curves(path=path,
                                            summary_steps=summary_steps,
                                            show_validation=show_validation,
                                            dpi=dpi,
                                            title=title)
    if path is None:
      return fig
    return self

  @property
  def is_semi_supervised(self) -> bool:
    return False

  @property
  def summary_writer(self) -> SummaryWriter:
    if self.trainer is not None:
      return self.trainer.summary_writer
    return None

  @property
  def tensorboard(self) -> Dict[Text, Tuple[float, int, float]]:
    if self.trainer is not None:
      return self.trainer.tensorboard
    return None

  @property
  def tensorboard_logdir(self) -> str:
    if self.trainer is not None:
      return self.trainer.logdir
    return None

  def _md5_objects(self):
    varray = []
    for n, v in enumerate(self.variables):
      v = v.numpy()
      varray.append(v.shape)
      varray.append(v.ravel())
    varray.append([n])
    varray = np.concatenate(varray, axis=0)
    return varray

  @property
  def save_path(self) -> Optional[str]:
    return self._save_path

  @property
  def init_args(self) -> Dict[str, Any]:
    return self._init_args

  @property
  def is_fitted(self) -> bool:
    return self.step.numpy() > 0


# ===========================================================================
# SequentialNetwork
# ===========================================================================
class SequentialNetwork(keras.Sequential):

  def __init__(self, layers=None, name=None):
    super().__init__(layers=None if layers is None else layers, name=name)

  def __repr__(self):
    return self.__str__()

  def __str__(self):
    return layer2text(self)


# ===========================================================================
# Networks
# ===========================================================================
def dense_network(units,
                  activation='relu',
                  use_bias=True,
                  kernel_initializer='glorot_uniform',
                  bias_initializer='zeros',
                  kernel_regularizer=None,
                  bias_regularizer=None,
                  activity_regularizer=None,
                  kernel_constraint=None,
                  bias_constraint=None,
                  flatten_inputs=True,
                  batchnorm=True,
                  input_dropout=0.,
                  dropout=0.,
                  input_shape=None):
  r""" Multi-layers dense feed-forward neural network """
  (units, activation, use_bias, kernel_initializer, bias_initializer,
   kernel_regularizer, bias_regularizer, activity_regularizer,
   kernel_constraint, bias_constraint, batchnorm,
   dropout), nlayers = _as_arg_tuples(units, activation, use_bias,
                                      kernel_initializer, bias_initializer,
                                      kernel_regularizer, bias_regularizer,
                                      activity_regularizer, kernel_constraint,
                                      bias_constraint, batchnorm, dropout)
  layers = []
  if input_shape is not None:
    layers.append(keras.layers.InputLayer(input_shape=input_shape))
  if flatten_inputs:
    layers.append(keras.layers.Flatten())
  if input_dropout > 0:
    layers.append(keras.layers.Dropout(rate=input_dropout))
  for i in range(nlayers):
    layers.append(
        keras.layers.Dense(\
          units[i],
          activation='linear',
          use_bias=(not batchnorm[i]) and use_bias[i],
          kernel_initializer=kernel_initializer[i],
          bias_initializer=bias_initializer[i],
          kernel_regularizer=kernel_regularizer[i],
          bias_regularizer=bias_regularizer[i],
          activity_regularizer=activity_regularizer[i],
          kernel_constraint=kernel_constraint[i],
          bias_constraint=bias_constraint[i],
          name="Layer%d" % i))
    if batchnorm[i]:
      layers.append(keras.layers.BatchNormalization())
    layers.append(keras.layers.Activation(activation[i]))
    if dropout[i] > 0:
      layers.append(keras.layers.Dropout(rate=dropout[i]))
  return layers


def conv_network(units,
                 rank=2,
                 kernel=3,
                 strides=1,
                 padding='same',
                 dilation=1,
                 activation='relu',
                 use_bias=True,
                 kernel_initializer='glorot_uniform',
                 bias_initializer='zeros',
                 kernel_regularizer=None,
                 bias_regularizer=None,
                 activity_regularizer=None,
                 kernel_constraint=None,
                 bias_constraint=None,
                 batchnorm=True,
                 input_dropout=0.,
                 dropout=0.,
                 projection=False,
                 input_shape=None,
                 name=None):
  r""" Multi-layers convolutional neural network

  Arguments:
    projection : {True, False, an Integer}.
      If True, flatten the output into 2-D.
      If an Integer, use a `Dense` layer with linear activation to project
      the output in to 2-D
  """
  rank, input_shape = _infer_rank_and_input_shape(rank, input_shape)
  (units, kernel, strides, padding, dilation, activation, use_bias,
   kernel_initializer, bias_initializer, kernel_regularizer, bias_regularizer,
   activity_regularizer, kernel_constraint, bias_constraint, batchnorm,
   dropout), nlayers = _as_arg_tuples(units, kernel, strides, padding, dilation,
                                      activation, use_bias, kernel_initializer,
                                      bias_initializer, kernel_regularizer,
                                      bias_regularizer, activity_regularizer,
                                      kernel_constraint, bias_constraint,
                                      batchnorm, dropout)

  layers = []
  if input_shape is not None:
    layers.append(keras.layers.InputLayer(input_shape=input_shape))
  if 0. < input_dropout < 1.:
    layers.append(keras.layers.Dropout(rate=input_dropout))

  if rank == 3:
    layer_type = keras.layers.Conv3D
  elif rank == 2:
    layer_type = keras.layers.Conv2D
  elif rank == 1:
    layer_type = keras.layers.Conv1D

  for i in range(nlayers):
    layers.append(
        layer_type(\
          filters=units[i],
          kernel_size=kernel[i],
          strides=strides[i],
          padding=padding[i],
          dilation_rate=dilation[i],
          activation='linear',
          use_bias=(not batchnorm[i]) and use_bias[i],
          kernel_initializer=kernel_initializer[i],
          bias_initializer=bias_initializer[i],
          kernel_regularizer=kernel_regularizer[i],
          bias_regularizer=bias_regularizer[i],
          activity_regularizer=activity_regularizer[i],
          kernel_constraint=kernel_constraint[i],
          bias_constraint=bias_constraint[i],
          name="Layer%d" % i))
    if batchnorm[i]:
      layers.append(keras.layers.BatchNormalization())
    layers.append(keras.layers.Activation(activation[i]))
    if dropout[i] > 0:
      layers.append(keras.layers.Dropout(rate=dropout[i]))
  # projection
  if isinstance(projection, bool):
    if projection:
      layers.append(keras.layers.Flatten())
  elif isinstance(projection, Number):
    layers.append(keras.layers.Flatten())
    layers.append(
        keras.layers.Dense(int(projection), activation='linear', use_bias=True))
  return layers


def deconv_network(units,
                   rank=2,
                   kernel=3,
                   strides=1,
                   padding='same',
                   output_padding=None,
                   dilation=1,
                   activation='relu',
                   use_bias=True,
                   kernel_initializer='glorot_uniform',
                   bias_initializer='zeros',
                   kernel_regularizer=None,
                   bias_regularizer=None,
                   activity_regularizer=None,
                   kernel_constraint=None,
                   bias_constraint=None,
                   batchnorm=True,
                   input_dropout=0.,
                   dropout=0.,
                   projection=None,
                   input_shape=None):
  r""" Multi-layers transposed convolutional neural network """
  rank, input_shape = _infer_rank_and_input_shape(rank, input_shape)
  (units, kernel, strides, padding, output_padding, dilation, activation,
   use_bias, kernel_initializer, bias_initializer, kernel_regularizer,
   bias_regularizer, activity_regularizer, kernel_constraint,
   bias_constraint, batchnorm, dropout), nlayers = _as_arg_tuples(
       units, kernel, strides, padding, output_padding, dilation, activation,
       use_bias, kernel_initializer, bias_initializer, kernel_regularizer,
       bias_regularizer, activity_regularizer, kernel_constraint,
       bias_constraint, batchnorm, dropout)
  #
  layers = []
  if input_shape is not None:
    layers.append(keras.layers.InputLayer(input_shape=input_shape))
  if 0. < input_dropout < 1.:
    layers.append(keras.layers.Dropout(input_dropout))
  #
  if rank == 3:
    raise NotImplementedError
  elif rank == 2:
    layer_type = keras.layers.Conv2DTranspose
  elif rank == 1:
    layer_type = Conv1DTranspose

  for i in range(nlayers):
    layers.append(
        layer_type(\
          filters=units[i],
          kernel_size=kernel[i],
          strides=strides[i],
          padding=padding[i],
          output_padding=output_padding[i],
          dilation_rate=dilation[i],
          activation='linear',
          use_bias=(not batchnorm[i]) and use_bias[i],
          kernel_initializer=kernel_initializer[i],
          bias_initializer=bias_initializer[i],
          kernel_regularizer=kernel_regularizer[i],
          bias_regularizer=bias_regularizer[i],
          activity_regularizer=activity_regularizer[i],
          kernel_constraint=kernel_constraint[i],
          bias_constraint=bias_constraint[i],
          name="Layer%d" % i))
    if batchnorm[i]:
      layers.append(keras.layers.BatchNormalization())
    layers.append(keras.layers.Activation(activation[i]))
    if dropout[i] > 0:
      layers.append(keras.layers.Dropout(rate=dropout[i]))
  # projection
  if isinstance(projection, bool):
    if projection:
      layers.append(keras.layers.Flatten())
  elif isinstance(projection, Number):
    layers.append(keras.layers.Flatten())
    layers.append(
        keras.layers.Dense(int(projection), activation='linear', use_bias=True))
  return layers


# ===========================================================================
# Serializable configuration
# ===========================================================================
@dataclasses.dataclass(init=True,
                       repr=True,
                       eq=True,
                       order=False,
                       unsafe_hash=False,
                       frozen=False)
class NetworkConfig(dict):
  r""" A dataclass for storing the autoencoder networks (encoder and decoder)
  configuration. Number of layers is determined by length of `units`

  Arguments:
    units : a list of Integer. Number of hidden units for each hidden layers
    kernel : a list of Integer, kernel size for convolution network
    strides : a list of Integer, stride step for convoltion
    activation : a String, alias of activation function
    input_dropout : A Scalar [0., 1.], dropout rate, if 0., turn-off dropout.
      this rate is applied for input layer.
    dropout : a list of Scalar [0., 1.], dropout rate between two hidden layers
    batchnorm : a Boolean, batch normalization
    linear_decoder : a Boolean, if `True`, use an `Identity` (i.e. Linear)
      decoder
    network : {'conv', 'deconv', 'dense'}.
      type of `Layer` for the network
    flatten_inputs: a Boolean. Flatten the inputs to 2D in case of `Dense`
      network
    projection : An Integer, number of hidden units for the `Dense`
      linear projection layer right after convolutional network.

  """

  units: Union[int, List[int]] = 64
  kernel: Union[int, List[int]] = 3
  strides: Union[int, List[int]] = 1
  dilation: Union[int, List[int]] = 1
  padding: Union[str, List[str]] = 'same'
  activation: Union[str, List[str]] = 'relu'
  use_bias: Union[bool, List[bool]] = True
  kernel_initializer: Union[str, List[str]] = 'glorot_uniform'
  bias_initializer: Union[str, List[str]] = 'zeros'
  kernel_regularizer: Union[str, List[str]] = None
  bias_regularizer: Union[str, List[str]] = None
  activity_regularizer: Union[str, List[str]] = None
  kernel_constraint: Union[str, List[str]] = None
  bias_constraint: Union[str, List[str]] = None
  batchnorm: Union[bool, List[bool]] = False
  input_dropout: float = 0.
  dropout: Union[float, List[float]] = 0.
  linear_decoder: bool = False
  network: Literal['conv', 'deconv', 'dense'] = 'dense'
  flatten_inputs: bool = True
  projection: Optional[int] = None
  input_shape: List[int] = None
  name: Optional[str] = None

  def __post_init__(self):
    if not isinstance(self.units, collections.Iterable):
      self.units = tf.nest.flatten(self.units)
    self.units = [int(i) for i in self.units]
    network_types = ('deconv', 'conv', 'dense', 'lstm', 'gru', 'rnn')
    assert self.network in network_types, \
      "Given network '%s', only support: %s" % (self.network, network_types)

  def keys(self):
    for i in dataclasses.fields(self):
      yield i.name

  def values(self):
    for i in dataclasses.fields(self):
      yield i.default

  def __iter__(self):
    for i in dataclasses.fields(self):
      yield i.name, i.default

  def __len__(self):
    return len(dataclasses.fields(self))

  def __getitem__(self, key):
    return getattr(self, key)

  def copy(self, **kwargs):
    obj = copy.deepcopy(self)
    return dataclasses.replace(obj, **kwargs)

  ################ Create the networks
  def create_autoencoder(
      self,
      input_shape: List[int],
      latent_shape: List[int],
      name: Optional[str] = None
  ) -> Tuple[SequentialNetwork, SequentialNetwork]:
    r""" Create both encoder and decoder at once """
    encoder_name = None if name is None else f"{name}_encoder"
    decoder_name = None if name is None else f"{name}_decoder"
    encoder = self.create_network(input_shape=input_shape, name=encoder_name)
    decoder = self.create_decoder(encoder=encoder,
                                  latent_shape=latent_shape,
                                  name=decoder_name)
    return encoder, decoder

  def create_decoder(self,
                     encoder: Layer,
                     latent_shape: List[int],
                     n_parameterization: int = 1,
                     name: Optional[str] = None) -> SequentialNetwork:
    r"""
    Arguments:
      latent_shape : a tuple of Integer. Shape of latent without the batch
         dimensions.
      n_parameterization : number of parameters in case the output of decoder
        parameterize a distribution
      name : a String (optional).

    Returns:
      decoder : keras.Sequential
    """
    if name is None:
      name = "Decoder"
    latent_shape = _shape(latent_shape)
    input_shape = encoder.input_shape[1:]
    n_channels = input_shape[-1]
    rank = 1 if len(input_shape) == 2 else 2
    # ====== linear decoder ====== #
    if self.linear_decoder:
      return Identity(name=name, input_shape=latent_shape)
    ### convolution network
    if self.network == 'conv':
      # get the last convolution shape
      eshape = encoder.layers[-3].output_shape[1:]
      start_layers = [keras.layers.InputLayer(input_shape=latent_shape)]
      if self.projection is not None and not isinstance(self.projection, bool):
        start_layers += [
            keras.layers.Dense(int(self.projection),
                               activation='linear',
                               use_bias=True),
            keras.layers.Dense(np.prod(eshape),
                               activation=self.activation,
                               use_bias=True),
            keras.layers.Reshape(eshape),
        ]
      decoder = deconv_network(
          tf.nest.flatten(self.units)[::-1][1:] +
          [n_channels * int(n_parameterization)],
          rank=rank,
          kernel=tf.nest.flatten(self.kernel)[::-1],
          strides=tf.nest.flatten(self.strides)[::-1],
          padding=tf.nest.flatten(self.padding)[::-1],
          dilation=tf.nest.flatten(self.dilation)[::-1],
          activation=tf.nest.flatten(self.activation)[::-1],
          use_bias=tf.nest.flatten(self.use_bias)[::-1],
          batchnorm=tf.nest.flatten(self.batchnorm)[::-1],
          input_dropout=self.input_dropout,
          dropout=tf.nest.flatten(self.dropout)[::-1],
          kernel_initializer=self.kernel_initializer,
          bias_initializer=self.bias_initializer,
          kernel_regularizer=self.kernel_regularizer,
          bias_regularizer=self.bias_regularizer,
          activity_regularizer=self.activity_regularizer,
          kernel_constraint=self.kernel_constraint,
          bias_constraint=self.bias_constraint,
      )
      decoder = start_layers + decoder
      decoder.append(keras.layers.Reshape(input_shape))
    ### dense network
    elif self.network == 'dense':
      decoder = dense_network(
          tf.nest.flatten(self.units)[::-1],
          activation=tf.nest.flatten(self.activation)[::-1],
          use_bias=tf.nest.flatten(self.use_bias)[::-1],
          batchnorm=tf.nest.flatten(self.batchnorm)[::-1],
          input_dropout=self.input_dropout,
          dropout=tf.nest.flatten(self.dropout)[::-1],
          kernel_initializer=self.kernel_initializer,
          bias_initializer=self.bias_initializer,
          kernel_regularizer=self.kernel_regularizer,
          bias_regularizer=self.bias_regularizer,
          activity_regularizer=self.activity_regularizer,
          kernel_constraint=self.kernel_constraint,
          bias_constraint=self.bias_constraint,
          flatten_inputs=self.flatten_inputs,
          input_shape=latent_shape,
      )
    ### deconv
    else:
      raise NotImplementedError("'%s' network doesn't support decoding." %
                                self.network)
    decoder = SequentialNetwork(decoder, name=name)
    decoder.copy = types.MethodType(
        lambda s, name=None: self.create_decoder(
            encoder=encoder,
            latent_shape=latent_shape,
            n_parameterization=n_parameterization,
            name=name,
        ),
        decoder,
    )
    return decoder

  def __call__(
      self,
      input_shape: Optional[List[int]] = None,
      sequential: bool = True,
      name: Optional[str] = None) -> Union[SequentialNetwork, List[Layer]]:
    return self.create_network(input_shape=input_shape,
                               sequential=sequential,
                               name=name)

  def create_network(
      self,
      input_shape: Optional[List[int]] = None,
      sequential: bool = True,
      name: Optional[str] = None) -> Union[SequentialNetwork, List[Layer]]:
    r"""
    Arguments:
      input_shape : a tuple of Integer. Shape of input without the batch
         dimensions.
      name : a String (optional).

    Returns:
      encoder : keras.Sequential
    """
    if self.name is not None:
      name = self.name
    ### prepare the shape
    input_shape = _shape(
        self.input_shape if input_shape is None else input_shape)
    ### convolution network
    if self.network == 'conv':
      # create the encoder
      network = conv_network(self.units,
                             kernel=self.kernel,
                             strides=self.strides,
                             padding=self.padding,
                             dilation=self.dilation,
                             activation=self.activation,
                             use_bias=self.use_bias,
                             batchnorm=self.batchnorm,
                             input_dropout=self.input_dropout,
                             dropout=self.dropout,
                             kernel_initializer=self.kernel_initializer,
                             bias_initializer=self.bias_initializer,
                             kernel_regularizer=self.kernel_regularizer,
                             bias_regularizer=self.bias_regularizer,
                             activity_regularizer=self.activity_regularizer,
                             kernel_constraint=self.kernel_constraint,
                             bias_constraint=self.bias_constraint,
                             projection=self.projection,
                             input_shape=input_shape)
    ### dense network
    elif self.network == 'dense':
      network = dense_network(self.units,
                              activation=self.activation,
                              use_bias=self.use_bias,
                              batchnorm=self.batchnorm,
                              input_dropout=self.input_dropout,
                              dropout=self.dropout,
                              kernel_initializer=self.kernel_initializer,
                              bias_initializer=self.bias_initializer,
                              kernel_regularizer=self.kernel_regularizer,
                              bias_regularizer=self.bias_regularizer,
                              activity_regularizer=self.activity_regularizer,
                              kernel_constraint=self.kernel_constraint,
                              bias_constraint=self.bias_constraint,
                              flatten_inputs=self.flatten_inputs,
                              input_shape=input_shape)
    ### deconv
    elif self.network == 'deconv':
      network = deconv_network(self.units,
                               kernel=self.kernel,
                               strides=self.strides,
                               padding=self.padding,
                               dilation=self.dilation,
                               activation=self.activation,
                               use_bias=self.use_bias,
                               batchnorm=self.batchnorm,
                               input_dropout=self.input_dropout,
                               dropout=self.dropout,
                               kernel_initializer=self.kernel_initializer,
                               bias_initializer=self.bias_initializer,
                               kernel_regularizer=self.kernel_regularizer,
                               bias_regularizer=self.bias_regularizer,
                               activity_regularizer=self.activity_regularizer,
                               kernel_constraint=self.kernel_constraint,
                               bias_constraint=self.bias_constraint,
                               projection=self.projection,
                               input_shape=input_shape)
    ### others
    else:
      raise NotImplementedError("No implementation for network of type: '%s'" %
                                self.network)
    # ====== return ====== #
    if sequential:
      network = SequentialNetwork(network, name=name)
      network.copy = types.MethodType(
          lambda s, name=None: self.create_network(input_shape=input_shape,
                                                   name=name),
          network,
      )
    return network
