# Security

Redactor handles sensitive footage, so security reports matter.

**Please report problems privately**, using GitHub's **Report a vulnerability** button on this
repository's *Security* tab — not in a public issue. Include what an attacker could do, and the steps
to reproduce it.

What Redactor protects, so you know what counts:

- It serves its page and API on `127.0.0.1` only. Every request needs this run's secret token and this
  server's exact `Host` header; pages are sent with a strict Content-Security-Policy and may not be framed.
  Another website open in the same browser should not be able to read projects, change them, or start
  exports.
- Nothing is sent over the network. The only files written are its own project folder
  (`%LOCALAPPDATA%\Redactor`), short-lived temporary files, and new files beside the source video; it
  never overwrites a file.
- Project folders contain **unredacted** face thumbnails and a preview copy of the video. They are
  protected by your Windows account's file permissions, like any other file you own; delete projects
  you no longer need.

Only the latest release is supported.
