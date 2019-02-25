import numpy as np
from scipy.stats import norm
import craystack.core as cs
import craystack.vectorans as vrans


def BBANS(prior, likelihood, posterior):
    """
    This codec is for data modelled with a latent variable model as described
    in the paper 'Practical Lossless Compression with Latent Variable Models'
    currently under review for ICLR '19.

       latent        observed
      variable         data

        ( z ) ------> ( x )

    This assumes data x is modelled via a model which includes a latent
    variable. The model has a prior p(z), likelihood p(x | z) and (possibly
    approximate) posterior q(z | x). See the paper for more details.
    """
    prior_append, prior_pop = prior

    def append(message, data):
        _, posterior_pop = posterior(data)
        message, latent = posterior_pop(message)
        likelihood_append, _ = likelihood(latent)
        message = likelihood_append(message, data)
        message = prior_append(message, latent)
        return message

    def pop(message):
        message, latent = prior_pop(message)
        _, likelihood_pop = likelihood(latent)
        message, data = likelihood_pop(message)
        posterior_append, _ = posterior(data)
        message = posterior_append(message, latent)
        return message, data
    return append, pop

def VAE(gen_net, rec_net, obs_codec, prior_prec, latent_prec):
    """
    This codec uses the BB-ANS algorithm to code data which is distributed
    according to a variational auto-encoder (VAE) model. It is assumed that the
    VAE uses an isotropic Gaussian prior and diagonal Gaussian for its
    posterior.
    """
    z_view = lambda head: head[0]
    x_view = lambda head: head[1]

    prior = cs.substack(cs.Uniform(prior_prec), z_view)

    def likelihood(latent_idxs):
        z = std_gaussian_centres(prior_prec)[latent_idxs]
        return cs.substack(obs_codec(gen_net(z)), x_view)

    def posterior(data):
        post_mean, post_stdd = rec_net(data)
        return cs.substack(DiagGaussianLatentStdBins(
            post_mean, post_stdd, prior_prec, latent_prec), z_view)
    return BBANS(prior, likelihood, posterior)

def TwoLayerVAE(rec_net1, rec_net2,
                post1_codec, obs_codec,
                prior_prec, latent_prec,
                get_theta):
    """
    rec_net1 outputs params for q(z1|x)
    rec_net2 outputs params for q(z2|x)
    post1_codec is to code z1 by q(z1|z2,x)
    obs_codec is to code x by p(x|z1)"""
    z1_view = lambda head: head[0]
    z2_view = lambda head: head[1]
    x_view = lambda head: head[2]

    prior_z1_append, prior_z1_pop = cs.substack(cs.Uniform(prior_prec), z1_view)
    prior_z2_append, prior_z2_pop = cs.substack(cs.Uniform(prior_prec), z2_view)

    def prior_append(message, latent):
        (z1, z2), theta1 = latent
        message = prior_z1_append(message, z1)
        message = prior_z2_append(message, z2)
        return message

    def prior_pop(message):
        message, z2 = prior_z2_pop(message)
        message, z1 = prior_z1_pop(message)
        # compute theta1
        eps1_vals = std_gaussian_centres(prior_prec)[z1]
        z2_vals = std_gaussian_centres(prior_prec)[z2]
        theta1 = get_theta(eps1_vals, z2_vals)
        return message, ((z1, z2), theta1)

    def likelihood(latent):
        (z1, z2), theta1 = latent
        # get z1_vals from the latent
        _, _, mu1_prior, sig1_prior = np.moveaxis(theta1, -1, 0)
        eps1_vals = std_gaussian_centres(prior_prec)[z1]
        z1_vals = mu1_prior + sig1_prior * eps1_vals
        append, pop = cs.substack(obs_codec(z1_vals), x_view)
        def pop_(msg):
            msg, (data, _) = pop(msg)
            return msg, data
        return append, pop_

    def posterior(data):
        mu1, sig1, h = rec_net1(data)
        mu2, sig2 = rec_net2(h)

        post_z2_append, post_z2_pop = cs.substack(DiagGaussianLatentStdBins(
            mu2, sig2, prior_prec, latent_prec), z2_view)

        def posterior_append(message, latents):
            (z1, z2), theta1 = latents
            _, _, mu1_prior, sig1_prior = np.moveaxis(theta1, -1, 0)
            post_z1_append, _ = cs.substack(DiagGaussianLatent(mu1, sig1,
                                                               mu1_prior, sig1_prior,
                                                               latent_prec, prior_prec),
                                            z1_view)
            message = post_z1_append(message, z1)
            message = post_z2_append(message, z2)
            return message

        def posterior_pop(message):
            message, z2 = post_z2_pop(message)
            z2_vals = std_gaussian_centres(prior_prec)[z2]
            # need to return theta1 from the z1 pop
            _, post_z1_pop = cs.substack(post1_codec(z2_vals, mu1, sig1), z1_view)
            message, (z1, theta1) = post_z1_pop(message)
            return message, ((z1, z2), theta1)

        return posterior_append, posterior_pop

    return BBANS((prior_append, prior_pop), likelihood, posterior)


