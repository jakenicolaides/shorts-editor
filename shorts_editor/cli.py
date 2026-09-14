"""CLI: python -m shorts_editor.cli run <clip> [--speed 1.0] [--game auto]
          python -m shorts_editor.cli note <job_id> "<note>"
          python -m shorts_editor.cli template <job_id> <game> <start> <end>   (clip the solve tone)"""
import sys
from pathlib import Path
from . import pipeline, solvetone


def main(argv):
    cmd = argv[0] if argv else "help"
    if cmd == "run":
        src = Path(argv[1])
        speed = float(argv[argv.index("--speed") + 1]) if "--speed" in argv else 1.0
        game = argv[argv.index("--game") + 1] if "--game" in argv else "auto"
        job = pipeline.Job.create(src, speed, game)
        print("job", job.id)
        job.run()
        for line in job.status().get("log", []):
            print(line)
    elif cmd == "note":
        job = pipeline.Job(argv[1])
        print(job.apply_note(" ".join(argv[2:])))
    elif cmd == "template":
        job = pipeline.Job(argv[1])
        out = solvetone.make_template(job.dir / "audio.wav", float(argv[3]), float(argv[4]), argv[2])
        print("wrote", out)
    elif cmd == "detect":
        job = pipeline.Job(argv[1])
        print(solvetone.detect_any(job.dir / "audio.wav"))
    else:
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
