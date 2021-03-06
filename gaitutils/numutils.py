# -*- coding: utf-8 -*-
"""
Created on Fri Sep 23 11:17:10 2016

Misc numerical utils

@author: Jussi (jnu@iki.fi)
"""

from __future__ import division

import logging
import numpy as np
import hashlib
from scipy import signal
from scipy.signal import medfilt
from scipy.special import erfcinv
from numpy.lib.stride_tricks import as_strided

logger = logging.getLogger(__name__)


def mad(data, axis=None, scale=1.4826, keepdims=False):
    """Median absolute deviation or MAD. Defined as the median absolute
    deviation from the median of the data. A robust alternative to stddev.
    Identical to scipy.stats.median_absolute_deviation(), which does
    not take a keepdims argument.

    Parameters
    ----------
    data : array_like
        The data
    scale : float
        Scaling of the result. By default, it is scaled to give a consistent
        estimate of the standard deviation of values from a normal
        distribution.
    axis : Axis or axes along which to compute MAD.

    Returns
    -------
    res : ndarray
        The result
    """
    # keep dims here so that broadcasting works
    med = np.median(data, axis=axis, keepdims=True)
    abs_devs = np.abs(data - med)
    return scale * np.median(abs_devs, axis=axis, keepdims=keepdims)


def modified_zscore(data, axis=None, single_mad=None):
    """Modified Z-score.

    Z-score analogue computed using robust (median-based) statistics.

    Parameters
    ----------
    data : array_like
        The data
    axis : Axis or axes along which to compute the statistic.
    single_mad : bool
        Use a single MAD estimate computed all over the data. If False, MAD
        will be computed along given axis (e.g. separately for each variable).

    Returns
    -------
    res : ndarray
        The result
    """
    med_ = np.median(data, axis=axis, keepdims=True)
    mad_ = mad(data, axis=axis, keepdims=True)
    if single_mad:
        mad_ = np.median(mad_)
    return (data - med_) / mad_


def outliers(x, axis=0, single_mad=None, p_threshold=1e-3):
    """Robustly detect outliers assuming a normal distribution.
    
    A modified Z-score is first computed based on the data. Then a threshold
    Z is computed according to p_threshold, and values that exceed it
    are rejected. p_threshold is the probability of rejection for strictly
    normally distributed data, i.e. probability for "false outlier"

    Parameters
    ----------
    data : array_like
        The data
    axis : Axis or axes along which to compute the Z scores. E.g. axis=0
        computes row-wise Z scores and rejects based on those.
    single_mad : bool
        Use a single MAD estimate computed all over the data. If False, MAD
        will be computed along given axis (e.g. separately for each variable).

    Returns
    -------
    idx : tuple
        Indexes of rejected values (as in np.where output)
    """
    zs = modified_zscore(x, axis=axis, single_mad=single_mad)
    z_threshold = np.sqrt(2) * erfcinv(p_threshold)
    logger.debug('Z threshold: %.2f' % z_threshold)
    return np.where(abs(zs) > z_threshold)


def files_digest(files):
    """Create total md5 digest for a list of files"""
    hashes = sorted(file_digest(fn) for fn in files)
    # concat as unicode and encode to get a definite byte representation
    # in both py2 and py3
    hash_str = u''.join(hashes).encode('utf-8')
    return hashlib.md5(hash_str).hexdigest()


def file_digest(fn):
    """Return md5 digest for file"""
    with open(fn, 'rb') as f:
        data = f.read()
    return hashlib.md5(data).hexdigest()


def rolling_fun_strided(m, fun, win, axis=None):
    """ Window array along given axis and apply fun() to the windowed data.
    No padding, i.e. returned array is shorter in the axis dim by (win-1) """
    if axis is None:
        m = m.flatten()
        axis = 0
    sh = m.shape
    st = m.strides
    # break up the given dim into windows, insert a new dim
    sh_ = sh[:axis] + (sh[axis] - win + 1, win) + sh[axis + 1 :]
    # insert a stride for the new dim, same as for the given dim
    st_ = st[:axis] + (st[axis], st[axis]) + st[axis + 1 :]
    # apply fun on the new dimension
    return fun(as_strided(m, sh_, st_), axis=axis + 1)


def rising_zerocross(x):
    """ Return indices of rising zero crossings in sequence,
    i.e. n where x[n] >= 0 and x[n-1] < 0 """
    x = np.array(x)  # this should not hurt
    return np.where(np.logical_and(x[1:] >= 0, x[:-1] < 0))[0] + 1


def falling_zerocross(x):
    return rising_zerocross(-x)


def _padded_shift(x, n):
    """Shift x right by n samples (or left if negative) and zero pad so
    that original length is kept"""
    pads = (n, 0) if n > 0 else (0, -n)
    x_ = np.pad(x, pads, mode='constant')
    return x_[:-n] if n > 0 else x_[-n:]


def best_match(v, b):
    """ Replace elements of v using their closest matches in b """
    v = np.array(v)
    b = np.array(b)
    if b.size == 0:
        return v
    inds = np.abs(v[np.newaxis, :] - b[:, np.newaxis]).argmin(axis=0)
    return b[inds]