def ResNetVAE(up_pass, rec_nets, gen_nets, obs_codec,
              prior_prec, latent_prec, num_latents):
    """
    Codec for a ResNetVAE.
    Assume that the posterior is bidirectional -
    i.e. has a deterministic upper pass but top down sampling.
    Further assume that all latent conditionals are factorised Gaussians,
    both in the generative network p(z_n|z_{n-1})
    and in the inference network q(z_n|x, z_{n-1})
    """
    x_view = lambda head: head[-1]

    def latent_view(n):
        # assume that the latent views are ordered top-down
        return lambda head: head[n]

    prior_codecs = [cs.substack(cs.Uniform(prior_prec), latent_view(i)) for i in range(num_latents)]

    def prior_append(message, latents):
        # append bottom-up
        for i in reversed(range(num_latents)):
            append, _ = prior_codecs[i]
            latent, _ = latents[i]
            message = append(message, latent)
        return message

    def prior_pop(message):
        # pop top-down
        latents = []
        previous_latent_val = None
        for codec, gen_net in zip(prior_codecs, gen_nets):
            _, pop = codec
            message, latent = pop(message)
            prior_mean, prior_stdd = gen_net(previous_latent_val) if previous_latent_val else gen_net()
            latents.append((latent, (prior_mean, prior_stdd)))
            previous_latent_val = prior_mean + std_gaussian_centres(prior_prec)[latent] * prior_stdd
        return message, latents

    def posterior(data):
        # run deterministic upper-pass
        context = up_pass(data)  # TODO: use this in relevant place

        def posterior_append(message, latents):
            # append bottom-up
            for i in reversed(range(num_latents)):
                latent, (prior_mean, prior_stdd) = latents[i]
                rec_net = rec_nets[i]
                previous_latent_val = None
                if i > 0:
                    previous_latent, _ = latents[i-1]
                    previous_latent_val = prior_mean + \
                                          std_gaussian_centres(prior_prec)[previous_latent] * prior_stdd
                post_mean, post_stdd = rec_net(previous_latent_val) if previous_latent_val else rec_net()
                append, _ = cs.substack(DiagGaussianLatent(post_mean, post_stdd,
                                                           prior_mean, prior_stdd,
                                                           latent_prec, prior_prec),
                                        latent_view(i))
                message = append(message, latent)
            return message

        def posterior_pop(message):
            # pop top-down
            latents = []
            previous_latent_val = None
            for i in range(num_latents):
                rec_net = rec_nets[i]
                gen_net = gen_nets[i]
                post_mean, post_stdd = rec_net(previous_latent_val) if previous_latent_val else rec_net()
                prior_mean, prior_stdd = gen_net(previous_latent_val) if previous_latent_val else gen_net()
                _, pop = cs.substack(DiagGaussianLatent(post_mean, post_stdd,
                                                        prior_mean, prior_stdd,
                                                        latent_prec, prior_prec),
                                     latent_view(i))
                message, latent = pop(message)
                previous_latent_val = prior_mean + std_gaussian_centres(prior_prec)[latent] * prior_stdd
                latents.append((latent, (prior_mean, prior_stdd)))
            return message, latents

        return posterior_append, posterior_pop

    def likelihood(latents):
        # get the z1 vals to condition on
        z1_idxs, (prior_mean, prior_stdd) = latents[-1]
        z1_vals = prior_mean + std_gaussian_centres(prior_prec) * prior_stdd
        return cs.substack(obs_codec(z1_vals), x_view)

    return BBANS((prior_append, prior_pop), likelihood, posterior)


