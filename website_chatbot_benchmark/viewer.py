import shutil
import subprocess
import webbrowser
from pathlib import Path
from threading import Timer

from flask import Flask, jsonify, send_file

from .common import BenchmarkError, read_json
from .config import CONFIG_FILENAMES, default_config, read_config
from .workspaces import BENCHMARKS_DIRECTORY


def local_runs(root):
    runs = []
    errors = []
    directory = root / BENCHMARKS_DIRECTORY
    projects = [path for path in directory.iterdir() if path.is_dir() and not path.name.startswith(".")] if directory.is_dir() else []
    for project in projects:
        if not any((project / name).is_file() for name in CONFIG_FILENAMES):
            continue
        try:
            config = read_config(default_config(project))
            directory = (project / config.get("results_directory", "runs")).resolve()
            if not directory.is_relative_to(root):
                errors.append(f"{project.name}: results directory is outside the viewer workspace.")
                continue
            for path in directory.glob("*.json"):
                if not path.resolve().is_relative_to(root):
                    continue
                try:
                    report = read_json(path)
                    if not isinstance(report, dict) or not isinstance(report.get("results"), list):
                        continue
                    runs.append({"project": project.name, "filename": path.name,
                                 "modified": path.stat().st_mtime * 1000, "report": report})
                except (BenchmarkError, OSError):
                    errors.append(f"{project.name}: could not read {path.name}.")
        except (BenchmarkError, OSError, TypeError):
            errors.append(f"{project.name}: could not read project configuration.")
    runs.sort(key=lambda item: item["modified"], reverse=True)
    return {"runs": runs, "errors": errors}


def create_app(root, assets):
    root = Path(root).resolve()
    assets = Path(assets).resolve()
    app = Flask(__name__, static_folder=str(assets / "assets"), static_url_path="/assets")
    app.config["TRUSTED_HOSTS"] = ["127.0.0.1", "localhost"]

    @app.get("/")
    def index():
        return send_file(assets / "index.html", max_age=0)

    @app.get("/api/runs")
    def results():
        response = jsonify(local_runs(root))
        response.headers["Cache-Control"] = "no-store"
        return response

    return app


def view(port=5555, open_browser=True):
    directory = Path(__file__).resolve().parent.parent / "frontend"
    if not (directory / "package.json").is_file():
        raise BenchmarkError("Viewer sources not found. Run this command from an editable installation of this repository.")
    if not shutil.which("npm"):
        raise BenchmarkError("The results viewer requires Node.js and npm.")
    if not (directory / "node_modules" / "vite").is_dir():
        print("Installing viewer dependencies ...", flush=True)
        installed = subprocess.run(["npm", "ci"], cwd=directory)
        if installed.returncode:
            raise BenchmarkError("Viewer dependency installation failed.")
    print("Building results viewer ...", flush=True)
    if subprocess.run(["npm", "run", "build"], cwd=directory).returncode:
        raise BenchmarkError("Viewer build failed.")
    app = create_app(Path.cwd(), directory / "dist")
    print(f"Opening results viewer at http://127.0.0.1:{port}. Press Ctrl+C to stop.", flush=True)
    if open_browser:
        timer = Timer(1, webbrowser.open, args=(f"http://127.0.0.1:{port}",))
        timer.daemon = True
        timer.start()
    try:
        app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)
    except KeyboardInterrupt:
        return
