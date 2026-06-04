"""Audio codec negotiation + edge resampling (backlog #213).

Clients may send audio in different formats: PCM16 (16-bit LE, various sample
rates), Opus (WebRTC), or mu-law (telephony).  The STT providers expect a
specific format (almost always PCM16 at 16 kHz or 24 kHz).  This module handles
**edge transcoding**: accept what the client sends, transcode to PCM16 for the
STT provider, and reject unsupported formats clearly rather than garbling.

Supported codecs and their properties:

+----------+----------+--------------------------------------------------+
| Codec    | Bits     | Typical source                                   |
+==========+==========+==================================================+
| pcm16    | 16-bit   | Browser MediaRecorder (PCM), desktop mic capture  |
| opus     | variable | WebRTC (browser), mobile                         |
| mulaw    | 8-bit    | G.711 telephony (Twilio, SIP)                    |
+----------+----------+--------------------------------------------------+

Opus decoding requires an optional dependency (``opuslib``); when it is not
installed, ``opus`` is excluded from negotiation and ``transcode_to_pcm`` raises
a clear error.  PCM16 and mu-law are dependency-free (use the existing
``telephony.py`` helpers).  No heavy or GPL-licensed dependency is introduced.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from movate.voice.base import AudioCodec

# The canonical set the edge accepts.  "opus" is conditional on the optional
# ``opuslib`` dep being installed — ``negotiate_codec`` filters it out when
# Opus decoding is unavailable.
SUPPORTED_CODECS: tuple[AudioCodec, ...] = ("pcm16", "opus", "mulaw")

# Codec → whether it needs a third-party dep to decode.
_NEEDS_DEP: dict[str, str] = {
    "opus": "opuslib",
}

CodecName = Literal["pcm16", "opus", "mulaw"]


def _opus_available() -> bool:
    """Return True if the ``opuslib`` package is importable."""
    import importlib.util  # noqa: PLC0415

    return importlib.util.find_spec("opuslib") is not None


def available_codecs() -> list[AudioCodec]:
    """Return the codecs the current runtime can actually decode.

    Always includes ``pcm16`` and ``mulaw`` (dependency-free); includes
    ``opus`` only when ``opuslib`` is installed.
    """
    codecs: list[AudioCodec] = ["pcm16", "mulaw"]
    if _opus_available():
        codecs.append("opus")
    return codecs


class UnsupportedCodecError(ValueError):
    """Raised when a client offers only codecs the server cannot handle.

    Carries enough detail for the transport to send a clear error control frame
    back (not an opaque 500).
    """

    def __init__(self, offered: Sequence[str], supported: Sequence[str]) -> None:
        self.offered = list(offered)
        self.supported = list(supported)
        super().__init__(
            f"unsupported codec(s): client offered {self.offered!r}; "
            f"server supports {self.supported!r}"
        )


def negotiate_codec(client_offered: list[str]) -> AudioCodec:
    """Pick the best codec from what the client offers.

    Preference order: ``pcm16`` (cheapest to forward), ``opus`` (compressed,
    common in WebRTC), ``mulaw`` (telephony fallback).  Raises
    :class:`UnsupportedCodecError` when no offered codec is supported.
    """
    supported = available_codecs()
    # Preference: pcm16 > opus > mulaw
    codec: AudioCodec
    for codec in ("pcm16", "opus", "mulaw"):
        if codec in client_offered and codec in supported:
            return codec
    raise UnsupportedCodecError(client_offered, supported)


def resample(audio: bytes, from_rate: int, to_rate: int) -> bytes:
    """Resample 16-bit LE PCM from ``from_rate`` to ``to_rate``.

    Delegates to the existing ``telephony.resample_pcm16`` which implements
    linear interpolation with a biquad anti-alias filter — dependency-free and
    sufficient for speech.  Returns ``audio`` unchanged when rates match.
    """
    from movate.voice.telephony import resample_pcm16  # noqa: PLC0415

    return resample_pcm16(audio, from_rate, to_rate)


def transcode_to_pcm(audio: bytes, codec: str, *, sample_rate: int = 16_000) -> bytes:
    """Transcode ``audio`` from ``codec`` to 16-bit LE PCM.

    Parameters
    ----------
    audio:
        Raw audio bytes in the source codec.
    codec:
        Source codec name (``"pcm16"``, ``"mulaw"``, ``"opus"``).
    sample_rate:
        The sample rate of the source audio (used for mu-law / Opus).

    Returns
    -------
    bytes
        16-bit little-endian PCM audio data.

    Raises
    ------
    UnsupportedCodecError
        When ``codec`` is not in the supported set or a required dependency is
        missing.
    """
    if codec == "pcm16":
        # Validate: must be even-length (16-bit = 2 bytes/sample).
        if len(audio) % 2 != 0:
            raise ValueError(f"pcm16 audio must have even byte length, got {len(audio)}")
        return audio

    if codec == "mulaw":
        from movate.voice.telephony import mulaw_to_pcm16  # noqa: PLC0415

        return mulaw_to_pcm16(audio)

    if codec == "opus":
        if not _opus_available():
            raise UnsupportedCodecError(
                [codec],
                available_codecs(),
            )
        import opuslib  # type: ignore[import-not-found]  # noqa: PLC0415

        decoder = opuslib.Decoder(sample_rate, 1)  # mono
        # Opus frames are self-delimiting; decode the whole buffer as one frame.
        pcm: bytes = decoder.decode(audio, frame_size=sample_rate // 50)  # 20ms frame
        return pcm

    raise UnsupportedCodecError(
        [codec],
        available_codecs(),
    )


def validate_pcm16(audio: bytes) -> None:
    """Validate that ``audio`` is plausible 16-bit LE PCM.

    Raises :class:`ValueError` on obviously invalid input (odd length, empty).
    Does NOT check for valid sample ranges — PCM16 is unconstrained.
    """
    if not audio:
        raise ValueError("empty audio buffer")
    if len(audio) % 2 != 0:
        raise ValueError(f"pcm16 audio must have even byte length, got {len(audio)}")


def pcm16_duration_seconds(audio: bytes, sample_rate: int) -> float:
    """Return the duration of PCM16 audio in seconds."""
    n_samples = len(audio) // 2
    return n_samples / sample_rate if sample_rate > 0 else 0.0


def codec_info(codec: str) -> dict[str, object]:
    """Return metadata about a codec (for capability responses)."""
    info: dict[str, object] = {
        "name": codec,
        "supported": codec in available_codecs(),
    }
    if codec == "pcm16":
        info["bits_per_sample"] = 16
        info["byte_order"] = "little-endian"
    elif codec == "mulaw":
        info["bits_per_sample"] = 8
        info["typical_sample_rate"] = 8000
    elif codec == "opus":
        info["bits_per_sample"] = "variable"
        info["requires"] = "opuslib"
        info["available"] = _opus_available()
    return info
