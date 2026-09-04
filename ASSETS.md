# Asset provenance and release status

The repository contains seven maintainer-supplied project-specific PNG images
and one derived Windows icon under `images/`:

- `parrotDogLogo.png` is the combined WatchDog and Parrot Dog logo;
- `watchdog_workflow_en.png`, `watchdog_workflow_cn.png`, and
  `watchdog_workflow_jp.png` explain the durable GitHub WatchDog loop in
  English, Chinese, and Japanese; and
- `parrot_workflow_en.png`, `parrot_workflow_cn.png`, and
  `parrot_workflow_jp.png` explain the Slack Parrot Dog relay in English,
  Chinese, and Japanese.

`codex-watchdog.ico` is a multi-resolution Windows application icon derived
from the approved `parrotDogLogo.png`. It contains square 16, 24, 32, 48, 64,
128, and 256 pixel representations. The release build passes this ICO to
PyInstaller and then compares the final PE icon-resource payloads with the
checked-in ICO; copying the file into the ZIP alone is not release acceptance.

All files were supplied by the project maintainer. On September 4, 2026, the
maintainer confirmed that the project is authorized to publish and redistribute
the images as part of this repository. None contains EXIF or textual PNG
metadata.

The portable Windows archive includes the combined logo, the derived ICO, and
the two English workflow images used by its bundled README. The Chinese and
Japanese variants remain available in the public repository beside their
language-specific landing pages.

The workflow comics include visual references to third-party products and
services. Those names, logos, and marks remain the property of their respective
owners and are not licensed by this project. The repository is an independent
community project and is not affiliated with or endorsed by those owners.

Publication status: **approved by the project maintainer**.
