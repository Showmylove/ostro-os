#[PROTEXCAT]
#\License: ALL RIGHTS RESERVED

import os
from oeqa.oetest import oeRuntimeTest
from oeqa.utils.helper import collect_pnp_log

class CPUUsageTest(oeRuntimeTest):

    """CPU consumption for system idle"""

    def _reboot(self):
        """reboot device for clean env"""
        (status, output) = self.target.run("reboot")
        time.sleep(120)
        self.assertEqual(status, 0, output)
    
    def test_cpuusage(self):
        """Used_cpu = 100% - idle_cpu"""
        # self._reboot()
        filename = os.path.basename(__file__)
        casename = os.path.splitext(filename)[0]
        (status, output) = self.target.run(
            "top -b -d 10 -n 12 >/tmp/top.log")
        (status, output) = self.target.run(
            "cat /tmp/top.log | grep 'CPU' | grep 'idle' | "
            "awk '{print $8}' | "
            "awk -F '%' '{sum+=$1} END {print sum/NR}'")
        cpu_idle = float(output)
        cpu_idle = float("{0:.2f}".format(cpu_idle))
        cpu_used = str(100 - cpu_idle) + "%"
        collect_pnp_log(casename, casename, cpu_used)
        print "\n%s:%s\n" % (casename, cpu_used)
        self.assertEqual(status, 0, cpu_used)

        (status, output) = self.target.run(
            "cat /tmp/top.log")
        logname = casename + "-topinfo"
        collect_pnp_log(casename, logname, output)
