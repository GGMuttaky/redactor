# Contributing

Thanks for looking. Issues and pull requests are welcome — especially test footage cases where a
face is missed (describe the scene; never upload footage of people who have not agreed to it).

Before a pull request:

1. Run the engine test on a few clips: `app\.venv\Scripts\python.exe app\tests\e2e.py <clips> --home <temp dir> --stills <temp dir>`.
2. If you touch detection or tracking, show the effect with numbers (see `spike/` for the method).

**Licensing of contributions.** Redactor is GPL-3.0-or-later. The author may also offer it under other
licence terms, so contributions are accepted on the condition that you agree your contribution may be
distributed under GPL-3.0-or-later **and** relicensed by the project author. Say so in your pull
request ("I agree to the contribution terms in CONTRIBUTING.md"). If you'd rather not, open an issue
describing the change instead.
