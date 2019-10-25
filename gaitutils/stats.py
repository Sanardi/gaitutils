# -*- coding: utf-8 -*-
"""
Compute statistics across/within trials


@author: Jussi (jnu@iki.fi)
"""

import logging
import numpy as np
import os.path as op
from collections import defaultdict

from .trial import Trial, Gaitcycle
from . import models, GaitDataError, cfg, numutils

logger = logging.getLogger(__name__)


class AvgTrial(Trial):
    """Trial containing cycle-averaged data"""

    def __repr__(self):
        s = '<AvgTrial |'
        s += ' trial name: %s' % self.trialname
        s += ', trials: %s' % self.source
        s += '>'
        return s

    def __init__(
        self,
        trials,
        sessionpath=None,
        reject_zeros=True,
        reject_outliers=None,
        fp_cycles_only=False,
    ):
        avgdata, stddata, n_ok, _ = average_trials(
            trials, reject_outliers=reject_outliers, fp_cycles_only=fp_cycles_only
        )
        # nfiles may be misleading since not all trials may contain valid data
        self.nfiles = len(trials)
        self.trials = trials
        if sessionpath:
            self.sessionpath = sessionpath
            self.sessiondir = op.split(sessionpath)[-1]
            self.trialname = '%s avg.' % self.sessiondir
        else:
            self.trialname = '%d trial avg.' % self.nfiles
            self.sessiondir = None
            self.sessionpath = None
        self.source = 'averaged data'
        self.name = 'Unknown'
        self._model_data = avgdata
        self.stddev_data = stddata
        self.n_ok = n_ok
        self.t = np.arange(101)  # data is on normalized cycle 0..100%
        # fake 2 gait cycles, L/R
        self.cycles = list()
        self.cycles.append(
            Gaitcycle(0, 101, 60, 'R', True, 1, 1000, name='right', trial=self)
        )
        self.cycles.append(
            Gaitcycle(0, 101, 60, 'L', True, 1, 1000, name='left', trial=self)
        )
        self.ncycles = 2
        self.eclipse_data = defaultdict(lambda: '', {})

    @property
    def emg(self):
        # FIXME: could average EMG RMS
        raise GaitDataError('EMG averaging not supported yet')

    def get_model_data(self, var):
        return self.t, self._model_data[var]

    def set_norm_cycle(self, cycle=None):
        if cycle is None:
            raise ValueError('AvgTrial does not support unnormalized data')
        else:
            logger.debug('setting norm. cycle for AvgTrial (no effect)')


def average_trials(
    trials,
    fp_cycles_only=False,
    reject_zeros=True,
    reject_outliers=1e-3,
    use_medians=False,
):
    """ Average model data from several trials.

    Parameters
    ----------
    trials : list
        filename, or list of filenames (c3d) to read trials from, or list
        of Trial instances
    fp_cycles_only : bool
        If True, only collect data from forceplate cycles. Kinetics will always
        be collected from forceplate cycles only.
    reject_zeros : bool
        Reject any curves which contain zero (0.000...) values. Zero values are
        sometimes used to mark gaps. No zero rejection is done for kinetic variables,
        since these may actually become zero due to clamping of force data.
    reject_outliers : float or None
        None for no automatic outlier rejection. Otherwise, a P value for false
        rejection (assuming strictly normally distributed data). Outliers are
        rejected based on robust statistics (modified Z-score) 
    use_medians: bool
        Use median and MAD (median absolute deviation) instead of mean and
        stddev. The median is robust to outliers but not robust to small
        sample size. Thus, use of medians may be a bad idea for small samples.

    Returns
    -------

    avgdata: dict
        Averaged (or median) data
    stddata: dict
        Standard dev (or MAD) for each var
    N_ok: dict
        N of accepted cycles for each var
    Ncyc: dict
        WIP
    """
    data, Ncyc = _collect_model_data(trials, fp_cycles_only=fp_cycles_only)
    if data is None:
        return (None,) * 4

    stddata = dict()
    avgdata = dict()
    N_ok = dict()

    for var, vardata in data.items():
        if vardata is None:
            stddata[var] = None
            avgdata[var] = None
            N_ok[var] = 0
            continue
        else:
            Ntot = vardata.shape[0]
            if reject_zeros:
                model_this = models.model_from_var(var)
                if not model_this.is_kinetic_var(var):
                    rows_bad = np.where(np.any(vardata == 0, axis=1))[0]
                    if len(rows_bad) > 0:
                        logger.info(
                            '%s: rejecting %d curves with zero values'
                            % (var, len(rows_bad))
                        )
                        vardata = np.delete(vardata, rows_bad, axis=0)
            n_ok = vardata.shape[0]
            if n_ok > 0 and reject_outliers is not None and not use_medians:
                # a Bonferroni type correction for the p-threshold
                p_threshold = reject_outliers / vardata.shape[0]
                # when computing outliers, estimate median absolute deviation
                # using all data, instead of a frame-dependent MAD estimate.
                # this is necessary since frame-based MAD values
                # can become really small especially in small datasets, causing
                # Z-score to blow up and data getting rejected unnecessarily.
                outlier_inds = numutils.outliers(
                    vardata, median_axis=0, mad_axis=None, p_threshold=p_threshold
                )
                outlier_rows = np.unique(outlier_inds[0])
                if outlier_rows.size > 0:
                    logger.info(
                        '%s: rejecting %d outlier(s) (P=%g)'
                        % (var, outlier_rows.size, p_threshold)
                    )
                    vardata = np.delete(vardata, outlier_rows, axis=0)
            n_ok = vardata.shape[0]
            if n_ok == 0:
                stddata[var] = None
                avgdata[var] = None
            elif use_medians:
                stddata[var] = numutils.mad(vardata, axis=0)
                avgdata[var] = np.median(vardata, axis=0)
            else:
                stddata[var] = vardata.std(axis=0) if n_ok > 0 else None
                avgdata[var] = vardata.mean(axis=0) if n_ok > 0 else None
            logger.debug('%s: averaged %d/%d curves' % (var, n_ok, Ntot))
            N_ok[var] = n_ok

    if not avgdata:
        logger.warning('nothing averaged')
    return (avgdata, stddata, N_ok, Ncyc)