def isfloat(x):
    """ Return True for float-conversible values, False otherwise """
    try:
        float(x)
        return True
    except ValueError:
        return False


def isint(x):
    """ Return True for int-conversible values, False otherwise """
    try:
        int(x)
        return True
    except ValueError:
        return False


def _baseline(v):
    """ Baseline v using histogram. Subtracts the most prominent
    signal level """
    v = v.squeeze()
    if len(v.shape) != 1:
        raise ValueError('Need 1-dim input')
    v = np.array(v)
    nbins = int(len(v) / 10)  # exact n of bins should not matter
    ns, edges = np.histogram(v, bins=nbins)
    peak_ind = np.where(ns == np.max(ns))[0][0]
    return v - np.mean(edges[peak_ind : peak_ind + 2])


def center_of_pressure(F, M, dz):
    """ Compute CoP according to AMTI instructions. The results differ
    slightly (few mm) from Nexus, for unknown reasons (different filter?)
    See http://health.uottawa.ca/biomech/courses/apa6903/amticalc.pdf """
    FP_FILTFUN = medfilt  # filter function
    FP_FILTW = 5  # median filter width
    fx, fy, fz = tuple(F.T)  # split columns into separate vars
    mx, my, mz = tuple(M.T)
    fz = FP_FILTFUN(fz, FP_FILTW)
    nz_inds = np.where(np.abs(fz) > 0)[0]  # only divide on nonzero inds
    cop = np.zeros((fx.shape[0], 3))
    cop[nz_inds, 0] = -(my[nz_inds] + fx[nz_inds] * dz) / fz[nz_inds]
    cop[nz_inds, 1] = (mx[nz_inds] - fy[nz_inds] * dz) / fz[nz_inds]
    return cop


def change_coords(pts, wR, wT):
    """ Translate pts (N x 3) into a new coordinate system described by
    rotation matrix wR and translation vector wT """
    pts = np.array(pts)
    return np.dot(wR, pts.T).T + wT


def segment_angles(P):
    """ Compute angles between segments defined by ordered points in P
    (N x 3 array). Can also be 3-d matrix of T x N x 3 to get time-dependent
    data. Output will be (N-2) vector or T x (N-2) matrix of angles in radians.
    If successive points are identical, nan:s will be output for the
    corresponding angles.
    """
    if P.shape[-1] != 3 or len(P.shape) not in [2, 3]:
        raise ValueError('Invalid shape of input matrix')
    if len(P.shape) == 2:
        P = P[np.newaxis, ...]  # insert singleton time axis
    Pd = np.diff(P, axis=1)  # point-to-point vectors
    vnorms = np.linalg.norm(Pd, axis=2)[..., np.newaxis]
    # ignore 0/0 and x/0 errors -> nan
    with np.errstate(divide='ignore', invalid='ignore'):
        Pdn = Pd / vnorms
    # take dot products between successive vectors and angles by arccos
    dots = np.sum(Pdn[:, 0:-1, :] * Pdn[:, 1:, :], axis=2)
    dots = dots[0, :] if dots.shape[0] == 1 else dots  # rm singleton dim
    return np.pi - np.arccos(dots)


def running_sum(M, win, axis=None):
    """ Running (windowed) sum of sequence M using cumulative sum,
        along given axis. Inspired by
        http://arogozhnikov.github.io/2015/09/30/NumpyTipsAndTricks2.html """
    if axis is None:
        M = M.flatten()
    s = np.cumsum(M, axis=axis)
    s = np.insert(s, 0, [0], axis=axis)
    len_ = s.shape[0] if axis is None else s.shape[axis]
    return s.take(np.arange(win, len_), axis=axis) - s.take(
        np.arange(0, len_ - win), axis=axis
    )


def rms(data, win, axis=None, pad_mode=None):
    """Return rolling window RMS for a numpy array."""
    if pad_mode is None:
        pad_mode = 'edge'
    if win % 2 != 1:
        raise ValueError('Need RMS window of odd length')
    datalen = len(data) if axis is None else data.shape[axis]
    if win > datalen:
        raise ValueError('Need win length < data length')
    rms_ = np.sqrt(running_sum(data ** 2, win, axis=axis) / win)
    # pad RMS data so that lengths are matched
    padw = int((win - 1) / 2)
    padarg_axis = (padw, padw)
    if axis == None:
        eff_dim = 1
        axis = 0
    else:
        eff_dim = data.ndim
    padarg = eff_dim * [(0, 0)]
    padarg[axis] = padarg_axis
    return np.pad(rms_, padarg, mode=pad_mode)


def filtfilt(data, passband, sfrate, buttord=5):
    """Filter given data to passband, e.g. [1, 40] (in Hz)
    Implemented as pure lowpass, if highpass freq = 0 """
    if passband is None:
        return data
    passbandn = 2 * np.array(passband) / sfrate
    if passbandn[0] > 0:  # bandpass
        b, a = signal.butter(buttord, passbandn, 'bandpass')
    else:  # lowpass
        b, a = signal.butter(buttord, passbandn[1])
    return signal.filtfilt(b, a, data)
