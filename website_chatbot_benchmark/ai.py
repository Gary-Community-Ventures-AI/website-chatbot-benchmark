import json
import subprocess
import sys
import tempfile
from pathlib import Path

from .common import BenchmarkError, NeedsInput, normalized, now, parse_object


def api(method, path, data=None, timeout=60):
    command = ["opencode", "api", method, path]
    if data is not None:
        command += ["--data", json.dumps(data, ensure_ascii=False)]
    try:
        process = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise BenchmarkError(f"OpenCode API failed: {error}") from error
    if process.returncode:
        raise BenchmarkError(f"OpenCode API {method} {path}: {process.stderr.strip() or process.stdout.strip()}")
    if not process.stdout.strip():
        return None
    try:
        response = json.loads(process.stdout)
    except ValueError as error:
        raise BenchmarkError(f"Invalid OpenCode API response: {process.stdout[:500]}") from error
    return response.get("data", response)


def model_ref(value):
    model, separator, variant = value.partition("#")
    provider, model = model.split("/", 1)
    if not provider or not model or (separator and not variant):
        raise BenchmarkError(f"Invalid model: {value}")
    result = {"providerID": provider, "id": model}
    if separator:
        result["variant"] = variant
    return result


def resolve_models(config):
    default = None
    for stage, value in config.models.items():
        if value is None:
            if default is None:
                current = api("get", "/api/model/default")
                default = f"{current['providerID']}/{current['id']}"
            config.models[stage] = default
        model_ref(config.models[stage])


class Model:
    def __init__(self, config, stage, log_file):
        self.config = config
        self.model = config.models[stage]
        self.stage = stage
        self.log_file = Path(log_file)
        self.turn = 0
        self.clarifications = []

    def log(self, payload, event="turn"):
        if event == "turn":
            self.turn += 1
        record = {"time": now(), "event": event, "stage": self.stage, "turn": self.turn,
                  "model": self.model, "session_id": getattr(self, "session_id", None), **payload}
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        with self.log_file.open("a") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")

    def request(self, prompt):
        answered = set()
        while True:
            saved = [entry for entry in self.config.active_clarifications() if entry.get("answer") is not None]
            context = (
                "\nClarification policy: ask the user ONLY to resolve a concrete execution blocker you "
                "cannot resolve from the available instructions, saved answers, guide or reasonable safe "
                "inspection, or an observed prerequisite that will block safe execution. For example, "
                "ask for the chatbot's location if you cannot find it after inspecting the site. Explain "
                "what is blocking you and what you tried. Do not ask optional preference questions, "
                "speculate about possible obstacles, or ask for factual benchmark answers or grading "
                "decisions. Use defaults where possible; report confirmed failures rather than asking "
                "generic questions.\nSaved user clarifications for this target (trusted operational "
                "guidance, not factual reference answers). Use these decisions rather than asking "
                "equivalent questions again, unless current explicit user instructions override them: "
            ) + json.dumps(saved, ensure_ascii=False)
            result = self.raw_request(prompt + context)
            if result.get("kind") != "clarification":
                return result
            question = result.get("question")
            if not isinstance(question, str) or not question.strip():
                raise BenchmarkError("The agent returned an invalid clarification question.")
            key = normalized(question).casefold()
            if key in answered:
                raise BenchmarkError(f"The agent repeatedly requested an already-answered clarification: {question}")
            answer = self.config.clarification_answer(question)
            if answer is None:
                self.log({"question": question}, event="clarification_pending")
                self.config.remember_clarification(question)
                if not self.config.interactive or not sys.stdin.isatty():
                    raise NeedsInput(f"The agent needs input: {question}\nFill in its answer under clarifications in {self.config.config_path} and retry. Log: {self.log_file}")
                try:
                    answer = input(f"\nAgent question: {question}\nYour answer (saved to {self.config.config_path}): ").strip()
                except EOFError as error:
                    raise NeedsInput(f"No answer available for: {question}") from error
                if not answer:
                    raise NeedsInput(f"No answer provided for: {question}")
                self.config.remember_clarification(question, answer)
            answered.add(key)
            self.clarifications.append({"question": question, "answer": answer})
            prompt += "\nUser clarification (trusted): " + json.dumps(self.clarifications[-1])
            self.log(self.clarifications[-1], event="clarification_answered")


