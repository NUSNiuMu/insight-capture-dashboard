"""Local speech synthesis helpers."""

import subprocess
from pathlib import Path


def synthesize_espeak(binary: str, voice: str, speed: int, text: str, output_path: Path) -> None:
    subprocess.run([binary, "-v", voice, "-s", str(speed), "-w", str(output_path), "--", text], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
