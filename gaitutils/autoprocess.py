#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Autoprocess all trials in current Nexus session directory. See autoproc
section in config for options.

1st pass (all trials):
-preprocess
-get fp context + strike/toeoff velocities etc.

2nd pass:
-automark (using velocity stats from previous step)
-run models + save

-write Eclipse info

GAP HANDLING:
    If cfg.autoproc.fail_on_gaps is set, the processing will fail on ANY gaps.
    Otherwise, a ROI (region of interest) will be determined based on
    cfg.autoproc.events_range. Gaps outside the ROI will not affect processing.
    However the tracking markers (cfg.autoproc.track_markers) are used to
    determine the ROI and they may not have any gaps anywhere.

@author: Jussi (jnu@iki.fi)
"""

from builtins import zip
import os
import os.path as op
import numpy as np
import time
import logging
import itertools

from . import (nexus, eclipse, utils, GaitDataError, sessionutils,
               read_data, cfg)
from gui.qt_widgets import ProgressSignals

logger = logging.getLogger(__name__)


def _do_autoproc(enffiles, signals=None):
    """Run autoprocessing for all enffiles (list of paths to .enf files).
    """

    # signals is used to track progress across threads; if not given, just
    # create a dummy one to simplify calls later
    if signals is None:
        signals = ProgressSignals()

    def _save_trial():
        """Save trial in Nexus"""
        logger.debug('saving trial')
        vicon.SaveTrial(cfg.autoproc.nexus_timeout)

    def _context_desc(fpev):
        """Get Eclipse description string for given forceplate events dict"""
        s = ""
        nr = len(fpev['R_strikes'])
        if nr:
            s += '%dR' % nr
        nl = len(fpev['L_strikes'])
        if nr and nl:
            s += '/'
        if nl:
            s += '%dL' % nl
        return s or cfg.autoproc.enf_descriptions['context_none']

    def _fail(trial, reason):
        """Abort processing: mark and save trial"""
        fail_desc = (cfg.autoproc.enf_descriptions[reason] if reason in
                     cfg.autoproc.enf_descriptions else reason)
        logger.debug('preprocessing failed: %s' % fail_desc)
        trial['recon_ok'] = False
        trial['description'] = fail_desc
        _save_trial()

    def _range_to_roi(subj_pos, gait_dim, mov_range):
        """Try to determine ROI (in frames) from movement range"""
        subj_pos1 = subj_pos[:, [gait_dim]]
        # find non-gap frames where we are inside movement range
        dist_ok = np.where((subj_pos1 >= mov_range[0]) &
                           (subj_pos1 <= mov_range[1]) &
                           (subj_pos1 != 0.0))[0]
        if dist_ok.size > 0:
            return min(dist_ok), max(dist_ok)
        else:
            raise GaitDataError('no frames inside given range')

    # used to store stats about foot velocity
    foot_vel = {'L_strike': np.array([]), 'R_strike': np.array([]),
                'L_toeoff': np.array([]), 'R_toeoff': np.array([])}
    # 1st pass
    logger.debug('\n1st pass - processing %d trial(s)\n' % len(enffiles))
    trials = dict()

    vicon = nexus.viconnexus()
    nexus_ver = nexus.true_ver()
    logger.debug('detected Nexus version: %g' % nexus_ver)
    # close trial to prevent 'Save trial?' dialog on first open
    if nexus_ver >= 2.8:
        logger.debug('force closing open trial')
        vicon.CloseTrial(5000)  # timeout in ms

    for ind, enffile in enumerate(enffiles):
        filepath = enffile[:enffile.find('.Trial')]  # rm .TrialXXX and .enf
        filename = os.path.split(filepath)[1]
      
        signals.progress.emit('Preprocessing: %s' % filename, 100*ind/len(enffiles))
        if signals.canceled:
            return None

        trial = dict()
        trials[filepath] = trial
        logger.debug('loading in Nexus: %s' % filename)
        vicon.OpenTrial(filepath, cfg.autoproc.nexus_timeout)
        try:
            nexus.get_metadata(vicon)
        except GaitDataError:
            # may indicate broken or video-only trial
            logger.warning('cannot read metadata')
            trial['recon_ok'] = False
            trial['description'] = 'skipped'
            continue

        edata = eclipse.get_eclipse_keys(enffile, return_empty=True)
        logger.debug('type: %s' % edata['TYPE'])
        logger.debug('description: %s' % edata['DESCRIPTION'])
        logger.debug('notes: %s' % edata['NOTES'])
        eclipse_str = ''

        # check whether to skip trial
        if edata['TYPE'] in cfg.autoproc.type_skip:
            logger.debug('skipping based on type: %s' % edata['TYPE'])
            trial['recon_ok'] = False
            trial['description'] = 'skipped'
            continue
        skip = [s.upper() for s in cfg.autoproc.eclipse_skip]
        if (any([s in edata['DESCRIPTION'].upper() for s in skip]) or
           any([s in edata['NOTES'].upper() for s in skip])):
            logger.debug('skipping based on description/notes')
            # run preprocessing + save even for skipped trials, to mark
            # them as processed - mostly so that Eclipse export to Polygon
            # will work
            nexus.run_pipelines_multiprocessing(cfg.autoproc.pre_pipelines)
            _save_trial()
            trial['recon_ok'] = False
            trial['description'] = 'skipped'
            continue

        # try to run preprocessing pipelines
        nexus.run_pipelines_multiprocessing(cfg.autoproc.pre_pipelines)

        # check trial length
        trange = vicon.GetTrialRange()
        if (trange[1] - trange[0]) < cfg.autoproc.min_trial_duration:
            _fail(trial, 'short')
            continue

        # check for valid marker data
        allmarkers = nexus._get_marker_names(vicon, trajs_only=True)
        try:
            mkrdata = read_data.get_marker_data(vicon, allmarkers,
                                                ignore_missing=True)
        except GaitDataError:
            logger.debug('get_marker_data failed')
            _fail(trial, 'label_failure')
            continue

        # fail on any gaps in trial (off by default)
        gaps_found = False
        if cfg.autoproc.fail_on_gaps:
            for marker in set(allmarkers) - set(cfg.autoproc.ignore_markers):
                gaps = mkrdata[marker + '_gaps']
                if gaps.size > 0:
                    gaps_found = True
                    break
            if gaps_found:
                _fail(trial, 'gaps')
                continue

        # check for valid Plug-in Gait set
        if not utils.is_plugingait_set(mkrdata):
            logger.warning('marker set does not correspond to Plug-in Gait')
            _fail(trial, 'label_failure')
            continue
        # check for flipped markers
        flipped = list(utils._check_markers_flipped(mkrdata))
        if flipped:
            for m1, m2 in flipped:
                logger.warning('trying to swap trajectories for %s and %s'
                               % (m1, m2))
                nexus._swap_markers(vicon, m1, m2)

        # get subject position by tracking markers
        try:
            subj_pos = utils.avg_markerdata(mkrdata,
                                            cfg.autoproc.track_markers)
        except GaitDataError:
            logger.debug('gaps in tracking markers')
            _fail(trial, 'label_failure')
            continue
        gait_dim = utils.principal_movement_direction(subj_pos)
        # our roi (in frames) according to events_range (which is in lab coords)
        # this is not the same as Nexus ROI, which is unset at this point
        try:
            roi = _range_to_roi(subj_pos, gait_dim, cfg.autoproc.events_range)
            logger.debug('events range corresponds to frames %d-%d' % roi)
            trial['roi'] = roi
        except GaitDataError:
            _fail(trial, 'no_frames_in_range')
            continue

        if signals.canceled:
            return None

        # check forceplate data
        fp_info = (eclipse.eclipse_fp_keys(edata) if
                   cfg.autoproc.use_eclipse_fp_info else None)
        try:
            fpev = utils.detect_forceplate_events(vicon, mkrdata,
                                                  fp_info=fp_info, roi=roi)
        except GaitDataError:
            logger.warning('cannot determine forceplate events, possibly due '
                           'to gaps')
            _fail(trial, 'gaps')
            continue
        # get foot velocity info for all events (do not reduce to median)
        try:
            vel = utils.get_foot_contact_velocity(mkrdata, fpev, medians=False,
                                                  roi=roi)
        except GaitDataError:
            logger.warning('cannot determine foot velocity, possibly due to '
                           'gaps')
            _fail(trial, 'gaps')
            continue

        # preprocessing looks ok at this stage
        trial['recon_ok'] = True
        trial['mkrdata'] = mkrdata

        eclipse_str += _context_desc(fpev)
        valid = fpev['valid']
        trial['valid'] = valid
        trial['fpev'] = fpev

        if signals.canceled:
            return None

        # save velocity data
        for context in valid:
            nv = np.append(foot_vel[context+'_strike'], vel[context+'_strike'])
            foot_vel[context+'_strike'] = nv
            nv = np.append(foot_vel[context+'_toeoff'], vel[context+'_toeoff'])
            foot_vel[context+'_toeoff'] = nv
        eclipse_str += ','

        # main direction in lab frame (1,2,3 for x,y,z)
        inds_ok = np.where(np.any(subj_pos, axis=1))  # ignore gaps
        subj_pos_ = subj_pos[inds_ok]
        # +1/-1 for forward/backward (coord increase / decrease)
        gait_dir = np.median(np.diff(subj_pos_, axis=0), axis=0)[gait_dim]
        # write Eclipse key for direction
        if ('dir_forward' in cfg.autoproc.enf_descriptions and 'dir_backward'
           in cfg.autoproc.enf_descriptions):
            dir_str = 'dir_forward' if gait_dir > 0 else 'dir_backward'
            dir_desc = cfg.autoproc.enf_descriptions[dir_str]
            eclipse_str += '%s,' % dir_desc

        # compute gait velocity
        median_vel = utils._trial_median_velocity(vicon)
        logger.debug('median forward velocity: %.2f m/s' % median_vel)
        eclipse_str += '%.2f m/s' % median_vel

        _save_trial()
        trial['description'] = eclipse_str

        # write Eclipse fp values according to our detection
        fp_info = fpev['our_fp_info']
        if cfg.autoproc.write_eclipse_fp_info is True:
            logger.debug('writing detected forceplate info into Eclipse')
            eclipse.set_eclipse_keys(enffile, fp_info,
                                     update_existing=True)
        elif cfg.autoproc.write_eclipse_fp_info == 'reset':
            logger.debug('resetting Eclipse forceplate info')
            fp_info_auto = {k: 'Auto' for k, v in fp_info.items()}
            eclipse.set_eclipse_keys(enffile, fp_info_auto,
                                     update_existing=True)

    # all preprocessing done
    # compute velocity thresholds using all trials
    vel_th = {key: (np.median(x) if x.size > 0 else None) for key, x in
              foot_vel.items()}

    # 2nd pass
    sel_trials = {filepath: trial for filepath, trial in trials.items()
                  if trial['recon_ok']}
    logger.debug('\n2nd pass - processing %d trials\n' % len(sel_trials))

    for ind, (filepath, trial) in enumerate(sel_trials.items()):
        filename = os.path.split(filepath)[1]
        logger.debug('loading in Nexus: %s' % filename)
        vicon.OpenTrial(filepath, cfg.autoproc.nexus_timeout)
        enf_file = filepath + '.Trial.enf'

        signals.progress.emit('Marking events and running models: %s' % filename,
                              100*ind/len(sel_trials))
        if signals.canceled:
            return None

        # automark using global velocity thresholds
        try:
            vicon.ClearAllEvents()
            evs = utils.automark_events(vicon, vel_thresholds=vel_th,
                                        mkrdata=trial['mkrdata'],
                                        fp_events=trial['fpev'],
                                        events_range=cfg.autoproc.events_range,
                                        start_on_forceplate=cfg.autoproc.
                                        start_on_forceplate, roi=trial['roi'])
        except GaitDataError:  # cannot automark
            eclipse_str = '%s,%s' % (trial['description'],
                                     cfg.autoproc.enf_descriptions
                                     ['automark_failure'])
            logger.debug('automark failed')
            _save_trial()
            trial['description'] = eclipse_str
            continue  # next trial

        if signals.canceled:
            return None

        # crop trial around events
        if nexus_ver >= 2.5:
            evs_all = list(itertools.chain.from_iterable(evs.values()))
            if evs_all:
                # when setting roi, do not go beyond trial range
                minfr, maxfr = vicon.GetTrialRange()
                roistart = max(min(evs_all) - cfg.autoproc.crop_margin, minfr)
                roiend = min(max(evs_all) + cfg.autoproc.crop_margin, maxfr)
                # method cannot take numpy.int64
                vicon.SetTrialRegionOfInterest(int(roistart), int(roiend))

        # run model pipeline and save
        eclipse_str = '%s,%s' % (cfg.autoproc.enf_descriptions['ok'],
                                 trial['description'])
        nexus.run_pipelines_multiprocessing(cfg.autoproc.model_pipelines)
        _save_trial()
        trial['description'] = eclipse_str

    # all done; update Eclipse descriptions
    if cfg.autoproc.eclipse_write_key:
        # try to avoid a possible race condition where Nexus is still
        # holding the .enf file open
        time.sleep(.5)
        for filepath, trial in trials.items():
            enf_file = filepath + '.Trial.enf'
            try:
                eclipse.set_eclipse_keys(enf_file,
                                         {cfg.autoproc.eclipse_write_key:
                                          trial['description']},
                                         update_existing=True)
            except IOError:
                logger.warning('Could not write Eclipse description to %s' %
                               enf_file)
    else:
        logger.debug('not updating Eclipse data')

    # print stats
    logger.debug('Complete')
    logger.debug('Trials opened: %d' % len(trials))
    logger.debug('Trials with recon ok: %d' % len(sel_trials))


def _delete_c3ds(enffiles):
    """ c3d files need to be deleted before processing. Otherwise Nexus will
    load analog data from existing c3d files which are affected by previous
    crop operations, e.g. forceplate data might be clipped """
    logger.debug('deleting previous c3d files')
    c3dfiles = sessionutils._filter_to_c3ds(enffiles)
    for enffile, c3dfile in zip(enffiles, c3dfiles):
        if not op.isfile(c3dfile):
            continue
        edata = eclipse.get_eclipse_keys(enffile, return_empty=True)
        # do not delete static .c3d files (needed for dynamic processing)
        if edata['TYPE'] == 'Static':
            logger.debug('keeping static c3d file %s' % c3dfile)
            continue

        # to prevent data loss, do not delete c3d if original
        # x1d and x2d do not exist
        x1dfile = sessionutils._enf2other(enffile, 'x1d')
        x2dfile = sessionutils._enf2other(enffile, 'x2d')
        if (op.isfile(x1dfile) and op.isfile(x2dfile)):
            logger.debug('deleting existing c3d file %s' % c3dfile)
            os.remove(c3dfile)
        else:
            logger.debug('refusing to delete c3d file %s since original '
                         'data files .(x1d and .x2d) do not exist' % c3dfile)


def autoproc_session(patterns=None, signals=None):
    sessionpath = nexus.get_sessionpath()
    enffiles = list(sessionutils.get_session_enfs(sessionpath))

    if not enffiles:
        raise GaitDataError('No trials found (no .enf files in session)')

    _delete_c3ds(enffiles)

    if patterns:
        # filter trial names according to patterns
        enffiles = [s for s in enffiles if any([p in s for p in patterns])]
    if enffiles:
        _do_autoproc(enffiles, signals=signals)


def autoproc_trial(signals=None):
    fn = nexus.get_trialname()
    if not fn:
        raise GaitDataError('No trial open in Nexus')
    fn += '.Trial.enf'
    enffiles = [op.join(nexus.get_sessionpath(), fn)]  # listify single enf
    _delete_c3ds(enffiles)
    _do_autoproc(enffiles, signals=signals)


def automark_trial(plot=False):

    vicon = nexus.viconnexus()
    roi = vicon.GetTrialRegionOfInterest()
    vicon.ClearAllEvents()

    foot_markers = (cfg.autoproc.left_foot_markers +
                    cfg.autoproc.right_foot_markers)
    mkrs = foot_markers + utils._pig_pelvis_markers()
    mkrdata = read_data.get_marker_data(vicon, mkrs, ignore_missing=True)
    fpe = utils.detect_forceplate_events(vicon, mkrdata, roi=roi)
    vel = utils.get_foot_contact_velocity(mkrdata, fpe, roi=roi)
    utils.automark_events(vicon, vel_thresholds=vel, fp_events=fpe, roi=roi,
                          plot=plot)


def copy_session_videos():
    """Copy Nexus session videos to desktop"""
    nexus.check_nexus()

    dest_dir = op.join(op.expanduser('~'), 'Desktop', 'nexus_videos')
    if not op.isdir(dest_dir):
        os.mkdir(dest_dir)

    sessionpath = nexus.get_sessionpath()
    c3dfiles = sessionutils.get_c3ds(sessionpath, tags=cfg.eclipse.repr_tags,
                                     trial_type='dynamic')
    vidfiles = itertools.chain.from_iterable(sessionutils.get_trial_videos(c3d)
                                             for c3d in c3dfiles)
    vidfiles = list(vidfiles)
    if not vidfiles:
        raise GaitDataError('No video files found for representative trials')

    for vidfile in vidfiles:
        logger.debug('copying %s -> %s' % (vidfile, dest_dir))
        shutil.copy2(vidfile, dest_dir)
