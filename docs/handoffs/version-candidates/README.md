# Resolution evidence only

These files are copied from the isolated version-validation workspace. They are not production lockfiles and have not been integrated into project manifests.

- `requirements.in`: seven direct Python candidate pins.
- `requirements.resolved.txt`: hashed uv compile resolution targeting Python 3.13.
- `package.json`: five direct frontend candidate pins in a minimal validation manifest.
- `package-lock.json`: successful npm package-lock-only resolution.

No dependency installation, runtime/import test, frontend build or live provider test is proved by these files. No cache, node_modules, credentials or live data is included. Follow ../versions.md and let M00 promote a clean-installed, tested set into the real project locks. Do not copy this sample package.json over an application manifest.
