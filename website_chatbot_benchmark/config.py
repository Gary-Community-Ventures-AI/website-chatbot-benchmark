from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from io import StringIO
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedSeq
from ruamel.yaml.error import YAMLError

from .common import BenchmarkError, canonical_url, normalized, same_origin, write_json, write_text


CONFIG_FILENAME = "config.yaml"
CONFIG_FILENAMES = (CONFIG_FILENAME, "config.yml", "benchmark.yaml", "benchmark.yml")
EXAMPLE_CONFIG_FILENAME = "config.example.yaml"


def default_config(directory="."):
    directory = Path(directory)
    for name in CONFIG_FILENAMES:
        path = directory / name
        if path.is_file():
            return str(path)
    return str(directory / CONFIG_FILENAME)


def read_config(path):
    try:
        # Safe loading also accepts existing JSON configs without executing YAML tags.
        raw = YAML(typ="safe").load(Path(path).read_text())
    except (OSError, YAMLError) as error:
        raise BenchmarkError(f"Cannot read config from {path}: {error}") from error
    if raw is None:
        return {}
    if not isinstance(raw, dict) or not all(isinstance(key, str) for key in raw):
        raise BenchmarkError("Config must be a mapping with string field names.")
    return raw


def validate_clarifications(entries, target):
    if not isinstance(entries, list):
        raise BenchmarkError("clarifications must be a list of question/answer mappings.")
    seen = []
    for entry in entries:
        if not isinstance(entry, Mapping) or entry.keys() - {"question", "answer", "target_url"}:
            raise BenchmarkError("Each clarification must contain question, answer, and optionally target_url.")
        if not isinstance(entry.get("question"), str) or not entry["question"].strip():
            raise BenchmarkError("Each clarification needs a nonempty question.")
        answer = entry.get("answer")
        if answer is not None and (not isinstance(answer, str) or not answer.strip()):
            raise BenchmarkError("Clarification answers must be nonempty strings or null for unanswered questions.")
        scope = entry.get("target_url", target)
        if not isinstance(scope, str):
            raise BenchmarkError("Clarification target_url must be a website URL.")
        canonical_url(scope)
        key = normalized(entry["question"]).casefold()
        if any(key == previous_key and same_origin(scope, previous_scope) for previous_key, previous_scope in seen):
            raise BenchmarkError(f"Duplicate clarification for the same target origin: {entry['question']}")
        seen.append((key, scope))


@dataclass
class Config:
    url: str = "http://localhost:5173/"
    models: dict = field(default_factory=lambda: {"generation": None, "runner": None})
    question_limit: int | None = None
    page_limit: int | None = None
    instructions: list[str] = field(default_factory=list)
    clarifications: list[dict] = field(default_factory=list)
    agent_timeout_seconds: int = 600
    browser_timeout_seconds: int = 60
    response_timeout_seconds: int = 120
    headed: bool = False
    interactive: bool = True
    benchmark_file: str = "benchmark.json"
    results_directory: str = "runs"

    def __post_init__(self):
        self._config_path = None
        self._clarification_target = self.url

    def to_dict(self):
        return {key: value for key, value in asdict(self).items() if key not in {"benchmark_file", "results_directory"}}

    @property
    def config_path(self):
        return self._config_path

    def active_clarifications(self):
        return [entry for entry in self.clarifications if same_origin(entry.get("target_url", self._clarification_target), self.url)]

    def clarification_answer(self, question):
        key = normalized(question).casefold()
        for entry in self.active_clarifications():
            if normalized(entry["question"]).casefold() == key:
                return entry.get("answer")
        return None

    def remember_clarification(self, question, answer=None):
        path = self.config_path
        if path is None:
            raise BenchmarkError("Cannot save clarification: this config has no source file.")
        formatter = YAML()
        formatter.preserve_quotes = True
        formatter.indent(mapping=2, sequence=4, offset=2)
        formatter.width = 1000
        formatter.representer.add_representer(type(None), lambda dumper, value: dumper.represent_scalar("tag:yaml.org,2002:null", "null"))
        try:
            # Re-read the file so unrelated edits and CLI-only overrides are not overwritten.
            document = formatter.load(path.read_text()) if path.exists() else {}
            if document is None:
                document = {}
            if not isinstance(document, Mapping):
                raise BenchmarkError("Cannot save clarification: config must be a mapping.")
            target = canonical_url(document.get("url", self._clarification_target))
            entries = document.setdefault("clarifications", CommentedSeq())
            validate_clarifications(entries, target)
            if not entries and isinstance(entries, CommentedSeq):
                entries.fa.set_block_style()
            key = normalized(question).casefold()
            entry = next((item for item in entries if normalized(item["question"]).casefold() == key
                          and same_origin(item.get("target_url", target), self.url)), None)
            if entry is None:
                entry = {"question": question.strip(), "answer": answer, "target_url": self.url}
                entries.append(entry)
            elif answer is not None:
                entry["answer"] = answer
            entry.setdefault("target_url", target)
            validate_clarifications(entries, target)
            if path.suffix.lower() == ".json":
                write_json(path, document)
            else:
                buffer = StringIO()
                formatter.dump(document, buffer)
                write_text(path, buffer.getvalue())
            self.clarifications = [dict(item) for item in entries]
            self._clarification_target = target
        except (OSError, YAMLError) as error:
            raise BenchmarkError(f"Cannot save clarification to {path}: {error}") from error