std_gaussian_bucket_cache = {}  # Stores bucket endpoints
std_gaussian_centres_cache = {}  # Stores bucket centres

def std_gaussian_buckets(precision):
    """
    Return the endpoints of buckets partioning the domain of the prior. Each
    bucket has mass 1 / (1 << precision) under the prior.
    """
    if precision in std_gaussian_bucket_cache:
        return std_gaussian_bucket_cache[precision]
    else:
        buckets = norm.ppf(np.linspace(0, 1, (1 << precision) + 1))
        std_gaussian_bucket_cache[precision] = buckets
        return buckets

def std_gaussian_centres(precision):
    """
    Return the centres of mass of buckets partioning the domain of the prior.
    Each bucket has mass 1 / (1 << precision) under the prior.
    """
    if precision in std_gaussian_centres_cache:
        return std_gaussian_centres_cache[precision]
    else:
        centres = np.float32(
            norm.ppf((np.arange(1 << precision) + 0.5) / (1 << precision)))
        std_gaussian_centres_cache[precision] = centres
        return centres

def _gaussian_latent_cdf(mean, stdd, prior_prec, post_prec):
    def cdf(idx):
        x = std_gaussian_buckets(prior_prec)[idx]
        return cs._nearest_int(norm.cdf(x, mean, stdd) * (1 << post_prec))
    return cdf

def _gaussian_latent_ppf(mean, stdd, prior_prec, post_prec):
    def ppf(cf):
        x = norm.ppf((cf + 0.5) / (1 << post_prec), mean, stdd)
        # Binary search is faster than using the actual gaussian cdf for the
        # precisions we typically use, however the cdf is O(1) whereas search
        # is O(precision), so for high precision cdf will be faster.
        return np.uint64(np.digitize(x, std_gaussian_buckets(prior_prec)) - 1)
    return ppf

def DiagGaussianLatentStdBins(mean, stdd, prior_prec, post_prec):
    enc_statfun = cs._cdf_to_enc_statfun(
        _gaussian_latent_cdf(mean, stdd, prior_prec, post_prec))
    dec_statfun = _gaussian_latent_ppf(mean, stdd, prior_prec, post_prec)
    return cs.NonUniform(enc_statfun, dec_statfun, post_prec)

def DiagGaussianLatent(mean, stdd, bin_mean, bin_stdd, coding_prec, bin_prec):
    """To code Gaussian data according to the bins of a different Gaussian"""

    def cdf(idx):
        x = norm.ppf(idx / (1 << bin_prec), bin_mean, bin_stdd)  # this gives lb of bin
        return cs._nearest_int(norm.cdf(x, mean, stdd) * (1 << coding_prec))

    def ppf(cf):
        x_max = norm.ppf((cf + 0.5) / (1 << coding_prec), mean, stdd)
        # if our gaussians have little overlap, then the cdf could be exactly 1
        # therefore cut off at (1<<bin_prec)-1 to make sure we return a valid bin
        return np.uint64(np.minimum((1 << bin_prec) - 1,
                                    norm.cdf(x_max, bin_mean, bin_stdd) * (1 << bin_prec)))

    enc_statfun = cs._cdf_to_enc_statfun(cdf)
    return cs.NonUniform(enc_statfun, ppf, coding_prec)
