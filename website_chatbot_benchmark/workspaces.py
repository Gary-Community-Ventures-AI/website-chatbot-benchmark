import re
import sysconfig
from pathlib import Path

from .common import BenchmarkError
from .config import CONFIG_FILENAME, EXAMPLE_CONFIG_FILENAME, default_config


COMMANDS = {"new", "generate", "run", "view"}
BENCHMARKS_DIRECTORY = "benchmarks"


def validate_name(name):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name) or name in COMMANDS:
        raise BenchmarkError("Benchmark names must start with a letter or number, contain only letters, numbers, dots, hyphens or underscores, and not be new, generate, run or view.")
    return name


def workspace_config(name):
    path = Path(default_config(Path(BENCHMARKS_DIRECTORY) / validate_name(name)))
    if not path.is_file():
        raise BenchmarkError(f"Benchmark config not found: {path}. Create it with 'benchmark new {name}'.")
    return str(path)


def example_config():
    candidates = [Path(__file__).resolve().parent.parent / EXAMPLE_CONFIG_FILENAME]
    for scheme in (sysconfig.get_default_scheme(), sysconfig.get_preferred_scheme("user")):
        candidates.append(Path(sysconfig.get_path("data", scheme=scheme)) / "share" / "website-chatbot-benchmark" / EXAMPLE_CONFIG_FILENAME)
    for path in candidates:
        if path.is_file():
            return path
    raise BenchmarkError(f"Cannot find the bundled {EXAMPLE_CONFIG_FILENAME} template. Reinstall the package or run from the source checkout.")


def create_workspace(name):
    directory = Path(BENCHMARKS_DIRECTORY) / validate_name(name)
    template = example_config().read_bytes()
    directory.parent.mkdir(parents=True, exist_ok=True)
    try:
        directory.mkdir()
    except FileExistsError as error:
        raise BenchmarkError(f"Directory already exists: {directory}. Choose a different benchmark name; existing files were not changed.") from error
    path = directory / CONFIG_FILENAME
    with path.open("xb") as handle:
        handle.write(template)
    print(f"Created {path}")
    return path