def load_config(arguments):
    path = Path(arguments.config).resolve()
    if not path.exists() and getattr(arguments, "config_required", True):
        raise BenchmarkError(f"Config file does not exist: {path}")
    raw = read_config(path) if path.exists() else {}
    unknown = raw.keys() - Config.__dataclass_fields__.keys()
    if unknown:
        raise BenchmarkError(f"Unknown config keys: {', '.join(sorted(unknown))}")
    config = Config(**raw)
    config._config_path = path
    config._clarification_target = canonical_url(config.url)
    validate_clarifications(config.clarifications, config._clarification_target)
    if not isinstance(config.models, dict) or config.models.keys() - {"generation", "runner", "judge"}:
        raise BenchmarkError("models must contain only generation, runner, and judge.")
    # Accept legacy judge settings, but evaluation now belongs to the runner.
    config.models = {"generation": None, "runner": None} | {key: value for key, value in config.models.items() if key != "judge"}
    for name in ("url", "question_limit", "page_limit", "agent_timeout_seconds", "browser_timeout_seconds", "response_timeout_seconds", "headed", "interactive"):
        value = getattr(arguments, name, None)
        if value is not None:
            setattr(config, name, value)
    if arguments.all_questions:
        config.question_limit = None
    if arguments.all_pages:
        config.page_limit = None
    for stage in config.models:
        model = getattr(arguments, f"{stage}_model", None) or arguments.model
        if model:
            config.models[stage] = model
        if config.models[stage] is not None and (not isinstance(config.models[stage], str) or "/" not in config.models[stage]):
            raise BenchmarkError(f"models.{stage} must be null or provider/model[#variant].")
    if not isinstance(config.instructions, list) or not all(isinstance(item, str) for item in config.instructions):
        raise BenchmarkError("instructions must be an array of strings.")
    config.instructions = list(config.instructions) + arguments.instructions
    for name in arguments.instructions_file:
        try:
            config.instructions.append(Path(name).read_text())
        except OSError as error:
            raise BenchmarkError(f"Cannot read instructions file {name}: {error}") from error
    for name in ("question_limit", "page_limit", "agent_timeout_seconds", "browser_timeout_seconds", "response_timeout_seconds"):
        value = getattr(config, name)
        if value is None and name.endswith("limit"):
            continue
        if type(value) is not int or value < 1:
            raise BenchmarkError(f"{name} must be a positive integer" + (" or null." if name.endswith("limit") else "."))
    for name in ("headed", "interactive"):
        if type(getattr(config, name)) is not bool:
            raise BenchmarkError(f"{name} must be a boolean.")
    for name in ("benchmark_file", "results_directory"):
        value = getattr(config, name)
        if not isinstance(value, str) or not value:
            raise BenchmarkError(f"{name} must be a nonempty path string.")
        setattr(config, name, str((path.parent / value).resolve()))
    config.url = canonical_url(config.url)
    return config
