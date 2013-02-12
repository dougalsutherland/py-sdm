#!/usr/bin/env python
'''
Code to do classification with support vector machines, as in
    Dougal J. Sutherland, Liang Xiong, Barnabas Poczos, Jeff Schneider.
    Kernels on Sample Sets via Nonparametric Divergence Estimates.
    http://arxiv.org/abs/1202.0302
'''

from __future__ import division, print_function

from functools import partial
import itertools
from operator import itemgetter
import os
import sys
import weakref

import h5py
import numpy as np
import scipy.io
import scipy.linalg
import sklearn.base
from sklearn.cross_validation import KFold
from sklearn.preprocessing import LabelEncoder
from sklearn import svm  # NOTE: needs version 0.13+ for svm iter limits

from get_divs import (get_divs, FIX_MODE_DEFAULT, FIX_TERM_MODES, TAIL_DEFAULT,
                      read_cell_array, subset_data, check_h5_settings,
                      normalize_div_name)
from utils import (positive_int, positive_float, portion, is_integer_type,
                   itervalues, iteritems)
from mp_utils import ForkedData, get_pool, progressbar_and_updater

# TODO: handle input files from {extract,proc}_features
# TODO: better logging
# TODO: better divergence cache support
# TODO: support getting decision values / probabilities

DEFAULT_SVM_CACHE = 1000
DEFAULT_SVM_TOL = 1e-3
DEFAULT_SVM_ITER = 10 ** 6
DEFAULT_SVM_ITER_TUNING = 1000
DEFAULT_SVM_SHRINKING = True

DEFAULT_C_VALS = tuple(2.0 ** np.arange(-9, 19, 3))
DEFAULT_SIGMA_VALS = tuple(2.0 ** np.arange(-4, 11, 2))
DEFAULT_K = 3
DEFAULT_TUNING_FOLDS = 3

def get_status_fn(val):
    if val is True:
        return partial(print, file=sys.stderr)
    elif val is None:
        return lambda *args, **kwargs: None
    else:
        return val

################################################################################
### PSD projection and friends

def project_psd(mat, min_eig=0, destroy=False):
    '''
    Project a real symmetric matrix to PSD by discarding any negative
    eigenvalues from its spectrum. Passing min_eig > 0 lets you similarly make
    it positive-definite, though this may not technically be a projection...?

    Symmetrizes the matrix before projecting.

    If destroy is True, turns the passed-in matrix into gibberish. If the
    matrix is very large, passing in a weakref.proxy to it will use the least
    amount of memory.
    '''
    if not destroy:
        mat = mat.copy()
    mat += mat.T
    mat /= 2

    # TODO: be smart and only get negative eigs?
    vals, vecs = scipy.linalg.eigh(mat)
    if vals.min() < min_eig:
        del mat
        mat = np.dot(vecs, np.dot(np.diag(np.maximum(vals, min_eig)), vecs.T))
        del vals, vecs
        mat += mat.T
        mat /= 2
    return mat

def make_km(divs, sigma):
    # pass through a Gaussian
    km = divs / sigma  # makes a copy
    km **= 2
    km /= -2
    np.exp(km, km)  # inplace

    # PSD projection
    return project_psd(weakref.proxy(km), destroy=True)

def split_km(km, train_idx, test_idx):
    train_km = np.ascontiguousarray(km[np.ix_(train_idx, train_idx)])
    test_km = np.ascontiguousarray(km[np.ix_(test_idx, train_idx)])
    return train_km, test_km


################################################################################
### Cached divs helper

