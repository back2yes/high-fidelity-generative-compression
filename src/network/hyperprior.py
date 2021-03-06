import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
from collections import namedtuple

# Custom
from src.helpers import maths, utils

MIN_SCALE = 0.11
LOG_SCALES_MIN = -3.
MIN_LIKELIHOOD = 1e-9
MAX_LIKELIHOOD = 1e3
SMALL_HYPERLATENT_FILTERS = 192
LARGE_HYPERLATENT_FILTERS = 320

HyperInfo = namedtuple(
    "HyperInfo",
    "decoded "
    "latent_nbpp hyperlatent_nbpp total_nbpp latent_qbpp hyperlatent_qbpp total_qbpp "
    "bitstring side_bitstring",
)

lower_bound_identity = maths.LowerBoundIdentity.apply
lower_bound_toward = maths.LowerBoundToward.apply

class CodingModel(nn.Module):
    """
    Probability model for estimation of (cross)-entropies in the context
    of data compression. TODO: Add tensor -> string compression and
    decompression functionality.
    """

    def __init__(self, n_channels, min_likelihood=MIN_LIKELIHOOD, max_likelihood=MAX_LIKELIHOOD):
        super(CodingModel, self).__init__()
        self.n_channels = n_channels
        self.min_likelihood = float(min_likelihood)
        self.max_likelihood = float(max_likelihood)

    def _quantize(self, x, mode='noise', means=None):
        """
        mode:       If 'noise', returns continuous relaxation of hard
                    quantization through additive uniform noise channel.
                    Otherwise perform actual quantization (through rounding).
        """

        if mode == 'noise':
            quantization_noise = torch.nn.init.uniform_(torch.zeros_like(x), -0.5, 0.5)
            x = x + quantization_noise

        elif mode == 'quantize':
            if means is not None:
                x = x - means
                x = torch.floor(x + 0.5)
                x = x + means
            else:
                x = torch.floor(x + 0.5)
        else:
            raise NotImplementedError
        
        return x

    def _estimate_entropy(self, likelihood, spatial_shape):

        EPS = 1e-9  
        quotient = -np.log(2.)
        batch_size = likelihood.size()[0]

        assert len(spatial_shape) == 2, 'Mispecified spatial dims'
        n_pixels = np.prod(spatial_shape)

        log_likelihood = torch.log(likelihood + EPS)
        n_bits = torch.sum(log_likelihood) / (batch_size * quotient)
        bpp = n_bits / n_pixels

        return n_bits, bpp

    def _estimate_entropy_log(self, log_likelihood, spatial_shape):

        quotient = -np.log(2.)
        batch_size = log_likelihood.size()[0]

        assert len(spatial_shape) == 2, 'Mispecified spatial dims'
        n_pixels = np.prod(spatial_shape)

        n_bits = torch.sum(log_likelihood) / (batch_size * quotient)
        bpp = n_bits / n_pixels

        return n_bits, bpp

    def quantize_latents_st(self, inputs, means=None):
        # Latents rounded instead of additive uniform noise
        # Ignore rounding in backward pass
        values = inputs

        if means is not None:
            values = values - means

        delta = (torch.floor(values + 0.5) - values).detach()
        values = values + delta

        if means is not None:
            values = values + means

        return values

    def latent_likelihood(self, x, mean, scale):

        # Assumes 1 - CDF(x) = CDF(-x)
        x = x - mean
        x = torch.abs(x)
        cdf_upper = self.standardized_CDF((0.5 - x) / scale)
        cdf_lower = self.standardized_CDF(-(0.5 + x) / scale)

        # Naive
        # cdf_upper = self.standardized_CDF( (x + 0.5) / scale )
        # cdf_lower = self.standardized_CDF( (x - 0.5) / scale )

        likelihood_ = cdf_upper - cdf_lower
        likelihood_ = lower_bound_toward(likelihood_, self.min_likelihood)

        return likelihood_


