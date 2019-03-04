#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Created on Thu Sep 03 14:54:34 2015

Copy trial videos to desktop under nexus_videos

@author: Jussi (jnu@iki.fi)
"""

import os
import os.path as op
import shutil
import itertools
import logging

from gaitutils import nexus, sessionutils, cfg, GaitDataError


logger = logging.getLogger(__name__)


def do_copy():

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


if __name__ == '__main__':
    logging.basicConfig(level=logging.DEBUG)
    do_copy()