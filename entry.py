"""AWS Batch entry point for the parking-occupancy prototype.

EventBridge submits one job when ``incoming/<run>/_READY`` (full run) or
``incoming/<run>/_DRYRUN`` (list the pictures only) lands in the test bucket. The job
reads the pictures under ``incoming/<run>/`` and writes everything to
``results/<run>/<job id>/`` in the same bucket, so a re-run never mixes with an earlier
one. It refuses any other bucket and never connects to Supabase.

S3 transfers use the pipeline's own helpers (parking.s3) with the same worker counts as
production's run_parked_car_pipeline_from_s3, so the recorded timings are comparable.

Logs are one JSON object per line, so they can be filtered in CloudWatch Logs Insights.
"""

import argparse
import json
import logging
import os
import resource
import sys
import tempfile
import time
import traceback
from datetime import datetime
from pathlib import Path

# Baked into the image; override (e.g. for a --local-dir baseline on another GPU host).
WEIGHTS_PATH = os.environ.get("RFDETR_WEIGHTS", "/app/weights/rcs_weights.pth")
MARKERS = {"_READY": False, "_DRYRUN": True}  # marker file name -> dry run?


def log(msg, **fields):
    print(json.dumps({"t": round(time.time(), 3), "msg": msg, **fields}, default=str), flush=True)


def seconds_since(iso_time):
    """Seconds from an EventBridge event time (e.g. 2026-10-05T12:00:00Z) to now, or None."""
    try:
        return round(time.time() - datetime.fromisoformat(iso_time.replace("Z", "+00:00")).timestamp(), 1)
    except (AttributeError, ValueError):
        return None


def peak_rss_mb():
    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)  # Linux: KiB


def run_pipeline(image_dir, outputs_dir, plan):
    # Same pipeline and defaults as production; no GSD is passed, so the run
    # self-calibrates from image features instead of reading Supabase.
    from parking_occupancy.parking.config import PipelineConfig
    from parking_occupancy.parking.runner import _run_pipeline

    config = PipelineConfig(
        image_dir=image_dir,
        outputs_dir=outputs_dir,
        rfdetr_weights=WEIGHTS_PATH,
        save_spot_annotations=True,
        batch_size=plan["batch_size"],
        half=plan["half"],
        decode_workers=plan["decode_workers"],
        annotate_workers=plan["annotate_workers"],
    )
    return _run_pipeline(config)


def describe_document(document):
    metadata = document.get("metadata") or {}
    calibration = (metadata.get("geo") or {}).get("calibration") or {}
    return {
        "bays": len(document.get("bays") or []),
        "spots": len(document.get("spots") or []),
        "frames_with_telemetry": int(metadata.get("n_frames") or 0),
        "detections_binned": int(metadata.get("n_detections_total") or 0),
        "calibration_source": calibration.get("source"),
        "warning": metadata.get("warning"),
    }


def gpu_peak_mb():
    try:
        import torch
        if torch.cuda.is_available():
            return round(torch.cuda.max_memory_allocated() / 2 ** 20, 1)
    except Exception:  # noqa: BLE001
        pass
    return None


def require_gpu(plan, allow_cpu):
    hw = plan["hw"]
    log("compute", gpu=hw["gpu"], gpu_name=hw["gpu_name"], vram_gb=hw["vram_gb"], cpu=hw["cpu"],
        batch_size=plan["batch_size"], decode_workers=plan["decode_workers"],
        annotate_workers=plan["annotate_workers"], half=plan["half"])
    if not hw["gpu"] and not allow_cpu:
        # A CPU fallback would still "succeed", with timings that mean nothing for the GPU decision.
        raise RuntimeError("no CUDA GPU visible to the container")