class PriorDensity(nn.Module):
    """
    Probability model for latents y. Based on Sec. 3. of [1].
    Returns convolution of Gaussian latent density with parameterized 
    mean and variance with uniform distribution U(-1/2, 1/2).

    [1] Ballé et. al., "Variational image compression with a scale hyperprior", 
        arXiv:1802.01436 (2018).
    """

    def __init__(self, n_channels, min_likelihood=MIN_LIKELIHOOD, max_likelihood=MAX_LIKELIHOOD,
        scale_lower_bound=MIN_SCALE, likelihood='gaussian', **kwargs):
        super(PriorDensity, self).__init__()

        self.n_channels = n_channels
        self.min_likelihood = float(min_likelihood)
        self.max_likelihood = float(max_likelihood)
        self.scale_lower_bound = scale_lower_bound


class HyperpriorDensity(nn.Module):
    """
    Probability model for hyper-latents z. Based on Sec. 6.1. of [1].
    Returns convolution of non-parametric hyperlatent density with uniform distribution 
    U(-1/2, 1/2).

    [1] Ballé et. al., "Variational image compression with a scale hyperprior", 
        arXiv:1802.01436 (2018).
    """

    def __init__(self, n_channels, init_scale=10., filters=(3, 3, 3), min_likelihood=MIN_LIKELIHOOD, 
        max_likelihood=MAX_LIKELIHOOD, **kwargs):
        """
        init_scale: Scaling factor determining the initial width of the
                    probability densities.
        filters:    Number of filters at each layer < K
                    of the density model. Default K=4 layers.
        """
        super(HyperpriorDensity, self).__init__()
        
        self.init_scale = float(init_scale)
        self.filters = tuple(int(f) for f in filters)
        self.min_likelihood = float(min_likelihood)
        self.max_likelihood = float(max_likelihood)

        filters = (1,) + self.filters + (1,)
        scale = self.init_scale ** (1 / (len(self.filters) + 1))

        # Define univariate density model 
        for k in range(len(self.filters)+1):
            
            # Weights
            H_init = np.log(np.expm1(1 / scale / filters[k + 1]))
            H_k = nn.Parameter(torch.ones((n_channels, filters[k+1], filters[k])))  # apply softmax for non-negativity
            torch.nn.init.constant_(H_k, H_init)
            self.register_parameter('H_{}'.format(k), H_k)

            # Scale factors
            a_k = nn.Parameter(torch.zeros((n_channels, filters[k+1], 1)))
            self.register_parameter('a_{}'.format(k), a_k)

            # Biases
            b_k = nn.Parameter(torch.zeros((n_channels, filters[k+1], 1)))
            torch.nn.init.uniform_(b_k, -0.5, 0.5)
            self.register_parameter('b_{}'.format(k), b_k)

    def cdf_logits(self, x, update_parameters=True):
        """
        Evaluate logits of the cumulative densities. 
        Independent density model for each channel.

        x:  The values at which to evaluate the cumulative densities.
            torch.Tensor - shape `(C, 1, *)`.
        """
        logits = x

        for k in range(len(self.filters)+1):
            H_k = getattr(self, 'H_{}'.format(str(k)))  # Weight
            a_k = getattr(self, 'a_{}'.format(str(k)))  # Scale
            b_k = getattr(self, 'b_{}'.format(str(k)))  # Bias

            if update_parameters is False:
                H_k, a_k, b_k = H_k.detach(), a_k.detach(), b_k.detach()
            logits = torch.bmm(F.softplus(H_k), logits)  # [C,filters[k+1],*]
            logits = logits + b_k
            logits = logits + torch.tanh(a_k) * torch.tanh(logits)

        return logits


    def likelihood(self, x):
        """
        Expected input: (N,C,H,W)
        """
        latents = x

        # Converts latents to (C,1,*) format
        N, C, H, W = latents.size()
        latents = latents.permute(1,0,2,3)
        shape = latents.shape
        latents = torch.reshape(latents, (shape[0],1,-1))

        cdf_upper = self.cdf_logits(latents + 0.5)
        cdf_lower = self.cdf_logits(latents - 0.5)

        # Numerical stability using some sigmoid identities
        # to avoid subtraction of two numbers close to 1
        sign = -torch.sign(cdf_upper + cdf_lower)
        sign = sign.detach()
        likelihood_ = torch.abs(
            torch.sigmoid(sign * cdf_upper) - torch.sigmoid(sign * cdf_lower))

        # Naive
        # likelihood_ = torch.sigmoid(cdf_upper) - torch.sigmoid(cdf_lower)

        # Reshape to (N,C,H,W)
        likelihood_ = torch.reshape(likelihood_, shape)
        likelihood_ = likelihood_.permute(1,0,2,3)

        likelihood_ = lower_bound_toward(likelihood_, self.min_likelihood)

        return likelihood_ #, max=self.max_likelihood)

    def forward(self, x, **kwargs):
        return self.likelihood(x)



