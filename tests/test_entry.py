"""entry.py control flow, with the pipeline and S3 replaced by small fakes (no AWS, no GPU).

The fakes mirror the real signatures in rcs-workflow-processor's parking.s3, parking.resources
and parking.runner, which entry.py calls.
"""

import importlib
import json
import os
import sys
import textwrap

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FAKE_S3 = '''
import os
STATE = {"puts": {}, "upload_short": False}
class Client:
    def put_object(self, Bucket, Key, Body, ContentType=None):
        STATE["puts"][Key] = Body
def s3_client():
    return Client()
def list_image_uris(uri, client=None):
    assert uri.endswith("/"), uri
    STATE["listed"] = uri
    return [uri + f"img{i}.JPG" for i in range(3)]
def download_images(uris, dest, *, workers=16, client=None, progress_cb=None):
    os.makedirs(dest, exist_ok=True)
    STATE["dl_workers"] = workers
    for u in uris:
        open(os.path.join(dest, u.rsplit("/", 1)[1]), "w").write("x")
    return len(uris), []
def upload_directory(local, uri, *, workers=0, client=None):
    STATE["up_uri"], STATE["up_workers"] = uri, workers
    files = [os.path.join(d, f) for d, _, fs in os.walk(local) for f in fs]
    return uri, files[:-1] if STATE["upload_short"] else files
'''

FAKE_RESOURCES = '''
GPU = {"on": True}
def plan_resources():
    return {"batch_size": 32, "decode_workers": 4, "annotate_workers": 2, "half": True,
            "hw": {"gpu": GPU["on"], "gpu_name": "Tesla T4" if GPU["on"] else None, "vram_gb": 14.6, "cpu": 4}}
'''

FAKE_CONFIG = '''
class PipelineConfig:
    def __init__(self, **kw):
        self.__dict__.update(kw)
'''

FAKE_RUNNER = '''
import json, os
def _run_pipeline(cfg):
    assert len(os.listdir(cfg.image_dir)) == 3
    os.makedirs(os.path.join(cfg.outputs_dir, "spots", "bay1_spot1"))
    doc = {"metadata": {"n_frames": 3, "n_detections_total": 9, "geo": {"calibration": {"source": "features"}}},
           "bays": [{}], "spots": [{}, {}]}
    json.dump(doc, open(os.path.join(cfg.outputs_dir, "occupied_bays.json"), "w"))
    open(os.path.join(cfg.outputs_dir, "spots", "bay1_spot1", "a.jpeg"), "w").write("x")
    return doc
'''


@pytest.fixture()
def entry(tmp_path, monkeypatch):
    pkg = tmp_path / "parking_occupancy" / "parking"
    pkg.mkdir(parents=True)
    (tmp_path / "parking_occupancy" / "__init__.py").write_text("")
    (pkg / "__init__.py").write_text("")
    for name, src in (("s3", FAKE_S3), ("resources", FAKE_RESOURCES), ("config", FAKE_CONFIG), ("runner", FAKE_RUNNER)):
        (pkg / f"{name}.py").write_text(textwrap.dedent(src))
    for mod in [m for m in sys.modules if m == "entry" or m.startswith("parking_occupancy")]:
        monkeypatch.delitem(sys.modules, mod)
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.syspath_prepend(REPO)
    monkeypatch.setenv("ALLOWED_BUCKET", "b")
    monkeypatch.setenv("AWS_BATCH_JOB_ID", "job-123")
    monkeypatch.delenv("RFDETR_WEIGHTS", raising=False)
    module = importlib.import_module("entry")
    s3 = importlib.import_module("parking_occupancy.parking.s3")
    resources = importlib.import_module("parking_occupancy.parking.resources")

    def run(*argv):
        monkeypatch.setattr(sys, "argv", ["entry.py", *argv])
        return module.main()

    return run, s3.STATE, resources.GPU, module


def summary(state, key="results/fw-test-1/job-123/run_summary.json"):
    return json.loads(state["puts"][key])


def test_refuses_other_buckets_and_non_marker_keys(entry):
    run, state, _, _ = entry
    assert run("--bucket", "rcsstoragebucket", "--key", "incoming/r/_READY") == 2
    assert run("--bucket", "b", "--key", "incoming/_READY") == 2
    assert run("--bucket", "b", "--key", "incoming/r/photo.jpg") == 2
    assert run("--bucket", "b", "--key", "other/r/_READY") == 2
    assert state["puts"] == {}


def test_dry_run_lists_and_writes_nothing(entry):
    run, state, _, _ = entry
    assert run("--bucket", "b", "--key", "incoming/fw-test-1/_DRYRUN") == 0
    assert state["listed"] == "s3://b/incoming/fw-test-1/"
    assert state["puts"] == {}


def test_real_run_writes_summary_under_job_id(entry):
    run, state, _, module = entry
    assert module.WEIGHTS_PATH == "/app/weights/rcs_weights.pth"
    assert run("--bucket", "b", "--key", "incoming/fw-test-1/_READY", "--event-time", "2026-10-05T12:00:00Z") == 0
    s = summary(state)
    assert s["status"] == "succeeded"
    assert (s["bays"], s["spots"], s["files_uploaded"]) == (1, 2, 2)
    assert s["calibration_source"] == "features" and s["gpu"] == "Tesla T4"
    assert s["trigger_to_start_s"] > 0
    assert set(s["timings"]) == {"download_s", "pipeline_s", "upload_s", "total_s"}
    # same transfer helpers and worker counts as production's run_parked_car_pipeline_from_s3
    assert state["up_uri"] == "s3://b/results/fw-test-1/job-123"
    assert (state["dl_workers"], state["up_workers"]) == (4, 16)


def test_partial_upload_fails_and_still_writes_summary(entry):
    run, state, _, _ = entry
    state["upload_short"] = True
    assert run("--bucket", "b", "--key", "incoming/fw-test-1/_READY") == 1
    s = summary(state)
    assert s["status"] == "failed" and "uploaded 1 of 2" in s["error"]
    assert s["trigger_to_start_s"] is None  # no --event-time given


def test_missing_gpu_fails_fast(entry):
    run, state, gpu, _ = entry
    gpu["on"] = False
    assert run("--bucket", "b", "--key", "incoming/fw-test-1/_READY") == 1
    assert "no CUDA GPU" in summary(state)["error"]


def test_nested_run_name(entry):
    run, state, _, _ = entry
    assert run("--bucket", "b", "--key", "incoming/site/day1/_READY") == 0
    assert "results/site/day1/job-123/run_summary.json" in state["puts"]


def test_local_mode(entry, tmp_path):
    run, _, _, _ = entry
    frames = tmp_path / "frames"
    frames.mkdir()
    for i in range(3):
        (frames / f"f{i}.JPG").write_text("x")
    out = tmp_path / "out"
    assert run("--local-dir", str(frames), "--out", str(out)) == 0
    s = json.loads((out / "run_summary.json").read_text())
    assert s["status"] == "succeeded" and s["spots"] == 2
