# Copyright 2015 Canonical, Ltd.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import logging
import os
import queue
import select

import netifaces
import yaml

from subiquitycore.async import Async
from subiquitycore.models import NetworkModel
from subiquitycore.ui.views import (NetworkView,
                                    NetworkSetDefaultRouteView,
                                    NetworkBondInterfacesView,
                                    NetworkConfigureInterfaceView,
                                    NetworkConfigureIPv4InterfaceView)
from subiquitycore.ui.views.network import ApplyingConfigWidget
from subiquitycore.ui.dummy import DummyView
from subiquitycore.controller import BaseController
from subiquitycore.utils import run_command_start, run_command_summarize

log = logging.getLogger("subiquitycore.controller.network")


class BackgroundTask:
    """Something that runs without blocking the UI and can be canceled."""

    def run(self):
        raise NotImplementedError(self.run)

    def cancel(self):
        raise NotImplementedError(self.cancel)


class BackgroundProcess(BackgroundTask):

    def __init__(self, cmd):
        self.cmd = cmd

    def __repr__(self):
        return 'BackgroundProcess(%r)'%(self.cmd,)

    def run(self, observer):
        self.proc = run_command_start(self.cmd)
        stdout, stderr = self.proc.communicate()
        result = run_command_summarize(self.proc, stdout, stderr)
        if result['status'] == 0:
            observer.task_succeeded()
        else:
            observer.task_failed()

    def cancel(self):
        try:
            self.proc.terminate()
        except ProcessLookupError:
            pass # It's OK if the process has already terminated.


class PythonSleep(BackgroundTask):

    def __init__(self, duration):
        self.duration = duration
        self.r, self.w = os.pipe()

    def __repr__(self):
        return 'PythonSleep(%r)'%(self.duration,)

    def run(self, observer):
        r, _, _ = select.select([self.r], [], [], self.duration)
        if not r:
            observer.task_succeeded()
        os.close(self.r)
        os.close(self.w)

    def cancel(self):
        os.write(self.w, b'x')


class TaskSequence:
    def __init__(self, loop, tasks, watcher):
        self.loop = loop
        self.tasks = tasks
        self.watcher = watcher
        self.canceled = False
        self.stage = None
        self.curtask = None
        self.incoming = queue.Queue()
        self.outgoing = queue.Queue()
        self.pipe = self.loop.watch_pipe(self._thread_callback)

    def run(self):
        self._run1()

    def cancel(self):
        if self.curtask is not None:
            log.debug("canceling %s", self.curtask)
            self.curtask.cancel()
        self.canceled = True

    def _run1(self):
        self.stage, self.curtask = self.tasks[0]
        self.tasks = self.tasks[1:]
        log.debug('running %s for stage %s', self.curtask, self.stage)
        def cb(fut):
            # We do this just so that any exceptions raised don't get lost.
            # Vomiting a traceback all over the console is nasty, but not as
            # nasty as silently doing nothing.
            fut.result()
        Async.pool.submit(self.curtask.run, self).add_done_callback(cb)

    def call_from_thread(self, func, *args):
        log.debug('call_from_thread %s %s', func, args)
        self.incoming.put((func, args))
        os.write(self.pipe, b'x')
        self.outgoing.get()

    def _thread_callback(self, ignored):
        func, args = self.incoming.get()
        func(*args)
        self.outgoing.put(None)

    def task_succeeded(self):
        self.call_from_thread(self._task_succeeded)

    def _task_succeeded(self):
        if self.canceled:
            return
        self.watcher.task_complete(self.stage)
        if len(self.tasks) == 0:
            self.watcher.tasks_finished()
        else:
            self._run1()

    def task_failed(self):
        if self.canceled:
            return
        self.call_from_thread(self.watcher.task_error, self.stage)