class Hyperprior(CodingModel):
    
    def __init__(self, bottleneck_capacity=220, hyperlatent_filters=LARGE_HYPERLATENT_FILTERS, mode='large',
        likelihood_type='gaussian', scale_lower_bound=MIN_SCALE):
        """
        Introduces probabilistic model over latents of 
        latents.

        The hyperprior over the standard latents is modelled as
        a non-parametric, fully factorized density.
        """
        super(Hyperprior, self).__init__(n_channels=bottleneck_capacity)
        
        self.bottleneck_capacity = bottleneck_capacity
        self.scale_lower_bound = scale_lower_bound

        analysis_net = HyperpriorAnalysis
        synthesis_net = HyperpriorSynthesis

        if mode == 'small':
            hyperlatent_filters = SMALL_HYPERLATENT_FILTERS

        self.analysis_net = analysis_net(C=bottleneck_capacity, N=hyperlatent_filters)

        # TODO: Combine scale, loc into single network
        self.synthesis_mu = synthesis_net(C=bottleneck_capacity, N=hyperlatent_filters)
        self.synthesis_std = synthesis_net(C=bottleneck_capacity, N=hyperlatent_filters)
            #final_activation='softplus')
        
        self.amortization_models = [self.analysis_net, self.synthesis_mu, self.synthesis_std]

        self.hyperlatent_likelihood = HyperpriorDensity(n_channels=hyperlatent_filters)

        if likelihood_type == 'gaussian':
            self.standardized_CDF = maths.standardized_CDF_gaussian
        elif likelihood_type == 'logistic':
            self.standardized_CDF = maths.standardized_CDF_logistic
        else:
            raise ValueError('Unknown likelihood model: {}'.format(likelihood_type))


    def forward(self, latents, spatial_shape, **kwargs):

        hyperlatents = self.analysis_net(latents)
        
        # Mismatch b/w continuous and discrete cases?
        # Differential entropy, hyperlatents
        noisy_hyperlatents = self._quantize(hyperlatents, mode='noise')
        noisy_hyperlatent_likelihood = self.hyperlatent_likelihood(noisy_hyperlatents)
        noisy_hyperlatent_bits, noisy_hyperlatent_bpp = self._estimate_entropy(
            noisy_hyperlatent_likelihood, spatial_shape)

        # Discrete entropy, hyperlatents
        quantized_hyperlatents = self._quantize(hyperlatents, mode='quantize')
        quantized_hyperlatent_likelihood = self.hyperlatent_likelihood(quantized_hyperlatents)
        quantized_hyperlatent_bits, quantized_hyperlatent_bpp = self._estimate_entropy(
            quantized_hyperlatent_likelihood, spatial_shape)

        if self.training is True:
            hyperlatents_decoded = noisy_hyperlatents
        else:
            hyperlatents_decoded = quantized_hyperlatents

        latent_means = self.synthesis_mu(hyperlatents_decoded)
        latent_scales = self.synthesis_std(hyperlatents_decoded)
        # latent_scales = F.softplus(latent_scales)
        latent_scales = lower_bound_toward(latent_scales, self.scale_lower_bound)

        # Differential entropy, latents
        noisy_latents = self._quantize(latents, mode='noise', means=latent_means)
        noisy_latent_likelihood = self.latent_likelihood(noisy_latents, mean=latent_means,
            scale=latent_scales)
        noisy_latent_bits, noisy_latent_bpp = self._estimate_entropy(
            noisy_latent_likelihood, spatial_shape)     

        # Discrete entropy, latents
        quantized_latents = self._quantize(latents, mode='quantize', means=latent_means)
        quantized_latent_likelihood = self.latent_likelihood(quantized_latents, mean=latent_means,
            scale=latent_scales)
        quantized_latent_bits, quantized_latent_bpp = self._estimate_entropy(
            quantized_latent_likelihood, spatial_shape)


        # if self.training is True:
        #     latents_decoded = self.quantize_latents_st(latents, latent_means)
        # else:
        #     latents_decoded = quantized_latents

        latents_decoded = self.quantize_latents_st(latents, latent_means)

        info = HyperInfo(
            decoded=latents_decoded,
            latent_nbpp=noisy_latent_bpp,
            hyperlatent_nbpp=noisy_hyperlatent_bpp,
            total_nbpp=noisy_latent_bpp + noisy_hyperlatent_bpp,
            latent_qbpp=quantized_latent_bpp,
            hyperlatent_qbpp=quantized_hyperlatent_bpp,
            total_qbpp=quantized_latent_bpp + quantized_hyperlatent_bpp,
            bitstring=None,  # TODO
            side_bitstring=None, # TODO
        )

        return info

