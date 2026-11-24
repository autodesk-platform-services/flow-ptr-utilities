import os
import subprocess
import sys

import maya.cmds as cmds

import tank
from tank import Hook
from tank import TankError


class PostSubmitHook(Hook):
    def execute(self, app, outputs, **kwargs):
        '''
        app:        main class app
        outputs:    list of dicts with the following keys:

                    output:     Dict
                                Dictionary with all data from the outputs:

                                name:         String
                                              Name of output in the environment.

                                tank_type:    String
                                              Tank type specified in the environment.

                    <attr1>:     String, Int or Bool

                    <attr2>:     String, Int or Bool
        '''

        self.app = app
        self.outputs = outputs

        results = []

        for output in self.outputs:
            errors = []
            if output['output']['name'] == "maya_render":
                try:
                    self.maya_render(output)
                except Exception as e:
                    errors.append("Submit failed - %s" % e)

            # if there is anything to report then add to result
            if len(errors) > 0:
                # add result:
                results.append({"output": output, "errors": errors})

        return results

    # Queue name (from the pre-submit hook) -> Backburner priority (0-100).
    QUEUE_PRIORITY = {"high": 80, "mid": 50, "low": 20}

    def _find_cmdjob(self):
        """Locate Backburner's cmdjob. Override with env var BACKBURNER_CMDJOB."""
        candidates = [
            os.environ.get("BACKBURNER_CMDJOB"),
            "/opt/Autodesk/backburner/cmdjob",                              # macOS
            "/usr/discreet/backburner/cmdjob",                              # Linux
            r"C:\Program Files (x86)\Autodesk\Backburner\cmdjob.exe",       # Windows
        ]
        for path in candidates:
            if path and os.path.exists(path):
                return path
        raise TankError("Backburner cmdjob not found. Install Backburner or set BACKBURNER_CMDJOB.")

    def _find_render(self):
        """Locate Maya's command-line renderer next to the running Maya."""
        exe = "Render.exe" if sys.platform == "win32" else "Render"
        path = os.path.join(os.environ.get("MAYA_LOCATION", ""), "bin", exe)
        if not os.path.exists(path):
            raise TankError("Maya Render executable not found at %s" % path)
        return path

    def maya_render(self, output):
        '''Method for submitting to the render farm (Autodesk Backburner).

        Submits one task per frame. The Manager defaults to localhost;
        override with env var BACKBURNER_MANAGER.
        '''
        print("tk-multi-renderfarm: maya_render called with output: %s" % output)

        if not output.get("submit", True):
            print("tk-multi-renderfarm: 'Submit Job' unchecked, skipping.")
            return

        start, end, by = int(output["start"]), int(output["end"]), int(output["by"])
        if by < 1 or end < start:
            raise TankError("Invalid frame range: %s-%s by %s" % (start, end, by))
        num_tasks = (end - start) // by + 1

        cmd = [
            self._find_cmdjob(),
            "-jobName:%s" % output["jobname"],
            "-manager:%s" % os.environ.get("BACKBURNER_MANAGER", "localhost"),
            "-priority:%d" % self.QUEUE_PRIORITY.get(output.get("queue"), 50),
            "-description:tk-multi-renderfarm Maya render",
            "-leaveInQueue",
            "-tp_start:%d" % start,
            "-tp_jump:%d" % by,
            "-numTasks:%d" % num_tasks,
            self._find_render(),
            "-s", "%tp1", "-e", "%tp1",
            output["submit_file"],
        ]
        print("tk-multi-renderfarm: running %s" % " ".join(cmd))

        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                universal_newlines=True)
        out, _ = proc.communicate()
        print(out)
        if proc.returncode != 0 or "Successfully submitted" not in out:
            raise TankError("cmdjob failed (exit %s): %s" % (proc.returncode, out.strip()[-500:]))
