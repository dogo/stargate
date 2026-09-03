"""Process and signal safety regressions for concurrent fan-out agents."""

from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import yaml

from tests.harness import ROOT, make_repo, run, write_config, write_fanout_config


def _write_graph(path: Path, task_ids: tuple[str, ...]) -> None:
    path.write_text(
        json.dumps(
            {
                "name": "signal safety",
                "tasks": [
                    {"id": task_id, "task": f"run {task_id}", "depends_on": []}
                    for task_id in task_ids
                ],
            }
        )
    )


def _start(repo: Path, config: Path, task: str = "signal fanout") -> subprocess.Popen[str]:
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "stargate",
            "--config",
            str(config),
            "run",
            "--fan-out",
            task,
        ],
        cwd=repo,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )


def _pid_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    stat = Path(f"/proc/{pid}/stat")
    if stat.exists():
        # A killed grandchild can briefly remain as a zombie until init reaps
        # it. It cannot execute or retain resources beyond its process entry.
        return stat.read_text().split()[2] != "Z"
    return True


def _wait_for_lines(path: Path, count: int, proc: subprocess.Popen[str]) -> list[str]:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and proc.poll() is None:
        if path.exists():
            lines = path.read_text().splitlines()
            if len(lines) >= count:
                return lines
        time.sleep(0.05)
    out, err = proc.communicate(timeout=10)
    raise AssertionError(f"agents did not start\nstdout:\n{out}\nstderr:\n{err}")


def test_sigterm_kills_parallel_agent_process_groups(root: Path) -> None:
    repo = make_repo(root)
    graph = root / "tasks.json"
    _write_graph(graph, ("alpha", "beta"))
    pids = root / "processes.txt"
    finished = root / "grandchild-finished.txt"
    grandchild = (
        "import pathlib,time; time.sleep(30); "
        f"pathlib.Path({str(finished)!r}).open('a').write('done\\n')"
    )
    parent = (
        "import os,pathlib,subprocess,sys,time; "
        f"child=subprocess.Popen([sys.executable,'-c',{grandchild!r}]); "
        f"path=pathlib.Path({str(pids)!r}); "
        "fd=os.open(str(path),os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o644); "
        "os.write(fd,f'{os.getpid()} {child.pid}\\n'.encode()); os.close(fd); "
        "time.sleep(30)"
    )
    config = root / "fanout.yaml"
    write_fanout_config(config, graph, "echo unused")
    data = yaml.safe_load(config.read_text())
    data["agents"]["dev"]["command"] = [sys.executable, "-c", parent]
    data["settings"]["agent_timeout_seconds"] = 60
    config.write_text(yaml.safe_dump(data))

    proc = _start(repo, config)
    lines = _wait_for_lines(pids, 2, proc)
    started = time.monotonic()
    proc.send_signal(signal.SIGTERM)
    out, err = proc.communicate(timeout=15)

    assert proc.returncode == 143, out + err
    assert time.monotonic() - started < 5, out + err
    assert "Resume with: stargate resume" in err, err
    state_path = next((repo / ".stargate" / "runs").glob("*/state.json"))
    assert json.loads(state_path.read_text())["status"] == "failed"
    all_pids = [int(pid) for line in lines for pid in line.split()]
    deadline = time.monotonic() + 5
    while any(_pid_running(pid) for pid in all_pids) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not [pid for pid in all_pids if _pid_running(pid)]
    time.sleep(0.2)
    assert not finished.exists(), "an agent grandchild continued after SIGTERM"


def test_sigterm_interrupts_retry_backoff(root: Path) -> None:
    repo = make_repo(root)
    graph = root / "tasks.json"
    _write_graph(graph, ("alpha",))
    calls = root / "calls.txt"
    config = root / "retry.yaml"
    write_fanout_config(
        config,
        graph,
        f"echo call >> {calls}; echo retry-me >&2; exit 7",
        max_parallel=1,
    )
    data = yaml.safe_load(config.read_text())
    data["settings"].update(
        agent_retries=2,
        agent_retry_backoff_seconds=30,
        agent_timeout_seconds=60,
    )
    config.write_text(yaml.safe_dump(data))

    proc = _start(repo, config, "retry interruption")
    assert proc.stdout is not None
    seen: list[str] = []
    lines: queue.Queue[str] = queue.Queue()

    def read_stdout() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.put(line)

    reader = threading.Thread(target=read_stdout, daemon=True)
    reader.start()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and proc.poll() is None:
        try:
            line = lines.get(timeout=0.1)
        except queue.Empty:
            continue
        seen.append(line)
        if "retrying in 30s" in line:
            break
    else:
        proc.kill()
        proc.wait(timeout=10)
        reader.join(timeout=2)
        while not lines.empty():
            seen.append(lines.get_nowait())
        assert proc.stderr is not None
        err = proc.stderr.read()
        raise AssertionError(
            f"agent did not enter retry backoff\n{''.join(seen)}\n{err}"
        )

    started = time.monotonic()
    proc.send_signal(signal.SIGTERM)
    proc.wait(timeout=10)
    reader.join(timeout=2)
    while not lines.empty():
        seen.append(lines.get_nowait())
    assert proc.stderr is not None
    err = proc.stderr.read()
    out = "".join(seen)
    assert proc.returncode == 143, out + err
    assert time.monotonic() - started < 3, out + err
    assert calls.read_text().splitlines() == ["call"]
    assert "Resume with: stargate resume" in err, err


