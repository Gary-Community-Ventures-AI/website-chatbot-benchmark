import argparse
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from .ai import resolve_models
from .common import BenchmarkError, NeedsInput
from .config import default_config, load_config
from .generation import generate
from .running import run
from .viewer import view
from .workspaces import COMMANDS, create_workspace, validate_name, workspace_config


def parser():
    root = argparse.ArgumentParser(
        prog="benchmark",
        usage="%(prog)s {new NAME | view | [NAME] {generate,run}} ...",
        description="Generate grounded website chatbot benchmarks and run them through the browser.",
        epilog="Use 'benchmark new NAME' to create benchmarks/NAME/config.yaml, then 'benchmark NAME generate' or 'benchmark NAME run'.",
    )
    commands = root.add_subparsers(dest="command", required=True)
    new = commands.add_parser("new", help="Create a named benchmark directory from the example config.")
    new.add_argument("name", help="Name of the new project under benchmarks/.")
    viewer = commands.add_parser("view", help="Open the local results viewer.")
    viewer.add_argument("--port", type=int, default=5555, help="Local viewer port (default: 5555).")
    viewer.add_argument("--open", dest="open_browser", action=argparse.BooleanOptionalAction, default=True, help="Open the viewer in your browser.")
    for name, description in (("generate", "Explore the site and generate grounded questions and a chatbot guide."), ("run", "Ask benchmark questions, using fresh chats when convenient, and classify answers.")):
        command = commands.add_parser(name, help=description, description=description)
        command.add_argument("--config", help="Benchmark YAML config; defaults to benchmarks/NAME/config.yaml when named, otherwise config.yaml. Also accepts .yml and explicit legacy JSON config paths. Never OpenCode config.")
        command.add_argument("--url", help="Target site URL (run must match the generated benchmark origin).")
        questions = command.add_mutually_exclusive_group()
        questions.add_argument("--limit", dest="question_limit", type=int, help="Maximum number of questions; default: all factual coverage.")
        questions.add_argument("--all-questions", action="store_true", help="Override a configured question limit.")
        pages = command.add_mutually_exclusive_group()
        pages.add_argument("--max-pages", dest="page_limit", type=int, help="Maximum number of pages to crawl; default: no limit.")
        pages.add_argument("--all-pages", action="store_true", help="Override a configured page limit.")
        command.add_argument("--model", help="Override all models with provider/model[#variant].")
        for stage in ("generation", "runner"):
            command.add_argument(f"--{stage}-model", help=f"Override only the {stage} model (takes precedence over --model).")
        command.add_argument("--instructions", action="append", default=[], help="Additional trusted instructions; repeatable.")
        command.add_argument("--instructions-file", action="append", default=[], help="Read additional trusted instructions from a file; repeatable.")
        command.add_argument("--agent-timeout", dest="agent_timeout_seconds", type=int, help="Maximum seconds per OpenCode turn.")
        command.add_argument("--browser-timeout", dest="browser_timeout_seconds", type=int, help="Maximum seconds per direct browser command.")
        command.add_argument("--response-timeout", dest="response_timeout_seconds", type=int, help="Maximum seconds to wait for a chatbot response.")
        command.add_argument("--headed", action=argparse.BooleanOptionalAction, default=None, help="Show the browser window.")
        command.add_argument("--interactive", action=argparse.BooleanOptionalAction, default=None, help="Allow terminal clarification questions (requires a TTY).")
        command.add_argument("--output", help="Output JSON path; existing files are protected by default.")
        command.add_argument("--force", action="store_true", help="Explicitly allow replacing the selected output file.")
        if name == "run":
            command.add_argument("--benchmark", help="Generated benchmark JSON; default: config benchmark_file.")
    return root


def parse_arguments(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    name = None
    if argv and not argv[0].startswith("-") and argv[0] not in COMMANDS:
        name = validate_name(argv.pop(0))
    root = parser()
    arguments = root.parse_args(argv)
    if arguments.command == "view":
        if name is not None:
            root.error("Use 'benchmark view'; open project results from the viewer.")
        if not 1 <= arguments.port <= 65535:
            root.error("--port must be between 1 and 65535.")
        return arguments
    if arguments.command != "new":
        arguments.config_required = arguments.config is not None or name is not None
    if name is not None:
        if arguments.command == "new":
            root.error("Use 'benchmark new NAME', not 'benchmark NAME new'.")
        named_config = workspace_config(name)
        arguments.config = arguments.config or named_config
    elif arguments.command != "new":
        arguments.config = arguments.config or default_config()
    return arguments


def main():
    try:
        arguments = parse_arguments()
        if arguments.command == "new":
            create_workspace(arguments.name)
            return
        if arguments.command == "view":
            view(arguments.port, arguments.open_browser)
            return
        config = load_config(arguments)
        for executable in ("opencode", "agent-browser"):
            if not shutil.which(executable):
                raise BenchmarkError(f"Required executable not on PATH: {executable}")
        resolve_models(config)
        if arguments.command == "generate":
            generate(config, arguments.output or config.benchmark_file, arguments.force)
        else:
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            output = arguments.output or str(Path(config.results_directory) / f"{timestamp}.json")
            run(config, arguments.benchmark or config.benchmark_file, output, arguments.force, arguments.url is not None)
    except NeedsInput as error:
        print(f"Input required: {error}", file=sys.stderr)
        raise SystemExit(3) from error
    except (BenchmarkError, subprocess.TimeoutExpired, OSError, ValueError, TypeError, KeyError) as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    except KeyboardInterrupt:
        print("Interrupted. Any completed work has been checkpointed to JSON.", file=sys.stderr)
        raise SystemExit(130) from None
