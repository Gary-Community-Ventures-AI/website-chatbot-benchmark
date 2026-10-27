import hashlib
from pathlib import Path
from uuid import uuid4

from .ai import BrowserAgent
from .browser import Browser
from .common import BenchmarkError, canonical_url, normalized, now, same_origin, write_json
from .prompts import discovery_prompt


GUIDE_STEPS = ("setup_steps", "reset_steps", "send_steps", "wait_steps", "extract_steps", "recovery_steps")


def validate_guide(guide, target):
    if not isinstance(guide, dict) or guide.get("verified") is not True:
        raise BenchmarkError("Chatbot operating guide was not successfully verified in the browser.")
    if not same_origin(canonical_url(guide.get("entry_url", "")), target):
        raise BenchmarkError("The chatbot guide points outside the target origin.")
    for name in GUIDE_STEPS:
        if not isinstance(guide.get(name), list) or not guide[name] or not all(isinstance(step, str) and step.strip() for step in guide[name]):
            raise BenchmarkError(f"Chatbot guide needs nonempty {name}.")
    verification = guide.get("verification", {})
    if not isinstance(verification, dict) or not all(isinstance(verification.get(key), str) and verification[key].strip() for key in ("probe_question", "probe_answer")):
        raise BenchmarkError("Chatbot guide lacks probe verification evidence.")


def validate_questions(response, sources, existing):
    if response.get("kind") != "questions" or type(response.get("done")) is not bool or not isinstance(response.get("questions"), list):
        raise BenchmarkError("Question generation returned an invalid batch shape.")
    by_id = {source["id"]: source for source in sources}
    seen = {normalized(item["question"]).casefold() for item in existing}
    accepted = []
    for question in response["questions"]:
        if not isinstance(question, dict):
            raise BenchmarkError("Each generated question must be an object.")
        for key in ("question", "expected_answer"):
            if not isinstance(question.get(key), str) or not question[key].strip():
                raise BenchmarkError(f"Generated question lacks {key}.")
        points = question.get("key_points")
        if not isinstance(points, list) or not points or not all(isinstance(point, str) and point.strip() for point in points):
            raise BenchmarkError("Generated question needs factual key_points.")
        evidence = question.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            raise BenchmarkError("Generated question needs source evidence.")
        for entry in evidence:
            if not isinstance(entry, dict) or not isinstance(entry.get("source_id"), str) or not isinstance(entry.get("quote"), str):
                raise BenchmarkError("Question evidence needs source_id and quote strings.")
            source = by_id.get(entry["source_id"])
            quote = normalized(entry["quote"])
            if not source or not quote or quote not in normalized(source["text"]):
                raise BenchmarkError(f"Unverifiable evidence quote for {entry['source_id']}: {quote[:150]}")
        key = normalized(question["question"]).casefold()
        if key not in seen:
            seen.add(key)
            accepted.append({key: question[key] for key in ("question", "expected_answer", "key_points", "evidence")})
    return accepted


