from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

import httpx


def main() -> None:
    parser = argparse.ArgumentParser(prog="wikipediarag")
    subparsers = parser.add_subparsers(dest="command", required=True)

    import_parser = subparsers.add_parser("import-wiki")
    import_parser.add_argument("--limit", type=int, default=None)
    import_parser.add_argument("--full", action="store_true")
    import_parser.add_argument("--wait", action="store_true")
    import_parser.add_argument("--api", default="http://localhost:8000")

    smoke_parser = subparsers.add_parser("smoke")
    smoke_parser.add_argument("--api", default="http://localhost:8000")

    eval_parser = subparsers.add_parser("eval")
    eval_parser.add_argument("--api", default="http://localhost:8000")

    models_parser = subparsers.add_parser("smoke-models")
    models_parser.add_argument("--provider", default="mock")
    models_parser.add_argument("--gateway", default="http://localhost:8081")

    gate_parser = subparsers.add_parser("release-gate")
    gate_parser.add_argument("--api", default="http://localhost:8000")

    args = parser.parse_args()
    if args.command == "import-wiki":
        import_wiki(args)
    elif args.command == "smoke":
        smoke(args.api)
    elif args.command == "eval":
        run_eval(args.api)
    elif args.command == "smoke-models":
        smoke_models(args.gateway)
    elif args.command == "release-gate":
        run_eval(args.api)
        print("release gate passed")


def import_wiki(args: argparse.Namespace) -> None:
    limit = None if args.full else args.limit
    if limit is None and not args.full:
        limit = 10000
    with httpx.Client(timeout=30) as client:
        response = client.post(f"{args.api}/api/v1/wikipedia/imports", json={"limit": limit})
        response.raise_for_status()
        job_id = response.json()["job_id"]
        print(f"created wiki import job {job_id}")
        if args.wait:
            wait_for_job(client, args.api, job_id)


def wait_for_job(client: httpx.Client, api: str, job_id: str) -> None:
    while True:
        response = client.get(f"{api}/api/v1/ingestion-jobs/{job_id}")
        response.raise_for_status()
        job = response.json()
        progress = job.get("progress", {})
        print(
            "status={status} pages_imported={pages} chunks_indexed={chunks}".format(
                status=job["status"],
                pages=progress.get("pages_imported", 0),
                chunks=progress.get("chunks_indexed", 0),
            )
        )
        if job["status"] in {"completed", "failed", "cancelled"}:
            if job["status"] != "completed":
                raise SystemExit(f"job finished with status {job['status']}: {job.get('error_message')}")
            return
        time.sleep(3)


def smoke(api: str) -> None:
    with httpx.Client(timeout=60) as client:
        health = client.get(f"{api}/health")
        health.raise_for_status()
        ready = client.get(f"{api}/ready")
        ready.raise_for_status()
        with client.stream(
            "POST",
            f"{api}/api/v1/chat",
            json={"message": "Что такое Россия?", "mode": "normal", "stream": True},
        ) as response:
            response.raise_for_status()
            events = list(_iter_sse(response.iter_lines()))
        names = [event["event"] for event in events]
        required = {"run.started", "message.delta", "usage.updated"}
        if not required.issubset(set(names)):
            raise SystemExit(f"smoke failed, got events {names}")
    print(json.dumps({"health": health.json(), "ready": ready.json(), "events": names}, ensure_ascii=False))


def run_eval(api: str) -> None:
    questions = ["Что такое Россия?", "Что известно о Литве?"]
    report: dict[str, Any] = {"dataset": "wiki-mini", "cases": []}
    with httpx.Client(timeout=60) as client:
        for question in questions:
            response = client.post(f"{api}/api/v1/search:debug", json={"message": question})
            if response.status_code >= 500:
                raise SystemExit(f"eval failed for {question}: {response.text}")
            payload = response.json()
            report["cases"].append(
                {
                    "question": question,
                    "evidence_count": len(payload.get("evidence", [])),
                    "insufficient_evidence": payload.get("insufficient_evidence", True),
                }
            )
    print(json.dumps(report, ensure_ascii=False, indent=2))


def smoke_models(gateway: str) -> None:
    with httpx.Client(timeout=30) as client:
        response = client.get(f"{gateway}/v1/models")
        response.raise_for_status()
        print(json.dumps(response.json(), ensure_ascii=False, indent=2))


def _iter_sse(lines: Any) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    current_event: str | None = None
    current_data: str | None = None
    for line in lines:
        if not line:
            if current_event and current_data:
                events.append({"event": current_event, "data": json.loads(current_data)})
            current_event = None
            current_data = None
            continue
        if line.startswith("event: "):
            current_event = line.removeprefix("event: ")
        elif line.startswith("data: "):
            current_data = line.removeprefix("data: ")
    return events


if __name__ == "__main__":
    try:
        main()
    except httpx.HTTPError as exc:
        print(f"HTTP error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