def get_divs_cache(bags, div_func, K, cache_filename=None,
                   n_proc=None, fix_mode=FIX_MODE_DEFAULT, tail=TAIL_DEFAULT,
                   min_dist=None, status_fn=True, progressbar=None):

    status = get_status_fn(status_fn)

    if cache_filename and os.path.exists(cache_filename):
        path = '{}/{}'.format(div_func, K)
        with h5py.File(cache_filename, 'r') as f:
            check_h5_settings(f, n=len(bags), dim=bags[0].shape[1],
                fix_mode=fix_mode, tail=tail, min_dist=min_dist)
            if path in f:
                divs = f[path]
                # assert divs.shape == (len(bags), len(bags)) # in check
                status("Loading divs from cache '{}'".format(cache_filename))
                return divs[...]

    divs = np.squeeze(get_divs(
            bags, specs=[div_func], Ks=[K],
            n_proc=n_proc, fix_mode=fix_mode, tail=tail, min_dist=min_dist,
            status_fn=status_fn, progressbar=progressbar))

    if cache_filename:
        status("Saving divs to cache '{}'".format(cache_filename))
        with h5py.File(cache_filename) as f:
            f.require_group(div_func).create_dataset(str(K), data=divs)

    return divs


################################################################################
### Parameter tuning

def try_params(km, labels, train_idx, test_idx, C, params):
    train_km, test_km = split_km(km.value, train_idx, test_idx)

    clf = svm.SVC(C=C, **params)
    clf.fit(train_km, labels.value[train_idx])

    preds = clf.predict(test_km)
    return np.mean(preds == labels.value[test_idx])

def _assign_score(scores, C_vals, sigma_vals, print_fn,
                 C_idx, sigma_idx, f_idx, val):
    scores[C_idx, sigma_idx, f_idx] = val
    #print_fn('C {}, sigma {}, fold {}: acc {}'.format(
    #    C_vals[C_idx], sigma_vals[sigma_idx], f_idx, val))

def tune_params(divs, labels,
                num_folds=DEFAULT_TUNING_FOLDS,
                n_proc=None,
                C_vals=DEFAULT_C_VALS,
                sigma_vals=DEFAULT_SIGMA_VALS, scale_sigma=True,
                weight_classes=False,
                cache_size=DEFAULT_SVM_CACHE,
                svm_tol=DEFAULT_SVM_TOL,
                svm_max_iter=DEFAULT_SVM_ITER_TUNING,
                svm_shrinking=DEFAULT_SVM_SHRINKING,
                status_fn=True,
                progressbar=None):
    if progressbar is None:
        progressbar = status_fn is True
    status_fn = get_status_fn(status_fn)

    C_vals = np.asarray(C_vals)
    sigma_vals = np.asarray(sigma_vals)
    if scale_sigma:
        sigma_vals *= np.median(divs[divs > 0])

    if C_vals.size <= 1 and sigma_vals.size <= 1:
        # no tuning necessary
        return C_vals[0], sigma_vals[0]

    num_bags = divs.shape[0]
    assert divs.ndim == 2 and divs.shape[1] == num_bags
    assert labels.shape == (num_bags,)

    svm_params = dict(
        cache_size=cache_size,
        class_weight='auto' if weight_classes else None,
        kernel='precomputed',
        tol=svm_tol,
        max_iter=svm_max_iter,
        shrinking=svm_shrinking,
        #verbose=True,
    )

    # get kernel matrices for the sigma vals we're trying
    # TODO: could be more careful about making copies here
    sigma_kms = {}
    status_fn('Projecting...')
    for sigma in sigma_vals:
        #status_fn('Projecting: sigma = {}'.format(sigma))
        sigma_kms[sigma] = ForkedData(make_km(divs, sigma))

    labels_d = ForkedData(labels)

    # try each sigma/C combination and see how they do
    scores = np.empty((C_vals.size, sigma_vals.size, num_folds))
    scores.fill(np.nan)
    assign_score = partial(_assign_score, scores, C_vals, sigma_vals, status_fn)
    if progressbar:
        assign_score_ = assign_score
        pbar, tick_pbar = progressbar_and_updater(
                maxval=len(C_vals) * len(sigma_vals) * num_folds)
        def assign_score(*args, **kwargs):
            assign_score_(*args, **kwargs)
            tick_pbar()

    status_fn('Cross-validating parameter sets...')
    jobs = itertools.product(enumerate(C_vals), enumerate(sigma_vals))
    folds = list(enumerate(KFold(n=num_bags, n_folds=num_folds, shuffle=True)))
    with get_pool(n_proc) as pool:
        for (C_idx, C), (sigma_idx, sigma) in jobs:
            for f_idx, (train, test) in folds:
                set_res = partial(assign_score, C_idx, sigma_idx, f_idx)
                pool.apply_async(try_params,
                    [sigma_kms[sigma], labels_d, train, test, C, svm_params],
                    callback=set_res)

    if progressbar:
        pbar.finish()

    # figure out which ones were best
    # TODO: randomize when there are ties...
    cv_means = scores.mean(axis=-1)
    best_C, best_sigma = np.unravel_index(cv_means.argmax(), cv_means.shape)

    return sigma_vals[best_sigma], C_vals[best_C]