def generate(config, output, force=False):
    output = Path(output)
    if output.exists() and not force:
        raise BenchmarkError(f"Output already exists: {output}. Choose --output or explicitly use --force.")
    identifier = uuid4().hex
    log_file = output.parent / "logs" / f"generate-{identifier}.jsonl"
    artifact = {
        "schema_version": 1, "id": identifier, "created_at": now(), "updated_at": now(),
        "status": "generating", "target_url": config.url, "config": config.to_dict(),
        "guide": None, "sources": [], "questions": [],
        "coverage": {"complete": False, "question_limit": config.question_limit, "page_limit": config.page_limit,
                     "pages": [], "pending_urls": [], "notes": []},
        "clarifications": [],
    }

    def save():
        artifact["updated_at"] = now()
        artifact["config"] = config.to_dict()
        if agent:
            artifact["clarifications"] = agent.clarifications
        write_json(output, artifact)

    session = f"benchmark-generate-{identifier}"
    browser = Browser(session, config)
    agent = None
    save()
    try:
        print(f"Exploring {config.url} and generating questions and the chatbot guide ...", flush=True)
        agent = BrowserAgent(config, "generation", log_file, session)
        browser.directory = agent.workspace.name
        artifact["opencode_session_id"] = agent.session_id
        save()
        prompt = discovery_prompt(config, session)
        while True:
            response = agent.request(prompt)
            if response.get("kind") != "benchmark":
                raise BenchmarkError(f"Benchmark generation failed: {response.get('reason') or response}")
            if artifact["guide"] is None:
                validate_guide(response.get("guide"), config.url)
                artifact["guide"] = response["guide"]
            sources = response.get("sources")
            if not isinstance(sources, list):
                raise BenchmarkError("Generation must return a list of captured sources.")
            by_id = {source["id"]: source for source in artifact["sources"]}
            for source in sources:
                if not isinstance(source, dict) or not all(isinstance(source.get(key), str) and source[key].strip() for key in ("id", "url", "text")):
                    raise BenchmarkError("Each source needs a nonempty id, URL and captured text.")
                url = canonical_url(source["url"])
                if not same_origin(url, config.url):
                    raise BenchmarkError("Captured source is outside the target origin.")
                if source["id"] in by_id:
                    previous = by_id[source["id"]]
                    if previous["url"] != url or previous["text"] != source["text"]:
                        raise BenchmarkError("Source IDs must remain stable across generation batches.")
                    continue
                captured = {"id": source["id"], "url": url, "title": source.get("title", ""), "text": source["text"],
                            "captured_at": now(), "content_sha256": hashlib.sha256(normalized(source["text"]).encode()).hexdigest()}
                artifact["sources"].append(captured)
                by_id[captured["id"]] = captured
            accepted = validate_questions({**response, "kind": "questions"}, artifact["sources"], artifact["questions"])
            if config.question_limit is not None:
                accepted = accepted[:max(0, config.question_limit - len(artifact["questions"]))]
            for question in accepted:
                question["id"] = f"question-{len(artifact['questions']) + 1:04}"
                artifact["questions"].append(question)
            coverage = response.get("coverage")
            if not isinstance(coverage, dict) or type(coverage.get("complete")) is not bool or not all(isinstance(coverage.get(key), list) for key in ("pages", "pending_urls", "notes")):
                raise BenchmarkError("Generation must report coverage, including pages and limitations.")
            artifact["coverage"].update({key: coverage[key] for key in ("complete", "pages", "pending_urls", "notes")})
            limit_reached = config.question_limit is not None and len(artifact["questions"]) >= config.question_limit
            artifact["coverage"]["question_limit_reached"] = limit_reached
            if limit_reached or coverage["pending_urls"] or not response["done"]:
                artifact["coverage"]["complete"] = False
            save()
            if response["done"] or limit_reached:
                break
            if not accepted:
                raise BenchmarkError("Generation made no progress while claiming more questions remain.")
            prompt = (
                "Continue in this SAME session using the website information already gathered. Do not "
                "start another crawl, recreate the household or repeat chatbot discovery. Return the next "
                "benchmark JSON batch with only new questions and any needed source captures from your "
                "existing observations. Reuse source IDs; guide may be omitted. Include updated coverage "
                "and done=true when finished. Questions already saved: "
                + str([question["question"] for question in artifact["questions"]])
            )
        if not artifact["questions"]:
            raise BenchmarkError("The discovered content did not yield supported factual questions.")
        artifact["status"] = "ready"
        artifact["completed_at"] = now()
        save()
        print(f"Saved {len(artifact['questions'])} questions and the verified chatbot guide to {output}", flush=True)
        return artifact
    except BaseException as error:
        artifact["status"] = "interrupted" if isinstance(error, KeyboardInterrupt) else "failed"
        artifact["error"] = str(error)
        save()
        raise
    finally:
        browser.close()
        if agent:
            agent.close()
