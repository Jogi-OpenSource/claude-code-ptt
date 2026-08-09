"""Tests for the Hub chatter the installer silences around the download.

_fetch_model() draws one self-overwriting \\r line while the weights come
down. huggingface_hub logs whatever notice the Hub sends back with a request -
"You are sending unauthenticated requests to the HF Hub" arrives right as the
download starts - and that log line lands in the middle of the progress line,
which then reads as garbage.

The notice is not a string in the library: the server sends it as an
`X-HF-Warning` header and `hf_raise_for_status` hands it to the
`huggingface_hub` logger. These tests feed that header through the real
function - no network, no model - and check the line is gone afterwards while
a genuinely failed request still raises.

Every case runs in its own interpreter: HF_HUB_VERBOSITY is read once, when
huggingface_hub configures its logger, so the import order decides the result.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

_http = pytest.importorskip("huggingface_hub.utils._http")
if not hasattr(_http, "_warn_on_warning_headers"):
    pytest.skip("huggingface_hub predates the server notice header",
                allow_module_level=True)
pytest.importorskip("httpx")

REPO = Path(__file__).resolve().parent.parent
NOTICE = ("You are sending unauthenticated requests to the HF Hub. "
          "Please log in to get higher rate limits.")

PROBE = """
import sys

if sys.argv[1] == "installer":
    from claude_code_ptt import installer          # sets the env at import

import httpx
from huggingface_hub.utils import hf_raise_for_status

response = httpx.Response(
    int(sys.argv[2]),
    headers=[("X-HF-Warning", "unauthenticated; " + sys.argv[3])],
    request=httpx.Request("GET", "https://huggingface.co/nowhere"),
)
try:
    hf_raise_for_status(response)
except Exception as exc:
    print(type(exc).__name__)
"""


def _probe(mode: str, status: int = 200) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    # Whatever the machine running the tests prefers must not decide this.
    env.pop("HF_HUB_VERBOSITY", None)
    return subprocess.run(
        [sys.executable, "-c", PROBE, mode, str(status), NOTICE],
        cwd=REPO, env=env, capture_output=True, text=True, check=True,
    )


def test_the_notice_reaches_the_console_without_the_installer():
    """Guards the test itself: no fix, and the line is on screen."""
    assert NOTICE in _probe("plain").stderr


def test_importing_the_installer_silences_the_notice():
    done = _probe("installer")
    assert NOTICE not in done.stderr + done.stdout


def test_a_failed_request_still_raises():
    """Silencing notices must not swallow what the installer has to report."""
    assert _probe("installer", status=404).stdout.strip() == "HfHubHTTPError"
