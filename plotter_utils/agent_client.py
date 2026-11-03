import json
import time
import urllib.error
import urllib.request

from .gcode_sender import GrblError, GrblStopped, clean_gcode_line, read_gcode

__all__ = ["RemoteConnection"]


class RemoteConnection:
    """
    A connection to a plotter on another machine, through the G-code agent running there
    (python -m plotter_utils.agent). It has the same methods as GrblConnection, so the
    pipeline, the job runner and the tablet page use it without changes:

        conn = pu.RemoteConnection("http://plotter-zero.local:8765", token)
        conn.send_file("job.gcode")

    Usually you don't build it yourself: set profile.agent_url (and agent_token) and
    pu.connect(profile) returns one.

    send_lines() hands the whole job to the agent, which streams it locally, then polls for
    progress. If contact is lost the job keeps running on the agent until its watchdog stops
    it (see the agent docs), and send_lines raises GrblError after `lost_after_s`.
    """
    simulated = False

    def __init__(self, url, token="", timeout=5.0, poll_s=0.3, lost_after_s=15.0, abort_lines=None):
        self.url = url.rstrip("/")
        self.port = self.url  # what GrblConnection calls the port, for display
        self.token = token
        self.timeout = timeout
        self.poll_s = poll_s
        self.lost_after_s = lost_after_s
        self.abort_lines = list(abort_lines or [])
        self._stopped = False
        self._get("/status")  # fail now, with a clear message, if the agent isn't reachable

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        pass  # nothing is held open between requests

    # ---- HTTP -----------------------------------------------------------------

    def _call(self, method, path, body=None, timeout=None):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.url + path, data=data, method=method, headers={
            "X-Token": self.token, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            try:
                message = json.loads(e.read()).get("error", "")
            except ValueError:
                message = ""
            if e.code == 403:
                raise GrblError(f"The plotter agent at {self.url} rejected the token "
                                f"(set profile.agent_token or PLOTTER_AGENT_TOKEN)")
            raise GrblError(message or f"The plotter agent answered HTTP {e.code}")
        except (urllib.error.URLError, OSError) as e:
            raise GrblError(f"Cannot reach the plotter agent at {self.url}: {getattr(e, 'reason', e)}")

    def _get(self, path):
        return self._call("GET", path)

    def _post(self, path, body=None, timeout=None):
        return self._call("POST", path, body or {}, timeout)

    # ---- the GrblConnection interface -------------------------------------------

    def status(self):
        """GRBL's status report from the agent, or None while a job is running (the agent's
        streamer owns the serial port then) or if the controller doesn't answer."""
        return self._get("/status").get("grbl")

    def command(self, line):
        """Sends one line, returns the response lines before 'ok'. Raises GrblError on a controller error."""
        return self._post("/command", {"line": line}).get("response", [])

    def unlock(self):
        return self._post("/unlock").get("response", [])

    def set_zero(self):
        return self.command("G92 X0 Y0")

    def pen_up(self, z=0):
        return self.command(f"G00 Z{z:g}")

    def pen_down(self, z=6):
        return self.command(f"G00 Z{z:g}")

    def stop(self):
        """Emergency stop on the agent: feed hold + reset. A running send_lines raises GrblStopped."""
        self._stopped = True
        self._post("/stop")

    def send_lines(self, lines, buffered=True, on_progress=None):
        lines = [l for l in (clean_gcode_line(l) for l in lines) if l]
        self._stopped = False
        wait_s = 2.0  # a short job (a pen move) completes inside this request, so it feels instant
        try:
            job = self._post("/job", {"lines": lines, "buffered": buffered, "abort_lines": self.abort_lines,
                                      "wait_s": wait_s}, timeout=self.timeout + wait_s)["job"]
            job_id, last_done, last_ok = job["id"], -1, time.monotonic()
            while True:
                if job["done"] != last_done:
                    last_done = job["done"]
                    if on_progress:
                        on_progress(last_done, job["total"])
                if job["state"] == "done":
                    return
                if job["state"] == "stopped":
                    raise GrblStopped(job.get("message") or "Stopped")
                if job["state"] == "error":
                    raise GrblError(job.get("message") or "The agent reported an error")
                time.sleep(self.poll_s)
                try:
                    status = self._get("/status")
                    last_ok = time.monotonic()
                except GrblError as e:
                    if time.monotonic() - last_ok > self.lost_after_s:
                        raise GrblError(f"Lost contact with the plotter agent ({e}). The job keeps running there "
                                        f"until the agent's watchdog stops it.")
                    continue
                job = status["job"]
                if job["id"] != job_id:
                    raise GrblError("The agent is running a different job now (was it restarted?)")
        except KeyboardInterrupt:
            self.stop()
            raise

    def send_file(self, filename, buffered=True, on_progress=None):
        self.send_lines(read_gcode(filename), buffered=buffered, on_progress=on_progress)
