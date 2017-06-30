# -*- coding: utf-8 -*-
"""
Created on Tue Mar 17 14:41:31 2015

Read gait trials.

@author: Jussi (jnu@iki.fi)
"""


from __future__ import division
from exceptions import GaitDataError
from . import read_data, utils, eclipse
from collections import defaultdict
import numpy as np
import os.path as op
import glob
import models
from emg import EMG
import logging

logger = logging.getLogger(__name__)


class Gaitcycle(object):
    """" Holds information about one gait cycle """
    def __init__(self, start, end, offset, toeoff, context, on_forceplate,
                 smp_per_frame):
        self.offset = offset
        self.len = end - start
        # convert frame indices to 0-based
        self.start = start - offset
        self.end = end - offset
        self.toeoff = toeoff - offset
        # which foot begins and ends the cycle
        self.context = context
        # whether cycle begins with forceplate strike
        self.on_forceplate = on_forceplate
        # start and end on the analog samples axis; round to whole samples
        self.start_smp = int(round(self.start * smp_per_frame))
        self.end_smp = int(round(self.end * smp_per_frame))
        self.len_smp = self.end_smp - self.start_smp
        # normalized x-axis (% of gait cycle) of same length as cycle
        self.t = np.linspace(0, 100, self.len)
        # same for analog variables
        self.tn_analog = np.linspace(0, 100, self.len_smp)
        # normalized x-axis of 0,1,2..100%
        self.tn = np.linspace(0, 100, 101)
        # normalize toe-off event to the cycle
        self.toeoffn = round(100*((self.toeoff - self.start) / self.len))

    def __repr__(self):
        s = '<Gaitcycle |'
        s += ' offset: %d,' % self.offset
        s += ' start: %d,' % self.start
        s += ' end: %d,' % self.end
        s += ' context: %s,' % self.context
        s += ' on forceplate,' if self.on_forceplate else ' not on forceplate,'
        s += ' toeoff: %d' % self.toeoff
        s += '>'
        return s

    def normalize(self, var):
        """ Normalize frames-based variable var to the cycle.
        New interpolated x axis is 0..100% of the cycle. """
        return self.tn, np.interp(self.tn, self.t, var[self.start:self.end])

    def crop_analog(self, var):
        """ Crop analog variable (EMG, forceplate, etc. ) to the
        cycle; no interpolation. """
        return self.tn_analog, var[self.start_smp:self.end_smp]


