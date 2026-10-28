import hashlib
from pathlib import Path
from uuid import uuid4

from .ai import BrowserAgent
from .browser import Browser
from .common import BenchmarkError, SCORES, Status, canonical_url, metrics, normalized, now, read_json, same_origin, write_json
from .generation import validate_guide, validate_questions
from .prompts import evaluation_prompt, runner_prompt


def result_summary(result):
    return {
        "question_id": result["question_id"],
        "question": result["question"],
        "answer": result.get("answer"),
        "grade": result.get("grade", result.get("status")),
        "explanation": result.get("explanation", result.get("reason", "")),
    }


def validate_evaluation(item, target):
    if item.get("status") not in {*SCORES, Status.OUTDATED, Status.UNRESOLVED}:
        raise BenchmarkError("Evaluator returned an invalid grade.")
    if not isinstance(item.get("reason"), str) or not item["reason"].strip():
        raise BenchmarkError("Evaluator must explain its grade.")
    investigation = item.get("investigation", {})
    if not isinstance(investigation, dict) or not isinstance(investigation.get("current_sources", []), list):
        raise BenchmarkError("Evaluation investigation must contain current source captures.")
    for source in investigation.get("current_sources", []):
        if not isinstance(source, dict) or not all(isinstance(source.get(key), str) and source[key].strip() for key in ("url", "text", "quote")):
            raise BenchmarkError("Current evidence requires a URL, captured text and quote.")
        if not same_origin(canonical_url(source["url"]), target) or normalized(source["quote"]) not in normalized(source["text"]):
            raise BenchmarkError("Current evidence must be same-origin with a verbatim quote in the captured text.")
    if item["status"] == Status.OUTDATED:
        if not investigation.get("current_sources") or not isinstance(investigation.get("context_comparison"), str) or not investigation["context_comparison"].strip():
            raise BenchmarkError("Outdated references require current evidence and context verification.")
        replacement = item.get("proposed_expected_answer")
        if replacement is not None:
            if not isinstance(replacement, str) or not replacement.strip():
                raise BenchmarkError("A replacement reference must be a nonempty answer or null.")
            points = item.get("replacement_key_points")
            if not isinstance(points, list) or not points or not all(isinstance(point, str) and point.strip() for point in points):
                raise BenchmarkError("A replacement reference requires supported key points.")
            if item.get("replacement_grade") not in {*SCORES, Status.UNRESOLVED} or not isinstance(item.get("replacement_explanation"), str) or not item["replacement_explanation"].strip():
                raise BenchmarkError("A replacement reference requires a grade and explanation for the captured reply.")


def apply_reference_updates(path, original_hash, updates):
    if not updates:
        return
    if hashlib.sha256(Path(path).read_bytes()).hexdigest() != original_hash:
        raise BenchmarkError("The benchmark changed during this run. Reference updates were saved in the run report but not applied.")
    benchmark = read_json(path)
    questions = {question["id"]: question for question in benchmark["questions"]}
    for update in updates:
        question = questions[update["question_id"]]
        timestamp = now()
        benchmark.setdefault("reference_history", []).append({
            "question_id": question["id"], "changed_at": timestamp,
            "expected_answer": question["expected_answer"], "key_points": question["key_points"],
            "evidence": question["evidence"], "reason": update["reason"],
            "current_evidence": update["current_evidence"],
        })
        question["reference_checked_at"] = timestamp
        question["reference_status"] = "outdated"
        if update["proposed_expected_answer"] is None:
            continue
        evidence = []
        for source in update["current_evidence"]:
            source_id = f"source-update-{uuid4().hex}"
            benchmark["sources"].append({
                "id": source_id, "url": source["url"], "title": "Updated reference evidence",
                "text": source["text"], "captured_at": timestamp,
                "content_sha256": hashlib.sha256(normalized(source["text"]).encode()).hexdigest(),
            })
            evidence.append({"source_id": source_id, "quote": source["quote"]})
        question.update(expected_answer=update["proposed_expected_answer"], key_points=update["replacement_key_points"],
                        evidence=evidence, reference_status="current")
    benchmark["updated_at"] = now()
    write_json(path, benchmark)


