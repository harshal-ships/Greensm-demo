"""PCM helpers and sample-rate constants for AgentDuet ↔ Nova Sonic."""

from __future__ import annotations

import struct

AGENTDUET_SAMPLE_RATE = 24000
NOVA_INPUT_SAMPLE_RATE = 16000
NOVA_OUTPUT_SAMPLE_RATE = 24000


def downsample_24k_to_16k(pcm_24k: bytes) -> bytes:
    """Resample AgentDuet 24 kHz PCM to Nova Sonic 16 kHz input."""
    if len(pcm_24k) < 2:
        return pcm_24k
    samples = struct.unpack(f"<{len(pcm_24k) // 2}h", pcm_24k)
    n_out = int(len(samples) * NOVA_INPUT_SAMPLE_RATE / AGENTDUET_SAMPLE_RATE)
    ratio = AGENTDUET_SAMPLE_RATE / NOVA_INPUT_SAMPLE_RATE
    out: list[int] = []
    for i in range(n_out):
        src = i * ratio
        idx = int(src)
        frac = src - idx
        if idx + 1 < len(samples):
            val = int(samples[idx] * (1 - frac) + samples[idx + 1] * frac)
        else:
            val = samples[min(idx, len(samples) - 1)]
        out.append(max(-32768, min(32767, val)))
    return struct.pack(f"<{len(out)}h", *out)
