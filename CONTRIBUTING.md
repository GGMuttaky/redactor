# Contributing

Thanks for looking. Issues and pull requests are welcome — especially cases where a face is missed.

## Reporting a problem

Please include:

- Redactor version (shown on the start screen, or `Redactor.bat --version`), Windows version,
  graphics card, and FFmpeg version (`ffmpeg -version`, first line).
- What the video is like: resolution, frame rate, length, camera or phone, and the scene
  (crowd, night, masks…). **Never upload footage of people who have not agreed to it** — describe it,
  or find a similar public clip.
- For missed faces: the time in the video and what the face looks like (size, angle, lighting).
- For errors: the message shown, and the end of `%LOCALAPPDATA%\Redactor\redactor.log`
  (check it for file paths or names you'd rather not share).

Security problems: see [`SECURITY.md`](SECURITY.md) — not in a public issue.

## Setting up

```bat
cd app
Redactor.bat --version
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

The first line creates `app\.venv` with the app's requirements; the second adds pytest, Ruff and ONNX.

## Before a pull request

1. `.venv\Scripts\python.exe -m ruff check . ..\research` — lint.
2. `.venv\Scripts\python.exe -m pytest` — unit tests, the local server, and the whole pipeline on small
   generated clips (about 20 s; needs FFmpeg).
3. If you change detection, tracking or export, also run the end-to-end check on real footage with faces:
   `.venv\Scripts\python.exe tests\e2e.py <clips> --home <temp dir> --stills <temp dir>`, and look at the
   stills it writes. The public clips used so far are listed in `testclips/SOURCES.md`.
4. If you change detection or tracking, show the effect with numbers — see `research/` for the method.

GitHub runs steps 1 and 2 on every push and pull request.

## Licensing of contributions

Redactor is GPL-3.0-or-later. The author may also offer it under other licence terms, so contributions
are accepted on the condition that you agree your contribution may be distributed under
GPL-3.0-or-later **and** relicensed by the project author. Say so in your pull request ("I agree to the
contribution terms in CONTRIBUTING.md"). If you'd rather not, open an issue describing the change instead.