################################################################################
### Main dealio

class SupportDistributionMachine(sklearn.base.BaseEstimator):
    def __init__(self,
                 div_func='renyi:.9',
                 K=DEFAULT_K,
                 tuning_folds=DEFAULT_TUNING_FOLDS,
                 n_proc=None,
                 C_vals=DEFAULT_C_VALS,
                 sigma_vals=DEFAULT_SIGMA_VALS, scale_sigma=True,
                 weight_classes=False,
                 cache_size=DEFAULT_SVM_CACHE,
                 tuning_cache_size=DEFAULT_SVM_CACHE,
                 svm_tol=DEFAULT_SVM_TOL,
                 tuning_svm_tol=DEFAULT_SVM_TOL,
                 svm_max_iter=DEFAULT_SVM_ITER,
                 tuning_svm_max_iter=DEFAULT_SVM_ITER_TUNING,
                 svm_shrinking=DEFAULT_SVM_SHRINKING,
                 status_fn=None, progressbar=None,
                 fix_mode=FIX_MODE_DEFAULT, tail=TAIL_DEFAULT, min_dist=None):
        self.div_func = div_func
        self.K = K
        self.tuning_folds = tuning_folds
        self.n_proc = n_proc
        self.C_vals = C_vals
        self.sigma_vals = sigma_vals
        self.scale_sigma = scale_sigma
        self.weight_classes = weight_classes
        self.cache_size = cache_size
        self.tuning_cache_size = tuning_cache_size
        self.svm_tol = svm_tol
        self.tuning_svm_tol = tuning_svm_tol
        self.svm_max_iter = svm_max_iter
        self.tuning_svm_max_iter = tuning_svm_max_iter
        self.svm_shrinking = svm_shrinking
        self._status_fn = status_fn
        self._progressbar = progressbar
        self.fix_mode = FIX_MODE_DEFAULT
        self.tail = tail
        self.min_dist = min_dist

    @property
    def status_fn(self):
        return get_status_fn(self._status_fn)

    @property
    def progressbar(self):
        if self._progressbar is None:
            return self._status_fn is True
        else:
            return self._progressbar

    def fit(self, X, y, divs=None, divs_cache=None):
        '''
        X: a list of row-instance data matrices, with common dimensionality

        y: a vector of nonnegative integer class labels.
            -1 corresponds to data that should be used semi-supervised, ie
            used in projecting the Gram matrix, but not in training the SVM.

        divs: precomputed divergences among the passed points
        '''
        n_bags = len(X)

        y = np.squeeze(y)
        assert is_integer_type(y)
        assert y.shape == (n_bags,)
        assert np.all(y >= -1)

        train_idx = y != -1
        train_y = y[train_idx]
        assert train_y.size >= 2
        self.train_bags_ = [X[i] for i, b in enumerate(train_idx) if b]

        # get divergences
        if divs is None:
            self.status_fn('Getting divergences...')
            divs = get_divs_cache(X, div_func=self.div_func, K=self.K,
                    cache_filename=divs_cache,
                    n_proc=self.n_proc, min_dist=self.min_dist,
                    fix_mode=self.fix_mode, tail=self.tail,
                    status_fn=self.status_fn, progressbar=self.progressbar)
        else:
            #self.status_fn('Using passed-in divergences...')
            assert divs.shape == (n_bags, n_bags)

        # tune params
        self.status_fn('Tuning SVM parameters...')
        self.sigma_, self.C_ = tune_params(
                divs=np.ascontiguousarray(divs[np.ix_(train_idx, train_idx)]),
                labels=train_y,
                num_folds=self.tuning_folds,
                n_proc=self.n_proc,
                C_vals=self.C_vals,
                sigma_vals=self.sigma_vals, scale_sigma=self.scale_sigma,
                weight_classes=self.weight_classes,
                cache_size=self.tuning_cache_size,
                svm_tol=self.tuning_svm_tol,
                svm_max_iter=self.tuning_svm_max_iter,
                svm_shrinking=self.svm_shrinking,
                status_fn=self.status_fn,
                progressbar=self.progressbar)
        self.status_fn('Selected sigma {}, C {}'.format(self.sigma_, self.C_))

        # project the final Gram matrix
        self.status_fn('Doing final projection')
        train_km = np.ascontiguousarray(
                make_km(divs, self.sigma_)[np.ix_(train_idx, train_idx)])

        # train the selected SVM
        self.status_fn('Training final SVM')
        clf = svm.SVC(
                C=self.C_,
                cache_size=self.cache_size,
                class_weight='auto' if self.weight_classes else None,
                tol=self.svm_tol,
                kernel='precomputed',
                max_iter=self.svm_max_iter,
                shrinking=self.svm_shrinking,
        )
        clf.fit(train_km, train_y)
        self.svm_ = clf

    def predict(self, data, divs=None):
        n_train = len(self.train_bags_)
        n_test = len(data)

        if divs is None:
            self.status_fn('Getting test bag divergences...')

            mask = np.zeros((n_train + n_test, n_train + n_test), dtype=bool)
            mask[:n_train, -n_test:] = True
            mask[-n_test:, :n_train] = True

            divs = np.squeeze(get_divs(
                    self.train_bags_ + data, mask=mask,
                    specs=[self.div_func], Ks=[self.K],
                    n_proc=self.n_proc, min_dist=self.min_dist,
                    fix_mode=self.fix_mode, tail=self.tail,
                    status_fn=self.status_fn, progressbar=self.progressbar))
            divs = (divs[-n_test:, :n_train] + divs[:n_train, -n_test].T) / 2
        else:
            assert divs.shape == (n_test, n_train)
            divs = divs.copy()

        # pass divs through a gaussian kernel
        divs /= self.sigma_
        divs **= 2
        divs /= -2
        np.exp(divs, divs)

        # TODO: smarter projection options for inductive use

        preds = self.svm_.predict(divs)
        assert np.all(preds == np.round(preds))
        return preds.astype(int)


