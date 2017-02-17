# -*- coding: utf-8 -*-
"""

c3d reader functions


@author: Jussi (jnu@iki.fi)
"""

from __future__ import print_function
import logging
import numpy as np
import os
from numutils import cop
logger = logging.getLogger(__name__)
try:
    import btk
except ImportError:
    print('Cannot find btk module; unable to read .c3d files')


def is_c3dfile(obj):
    """ Check whether obj is a valid c3d file. Currently just checks
    existence. """
    try:
        return os.path.isfile(obj)
    except TypeError:
        return False


def get_emg_data(c3dfile):
    """ Read EMG data from a c3d file. """
    reader = btk.btkAcquisitionFileReader()
    reader.SetFilename(str(c3dfile))
    reader.Update()
    acq = reader.GetOutput()
    data = dict()
    elnames = []
    for i in btk.Iterate(acq.GetAnalogs()):
        if i.GetDescription().find('EMG') >= 0 and i.GetUnit() == 'V':
            elname = i.GetLabel()
            elnames.append(elname)
            data[elname] = np.squeeze(i.GetValues())
    if elnames:
        return {'t': np.arange(len(data[elname])) / acq.GetAnalogFrequency(),
                'data': data}
    else:
        raise Exception('No EMG channels found in data')


def get_marker_data(c3dfile, markers):
    if not isinstance(markers, list):  # listify if not already a list
        markers = [markers]
    reader = btk.btkAcquisitionFileReader()
    reader.SetFilename(str(c3dfile))
    reader.Update()
    acq = reader.GetOutput()
    mdata = dict()
    for marker in markers:
        try:
            mP = np.squeeze(acq.GetPoint(marker).GetValues())
        except RuntimeError:
            raise Exception('Cannot read variable %s from c3d file' % marker)
        mdata[marker + '_P'] = mP
        mdata[marker + '_V'] = np.gradient(mP)[0]
        mdata[marker + '_A'] = np.gradient(mdata[marker+'_V'])[0]
        # find gaps
        allzero = np.logical_and(mP[:, 0] == 0, mP[:, 1] == 0, mP[:, 2] == 0)
        mdata[marker + '_gaps'] = np.where(allzero)[0]
    return mdata


def get_metadata(c3dfile):
    """ Read trial and subject metadata """
    trialname = os.path.basename(os.path.splitext(c3dfile)[0])
    sessionpath = os.path.dirname(c3dfile)
    reader = btk.btkAcquisitionFileReader()
    reader.SetFilename(str(c3dfile))  # check existence?
    reader.Update()
    acq = reader.GetOutput()
    # frame offset (start of trial data in frames)
    offset = acq.GetFirstFrame()
    lastfr = acq.GetLastFrame()
    length = lastfr - offset + 1
    framerate = acq.GetPointFrequency()
    analograte = acq.GetAnalogFrequency()
    samplesperframe = acq.GetNumberAnalogSamplePerFrame()
    # count forceplates
    n_forceplates = 0
    for i in btk.Iterate(acq.GetAnalogs()):
        desc = i.GetLabel()
        if desc.find('Force.') >= 0 and i.GetUnit() == 'N':
            n_forceplates += 1
    #  get events
    rstrikes, lstrikes, rtoeoffs, ltoeoffs = [], [], [], []
    for i in btk.Iterate(acq.GetEvents()):
        if i.GetLabel() == "Foot Strike":
            if i.GetContext() == "Right":
                rstrikes.append(i.GetFrame())
            elif i.GetContext() == "Left":
                lstrikes.append(i.GetFrame())
            else:
                raise ValueError("Unknown context on foot strike event")
        elif i.GetLabel() == "Foot Off":
            if i.GetContext() == "Right":
                rtoeoffs.append(i.GetFrame())
            elif i.GetContext() == "Left":
                ltoeoffs.append(i.GetFrame())
            else:
                raise ValueError("Unknown context on foot strike event")
    # get subject info
    metadata = acq.GetMetaData()
    # don't ask
    name = (metadata.FindChild("SUBJECTS").value().
            FindChild("NAMES").value().GetInfo().ToString()[0].strip())
    bodymass = (metadata.FindChild("PROCESSING").value().
                FindChild("Bodymass").value().GetInfo().ToDouble()[0])
    # sort events (may be in wrong temporal order, at least in c3d files)
    for li in [lstrikes, rstrikes, ltoeoffs, rtoeoffs]:
        li.sort()
    return {'trialname': trialname, 'sessionpath': sessionpath,
            'offset': offset, 'framerate': framerate, 'analograte': analograte,
            'name': name, 'bodymass': bodymass, 'lstrikes': lstrikes,
            'rstrikes': rstrikes, 'ltoeoffs': ltoeoffs, 'rtoeoffs': rtoeoffs,
            'length': length, 'samplesperframe': samplesperframe,
            'n_forceplates': n_forceplates}


def get_model_data(c3dfile, model):
    modeldata = dict()
    reader = btk.btkAcquisitionFileReader()
    reader.SetFilename(str(c3dfile))
    reader.Update()
    acq = reader.GetOutput()
    for var in model.read_vars:
        try:
            vals = acq.GetPoint(var).GetValues()
            modeldata[var] = np.transpose(np.squeeze(vals))
        except RuntimeError:
            raise ValueError('Cannot find model variable %s in c3d file' %
                             var)
        # c3d stores scalars as last dim of 3-d array
        if model.read_strategy == 'last':
            modeldata[var] = modeldata[var][2, :]
    return modeldata


def get_forceplate_data(c3dfile):
    """ Read forceplate data. Does not support multiple plates yet.
    Force results differ somewhat from Nexus, not sure why. Calibration? """
    reader = btk.btkAcquisitionFileReader()
    reader.SetFilename(str(c3dfile))  # btk does not tolerate unicode
    reader.Update()
    acq = reader.GetOutput()
    frame1 = acq.GetFirstFrame()  # start of ROI (1-based)
    samplesperframe = acq.GetNumberAnalogSamplePerFrame()
    sfrate = acq.GetAnalogFrequency()
    # TODO: raise DeviceNotFound if needed
    fx, fy, fz, mx, my, mz = (None,) * 6
    for i in btk.Iterate(acq.GetAnalogs()):
        desc = i.GetLabel()
        if desc.find('Force.') >= 0 and i.GetUnit() == 'N':
            if desc.find('Fx') > 0:
                fx = np.squeeze(i.GetValues())  # rm singleton dimension
            elif desc.find('Fy') > 0:
                fy = np.squeeze(i.GetValues())
            elif desc.find('Fz') > 0:
                fz = np.squeeze(i.GetValues())
        elif desc.find('Moment.') >= 0 and i.GetUnit() == 'Nmm':
            if desc.find('Mx') > 0:
                mx = np.squeeze(i.GetValues())  # rm singleton dimension
            elif desc.find('My') > 0:
                my = np.squeeze(i.GetValues())
            elif desc.find('Mz') > 0:
                mz = np.squeeze(i.GetValues())
    if any([var is None for var in (fx, fy, fz, mx, my, mz)]):
        raise ValueError('Cannot read force/moment variable')
    # c3d does not seem to include center of pressure data, so we compute it
    F = np.array([fx, fy, fz]).transpose()
    Ftot = np.sqrt(np.sum(F**2, axis=1))
    M = np.array([mx, my, mz]).transpose()
    CoP = cop(F, M)
    return {'F': F, 'Ftot': Ftot, 'CoP': CoP,
            'samplesperframe': samplesperframe, 'analograte': sfrate}