class Trial(object):
    """ A gait trial. Contains:
    -subject and trial info
    -gait cycles
    -analog data (EMG, forceplate, etc.)
    -model output variables (Plug-in Gait, muscle length, etc.)
    TODO:
    -lazy reads should check whether underlying trial has changed
    (at least for Nexus)
    """

    def __repr__(self):
        s = '<Trial |'
        s += ' trial: %s' % self.trialname
        s += ', data source: %s' % self.source
        s += ', subject: %s' % self.name
        s += ', gait cycles: %s' % self.ncycles
        s += '>'
        return s

    def __init__(self, source):
        logger.debug('new trial instance from %s' % source)
        self.source = source
        # read metadata into instance attributes
        meta = read_data.get_metadata(source)
        self.__dict__.update(meta)
        # events may be in wrong temporal order, at least in c3d files
        for li in [self.lstrikes, self.rstrikes, self.ltoeoffs,
                   self.rtoeoffs]:
            li.sort()
        # get description and notes from Eclipse database
        if not self.sessionpath[-1] == '\\':
            self.sessionpath = self.sessionpath+('\\')
        self.trialdirname = self.sessionpath.split('\\')[-2]
        # TODO: sometimes trial .enf name seems to be different?
        enfpath = self.sessionpath + self.trialname + '.Trial.enf'
        if op.isfile(enfpath):
            edata = eclipse.get_eclipse_keys(enfpath)
            # for convenience, eclipse_data returns '' for nonexistent keys
            self.eclipse_data = defaultdict(lambda: '', edata)
        else:
            self.eclipse_data = defaultdict(lambda: '', {})
        # analog and model data are lazily read
        self.emg = EMG(self.source)
        self._forceplate_data = None
        self._fp_events = None
        self._models_data = dict()
        # whether to normalize data
        self._normalize = None
        # frames 0...length
        self.t = np.arange(self.length)
        # analog frames 0...length
        self.t_analog = np.arange(self.length * self.samplesperframe)
        # normalized x-axis of 0, 1, 2 .. 100%
        self.tn = np.linspace(0, 100, 101)
        self.samplesperframe = self.analograte/self.framerate
        self.cycles = list(self._scan_cycles())
        self.ncycles = len(self.cycles)
        self.video_files = glob.glob(self.sessionpath+self.trialname+'*avi')

    def __getitem__(self, item):
        """ Get model variable or EMG channel by indexing, normalized
        according to normalization cycle. Does not check for duplicate names.
        """
        try:
            t = self.t
            data = self._get_modelvar(item)
            if self._normalize:
                t, data = self._normalize.normalize(data)
            return t, data
        except ValueError:
                t = self.t_analog
                data = self.emg[item]
                if self._normalize:
                    t, data = self._normalize.crop_analog(data)
                return t, data

    @property
    def forceplate_data(self):
        if not self._forceplate_data:
            self._forceplate_data = read_data.get_forceplate_data(self.source)
        return self._forceplate_data

    @property
    def fp_events(self):
        if not self._fp_events:
            try:
                self._fp_events = utils.detect_forceplate_events(self.source)
            except GaitDataError:
                self._fp_events = None
        return self._fp_events

    def set_norm_cycle(self, cycle=None):
        """ Set normalization cycle (int for cycle index or a Gaitcycle
        instance). None to get unnormalized data. Affects the data returned
        by __getitem__ """
        if type(cycle) == int:
            cycle = self.cycles[cycle]
        self._normalize = cycle if cycle else None

    def get_cycle(self, context, ncycle):
        """ e.g. ncycle=2 and context='L' returns 2nd left gait cycle. """
        cycles = [cycle for cycle in self.cycles
                  if cycle.context == context.upper()]
        if ncycle < 1:
            raise GaitDataError('Index of gait cycle must be >= 1')
        if len(cycles) < ncycle:
            raise GaitDataError('Requested gait cycle %d does not '
                                'exist in data' % ncycle)
        else:
            return cycles[ncycle-1]

    def _get_modelvar(self, var):
        """ Return (unnormalized) model variable, load and cache data for
        model if needed """
        model_ = models.model_from_var(var)
        if not model_:
            raise GaitDataError('No model found for %s' % var)
        if model_.desc not in self._models_data:
            # read and cache model data
            modeldata = read_data.get_model_data(self.source, model_)
            self._models_data[model_.desc] = modeldata
        return self._models_data[model_.desc][var]

    def _scan_cycles(self):
        """ Create gait cycle instances based on strike/toeoff markers. """
        STRIKE_TOL = 4  # tolerance for matching forceplate strikes (frames)
        for strikes in [self.lstrikes, self.rstrikes]:
            len_s = len(strikes)
            if len_s < 2:
                return
            if strikes == self.lstrikes:
                toeoffs = self.ltoeoffs
                context = 'L'
            else:
                toeoffs = self.rtoeoffs
                context = 'R'
            for k in range(0, len_s-1):
                start = strikes[k]
                # see if cycle starts on forceplate strike
                fp_strikes = np.array(self.fp_events[context + '_strikes'])
                if fp_strikes.size == 0:
                    on_forceplate = False
                else:
                    # offset is needed since events are not 0-offset here
                    diffs = np.abs(fp_strikes - start + self.offset)
                    on_forceplate = min(diffs) <= STRIKE_TOL
                end = strikes[k+1]
                toeoff = [x for x in toeoffs if x > start and x < end]
                if len(toeoff) == 0:
                    raise GaitDataError('No toeoff for cycle starting at %d'
                                        % start)
                elif len(toeoff) > 1:
                    raise GaitDataError('Multiple toeoffs for cycle starting '
                                        'at %d' % start)
                else:
                    toeoff = toeoff[0]
                yield Gaitcycle(start, end, self.offset, toeoff, context,
                                on_forceplate, self.samplesperframe)
