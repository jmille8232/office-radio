"""Start an always-on Office Radio sandbox. Run this on your laptop.

Before running:
  1. Chalk secrets exist: SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET,
     SPOTIFY_REFRESH_TOKEN (Chalk dashboard -> Secrets).
  2. A Chalk Volume named office-radio-data exists (dashboard or
     `chalk volume create chalk://office-radio-data`).
  3. `pip install chalkcompute` and you're signed in to Chalk on this laptop.

Then:  python launch_sandbox.py
"""
from chalkcompute import Image, Sandbox, Secret

REPO_ZIP = "https://github.com/jmille8232/office-radio/archive/refs/heads/main.zip"
VOLUME = "office-radio-data"

image = Image.debian_slim("3.12").pip_install(["requests"])

sandbox = Sandbox(
    image=image,
    name="office-radio",
    cpu="1",
    memory="1Gi",
    secrets=[
        Secret.from_chalk_env("SPOTIFY_CLIENT_ID"),
        Secret.from_chalk_env("SPOTIFY_CLIENT_SECRET"),
        Secret.from_chalk_env("SPOTIFY_REFRESH_TOKEN"),
    ],
    env={
        "SPOTIFY_REDIRECT_URI": "http://127.0.0.1:8888/callback",
        "OFFICE_RADIO_DATA_DIR": "/data",
        "OFFICE_RADIO_ADMIN": "john",
    },
    volumes=[(VOLUME, "/data")],
).run()

setup = (
    "set -e; cd /root; "
    f"python -c \"import urllib.request; urllib.request.urlretrieve('{REPO_ZIP}', 'code.zip')\"; "
    "rm -rf office-radio-main; python -m zipfile -e code.zip .; "
    "cd office-radio-main && python -m office_radio.cli check"
)
result = sandbox.exec("bash", "-lc", setup)
print(result.stdout_text or "", result.stderr_text or "")

start = (
    "cd /root/office-radio-main && "
    "nohup python -m office_radio.cli scheduler > /data/scheduler.log 2>&1 &"
)
sandbox.exec("bash", "-lc", start)
print("Scheduler started in sandbox 'office-radio'. Logs: /data/scheduler.log")
