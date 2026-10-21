import hashlib
from collections import deque
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from .ai import BrowserAgent, TextModel
from .browser import Browser
from .common import BenchmarkError, NeedsInput, canonical_url, normalized, now, same_origin, write_json
from .prompts import discovery_prompt, expand_prompt, questions_prompt


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
    if not isinstance(verification, dict) or not all(isinstance(verification.get(key), str) and verification[key].strip() for key in ("probe_question", "probe_answer", "reset_evidence")):
        raise BenchmarkError("Chatbot guide lacks probe and reset verification evidence.")


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
        if key in seen:
            continue
        seen.add(key)
        accepted.append({key: question[key] for key in ("question", "expected_answer", "key_points", "evidence")})
    return accepted


def source_bundles(sources, maximum_characters=30000):
    bundle = []
    size = 0
    for source in sources:
        # Overlapping chunks preserve facts crossing a paragraph/character boundary. No text is dropped.
        text = source["text"]
        for offset in range(0, max(len(text), 1), 18000):
            part = {**source, "text": text[max(0, offset - 1000):offset + 18000]}
            if bundle and size + len(part["text"]) > maximum_characters:
                yield bundle
                bundle, size = [], 0
            bundle.append(part)
            size += len(part["text"])
    if bundle:
        yield bundle