def transduct(train_bags, train_labels, test_bags,
              div_func='renyi:.9',
              K=DEFAULT_K,
              tuning_folds=DEFAULT_TUNING_FOLDS,
              n_proc=None,
              C_vals=DEFAULT_C_VALS,
              sigma_vals=DEFAULT_SIGMA_VALS, scale_sigma=True,
              weight_classes=False,
              cache_size=DEFAULT_SVM_CACHE, tuning_cache_size=DEFAULT_SVM_CACHE,
              svm_tol=DEFAULT_SVM_TOL, tuning_svm_tol=DEFAULT_SVM_TOL,
              svm_max_iter=DEFAULT_SVM_ITER,
              tuning_svm_max_iter=DEFAULT_SVM_ITER_TUNING,
              svm_shrinking=DEFAULT_SVM_SHRINKING,
              status_fn=True,
              progressbar=None,
              fix_mode=FIX_MODE_DEFAULT, tail=TAIL_DEFAULT, min_dist=None,
              divs=None,
              divs_cache=None,
              return_config=False):
    # TODO: support non-Gaussian kernels
    # TODO: support CVing between multiple div funcs, values of K
    # TODO: support more SVM options

    if progressbar is None:
        progressbar = status_fn is True
    status_fn = get_status_fn(status_fn)

    num_train = len(train_bags)
    train_labels = np.squeeze(train_labels)
    assert train_labels.shape == (num_train,)
    assert is_integer_type(train_labels)

    if divs is None:
        status_fn('Getting divergences...')

        divs = get_divs_cache(
                train_bags + test_bags,
                div_func=div_func, K=K,
                cache_filename=divs_cache,
                n_proc=n_proc, fix_mode=fix_mode, tail=tail, min_dist=min_dist,
                status_fn=status_fn, progressbar=progressbar)
    else:
        #status_fn('Using passed-in divergences...')
        n_bags = len(train_bags) + len(test_bags)
        assert divs.shape == (n_bags, n_bags)

    status_fn('Tuning parameters...')
    sigma, C = tune_params(
            divs=np.ascontiguousarray(divs[:num_train, :num_train]),
            labels=train_labels,
            num_folds=tuning_folds,
            n_proc=n_proc,
            C_vals=C_vals,
            sigma_vals=sigma_vals, scale_sigma=scale_sigma,
            weight_classes=weight_classes,
            cache_size=tuning_cache_size,
            svm_tol=tuning_svm_tol,
            svm_max_iter=tuning_svm_max_iter,
            svm_shrinking=svm_shrinking,
            status_fn=status_fn,
            progressbar=progressbar)
    status_fn('Selected sigma {}, C {}'.format(sigma, C))

    status_fn('Doing final projection')
    train_km, test_km = split_km(
            make_km(divs, sigma),
            xrange(num_train),
            xrange(num_train, divs.shape[0]))

    status_fn('Training final SVM')
    clf = svm.SVC(
            C=C,
            cache_size=cache_size,
            class_weight='auto' if weight_classes else None,
            tol=svm_tol,
            kernel='precomputed',
            max_iter=svm_max_iter,
            shrinking=svm_shrinking,
    )
    clf.fit(train_km, train_labels)

    preds = clf.predict(test_km)
    assert np.all(preds == np.round(preds))
    preds = preds.astype(int)

    return (preds, (sigma, C)) if return_config else preds


