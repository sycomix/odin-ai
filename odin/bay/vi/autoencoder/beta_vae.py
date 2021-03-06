from typing import Dict, List, Optional, Tuple, Union

import tensorflow as tf
from odin.backend import interpolation as interp
from odin.bay.vi.autoencoder.variational_autoencoder import (
    TensorTypes, VariationalAutoencoder)
from odin.bay.vi.losses import total_correlation
from tensorflow import Tensor
from tensorflow_probability.python.distributions import Distribution


class betaVAE(VariationalAutoencoder):
  r""" Implementation of beta-VAE
  Arguments:
    beta : a Scalar. A regularizer weight indicate the capacity of the latent.

  Reference:
    Higgins, I., Matthey, L., Pal, A., et al. "beta-VAE: Learning Basic
      Visual Concepts with a Constrained Variational Framework".
      ICLR'17
  """

  def __init__(self, beta: float = 10.0, **kwargs):
    super().__init__(**kwargs)
    self.beta = beta

  @property
  def beta(self):
    if isinstance(self._beta, interp.Interpolation):
      return self._beta(self.step)
    return self._beta

  @beta.setter
  def beta(self, b):
    if isinstance(b, interp.Interpolation):
      self._beta = b
    else:
      self._beta = tf.convert_to_tensor(b, dtype=self.dtype, name='beta')

  def elbo_components(self,
                      inputs,
                      training=None,
                      pX_Z=None,
                      qZ_X=None,
                      mask=None):
    llk, kl = super().elbo_components(inputs=inputs,
                                      pX_Z=pX_Z,
                                      qZ_X=qZ_X,
                                      mask=mask,
                                      training=training)
    kl = {key: self.beta * val for key, val in kl.items()}
    return llk, kl


class betatcVAE(betaVAE):
  r""" Extend the beta-VAE with total correlation loss added.

  Based on Equation (4) with alpha = gamma = 1
  If alpha = gamma = 1, Eq. 4 can be written as
    `ELBO = LLK - (KL + (beta - 1) * TC)`.

  Reference:
    Chen, R.T.Q., Li, X., Grosse, R., Duvenaud, D., 2019. "Isolating Sources
      of Disentanglement in Variational Autoencoders".
      arXiv:1802.04942 [cs, stat].
  """

  def elbo_components(self,
                      inputs,
                      training=None,
                      pX_Z=None,
                      qZ_X=None,
                      mask=None):
    llk, kl = super().elbo_components(inputs,
                                      pX_Z=pX_Z,
                                      qZ_X=qZ_X,
                                      mask=mask,
                                      training=training)
    for z, qz in zip(self.latents, tf.nest.flatten(qZ_X)):
      tc = total_correlation(tf.convert_to_tensor(qz), qz)
      kl[f'tc_{z.name}'] = (self.beta - 1.) * tc
    return llk, kl


class annealedVAE(VariationalAutoencoder):
  r"""Creates an annealedVAE model.

  Implementing Eq. 8 of (Burgess et al. 2018)

  Arguments:
    gamma: Hyperparameter for the regularizer.
    c_max: a Scalar. Maximum capacity of the bottleneck.
      is gradually increased from zero to a value large enough to produce
      good quality reconstructions
    iter_max: an Integer. Number of iteration until reach the maximum
      capacity (start from 0).
    interpolation : a String. Type of interpolation for increasing capacity.

  Example:
    vae = annealedVAE()
    elbo = vae.elbo(x, px, qz, n_iter=1)

  Reference:
    Burgess, C.P., Higgins, I., et al. 2018. "Understanding disentangling in
      beta-VAE". arXiv:1804.03599 [cs, stat].
  """

  def __init__(self,
               gamma: float = 1.0,
               c_min: float = 0.,
               c_max: float = 25.,
               iter_max: int = 1000,
               interpolation: str = 'linear',
               **kwargs):
    super().__init__(**kwargs)
    self.gamma = tf.convert_to_tensor(gamma, dtype=self.dtype, name='gamma')
    self.interpolation = interp.get(str(interpolation))(
        vmin=tf.constant(c_min, self.dtype),
        vmax=tf.constant(c_max, self.dtype),
        norm=int(iter_max))

  def elbo_components(self,
                      inputs,
                      training=None,
                      pX_Z=None,
                      qZ_X=None,
                      mask=None):
    llk, kl = super().elbo_components(inputs,
                                      pX_Z=pX_Z,
                                      qZ_X=qZ_X,
                                      mask=mask,
                                      training=training)
    # step : training step, updated when call `.train_steps()`
    c = self.interpolation(self.step)
    kl = {key: self.gamma * tf.math.abs(val - c) for key, val in kl.items()}
    return llk, kl


# class CyclicalAnnealingVAE(betaVAE):
#   r"""
#   Reference:
#     Fu, H., Li, C., Liu, X., Gao, J., Celikyilmaz, A., Carin, L., 2019.
#       "Cyclical Annealing Schedule: A Simple Approach to Mitigating KL
#       Vanishing". arXiv:1903.10145 [cs, stat].
#   """
#   pass
