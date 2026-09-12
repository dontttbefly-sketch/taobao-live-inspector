from __future__ import annotations

from contextlib import redirect_stdout
import json
from pathlib import Path
from types import SimpleNamespace
import sys


def _main() -> int:
    try:
        payload = json.loads(sys.stdin.readline())
        expected = {
            "audio_path", "hotwords", "punc_model", "recognizer_device",
            "recognizer_model", "recognizer_revision", "vad_model",
        }
        if (
            not isinstance(payload, dict)
            or set(payload) != expected
            or not isinstance(payload["audio_path"], str)
            or not isinstance(payload["hotwords"], list)
            or not payload["hotwords"]
            or any(not isinstance(item, str) or not item for item in payload["hotwords"])
            or any(
                not isinstance(payload[key], str) or not payload[key]
                for key in expected - {"audio_path", "hotwords"}
            )
        ):
            return 2
        from .events import serialize_transcript_batch
        from .recognizer import SeacoParaformerRecognizer

        settings = SimpleNamespace(**{
            key: payload[key] for key in expected - {"audio_path", "hotwords"}
        })
        recognizer = SeacoParaformerRecognizer(settings)
        with redirect_stdout(sys.stderr):
            batch = recognizer.transcribe(
                Path(payload["audio_path"]), tuple(payload["hotwords"])
            )
        sys.stdout.write(serialize_transcript_batch(batch))
        sys.stdout.flush()
        return 0
    except Exception:
        return 3


if __name__ == "__main__":  # pragma: no cover - subprocess entrypoint
    raise SystemExit(_main())
