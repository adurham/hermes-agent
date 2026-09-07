# Shared bundle builds

The build-only Python modules live in `scripts/bundles/`. They use PM for
package installation and dependency resolution. They do not implement a
second package manager.

| Module | Responsibility | Consumers |
|---|---|---|
| `payload.py` | Git snapshots, manifest/facts, JS output placement, relocation and launcher generation | Native desktop and Termux |
| `native.py` | Native tool-store staging and dependency venv assembly | `hermes pm bundle`, desktop builder |
| `desktop.py` | Build the JS surfaces, assemble the payload, invoke Electron packaging | Native release workflow, local builds |
| `stage.py` | Stage a payload and its launchers without creating an Electron package | `npm run payload` |
| `mint_launchers.py` | Mint Windows launchers with the payload interpreter's distlib | Shared launcher assembly |
| `launcher_wrapper.py` | Runtime template for the minted executable | Shared launcher renderer |

Build a desktop bundle from a checkout at its release tag:

```sh
uv run --no-project --python 3.11 python scripts/bundles/desktop.py --tag=vX.Y.Z
```

The builder derives console entrypoints from the archived `pyproject.toml`.
It records launcher names in the payload manifest. The MSIX hook reads those
names rather than carrying a second entrypoint list. POSIX launchers follow
links to the installed payload and call each declared function directly.
Windows launchers are minted by the payload's own Python, preserving native
architecture and relocation behavior.

Termux uses the same snapshot, manifest/fact writer, JS asset placement and
POSIX launcher generator. Its package-manager hooks stay in `scripts/termux/`.
Its bionic wheel compilation and offline installation remain target-specific:
a glibc host cannot execute the shipped interpreter, and Termux has a fixed
installation prefix. Native desktop staging instead resolves on the target OS.
Neither path compiles dependencies on the user's machine.

Electron-specific work stays with Electron: renderer/main-process bundling,
Node native bindings, MSIX metadata, signing, notarization and app packaging.
The after-pack hook invokes the shared Python relocation command instead of
maintaining its own link rewrite algorithm.

Ordinary bundle staging does not scan user plugin trees. Runtime plugin
admission is a separate PM transaction. Build output cleanup is confined to
its payload store; it must not prune the machine-wide downloader partials.

## Verification boundary

Tests execute shared snapshot/manifest helpers on real git fixtures, stage
real subprocesses and venvs at controlled boundaries, and mint/run Windows
launchers after relocation. POSIX launcher and symlink tests run on POSIX.
A local helper test is not proof of a signed installer, bionic wheel build,
or stable-release upgrade. The release workflows own those native receipts.
