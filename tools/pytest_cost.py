"""Record pytest collection and reported test phases without changing selection."""

from __future__ import annotations

import json
import resource
import sys
import time
from pathlib import Path

import pytest


def pytest_addoption(parser):
    parser.addoption("--cost-report", help="Write collection and test-phase timing as JSON")


def pytest_configure(config):
    if config.getoption("--cost-report"):
        config.pluginmanager.register(CostReport(config), "verification-cost")


class CostReport:
    def __init__(self, config):
        self.config = config
        self.started = time.perf_counter()
        self.selected = []
        self.collection = {}
        self.reports = []
        self.processes_started = 0
        self.observed_threads = 0
        self.workers = {}
        sys.addaudithook(self._audit)

    def _audit(self, event, args):
        if event == "subprocess.Popen":
            self.processes_started += 1

    def pytest_collection_finish(self, session):
        self.selected = [item.nodeid for item in session.items]
        self.collection["main"] = time.perf_counter() - self.started

    @pytest.hookimpl(optionalhook=True)
    def pytest_xdist_node_collection_finished(self, node, ids):
        self.selected = list(ids)
        self.collection[node.gateway.id] = time.perf_counter() - self.started

    def pytest_runtest_logreport(self, report):
        tasks = Path("/proc/self/task")
        if tasks.is_dir():
            self.observed_threads = max(self.observed_threads, len(list(tasks.iterdir())))
        self.reports.append(
            {
                "test": report.nodeid,
                "phase": report.when,
                "outcome": report.outcome,
                "seconds": report.duration,
                "worker": getattr(report, "worker_id", "main"),
            }
        )

    @pytest.hookimpl(optionalhook=True)
    def pytest_testnodedown(self, node, error):
        self.workers[node.gateway.id] = node.workeroutput.get("verification_cost")

    def pytest_sessionfinish(self, session, exitstatus):
        usage = resource.getrusage(resource.RUSAGE_SELF)
        process = {
            "subprocesses_started": self.processes_started,
            "observed_thread_max": self.observed_threads,
            "cpu_seconds": usage.ru_utime + usage.ru_stime,
        }
        if hasattr(self.config, "workerinput"):
            self.config.workeroutput["verification_cost"] = process
            return
        phases = {
            phase: sum(r["seconds"] for r in self.reports if r["phase"] == phase)
            for phase in ("setup", "call", "teardown")
        }
        Path(self.config.getoption("--cost-report")).write_text(
            json.dumps(
                {
                    "python": sys.version,
                    "executable": sys.executable,
                    "exit_status": int(exitstatus),
                    "selected_tests": self.selected,
                    "worker_startup_and_collection_seconds": self.collection,
                    "wall_seconds": time.perf_counter() - self.started,
                    "summed_test_phase_seconds": phases,
                    "test_reports": self.reports,
                    "processes": {"main": process, **self.workers},
                },
                indent=2,
            )
            + "\n"
        )