def generate(config, output, force=False):
    output = Path(output)
    if output.exists() and not force:
        raise BenchmarkError(f"Output already exists: {output}. Choose --output or explicitly use --force.")
    identifier = uuid4().hex
    log_directory = output.parent / "logs" / identifier
    artifact = {
        "schema_version": 1,
        "id": identifier,
        "created_at": now(),
        "updated_at": now(),
        "status": "generating",
        "target_url": config.url,
        "config": config.to_dict(),
        "guide": None,
        "sources": [],
        "questions": [],
        "coverage": {"complete": False, "question_limit": config.question_limit, "page_limit": config.page_limit, "pages": [], "pending_urls": [], "notes": []},
        "logs_directory": str(log_directory.resolve()),
        "clarifications": [],
    }

    def save():
        artifact["updated_at"] = now()
        write_json(output, artifact)

    session = f"benchmark-generate-{identifier}"
    browser = Browser(session, config)
    agent = None
    save()
    try:
        print(f"Discovering and verifying chatbot controls on {config.url} ...", flush=True)
        browser.open(config.url)
        agent = BrowserAgent(config, "generation", log_directory / "browser", session)
        artifact["opencode_session_id"] = agent.session_id
        save()
        discovery = agent.request(discovery_prompt(config, session))
        if discovery.get("kind") != "discovery":
            raise BenchmarkError(f"Chatbot discovery failed: {discovery.get('reason') or discovery}")
        validate_guide(discovery.get("guide"), config.url)
        artifact["guide"] = discovery["guide"]
        save()
        print("Chatbot guide verified. Crawling reachable same-origin pages ...", flush=True)
        queue = deque()
        queued = set()
        visited = set()
        fingerprints = set()

        def enqueue(urls, base=config.url):
            if not isinstance(urls, list):
                raise BenchmarkError("Discovered URLs must be an array.")
            for value in urls:
                if not isinstance(value, str):
                    continue
                try:
                    url = canonical_url(value, base)
                except BenchmarkError:
                    continue
                if not same_origin(url, config.url) or url in queued:
                    continue
                queued.add(url)
                queue.append(url)

        def capture(value, requested_url):
            if not isinstance(value, dict) or not isinstance(value.get("text"), str) or not isinstance(value.get("links"), list):
                raise BenchmarkError("Invalid rendered page capture.")
            url = canonical_url(value.get("url", ""), requested_url)
            if not same_origin(url, config.url):
                raise BenchmarkError("Rendered capture is outside the target origin.")
            enqueue(value["links"], url)
            fingerprint = hashlib.sha256(normalized(value["text"]).encode()).hexdigest()
            if fingerprint in fingerprints or not normalized(value["text"]):
                return
            fingerprints.add(fingerprint)
            source = {"id": f"source-{len(artifact['sources']) + 1:04}", "url": url, "title": value.get("title", ""),
                      "text": value["text"], "captured_at": now(), "content_sha256": fingerprint}
            artifact["sources"].append(source)

        enqueue([config.url, *discovery.get("discovered_urls", [])])
        # Capture the established profile's current page before navigation changes it.
        current = browser.capture()
        capture(current, config.url)
        enqueue([current["url"]])
        while queue:
            if config.page_limit is not None and len(artifact["coverage"]["pages"]) >= config.page_limit:
                artifact["coverage"]["notes"].append("Crawl stopped at the configured page limit.")
                break
            url = queue.popleft()
            if url in visited:
                continue
            visited.add(url)
            record = {"requested_url": url, "status": "capturing"}
            artifact["coverage"]["pages"].append(record)
            print(f"  Page {len(visited)}: {url}", flush=True)
            try:
                if Path(urlsplit(url).path).suffix.lower() in {".pdf", ".zip", ".png", ".jpg", ".jpeg", ".gif", ".mp4", ".xlsx", ".docx"}:
                    record.update(status="unsupported", error="Non-HTML resource; rendered text extraction is not supported.")
                    continue
                page = browser.open(url)
                capture(page, url)
                expanded = agent.request(expand_prompt(config, session, page["url"]))
                if expanded.get("kind") != "page":
                    raise BenchmarkError("Page expansion did not return a page result.")
                for extra in expanded.get("additional_captures", []):
                    capture(extra, page["url"])
                enqueue(expanded.get("additional_urls", []), page["url"])
                capture(browser.capture(), url)
                record.update(status="captured", url=page["url"], notes=expanded.get("notes", []))
            except NeedsInput:
                raise
            except BenchmarkError as error:
                record.update(status="failed", error=str(error))
            finally:
                artifact["coverage"]["pending_urls"] = list(queue)
                artifact["clarifications"] = agent.clarifications
                save()
        if not artifact["sources"]:
            raise BenchmarkError("No rendered website content could be captured.")
        generator = TextModel(config, "generation", log_directory / "questions")
        bundles = list(source_bundles(artifact["sources"]))
        covered_bundles = 0
        exhausted_budget = False
        for index, bundle in enumerate(bundles):
            existing = [{"question": question["question"]} for question in artifact["questions"]]
            while True:
                remaining = None if config.question_limit is None else config.question_limit - len(artifact["questions"])
                if remaining is not None and remaining <= 0:
                    exhausted_budget = True
                    break
                print(f"Generating factual questions (source bundle {index + 1}/{len(bundles)}, {len(artifact['questions'])} questions saved) ...", flush=True)
                prompt = questions_prompt(bundle, existing, remaining)
                for attempt in range(3):
                    try:
                        response = generator.request(prompt)
                        accepted = validate_questions(response, artifact["sources"], artifact["questions"])
                        break
                    except NeedsInput:
                        raise
                    except BenchmarkError as error:
                        if attempt == 2:
                            raise
                        prompt += f"\nYour previous response failed validation: {error}. Return a corrected complete JSON batch."
                if remaining is not None:
                    accepted = accepted[:remaining]
                for question in accepted:
                    question["id"] = f"question-{len(artifact['questions']) + 1:04}"
                    artifact["questions"].append(question)
                    existing.append({"question": question["question"]})
                artifact["clarifications"] = agent.clarifications + generator.clarifications
                save()
                if response["done"]:
                    covered_bundles += 1
                    break
                if not accepted:
                    raise BenchmarkError("Question generation made no progress while claiming more coverage remains.")
            if exhausted_budget:
                break
        if not artifact["questions"]:
            raise BenchmarkError("The captured site did not yield any supported factual questions.")
        coverage = artifact["coverage"]
        coverage["source_bundles"] = len(bundles)
        coverage["covered_source_bundles"] = covered_bundles
        coverage["question_limit_reached"] = config.question_limit is not None and len(artifact["questions"]) >= config.question_limit
        coverage["complete"] = (
            not coverage["pending_urls"] and covered_bundles == len(bundles)
            and not coverage["question_limit_reached"]
            and all(page["status"] == "captured" and not page.get("notes") for page in coverage["pages"])
        )
        coverage["notes"].append("Coverage describes discovered rendered same-origin content, not an assurance that unlinked, private or infinite content exists nowhere else.")
        artifact["status"] = "ready"
        artifact["completed_at"] = now()
        save()
        print(f"Saved {len(artifact['questions'])} questions and the verified chatbot guide to {output}", flush=True)
        if not coverage["complete"]:
            print("Coverage is partial; see the JSON coverage manifest for limits, failures and pending URLs.", flush=True)
        return artifact
    except BaseException as error:
        artifact["status"] = "interrupted" if isinstance(error, KeyboardInterrupt) else "failed"
        artifact["error"] = str(error)
        if agent:
            artifact["clarifications"] = agent.clarifications
        save()
        raise
    finally:
        if agent:
            agent.close()
        browser.close()
