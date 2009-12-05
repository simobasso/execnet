"""
Managing Gateway Groups and interactions with multiple channels.

(c) 2008-2009, Holger Krekel and others
"""

import os, sys, weakref, atexit
import execnet
from execnet import XSpec
from execnet import gateway
from execnet.gateway_base import queue, reraise, trace, TimeoutError

NO_ENDMARKER_WANTED = object()

class Group:
    """ Gateway Groups. """
    def __init__(self, xspecs=()):
        """ initialize group and make gateways as specified. """
        # Gateways may evolve to become GC-collectable
        self._activegateways = weakref.WeakKeyDictionary()
        self._id2gateway = weakref.WeakValueDictionary()
        self._autoidcounter = 1
        self._gateways_to_join = []
        for xspec in xspecs:
            self.makegateway(xspec)
        atexit.register(self._cleanup_atexit)

    def __repr__(self):
        keys = list(self._id2gateway)
        keys.sort()
        return "<Group %r>" %(keys,)

    def __getitem__(self, key):
        return self._id2gateway[key]

    def __contains__(self, key):
        return key in self._id2gateway or key in self._activegateways

    def __len__(self):
        return len(self._activegateways)

    def __iter__(self):
        l = list(self._id2gateway.items())
        l.sort()
        for id, gw in l:
            yield gw

    def makegateway(self, spec):
        """ create and configure a gateway to a Python interpreter
            specified by a 'execution specification' string.
            The format of the string generally is::

                key1=value1//key2=value2//...

            If you leave out the ``=value`` part a True value is assumed.
        """
        if not isinstance(spec, XSpec):
            spec = XSpec(spec)
        id = self._allocate_id(spec.id)
        if spec.popen:
            gw = gateway.PopenGateway(python=spec.python, id=id)
        elif spec.ssh:
            gw = gateway.SshGateway(spec.ssh, remotepython=spec.python, 
                                    ssh_config=spec.ssh_config, id=id)
        elif spec.socket:
            assert not spec.python, (
                "socket: specifying python executables not yet supported")
            gateway_id = spec.installvia
            if gateway_id:
                viagw = self._id2gateway[gateway_id]
                gw = gateway.SocketGateway.new_remote(viagw, id=id)
            else:
                host, port = spec.socket.split(":")
                gw = gateway.SocketGateway(host, port, id=id)
        else:
            raise ValueError("no gateway type found for %r" % (spec._spec,))
        gw.spec = spec
        self._register(gw)
        if spec.chdir or spec.nice:
            channel = gw.remote_exec("""
                import os
                path, nice = channel.receive()
                if path:
                    if not os.path.exists(path):
                        os.mkdir(path)
                    os.chdir(path)
                if nice and hasattr(os, 'nice'):
                    os.nice(nice)
            """)
            nice = spec.nice and int(spec.nice) or 0
            channel.send((spec.chdir, nice))
            channel.waitclose()
        return gw

    def _allocate_id(self, id=None):
        if id is None:
            id = str(self._autoidcounter)
            self._autoidcounter += 1
        assert id not in self._id2gateway
        return id

    def _register(self, gateway):
        assert not hasattr(gateway, '_group')
        assert gateway.id
        assert id not in self._id2gateway
        assert gateway not in self._activegateways
        self._activegateways[gateway] = True
        self._id2gateway[gateway.id] = gateway
        gateway._group = self

    def _unregister(self, gateway):
        del self._id2gateway[gateway.id]
        del self._activegateways[gateway]
        self._gateways_to_join.append(gateway)

    def _cleanup_atexit(self):
        trace("=== atexit cleanup %r ===" %(self,))
        self.terminate(timeout=1.0)

    def terminate(self, timeout=None):
        """ trigger exit of member gateways and wait for termination 
        of member gateways and associated subprocesses.  After waiting 
        timeout seconds an attempt to kill local sub processes of popen- 
        and ssh-gateways is started.  Timeout defaults to None meaning 
        open-ended waiting and no kill attempts.
        """
        for gw in self:
            gw.exit()
        def join_receiver_and_wait_for_subprocesses():
            for gw in self._gateways_to_join:
                gw.join()
            while self._gateways_to_join:
                gw = self._gateways_to_join[0]
                if hasattr(gw, '_popen'):
                    gw._popen.wait()
                del self._gateways_to_join[0]
        from execnet.threadpool import WorkerPool
        pool = WorkerPool(1)
        reply = pool.dispatch(join_receiver_and_wait_for_subprocesses)
        try:
            reply.get(timeout=timeout)
        except IOError:
            trace("Gateways did not come down after timeout: %r" 
                  %(self._gateways_to_join))
            while self._gateways_to_join:
                gw = self._gateways_to_join.pop(0)
                popen = getattr(gw, '_popen', None)
                if popen:
                    killpopen(popen)

    def remote_exec(self, source):
        """ remote_exec source on all member gateways and return
            MultiChannel connecting to all sub processes.
        """
        channels = []
        for gw in list(self._activegateways):
            channels.append(gw.remote_exec(source))
        return MultiChannel(channels)

