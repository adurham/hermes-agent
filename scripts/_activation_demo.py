#!/usr/bin/env -S bash -c 'exec "$BASH" "$(dirname "$0")/_hermes-python" "$0" "$@"'
"""Demo: a POSIX script that activates the Hermes environment for itself.

Run it from any cwd in any shell: the shebang hands the file to
``scripts/_hermes-python``, which sources ``activate`` and execs the
interpreter on this same file. Delete this file once you have copied the
header, or keep it as the template.
"""

import os
import sys

from _activation import require_activation

require_activation()

print("activated sentinel:", os.environ.get("__HERMES_ACTIVATED"))
print("interpreter       :", sys.executable)
print("args              :", sys.argv[1:])
