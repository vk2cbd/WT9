#!/usr/bin/env python3
"""Generate WT9's offline annunciation WAV pack with Kokoro British voice bf_emma.

Run once in a dedicated Python virtual environment; WT9 only needs the WAVs.
"""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from wt9_voice import PHRASES


def render(pipeline, text, voice, speed):
    import numpy as np
    chunks = []
    for result in pipeline(text, voice=voice, speed=speed, split_pattern=r"\\n+"):
        audio = result.audio if hasattr(result, "audio") else result[2]
        if audio is not None:
            samples = audio.detach().cpu().numpy() if hasattr(audio, "detach") else np.asarray(audio)
            if samples.size:
                chunks.append(samples.reshape(-1))
    if not chunks:
        raise RuntimeError("Kokoro returned no audio for " + repr(text))
    return np.concatenate(chunks).astype(np.float32)


def tidy_audio(samples, sample_rate=24000):
    """Trim excessive edge silence and apply short fades to avoid hard clicks."""
    import numpy as np
    samples = np.nan_to_num(samples, nan=0.0, posinf=0.0, neginf=0.0)
    threshold = max(0.0015, float(np.max(np.abs(samples))) * 0.015)
    active = np.flatnonzero(np.abs(samples) > threshold)
    if active.size:
        margin = int(0.12 * sample_rate)
        start = max(0, int(active[0]) - margin)
        end = min(len(samples), int(active[-1]) + margin + 1)
        samples = samples[start:end].copy()
    peak = float(np.max(np.abs(samples))) if samples.size else 0.0
    if peak > 0.95:
        samples *= 0.95 / peak
    fade = min(int(0.035 * sample_rate), len(samples) // 2)
    if fade:
        samples[:fade] *= np.linspace(0, 1, fade, dtype=np.float32)
        samples[-fade:] *= np.linspace(1, 0, fade, dtype=np.float32)
    return samples


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--voice", default="bf_emma")
    parser.add_argument("--speed", type=float, default=0.95)
    parser.add_argument("--output", type=Path, default=ROOT / "voice" / "en_AU_female",
                        help="WT9's existing WAV folder (name retained for compatibility)")
    parser.add_argument("--test-only", action="store_true",
                        help="Generate only east_test and west_tracking for audition")
    args = parser.parse_args()
    if args.voice != "bf_emma":
        parser.error("This generator is configured for British female bf_emma")
    import soundfile as sf
    from kokoro import KPipeline
    pipeline = KPipeline(lang_code="b")
    args.output.mkdir(parents=True, exist_ok=True)
    phrases = [("east", "test"), ("west", "tracking")] if args.test_only else [
        (name, key) for name in ("east", "west") for key in PHRASES
    ]
    for name, state in phrases:
        phrase = f"{name.capitalize()} {PHRASES[state]}."
        destination = args.output / f"{name}_{state.replace(" + ", "_and_").replace(" ", "_").replace("+", "plus").replace("-", "minus")}.wav"
        samples = tidy_audio(render(pipeline, phrase, args.voice, args.speed))
        sf.write(str(destination), samples, 24000, subtype="PCM_16")
        print(f"{destination.name}: {phrase}", flush=True)
    print(f"Generated {len(phrases)} recordings in {args.output}")
    print("Test: aplay voice/en_AU_female/west_tracking.wav")


if __name__ == "__main__":
    main()
