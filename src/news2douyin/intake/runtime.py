"""A process-owned receiver resumes abandoned leases before background acquisition."""
import logging
import os
from pathlib import Path
import sys
from threading import Event,Thread
from sqlalchemy.exc import SQLAlchemyError
from . import service

log=logging.getLogger(__name__)


class ReceiverBusy(RuntimeError):pass


class ReceiverLock:
    """OS-backed advisory ownership; crashes release the lock without deleting its inode."""
    def __init__(self,storage_root):
        self.path=Path(storage_root).resolve()/'intake'/'receiver.lock';self.file=None

    def acquire(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        file=self.path.open('a+b')
        try:
            if sys.platform=='win32':
                import msvcrt
                file.seek(0,2)
                if not file.tell():file.write(b'0');file.flush()
                file.seek(0);msvcrt.locking(file.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(file.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError as exc:
            file.close();raise ReceiverBusy('另一个接收进程正在使用此采集目录') from exc
        self.file=file;return self

    def release(self):
        file,self.file=self.file,None
        if file is None:return
        try:
            if sys.platform=='win32':
                import msvcrt
                file.seek(0);msvcrt.locking(file.fileno(),msvcrt.LK_UNLCK,1)
            else:
                import fcntl
                fcntl.flock(file.fileno(),fcntl.LOCK_UN)
        finally:file.close()


class IntakeWorker:
    def __init__(self,engine,storage_root):
        self.engine=engine;self.storage_root=storage_root;self.stop_event=Event();self.thread=None
        self._ownership=None;self.status_error=''
        self.last_error='';self.failures=0;self._needs_recovery=False

    @property
    def running(self):return bool(self.thread and self.thread.is_alive())

    def start(self):
        if self.running:return True
        if not service.runtime_enabled():return False
        ownership=ReceiverLock(self.storage_root)
        try:
            ownership.acquire()
            # Ownership guarantees no other cooperating receiver is still using these leases.
            service.recover_leases(self.engine)
            self._ownership=ownership;self.status_error='';self.last_error='';self.failures=0;self._needs_recovery=False;self.stop_event.clear()
            self.thread=Thread(target=self.loop,daemon=True,name='gkg-archive');self.thread.start()
            return True
        except ReceiverBusy as exc:
            self.status_error=str(exc);ownership.release();return False
        except Exception as exc:
            self.status_error='接收器启动失败 · '+type(exc).__name__;ownership.release();return False

    def stop(self):
        self.stop_event.set()
        if self.thread:self.thread.join(timeout=15)
        # A blocked read may outlive stop(). Its thread retains ownership until its finally.

    def step(self,feed=None,now=None):return service.step(self.engine,self.storage_root,feed=feed,now=now,stop=self.stop_event)

    def loop(self):
        try:
            while not self.stop_event.is_set():
                try:
                    if self._needs_recovery and self._ownership:
                        service.recover_leases(self.engine);self._needs_recovery=False
                    changed=self.step()
                except Exception as exc:
                    log.exception('archive receiver tick failed')
                    self.last_error=type(exc).__name__;self.failures+=1
                    if isinstance(exc,SQLAlchemyError):self._needs_recovery=True
                    delay=min(60,2**min(self.failures-1,6))
                else:
                    self.last_error='';self.failures=0
                    name='NEWS2DOUYIN_GKG_STREAM_INTERVAL' if changed else 'NEWS2DOUYIN_GKG_STREAM_IDLE_INTERVAL'
                    try:delay=float(os.getenv(name,'1' if changed else '5'))
                    except ValueError:delay=1 if changed else 5
                    delay=max(1,delay) if delay<float('inf') else (1 if changed else 5)
                self.stop_event.wait(delay)
        finally:
            if self._ownership:
                self._ownership.release();self._ownership=None