################################################################################
### Cross-validation helper

def crossvalidate(bags, labels, num_folds=10,
        div_func='renyi:.9',
        K=DEFAULT_K,
        tuning_folds=DEFAULT_TUNING_FOLDS,
        project_all=True,
        n_proc=None,
        C_vals=DEFAULT_C_VALS,
        sigma_vals=DEFAULT_SIGMA_VALS, scale_sigma=True,
        weight_classes=False,
        cache_size=DEFAULT_SVM_CACHE, tuning_cache_size=DEFAULT_SVM_CACHE,
        svm_tol=DEFAULT_SVM_TOL, tuning_svm_tol=DEFAULT_SVM_TOL,
        svm_max_iter=DEFAULT_SVM_ITER,
        tuning_svm_max_iter=DEFAULT_SVM_ITER_TUNING,
        svm_shrinking=DEFAULT_SVM_SHRINKING,
        status_fn=True,
        progressbar=None,
        fix_mode=FIX_MODE_DEFAULT, tail=TAIL_DEFAULT, min_dist=None,
        divs=None,
        divs_cache=None):

    args = locals()
    opts = dict((v, args[v]) for v in
        ['div_func', 'K', 'tuning_folds', 'n_proc', 'C_vals', 'sigma_vals',
         'scale_sigma', 'weight_classes', 'cache_size', 'tuning_cache_size',
         'svm_tol', 'tuning_svm_tol', 'svm_max_iter', 'tuning_svm_max_iter',
         'svm_shrinking', 'status_fn', 'progressbar', 'fix_mode', 'tail'])

    status = get_status_fn(status_fn)

    num_bags = len(bags)
    dim = bags[0].shape[1]
    assert all(bag.ndim == 2 and bag.shape[1] == dim and bag.shape[0] > 0
               for bag in bags)

    labels = np.squeeze(labels)
    assert labels.shape == (num_bags,)
    assert is_integer_type(labels)

    if divs is None:
        status('Getting divergences...')
        divs = get_divs_cache(bags, div_func=div_func, K=K,
                cache_filename=divs_cache,
                fix_mode=fix_mode, tail=tail, min_dist=min_dist,
                status_fn=status_fn, progressbar=progressbar)
    else:
        #status_fn('Using passed-in divergences...')
        assert divs.shape == (num_bags, num_bags)

    preds = -np.ones(num_bags, dtype=int)

    for i, (train, test) in \
            enumerate(KFold(n=num_bags, n_folds=num_folds, shuffle=True), 1):
        status('')
        status('Starting fold {} / {}'.format(i, num_folds))
        train_bags = itemgetter(*train)(bags)
        train_labels = labels[train]
        test_bags = itemgetter(*test)(bags)
        both = np.hstack((train, test))
        if project_all:
            preds[test] = transduct(
                    train_bags, train_labels, test_bags,
                    divs=divs[np.ix_(both, both)],
                    **opts)
        else:
            clf = SupportDistributionMachine(**opts)
            clf.fit(train_bags, train_labels, divs=divs[np.ix_(train, train)])
            preds[test] = clf.predict(test_bags)

    # TODO: optionally return params for each fold, what the folds were, ...

    return np.mean(preds == labels), preds


