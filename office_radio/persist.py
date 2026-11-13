"""Save data on a Chalk Volume.

Chalk Volume writes are buffered on the sandbox and only saved to the volume
when committed by running `<mount>/.chalk-volume/commit`. Call `commit()` after
any change to the database or token file. It does nothing when the data
directory isn't on a Chalk Volume (e.g. local tests or /tmp).
"""
from __future__ import annotations

import logging
import os
import subprocess

log = logging.getLogger("office_radio.persist")


def _commit_script(data_dir: str) -> str | None:
    root = os.getenv("OFFICE_RADIO_VOLUME_ROOT")
    candidates = [root] if root else []
    path = os.path.abspath(data_dir)
    while True:  # walk up from the data dir to find the mount root
        candidates.append(path)
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    for c in candidates:
        script = os.path.join(c, ".chalk-volume", "commit")
        if os.path.exists(script):
            return script
    return None


def commit(data_dir: str) -> bool:
    script = _commit_script(data_dir)
    if not script:
        return False
    try:
        subprocess.run([script], check=True, timeout=120, capture_output=True)
        log.info("Saved data to Chalk Volume")
        return True
    except (subprocess.SubprocessError, OSError) as exc:
        log.error("Volume commit failed: %s", exc)
        return False
