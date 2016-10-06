from __future__ import division
import autograd.numpy as np
import autograd.numpy.random as npr
from autograd import grad
from autograd.scipy.misc import logsumexp
from autograd.scipy.linalg import block_diag
from autograd import hessian, jacobian, grad_and_aux, value_and_grad
from autograd.util import getval, flatten
from data import load_mnist
from operator import add


### Vanilla neural net functions

def neural_net_predict(params, inputs):
    return softmax(mlp(params, inputs))

def mlp(params, inputs):
    for W, b in params:
        outputs = np.dot(inputs, W) + b
        inputs = np.tanh(outputs)
    return outputs

def softmax(inputs):
    return inputs - logsumexp(inputs, axis=1, keepdims=True)

def init_random_params(scale, layer_sizes, rng=npr):
    return [(scale * rng.randn(m, n), scale * rng.randn(n))
            for m, n in zip(layer_sizes[:-1], layer_sizes[1:])]

def log_likelihood(params, inputs, targets):
    logprobs = neural_net_predict(params, inputs)
    return np.sum(logprobs * targets)

def l2_norm(params):
    flattened, _ = flatten(params)
    return np.dot(flattened, flattened)

def log_joint(params, inputs, targets, L2_reg):
    return -L2_reg * l2_norm(params) + log_likelihood(params, inputs, targets)

def accuracy(params, inputs, targets):
    target_class    = np.argmax(targets, axis=1)
    predicted_class = np.argmax(neural_net_predict(params, inputs), axis=1)
    return np.mean(predicted_class == target_class)

### General utility functions

homog = lambda X: np.hstack((X, np.ones(X.shape[0])[:,None]))

def sample_discrete_from_log(logprobs):
    probs = np.exp(logprobs)
    cumvals = np.cumsum(probs, axis=1)
    indices = np.sum(npr.rand(logprobs.shape[0], 1) > cumvals, axis=1)
    return np.eye(logprobs.shape[1])[indices]

### K-FAC utility functions

# First, we need to augment the neural net computation to collect the required
# statistics, namely samples of the activations and samples of the gradients of
# those activations under random targets generated by the model. To collect the
# gradients, we use an autograd trick: we add extra bias terms (set to zero)
# and compute gradients with respect to them.

def neural_net_predict_and_activations(extra_biases, params, inputs):
    '''Like the neural_net_predict function in neural_net.py, but
       (1) adds extra biases and (2) also returns all computed activations.'''
    all_activations = [inputs]
    for (W, b), extra_bias in zip(params, extra_biases):
        s = np.dot(all_activations[-1], W) + b + extra_bias
        all_activations.append(np.tanh(s))
    logprobs = s - logsumexp(s, axis=1, keepdims=True)
    return logprobs, all_activations[:-1]

def model_predictive_log_likelihood(extra_biases, params, inputs):
    '''Computes Monte Carlo estimate of log_likelihood on targets sampled from
       the model. Also returns all computed activations.'''
    logprobs, activations = \
        neural_net_predict_and_activations(extra_biases, params, inputs)
    model_sampled_targets = sample_discrete_from_log(getval(logprobs))
    return np.sum(logprobs * model_sampled_targets), activations

def collect_stats(params, inputs, num_samples):
    '''Collects the statistics necessary to estimate the approximate Fisher
       information matrix used in K-FAC.'''
    inputs = inputs[npr.choice(inputs.shape[0], size=num_samples)]
    extra_biases = [np.zeros((inputs.shape[0], b.shape[0])) for W, b in params]
    gradfun = grad_and_aux(model_predictive_log_likelihood)
    g_samples, a_samples = gradfun(extra_biases, params, inputs)
    outer = lambda X: np.dot(X.T, X)
    return [(outer(homog(A)), outer(G), len(A))
            for A, G in zip(a_samples, g_samples)]

### Bookkeeping

def update_stats(stats, new_stats):
    return [map(add, s1, s2) for s1, s2 in zip(stats, new_stats)]

def init_stats(layer_sizes):
    return [(np.zeros((m+1, m+1)), np.zeros((n, n)), 0)
            for m, n in zip(layer_sizes[:-1], layer_sizes[1:])]

def update_factor_estimates(factors, stats, eps):
    update = lambda X, Xhat: eps * X + (1.-eps) * Xhat
    return [(update(A, aaT / n), update(G, ggT / n))
            for (A, G), (aaT, ggT, n) in zip(factors, stats)]

def init_factor_estimates(layer_sizes):
    return [(np.eye(m+1), np.eye(n))
            for m, n in zip(layer_sizes[:-1], layer_sizes[1:])]

### Computing and applying the preconditioner

def compute_precond(factor_estimates, lmbda):
    inv = lambda X: np.linalg.inv(X + lmbda*np.eye(X.shape[0]))
    return [(inv(A), inv(G)) for A, G in factor_estimates]

def apply_preconditioner(precond, gradient):
    stack = lambda W, b: np.vstack((W, b))
    split = lambda Wb: (Wb[:-1], Wb[-1])
    kronp = lambda Ainv, Ginv, Wb: np.dot(Ainv, np.dot(Wb, Ginv.T))
    return [split(kronp(Ainv, Ginv, stack(dW, db)))
            for (Ainv, Ginv), (dW, db) in zip(precond, gradient)]

### K-FAC-pre (simplified preconditioned SGD version)

# K-FAC is specific to fully-connected layers, so its interface needs to know
# more than the other optimizers in optimizers.py. In particular, it only works
# when the parameters are a list of weights and biases, it needs to know the
# layer sizes, it needs ot know the likelihood model on the last layer (logistic
# regression here), and it needs to have direct access to the training data.

