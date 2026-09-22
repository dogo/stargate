"""The opencode wrapper's failure signalling, output filtering and surviving processes."""
from __future__ import annotations

import json
import os
import signal as signals
import subprocess
import time
from contextlib import suppress
from pathlib import Path

from stargate.stages import parse_review
from tests.harness import ROOT, fake_bin


def _event(part_id: str, text: str) -> str:
    return json.dumps({"type": "text", "part": {"type": "text", "id": part_id, "text": text}})


def run_wrapper(
    root: Path, script: str, *, env: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], str]:
    bindir = Path(fake_bin(root, "opencode"))
    (bindir / "opencode").write_text("#!/bin/sh\n" + script + "\n")
    temporary = root / "tmp"
    temporary.mkdir()
    output = root / "answer.txt"
    proc = subprocess.run(
        [str(ROOT / "examples/opencode/opencode-stargate"), str(output),
         "--agent", "plan", "--format", "json", "p"],
        env={"PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}", "HOME": str(root),
             "TMPDIR": str(temporary), **(env or {})},
        text=True, encoding="utf-8", capture_output=True, timeout=60,
    )
    assert not list(temporary.iterdir()), proc.stdout + proc.stderr
    return proc, output.read_text(encoding="utf-8") if output.is_file() else ""


def test_an_unwritable_answer_cannot_turn_a_successful_request_into_success(root: Path) -> None:
    for status in (0, 17):
        case = root / str(status)
        case.mkdir()
        # A directory fails writes even when the suite runs as root.
        (case / "answer.txt").mkdir()
        proc, _ = run_wrapper(case, f"cat <<'EOF'\n{_event('reply', 'reply')}\nEOF\nexit {status}")
        assert proc.returncode != 0, proc.stdout + proc.stderr
        if status:
            assert proc.returncode == status, proc.stdout + proc.stderr


def test_a_failing_opencode_exits_nonzero_and_keeps_its_diagnostic_out_of_the_answer(
    root: Path,
) -> None:
    proc, answer = run_wrapper(root, 'echo "authentication failed" >&2; exit 17')
    assert proc.returncode == 17, proc.stdout + proc.stderr
    assert "authentication failed" in proc.stderr, proc.stdout + proc.stderr
    assert (root / "answer.txt").is_file(), proc.stdout + proc.stderr
    assert answer == "", proc.stdout + proc.stderr


def test_markdown_in_the_reply_reaches_the_answer_untouched(root: Path) -> None:
    reply = (
        "> plan · google/gemini-3.6-flash\n> quoted text from the diff\n• a bullet\n"
        "✱ Glob *.py\n→ Read AGENTS.md\n← Write impl.txt\n"
        "✓ checked\n● another bullet\n⋮ ordinary prose\n◆ a future marker\n"
        "\x1b[32mVERDICT: APPROVED\x1b[0m\n"
    )
    proc, answer = run_wrapper(root, f"cat <<'EOF'\n{_event('reply', reply)}\nEOF")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert answer == reply, proc.stdout + proc.stderr
    assert proc.stdout == answer, proc.stdout + proc.stderr


def test_a_tool_result_before_the_final_answer_cannot_push_a_json_review_off_the_first_line(
    root: Path,
) -> None:
    review = json.dumps({"verdict": "APPROVED", "findings": []})
    tool = json.dumps({"type": "tool_use", "part": {
        "id": "tool", "type": "tool", "state": {"output": "Wrote file successfully."},
    }})
    proc, answer = run_wrapper(root, f"cat <<'EOF'\n{tool}\n{_event('reply', review)}\nEOF")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert answer == review + "\n", proc.stdout + proc.stderr
    assert answer.startswith("{"), proc.stdout + proc.stderr
    assert parse_review(answer) == ("APPROVED", [], "json"), answer


def test_pre_tool_narration_cannot_prefix_the_final_json_review(root: Path) -> None:
    review = json.dumps({"verdict": "APPROVED", "findings": []})
    events = "\n".join([
        _event("first", "Let me read the file first."),
        '{"type": "tool_use", "part": {"type": "tool"}}',
        _event("second", "Let me check one more file."),
        '{"type": "tool_use", "part": {"type": "tool"}}',
        _event("first", review[:10]),
        _event("last", review[10:]),
        _event("first", review[:10]),
    ])
    proc, answer = run_wrapper(root, f"cat <<'EOF'\n{events}\nEOF")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert answer == review + "\n", proc.stdout + proc.stderr
    assert proc.stdout == answer, proc.stdout + proc.stderr
    assert parse_review(answer) == ("APPROVED", [], "json"), answer


