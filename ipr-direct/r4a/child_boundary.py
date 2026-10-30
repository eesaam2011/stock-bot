# COMPLETE: R4-A child process boundary helper; production stage CLI belongs to R4-C.
"""Same Linux parent-death invariant as reference shadow_guard_main.run_child."""
import os,sys,signal,ctypes

def arm_parent_death(expected_parent_pid,*,platform=None,libc=None,getppid=None):
 platform=sys.platform if platform is None else platform
 if platform!='linux':raise RuntimeError('LINUX_PARENT_DEATH_SIGNAL_REQUIRED')
 libc=libc or ctypes.CDLL(None,use_errno=True);getppid=getppid or os.getppid
 if libc.prctl(1,signal.SIGKILL,0,0,0)!=0:raise RuntimeError('PRCTL_FAILED')
 if getppid()!=expected_parent_pid:raise RuntimeError('PARENT_ALREADY_CHANGED')
 return True

def child_bootstrap(expected_parent_pid,build):
 arm_parent_death(expected_parent_pid)
 return build() # Never starts an engine if parent is already gone.
