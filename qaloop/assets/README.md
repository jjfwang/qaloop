# qaloop/assets — vendored third-party browser assets

These files are bundled in-repo (not fetched at runtime) so flows stay
deterministic and offline-safe.

## axe.min.js (issue #85)

- Version: axe-core 4.10.2
- Source: https://cdnjs.cloudflare.com/ajax/libs/axe-core/4.10.2/axe.min.js
  (downloaded 2026-10-11; HTTP 200)
- License: Mozilla Public License 2.0 (MPL-2.0), (c) 2015-2024 Deque
  Systems, Inc. The license header is intact at the top of axe.min.js;
  axe-core's LICENSE file lives at
  https://github.com/dequelabs/axe-core/blob/develop/LICENSE.
- VENDERING DECISION: bundled in-repo rather than fetched at runtime, so
  `a11y` scans never depend on network access and never drift with a
  silently updated CDN copy.
