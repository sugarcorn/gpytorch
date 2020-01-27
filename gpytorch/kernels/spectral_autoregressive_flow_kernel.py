#!/usr/bin/env python3

import math

import numpy as np
import torch
from pyro import distributions as dist
from pyro.distributions.transforms import BlockAutoregressive
from scipy.fftpack import fft
from scipy.integrate import cumtrapz

from ..constraints import Positive
from ..lazy import MatmulLazyTensor, RootLazyTensor
from ..settings import num_spectral_samples
from .kernel import Kernel


class SpectralAutoregressiveFlowKernel(Kernel):
    def __init__(self, num_dims, stack_size=1, **kwargs):
        super(SpectralAutoregressiveFlowKernel, self).__init__(has_lengthscale=True, **kwargs)
        if stack_size > 1:
            self.dsf = torch.nn.ModuleList([BlockAutoregressive(num_dims, **kwargs) for _ in range(stack_size)])
        else:
            self.dsf = BlockAutoregressive(num_dims, **kwargs)
        self.num_dims = num_dims

    def _create_input_grid(self, x1, x2, diag=False, last_dim_is_batch=False, **params):
        """
        This is a helper method for creating a grid of the kernel's inputs.
        Use this helper rather than maually creating a meshgrid.

        The grid dimensions depend on the kernel's evaluation mode.

        Args:
            :attr:`x1` (Tensor `n x d` or `b x n x d`)
            :attr:`x2` (Tensor `m x d` or `b x m x d`) - for diag mode, these must be the same inputs

        Returns:
            (:class:`Tensor`, :class:`Tensor) corresponding to the gridded `x1` and `x2`.
            The shape depends on the kernel's mode

            * `full_covar`: (`b x n x 1 x d` and `b x 1 x m x d`)
            * `full_covar` with `last_dim_is_batch=True`: (`b x k x n x 1 x 1` and `b x k x 1 x m x 1`)
            * `diag`: (`b x n x d` and `b x n x d`)
            * `diag` with `last_dim_is_batch=True`: (`b x k x n x 1` and `b x k x n x 1`)
        """
        x1_, x2_ = x1, x2
        if last_dim_is_batch:
            x1_ = x1_.transpose(-1, -2).unsqueeze(-1)
            if torch.equal(x1, x2):
                x2_ = x1_
            else:
                x2_ = x2_.transpose(-1, -2).unsqueeze(-1)

        if diag:
            return x1_, x2_
        else:
            return x1_.unsqueeze(-2), x2_.unsqueeze(-3)

    def forward(self, x1, x2, diag=False, **params):
        x1_ = x1.div(self.lengthscale)
        x2_ = x2.div(self.lengthscale)
        x1_, x2_ = self._create_input_grid(x1_, x2_, diag=diag)

        diffs = x1_ - x2_
        base_dist = dist.Normal(
            torch.zeros(self.num_dims, device=x1.device, dtype=x1.dtype),
            torch.ones(self.num_dims, device=x1.device, dtype=x1.dtype),
        )
        if isinstance(self.dsf, torch.nn.ModuleList):
            dsf = list(self.dsf)
        else:
            dsf = self.dsf
        dsf_dist = dist.TransformedDistribution(base_dist, dsf)
        if self.training:
            Z = dsf_dist.rsample(torch.Size([2000]))
        else:
            Z = dsf_dist.rsample(torch.Size([2000]))

        if diag:
            diffs_times_Z = (Z.unsqueeze(-2) * diffs.unsqueeze(-3)).sum(-1)
            K = diffs_times_Z.mul(2 * math.pi).cos().mean(dim=-2)
        else:
            diffs_times_Z = (Z.unsqueeze(-2).unsqueeze(-2) * diffs.unsqueeze(-4)).sum(-1)
            K = diffs_times_Z.mul(2 * math.pi).cos().mean(dim=-3)
        return K


class NSSpectralAutoregressiveFlowKernel(SpectralAutoregressiveFlowKernel):
    def __init__(self, num_dims, stack_size=1, **kwargs):
        Kernel.__init__(self, has_lengthscale=True, **kwargs)
        if stack_size > 1:
            self.dsf = torch.nn.ModuleList([BlockAutoregressive(2 * num_dims, **kwargs) for _ in range(stack_size)])
        else:
            self.dsf = BlockAutoregressive(2 * num_dims, **kwargs)

        self.num_dims = num_dims

    def forward(self, x1, x2, diag=False, **params):
        x1_ = x1.div(self.lengthscale)
        x2_ = x2.div(self.lengthscale)
        x1_, x2_ = self._create_input_grid(x1_, x2_, diag=diag)

        base_dist = dist.Normal(
            torch.zeros(self.num_dims * 2, device=x1.device, dtype=x1.dtype),
            torch.ones(self.num_dims * 2, device=x1.device, dtype=x1.dtype),
        )

        if isinstance(self.dsf, torch.nn.ModuleList):
            dsf = list(self.dsf)
        else:
            dsf = self.dsf

        dsf_dist = dist.TransformedDistribution(base_dist, dsf)

        Z = dsf_dist.rsample(torch.Size([128]))
        Z1 = Z[:, : self.num_dims]
        Z2 = Z[:, self.num_dims :]

        Z1_ = torch.cat(([Z1, Z2, -Z1, -Z2]))
        Z2_ = torch.cat(([Z1, Z2, -Z1, -Z2]))

        Z1_ = Z1_.unsqueeze(1)
        Z2_ = Z2_.unsqueeze(1)
        if not diag:
            Z1_ = Z1_.unsqueeze(1)
            Z2_ = Z2_.unsqueeze(1)

        x1z1 = x1_ * Z1_  # s x n x 1 x d
        x2z2 = x2_ * Z2_  # s x 1 x n x d

        x1z1 = x1z1.sum(-1)  # s x n x 1
        x2z2 = x2z2.sum(-1)  # s x 1 x n

        diff = x1z1 - x2z2  # s x n x n
        K = diff.mul(2 * math.pi).cos().mean(0)  # n x n

        return K