################################################################################
### Command-line interface

def parse_args():
    import argparse

    # helper for boolean flags
    # based on http://stackoverflow.com/a/9236426/344821
    class ActionNoYes(argparse.Action):
        def __init__(self, opt_name, off_name=None, dest=None,
                     default=True, required=False, help=None):

            if off_name is None:
                off_name = 'no-' + opt_name
            self.off_name = '--' + off_name

            if dest is None:
                dest = opt_name.replace('-', '_')

            super(ActionNoYes, self).__init__(
                    ['--' + opt_name, '--' + off_name],
                    dest, nargs=0, const=None,
                    default=default, required=required, help=help)

        def __call__(self, parser, namespace, values, option_string=None):
            setattr(namespace, self.dest, option_string != self.off_name)

    # component of a help string that adds the default value
    _def = "(default %(default)r)."

    # add common options to a parser
    # would use parents=[...], except for http://bugs.python.org/issue16807
    def add_opts(parser):
        algo = parser.add_argument_group('algorithm options')

        m = algo.add_mutually_exclusive_group()
        m.set_defaults(mode='transduct')
        m.add_argument('--transduct',
            action='store_const', dest='mode', const='transduct',
            help="Operate transductively (project full Gram matrix; default).")
        m.add_argument('--induct',
            action='store_const', dest='mode', const='induct',
                help="Operate inductively (only project training Gram matrix).")

        algo.add_argument('--div-func', '-d', default='renyi:.9',
            type=normalize_div_name,
            help="The divergence function to use " + _def)

        algo.add_argument('-K', type=positive_int, default=DEFAULT_K,
            help="How many nearest neighbors to use " + _def)

        algo.add_argument('--n-points', type=positive_int, default=None,
            help="The number of points to use per group; defaults to all.")

        algo.add_argument('--tuning-folds', '-F', type=positive_int,
            default=DEFAULT_TUNING_FOLDS,
            help="Number of CV folds to use in evaluating parameters " + _def)

        comp = parser.add_argument_group('computation options')
        comp.add_argument('--n-proc', type=positive_int, default=None,
            help="Number of processes to use; default is as many as CPU cores.")

        comp.add_argument('--svm-tol',
            type=positive_float, default=DEFAULT_SVM_TOL,
            help="SVM solution tolerance " + _def)
        comp.add_argument('--svm-max-iter',
            type=positive_int, default=DEFAULT_SVM_ITER,
            help="Limit on the number of SVM iterations " + _def)
        comp.add_argument('--svm-unlimited-iter',
            action='store_const', const=-1, dest='svm_max_iter',
            help="Let the SVM try to iterate until full convergence.")
        comp.add_argument('--cache-size',
            type=positive_float, default=DEFAULT_SVM_CACHE,
            help="Size of the SVM cache, in megabytes " + _def)
        comp._add_action(ActionNoYes('svm-shrinking', default=True,
            help="Use the shrinking heuristics in the SVM (default: do)."))

        comp.add_argument('--tuning-svm-tol',
            type=positive_float, default=DEFAULT_SVM_TOL,
            help="SVM solution tolerance in tuning " + _def)
        comp.add_argument('--tuning-svm-max-iter',
            type=positive_int, default=DEFAULT_SVM_ITER_TUNING,
            help="Limit on the number of SVM iterations in tuning " + _def)
        comp.add_argument('--tuning-svm-unlimited-iter',
            action='store_const', const=-1, dest='tuning_svm_max_iter',
            help="Let the SVM try to iterate until full convergence in tuning.")
        comp.add_argument('--tuning-cache-size', type=positive_float,
            default=DEFAULT_SVM_CACHE,
            help="Size of tuning SVMs' cache, in megabytes " + _def)

        algo._add_action(ActionNoYes('weight-classes', default=False,
            help="Reweight SVM loss to equalize classes (default: don't)."))

        algo.add_argument('--c-vals', '-C', type=positive_float, nargs='+',
            default=DEFAULT_C_VALS, metavar='C',
            help="Values to try for tuning SVM regularization strength " + _def)
        algo.add_argument('--sigma-vals', '-S', type=positive_float, nargs='+',
            default=DEFAULT_SIGMA_VALS, metavar='SIGMA',
            help="Values to try for tuning kernel bandwidth sigma " + _def)

        algo._add_action(ActionNoYes('scale-sigma', default=True,
            help="Scale --sigma-vals by the median nonzero divergence; "
                 "does by default."))

        algo.add_argument('--trim-tails', type=portion, metavar='PORTION',
            default=TAIL_DEFAULT,
            help="How much to trim when using a trimmed mean estimator " + _def)
        parser.add_argument('--trim-mode',
            choices=FIX_TERM_MODES, default=FIX_MODE_DEFAULT,
            help="Whether to trim or clip ends; default %(default)s.")
        algo.add_argument('--min-dist', type=float, default=None,
            help="Protect against identical points by making sure kNN "
                 "distances are always at least this big. Default: the smaller "
                 "of .01 and 10 ^ (100 / dim).")

    ### the top-level parser
    parser = argparse.ArgumentParser(
            description='Performs support distribution machine classification.')
    subparsers = parser.add_subparsers(dest='subcommand',
            help="The kind of action to perform.")

    ### parser for the prediction task
    parser_pred = subparsers.add_parser('predict',
            help="Train on labeled training data, predict on test data.")
    parser_pred.set_defaults(func=do_predict)

    io = parser_pred.add_argument_group('input/output options')
    io.add_argument('input_file',
        help="The input HDF5 file (e.g. a .mat file with -v7.3).")

    io.add_argument('--train-bags-name', default='train_bags',
        help="The name of a cell array of row-instance data matrices " + _def)
    io.add_argument('--test-bags-name', default='test_bags',
        help="The name of a cell array of row-instance data matrices " + _def)
    io.add_argument('--train-labels-name', default='train_labels',
        help="The name of a vector of integer training labels " + _def)

    io.add_argument('--output-file', required=False,
        help="Name of the output file; defaults to input_file.sdm_preds.mat.")

    io.add_argument('--div-cache-file',
        help="An HDF5 file that serves as a cache of divergences.")

    add_opts(parser_pred)

    ### parser for the cross-validation task
    parser_cv = subparsers.add_parser('cv',
            help="Cross-validate predictions on fully labeled data.")
    parser_cv.set_defaults(func=do_cv)

    io = parser_cv.add_argument_group('input/output options')
    io.add_argument('input_file',
        help="The input HDF5 file (e.g. a .mat file with -v7.3).")
    io.add_argument('--input-format',
        choices=['matlab', 'python'], default='python',
        help="Whether the features file was generated by the matlab code or "
             "the python code; default python.")

    io.add_argument('--bags-name', default='bags',
        help="The name of a cell array of row-instance data matrices " + _def
             + " Only used for matlab format.")
    io.add_argument('--labels-name', default='labels',
        help="The name of a vector of training labels (integers) " + _def
             + " Only used for matlab format.")

    io.add_argument('--cv-folds', '-f', type=positive_int, default=10,
        help="The number of cross-validation folds " + _def)

    io.add_argument('--output-file', required=False,
        help="Name of the output file; defaults to input_file.sdm_cv.mat.")

    io.add_argument('--div-cache-file',
        help="An HDF5 file that serves as a cache of divergences.")

    add_opts(parser_cv)

    ### parse the arguments and do some post-processing
    args = parser.parse_args()

    if args.output_file is None:
        suffixes = {
            'predict': '.sdm_preds.mat',
            'cv': '.sdm_cv.mat',
        }
        args.output_file = args.input_file + suffixes[args.subcommand]

    args.c_vals = np.sort(args.c_vals)
    args.sigma_vals = np.sort(args.sigma_vals)

    return args