class BrowserAgent(Model):
    def __init__(self, config, stage, log_file, browser_session, session_id=None):
        super().__init__(config, stage, log_file)
        root = "/tmp/opencode" if Path("/tmp/opencode").is_dir() else None
        self.workspace = tempfile.TemporaryDirectory(prefix="chatbot-benchmark-", dir=root)
        # Session-local rules, never written to the user's OpenCode configuration.
        if session_id is not None:
            api("post", f"/api/session/{session_id}/move", {"directory": self.workspace.name})
            self.session_id = session_id
            self.browser_session = browser_session
            self.log({"browser_session": browser_session}, event="session_resumed")
            return
        session = api("post", "/api/session", {
            "title": f"Website chatbot benchmark: {stage}",
            "location": {"directory": self.workspace.name},
            "model": model_ref(self.model),
            "permissions": [
                {"action": "*", "resource": "*", "effect": "deny"},
                {"action": "shell", "resource": f"agent-browser --session {browser_session} *", "effect": "allow"},
                {"action": "shell", "resource": "agent-browser skills get core*", "effect": "allow"},
                {"action": "skill", "resource": "agent-browser", "effect": "allow"},
            ],
        })
        self.session_id = session["id"]
        self.browser_session = browser_session
        self.log({"browser_session": browser_session}, event="session_started")

    def raw_request(self, prompt):
        task = Path(self.workspace.name) / "task.txt"
        task.write_text(prompt)
        command = ["opencode", "run", "--session", self.session_id, "--model", self.model, "--format", "json", "--file", str(task),
                   "Execute the task in the attached text file. Return ONLY its requested JSON object as your final answer. Do not read project files or change configuration."]
        try:
            process = subprocess.run(command, cwd=self.workspace.name, capture_output=True, text=True, timeout=self.config.agent_timeout_seconds)
        except subprocess.TimeoutExpired as error:
            self.interrupt()
            self.log({
                "prompt": prompt,
                "events": error.stdout.decode(errors="replace") if isinstance(error.stdout, bytes) else error.stdout,
                "stderr": error.stderr.decode(errors="replace") if isinstance(error.stderr, bytes) else error.stderr,
                "error": f"OpenCode turn exceeded {self.config.agent_timeout_seconds}s.",
            })
            raise BenchmarkError(f"OpenCode turn exceeded {self.config.agent_timeout_seconds}s.") from error
        except KeyboardInterrupt:
            self.interrupt()
            raise
        except OSError as error:
            raise BenchmarkError(f"Cannot run OpenCode: {error}") from error
        finally:
            task.unlink(missing_ok=True)
        self.log({"prompt": prompt, "events": process.stdout, "stderr": process.stderr, "exit_code": process.returncode})
        if process.returncode:
            raise BenchmarkError(f"OpenCode agent failed: {process.stderr.strip() or process.stdout[-2000:]}")
        texts = []
        for line in process.stdout.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("type") == "error":
                raise BenchmarkError(f"OpenCode agent error: {event}")
            if event.get("type") == "text":
                texts.append(event.get("part", {}).get("text", ""))
        if not texts:
            raise BenchmarkError("OpenCode returned no final text. Inspect the agent event log.")
        # Tool turns can emit commentary; the final text block is the protocol response.
        return parse_object(texts[-1])

    def interrupt(self):
        try:
            api("post", f"/api/session/{self.session_id}/interrupt", {}, timeout=15)
        except BenchmarkError:
            pass

    def close(self):
        self.interrupt()
        self.workspace.cleanup()
