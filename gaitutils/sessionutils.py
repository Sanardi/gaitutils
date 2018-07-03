# -*- coding: utf-8 -*-
"""
Session related functions


@author: Jussi (jnu@iki.fi)
"""

import io
import os.path as op
import json
import datetime
import glob
import logging

from .eclipse import get_eclipse_keys
from .envutils import GaitDataError
from .config import cfg


logger = logging.getLogger(__name__)

json_keys = ['fullname', 'hetu', 'session_description', 'report_notes']


def default_info():
    """Return info dict with placeholder values"""
    return {key: None for key in json_keys}


def load_info(session):
    """Return the patient info dict from the given session"""
    fname = op.join(session, 'patient_info.json')
    if op.isfile(fname):
        with io.open(fname, 'r', encoding='utf-8') as f:
            try:
                info = json.load(f)
                extra_keys = set(info.keys()) - set(json_keys)
                if extra_keys:
                    logger.warning('Extra keys %s in patient info file %s'
                                   % (extra_keys, fname))
                    for key in extra_keys:
                        info.pop(key)
                missing_keys = set(json_keys) - set(info.keys())
                if missing_keys:
                    logger.warning('Missing keys %s in patient info file %s'
                                   % (missing_keys, fname))
                    # supply default values for missing keys
                    for key in missing_keys:
                        info[key] = default_info()[key]
            except (UnicodeDecodeError, EOFError, IOError, TypeError,
                    ValueError):
                raise GaitDataError('Error loading patient info file %s'
                                    % fname)
    else:
        info = None

    return info


def save_info(session, patient_info):
    """Save patient info."""
    fname = op.join(session, 'patient_info.json')
    try:
        with io.open(fname, 'w', encoding='utf-8') as f:
            f.write(unicode(json.dumps(patient_info, ensure_ascii=False)))
    except (UnicodeDecodeError, EOFError, IOError, TypeError):
        raise GaitDataError('Error saving patient info file %s ' % fname)


def _merge_session_info(sessions):
    """merge patient info files across sessions. fullname and hetu must match.
    Returns dict of individual session infos and the mergeD info"""
    session_infos = {session: (load_info(session) or default_info())
                     for session in sessions}
    info = default_info()
    # ignore the session description
    for key in ['fullname', 'hetu', 'report_notes']:
        allvals = set([session_infos[session][key] for session in sessions])
        if None in allvals:
            allvals.remove(None)
        if key == 'fullname' or key == 'hetu':
            if len(allvals) > 1:
                logger.warning('name / hetu do not match across sessions')
                return session_infos, None
        if key == 'report_notes':
            if len(allvals) > 1:
                logger.warning('report notes do not match across sessions')
        # in case of conflicts, this just picks from the last session
        info[key] = allvals.pop() if allvals else None
    return session_infos, info


def _enf2other(fname, ext):
    """Converts name of trial .enf file to corresponding .c3d or other
    file type"""
    enfstr = '.Trial.enf'
    if enfstr not in fname:
        raise ValueError('Filename is not a trial .enf')
    return fname.replace(enfstr, '.%s' % ext)


def get_session_date(sessionpath):
    """Return date when session was recorded (datetime.datetime object)"""
    enfs = get_session_enfs(sessionpath)
    x1ds = [_enf2other(fn, 'x1d') for fn in enfs]
    if not x1ds:
        raise ValueError('No .x1d files for given session')
    return datetime.datetime.fromtimestamp(op.getmtime(x1ds[0]))


def get_session_enfs(sessionpath):
    """Return list of .enf files for the session """
    enfglob = op.join(sessionpath, '*Trial*.enf')
    enffiles = glob.glob(enfglob) if sessionpath else None
    logger.debug('found %d .enf files for session %s' %
                 (len(enffiles) if enffiles else 0, sessionpath))
    return enffiles


def find_tagged(sessionpath, tags=None, eclipse_keys=None):
    """ Find tagged trials in given path. Returns a list of .c3d files. """

    eclipse_keys = eclipse_keys or cfg.eclipse.tag_keys
    tags = tags or cfg.eclipse.tags

    tagged_enfs = list(_find_enfs(sessionpath, tags, eclipse_keys))
    return [_enf2other(fn, 'c3d') for fn in tagged_enfs]


def _find_enfs(sessionpath, tags, eclipse_keys):
    """ Yield .enf files for trials in current Nexus session directory
    (or given session path) whose Eclipse fields (list) contain any of
    strings (list). Case insensitive. """
    tags = [t.upper() for t in tags]
    for enf in get_session_enfs(sessionpath):
        ecldi = get_eclipse_keys(enf).items()
        eclvals = [val.upper() for key, val in ecldi if key in eclipse_keys]
        if any([s in val for s in tags for val in eclvals]):
            yield enf