class NSSpectralDeltaKernel(SpectralAutoregressiveFlowKernel):
    def __init__(self, num_dims, num_deltas=128, **kwargs):
        Kernel.__init__(self, has_lengthscale=True, **kwargs)

        self.Z = torch.nn.Parameter(torch.randn(num_deltas, 2 * num_dims))

        self.num_dims = num_dims

    def forward(self, x1, x2, diag=False, **params):
        x1_ = x1.div(self.lengthscale)
        x2_ = x2.div(self.lengthscale)
        x1_, x2_ = self._create_input_grid(x1_, x2_, diag=diag)

        Z = self.Z

        Z1 = Z[:, : self.num_dims]
        Z2 = Z[:, self.num_dims :]

        Z1_ = torch.cat(([Z1, Z2, -Z1, -Z2]))
        Z2_ = torch.cat(([Z1, Z2, -Z1, -Z2]))

        Z1_ = Z1_.unsqueeze(1)
        Z2_ = Z2_.unsqueeze(1)
        if not diag:
            Z1_ = Z1_.unsqueeze(1)
            Z2_ = Z2_.unsqueeze(1)

        x1z1 = x1_ * Z1_  # s x n x 1 x d
        x2z2 = x2_ * Z2_  # s x 1 x n x d

        x1z1 = x1z1.sum(-1)  # s x n x 1
        x2z2 = x2z2.sum(-1)  # s x 1 x n

        diff = x1z1 - x2z2  # s x n x n
        K = diff.mul(2 * math.pi).cos().mean(0)  # n x n

        return K


class RFNSSpectralDeltaKernel(NSSpectralDeltaKernel):
    has_lengthscale = True

    def __init__(self, num_dims, num_deltas=128, init_scale=1.0, nonstationary=False, Z_constraint=None, **kwargs):
        Kernel.__init__(self, has_lengthscale=True, **kwargs)

        if nonstationary:
            self.raw_Z = torch.nn.Parameter(init_scale * torch.rand(num_deltas, 2 * num_dims))
        else:
            self.raw_Z = torch.nn.Parameter(init_scale * torch.rand(num_deltas, num_dims))

        if Z_constraint:
            self.register_constraint("raw_Z", Z_constraint)
        else:
            self.register_constraint("raw_Z", Positive())

        self.nonstationary = nonstationary
        self.num_dims = num_dims

    def initialize_from_data(self, train_x, train_y):
        N = train_x.size(-2)
        emp_spect = np.abs(fft(train_y.cpu().detach().numpy())) ** 2 / N
        M = math.floor(N / 2)

        freq1 = np.arange(M + 1)
        freq2 = np.arange(-M + 1, 0)
        freq = np.hstack((freq1, freq2)) / N
        freq = freq[: M + 1]
        emp_spect = emp_spect[: M + 1]

        total_area = np.trapz(emp_spect, freq)
        spec_cdf = np.hstack((np.zeros(1), cumtrapz(emp_spect, freq)))
        spec_cdf = spec_cdf / total_area

        a = np.random.rand(self.raw_Z.size(-2), 1)
        p, q = np.histogram(a, spec_cdf)
        bins = np.digitize(a, q)
        slopes = (spec_cdf[bins] - spec_cdf[bins - 1]) / (freq[bins] - freq[bins - 1])
        intercepts = spec_cdf[bins - 1] - slopes * freq[bins - 1]
        inv_spec = (a - intercepts) / slopes

        self.Z = inv_spec

    def initialize_from_data_simple(self, train_x, train_y, **kwargs):
        if not torch.is_tensor(train_x) or not torch.is_tensor(train_y):
            raise RuntimeError("train_x and train_y should be tensors")
        if train_x.ndimension() == 1:
            train_x = train_x.unsqueeze(-1)
        if train_x.ndimension() == 2:
            train_x = train_x.unsqueeze(0)

        train_x_sort = train_x.sort(1)[0]
        min_dist_sort = (train_x_sort[:, 1:, :] - train_x_sort[:, :-1, :]).squeeze(0)
        ard_num_dims = 1 if self.ard_num_dims is None else self.ard_num_dims
        min_dist = torch.zeros(1, ard_num_dims, dtype=self.Z.dtype, device=self.Z.device)
        for ind in range(ard_num_dims):
            min_dist[:, ind] = min_dist_sort[(torch.nonzero(min_dist_sort[:, ind]))[0], ind]

        z_init = torch.rand_like(self.Z).mul_(0.5).div_(min_dist)

        self.Z = z_init

    @property
    def Z(self):
        return self.raw_Z_constraint.transform(self.raw_Z)

    @Z.setter
    def Z(self, value):
        self._set_Z(value)

    def _set_Z(self, value):
        if not torch.is_tensor(value):
            value = torch.as_tensor(value).to(self.raw_Z)
        self.initialize(raw_Z=self.raw_Z_constraint.inverse_transform(value))

    def forward(self, x1, x2, diag=False, **params):
        x1_ = x1.div(self.lengthscale)
        x2_ = x2.div(self.lengthscale)

        Z = self.Z

        if self.nonstationary:
            Z1 = Z[:, : self.num_dims]
            Z2 = Z[:, self.num_dims :]

            Z1_ = torch.cat(([Z1, Z2, -Z1, -Z2]))
            Z2_ = torch.cat(([Z1, Z2, -Z1, -Z2]))
        else:
            Z1_ = Z
            Z2_ = Z

        # Z1_ and Z2_ are s x d
        x1z1 = x1_.matmul(Z1_.transpose(-2, -1))  # n x s
        x2z2 = x2_.matmul(Z2_.transpose(-2, -1))  # n x s

        x1z1 = x1z1 * 2 * math.pi
        x2z2 = x2z2 * 2 * math.pi

        x1z1 = torch.cat([x1z1.cos(), x1z1.sin()], dim=-1) / math.sqrt(x1z1.size(-1))
        x2z2 = torch.cat([x2z2.cos(), x2z2.sin()], dim=-1) / math.sqrt(x2z2.size(-1))

        if x1.size() == x2.size() and torch.equal(x1, x2):
            prod = RootLazyTensor(x1z1)
        else:
            prod = MatmulLazyTensor(x1z1, x2z2.transpose(-2, -1))

        if diag:
            return prod.diag()
        else:
            return prod


