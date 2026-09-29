#!/usr/bin/env python3
"""Batch-produce all unfinished shots of a project via the factory API.

Usage: python3 batch_produce.py <project_id> [--bind <asset_id>]
Waits for each run to reach a terminal state before starting the next
(the worker already serializes on the GPU lease; this just keeps logs tidy).
"""
import argparse
import json
import sys
import time
import urllib.request

B = "http://localhost:8600"


def req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(B + path, data=data, method=method,
                               headers={"Content-Type": "application/json"} if data else {})
    return json.load(urllib.request.urlopen(r))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("project_id")
    ap.add_argument("--bind", default=None, help="asset_id to bind as reference on every shot")
    args = ap.parse_args()

    proj = req("GET", f"/projects/{args.project_id}")
    shots = proj["shots"]
    todo = [s for s in shots if not s["accepted_run_id"]]
    print(f"{len(shots)} shots total, {len(todo)} to produce")

    if args.bind:
        for s in todo:
            req("POST", f"/shots/{s['shot_id']}/asset-bindings",
                {"asset_id": args.bind, "asset_version": 1, "role": "reference"})
        print(f"bound {args.bind} to {len(todo)} shots")

    results = {}
    for i, s in enumerate(todo, 1):
        sid = s["shot_id"]
        run = req("POST", f"/shots/{sid}/runs",
                  {"command_id": f"batch-{sid}-{int(time.time())}"})
        rid = run["run_id"]
        print(f"[{i}/{len(todo)}] {sid} -> {rid} queued", flush=True)
        while True:
            time.sleep(20)
            state = req("GET", f"/runs/{rid}")["run"]["state"]
            if state in ("ACCEPTED", "FAILED", "CANCELLED", "HUMAN_REVIEW"):
                results[sid] = state
                print(f"[{i}/{len(todo)}] {sid} -> {state}", flush=True)
                break
    print(json.dumps(results, indent=1))


if __name__ == "__main__":
    main()
