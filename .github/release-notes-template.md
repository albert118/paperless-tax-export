<!--
Usage: copy this file, replace every placeholder, then publish:

  gh release create vX.Y.Z --notes-file <edited-copy>.md --generate-notes

--notes-file text is placed first and the changelog from .github/release.yml
(grouped by PR label) is appended under "What's Changed". Add --prerelease for
release candidates (vX.Y.Z-rc.N). Delete this comment before publishing.
-->

## Highlights

-

## Upgrade notes

<!-- Changes to flags, CSV columns or format, manifest.json fields, settings, secret file location or Python version. "None" if none. -->

## Output files

<!-- Changes to review.csv, deductions.csv, ibkr-documents.csv or manifest.json. Same input still gives byte-identical output unless stated here. -->

## Image

```
docker pull ghcr.io/albert118/paperless-tax-export:X.Y.Z
docker run --rm ghcr.io/albert118/paperless-tax-export:X.Y.Z --version
```

Tags published: `X.Y.Z`, `X.Y`, and `latest` for stable releases only.

## Known issues

<!-- Open problems that affect a run. "None known" if none. -->