class MultiChannel:
    def __init__(self, channels):
        self._channels = channels

    def send_each(self, item):
        for ch in self._channels:
            ch.send(item)

    def receive_each(self, withchannel=False):
        assert not hasattr(self, '_queue')
        l = []
        for ch in self._channels:
            obj = ch.receive()
            if withchannel:
                l.append((ch, obj))
            else:
                l.append(obj)
        return l

    def make_receive_queue(self, endmarker=NO_ENDMARKER_WANTED):
        try:
            return self._queue
        except AttributeError:
            self._queue = queue.Queue()
            for ch in self._channels:
                def putreceived(obj, channel=ch):
                    self._queue.put((channel, obj))
                if endmarker is NO_ENDMARKER_WANTED:
                    ch.setcallback(putreceived)
                else:
                    ch.setcallback(putreceived, endmarker=endmarker)
            return self._queue


    def waitclose(self):
        first = None
        for ch in self._channels:
            try:
                ch.waitclose()
            except ch.RemoteError:
                if first is None:
                    first = sys.exc_info()
        if first:
            reraise(*first)


default_group = Group()

makegateway = default_group.makegateway

def PopenGateway(python=None):
    """ instantiate a gateway to a subprocess
        started with the given 'python' executable.
    """
    APIWARN("1.0.0b4", "use makegateway('popen')")
    spec = execnet.XSpec("popen")
    spec.python = python
    return default_group.makegateway(spec)

def SocketGateway(host, port):
    """ This Gateway provides interaction with a remote process
        by connecting to a specified socket.  On the remote
        side you need to manually start a small script
        (py/execnet/script/socketserver.py) that accepts
        SocketGateway connections or use the experimental
        new_remote() method on existing gateways.
    """
    APIWARN("1.0.0b4", "use makegateway('socket=host:port')")
    spec = execnet.XSpec("socket=%s:%s" %(host, port))
    return default_group.makegateway(spec)

def SshGateway(sshaddress, remotepython=None, ssh_config=None):
    """ instantiate a remote ssh process with the
        given 'sshaddress' and remotepython version.
        you may specify an ssh_config file.
    """
    APIWARN("1.0.0b4", "use makegateway('ssh=host')")
    spec = execnet.XSpec("ssh=%s" % sshaddress)
    spec.python = remotepython
    spec.ssh_config = ssh_config
    return default_group.makegateway(spec)

def APIWARN(version, msg, stacklevel=3):
    import warnings
    Warn = DeprecationWarning("(since version %s) %s" %(version, msg))
    warnings.warn(Warn, stacklevel=stacklevel)

def killpopen(popen):
    try:
        if hasattr(popen, 'kill'):
            popen.kill()
        else:
            killpid(popen.pid)
    except EnvironmentError:
        sys.stderr.write("ERROR killing: %s\n" %(sys.exc_info()[1]))
        sys.stderr.flush()

def killpid(pid):
    if hasattr(os, 'kill'):
        os.kill(pid, 15)
    elif sys.platform == "win32":
        try:
            import ctypes
        except ImportError:
            # T: treekill, F: Force 
            cmd = ("taskkill /T /F /PID %d" %(pid)).split()
            ret = subprocess.call(cmd)
            if ret != 0:
                raise EnvironmentError("taskkill returned %r" %(ret,))
        else:
            PROCESS_TERMINATE = 1
            handle = ctypes.windll.kernel32.OpenProcess(
                        PROCESS_TERMINATE, False, pid)
            ctypes.windll.kernel32.TerminateProcess(handle, -1)
            ctypes.windll.kernel32.CloseHandle(handle)
    else:
        raise EnvironmmentError("no method to kill %s" %(pid,))