def kfac(objective, get_batch, layer_sizes, init_params, step_size, num_iters,
         num_samples, sample_period, reestimate_period, update_precond_period,
         lmbda, eps, mu=0.9, callback=None):

    ## initialize

    stats   = init_stats(layer_sizes)
    factors = init_factor_estimates(layer_sizes)
    precond = compute_precond(factors, lmbda=lmbda)

    objective_grad = value_and_grad(objective)

    ## main loop

    params = init_params
    flat_params, unflatten = flatten(init_params)
    momentum = np.zeros_like(flat_params)
    for i in range(num_iters):
        val, gradient = objective_grad(params, i)
        if callback: callback(params, i, gradient)

        if (i+1) % sample_period == 0:
            new_stats = collect_stats(params, get_batch(i), num_samples)
            stats = update_stats(stats, new_stats)

        if (i+1) % reestimate_period == 0:
            factors = update_factor_estimates(factors, stats, eps)
            stats = init_stats(layer_sizes)

        if (i+1) % update_precond_period == 0:
            precond = compute_precond(factors, lmbda=lmbda)

        natgrad = flatten(apply_preconditioner(precond, gradient))[0]
        momentum = mu * momentum - (1. - mu) * natgrad
        flat_params = flat_params + step_size * momentum
        params = unflatten(flat_params)

    return params

### testing

def exact_fisher(params, inputs, start_layer, stop_layer):
    '''Computes the exact Fisher information from start_layer to stop_layer.'''
    flat_params, unflatten = flatten(params[start_layer:stop_layer])
    merge_params = lambda flat_params: \
        params[:start_layer] + unflatten(flat_params) + params[stop_layer:]
    flat_mlp = lambda flat_params, inputs: mlp(merge_params(flat_params), inputs)
    mlp_outputs = flat_mlp(flat_params, inputs)

    F = np.zeros(2*(flat_params.shape[0],))
    for x, z in zip(inputs, mlp_outputs):
        J_f = jacobian(flat_mlp)(flat_params, x)
        F_R = hessian(logsumexp)(z)
        F += np.dot(J_f.T, np.dot(F_R, J_f))

    return F / inputs.shape[0]

def montecarlo_fisher(num_samples, params, inputs, start_layer, stop_layer):
    '''Estimates the Fisher information from start_layer to stop_layer
       using Monte Carlo to estimate the covariance of the gradients.'''
    flat_params, unflatten = flatten(params[start_layer:stop_layer])
    merge_params = lambda flat_params: \
        params[:start_layer] + unflatten(flat_params) + params[stop_layer:]
    flat_loglike = lambda flat_params, inputs, targets: \
        log_likelihood(merge_params(flat_params), inputs, targets)
    random_targets = lambda: \
        sample_discrete_from_log(neural_net_predict(params, inputs))

    F = np.zeros(2*(flat_params.shape[0],))
    for i in range(num_samples):
        g = grad(flat_loglike)(flat_params, inputs, random_targets())
        F += np.outer(g, g) / inputs.shape[0]

    return F / num_samples

def kfac_approx_fisher(sample_factor, params, inputs, start_layer, stop_layer):
    '''Estimate the K-FAC approximate Fisher using Monte Carlo samples.'''
    layer_sizes = [W.shape[0] for W, _ in params] + [params[-1][0].shape[1]]
    stats = collect_stats(params, inputs, sample_factor*inputs.shape[0])
    factors = update_factor_estimates(init_factor_estimates(layer_sizes), stats, 0.)
    return block_diag(*[np.kron(A, G) for A, G in factors[start_layer:stop_layer]])


### printing

def make_table(column_labels):
    lens = list(map(len, column_labels))
    print(' | '.join('{{:>{}}}'.format(l) for l in lens).format(*column_labels))
    row_format = ' | '.join(['{{:{}d}}'.format(lens[0])]
                            + ['{{:{}.4f}}'.format(l) for l in lens[1:]])
    def print_row(*vals):
        print(row_format.format(*vals))
    return print_row


### script

if __name__ == '__main__':
    npr.seed(0)

    # Model parameters
    layer_sizes = [784, 200, 100, 10]
    l2_reg = 0.

    # Training parameters
    param_scale = 0.1
    batch_size = 256
    num_epochs = 50

    # Load data
    print("Loading training data...")
    N, train_images, train_labels, test_images,  test_labels = load_mnist()

    # Divide data into batches
    num_batches = int(np.ceil(len(train_images) / batch_size))

    def batch_indices(itr):
        idx = itr % num_batches
        return slice(idx * batch_size, (idx+1) * batch_size)

    get_batch = lambda itr: train_images[batch_indices(itr)]

    # Define training objective as a function of iteration index
    def objective(params, itr):
        idx = batch_indices(itr)
        return -log_joint(params, train_images[idx], train_labels[idx], l2_reg)

    print_row = make_table(['Epoch', 'Train objective' ,'Train accuracy', 'Test accuracy'])
    def print_perf(params, i, gradient):
        if i % num_batches == 0:
            train_obj = -log_joint(params, train_images, train_labels, l2_reg)
            train_acc = accuracy(params, train_images, train_labels)
            test_acc  = accuracy(params, test_images, test_labels)
            print_row(i // num_batches, train_obj, train_acc, test_acc)

    # Initialize parameters
    init_params = init_random_params(param_scale, layer_sizes)

    # Optimize!
    optimized_params = kfac(
        objective, get_batch, layer_sizes, init_params, step_size=1e-3,
        num_iters=num_epochs*num_batches, lmbda=0.1, eps=0.05, num_samples=batch_size,
        sample_period=1, reestimate_period=5, update_precond_period=5,
        callback=print_perf)

# TODO rename lmbda to gamma, make it follow the paper (i.e. compute \pi_\ell)
