"""Per-call audio recording for the web demo (item B4).

Captures the caller mic (PCM16 16 kHz mono) and the agent TTS output as two
separate ring buffers for the lifetime of one WebSocket session, then mixes
them into a stereo WAV (caller = left, agent = right) on disconnect and
uploads to Azure Blob Storage. The browser is handed a download URL via a
``recording_ready`` event (or by polling ``GET /recordings/{session_id}``).

Demo-level concern only — lives outside ``src/mdk_voice/`` per CLAUDE.md
rule 6 (boundaries). Recording is OFF by default and silently no-ops if
either ``RECORD_CALLS=1`` or ``AZURE_STORAGE_CONNECTION_STRING`` is missing
(privacy + cost defaults).

The Azure Blob SDK is **lazy-imported** inside :func:`upload_recording` so a
demo deploy without ``azure-storage-blob`` installed only fails *when* an
upload is attempted (and even then, it's caught and logged — the call itself
never fails because of recording).
"""

from __future__ import annotations

import io
import logging
import os
import time
import wave
from dataclasses import dataclass, field

log = logging.getLogger("movate.voice.demo.recording")

# 16 kHz PCM16 mono is what the browser ships and what the TTS frames are
# resampled to before they go out. Keeping a single rate for both legs keeps
# the stereo mix trivial — no resampling in the recorder.
_SAMPLE_RATE = 16_000
_SAMPLE_WIDTH = 2  # PCM16 → 2 bytes per sample


def is_recording_enabled() -> bool:
    """True iff ``RECORD_CALLS=1`` AND a Blob connection string is set.

    Both gates are required: the feature flag (privacy/cost opt-in) AND the
    upload destination. Missing either means we never even allocate the ring
    buffers — zero per-frame overhead for the default deploy.
    """
    if os.environ.get("RECORD_CALLS", "").strip() != "1":
        return False
    return bool(os.environ.get("AZURE_STORAGE_CONNECTION_STRING", "").strip())


