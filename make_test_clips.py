#!/usr/bin/env python3
"""make_test_clips.py - synthetic speech with pauses of known length.

Usage:  /opt/mci-venv/bin/python /opt/iconect/make_test_clips.py OUTPUT_FOLDER

Makes two ~77 s clips from 21 spoken sentences (espeak-ng) separated by 20
pauses of known length (0.15 ... 3.0 s, each used twice, in shuffled order):
    speech_clean.wav   perfect silence between sentences
    speech_noisy.wav   the same plus faint steady background hiss (-50 dBFS)
    speech_clean.truth.json, speech_noisy.truth.json
                       where every sentence and pause is, in clip seconds
                       (validate.py finds these next to the clip automatically)
"""
import json, random, subprocess, sys, tempfile
from pathlib import Path

import numpy as np
import soundfile as sf

SR = 48000
SENTENCES = [
    "The garden was full of yellow flowers this morning",
    "My daughter called me on Sunday afternoon",
    "We walked down to the river after lunch",
    "I used to work at the post office for many years",
    "The weather has been very cold lately",
    "There is a small bakery on the corner of our street",
    "I like to read the newspaper with my coffee",
    "Last summer we visited my brother in Ohio",
    "The doctor said my blood pressure is fine",
    "On Tuesdays I play cards with my neighbors",
    "My grandson is learning to play the piano",
    "We had chicken soup for dinner yesterday",
    "The library opens at nine in the morning",
    "I planted tomatoes and beans in the spring",
    "Our church has a choir that sings every week",
    "The bus to the market comes every half hour",
    "I remember the old movie theater downtown",
    "My wife and I met at a dance many years ago",
    "The children were playing in the park",
    "I keep my photographs in a big blue box",
    "Tomorrow I will go for a walk if it is sunny",
]
PAUSES = [0.15, 0.25, 0.4, 0.6, 0.8, 1.0, 1.5, 2.0, 2.5, 3.0] * 2
EDGE_S = 0.5            # silence at the very start and end of the clip
NOISE_RMS = 10 ** (-50 / 20)

def speak(text, speed):
    """espeak-ng -> mono float32 at 48 kHz, trimmed, peak 0.5."""
    with tempfile.TemporaryDirectory() as d:
        wav = Path(d) / "s.wav"
        subprocess.run(["espeak-ng", "-v", "en-us", "-s", str(speed), "-w", str(wav), text],
                       check=True, capture_output=True)
        raw = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-i", str(wav), "-ac", "1",
                              "-ar", str(SR), "-f", "f32le", "-"], check=True, capture_output=True).stdout
    x = np.frombuffer(raw, dtype="<f4").copy()
    loud = np.flatnonzero(np.abs(x) > 0.02 * np.max(np.abs(x)))
    x = x[loud[0]:loud[-1] + 1]                          # trim leading/trailing silence
    return (0.5 * x / np.max(np.abs(x))).astype(np.float32)

def main():
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(42)                              # same clips every time
    pauses = PAUSES[:]
    rng.shuffle(pauses)
    parts = [np.zeros(int(EDGE_S * SR), np.float32)]
    t = EDGE_S
    truth = {"sample_rate": SR, "sentences": [], "pauses": []}
    for i, text in enumerate(SENTENCES):
        x = speak(text, speed=rng.choice([140, 155, 170]))
        truth["sentences"].append({"text": text, "start_s": round(t, 4), "end_s": round(t + len(x) / SR, 4)})
        parts.append(x)
        t += len(x) / SR
        if i < len(pauses):
            p = pauses[i]
            truth["pauses"].append({"after_sentence": i, "start_s": round(t, 4), "length_s": p})
            parts.append(np.zeros(int(round(p * SR)), np.float32))
            t += p
    parts.append(np.zeros(int(EDGE_S * SR), np.float32))
    clean = np.concatenate(parts)
    noise = np.random.default_rng(7).normal(0, NOISE_RMS, len(clean)).astype(np.float32)
    noisy = np.clip(clean + noise, -1, 1)
    sf.write(out / "speech_clean.wav", clean, SR, subtype="PCM_16")
    sf.write(out / "speech_noisy.wav", noisy, SR, subtype="PCM_16")
    truth["length_s"] = round(len(clean) / SR, 3)
    for name in ("speech_clean", "speech_noisy"):
        (out / f"{name}.truth.json").write_text(json.dumps(truth, indent=1))
    print(f"Made {out}/speech_clean.wav and speech_noisy.wav: {truth['length_s']} s, "
          f"{len(truth['sentences'])} sentences, {len(truth['pauses'])} pauses")
    print("Pause lengths in order:", [p["length_s"] for p in truth["pauses"]])

if __name__ == "__main__":
    main()