def run_from_s3(args):
    allowed = os.environ.get("ALLOWED_BUCKET", "")
    if not allowed or args.bucket != allowed:
        log("refusing bucket", bucket=args.bucket, allowed=allowed)
        return 2
    prefix, _, marker = args.key.rpartition("/")
    if marker not in MARKERS or not prefix.startswith("incoming/"):
        log("not a trigger key, nothing to do", key=args.key)
        return 2

    dry_run = MARKERS[marker]
    run_name = prefix.split("/", 1)[1]
    job_id = os.environ.get("AWS_BATCH_JOB_ID") or f"manual-{int(time.time())}"
    results_prefix = f"results/{run_name}/{job_id}/"
    trigger_to_start_s = seconds_since(args.event_time)
    log("job started", job_id=job_id, bucket=args.bucket, key=args.key, dry_run=dry_run,
        event_time=args.event_time, trigger_to_start_s=trigger_to_start_s)

    from parking_occupancy.parking import s3 as ps3

    client = ps3.s3_client()
    started = time.monotonic()
    uris = ps3.list_image_uris(f"s3://{args.bucket}/{prefix}/", client=client)
    log("images listed", count=len(uris), first=uris[:3])
    if not uris:
        log("no images under prefix", prefix=prefix)
        return 1
    if dry_run:
        log("dry run finished", seconds=round(time.monotonic() - started, 1))
        return 0

    timings = {}
    summary = {
        "status": "failed",
        "job_id": job_id,
        "trigger_key": args.key,
        "event_time": args.event_time,
        "trigger_to_start_s": trigger_to_start_s,
        "images_listed": len(uris),
        "results": f"s3://{args.bucket}/{results_prefix}",
    }
    try:
        from parking_occupancy.parking.resources import plan_resources

        plan = plan_resources()
        require_gpu(plan, args.allow_cpu)
        summary["gpu"] = plan["hw"]["gpu_name"]
        summary["instance_vcpus"] = plan["hw"]["cpu"]

        with tempfile.TemporaryDirectory() as tmp:
            image_dir = os.path.join(tmp, "images")
            outputs_dir = os.path.join(tmp, "outputs")
            os.makedirs(outputs_dir)

            t = time.monotonic()
            ok, errors = ps3.download_images(uris, image_dir, workers=plan["decode_workers"], client=client)
            timings["download_s"] = round(time.monotonic() - t, 1)
            summary["images_downloaded"] = ok
            summary["download_errors"] = errors[:20]
            log("images downloaded", ok=ok, failed=len(errors), seconds=timings["download_s"])
            if ok == 0:
                raise RuntimeError("could not download any image from the input prefix")

            t = time.monotonic()
            document = run_pipeline(image_dir, outputs_dir, plan)
            timings["pipeline_s"] = round(time.monotonic() - t, 1)
            summary.update(describe_document(document))
            log("pipeline finished", seconds=timings["pipeline_s"], **describe_document(document))

            t = time.monotonic()
            local_files = sum(1 for p in Path(outputs_dir).rglob("*") if p.is_file())
            _, uploaded = ps3.upload_directory(
                outputs_dir, f"s3://{args.bucket}/{results_prefix.rstrip('/')}",
                workers=max(plan["decode_workers"], 16), client=client)
            timings["upload_s"] = round(time.monotonic() - t, 1)
            summary["files_uploaded"] = len(uploaded)
            if len(uploaded) != local_files:
                raise RuntimeError(f"uploaded {len(uploaded)} of {local_files} result files")

        summary["status"] = "succeeded"
        return 0
    except Exception as exc:  # noqa: BLE001 -- recorded in run_summary.json, then the job fails
        summary["error"] = f"{type(exc).__name__}: {exc}"
        log("job failed", error=summary["error"], traceback=traceback.format_exc())
        return 1
    finally:
        timings["total_s"] = round(time.monotonic() - started, 1)
        summary["timings"] = timings
        summary["peak_rss_mb"] = peak_rss_mb()
        summary["gpu_peak_allocated_mb"] = gpu_peak_mb()
        try:
            client.put_object(Bucket=args.bucket, Key=results_prefix + "run_summary.json",
                              Body=json.dumps(summary, indent=2).encode(), ContentType="application/json")
        except Exception as exc:  # noqa: BLE001
            log("could not write run_summary.json", error=str(exc))
        log("job finished", **summary)


def run_local(args):
    """Run on a local folder (no S3), e.g. on any CUDA machine: --local-dir frames --out out."""
    from parking_occupancy.parking.resources import plan_resources

    plan = plan_resources()
    require_gpu(plan, args.allow_cpu)
    outputs_dir = os.path.join(args.out, "outputs")
    os.makedirs(outputs_dir, exist_ok=True)
    t = time.monotonic()
    document = run_pipeline(args.local_dir, outputs_dir, plan)
    summary = {"status": "succeeded", "gpu": plan["hw"]["gpu_name"],
               "timings": {"pipeline_s": round(time.monotonic() - t, 1)},
               "peak_rss_mb": peak_rss_mb(), "gpu_peak_allocated_mb": gpu_peak_mb(),
               **describe_document(document)}
    with open(os.path.join(args.out, "run_summary.json"), "w") as fh:
        json.dump(summary, fh, indent=2)
    log("local run finished", **summary)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bucket")
    parser.add_argument("--key")
    parser.add_argument("--event-time", default="none", help="EventBridge event time of the trigger upload.")
    parser.add_argument("--local-dir", help="Run on this folder of frames instead of S3.")
    parser.add_argument("--out", default="/tmp/occupancy-local", help="Output folder for --local-dir.")
    parser.add_argument("--allow-cpu", action="store_true", help="Run even without a CUDA GPU.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    for noisy in ("botocore", "boto3", "urllib3", "s3transfer"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if args.local_dir:
        return run_local(args)
    if not args.bucket or not args.key:
        parser.error("--bucket and --key are required (or --local-dir)")
    return run_from_s3(args)


if __name__ == "__main__":
    sys.exit(main())
