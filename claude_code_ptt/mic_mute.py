"""System microphone mute control (Windows Core Audio via pycaw).

The daemon records from the default input device; if that device is muted on
the OS level the stream delivers silence. So recording start unmutes the
microphone and recording stop restores the previous state - the hotkey alone
is enough, no separate OS mute toggle needed.

All COM work runs on one dedicated, permanently COM-initialized thread: COM
endpoint pointers must be released by the thread that created them. Left to
the garbage collector they may get released from a thread without COM, which
crashes the process (access violation in ctypes). The worker therefore runs
gc.collect() after every job so COM garbage never survives into a foreign
thread's collection.
"""
import gc
import logging
import queue
import threading

log = logging.getLogger("claude_code_ptt")

_JOB_TIMEOUT = 3.0


class MicMute:
    """Unmute the default capture device for recording, then restore."""

    def __init__(self):
        self._was_muted = False
        self._jobs: queue.Queue = queue.Queue()
        self._worker: threading.Thread | None = None

    # ------------------------------------------------------ COM worker
    def _ensure_worker(self) -> None:
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(
                target=self._com_loop, daemon=True, name="mic-mute-com")
            self._worker.start()

    def _com_loop(self) -> None:
        import comtypes

        try:
            comtypes.CoInitialize()
        except OSError:
            pass                           # COM already initialized here
        while True:
            job, done = self._jobs.get()
            try:
                job()
            except Exception:              # noqa: BLE001
                log.exception("microphone mute operation failed")
            finally:
                # Release COM garbage while this thread's apartment lives.
                gc.collect()
                done.set()

    def _run(self, job) -> None:
        self._ensure_worker()
        done = threading.Event()
        self._jobs.put((job, done))
        if not done.wait(_JOB_TIMEOUT):
            log.warning("microphone mute operation timed out")

    @staticmethod
    def _endpoint():
        import comtypes
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

        device = AudioUtilities.GetMicrophone()
        if device is None:
            raise RuntimeError("no default capture device found")
        interface = device.Activate(
            IAudioEndpointVolume._iid_, comtypes.CLSCTX_ALL, None)
        # QueryInterface adds a reference of its own. The widespread
        # ctypes.cast() pattern shares ONE reference between two wrappers,
        # whose two __del__ Releases corrupt the object and crash the
        # process (access violation) as soon as the GC runs.
        return interface.QueryInterface(IAudioEndpointVolume)

    # ------------------------------------------------------ public API
    def open_for_recording(self) -> None:
        """Remember the current mute state and unmute the microphone."""
        def job() -> None:
            try:
                endpoint = self._endpoint()
                self._was_muted = bool(endpoint.GetMute())
                if self._was_muted:
                    endpoint.SetMute(False, None)
                    log.info("microphone unmuted for recording")
            except Exception:              # noqa: BLE001
                self._was_muted = False
                log.exception(
                    "could not unmute the microphone - recording anyway")

        self._run(job)

    def restore(self) -> None:
        """Re-mute the microphone if it was muted before recording."""
        if not self._was_muted:
            return
        self._was_muted = False

        def job() -> None:
            try:
                self._endpoint().SetMute(True, None)
                log.info("microphone re-muted after recording")
            except Exception:              # noqa: BLE001
                log.exception("could not re-mute the microphone")

        self._run(job)