class HyperpriorAnalysis(nn.Module):
    """
    Hyperprior 'analysis model' as proposed in [1]. 

    [1] Ballé et. al., "Variational image compression with a scale hyperprior", 
        arXiv:1802.01436 (2018).

    C:  Number of input channels
    """
    def __init__(self, C=220, N=320, activation='relu'):
        super(HyperpriorAnalysis, self).__init__()

        cnn_kwargs = dict(kernel_size=5, stride=2, padding=2, padding_mode='reflect')
        self.activation = getattr(F, activation)
        self.n_downsampling_layers = 2

        self.conv1 = nn.Conv2d(C, N, kernel_size=3, stride=1, padding=1)
        self.conv2 = nn.Conv2d(N, N, **cnn_kwargs)
        self.conv3 = nn.Conv2d(N, N, **cnn_kwargs)

    def forward(self, x):
        
        # x = torch.abs(x)
        x = self.activation(self.conv1(x))
        x = self.activation(self.conv2(x))
        x = self.conv3(x)

        return x


class HyperpriorSynthesis(nn.Module):
    """
    Hyperprior 'synthesis model' as proposed in [1]. Outputs 
    distribution parameters of input latents.

    [1] Ballé et. al., "Variational image compression with a scale hyperprior", 
        arXiv:1802.01436 (2018).

    C:  Number of output channels
    """
    def __init__(self, C=220, N=320, activation='relu', final_activation=None):
        super(HyperpriorSynthesis, self).__init__()

        cnn_kwargs = dict(kernel_size=5, stride=2, padding=2, output_padding=1)
        self.activation = getattr(F, activation)
        self.final_activation = final_activation

        self.conv1 = nn.ConvTranspose2d(N, N, **cnn_kwargs)
        self.conv2 = nn.ConvTranspose2d(N, N, **cnn_kwargs)
        self.conv3 = nn.ConvTranspose2d(N, C, kernel_size=3, stride=1, padding=1)

        if self.final_activation is not None:
            self.final_activation = getattr(F, final_activation)

    def forward(self, x):
        x = self.activation(self.conv1(x))
        x = self.activation(self.conv2(x))
        x = self.conv3(x)

        if self.final_activation is not None:
            x = self.final_activation(x)
        return x

"""
========
Discretized logistic mixture model.
========
"""

