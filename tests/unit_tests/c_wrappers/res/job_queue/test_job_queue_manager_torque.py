import os
import stat
from dataclasses import dataclass
from pathlib import Path
from threading import BoundedSemaphore
from typing import Callable, TypedDict

import pytest

from ert._c_wrappers.job_queue import Driver, JobQueueNode, QueueDriverEnum
from ert._clib.model_callbacks import LoadStatus


@pytest.fixture(name="temp_working_directory")
def fixture_temp_working_directory(tmpdir):
    old_cwd = os.getcwd()
    os.chdir(tmpdir)
    yield tmpdir

    os.chdir(old_cwd)


@pytest.fixture(name="dummy_config")
def fixture_dummy_config():
    return JobConfig(
        {
            "job_script": "job_script.py",
            "num_cpu": 1,
            "job_name": "dummy_job_{}",
            "run_path": "dummy_path_{}",
            "ok_callback": dummy_ok_callback,
            "exit_callback": dummy_exit_callback,
        }
    )


@dataclass
class RunArg:
    iens: int


class JobConfig(TypedDict):
    job_script: str
    num_cpu: int
    job_name: str
    run_path: str
    ok_callback: Callable
    exit_callback: Callable


def dummy_ok_callback(args):
    (Path(args[1]) / "OK").write_text("success", encoding="utf-8")
    return (LoadStatus.LOAD_SUCCESSFUL, "")


def dummy_exit_callback(_args):
    Path("ERROR").write_text("failure", encoding="utf-8")


SIMPLE_SCRIPT = """#!/bin/sh
echo "finished successfully" > STATUS
"""

# This script is susceptible to race conditions. Python works
# better than sh.
FAILING_SCRIPT = """#!/usr/bin/env python
import sys
with open("one_byte_pr_invocation", "a") as f:
    f.write(".")
sys.exit(1)
"""

MOCK_QSUB = """#!/bin/sh
echo "torque job submitted" > job_output
echo "$@" >> job_output
echo "10001.s034-lcam"
exit 0
"""

# A qsub shell script that will fail on the first invocation, but succeed on the
# second (by persisting its state in the current working directory)
FLAKY_QSUB = """#!/bin/sh
if [ -s firstwasflaky ]; then
    echo ok > job_output
    echo "10001.s034-lcam"
    exit 0
fi
echo "it was" > firstwasflaky
exit 1
"""


def create_qstat_output(
    state: str, job_id: str = "10001.s034-lcam", bash=False, bashindent=""
):
    assert len(state) == 1
    mocked_output = f"""Job id            Name             User              Time Use S Queue
----------------  ---------------- ----------------  -------- - -----
{job_id: <16}  MyMockedJob      rms                      0 {state:<1} normal   100
"""  # noqa
    if bash:
        return "\n".join(
            [f'{bashindent}echo "{line}"' for line in mocked_output.splitlines()]
        )
    return mocked_output


# A qstat script that works as expected:
MOCK_QSTAT = "#!/bin/sh\n" + create_qstat_output(state="E", bash=True)

# A qstat shell script that will fail on the first invocation, but succeed on
# the second (by persisting its state in the current working directory)
FLAKY_QSTAT = (
    """#!/bin/sh
sleep 1
if [ -s firstwasflaky ]; then
"""
    + create_qstat_output(state="E", bash=True, bashindent="    ")
    + """
    exit 0
fi
echo "it was" > firstwasflaky
# These stderr messages should be swallowed and muted by driver:
if [ $RANDOM -le 10000 ]; then
    echo "qstat: Invalid credential 10001.s034-lcam" >&2
else
    echo "qstat: Invalid credential" >&2
fi
exit 1
"""
)


def _deploy_script(scriptname: Path, scripttext: str):
    script = Path(scriptname)
    script.write_text(scripttext, encoding="utf-8")
    script.chmod(stat.S_IRWXU)


def _build_jobqueuenode(dummy_config: JobConfig, job_id=0):
    runpath = Path(dummy_config["run_path"].format(job_id))
    runpath.mkdir()

    job = JobQueueNode(
        job_script=dummy_config["job_script"],
        job_name=dummy_config["job_name"].format(job_id),
        run_path=os.path.realpath(dummy_config["run_path"].format(job_id)),
        num_cpu=1,
        status_file="STATUS",
        ok_file="OK",
        exit_file="ERROR",
        done_callback_function=dummy_config["ok_callback"],
        exit_callback_function=dummy_config["exit_callback"],
        callback_arguments=[
            RunArg(iens=job_id),
            Path(dummy_config["run_path"].format(job_id)).resolve(),
        ],
    )
    return (job, runpath)


@pytest.mark.parametrize(
    "qsub_script, qstat_script",
    [
        pytest.param(MOCK_QSUB, MOCK_QSTAT, id="none_flaky"),
        pytest.param(
            MOCK_QSUB.replace(".s034-lcam", ""),
            MOCK_QSTAT.replace(".s034-lcam", ""),
            id="none_flaky_no_namespace",
        ),
        pytest.param(FLAKY_QSUB, MOCK_QSTAT, id="flaky_qsub"),
        pytest.param(MOCK_QSUB, FLAKY_QSTAT, id="flaky_qstat"),
        pytest.param(FLAKY_QSUB, FLAKY_QSTAT, id="all_flaky"),
    ],
)
def test_run_torque_job(
    temp_working_directory, dummy_config, qsub_script, qstat_script
):
    # pylint: disable=unused-argument
    """Verify that the torque driver will succeed in submitting and
    monitoring torque jobs even when the Torque commands qsub and qstat
    are flaky.

    A flaky torque command is a shell script that sometimes but not
    always returns with a non-zero exit code."""

    _deploy_script(dummy_config["job_script"], SIMPLE_SCRIPT)
    _deploy_script("qsub", qsub_script)
    _deploy_script("qstat", qstat_script)

    driver = Driver(
        driver_type=QueueDriverEnum.TORQUE_DRIVER,
        options=[("QSTAT_CMD", temp_working_directory / "qstat")],
    )

    (job, runpath) = _build_jobqueuenode(dummy_config)
    job.run(driver, BoundedSemaphore())
    job.wait_for()

    # This file is supposed created by the job that the qsub script points to,
    # but here it is created by the mocked qsub.
    assert Path("job_output").exists()

    # The "done" callback:
    assert (runpath / "OK").read_text(encoding="utf-8") == "success"


def test_that_torque_driver_passes_dash_x_to_qstat(
    temp_working_directory, dummy_config
):
    # pylint: disable=unused-argument
    """-x is a default option in the driver for the qstat option,
    making qstat also display information about finished jobs."""

    _deploy_script(dummy_config["job_script"], SIMPLE_SCRIPT)
    _deploy_script("qsub", MOCK_QSUB)
    _deploy_script(
        "qstat",
        "#!/bin/bash\n"
        + create_qstat_output(state="E", bash=True)
        + "\n"
        + "echo $@ > qstat_options",
    )

    driver = Driver(
        driver_type=QueueDriverEnum.TORQUE_DRIVER,
        options=[("QSTAT_CMD", temp_working_directory / "qstat")],
    )

    job, _runpath = _build_jobqueuenode(dummy_config)
    job.run(driver, BoundedSemaphore())
    job.wait_for()
    # qstat job id = 10001
    assert Path("qstat_options").read_text(encoding="utf-8").strip() == "-x 10001"
