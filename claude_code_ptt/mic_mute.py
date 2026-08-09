"""System microphone mute control (Windows Core Audio via pycaw).

The daemon records from the default input device; if that device is muted on
the OS level the stream delivers silence. So recording start unmutes the
microphone and recording stop restores the previous state - the hotkey alone
is enough, no separate OS mute toggle needed.
"""
import logging

log = logging.getLogger("claude_code_ptt")


class MicMute:
    """Unmute the default capture device for recording, then restore."""

    def __init__(self):
        self._was_muted = False

    def _endpoint(self):
        from ctypes import POINTER, cast

        import comtypes
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

        try:
            comtypes.CoInitialize()
        except OSError:
            pass                           # COM already initialized here
        device = AudioUtilities.GetMicrophone()
        if device is None:
            raise RuntimeError("no default capture device found")
        interface = device.Activate(
            IAudioEndpointVolume._iid_, comtypes.CLSCTX_ALL, None)
        return cast(interface, POINTER(IAudioEndpointVolume))

    def open_for_recording(self) -> None:
        """Remember the current mute state and unmute the microphone."""
        try:
            endpoint = self._endpoint()
            self._was_muted = bool(endpoint.GetMute())
            if self._was_muted:
                endpoint.SetMute(False, None)
                log.info("microphone unmuted for recording")
        except Exception:                  # noqa: BLE001
            self._was_muted = False
            log.exception("could not unmute the microphone - recording anyway")

    def restore(self) -> None:
        """Re-mute the microphone if it was muted before recording."""
        if not self._was_muted:
            return
        self._was_muted = False
        try:
            self._endpoint().SetMute(True, None)
            log.info("microphone re-muted after recording")
        except Exception:                  # noqa: BLE001
            log.exception("could not re-mute the microphone")
