import json
import os
import subprocess

from .common import BrowserError, canonical_url, same_origin


CAPTURE = """JSON.stringify({
  url: location.href,
  title: document.title,
  text: document.body.innerText,
  links: Array.from(document.querySelectorAll('a[href]')).map(a => a.href)
})"""


class Browser:
    def __init__(self, session, config):
        self.session = session
        self.config = config

    def command(self, *arguments):
        command = ["agent-browser", "--session", self.session, "--json", *arguments]
        environment = os.environ.copy()
        environment["AGENT_BROWSER_DEFAULT_TIMEOUT"] = str(self.config.browser_timeout_seconds * 1000)
        try:
            process = subprocess.run(command, capture_output=True, text=True, timeout=self.config.browser_timeout_seconds + 15, env=environment)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise BrowserError(f"agent-browser failed: {error}") from error
        try:
            response = json.loads(process.stdout)
        except ValueError as error:
            raise BrowserError(f"agent-browser returned invalid JSON: {process.stderr or process.stdout}") from error
        if process.returncode or not response.get("success"):
            raise BrowserError(f"agent-browser: {response.get('error') or process.stderr or response}")
        return response.get("data")

    def open(self, url):
        if not same_origin(url, self.config.url):
            raise BrowserError(f"Refusing to navigate outside the target origin: {url}")
        arguments = ["open", url]
        if self.config.headed:
            arguments.append("--headed")
        self.command(*arguments)
        self.command("wait", "--load", "domcontentloaded")
        return self.capture()

    def capture(self):
        data = self.command("eval", CAPTURE)
        value = data.get("result") if isinstance(data, dict) else data
        if isinstance(value, str):
            value = json.loads(value)
        if not isinstance(value, dict) or not isinstance(value.get("text"), str):
            raise BrowserError(f"Unexpected page capture: {data}")
        value["url"] = canonical_url(value["url"])
        if not same_origin(value["url"], self.config.url):
            raise BrowserError(f"Page redirected outside the target origin: {value['url']}")
        return value

    def close(self):
        try:
            self.command("close")
        except BrowserError:
            pass