def opts_dict(args):
    return dict(
        div_func=args.div_func,
        K=args.K,
        tuning_folds=args.tuning_folds,
        n_proc=args.n_proc,
        C_vals=args.c_vals,
        sigma_vals=args.sigma_vals, scale_sigma=args.scale_sigma,
        weight_classes=args.weight_classes,
        cache_size=args.cache_size,
        tuning_cache_size=args.tuning_cache_size,
        svm_tol=args.svm_tol,
        tuning_svm_tol=args.tuning_svm_tol,
        svm_max_iter=args.svm_max_iter,
        tuning_svm_max_iter=args.tuning_svm_max_iter,
        svm_shrinking=args.svm_shrinking,
        tail=args.trim_tails,
        fix_mode=args.trim_mode,
        min_dist=args.min_dist,
    )


def do_predict(args):
    status_fn = get_status_fn(True)

    status_fn('Reading inputs...')
    with h5py.File(args.input_file, 'r') as f:
        train_bags = read_cell_array(f, f[args.train_bags_name])
        train_labels = f[args.train_labels_name][...]
        test_bags = read_cell_array(f, f[args.test_bags_name])
        if args.n_points:
            train_bags = subset_data(train_bags, args.n_points)
            test_bags = subset_data(test_bags, args.n_points)

    assert np.all(train_labels == np.round(train_labels))
    train_labels = train_labels.astype(int)

    if args.mode == 'transduct':
        preds, (sigma, C) = transduct(
                train_bags, train_labels, test_bags,
                divs_cache=args.div_cache_file,
                return_config=True, **opts_dict(args))
    elif args.mode == 'induct':
        clf = SupportDistributionMachine(status_fn=True, **opts_dict(args))
        clf.fit(train_bags, train_labels, divs_cache=args.div_cache_file)
        sigma = clf.sigma_
        C = clf.C_
        preds = clf.predict(test_bags)

    out = {
        'div_func': args.div_func,
        'K': args.K,
        'C_vals': args.c_vals,
        'sigma_vals': args.sigma_vals,
        'scale_sigma': args.scale_sigma,
        'C': C,
        'sigma': sigma,
        'preds': preds,
    }
    status_fn('Saving output to {}'.format(args.output_file))
    scipy.io.savemat(args.output_file, out, oned_as='column')


