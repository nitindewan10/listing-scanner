# listing-scanner

A small scheduled scanner that reads public job listings and keeps the ones matching private settings.

- Runs on GitHub Actions (`.github/workflows/scan.yml`), started at fixed times by an outside timer, with GitHub's own timer as a back-up.
- The settings and the scanner's memory live in a separate private store; nothing personal is kept in this repository.
- Logs show counts only.
- It never applies, signs in or contacts anyone. It only reads public pages.
