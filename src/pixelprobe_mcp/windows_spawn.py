"""Windows 无窗口 spawn 适配，隔离 CPython multiprocessing 的平台接口。

启动握手遵循 CPython popen_spawn_win32（PSF License），额外设置
CREATE_NO_WINDOW，避免无控制台 MCP 宿主启动工作进程时等待控制台初始化。
"""
import os
import sys
import msvcrt
import _winapi
from subprocess import CREATE_NO_WINDOW, STARTUPINFO, list2cmdline
from multiprocessing import spawn, util
from multiprocessing.context import SpawnProcess, reduction, set_spawning_popen
from multiprocessing.popen_spawn_win32 import Popen, _close_handles


class HiddenPopen(Popen):
    def __init__(self, process_obj):
        preparation = spawn.get_preparation_data(process_obj.name)
        read_handle, write_handle = _winapi.CreatePipe(None, 0)
        try:
            write_fd = msvcrt.open_osfhandle(write_handle, 0)
        except BaseException:
            _close_handles(read_handle, write_handle)
            raise
        with open(write_fd, "wb", closefd=True) as output:
            try:
                command = spawn.get_command_line(parent_pid=os.getpid(), pipe_handle=read_handle)
                executable = spawn.get_executable()
                environment = None
                if (os.path.normcase(sys.executable) != os.path.normcase(sys._base_executable)
                        and os.path.normcase(executable) == os.path.normcase(sys.executable)):
                    command[0] = executable = sys._base_executable
                    environment = {**os.environ, "__PYVENV_LAUNCHER__": sys.executable}
                handle, thread, pid, _ = _winapi.CreateProcess(
                    executable, list2cmdline(command), None, None, False,
                    CREATE_NO_WINDOW, environment, None, STARTUPINFO(),
                )
            except BaseException:
                _winapi.CloseHandle(read_handle)
                raise
            _winapi.CloseHandle(thread)
            self.pid, self.returncode = pid, None
            self._handle = self.sentinel = int(handle)
            self.finalizer = util.Finalize(self, _close_handles, (self.sentinel, read_handle))
            set_spawning_popen(self)
            try:
                reduction.dump(preparation, output)
                reduction.dump(process_obj, output)
            except BaseException:
                self.terminate()
                self.wait()
                self.finalizer()
                raise
            finally:
                set_spawning_popen(None)


class HiddenSpawnProcess(SpawnProcess):
    @staticmethod
    def _Popen(process_obj):
        return HiddenPopen(process_obj)