def do_cv(args):
    status_fn = get_status_fn(True)

    status_fn('Reading inputs...')
    with h5py.File(args.input_file, 'r') as f:
        if args.input_format == 'matlab':
            bags = read_cell_array(f, f[args.bags_name])
            labels = f[args.labels_name][...]
        else:
            label_encoder = LabelEncoder()
            bags = []
            label_strs = []
            for label, label_g in iteritems(f):
                for g in itervalues(label_g):
                    bags.append(g['features'][...])
                    label_strs.append(label)
            labels = label_encoder.fit_transform(label_strs)
        if args.n_points:
            bags = subset_data(bags, args.n_points)

    assert np.all(labels == np.round(labels))
    labels = labels.astype(int)

    opts = opts_dict(args)
    acc, preds = crossvalidate(bags, labels, num_folds=args.cv_folds,
                               divs_cache=args.div_cache_file, **opts)

    status_fn('')
    status_fn('Accuracy: {:.1%}'.format(acc))

    out = {
        'div_func': args.div_func,
        'K': args.K,
        'C_vals': args.c_vals,
        'sigma_vals': args.sigma_vals,
        'scale_sigma': args.scale_sigma,
        'preds': preds,
        'acc': acc,
    }
    status_fn('Saving output to {}'.format(args.output_file))
    scipy.io.savemat(args.output_file, out, oned_as='column')


def main():
    args = parse_args()
    args.func(args)


if __name__ == '__main__':
    main()