class RFNSSpectralNFKernel(NSSpectralDeltaKernel):
    has_lengthscale = True

    def __init__(self, num_dims, stack_size=1, nonstationary=False, **kwargs):
        Kernel.__init__(self, has_lengthscale=True, **kwargs)

        if nonstationary:
            ndims = 2 * num_dims
        else:
            ndims = num_dims

        if stack_size > 1:
            self.dsf = torch.nn.ModuleList([BlockAutoregressive(ndims, **kwargs) for _ in range(stack_size)])
        else:
            self.dsf = BlockAutoregressive(ndims, **kwargs)

        self.num_dims = num_dims
        self.ndims = ndims
        self.nonstationary = nonstationary

    def Z(self, x1, x2, n_samples=1024):
        base_dist = dist.Normal(
            torch.zeros(self.ndims, device=x1.device, dtype=x1.dtype),
            torch.ones(self.ndims, device=x1.device, dtype=x1.dtype),
        )

        if isinstance(self.dsf, torch.nn.ModuleList):
            dsf = list(self.dsf)
        else:
            dsf = self.dsf

        dsf_dist = dist.TransformedDistribution(base_dist, dsf)

        return dsf_dist.rsample(torch.Size([n_samples]))

    def forward(self, x1, x2, diag=False, **params):
        x1_ = x1.div(self.lengthscale)
        x2_ = x2.div(self.lengthscale)

        Z = self.Z(x1_, x2_, n_samples=num_spectral_samples.value())

        if self.nonstationary:
            Z1 = Z[:, : self.num_dims]
            Z2 = Z[:, self.num_dims :]

            Z1_ = torch.cat(([Z1, Z2, -Z1, -Z2]))
            Z2_ = torch.cat(([Z1, Z2, -Z1, -Z2]))
        else:
            Z1_ = Z
            Z2_ = Z

        # Z1_ and Z2_ are s x d
        x1z1 = x1_.matmul(Z1_.transpose(-2, -1))  # n x s
        x2z2 = x2_.matmul(Z2_.transpose(-2, -1))  # n x s

        x1z1 = x1z1 * 2 * math.pi
        x2z2 = x2z2 * 2 * math.pi

        x1z1 = torch.cat([x1z1.cos(), x1z1.sin()], dim=-1) / math.sqrt(x1z1.size(-1))
        x2z2 = torch.cat([x2z2.cos(), x2z2.sin()], dim=-1) / math.sqrt(x2z2.size(-1))

        if x1.size() == x2.size() and torch.equal(x1, x2):
            prod = RootLazyTensor(x1z1)
        else:
            prod = MatmulLazyTensor(x1z1, x2z2.transpose(-2, -1))

        if diag:
            return prod.diag()
        else:
            return prod
