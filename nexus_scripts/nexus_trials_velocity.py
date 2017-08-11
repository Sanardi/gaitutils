# -*- coding: utf-8 -*-
"""
Created on Wed Jun 21 13:48:27 2017

@author: HUS20664877
"""

from gaitutils import nexus, cfg, utils, read_data, eclipse
from gaitutils.exceptions import GaitDataError
import numpy as np
import matplotlib.pyplot as plt
import os.path as op


def trial_median_velocity(source):
    MIN_VEL = .1
    try:
        frate = read_data.get_metadata(source)['framerate']
        dim = utils.principal_movement_direction(source, cfg.autoproc.
                                                 track_markers)
        mkr = cfg.autoproc.track_markers[0]
        vel_ = read_data.get_marker_data(source, mkr)[mkr+'_V'][:, dim]
    except (GaitDataError, ValueError):
        return np.nan
    vel = np.median(np.abs(vel_[np.where(vel_)]))
    vel_ms = vel * frate / 1000.
    return vel_ms if vel_ms >= MIN_VEL else np.nan


def do_plot():

    enfs = nexus.get_trial_enfs()
    if enfs is None:
        raise Exception('Cannot read session from Nexus (maybe in live mode?)')
    enfs_ = [enf for enf in enfs if
             eclipse.get_eclipse_keys(enf,
                                      return_empty=True)['TYPE'] == 'Dynamic']
    c3ds = [nexus.enf2c3d(enf) for enf in enfs_]
    labels = [op.splitext(op.split(file)[1])[0] for file in c3ds]
    vels = np.array([trial_median_velocity(trial) for trial in c3ds])
    vavg = np.nanmean(vels)

    plt.figure()
    plt.stem(vels)
    plt.xticks(range(len(vels)), labels, rotation='vertical')
    plt.ylabel('Velocity (m/s)')
    plt.tick_params(axis='both', which='major', labelsize=8)
    plt.title('Gait velocity for dynamic trials (average %.2f m/s)' % vavg)
    plt.tight_layout()
    plt.show()

if __name__ == '__main__':
    do_plot()