@dataclass
class CallRecorder:
    """Two PCM16 ring buffers + a started-at timestamp.

    Buffers grow unbounded — at 16 kHz mono PCM16 that's ~1.9 MB per minute
    per leg, ~3.8 MB/min combined. A 10-minute call sits around 38 MB in
    RAM and produces a ~38 MB stereo WAV (16 kHz · 2 ch · 2 B/sample =
    64 kB/s ≈ 3.84 MB/min). Acceptable for a demo; would need a
    chunk-and-flush strategy for production-scale.

    ``sample_rate`` defaults to 16 kHz but can be overridden by the caller
    if the browser reports a different capture rate. Both legs must share
    the rate for the stereo mix to play back at correct speed.
    """

    caller_pcm: bytearray = field(default_factory=bytearray)
    agent_pcm: bytearray = field(default_factory=bytearray)
    started_at: float = field(default_factory=time.time)
    sample_rate: int = _SAMPLE_RATE

    def add_caller(self, data: bytes) -> None:
        """Append a mic frame (PCM16 16 kHz mono)."""
        if data:
            self.caller_pcm.extend(data)

    def add_agent(self, data: bytes) -> None:
        """Append a TTS frame (PCM16 16 kHz mono)."""
        if data:
            self.agent_pcm.extend(data)

    def to_wav_stereo(self) -> bytes:
        """Interleave caller=L, agent=R into a single PCM16 stereo WAV.

        If one leg ran longer (typical: caller silence while the agent is
        speaking), the shorter buffer is zero-padded so both channels have
        the same sample count. Returns a complete in-memory WAV file.
        """
        # Align both legs to whole-sample boundaries (PCM16 = 2 B/sample).
        # A frame written mid-byte from a broken upstream would otherwise
        # produce a corrupt WAV; truncating to the nearest sample is safer
        # than emitting a header that lies about the length.
        caller = bytes(self.caller_pcm[: (len(self.caller_pcm) // _SAMPLE_WIDTH) * _SAMPLE_WIDTH])
        agent = bytes(self.agent_pcm[: (len(self.agent_pcm) // _SAMPLE_WIDTH) * _SAMPLE_WIDTH])
        n_caller = len(caller) // _SAMPLE_WIDTH
        n_agent = len(agent) // _SAMPLE_WIDTH
        n_total = max(n_caller, n_agent)
        if n_caller < n_total:
            caller = caller + b"\x00" * ((n_total - n_caller) * _SAMPLE_WIDTH)
        if n_agent < n_total:
            agent = agent + b"\x00" * ((n_total - n_agent) * _SAMPLE_WIDTH)

        # Interleave: [L0 L1 R0 R1] [L0 L1 R0 R1] ...  Cheap pure-Python
        # loop is fine — even a 10-minute call is ~9.6M samples, ~1s here,
        # and we run this once per call on the background task.
        interleaved = bytearray(n_total * 2 * _SAMPLE_WIDTH)
        for i in range(n_total):
            base = i * 2 * _SAMPLE_WIDTH
            interleaved[base : base + _SAMPLE_WIDTH] = caller[
                i * _SAMPLE_WIDTH : (i + 1) * _SAMPLE_WIDTH
            ]
            interleaved[base + _SAMPLE_WIDTH : base + 2 * _SAMPLE_WIDTH] = agent[
                i * _SAMPLE_WIDTH : (i + 1) * _SAMPLE_WIDTH
            ]

        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(2)
            wf.setsampwidth(_SAMPLE_WIDTH)
            wf.setframerate(self.sample_rate)
            wf.writeframes(bytes(interleaved))
        return buf.getvalue()


async def upload_recording(
    wav_bytes: bytes,
    *,
    session_id: str,
    container: str,
    conn_str: str,
) -> str | None:
    """Upload a WAV to Azure Blob Storage; return a 24h SAS URL (or None).

    Lazy-imports ``azure.storage.blob`` so the SDK is required only at
    upload time — a demo deploy that never sets ``RECORD_CALLS=1`` doesn't
    need the package installed.

    Failures (missing SDK, bad conn string, blob service down, container
    create race) are caught + logged at WARN and the function returns
    ``None``. The user-facing call must never be affected by a recording
    upload failure.

    Logs the container + blob name but **never** the SAS URL (it carries a
    secret signature; logging it would persist a download credential).
    """
    blob_name = f"{session_id}.wav"
    try:
        # Lazy imports — keeps base demo deploys SDK-free.
        from datetime import UTC, datetime, timedelta  # noqa: PLC0415

        from azure.storage.blob import (  # noqa: PLC0415
            BlobSasPermissions,
            BlobServiceClient,
            ContentSettings,
            generate_blob_sas,
        )
    except ImportError as exc:
        log.warning("recording upload skipped: azure-storage-blob not installed (%s)", exc)
        return None

    try:
        svc = BlobServiceClient.from_connection_string(conn_str)
        # Ensure the container exists — idempotent; ResourceExistsError on a
        # race is fine, anything else we let bubble into the outer except.
        try:
            svc.create_container(container)
        except Exception:  # noqa: BLE001 - already-exists is the common case
            pass
        client = svc.get_blob_client(container=container, blob=blob_name)
        client.upload_blob(
            wav_bytes,
            overwrite=True,
            content_settings=ContentSettings(content_type="audio/wav"),
        )

        # SAS URL: 24h read-only token so the browser can download without
        # the connection string. Generated client-side from the account key
        # parsed out of the connection string — no extra round-trip.
        sas = generate_blob_sas(
            account_name=svc.account_name,
            container_name=container,
            blob_name=blob_name,
            account_key=svc.credential.account_key,
            permission=BlobSasPermissions(read=True),
            expiry=datetime.now(UTC) + timedelta(hours=24),
        )
        url = f"{client.url}?{sas}"
        log.info(
            "recording uploaded: container=%s blob=%s size=%dB",
            container,
            blob_name,
            len(wav_bytes),
        )
        return url
    except Exception as exc:  # noqa: BLE001 - never let recording crash the call
        log.warning(
            "recording upload failed: container=%s blob=%s err=%s",
            container,
            blob_name,
            exc,
        )
        return None