def test_parallel_agent_output_has_task_attribution(root: Path) -> None:
    repo = make_repo(root)
    graph = root / "tasks.json"
    _write_graph(graph, ("alpha", "beta"))
    config = root / "output.yaml"
    developer = (
        'case "$0" in *"TASK ID: alpha"*) echo alpha > alpha.txt ;; '
        '*"TASK ID: beta"*) echo beta > beta.txt ;; esac; '
        'sleep 0.2; echo "tokens used 7"'
    )
    write_fanout_config(config, graph, developer)
    data = yaml.safe_load(config.read_text())
    data["agents"]["dev"]["usage_pattern"] = r"tokens used (\d+)"
    config.write_text(yaml.safe_dump(data))

    proc = run(repo, config, "attributed output", "--fan-out")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    for task_id in ("alpha", "beta"):
        label = f"[task {task_id}/developer]"
        assert f"{label} trace: tail -f" in proc.stdout, proc.stdout
        assert f"{label} $ " in proc.stdout, proc.stdout
        assert f"{label} exit 0 in" in proc.stdout, proc.stdout
        assert f"{label} reported 7 tokens" in proc.stdout, proc.stdout
    state_path = next((repo / ".stargate" / "runs").glob("*/state.json"))
    state = json.loads(state_path.read_text())
    assert state["tokens_used"] == 14, state
    assert {
        task_id: record["tokens_used"]
        for task_id, record in state["fanout"]["tasks"].items()
    } == {"alpha": 7, "beta": 7}

    linear = root / "linear.yaml"
    write_config(linear, 'echo "VERDICT: APPROVED"', test_command="true")
    linear_run = run(repo, linear, "linear output")
    assert linear_run.returncode == 0, linear_run.stdout + linear_run.stderr
    assert "[task " not in linear_run.stdout
    assert "\ntrace: tail -f " in linear_run.stdout
    assert "\n$ /bin/sh -c " in linear_run.stdout


def test_killed_attempt_usage_is_charged_once(root: Path) -> None:
    repo = make_repo(root)
    helper = root / "usage_worker.py"
    helper.write_text(
        """
import pathlib
import sys
import threading
import time

from stargate.agent import invoke_agent
from stargate.core import RunContext, StargateError, terminate_active_processes

root = pathlib.Path(sys.argv[1])
repo = pathlib.Path(sys.argv[2])
started = root / "started.txt"
agent_code = (
    "import pathlib,time; print('tokens used 37', flush=True); "
    f"pathlib.Path({str(started)!r}).write_text('yes'); time.sleep(30)"
)
config = {
    "agents": {"dev": {"command": [sys.executable, "-c", agent_code],
                         "usage_pattern": r"tokens used (\\d+)"}},
    "workflow": {"developer": "dev"},
    "settings": {"agent_timeout_seconds": 60},
}
ctx = RunContext(repo, config, "run", "alpha", "branch", "main", "base",
                 repo, root, mode="fanout-task")
errors = []

def work():
    try:
        invoke_agent(ctx, "developer", "prompt", repo, root / "developer.txt")
    except StargateError as exc:
        errors.append(str(exc))

thread = threading.Thread(target=work)
thread.start()
deadline = time.monotonic() + 10
while not started.exists() and time.monotonic() < deadline:
    time.sleep(0.02)
if not started.exists():
    raise RuntimeError("agent never started")
terminate_active_processes()
thread.join(timeout=5)
print(f"used={ctx.tokens_used} errors={len(errors)} alive={thread.is_alive()}")
"""
    )
    proc = subprocess.run(
        [sys.executable, str(helper), str(root), str(repo)],
        text=True,
        capture_output=True,
        timeout=15,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "used=37 errors=1 alive=False" in proc.stdout, proc.stdout
    assert proc.stdout.count("reported 37 tokens") == 1, proc.stdout
