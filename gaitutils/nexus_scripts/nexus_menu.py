#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Show a Qt menu for running various Nexus tasks.

@author: Jussi (jnu@iki.fi)
"""

from __future__ import print_function
from builtins import str
from builtins import range
from PyQt5 import QtGui, QtCore, uic, QtWidgets
from PyQt5.QtCore import QRunnable, QThreadPool, pyqtSignal, QObject
from pkg_resources import resource_filename
from functools import partial
import sys
import ast
import os.path as op
import os
import subprocess
import time
import requests
import logging
import traceback
import itertools

from gaitutils.numutils import check_hetu
from gaitutils.guiutils import (qt_message_dialog, qt_yesno_dialog,
                                qt_dir_chooser)
from gaitutils import (GaitDataError, nexus, cfg, report, sessionutils, videos,
                       envutils)
from gaitutils.nexus_scripts import (nexus_plot,
                                     nexus_emg_consistency,
                                     nexus_kin_consistency,
                                     nexus_musclelen_consistency,
                                     nexus_autoprocess_trial,
                                     nexus_autoprocess_session,
                                     nexus_tardieu,
                                     nexus_copy_trial_videos,
                                     nexus_trials_velocity,
                                     nexus_make_pdf_report,
                                     nexus_make_comparison_report,
                                     nexus_kin_average,
                                     nexus_automark_trial,
                                     nexus_time_distance_vars)


logger = logging.getLogger(__name__)


def _exception_msg(e):
    """Return text representation of exception e"""
    # for our own error class, we know that a neat message is there
    if isinstance(e, GaitDataError):
        err_msg = e.message
    else:  # otherwise, we have no idea, so use generic repr()
        err_msg = repr(e)
    return 'There was an error running the operation. Details:\n%s' % err_msg


class PdfReportDialog(QtWidgets.QDialog):
    """Ask for patient/session info and report options"""

    def __init__(self, info, prompt='Hello', parent=None):
        super(self.__class__, self).__init__()
        uifile = resource_filename('gaitutils',
                                   'nexus_scripts/pdf_report_dialog.ui')
        uic.loadUi(uifile, self)
        self.prompt.setText(prompt)
        if info is not None:
            if info['fullname'] is not None:
                self.lnFullName.setText(info['fullname'])
            if info['hetu'] is not None:
                self.lnHetu.setText(info['hetu'])
            if info['session_description'] is not None:
                self.lnDescription.setText(info['session_description'])

    def accept(self):
        """ Update config and close dialog, if widget inputs are ok. Otherwise
        show an error dialog """
        self.hetu = self.lnHetu.text()
        self.fullname = self.lnFullName.text()
        self.session_description = self.lnDescription.text()
        # get all the report page selections
        self.pages = dict()
        for w in self.findChildren(QtWidgets.QWidget):
            wname = w.objectName()
            if wname[:2] == 'cb':
                self.pages[wname[2:]] = w.checkState()
        if self.fullname and check_hetu(self.hetu):
            self.done(QtWidgets.QDialog.Accepted)  # or call superclass accept
        else:
            qt_message_dialog('Please enter a valid name and hetu')


class WebReportInfoDialog(QtWidgets.QDialog):
    """Ask for patient info"""

    def __init__(self, info, parent=None, check_info=True):
        super(self.__class__, self).__init__()
        uifile = resource_filename('gaitutils',
                                   'nexus_scripts/web_report_info.ui')
        uic.loadUi(uifile, self)
        self.check_info = check_info
        if info is not None:
            if info['fullname'] is not None:
                self.lnFullName.setText(info['fullname'])
            if info['hetu'] is not None:
                self.lnHetu.setText(info['hetu'])
            if info['report_notes'] is not None:
                self.txtNotes.setPlainText(info['report_notes'])

    def accept(self):
        """ Update config and close dialog, if widget inputs are ok. Otherwise
        show an error dialog """
        self.hetu = self.lnHetu.text().strip()
        self.fullname = self.lnFullName.text().strip()
        self.report_notes = str(self.txtNotes.toPlainText()).strip()
        if self.check_info:
            ok = self.fullname and check_hetu(self.hetu)
        else:
            ok = not self.hetu or check_hetu(self.hetu)
        if ok:
            self.done(QtWidgets.QDialog.Accepted)  # or call superclass accept
        else:
            msg = 'Please enter a valid name and hetu'
            if not self.check_info:
                msg += ' (or leave empty)'
            qt_message_dialog(msg)


class WebReportDialog(QtWidgets.QDialog):
    """Dialog for managing web reports"""

    def __init__(self, parent):
        super(self.__class__, self).__init__()
        # load user interface made with designer
        uifile = resource_filename('gaitutils',
                                   'nexus_scripts/web_report_dialog.ui')
        uic.loadUi(uifile, self)
        self.btnCreateReport.clicked.connect(self._create_web_report)
        self.btnDeleteReport.clicked.connect(self._delete_current_report)
        self.btnDeleteAllReports.clicked.connect(self._delete_all_reports)
        self.btnViewReport.clicked.connect(self._view_current_report)
        # add double click action to browse current report
        (self.listActiveReports.itemDoubleClicked.
         connect(lambda item: parent._browse_localhost(item.userdata)))
        # these require active reports to be enabled
        self.reportWidgets = [self.btnDeleteReport, self.btnDeleteAllReports,
                              self.btnViewReport]
        self._set_report_button_status()

    def _create_web_report(self):
        """Collect sessions, create the dash app, start it and launch a
        web browser on localhost on the correct port"""

        if self.listActiveReports.count() == cfg.web_report.max_reports:
            qt_message_dialog('Maximum number of active web reports active. '
                              'Please delete some reports first.')
            return

        dlg = ChooseSessionsDialog()
        if not dlg.exec_():
            return
        sessions = dlg.sessions

        sessions_str = '/'.join([op.split(s)[-1] for s in sessions])
        report_type = ('single session' if len(sessions) == 1
                       else 'comparison')
        report_name = '%s: %s' % (report_type, sessions_str)
        existing_names = [item.text for item in self.listActiveReports.items]
        if report_name in existing_names:
            qt_message_dialog('There is already a report for %s' %
                              report_name)
            return

        session_infos, info = sessionutils._merge_session_info(sessions)
        if info is None:
            qt_message_dialog('Patient files do not match. Sessions may be '
                              'from different patients. Continuing without '
                              'patient info.')
            info = sessionutils.default_info()
        else:
            dlg_info = WebReportInfoDialog(info, check_info=False)
            if dlg_info.exec_():
                new_info = dict(hetu=dlg_info.hetu, fullname=dlg_info.fullname,
                                report_notes=dlg_info.report_notes)
                info.update(new_info)

                # update info files (except session specific keys)
                for session in sessions:
                    update_dict = dict(report_notes=dlg_info.report_notes,
                                       fullname=dlg_info.fullname,
                                       hetu=dlg_info.hetu)
                    session_infos[session].update(update_dict)
                    sessionutils.save_info(session, session_infos[session])
            else:
                return

        prog = ProgressBar('Creating web report...')
        prog.update('Collecting session information...', 0)
        signals = ProgressSignals()
        signals.progress.connect(lambda text, p: prog.update(text, p))

        # for comparison between sessions, get representative trials only
        tags = (cfg.eclipse.repr_tags if len(sessions) > 1 else
                cfg.eclipse.tags)

        # collect all video files for conversion
        # includes tagged dynamic, video-only tagged, and static trials
        vidfiles = list()
        for session in sessions:
            vids = self._collect_videos_to_convert(session, tags=tags)
            vidfiles.extend(vids)

        if not report.convert_videos(vidfiles, check_only=True):
            self._convert_vidfiles(vidfiles, signals)

        self._report_creation_status = None
        self._execute(report.dash_report, thread=True, block_ui=True,
                      finished_func=self._web_report_finished,
                      info=info, sessions=sessions, tags=tags, signals=signals)

        # wait for report creation thread to complete
        while self._report_creation_status is None:
            time.sleep(.05)
            QtWidgets.QApplication.processEvents()
        prog.close()

        if self._report_creation_status is False:
            qt_message_dialog('Could not create report, check that session is '
                              'valid')
            return
        app = self._report_creation_status

        # figure out first free TCP port
        ports_taken = [item.userdata for item in self.listActiveReports.items]
        port = cfg.web_report.tcp_port
        while port in ports_taken:  # find first port not taken by us
            port += 1

        # report ok - start server in a thread
        # also enable the threaded mode of the server. serving is a bit flaky
        # in Python 2 (multiple requests cause exceptions)
        self._execute(app.server.run, thread=True, block_ui=False,
                      debug=False, port=port, threaded=True)
        # double clicking on the list item will browse to corresponding port
        self.listActiveReports.add_item(report_name, data=port)
        logger.debug('starting web browser')
        self._browse_localhost(port)

        logger.debug('%d thread(s) now active (%d max)'
                     % (self.threadpool.activeThreadCount(),
                        self.threadpool.maxThreadCount()))

    def _delete_report(self, item):
        """Shut down server for given list item, remove item"""
        port = item.userdata
        # compose url for shutdown request - see report.py
        url = 'http://127.0.0.1:%d/shutdown' % port
        # we have to make sure that localhost is not proxied
        proxies = {"http": None, "https": None}
        logger.debug('requesting server shutdown for port %d' % port)
        requests.get(url, proxies=proxies)
        self.listActiveReports.rm_current_item()

    def _delete_current_report(self):
        """Shut down server for current item, remove item"""
        item = self.listActiveReports.currentItem()
        if item is None:
            return
        msg = 'Are you sure you want to delete the report for %s?' % item.text
        reply = qt_yesno_dialog(msg)
        if reply == QtWidgets.QMessageBox.YesRole:
            self._delete_report(item)
        self._set_report_button_status()

    def _delete_all_reports(self):
        """Delete all web reports"""
        if self.listActiveReports.count() == 0:
            return
        msg = 'Are you sure you want to delete all reports?'
        reply = qt_yesno_dialog(msg)
        if reply != QtWidgets.QMessageBox.YesRole:
            return
        # cannot use generator here since the loop changes the items
        for item in list(self.listActiveReports.items):
            self._delete_report(item)
        self._set_report_button_status()

    def _view_current_report(self):
        """Open current report in browser"""
        item = self.listActiveReports.currentItem()
        if item is None:
            return
        port = item.userdata
        self._browse_localhost(port)

    def _set_report_button_status(self):
        """Enable report buttons if active reports, otherwise disable them"""
        status = bool(self.listActiveReports.count())
        for widget in self.reportWidgets:
            widget.setEnabled(status)

    def _web_report_finished(self, app):
        """Gets called when web report creation is finished"""
        logger.debug('report creation finished')
        self._enable_op_buttons(None)
        self._report_creation_status = app




class ChooseSessionsDialog(QtWidgets.QDialog):
    """Display a dialog for picking sessions"""

    def __init__(self, min_sessions=1, max_sessions=3):
        super(self.__class__, self).__init__()
        # load user interface made with designer
        uifile = resource_filename('gaitutils',
                                   'nexus_scripts/web_report_sessions.ui')
        uic.loadUi(uifile, self)
        self.btnBrowseSession.clicked.connect(self.add_session)
        self.btnAddNexusSession.clicked.connect(lambda: self.
                                                add_session(from_nexus=True))
        self.btnClearAll.clicked.connect(self.listSessions.clear)
        self.btnClearCurrent.clicked.connect(self.listSessions.rm_current_item)
        self.max_sessions = max_sessions
        self.min_sessions = min_sessions

    def add_session(self, from_nexus=False):
        if len(self.sessions) == self.max_sessions:
            qt_message_dialog('You can specify maximum of %d sessions' %
                              self.max_sessions)
            return
        if from_nexus:
            try:
                dirs = [nexus.get_sessionpath()]
            except GaitDataError as e:
                qt_message_dialog(_exception_msg(e))
                return
        else:
            dirs = qt_dir_chooser()
        if dirs:
            for dir_ in dirs:
                if dir_ in self.sessions:
                    qt_message_dialog('Session %s already loaded' % dir_)
                elif dir_:
                    self.listSessions.add_item(dir_, data=dir_)

    @property
    def sessions(self):
        return [item.userdata for item in self.listSessions.items]

    def accept(self):
        if len(self.sessions) < self.min_sessions:
            qt_message_dialog('Please select at least %d session%s' %
                              (self.min_sessions,
                               's' if self.min_sessions > 1 else ''))
        else:
            self.done(QtWidgets.QDialog.Accepted)


class QtHandler(logging.Handler):

    def __init__(self):
        logging.Handler.__init__(self)

    def emit(self, record):
        record = self.format(record)
        if record:
            XStream.stdout().write('%s\n' % record)


class XStream(QtCore.QObject):
    _stdout = None
    _stderr = None
    messageWritten = QtCore.pyqtSignal(str)

    def flush(self):
        pass

    def fileno(self):
        return -1

    def write(self, msg):
        if not self.signalsBlocked():
            self.messageWritten.emit(str(msg))

    @staticmethod
    def stdout():
        if not XStream._stdout:
            XStream._stdout = XStream()
            sys.stdout = XStream._stdout  # also capture stdout
        return XStream._stdout

    @staticmethod
    def stderr():
        if not XStream._stderr:
            XStream._stderr = XStream()
            sys.stderr = XStream._stderr  # ... and stderr
        return XStream._stderr


class OptionsDialog(QtWidgets.QDialog):
    """ Display a tabbed dialog for changing gaitutils options """

    def __init__(self, parent, default_tab=0):
        super(self.__class__, self).__init__(parent)
        # load user interface made with designer
        uifile = resource_filename('gaitutils',
                                   'nexus_scripts/options_dialog.ui')
        uic.loadUi(uifile, self)
        self.setAttribute(QtCore.Qt.WA_DeleteOnClose)

        # add some buttons to the standard button box
        loadButton = QtWidgets.QPushButton('Load...')
        self.buttonBox.addButton(loadButton,
                                 QtWidgets.QDialogButtonBox.ActionRole)
        loadButton.clicked.connect(self.load_config_dialog)
        saveButton = QtWidgets.QPushButton('Save...')
        self.buttonBox.addButton(saveButton,
                                 QtWidgets.QDialogButtonBox.ActionRole)
        saveButton.clicked.connect(self.save_config_dialog)

        # show page
        self.tabWidget.setCurrentIndex(default_tab)

        """ Collect config widgets into a dict of dict. First key is tab
        (same as config category, e.g. autoproc), second key is widget name """
        self.cfg_widgets = dict()
        for page in [self.tabWidget.widget(n) for n in
                     range(self.tabWidget.count())]:
            pname = page.objectName()
            self.cfg_widgets[pname] = dict()
            for w in page.findChildren(QtWidgets.QWidget):
                wname = w.objectName()
                if wname[:4] == 'cfg_':  # config widgets are specially named
                    self.cfg_widgets[pname][wname] = w
        self._update_widgets()
        self.homedir = os.path.expanduser('~')

    def load_config_dialog(self):
        """ Bring up load dialog and load selected file. """
        global cfg
        fout = QtWidgets.QFileDialog.getOpenFileName(self,
                                                     'Load config file',
                                                     self.homedir,
                                                     'Config files (*.cfg)')
        # TODO : filedialog set filter -> PyQt5.QtCore.QDir.Hidden?
        fname = fout[0]
        if fname:
            # TODO: check for errors on config read
            # cfg.load_default()  TODO: load defaults before loading cfg file?
            cfg.read(fname)
            self._update_widgets()

    def save_config_dialog(self):
        """ Bring up save dialog and save data. """
        global cfg
        res, txt = self._check_widget_inputs()
        if not res:
            qt_message_dialog('Invalid input: %s\nPlease fix before saving'
                              % txt)
        else:
            fout = QtWidgets.QFileDialog.getSaveFileName(self,
                                                         'Save config file',
                                                         self.homedir,
                                                         'Config files '
                                                         '(*.cfg)')
            fname = fout[0]
            if fname:
                self.update_cfg()
                cfg.write_file(fname)

    def _update_widgets(self):
        """ Update config widgets according to current cfg """
        for section in self.cfg_widgets:
            for wname, widget in self.cfg_widgets[section].items():
                item = wname[4:]
                cfgval = getattr(getattr(cfg, section), item)
                if str(cfgval) != str(self._getval(widget)):
                    self._setval(widget, cfgval)  # set using native type
                if isinstance(widget, QtWidgets.QLineEdit):
                    widget.setCursorPosition(0)  # show beginning of line

    def _check_widget_inputs(self):
        """ Check widget inputs. Currently only QLineEdits are checked for
        eval - ability """
        for section in self.cfg_widgets:
            for widget in self.cfg_widgets[section].values():
                if isinstance(widget, QtWidgets.QLineEdit):
                    txt = widget.text()
                    try:
                        ast.literal_eval(txt)
                    except (SyntaxError, ValueError):
                        return (False, txt)
        return (True, '')

    def _getval(self, widget):
        """ Universal value getter that takes any type of config widget.
        Returns native types, except QLineEdit input is auto-evaluated """
        if (isinstance(widget, QtWidgets.QSpinBox) or
           isinstance(widget, QtWidgets.QDoubleSpinBox)):
            return widget.value()
        elif isinstance(widget, QtWidgets.QCheckBox):
            return bool(widget.checkState())
        elif isinstance(widget, QtWidgets.QComboBox):
            # convert to str to avoid writing out unicode repr() into config
            # files unnecessarily
            return str(widget.currentText())
        elif isinstance(widget, QtWidgets.QLineEdit):
            # Directly eval lineEdit contents. This means that string vars
            # must be quoted in the lineEdit.
            txt = widget.text()
            return ast.literal_eval(txt) if txt else None
        else:
            raise Exception('Unhandled type of config widget')

    def _setval(self, widget, val):
        """ Universal value setter that takes any type of config widget.
        val must match widget type, except for QLineEdit that can take
        any type, which will be converted to its repr """
        if (isinstance(widget, QtWidgets.QSpinBox) or
           isinstance(widget, QtWidgets.QDoubleSpinBox)):
            widget.setValue(val)
        elif isinstance(widget, QtWidgets.QCheckBox):
            widget.setCheckState(2 if val else 0)
        elif isinstance(widget, QtWidgets.QComboBox):
            idx = widget.findText(val)
            if idx >= 0:
                widget.setCurrentIndex(idx)
            else:
                raise ValueError('Tried to set combobox to invalid value.')
        elif isinstance(widget, QtWidgets.QLineEdit):
            widget.setText(repr(val))
        else:
            raise Exception('Unhandled type of config widget')

    def update_cfg(self):
        """ Update cfg according to current dialog settings """
        global cfg
        for section in self.cfg_widgets.keys():
            for wname, widget in self.cfg_widgets[section].items():
                item = wname[4:]
                widgetval = self._getval(widget)
                cfgval = getattr(getattr(cfg, section), item)
                if widgetval != cfgval:
                    cfg[section][item] = repr(widgetval)

    def accept(self):
        """ Update config and close dialog, if widget inputs are ok. Otherwise
        show an error dialog """
        res, txt = self._check_widget_inputs()
        if res:
            self.update_cfg()
            self.done(QtWidgets.QDialog.Accepted)  # or call superclass accept
        else:
            qt_message_dialog("Invalid input: %s" % txt)


class ProgressBar(QtWidgets.QProgressDialog):
    """ Qt progress bar with reasonable defaults """

    def __init__(self, title):
        super(self.__class__, self).__init__()
        self.setWindowTitle(title)
        self.setCancelButton(None)
        self.setMinimum(0)
        self.setMaximum(100)
        self.setGeometry(500, 300, 500, 100)
        self.setAutoClose(False)
        self.setAutoReset(False)
        self.show()

    def update(self, text, p):
        """ Update bar, showing text and bar at p% """
        self.setLabelText(text)
        self.setValue(p)
        # update right away in case that thread is blocked
        QtWidgets.QApplication.processEvents()


class ProgressSignals(QObject):
    """Used to emit progress signals"""
    progress = pyqtSignal(object, object)


class Gaitmenu(QtWidgets.QMainWindow):

    def __init__(self):
        super(self.__class__, self).__init__()
        # load user interface made with designer
        uifile = resource_filename('gaitutils', 'nexus_scripts/nexus_menu.ui')
        uic.loadUi(uifile, self)
        """
        matplotlib and threads:
        -----------------------
        Stuff that shows matplotlib plots cannot be run in directly in
        worker threads. To put plotting stuff into a worker thread, we need to:
        -make the plotting function return a figure (and not invoke the qt
        event loop)
        -put the plotting function into a worker thread
        -call plotting function
        -create a qt dialog and canvas in GUI thread
        -draw resulting figure onto canvas
        So far this has not been done since the plotting functions return
        rather quickly.
        Running the plotting functions directly from the GUI thread is also a
        bit ugly since the Qt event loop gets called twice, but this does not
        seem to do any harm.

        launching web servers:
        ----------------------
        Web servers need to go into separate threads/processes so that the rest
        of the app can continue running. It's hard to use processes because of
        problems with multiprocessing/pickle, so the Qt threadpool is used to
        launch the servers. However since each running server occupies a
        thread, this means that we need to increase the threadpool max threads
        limit; otherwise new servers will get queued by the threadpool and will
        not run.
        """

        if not cfg.general.allow_multiple_menu_instances:
            sname = op.split(__file__)[1]
            nprocs = envutils._count_script_instances(sname)
            if nprocs >= 2:
                qt_message_dialog('Another instance of the menu seems to be '
                                  'running. Please use that instance or '
                                  'stop it before starting a new one.')
                sys.exit()

        if cfg.general.git_autoupdate:
            if envutils._git_autoupdate():
                qt_message_dialog('The package was automatically updated. '
                                  'Restarting...')
                os.execv(sys.executable, ['python'] + sys.argv)

        # if using the plotly backend, we can run plotters in worker threads
        thread_plotters = cfg.plot.backend == 'plotly'

        # modal dialogs etc. (simple signal->slot connection)
        self.actionCreate_PDF_report.triggered.connect(self._create_pdf_report)
        self.actionCreate_comparison_PDF_report.triggered.connect(self._create_comparison)
        self.actionWeb_reports.triggered.connect(self._web_report_dialog)
        self.actionQuit.triggered.connect(self.close)
        self.actionOpts.triggered.connect(self._options_dialog)
        self.actionTardieu_analysis.triggered.connect(self._tardieu)
        self.actionAutoprocess_session.triggered.connect(self._autoproc_session)

        # main UI buttons
        self._widget_connect_task(self.btnPlotNexusTrial,
                                  self._plot_nexus_trial,
                                  thread=thread_plotters)

        # consistency menu
        self._widget_connect_task(self.actionTrial_velocity,
                                  nexus_trials_velocity.do_plot)
        self._widget_connect_task(self.actionTime_distance_average,
                                  nexus_time_distance_vars.
                                  do_session_average_plot)
        self._widget_connect_task(self.actionKinematics_consistency,
                                  nexus_kin_consistency.do_plot)
        self._widget_connect_task(self.actionEMG_consistency,
                                  nexus_emg_consistency.do_plot)
        self._widget_connect_task(self.actionMuscle_length_consistency,
                                  nexus_musclelen_consistency.do_plot)

        # processing menu
        self._widget_connect_task(self.actionAutoprocess_single_trial,
                                  nexus_autoprocess_trial.autoproc_single,
                                  thread=True)
        self._widget_connect_task(self.actionAutomark_events,
                                  nexus_automark_trial.automark_single,
                                  thread=True)
        self._widget_connect_task(self.actionRun_postprocessing_pipelines,
                                  self._postprocess_session)
        self._widget_connect_task(self.actionConvert_session_videos_to_web_format,
                                  self._convert_session_videos)
        self._widget_connect_task(self.actionCopy_session_videos_to_desktop,
                                  nexus_copy_trial_videos.do_copy)

        # add predefined plot layouts to combobox
        cb_items = sorted(cfg.layouts.menu_layouts.keys())
        self.cbNexusTrialLayout.addItems(cb_items)
        # set default option to PiG lower body (if it's on the list)
        try:
            default_index = cb_items.index('PiG lower body kinematics+'
                                           'kinetics')
        except ValueError:
            default_index = 0
        self.cbNexusTrialLayout.setCurrentIndex(default_index)

        XStream.stdout().messageWritten.connect(self._log_message)
        XStream.stderr().messageWritten.connect(self._log_message)
        logger.debug('interpreter: %s' % sys.executable)
        self.threadpool = QThreadPool()
        # we need a thread for each web server plus one worker thread
        self.threadpool.setMaxThreadCount(cfg.web_report.max_reports + 1)

        self._browser_procs = list()
        self._flask_apps = dict()

    def _web_report_dialog(self):
        dlg = WebReportDialog(self)
        dlg.show()

    def _autoproc_session(self):
        """Run autoprocess for Nexus session"""
        sessionpath = nexus.get_sessionpath()
        enfs = sessionutils.get_session_enfs(sessionpath)
        enfs = sessionutils._filter_by_type(enfs, 'DYNAMIC')
        c3ds = list(sessionutils._filter_to_c3ds(enfs))
        if c3ds:
            reply = qt_yesno_dialog('Some of the dynamic trials have been '
                                    'processed already. Are you sure you want '
                                    'to run autoprocessing?')
            if reply == QtWidgets.QMessageBox.YesRole:
                self._execute(nexus_autoprocess_session.autoproc_session,
                              thread=True,
                              finished_func=self._enable_op_buttons)

    def _plot_nexus_trial(self):
        """Plot the current Nexus trial"""
        lout_desc = self.cbNexusTrialLayout.currentText()
        lout_name = cfg.layouts.menu_layouts[lout_desc]
        cycs = 'unnormalized' if self.xbPlotUnnorm.checkState() else None
        model_cycles = emg_cycles = cycs
        from_c3d = self.xbPlotFromC3D.checkState()
        nexus_plot.do_plot(lout_name, model_cycles=model_cycles,
                           emg_cycles=emg_cycles, from_c3d=from_c3d)

    def _widget_connect_task(self, widget, fun, thread=False):
        """ Helper to connect button or action item with task. Use lambda to
        consume unused events argument. If thread=True, launch in a separate
        worker thread. """
        # by default, just enable UI buttons when thread finishes
        finished_func = self._enable_op_buttons if thread else None
        if isinstance(widget, QtWidgets.QPushButton):
            sig = widget.clicked
        elif isinstance(widget, QtWidgets.QAction):
            sig = widget.triggered
        sig.connect(lambda ev: self._execute(fun, thread=thread,
                                             finished_func=finished_func))

    def _convert_vidfiles(self, vidfiles, signals):
        """Convert given list of video files to web format. Uses non-blocking
        Popen() calls"""
        self._disable_op_buttons()
        procs = self._execute(report.convert_videos, thread=False,
                              block_ui=False, vidfiles=vidfiles)
        if not procs:
            return

        completed = False
        while not completed:
            n_complete = len([p for p in procs if p.poll() is not None])
            prog_txt = ('Converting videos: %d of %d files done'
                        % (n_complete, len(procs)))
            prog_p = 100 * n_complete / float(len(procs))
            signals.progress.emit(prog_txt, prog_p)
            time.sleep(.25)
            completed = n_complete == len(procs)
        self._enable_op_buttons(None)

    def _browse_localhost(self, port):
        """Open configured browser on localhost:port"""
        url = '127.0.0.1:%d' % port
        try:
            proc = subprocess.Popen([cfg.general.browser_path, url])
            self._browser_procs.append(proc)
            logger.debug('new browser pid %d' % proc.pid)
        except Exception:
            qt_message_dialog('Cannot start configured web browser: %s'
                              % cfg.general.browser_path)

    @staticmethod
    def _collect_videos_to_convert(session, tags):
        """Collect session AVI files for conversion to web format"""
        c3ds = sessionutils.get_c3ds(session, tags=tags,
                                     trial_type='dynamic')
        c3ds += sessionutils.get_c3ds(session, tags=cfg.eclipse.video_tags,
                                      trial_type='dynamic')
        c3ds += sessionutils.get_c3ds(session, trial_type='static')
        vids_it = (videos.get_trial_videos(c3d, vid_ext='.avi')
                   for c3d in c3ds)
        return list(itertools.chain.from_iterable(vids_it))

    def _convert_session_videos(self):
        """Convert current Nexus session videos to web format."""
        try:
            session = nexus.get_sessionpath()
        except GaitDataError as e:
            qt_message_dialog(_exception_msg(e))
            return
        try:
            vidfiles = self._collect_videos_to_convert(session,
                                                       tags=cfg.eclipse.tags)
        except GaitDataError as e:
            qt_message_dialog(_exception_msg(e))
            return
        if not vidfiles:
            qt_message_dialog('Cannot find any video files for session %s'
                              % session)
            return
        if report.convert_videos(vidfiles, check_only=True):
            qt_message_dialog('It looks like the session videos have already '
                              'been converted.')
            return
        prog = ProgressBar('')
        signals = ProgressSignals()
        signals.progress.connect(lambda text, p: prog.update(text, p))
        self._convert_vidfiles(vidfiles, signals)
        prog.close()

    def _postprocess_session(self):
        """Run additional postprocessing pipelines for tagged trials"""
        try:
            session = nexus.get_sessionpath()
        except GaitDataError as e:
            qt_message_dialog(_exception_msg(e))
            return
        # XXX: run for tagged + static - maybe this should be configurable
        trials = sessionutils.get_c3ds(session, tags=cfg.eclipse.tags,
                                       trial_type='dynamic')
        trials += sessionutils.get_c3ds(session, trial_type='static')
        if trials and cfg.autoproc.postproc_pipelines:
            logger.debug('running postprocessing for %s' % trials)
            prog = ProgressBar('')
            vicon = nexus.viconnexus()
            prog.update('Running postprocessing pipelines: %s for %d '
                        'trials' % (cfg.autoproc.postproc_pipelines,
                                    len(trials)), 0)
            for k, tr in enumerate(trials):
                trbase = op.splitext(tr)[0]
                vicon.OpenTrial(trbase, cfg.autoproc.nexus_timeout)
                nexus.run_pipelines(vicon, cfg.autoproc.postproc_pipelines)
                prog.update('Running postprocessing pipelines: %s for %d '
                            'trials' % (cfg.autoproc.postproc_pipelines, len(trials)), 100*k/len(trials))
        elif not trials:
            qt_message_dialog('No trials in session to run postprocessing for')

    def closeEvent(self, event):
        """ Confirm and close application. """
        if self.listActiveReports.count():
            reply = qt_yesno_dialog('There are active processes which '
                                    'will be terminated. Are you sure you '
                                    'want to quit?')
            if reply == QtWidgets.QMessageBox.YesRole:
                # try to shutdown browser processes and web servers
                for proc in self._browser_procs:
                    proc.kill()
                # cannot use generator here since the loop changes the items
                for item in list(self.listActiveReports.items):
                    self._delete_report(item)
                event.accept()
            else:
                event.ignore()
        else:
            event.accept()

    def _options_dialog(self):
        """Show the options dialog"""
        dlg = OptionsDialog(self)
        dlg.exec_()

    def _create_comparison(self):
        dlg = ChooseSessionsDialog(min_sessions=2, max_sessions=2)
        if dlg.exec_():
            self._sessions = dlg.sessions
            self._execute(nexus_make_comparison_report.do_plot,
                          sessions=dlg.sessions)


    def _create_pdf_report(self):
        """Creates the full pdf report"""
        try:
            subj = nexus.get_subjectnames()
        except GaitDataError as e:
            qt_message_dialog(_exception_msg(e))
            return

        # ask for patient info, update saved info accordingly
        session = nexus.get_sessionpath()
        info = sessionutils.load_info(session) or sessionutils.default_info()
        prompt_ = 'Please give additional subject information for %s:' % subj
        dlg = PdfReportDialog(info, prompt=prompt_)
        if dlg.exec_():
            new_info = dict(hetu=dlg.hetu, fullname=dlg.fullname,
                            session_description=dlg.session_description)
            self._execute(nexus_make_pdf_report.do_plot, thread=True,
                          finished_func=self._enable_op_buttons,
                          fullname=dlg.fullname, hetu=dlg.hetu,
                          session_description=dlg.session_description,
                          pages=dlg.pages)
            info.update(new_info)
            sessionutils.save_info(session, info)

    def _log_message(self, msg):
        c = self.txtOutput.textCursor()
        c.movePosition(QtGui.QTextCursor.End)
        self.txtOutput.setTextCursor(c)
        self.txtOutput.insertPlainText(msg)
        self.txtOutput.ensureCursorVisible()

    def _exception(self, e):
        logger.debug('caught exception while running task')
        qt_message_dialog(_exception_msg(e))

    def _disable_op_buttons(self):
        """ Disable all operation buttons """
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        self.setEnabled(False)  # disables whole main window
        # update display immediately in case thread gets blocked
        QtWidgets.QApplication.processEvents()

    def _enable_op_buttons(self, r):
        """Enable all operation buttons and restore cursor. Takes single
        argument to fit the _finished_func call signature (see _execute) """
        QtWidgets.QApplication.restoreOverrideCursor()
        self.setEnabled(True)
        # set status of report buttons separately
        self._set_report_button_status()

    def _tardieu(self):
        win = nexus_tardieu.TardieuWindow()
        win.show()

    def _execute(self, fun, thread=False, block_ui=True, finished_func=None,
                 **kwargs):
        """ Run function fun. If thread==True, run it in a separate worker
        thread. If block_ui, disable the ui until worker thread is finished
        (except for messages!) Returns function return value if not threaded.
        If threaded and finished_func is given, the latter is called with the
        function return value as single argument when thread completes
        execution.
        If block_ui, finished_func should re-enable UI buttons.
        kwargs are passed to function.
        """
        fun_ = partial(fun, **kwargs)
        if block_ui:
            self._disable_op_buttons()
        if thread:
            self.runner = Runner(fun_)
            if finished_func:
                self.runner.signals.finished.connect(lambda r:
                                                     finished_func(r))
            self.runner.signals.error.connect(lambda e: self._exception(e))
            self.threadpool.start(self.runner)
            retval = None
        else:  # nonthreaded execute
            try:
                retval = fun_()
            except Exception as e:
                retval = None
                self._exception(e)
            finally:
                if block_ui:
                    self._enable_op_buttons(None)
        return retval


class RunnerSignals(QObject):
    """Need a separate class since QRunnable cannot emit signals"""
    finished = pyqtSignal(object)
    error = pyqtSignal(Exception)


class Runner(QRunnable):
    """Encapsulates threaded functions for QThreadPool"""

    def __init__(self, fun):
        super(Runner, self).__init__()
        self.fun = fun
        self.signals = RunnerSignals()
        self.retval = False  # default "return value" when exception is thrown

    def run(self):
        try:
            self.retval = self.fun()
        except Exception as e:
            self.signals.error.emit(e)
        finally:
            self.signals.finished.emit(self.retval)


def main():

    app = QtWidgets.QApplication(sys.argv)

    def my_excepthook(type_, value, tback):
        """ Custom handler for unhandled exceptions:
        report to user via GUI and terminate. """
        tb_full = u''.join(traceback.format_exception(type_, value, tback))
        qt_message_dialog('Oops! An unhandled exception was generated. '
                          'The application will be closed.\n\n %s' % tb_full)
        # dump traceback to file
        # try:
        #    with io.open(Config.traceback_file, 'w', encoding='utf-8') as f:
        #        f.write(tb_full)
        # here is a danger of infinitely looping the exception hook,
        # so try to catch any exceptions...
        # except Exception:
        #    print('Cannot dump traceback!')
        sys.__excepthook__(type_, value, tback)
        app.quit()

    sys.excepthook = my_excepthook

    logger = logging.getLogger()
    handler = QtHandler()  # log to Qt logging widget
    # handler = logging.StreamHandler()   # log to sys.stdout

    handler.setFormatter(logging.
                         Formatter("%(name)s: %(levelname)s: %(message)s"))
    handler.setFormatter(logging.Formatter("%(name)s: %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)

    # quiet down some noisy loggers
    logging.getLogger('PyQt5.uic').setLevel(logging.WARNING)
    logging.getLogger('matplotlib.font_manager').setLevel(logging.WARNING)
    logging.getLogger('matplotlib.backends.backend_pdf').setLevel(logging.WARNING)
    logging.getLogger('werkzeug').setLevel(logging.WARNING)

    gaitmenu = Gaitmenu()
    gaitmenu.show()

    nexus_status = 'Vicon Nexus is %srunning' % ('' if nexus.pid() else 'not ')
    logger.debug(nexus_status)
    app.exec_()


if __name__ == '__main__':
    main()
