"""The suite launcher tests never call a real model or use real credentials."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

import pytest

from benchmarks import suite


def images(root: Path, names=("01.png", "01答案.png")) -> Path:
    directory = root / "tests" / "test-items"
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        (directory / name).write_bytes(b"offline-test-fixture")
    return directory


def json_file(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def child_result(command, *, status="completed", elapsed=120., input_hash="same-input", output_tokens=20):
    directory = Path(command[command.index("--out") + 1])
    case = command[command.index("--case-id") + 1]
    profile = command[command.index("--profile") + 1]
    job = "a" * 32
    json_file(directory / "result.json", {"case_id": case, "profile": profile, "job_id": job,
        "status": status, "end_to_end_seconds": elapsed, "model_calls": 3 if status == "completed" else None,
        "input_hash": input_hash, "effective_config": {"model": "fixture", "effort": "high"},
        "perf": {"model_stages": {"solve": {"output_tokens": output_tokens, "missing_usage": 0}}}})
    job_dir = directory / "runtime" / "jobs" / job
    json_file(job_dir / "report.json", {"status": status})
    page = job_dir / "output" / "result.html"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("<p>fixture</p>")
    return directory


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    monkeypatch.setattr(suite, "ROOT", tmp_path)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_REASONING_EFFORT", raising=False)
    monkeypatch.delenv("PREVIOUS_CHILD", raising=False)
    monkeypatch.setattr(suite, "preflight", lambda *_: None)
    images(tmp_path)
    (tmp_path / ".env").write_text("DEEPSEEK_API_KEY=offline-secret-example\nDEEPSEEK_REASONING_EFFORT=high\n", encoding="utf-8")
    return tmp_path


def test_number_pairing_and_alternating_order(tmp_path):
    directory = images(tmp_path, ("02.png", "02答案.png", "01.PNG", "01答案.PNG", "readme.txt"))
    cases = suite.discover(directory, ["all"])
    assert [c.id for c in cases] == ["01", "02"]
    plan = suite.make_plan(cases, ["A1", "B0"])
    assert [(r["case_id"], r["profile"]) for r in plan["runs"]] == [
        ("01", "A1"), ("01", "B0"), ("02", "B0"), ("02", "A1")]
    assert plan["runs"][0]["answer"].endswith("01答案.PNG")
    assert all(len(r["question_sha256"]) == 64 for r in plan["runs"])


def test_range_comma_and_deduplication(tmp_path):
    directory = images(tmp_path, ("01.png", "01答案.png", "02.png", "02答案.png", "03.png", "03答案.png"))
    assert [c.id for c in suite.discover(directory, ["01-02", "2,03"])] == ["01", "02", "03"]


@pytest.mark.parametrize("selector", ["../01", "all,01", "03-01", "0", "1-10002", "banana"])
def test_invalid_selection_rejected(tmp_path, selector):
    with pytest.raises(ValueError):
        suite.discover(images(tmp_path), [selector])


def test_missing_answer_requires_explicit_without_answer(tmp_path):
    directory = images(tmp_path, ("01.png",))
    with pytest.raises(ValueError, match="缺少答案"):
        suite.discover(directory, ["01"])
    assert suite.discover(directory, ["01"], True)[0].answer is None


def test_duplicate_numeric_image_is_not_chosen_arbitrarily(tmp_path):
    directory = images(tmp_path, ("01.png", "1.jpg", "01答案.png"))
    with pytest.raises(ValueError, match="重复"):
        suite.discover(directory, ["01"])


def test_symlink_cannot_escape_input_root(tmp_path):
    directory = images(tmp_path)
    external = tmp_path / "private.png"
    external.write_bytes(b"private")
    (directory / "02.png").symlink_to(external)
    with pytest.raises(ValueError, match="之外"):
        suite.discover(directory, ["all"])


def test_empty_or_pending_upload_is_not_success(tmp_path):
    with pytest.raises(ValueError, match="不存在"):
        suite.discover(tmp_path / "not-pushed", ["all"])
    directory = images(tmp_path, ())
    with pytest.raises(ValueError, match="没有发现"):
        suite.discover(directory, ["all"])


def test_dotenv_is_data_and_exported_values_win(tmp_path):
    path = tmp_path / ".env"
    path.write_text("\ufeff# comment\nexport TOKEN='private # token'\nE=\nNAME=你好 # comment\n"
                    'QUOTED="hello world"\nLITERAL=$(touch /never-execute)\nRAW=a#b\n', encoding="utf-8")
    env = suite.load_env(path, {"TOKEN": "from-shell", "E": ""})
    assert env["TOKEN"] == "from-shell" and env["E"] == ""
    assert env["NAME"] == "你好" and env["QUOTED"] == "hello world"
    assert env["LITERAL"] == "$(touch /never-execute)" and env["RAW"] == "a#b"


def test_dotenv_diagnostic_does_not_echo_secret(tmp_path):
    path = tmp_path / ".env"
    path.write_text("TOKEN='sensitive-unclosed\n")
    with pytest.raises(ValueError) as error:
        suite.load_env(path, {})
    assert "sensitive" not in str(error.value)


def test_dry_run_does_not_read_env_or_start_model(checkout, monkeypatch, capsys):
    def forbidden(*_):
        pytest.fail("preview must not create child processes or load credentials")
    monkeypatch.setattr(suite, "load_env", forbidden)
    monkeypatch.setattr(suite, "launch", forbidden)
    assert suite.main(["01"]) == 0
    assert "模型调用为0" in capsys.readouterr().out
    assert not (checkout / "benchmark-runs").exists()


def test_missing_key_blocks_before_paid_work(checkout, monkeypatch):
    (checkout / ".env").write_text("DEEPSEEK_API_KEY=\n")
    monkeypatch.setattr(suite, "launch", lambda *_: pytest.fail("no key"))
    assert suite.main(["01", "--run"]) == 2
    assert not (checkout / "benchmark-runs").exists()


def test_paired_run_uses_fresh_env_and_generates_links(checkout, monkeypatch):
    calls = []
    def launch(command, env, stop):
        assert env["DEEPSEEK_API_KEY"] == "offline-secret-example"
        assert "PREVIOUS_CHILD" not in env
        assert env["DEEPSEEK_REASONING_EFFORT"] == "high"
        env["PREVIOUS_CHILD"] = "must-not-leak"
        calls.append(command)
        child_result(command)
        return 0, "completed"
    monkeypatch.setattr(suite, "launch", launch)
    out = checkout / "comparison"
    assert suite.main(["01", "--run", "--out", str(out)]) == 0
    assert len(calls) == 2 and all("--allow-paid" in command for command in calls)
    assert all(command[:3] == [sys.executable, "-m", "benchmarks.run"] for command in calls)
    assert all(command[command.index("--answer") + 1].endswith("01答案.png") for command in calls)
    result = json.loads((out / "summary.json").read_text())
    assert len(result["runs"]) == 2 and result["runs"][0]["quality"] == "未人工评审"
    assert "讲解网页" in result["runs"][0]["links"]
    assert (out / "summary.html").is_file() and (out / "summary.md").is_file()
    assert "offline-secret-example" not in (out / "suite.json").read_text()


def test_task_failure_is_kept_without_retries(checkout, monkeypatch):
    calls = []
    def launch(command, env, stop):
        calls.append(command)
        failed = len(calls) == 1
        child_result(command, status="failed" if failed else "completed")
        return (1 if failed else 0), ""
    monkeypatch.setattr(suite, "launch", launch)
    out = checkout / "results"
    assert suite.main(["01", "--run", "--out", str(out)]) == 1
    assert len(calls) == 2
    summary = suite.report(out)
    assert summary["runs"][0]["model_calls"] is None
    assert "讲解网页" not in summary["runs"][0]["links"]
    assert summary["groups"]["A1"]["completed"] == 0


def test_launcher_failure_stops_batch_without_model_retry(checkout, monkeypatch):
    calls = []
    monkeypatch.setattr(suite, "launch", lambda command, *_: (calls.append(command) or (1, "broken import")))
    out = checkout / "results"
    assert suite.main(["01", "--run", "--out", str(out)]) == 1
    assert len(calls) == 1
    assert [r["status"] for r in suite.report(out)["runs"]] == ["missing_result", "not_run"]


def test_cancel_preserves_partial_report_and_does_not_start_next(checkout, monkeypatch):
    calls = []
    def launch(command, env, stop):
        calls.append(command)
        child_result(command, status="interrupted")
        stop.requested = True
        return 1, "stopped"
    monkeypatch.setattr(suite, "launch", launch)
    out = checkout / "cancelled"
    assert suite.main(["01", "--run", "--out", str(out)]) == 130
    assert len(calls) == 1
    assert [r["status"] for r in suite.report(out)["runs"]] == ["interrupted", "not_run"]


def test_existing_directory_is_never_overwritten_or_resumed(checkout, monkeypatch):
    out = checkout / "old-run"
    out.mkdir()
    monkeypatch.setattr(suite, "launch", lambda *_: pytest.fail("must not replay"))
    assert suite.main(["01", "--run", "--out", str(out)]) == 2
    assert list(out.iterdir()) == []


def test_changed_image_stops_before_next_request(checkout, monkeypatch):
    calls = []
    def launch(command, env, stop):
        calls.append(command)
        child_result(command)
        (checkout / "tests/test-items/01.png").write_bytes(b"changed")
        return 0, ""
    monkeypatch.setattr(suite, "launch", launch)
    out = checkout / "changed"
    assert suite.main(["01", "--run", "--out", str(out)]) == 2
    assert len(calls) == 1
    assert suite.report(out)["runs"][1]["status"] == "not_run"


def test_report_only_works_with_no_key_or_input_images(checkout, monkeypatch):
    out = checkout / "report-only"
    out.mkdir()
    plan = suite.make_plan(suite.discover(checkout / "tests/test-items", ["01"]), ["A1"])
    suite.atomic_json(out / "suite.json", plan)
    monkeypatch.setattr(suite, "launch", lambda *_: pytest.fail("report-only never runs a model"))
    monkeypatch.setattr(suite, "load_env", lambda *_: pytest.fail("no credentials required"))
    assert suite.main(["--report", str(out)]) == 0
    assert suite.main(["--report", str(out), "--run"]) == 2


def test_pair_setting_mismatch_is_visible(checkout, monkeypatch):
    def launch(command, env, stop):
        directory = child_result(command)
        path = directory / "result.json"
        value = json.loads(path.read_text())
        if value["profile"] == "B0":
            value["effective_config"]["effort"] = "low"
            value["input_hash"] = "changed"
        path.write_text(json.dumps(value))
        return 0, ""
    monkeypatch.setattr(suite, "launch", launch)
    out = checkout / "mismatch"
    assert suite.main(["01", "--run", "--out", str(out)]) == 0
    warnings = suite.report(out)["warnings"]
    assert any("effort" in w for w in warnings) and any("哈希" in w for w in warnings)


def test_exact_secret_is_scrubbed_from_child_output(tmp_path, monkeypatch):
    monkeypatch.setattr(suite, "ROOT", tmp_path)
    env = {**os.environ, "DEEPSEEK_API_KEY": "offline-secret-example"}
    rc, output = suite.launch([sys.executable, "-c", "import os; print(os.environ['DEEPSEEK_API_KEY'])"], env, suite.Stop())
    assert rc == 0 and "offline-secret-example" not in output and "REDACTED" in output


def test_cancel_signal_reaches_supervisor_not_an_automatic_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(suite, "ROOT", tmp_path)
    ready = tmp_path / "ready"
    code = ("import signal,time,pathlib\n"
            "stopped=False\n"
            "def stop(*a):\n global stopped\n stopped=True\n"
            "signal.signal(signal.SIGTERM,stop)\n"
            f"pathlib.Path({str(ready)!r}).write_text('ready')\n"
            "while not stopped: time.sleep(.01)\nprint('cleaned-up')\n")
    stop = suite.Stop()
    def cancel():
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        stop.handle()
    thread = threading.Thread(target=cancel, daemon=True)
    thread.start()
    rc, output = suite.launch([sys.executable, "-c", code], dict(os.environ), stop)
    thread.join(5)
    assert rc == 0 and "cleaned-up" in output and stop.requested


def test_one_checkout_cannot_run_two_suites(checkout):
    with suite.checkout_lock():
        with pytest.raises(ValueError, match="已有"):
            with suite.checkout_lock():
                pytest.fail("lock should exclude a second suite")


def test_preflight_failure_stops_before_paid_execution(tmp_path, monkeypatch):
    monkeypatch.setattr(suite.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 1, "bad offline-secret-example"))
    with pytest.raises(ValueError) as exc:
        suite.preflight({"DEEPSEEK_API_KEY": "offline-secret-example"}, {"profiles": ["A1"], "runs": []})
    assert "offline-secret-example" not in str(exc.value)
    assert "尚未调用模型" in str(exc.value)


def test_shell_wrapper_is_valid_and_preserves_arguments():
    wrapper = Path(suite.__file__).resolve().parents[1] / "scripts/test_compare.sh"
    subprocess.run(["bash", "-n", str(wrapper)], check=True)
    result = subprocess.run(["bash", str(wrapper), "--help"], env={**os.environ, "PYTHON": sys.executable},
                            capture_output=True, text=True, cwd="/tmp", timeout=10)
    assert result.returncode == 0 and "--run" in result.stdout


def test_all_rejects_answer_without_question(tmp_path):
    directory = images(tmp_path, ("01.png", "01答案.png", "02答案.png"))
    with pytest.raises(ValueError, match="只有答案"):
        suite.discover(directory, ["all"])


def test_result_file_cannot_escape_report_directory(checkout, monkeypatch):
    def launch(command, env, stop):
        directory = child_result(command)
        outside = checkout / "external.json"
        (directory / "result.json").replace(outside)
        (directory / "result.json").symlink_to(outside)
        return 0, ""
    monkeypatch.setattr(suite, "launch", launch)
    out = checkout / "unsafe"
    assert suite.main(["01", "--run", "--out", str(out)]) == 1
    summary = suite.report(out)
    assert summary["runs"][0]["status"] == "invalid_result"
    assert summary["groups"]["A1"]["failed_or_unknown"] == 1
    assert summary["groups"]["B0"]["not_run"] == 1