def test_a_tool_event_after_the_final_answer_does_not_empty_the_output(root: Path) -> None:
    review = json.dumps({"verdict": "APPROVED", "findings": []})
    events = "\n".join([
        _event("reply", review),
        '{"type": "tool_use", "part": {"type": "tool"}}',
        '{"part": {"type": "tool"}}',
        '{"part": {"type": "text", "text": null}}',
        '{"type": "step_finish"}',
    ])
    proc, answer = run_wrapper(root, f"cat <<'EOF'\n{events}\nEOF")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert answer == review + "\n", proc.stdout + proc.stderr
    assert proc.stdout == answer, proc.stdout + proc.stderr
    assert parse_review(answer) == ("APPROVED", [], "json"), answer


def test_text_after_the_last_tool_use_wins_over_an_earlier_complete_answer(root: Path) -> None:
    # With a tool call between two text parts, a superseded draft and a closing
    # remark are indistinguishable. Later text wins by design: preserving the
    # earlier text would let interim narration break a final JSON review.
    review = json.dumps({"verdict": "APPROVED", "findings": []})
    events = "\n".join([
        _event("first", review),
        '{"type": "tool_use", "part": {"type": "tool"}}',
        _event("second", "Let me know if you want more detail."),
    ])
    proc, answer = run_wrapper(root, f"cat <<'EOF'\n{events}\nEOF")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert answer == "Let me know if you want more detail.\n", proc.stdout + proc.stderr
    assert proc.stdout == answer, proc.stdout + proc.stderr


def test_growing_text_without_a_string_id_does_not_duplicate_the_json_review(root: Path) -> None:
    review = json.dumps({"verdict": "APPROVED", "findings": []})
    for index, fields in enumerate(({}, {"id": None}, {"id": 42}, {"id": []})):
        case = root / str(index)
        case.mkdir()
        events = "\n".join(
            json.dumps({"part": {"type": "text", "text": text, **fields}})
            for text in (review[:10], review)
        )
        proc, answer = run_wrapper(case, f"cat <<'EOF'\n{events}\nEOF")
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert answer == review + "\n", proc.stdout + proc.stderr
        assert parse_review(answer) == ("APPROVED", [], "json"), answer


def test_invalid_diagnostic_bytes_preserve_utf8_answers_and_the_request_exit_status(
    root: Path,
) -> None:
    reply = "Revisão ✓"
    # Literal UTF-8 tests input decoding as well as output under an ASCII locale.
    event = json.dumps(json.loads(_event("reply", reply)), ensure_ascii=False)
    for status in (0, 17):
        case = root / str(status)
        case.mkdir()
        script = (
            "printf 'invalid \\377 diagnostic\\n' >&2\n"
            # Exceed pipe buffering: a reader that dies early interrupts the writer.
            "i=0; while [ $i -lt 4096 ]; do echo '{}'; i=$((i + 1)); done\n"
            f"cat <<'EOF'\n{event}\nEOF\nexit {status}"
        )
        proc, answer = run_wrapper(case, script, env={
            "LC_ALL": "C", "PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0",
            "PYTHONIOENCODING": "ascii:strict",
        })
        assert proc.returncode == status, proc.stdout + proc.stderr
        assert answer == reply + "\n", proc.stdout + proc.stderr
        assert proc.stdout == answer, proc.stdout + proc.stderr
        assert "invalid \\xff diagnostic" in proc.stderr, proc.stdout + proc.stderr


def test_a_text_part_reemitted_as_it_grows_is_not_duplicated_in_the_answer(root: Path) -> None:
    events = "\n".join([
        _event("first", "VERD"), _event("second", ": APPROVED"), _event("first", "VERDICT"),
    ])
    proc, answer = run_wrapper(root, f"cat <<'EOF'\n{events}\nEOF")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert answer == "VERDICT: APPROVED\n", proc.stdout + proc.stderr


def test_a_stray_non_json_line_does_not_crash_the_wrapper_or_reach_the_answer(root: Path) -> None:
    events = "\n".join([
        _event("first", "VERDICT"), "stray diagnostic", "[]", "null", '"string"', "42",
        '{"part": null}', '{"part": {"type": "text", "text": 42}}',
        _event("second", ": APPROVED"),
    ])
    proc, answer = run_wrapper(root, f"cat <<'EOF'\n{events}\nEOF")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert answer == "VERDICT: APPROVED\n", proc.stdout + proc.stderr
    for diagnostic in ("stray diagnostic", "[]", "null", '"string"', "42"):
        assert diagnostic in proc.stderr, proc.stdout + proc.stderr


