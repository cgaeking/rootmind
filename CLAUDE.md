# Working on RootMind

## GUI: prevent errors instead of reporting them

A good GUI never offers the user a choice that leads to an error. Error
messages are prevented up front wherever possible; this is what separates a
good GUI from a simple generated one.

- Offer only what works: leave out options that cannot work (a model the
  check refused leaves the model list; a setting with one possible value is
  shown fixed, not as a choice).
- When something the user chose turns out not to work, fall back on its own to
  something that does (the previous choice, a sensible default) and say so in
  one short, neutral line. No red error banner for something the app can
  resolve itself.
- Check early (on choosing, on saving), not on first use.
- An error message is the last resort, for what the app cannot resolve
  (no connection, a rejected key). Then say what to do, not what failed
  internally.

## GUI text

- Short. No explanations of things users already know, no hint texts that
  restate the label.
- Every label in English and German (`desktop/src/i18n/labels.en.ts`,
  `labels.de.ts`).

## Repository conventions

- No model identifiers in commit messages, PR titles or PR bodies.
- Releases: a `chore: release X.Y.Z` PR bumps all versions
  (`scripts/check_versions.py` must report they match) and turns
  `## [Unreleased]` in `CHANGELOG.md` into the version. The maintainer pushes
  the tag.