class NetworkController(BaseController):
    def __init__(self, common):
        super().__init__(common)
        self.model = NetworkModel(self.prober, self.opts)

    def network(self):
        # The network signal is the one that is called when we enter
        # the network configuration from the preceding or following
        # screen. We clear any existing state, probe for the current
        # state and then invoke the 'start' signal, which is what the
        # sub screens will return to, so that the network state does
        # not get re-probed when they return, which would throw away
        # any configuration made in the sub-screen!
        self.model.reset()
        log.info("probing for network devices")
        self.model.probe_network()
        self.signal.emit_signal('menu:network:main:start')

    def start(self):
        title = "Network connections"
        excerpt = ("Configure at least the main interface this server will "
                   "use to talk to the store.")
        footer = ("Additional networking info here")
        self.ui.set_header(title, excerpt)
        self.ui.set_footer(footer, 20)
        self.ui.set_body(NetworkView(self.model, self.signal))

    def network_finish(self, config):
        log.debug("network config: \n%s", yaml.dump(config, default_flow_style=False))

        if self.opts.dry_run:
            if hasattr(self, 'tried_once'):
                tasks = [
                    ('one', BackgroundProcess(['sleep', '1'])),
                    ('two', PythonSleep(1)),
                    ('three', BackgroundProcess(['sleep', '1'])),
                    ]
            else:
                self.tried_once = True
                tasks = [
                    ('one', BackgroundProcess(['sleep', '1'])),
                    ('two', BackgroundProcess(['sleep', '1'])),
                    ('three', BackgroundProcess(['false'])),
                    ('four', BackgroundProcess(['sleep 1'])),
                    ]
        else:
            with open('/etc/netplan/01-console-conf.yaml', 'w') as w:
                w.write(yaml.dump(config))
            tasks = [
                ('generate', BackgroundProcess(['/lib/netplan/generate'])),
                ('apply', BackgroundProcess(['netplan', 'apply'])),
                ('timeout', BackgroundProcess(['/lib/systemd/systemd-networkd-wait-online', '--timeout=30'])),
                ]

        def cancel():
            self.cs.cancel()
            self.task_error('canceled')
        self.acw = ApplyingConfigWidget(len(tasks), cancel)
        self.ui.frame.body.show_overlay(self.acw)

        self.cs = TaskSequence(self.loop, tasks, self)
        self.cs.run()

    def task_complete(self, stage):
        self.acw.advance()

    def task_error(self, stage):
        self.ui.frame.body.remove_overlay(self.acw)
        self.ui.frame.body.show_network_error(stage)

    def tasks_finished(self):
        self.signal.emit_signal('menu:identity:main')

    def set_default_v4_route(self):
        self.ui.set_header("Default route")
        self.ui.set_body(NetworkSetDefaultRouteView(self.model,
                                                    netifaces.AF_INET,
                                                    self.signal))

    def set_default_v6_route(self):
        self.ui.set_header("Default route")
        self.ui.set_body(NetworkSetDefaultRouteView(self.model,
                                                    netifaces.AF_INET6,
                                                    self.signal))

    def bond_interfaces(self):
        self.ui.set_header("Bond interfaces")
        self.ui.set_body(NetworkBondInterfacesView(self.model,
                                                   self.signal))

    def network_configure_interface(self, iface):
        self.ui.set_header("Network interface {}".format(iface))
        self.ui.set_body(NetworkConfigureInterfaceView(self.model,
                                                       self.signal,
                                                       iface))

    def network_configure_ipv4_interface(self, iface):
        self.model.prev_signal = ('Back to configure interface menu',
                                  'network:configure-interface-menu',
                                  'network_configure_interface')
        self.ui.set_header("Network interface {} manual IPv4 "
                           "configuration".format(iface))
        self.ui.set_body(NetworkConfigureIPv4InterfaceView(self.model,
                                                           self.signal,
                                                           iface))

    def network_configure_ipv6_interface(self, iface):
        self.model.prev_signal = ('Back to configure interface menu',
                                  'network:configure-interface-menu',
                                  'network_configure_interface')
        self.ui.set_body(DummyView(self.signal))

    def install_network_driver(self):
        self.ui.set_body(DummyView(self.signal))

