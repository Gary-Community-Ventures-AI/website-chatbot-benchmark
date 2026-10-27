import json
import os
import re
import tempfile
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit


class BenchmarkError(Exception):
    pass


class BrowserError(BenchmarkError):
    pass


class NeedsInput(BenchmarkError):
    pass


class Status(StrEnum):
    CORRECT = "correct"
    PARTIALLY_CORRECT = "partially_correct"
    INCORRECT = "incorrect"
    OUTDATED = "outdated"
    UNRESOLVED = "unresolved"
    BROWSER_FAILURE = "browser_failure"
    AGENT_FAILURE = "agent_failure"
    JUDGE_FAILURE = "judge_failure"


SCORES = {
    Status.CORRECT: 1.0,
    Status.PARTIALLY_CORRECT: 0.5,
    Status.INCORRECT: 0.0,
}


def now():
    return datetime.now(timezone.utc).isoformat()


def read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError) as error:
        raise BenchmarkError(f"Cannot read JSON from {path}: {error}") from error


def write_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else None
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as handle:
            if mode is not None:
                os.fchmod(handle.fileno(), mode)
            handle.write(text)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def write_json(path, value):
    write_text(path, json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n")


def parse_object(text):
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text).strip()
    try:
        result = json.loads(text)
    except ValueError as error:
        raise BenchmarkError("The model did not return a valid JSON object.") from error
    if not isinstance(result, dict):
        raise BenchmarkError("The model response must be a JSON object.")
    return result


def normalized(text):
    return " ".join(text.split())


def canonical_url(url, base=None):
    parsed = urlsplit(urljoin(base or url, url))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise BenchmarkError(f"Not a supported public-page URL: {url}")
    # Preserve query parameters: different queries can contain different facts.
    return urlunsplit((parsed.scheme, parsed.netloc.lower(), parsed.path or "/", parsed.query, ""))


def same_origin(url, target):
    def origin(value):
        parsed = urlsplit(value)
        return parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)

    return origin(url) == origin(target)


def metrics(results):
    counts = {status.value: 0 for status in Status}
    for result in results:
        counts[result["status"]] += 1
    graded = sum(counts[status] for status in SCORES)
    answered = sum(result.get("answer") is not None for result in results)
    return {
        "attempted": len(results),
        "answered": answered,
        "graded": graded,
        "counts": counts,
        "accuracy": counts[Status.CORRECT] / graded if graded else None,
        "weighted_score": sum(counts[status] * score for status, score in SCORES.items()) / graded if graded else None,
        "browser_failure_rate": counts[Status.BROWSER_FAILURE] / len(results) if results else None,
        "grading_coverage": graded / len(results) if results else None,
    }