def run(config, benchmark_path, output, force=False, url_override=False, resume=False, additional_prompt=None):
    output = Path(output)
    previous = read_json(output) if resume else None
    if resume and (not isinstance(previous, dict) or previous.get("schema_version") != 1 or previous.get("status") not in {"failed", "interrupted", "running"}):
        raise BenchmarkError("Resume requires an unfinished run report.")
    if output.exists() and not force and not resume:
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
    benchmark_hash = hashlib.sha256(Path(benchmark_path).read_bytes()).hexdigest()
    if resume:
        if previous.get("benchmark_id") != benchmark["id"] or previous.get("benchmark_sha256") != benchmark_hash:
            raise BenchmarkError("The benchmark has changed since this run started; start a new run instead of resuming.")
        planned = previous.get("planned_questions")
        if type(planned) is not int or not 1 <= planned <= len(questions):
            raise BenchmarkError("Saved run has an invalid question count.")
        if not isinstance(previous.get("opencode_session_id"), str):
            raise BenchmarkError("Saved run has no OpenCode session to resume.")
        questions = questions[:planned]
    elif config.question_limit is not None:
        questions = questions[:config.question_limit]
    by_id = {question["id"]: question for question in questions}
    identifier = previous["id"] if resume else uuid4().hex
    log_file = output.parent / "logs" / f"run-{identifier}.jsonl"
    browser_session = f"benchmark-run-{identifier}"
    browser = Browser(browser_session, config)
    runner = None
    report = {
        "schema_version": 1, "id": identifier, "started_at": now(), "updated_at": now(),
        "status": "running", "target_url": target, "benchmark_id": benchmark["id"],
        "benchmark_sha256": benchmark_hash,
        "config": config.to_dict(), "planned_questions": len(questions), "results": [],
        "metrics": metrics([]), "clarifications": [],
        "fresh_chat_at_start_verified": False,
        "reference_updates": [], "investigations": [],
    }
    if resume:
        report = previous
        restored = []
        seen = set()
        for saved in report["results"]:
            question_id = saved.get("question_id")
            if question_id not in by_id or question_id in seen:
                raise BenchmarkError("Saved run contains unknown or duplicate question IDs.")
            seen.add(question_id)
            # Unanswered failures are retryable; captured answers and finished grades are retained.
            if saved.get("answer") is None:
                continue
            question = by_id[question_id]
            status = saved.get("grade", saved.get("status"))
            if status not in {item.value for item in Status} or not isinstance(saved["answer"], str):
                raise BenchmarkError("Saved run contains an invalid answer or grade.")
            restored.append({"question_id": question_id, "question": saved["question"], "answer": saved["answer"],
                             "status": status, "reason": saved.get("explanation", saved.get("reason", "")),
                             "score": SCORES.get(status), "expected_answer": question["expected_answer"],
                             "key_points": question["key_points"], "evidence": question["evidence"]})
        report.setdefault("resume_history", []).append({
            "resumed_at": now(), "previous_status": report["status"], "previous_error": report.get("error"),
            "saved_answers": len(restored), "prompt": additional_prompt,
        })
        report["results"] = restored
        report["status"] = "running"
        report.pop("error", None)
        report.pop("completed_at", None)
        report.setdefault("reference_updates", [])
        report.setdefault("investigations", [])

    def save():
        report["updated_at"] = now()
        report["config"] = config.to_dict()
        report["metrics"] = metrics(report["results"])
        if runner:
            report["clarifications"] = runner.clarifications
        write_json(output, {**report, "results": [result_summary(result) for result in report["results"]]})

    def record(question, response):
        kind = response.get("kind")
        if kind not in {"answer", "browser_failure"}:
            raise BenchmarkError("Runner outcomes must be answers or browser failures.")
        value = response.get("answer" if kind == "answer" else "error")
        if not isinstance(value, str) or not value.strip():
            raise BenchmarkError("Runner must provide a verbatim answer or precise failure reason.")
        fresh = response.get("fresh_chat_verified") is True
        reused = response.get("conversation_reused") is True
        if kind == "answer" and (not report["fresh_chat_at_start_verified"] or fresh == reused):
            raise BenchmarkError("Runner must verify fresh run startup and report whether each answer used a fresh or reused conversation.")
        return {
            "question_id": question["id"], "question": question["question"],
            "expected_answer": question["expected_answer"], "key_points": question["key_points"],
            "evidence": question["evidence"], "answer": value if kind == "answer" else None,
            "fresh_chat_verified": fresh, "conversation_reused": reused,
            "status": Status.AGENT_FAILURE if kind == "answer" else Status.BROWSER_FAILURE,
            "reason": "Answer saved; evaluation has not completed." if kind == "answer" else value,
            "score": None, "captured_at": now(),
        }

    save()
    try:
        runner = BrowserAgent(config, "runner", log_file, browser_session,
                              session_id=report["opencode_session_id"] if resume else None)
        if resume:
            runner.clarifications = report.get("clarifications", [])
        browser.directory = runner.workspace.name
        report["opencode_session_id"] = runner.session_id
        save()
        if resume:
            print(f"Resuming with {len(report['results'])} saved answers in the original OpenCode session.", flush=True)
        print(f"Runner is working through and evaluating {len(questions)} questions with {config.models['runner']}", flush=True)
        public_questions = [{"id": question["id"], "question": question["question"]} for question in questions]
        completed = {result["question_id"] for result in report["results"]}
        pending_questions = [question for question in public_questions if question["id"] not in completed]
        resume_context = ""
        if resume:
            resume_context = (
                "\nRESUME of the SAME run, not a new run. Keep all previously captured replies immutable. "
                "Use this OpenCode session's history to recover exact replies from the interrupted turn "
                "before submitting more questions. Do not resend a question whose reply can be recovered. "
                "Return recovered outcomes for pending IDs in the same run JSON format, then continue "
                "the rest. Previously saved IDs must not be returned again: " + str(sorted(completed)) + ". "
                "The previous browser may have been closed. Restore the same household/profile and "
                "conversation if available, rather than resetting solely because this is a resume. "
                "If recovery requires a new browser/chat, record that discontinuity in your explanation, "
                "verify a fresh context and do not pretend missing conversational history is present. "
                "Do not fabricate replies or silently rewrite dependent questions. Return completed "
                "batches regularly so they can be saved before the turn timeout."
            )
        extra = "\nAdditional trusted user instructions for this invocation: " + additional_prompt if additional_prompt else ""
        prompt = runner_prompt(config, browser_session, benchmark["guide"], pending_questions) + resume_context + extra
        while len(completed) < len(questions):
            report["phase"] = "capture"
            save()
            response = runner.request(prompt)
            if response.get("kind") != "run" or type(response.get("done")) is not bool or not isinstance(response.get("results"), list):
                raise BenchmarkError("Runner must return a run result containing outcomes and a done flag.")
            if response.get("fresh_chat_at_start_verified") is True:
                report["fresh_chat_at_start_verified"] = True
            count = len(completed)
            for outcome in response["results"]:
                question_id = outcome.get("question_id") if isinstance(outcome, dict) else None
                if not isinstance(question_id, str) or question_id not in by_id or question_id in completed:
                    raise BenchmarkError("Runner returned an unknown or duplicate question ID.")
                result = record(by_id[question_id], outcome)
                report["results"].append(result)
                completed.add(question_id)
                save()
                print(f"  Captured [{len(completed)}/{len(questions)}] {result['question']}", flush=True)
            if response["done"] or len(completed) == len(questions):
                break
            if len(completed) == count:
                raise BenchmarkError("Runner returned no outcomes while claiming work remains.")
            pending = [question for question in public_questions if question["id"] not in completed]
            prompt = (
                "Continue the remaining question list yourself in this SAME agent and browser session. "
                "Do not repeat completed questions or reset an already established run conversation. "
                "Recover reasonable operational errors before skipping questions. Return the same run "
                "JSON shape with only new outcomes and done=true when finished. Remaining questions: "
                + str(pending) + resume_context + extra
            )
        for question in questions:
            if question["id"] not in completed:
                report["results"].append({
                    "question_id": question["id"], "question": question["question"],
                    "expected_answer": question["expected_answer"], "key_points": question["key_points"],
                    "evidence": question["evidence"], "answer": None, "score": None,
                    "fresh_chat_verified": False, "conversation_reused": False,
                    "status": Status.AGENT_FAILURE, "reason": "Runner finished without returning this question's outcome.",
                })
        save()
        captured = {result["question_id"]: result for result in report["results"] if result["answer"] is not None}
        cases = []
        for question_id, result in captured.items():
            question = by_id[question_id]
            cases.append({
                "question_id": question_id, "question": question["question"], "answer": result["answer"],
                "expected_answer": question["expected_answer"], "key_points": question["key_points"],
                "saved_evidence": [
                    {**entry, "url": next(source["url"] for source in sources if source["id"] == entry["source_id"])}
                    for entry in question["evidence"]
                ],
                "benchmark_created_at": benchmark.get("created_at"),
                "reference_status": question.get("reference_status", "current"),
            })
        evaluated = {result["question_id"] for result in report["results"]
                     if result["status"] in {*SCORES, Status.OUTDATED, Status.UNRESOLVED}}
        while len(evaluated) < len(captured):
            pending_cases = [case for case in cases if case["question_id"] not in evaluated]
            print(f"Evaluating {len(pending_cases)} captured answers and investigating discrepancies ...", flush=True)
            report["phase"] = "evaluation"
            save()
            evaluation_resume = (
                "\nThis is a resumed run. Keep previously saved chatbot replies and completed grades unchanged. "
                "Evaluate only the pending cases supplied here. Use session history to recover earlier "
                "investigation observations; restore the same website profile/context if the browser "
                "was closed. Return evaluation JSON, not run/capture JSON."
                if resume else ""
            )
            response = runner.request(evaluation_prompt(config, browser_session, pending_cases) + evaluation_resume + extra)
            if response.get("kind") != "evaluation" or type(response.get("done")) is not bool or not isinstance(response.get("results"), list):
                raise BenchmarkError("Evaluator must return evaluation outcomes and a done flag.")
            if not response["results"]:
                raise BenchmarkError("Evaluator made no progress.")
            for judgment in response["results"]:
                question_id = judgment.get("question_id") if isinstance(judgment, dict) else None
                if not isinstance(question_id, str) or question_id not in captured or question_id in evaluated:
                    raise BenchmarkError("Evaluator returned an unknown or duplicate question ID.")
                validate_evaluation(judgment, target)
                result = captured[question_id]
                result.update(status=judgment["status"], reason=judgment["reason"], score=SCORES.get(judgment["status"]), completed_at=now())
                if judgment.get("investigation"):
                    report["investigations"].append({"question_id": question_id, "checked_at": now(), **judgment["investigation"]})
                if judgment["status"] == Status.OUTDATED:
                    report["reference_updates"].append({
                        "question_id": question_id, "original_expected_answer": by_id[question_id]["expected_answer"],
                        "original_evidence": by_id[question_id]["evidence"],
                        "original_key_points": by_id[question_id]["key_points"],
                        "reason": judgment["reason"],
                        "proposed_expected_answer": judgment.get("proposed_expected_answer"),
                        "replacement_key_points": judgment.get("replacement_key_points"),
                        "current_evidence": judgment["investigation"]["current_sources"],
                        "context_comparison": judgment["investigation"]["context_comparison"],
                    })
                    if judgment.get("proposed_expected_answer") is not None:
                        result.update(status=judgment["replacement_grade"], reason=judgment["replacement_explanation"],
                                      score=SCORES.get(judgment["replacement_grade"]))
                evaluated.add(question_id)
                save()
                print(f"  {question_id}: {result['status']}: {result['reason']}", flush=True)
            if response["done"] and len(evaluated) != len(captured):
                raise BenchmarkError("Evaluator finished without grading every captured answer.")
        apply_reference_updates(benchmark_path, report["benchmark_sha256"], report["reference_updates"])
        for update in report["reference_updates"]:
            update["applied"] = update["proposed_expected_answer"] is not None
        report["status"] = "completed"
        report["phase"] = "completed"
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
        browser.close()
        if runner:
            runner.close()