class HyperpriorDLMM(CodingModel):
    
    def __init__(self, bottleneck_capacity=64, hyperlatent_filters=LARGE_HYPERLATENT_FILTERS, mode='large',
        likelihood_type='gaussian', scale_lower_bound=MIN_SCALE, mixture_components=4):
        """
        Introduces probabilistic model over latents of 
        latents.

        The hyperprior over the standard latents is modelled as
        a non-parametric, fully factorized density.
        """
        super(HyperpriorDLMM, self).__init__(n_channels=bottleneck_capacity)
        
        assert bottleneck_capacity <= 128, 'Will probably run out of memory!'
        self.bottleneck_capacity = bottleneck_capacity
        self.scale_lower_bound = scale_lower_bound
        self.mixture_components = mixture_components

        analysis_net = HyperpriorAnalysis
        synthesis_net = HyperpriorSynthesisDLMM

        if mode == 'small':
            hyperlatent_filters = SMALL_HYPERLATENT_FILTERS

        self.analysis_net = analysis_net(C=bottleneck_capacity, N=hyperlatent_filters)

        # TODO: Combine scale, loc into single network
        self.synthesis_DLMM_params = synthesis_net(C=bottleneck_capacity, N=hyperlatent_filters)
    
        self.amortization_models = [self.analysis_net, self.synthesis_DLMM_params]

        self.hyperlatent_likelihood = HyperpriorDensity(n_channels=hyperlatent_filters)

        if likelihood_type == 'gaussian':
            self.standardized_CDF = maths.standardized_CDF_gaussian
        elif likelihood_type == 'logistic':
            self.standardized_CDF = maths.standardized_CDF_logistic
        else:
            raise ValueError('Unknown likelihood model: {}'.format(likelihood_type))

    def latent_log_likelihood_DLMM(self, x, DLMM_params):

        # (B C K H W)
        x, (logit_pis, means, log_scales), K = unpack_likelihood_params(x, DLMM_params)

        # Assumes 1 - CDF(x) = CDF(-x) symmetry
        # Numerical stability, do subtraction in left tail

        x_centered = x - means
        x_centered = torch.abs(x_centered)
        inv_stds = torch.exp(-log_scales)
        cdf_upper = self.standardized_CDF(inv_stds * (0.5 - x_centered))
        cdf_lower = self.standardized_CDF(inv_stds * (- 0.5 - x_centered))
        pmf_mixture_component = lower_bound_toward(cdf_upper - cdf_lower, MIN_LIKELIHOOD)
        log_pmf_mixture_component = torch.log(pmf_mixture_component)

        # Non-negativity + normalization via softmax
        lse_in = F.log_softmax(logit_pis, dim=2) + log_pmf_mixture_component
        log_DLMM = torch.logsumexp(lse_in, dim=2)

        return log_DLMM

    def forward(self, latents, spatial_shape, **kwargs):

        hyperlatents = self.analysis_net(latents)
        
        # Mismatch b/w continuous and discrete cases?
        # Differential entropy, hyperlatents
        noisy_hyperlatents = self._quantize(hyperlatents, mode='noise')
        noisy_hyperlatent_likelihood = self.hyperlatent_likelihood(noisy_hyperlatents)
        noisy_hyperlatent_bits, noisy_hyperlatent_bpp = self._estimate_entropy(
            noisy_hyperlatent_likelihood, spatial_shape)

        # Discrete entropy, hyperlatents
        quantized_hyperlatents = self._quantize(hyperlatents, mode='quantize')
        quantized_hyperlatent_likelihood = self.hyperlatent_likelihood(quantized_hyperlatents)
        quantized_hyperlatent_bits, quantized_hyperlatent_bpp = self._estimate_entropy(
            quantized_hyperlatent_likelihood, spatial_shape)

        if self.training is True:
            hyperlatents_decoded = noisy_hyperlatents
        else:
            hyperlatents_decoded = quantized_hyperlatents

        latent_DLMM_params = self.synthesis_DLMM_params(hyperlatents_decoded)

        # Differential entropy, latents
        noisy_latents = self._quantize(latents, mode='noise')
        noisy_latent_log_likelihood = self.latent_log_likelihood_DLMM(noisy_latents, 
            DLMM_params=latent_DLMM_params)
        noisy_latent_bits, noisy_latent_bpp = self._estimate_entropy_log(
            noisy_latent_log_likelihood, spatial_shape)     

        # Discrete entropy, latents
        quantized_latents = self._quantize(latents, mode='quantize')
        quantized_latent_log_likelihood = self.latent_log_likelihood_DLMM(quantized_latents, 
            DLMM_params=latent_DLMM_params)
        quantized_latent_bits, quantized_latent_bpp = self._estimate_entropy_log(
            quantized_latent_log_likelihood, spatial_shape)     


        if self.training is True:
            latents_decoded = self.quantize_latents_st(latents)
        else:
            latents_decoded = quantized_latents

        info = HyperInfo(
            decoded=latents_decoded,
            latent_nbpp=noisy_latent_bpp,
            hyperlatent_nbpp=noisy_hyperlatent_bpp,
            total_nbpp=noisy_latent_bpp + noisy_hyperlatent_bpp,
            latent_qbpp=quantized_latent_bpp,
            hyperlatent_qbpp=quantized_hyperlatent_bpp,
            total_qbpp=quantized_latent_bpp + quantized_hyperlatent_bpp,
            bitstring=None,  # TODO
            side_bitstring=None, # TODO
        )

        return info

