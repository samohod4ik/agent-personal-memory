"""Available process memory for bounded in-memory operations."""
import os
from pathlib import Path

def available_memory():
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [('length', wintypes.DWORD), ('load', wintypes.DWORD),
                        ('total_phys', ctypes.c_ulonglong), ('avail_phys', ctypes.c_ulonglong),
                        ('total_page', ctypes.c_ulonglong), ('avail_page', ctypes.c_ulonglong),
                        ('total_virtual', ctypes.c_ulonglong), ('avail_virtual', ctypes.c_ulonglong),
                        ('avail_extended', ctypes.c_ulonglong)]
        status = MEMORYSTATUSEX()
        status.length = ctypes.sizeof(status)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            raise ValueError('memory availability unavailable')
        return min(status.avail_phys, status.avail_virtual)
    if os.name == 'posix' and Path('/proc/meminfo').exists():
        for line in Path('/proc/meminfo').read_text().splitlines():
            if line.startswith('MemAvailable:'):
                available = int(line.split()[1]) * 1024
                cgroup = Path('/sys/fs/cgroup')
                memberships = Path('/proc/self/cgroup').read_text().splitlines()
                if (cgroup / 'memory.max').exists():
                    group = next((line.split('::', 1)[1] for line in memberships if '::' in line), '/')
                    group_path = cgroup / group.lstrip('/')
                    while True:
                        maximum = (group_path / 'memory.max').read_text().strip()
                        if maximum != 'max':
                            available = min(available, max(0, int(maximum) - int((group_path / 'memory.current').read_text())))
                        if group_path == cgroup:
                            break
                        group_path = group_path.parent
                elif any('memory' in line.split(':', 2)[1].split(',') for line in memberships):
                    raise ValueError('memory availability unavailable')
                import resource
                limit, _ = resource.getrlimit(resource.RLIMIT_AS)
                if limit != resource.RLIM_INFINITY:
                    status = Path('/proc/self/status').read_text().splitlines()
                    virtual = next(int(line.split()[1]) * 1024 for line in status if line.startswith('VmSize:'))
                    available = min(available, max(0, limit - virtual))
                return available
    raise ValueError('memory availability unavailable')