def _collect_model_data(trials, fp_cycles_only=False):
    """ Collect given model data across trials and cycles.
    Returns a dict of numpy arrays keyed by variable.

    trials: list
        filename, or list of filenames (c3d) to read trials from, or list
        of Trial instances
    fp_cycles_only: bool
        If True, only collect data from forceplate cycles. Kinetics will always
        be collected from forceplate cycles only.
    """

    if not trials:
        logger.warning('no trials')
        return None, None
    if not isinstance(trials, list):
        trials = [trials]

    data_all = defaultdict(lambda: None)

    nc = dict()
    nc['R'], nc['L'], nc['Rkin'], nc['Lkin'] = (0,) * 4

    for n, trial_ in enumerate(trials):
        # see whether it's already a Trial instance or we need to create one
        if isinstance(trial_, Trial):
            trial = trial_
        else:
            trial = Trial(trial_)

        logger.info('collecting data for %s' % trial.trialname)

        # see which models are included in trial
        models_ok = list()
        for model in models.models_all:
            var = list(model.varnames)[0]
            try:
                trial.get_model_data(var)
                models_ok.append(model)
            except GaitDataError:
                logger.info(
                    'cannot read variable %s from %s, skipping '
                    'corresponding model %s' % (var, trial.trialname, model.desc)
                )
        for model in models_ok:
            # gather data
            for cycle in trial.cycles:
                trial.set_norm_cycle(cycle)
                side = cycle.context
                if cycle.on_forceplate:
                    nc[side + 'kin'] += 1
                nc[side] += 1

                for var in model.varnames:
                    # pick data only if var context matches cycle context
                    # FIXME: this may not work with all models
                    if var[0] == side:
                        # don't collect kinetics if cycle is not on forceplate
                        if (
                            model.is_kinetic_var(var) or fp_cycles_only
                        ) and not cycle.on_forceplate:
                            continue
                        _, data = trial.get_model_data(var)
                        if np.all(np.isnan(data)):
                            logger.info(
                                'no data was found for %s/%s' % (trial.trialname, var)
                            )
                        else:
                            # add as first row or concatenate to existing data
                            data_all[var] = (
                                data[None, :]
                                if data_all[var] is None
                                else np.concatenate([data_all[var], data[None, :]])
                            )
    n = len(trials)
    logger.debug(
        'collected %d trials, %d/%d R/L cycles, %d/%d kinetics cycles'
        % (n, nc['R'], nc['L'], nc['Rkin'], nc['Lkin'])
    )
    return data_all, nc


def _collect_emg_data(trials, rms=True, grid_points=101):
    """Collect cycle normalized EMG data from trials"""
    if not trials:
        logger.warning('no trials')
        return
    if not isinstance(trials, list):
        trials = [trials]

    data_all = dict()
    meta = list()

    chs = cfg.emg.channel_labels.keys()

    for n, trial_ in enumerate(trials):
        # see whether it's already a Trial instance or we need to create one
        if isinstance(trial_, Trial):
            trial = trial_
        else:
            trial = Trial(trial_)

        for cycle in trial.cycles:
            trial.set_norm_cycle(cycle)

            for ch in chs:

                if not trial.emg.context_ok(ch, cycle.context):
                    continue

                # get data on analog sampling grid and compute rms
                try:
                    t, data = trial.get_emg_data(ch)
                except KeyError:
                    logger.warning('no channel %s for %s' % (ch, trial))
                    continue
                if rms:
                    data = numutils.rms(data, cfg.emg.rms_win)
                # transform to desired grid (0..100% by default)
                t_analog = np.linspace(0, 100, len(data))
                tn = np.linspace(0, 100, grid_points)
                data_cyc = np.interp(tn, t_analog, data)
                if ch not in data_all:
                    data_all[ch] = data_cyc[None, :]
                else:
                    data_all[ch] = np.concatenate([data_all[ch], data_cyc[None, :]])
            meta.append('%s: %s' % (trial.trialname, cycle.name))
    return data_all, meta