def get_num_DLMM_channels(C, K=4, params=['mu','scale','mix']):
    """
    C:  Channels of latent representation (L3C uses 5).
    K:  Number of mixture coefficients.
    """
    return C * K * len(params)

def get_num_mixtures(K_agg, C, params=['mu','scale','mix']):
    return K_agg // (len(params) * C)

def unpack_likelihood_params(x, conv_out):
    
    N, C, H, W = x.shape
    K_agg = conv_out.shape[1]

    K = get_num_mixtures(K_agg, C)

    # For each channel: K pi / K mu / K sigma 
    conv_out = conv_out.reshape(N, 3, C, K, H, W)
    logit_pis = conv_out[:, 0, ...]
    means = conv_out[:, 1, ...]
    log_scales = conv_out[:, 2, ...]
    log_scales = lower_bound_toward(log_scales, LOG_SCALES_MIN)
    x = x.reshape(N, C, 1, H, W)

    return x, (logit_pis, means, log_scales), K



class HyperpriorSynthesisDLMM(nn.Module):
    """
    Outputs distribution parameters of input latents, conditional on 
    hyperlatents, assuming a discrete logistic mixture model.

    C:  Number of output channels
    """
    def __init__(self, C=64, N=320, activation='relu', final_activation=None):
        super(HyperpriorSynthesisDLMM, self).__init__()

        cnn_kwargs = dict(kernel_size=5, stride=2, padding=2, output_padding=1)
        self.activation = getattr(F, activation)
        self.final_activation = final_activation

        self.conv1 = nn.ConvTranspose2d(N, N, **cnn_kwargs)
        self.conv2 = nn.ConvTranspose2d(N, N, **cnn_kwargs)
        self.conv3 = nn.ConvTranspose2d(N, C, kernel_size=3, stride=1, padding=1)
        self.conv_out = nn.Conv2d(C, get_num_DLMM_channels(C), kernel_size=1, stride=1)

        if self.final_activation is not None:
            self.final_activation = getattr(F, final_activation)

    def forward(self, x):
        x = self.activation(self.conv1(x))
        x = self.activation(self.conv2(x))
        x = self.conv3(x)
        x = self.conv_out(x)

        if self.final_activation is not None:
            x = self.final_activation(x)
        return x
        

if __name__ == '__main__':

    def pad_factor(input_image, spatial_dims, factor):
        """Pad `input_image` (N,C,H,W) such that H and W are divisible by `factor`."""
        H, W = spatial_dims[0], spatial_dims[1]
        pad_H = (factor - (H % factor)) % factor
        pad_W = (factor - (W % factor)) % factor
        return F.pad(input_image, pad=(0, pad_W, 0, pad_H), mode='reflect')

    C = 8
    hp = Hyperprior(C)
    hp_dlmm = HyperpriorDLMM(8)

    y = torch.randn((3,C,16,16))
    # y = torch.randn((10,C,126,95))

    n_downsamples = hp.analysis_net.n_downsampling_layers
    factor = 2 ** n_downsamples
    print('Padding to {}'.format(factor))
    y = pad_factor(y, y.size()[2:], factor)
    print('Size after padding', y.size())

    f = hp(y, spatial_shape=(1,1))
    print('Shape of decoded latents', f.decoded.shape)

    f_dlmm = hp_dlmm(y, spatial_shape=(1,1))
    print('Shape of decoded latents', f_dlmm.decoded.shape)
