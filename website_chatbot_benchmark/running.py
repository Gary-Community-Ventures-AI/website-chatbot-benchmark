import hashlib
import time
from pathlib import Path
from uuid import uuid4

from .ai import BrowserAgent, TextModel
from .browser import Browser
from .common import BenchmarkError, BrowserError, NeedsInput, SCORES, Status, canonical_url, metrics, now, read_json, same_origin, write_json
from .generation import validate_guide, validate_questions
from .prompts import judge_prompt, runner_prompt


def run(config, benchmark_path, output, force=False, url_override=False):
    output = Path(output)
    if output.exists() and not force:
        raise BenchmarkError(f"Output already exists: {output}. Choose --output or explicitly use --force.")
    benchmark = read_json(benchmark_path)
    if not isinstance(benchmark, dict) or benchmark.get("schema_version") != 1 or benchmark.get("status") != "ready":
        raise BenchmarkError("Expected a ready schema_version=1 benchmark. Finish generation first.")
    target = canonical_url(benchmark["target_url"])
    if url_override and not same_origin(target, config.url):
        raise BenchmarkError("The URL override changes the benchmark origin. Regenerate the guide and sources for that origin.")
    config.url = target
    validate_guide(benchmark.get("guide"), target)
    questions = benchmark.get("questions", [])
    if not isinstance(questions, list) or not questions:
        raise BenchmarkError("The benchmark has no questions.")
    sources = benchmark.get("sources", [])
    validate_questions({"kind": "questions", "done": True, "questions": questions}, sources, [])
    ids = [question.get("id") for question in questions]
    if not all(isinstance(item, str) and item for item in ids) or len(set(ids)) != len(ids):
        raise BenchmarkError("Every question must have a unique nonempty id.")
    if config.question_limit is not None:
        questions = questions[:config.question_limit]
    identifier = uuid4().hex
    log_directory = output.parent / f"{output.stem}-logs"
    browser_session = f"benchmark-run-{identifier}"
    browser = Browser(browser_session, config)
    runner = None
    report = {
        "schema_version": 1,
        "id": identifier,
        "started_at": now(),
        "updated_at": now(),
        "status": "running",
        "target_url": target,
        "benchmark_id": benchmark["id"],
        "benchmark_file": str(Path(benchmark_path).resolve()),
        "benchmark_sha256": hashlib.sha256(Path(benchmark_path).read_bytes()).hexdigest(),
        "config": config.to_dict(),
        "planned_questions": len(questions),
        "results": [],
        "metrics": metrics([]),
        "logs_directory": str(log_directory.resolve()),
        "clarifications": [],
    }

    def save():
        report["updated_at"] = now()
        report["metrics"] = metrics(report["results"])
        if runner:
            report["clarifications"] = runner.clarifications
        write_json(output, report)

    save()
    try:
        runner = BrowserAgent(config, "runner", log_directory / "runner", browser_session)
        judge = TextModel(config, "judge", log_directory / "judge")
        report["opencode_session_id"] = runner.session_id
        save()
        print(f"Running {len(questions)} questions with {config.models['runner']}; judge: {config.models['judge']}", flush=True)
        for index, question in enumerate(questions):
            started = time.monotonic()
            result = {
                "question_id": question["id"], "question": question["question"],
                "expected_answer": question["expected_answer"], "key_points": question["key_points"],
                "evidence": question["evidence"], "started_at": now(), "answer": None,
                "fresh_chat_verified": False, "status": Status.AGENT_FAILURE,
                "reason": "Agent has not yet returned a result.", "score": None,
            }
            print(f"  [{index + 1}/{len(questions)}] {question['question']}", flush=True)
            try:
                first_turn = runner.turn == 0
                if first_turn:
                    browser.open(benchmark["guide"]["entry_url"])
                response = runner.request(runner_prompt(config, browser_session, benchmark["guide"], question["question"], first_turn))
                result["fresh_chat_verified"] = response.get("fresh_chat_verified") is True
                if response.get("kind") == "browser_failure":
                    raise BrowserError(str(response.get("error") or "Unspecified chatbot UI failure."))
                if response.get("kind") != "answer" or not isinstance(response.get("answer"), str) or not response["answer"].strip():
                    raise BenchmarkError("Runner did not return a nonempty verbatim chatbot answer.")
                if not result["fresh_chat_verified"]:
                    raise BrowserError("Runner could not verify a fresh chatbot conversation.")
                result["answer"] = response["answer"]
                result["browser_seconds"] = round(time.monotonic() - started, 3)
                # Checkpoint the actual answer before invoking a fallible judge.
                result.update(status=Status.JUDGE_FAILURE, reason="Answer saved; grading has not completed.")
                report["results"].append(result)
                save()
                supporting = [
                    {"id": source["id"], "url": source["url"], "title": source["title"],
                     "supporting_quotes": [entry["quote"] for entry in question["evidence"] if entry["source_id"] == source["id"]]}
                    for source in sources
                    if source["id"] in {entry["source_id"] for entry in question["evidence"]}
                ]
                prompt = judge_prompt(question, result["answer"], supporting)
                for attempt in range(2):
                    try:
                        judgment = judge.request(prompt)
                        if judgment.get("status") not in SCORES or not isinstance(judgment.get("reason"), str) or not judgment["reason"].strip():
                            raise BenchmarkError("Judge returned an invalid status or explanation.")
                        break
                    except NeedsInput:
                        raise
                    except BenchmarkError as error:
                        if attempt == 1:
                            raise
                        prompt += f"\nYour last response was invalid: {error}. Return only the requested classifier JSON."
                result.update(status=judgment["status"], reason=judgment["reason"], score=SCORES[judgment["status"]])
            except NeedsInput:
                raise
            except BrowserError as error:
                result.update(status=Status.BROWSER_FAILURE, reason=str(error))
            except (BenchmarkError, TimeoutError) as error:
                result["reason"] = str(error)
            finally:
                if not any(saved is result for saved in report["results"]):
                    report["results"].append(result)
                result["completed_at"] = now()
                result["elapsed_seconds"] = round(time.monotonic() - started, 3)
                save()
            print(f"    {result['status']}: {result['reason']}", flush=True)
        report["status"] = "completed"
        report["completed_at"] = now()
        save()
        summary = report["metrics"]
        accuracy = f"{summary['accuracy']:.1%}" if summary["accuracy"] is not None else "n/a"
        print(f"Accuracy: {accuracy} ({summary['graded']} graded; {summary['counts']['browser_failure']} browser failures). Results: {output}", flush=True)
        return report
    except BaseException as error:
        report["status"] = "interrupted" if isinstance(error, KeyboardInterrupt) else "failed"
        report["error"] = str(error)
        save()
        raise
    finally:
        if runner:
            runner.close()
        browser.close()