def test_a_stream_without_assistant_text_leaves_an_empty_answer_for_stargate_to_reject(
    root: Path,
) -> None:
    proc, answer = run_wrapper(root, """cat <<'EOF'
{"type": "step_start", "part": {"type": "step-start"}}
{"type": "tool_use", "part": {"type": "tool", "text": "Wrote file successfully."}}
{"type": "step_finish", "part": {"type": "step-finish"}}
EOF""")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert answer == "", proc.stdout + proc.stderr
    assert (root / "answer.txt").is_file(), proc.stdout + proc.stderr
    assert proc.stdout == "", proc.stdout + proc.stderr


def test_opencode_is_still_handed_a_pipe_and_not_a_regular_file(root: Path) -> None:
    proc, answer = run_wrapper(root, """[ -p /dev/stdout ] && msg=done || msg=REGULAR-FILE
printf '{"part":{"type":"text","id":"reply","text":"%s"}}\\n' "$msg"
""")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert answer == "done\n", proc.stdout + proc.stderr


def test_interrupting_the_wrapper_removes_its_private_fifo_directory(root: Path) -> None:
    for signal, status in (("INT", 130), ("TERM", 143), ("HUP", 129)):
        case = root / signal
        case.mkdir()
        proc, _ = run_wrapper(case, f'kill -{signal} "$PPID"; echo done')
        assert proc.returncode == status, proc.stdout + proc.stderr


def test_signalling_the_wrapper_leaves_no_opencode_still_running_and_billing(root: Path) -> None:
    _assert_signalled_request_stops(root, new_session=True)


def test_signalling_a_nonleader_wrapper_stops_its_direct_opencode_request(root: Path) -> None:
    _assert_signalled_request_stops(root, new_session=False)


def _assert_signalled_request_stops(root: Path, *, new_session: bool) -> None:
    for signum, status in ((signals.SIGINT, 130), (signals.SIGTERM, 143), (signals.SIGHUP, 129)):
        case = root / str(signum)
        case.mkdir()
        bindir = Path(fake_bin(case, "opencode"))
        script = (
            'sleep 30 &\nprintf "%s\\n%s\\n" "$$" "$!" >"$PIDS"\nwait\n'
            if new_session else 'echo "$$" >"$PIDS"\nexec sleep 30\n'
        )
        (bindir / "opencode").write_text("#!/bin/sh\n" + script)
        expected_pids = 2 if new_session else 1
        temporary = case / "tmp"
        temporary.mkdir()
        pidfile = case / "pids"
        with (case / "trace").open("w+") as trace:
            child = subprocess.Popen(
                [str(ROOT / "examples/opencode/opencode-stargate"), str(case / "answer"), "p"],
                env={"PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}", "HOME": str(case),
                     "TMPDIR": str(temporary), "PIDS": str(pidfile)},
                stdin=subprocess.DEVNULL, stdout=trace, stderr=subprocess.STDOUT,
                start_new_session=new_session,
            )
            pids = []
            try:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if pidfile.exists():
                        pids = [int(pid) for pid in pidfile.read_text().splitlines()]
                        if len(pids) == expected_pids:
                            break
                    time.sleep(0.05)
                # Signal only the wrapper: signalling the group would hide the bug.
                child.send_signal(signum)
                child.wait(timeout=10)
                trace.seek(0)
                proc = subprocess.CompletedProcess(child.args, child.returncode, trace.read())
                assert len(pids) == expected_pids, proc.stdout
                assert proc.returncode == status, proc.stdout
                assert not list(temporary.iterdir()), proc.stdout
                remaining = set(pids)
                deadline = time.monotonic() + 5
                while remaining and time.monotonic() < deadline:
                    for pid in tuple(remaining):
                        try:
                            os.kill(pid, 0)
                        except ProcessLookupError:
                            remaining.remove(pid)
                            continue
                        # A zombie cannot run or bill; PID 1 may delay reaping it.
                        try:
                            stat = Path(f"/proc/{pid}/stat").read_text()
                        except FileNotFoundError:
                            continue
                        if stat.rsplit(")", 1)[1].split()[0] == "Z":
                            remaining.remove(pid)
                    if remaining:
                        time.sleep(0.05)
                assert not remaining, proc.stdout + f"\nsurviving opencode pids: {remaining}"
            finally:
                # Also clean up the filter if the regression fails before wait returns.
                if new_session:
                    with suppress(ProcessLookupError):
                        os.killpg(child.pid, signals.SIGKILL)
                else:
                    for pid in pids:
                        with suppress(ProcessLookupError):
                            os.kill(pid, signals.SIGKILL)
                    if child.poll() is None:
                        child.kill()
                child.wait(timeout=10)
