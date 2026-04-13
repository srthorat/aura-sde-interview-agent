#!/usr/bin/env python3
"""Create or retrieve the Vertex AI Reasoning Engine for Aura session storage.

Usage:
    python3 create_reasoning_engine.py \
        --project PROJECT_ID \
        --location us-central1 \
        --staging-bucket gs://your-bucket \
        [--display-name aura-sessions]

Prints the numeric engine ID to stdout.

If an engine with the given display_name already exists the existing ID is
returned without creating a new one — so this script is safe to run on every
deploy.
"""

from __future__ import annotations

import argparse
import sys


def _find_existing(project: str, location: str, display_name: str) -> str | None:
    """Return the numeric ID of an existing engine with display_name, or None."""
    import vertexai
    from vertexai.preview.reasoning_engines import ReasoningEngine

    vertexai.init(project=project, location=location)
    try:
        engines = ReasoningEngine.list()
        for eng in engines:
            if eng.display_name == display_name:
                return eng.resource_name.split("/")[-1]
    except Exception as exc:
        print(f"[warning] Could not list engines: {exc}", file=sys.stderr)
    return None


def _create(project: str, location: str, staging_bucket: str, display_name: str) -> str:
    """Create a new Reasoning Engine and return its numeric ID."""
    import vertexai
    from vertexai.preview.reasoning_engines import ReasoningEngine

    vertexai.init(project=project, location=location, staging_bucket=staging_bucket)

    class _SessionStore:
        """Minimal stub deployed to the Reasoning Engine container.

        The actual session data is stored by the Vertex AI Sessions API which
        only requires the engine resource to exist — the code running inside it
        is irrelevant for Aura's use case.
        """

        def set_up(self) -> None:
            pass

        def query(self, **kwargs):  # noqa: ANN201
            return {"status": "ok"}

    engine = ReasoningEngine.create(
        _SessionStore(),
        display_name=display_name,
        description="Aura SDE Interview Coach — session storage for VertexAiSessionService",
        requirements=["google-cloud-aiplatform>=1.45.0"],
    )
    return engine.resource_name.split("/")[-1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--location", default="us-central1")
    parser.add_argument("--staging-bucket", default="",
                        help="GCS bucket (gs://name or just name). Required when creating.")
    parser.add_argument("--display-name", default="aura-sessions")
    args = parser.parse_args()

    print(f"[reasoning-engine] Checking for existing engine '{args.display_name}'...",
          file=sys.stderr)

    engine_id = _find_existing(args.project, args.location, args.display_name)
    if engine_id:
        print(f"[reasoning-engine] Found existing engine: {engine_id}", file=sys.stderr)
        print(engine_id)
        return

    if not args.staging_bucket:
        print("[reasoning-engine] ERROR: --staging-bucket is required to create a new engine.",
              file=sys.stderr)
        sys.exit(1)

    bucket = args.staging_bucket
    if not bucket.startswith("gs://"):
        bucket = f"gs://{bucket}"

    print(f"[reasoning-engine] Creating new engine (staging: {bucket}) — this takes ~60s...",
          file=sys.stderr)
    engine_id = _create(args.project, args.location, bucket, args.display_name)
    print(f"[reasoning-engine] Created engine: {engine_id}", file=sys.stderr)
    print(engine_id)


if __name__ == "__main__":
    main()
