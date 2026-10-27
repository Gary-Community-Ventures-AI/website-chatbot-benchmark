import React, { useCallback, useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  Activity,
  ArrowUpRight,
  Check,
  ChevronRight,
  FileJson,
  RefreshCw,
  Layers3,
  Search,
  Upload,
  X,
} from "lucide-react";
import {
  dateLabel,
  grades,
  label,
  parseRun,
  safeUrl,
  summary,
  type Grade,
  type Result,
  type Run,
  type Source,
} from "./results";
import "./style.css";

const staticReport = import.meta.env.MODE === "pages";

function Badge({ grade }: { grade: string }) {
  return (
    <span
      className="badge"
      style={
        {
          "--grade": grades[grade as Grade]?.color ?? "#8a929e",
        } as React.CSSProperties
      }
    >
      <span />
      {label(grade)}
    </span>
  );
}

function Sources({ sources }: { sources: Source[] }) {
  return (
    <div className="sources">
      {sources.map((source, index) => (
        <div className="source" key={index}>
          {source.url && (
            <a href={safeUrl(source.url)} target="_blank" rel="noreferrer">
              {source.url}
              <ArrowUpRight size={13} />
            </a>
          )}
          <blockquote>
            {source.quote || source.text || "No captured excerpt."}
          </blockquote>
        </div>
      ))}
    </div>
  );
}

function ChatMessage({
  role,
  text,
}: {
  role: "user" | "assistant";
  text: string | null;
}) {
  return (
    <div className={`chat-message ${role}`}>
      <span className="chat-speaker">
        {role === "user" ? "You" : "Chatbot"}
      </span>
      <div className={`chat-bubble${text === null ? " missing-answer" : ""}`}>
        {text ?? "No answer captured."}
      </div>
    </div>
  );
}

function Question({
  result,
  run,
  index,
}: {
  result: Result;
  run: Run;
  index: number;
}) {
  const investigations = run.investigations.filter(
    (item) => item.question_id === result.question_id,
  );
  const updates = run.updates.filter(
    (item) => item.question_id === result.question_id,
  );
  return (
    <details className="question">
      <summary>
        <span className="question-number">
          {String(index + 1).padStart(2, "0")}
        </span>
        <span className="question-heading">
          <span>{result.question}</span>
          <small>{result.question_id}</small>
        </span>
        <Badge grade={result.grade} />
        <ChevronRight className="disclosure" size={16} />
      </summary>
      <div className="question-body">
        <div className="chat-thread" aria-label="Question and chatbot response">
          <ChatMessage role="user" text={result.question} />
          <ChatMessage role="assistant" text={result.answer} />
        </div>
        <section className="chat-evaluation">
          <h3>Evaluation</h3>
          <div className="prose">
            {result.explanation || "No explanation recorded yet."}
          </div>
        </section>
        {updates.map((update, i) => (
          <section className="reference-update" key={i}>
            <h3>
              {update.applied
                ? "Reference updated"
                : update.proposed_expected_answer
                  ? "Reference update proposed"
                  : "Reference remains outdated"}
            </h3>
            <div className="response-columns">
              <div>
                <small>Original reference</small>
                <p className="prose">{update.original_expected_answer}</p>
              </div>
              <div>
                <small>Proposed replacement</small>
                <p className="prose">
                  {update.proposed_expected_answer ||
                    "No verified replacement is available."}
                </p>
              </div>
            </div>
            <Sources sources={update.current_evidence ?? []} />
          </section>
        ))}
        {investigations.map((investigation, i) => (
          <section className="investigation" key={i}>
            <h3>Website investigation</h3>
            <p className="prose">{investigation.context_comparison}</p>
            <Sources sources={investigation.current_sources ?? []} />
          </section>
        ))}
      </div>
    </details>
  );
}

function App() {
  const [runs, setRuns] = useState<Run[]>([]);
  const [projects, setProjects] = useState<string[]>([]);
  const [selected, setSelected] = useState("");
  const [filter, setFilter] = useState("all");
  const [query, setQuery] = useState("");
  const [error, setError] = useState("");
  const [dragging, setDragging] = useState(false);
  const [loading, setLoading] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const dragDepth = useRef(0);
  const imported = useRef(new Map<string, Run>());
  const request = useRef<AbortController | null>(null);
  const run = runs.find((item) => item.key === selected);
  const groupedRuns = new Map<string, Run[]>();
  projects.forEach((project) => groupedRuns.set(project, []));
  for (const item of runs) {
    const group = groupedRuns.get(item.project) ?? [];
    group.push(item);
    groupedRuns.set(item.project, group);
  }
  const stats = summary(run?.results ?? []);
  const percentage = (value: number | null) =>
    value === null ? "—" : `${Math.round(value * 100)}%`;

  const refreshRuns = useCallback(async () => {
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setLoading(true);
    try {
      const response = await fetch(
        staticReport
          ? `${import.meta.env.BASE_URL}data/runs.json`
          : "/api/runs",
        {
          signal: controller.signal,
          cache: "no-store",
        },
      );
      if (!response.ok)
        throw new Error(
          staticReport
            ? "Could not load the published report data. Try refreshing the page."
            : "Could not load local results. Start the server with benchmark view.",
        );
      const data = await response.json();
      const loaded: Run[] = data.runs.map(
        (item: {
          project: string;
          filename: string;
          modified: number;
          report: unknown;
        }) => {
          const parsed = parseRun(item.report, {
            name: item.filename,
            lastModified: item.modified,
          });
          return {
            ...parsed,
            key: `${item.project}:${parsed.id}`,
            project: item.project,
          };
        },
      );
      const all = [...loaded, ...imported.current.values()];
      setProjects(
        Array.isArray(data.projects)
          ? data.projects
          : [...new Set(loaded.map((item) => item.project))],
      );
      all.sort((a, b) => b.date.localeCompare(a.date));
      setRuns(all);
      setSelected((previous) =>
        all.some((item) => item.key === previous)
          ? previous
          : (all[0]?.key ?? ""),
      );
      setError(data.errors.join("\n"));
    } catch (cause) {
      if (!controller.signal.aborted)
        setError(
          cause instanceof Error ? cause.message : "Could not load local runs.",
        );
    } finally {
      if (!controller.signal.aborted) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refreshRuns();
    const interval = staticReport
      ? null
      : window.setInterval(() => void refreshRuns(), 10000);
    return () => {
      if (interval !== null) window.clearInterval(interval);
      request.current?.abort();
    };
  }, [refreshRuns]);

  async function openFiles(files: File[]) {
    setLoading(true);
    setError("");
    const loaded: Run[] = [];
    const errors: string[] = [];
    for (const file of files) {
      try {
        loaded.push(parseRun(JSON.parse(await file.text()), file));
      } catch (cause) {
        errors.push(
          `${file.name}: ${cause instanceof Error ? cause.message : "Could not read this file."}`,
        );
      }
    }
    if (loaded.length) {
      loaded.forEach((item) => imported.current.set(item.key, item));
      setRuns((previous) => {
        const all = new Map(previous.map((item) => [item.key, item]));
        loaded.forEach((item) => all.set(item.key, item));
        return [...all.values()].sort((a, b) => b.date.localeCompare(a.date));
      });
      setSelected(loaded[0].key);
      setFilter("all");
      setQuery("");
    }
    setError(errors.join("\n"));
    setLoading(false);
  }

  const visible =
    run?.results
      .map((result, index) => ({ result, index }))
      .filter(
        ({ result }) =>
          (filter === "all" || result.grade === filter) &&
          [
            result.question,
            result.answer,
            result.explanation,
            result.question_id,
          ].some((value) => value?.toLowerCase().includes(query.toLowerCase())),
      ) ?? [];

  return (
    <div
      className="app"
      onDragEnter={(event) => {
        event.preventDefault();
        dragDepth.current++;
        setDragging(true);
      }}
      onDragOver={(event) => event.preventDefault()}
      onDragLeave={(event) => {
        event.preventDefault();
        dragDepth.current--;
        if (!dragDepth.current) setDragging(false);
      }}
      onDrop={(event) => {
        event.preventDefault();
        dragDepth.current = 0;
        setDragging(false);
        void openFiles(Array.from(event.dataTransfer.files));
      }}
    >
      <input
        ref={input}
        type="file"
        accept=".json,application/json"
        multiple
        className="file-input"
        aria-label="Open result JSON files"
        onChange={(event) => {
          void openFiles(Array.from(event.target.files ?? []));
          event.target.value = "";
        }}
      />
      <aside className="sidebar">
        <a className="brand" href="#">
          <span className="brand-icon">
            <Layers3 size={19} />
          </span>
          benchmark<span className="brand-dot">.</span>
        </a>
        <div className="sidebar-label">
          WORKSPACE <span>{staticReport ? "PUBLISHED" : "LOCAL"}</span>
        </div>
        <button
          className="open-button"
          onClick={() => void refreshRuns()}
          disabled={loading}
        >
          <RefreshCw size={16} />
          {loading ? "Refreshing…" : "Refresh runs"}
        </button>
        <div className="sidebar-label runs-label">
          RUN HISTORY <span>{runs.length}</span>
        </div>
        <nav aria-label="Loaded runs">
          {[...groupedRuns]
            .sort(([a], [b]) => a.localeCompare(b))
            .map(([project, projectRuns]) => (
              <details className="project-group" key={project} open>
                <summary className="project-heading">
                  <ChevronRight size={13} />
                  <span>{project}</span>
                  <small>{projectRuns.length}</small>
                </summary>
                {projectRuns.map((item) => {
                  const info = summary(item.results);
                  return (
                    <button
                      key={item.key}
                      className={`run-link ${item.key === selected ? "selected" : ""}`}
                      onClick={() => {
                        setSelected(item.key);
                        setFilter("all");
                        setQuery("");
                      }}
                    >
                      <span className="run-link-top">
                        <FileJson size={15} />
                        <span>{dateLabel(item.date)}</span>
                        <span className="run-score">
                          {percentage(info.accuracy)}
                        </span>
                      </span>
                      <span className="run-link-bottom">
                        {item.results.length} questions · {item.status}
                      </span>
                    </button>
                  );
                })}
              </details>
            ))}
        </nav>
        {!runs.length && (
          <p className="sidebar-empty">Project runs will appear here.</p>
        )}
        <div className="sidebar-footer">
          <span className="local-dot" />
          {staticReport
            ? "Updated when committed results are published"
            : "Local server · refreshes every 10s"}
        </div>
      </aside>
      <main>
        <header className="topbar">
          <div>
            <span className="breadcrumb">Workspace</span>
            <ChevronRight size={13} />
            <span>Results</span>
          </div>
          <span className="topbar-note">
            <Activity size={14} />
            Benchmark explorer
          </span>
        </header>
        <div className="content">
          {error && (
            <div className="error" role="alert">
              <span>{error}</span>
              <button aria-label="Dismiss error" onClick={() => setError("")}>
                <X size={16} />
              </button>
            </div>
          )}
          {!run ? (
            <div className="empty-state">
              <div className="empty-art">
                <div className="empty-line" />
                <div className="empty-chart">
                  <span />
                  <span />
                  <span />
                  <span />
                  <span />
                </div>
                <FileJson size={23} />
              </div>
              <span className="eyebrow">A CLEARER VIEW OF YOUR BENCHMARKS</span>
              <h1>Every answer. In perspective.</h1>
              <p>
                Project runs appear automatically. Explore answers, grades,
                <br />
                and the evidence behind each evaluation.
              </p>
              <button
                className="primary-button"
                onClick={() => void refreshRuns()}
                disabled={loading}
              >
                <RefreshCw size={17} />
                Refresh runs
              </button>
              <span className="drop-hint">
                Run a benchmark from this workspace to see its results here
              </span>
              <div className="empty-features">
                <span>
                  <Check size={14} />
                  Search every answer
                </span>
                <span>
                  <Check size={14} />
                  Filter by grade
                </span>
                <span>
                  <Check size={14} />
                  Review changed references
                </span>
              </div>
            </div>
          ) : (
            <>
              <div className="page-heading">
                <div>
                  <span className="eyebrow">RUN OVERVIEW</span>
                  <h1>
                    Results{" "}
                    <span
                      className={`run-status ${run.status === "completed" ? "completed" : ""}`}
                    >
                      {run.status}
                    </span>
                  </h1>
                  <div className="run-meta">
                    <span>{dateLabel(run.date)}</span>
                    <span>·</span>
                    <span>{run.model}</span>
                  </div>
                </div>
                <button
                  className="secondary-button"
                  onClick={() => void refreshRuns()}
                >
                  <RefreshCw size={15} />
                  Refresh runs
                </button>
              </div>
              {run.target && (
                <a
                  className="target"
                  href={safeUrl(run.target)}
                  target="_blank"
                  rel="noreferrer"
                >
                  {run.target}
                  <ArrowUpRight size={12} />
                </a>
              )}
              <div className="stats">
                <div>
                  <span className="stat-label">Accuracy</span>
                  <strong>{percentage(stats.accuracy)}</strong>
                  <small>
                    {stats.counts.correct} correct of {stats.graded} graded
                  </small>
                </div>
                <div>
                  <span className="stat-label">Weighted score</span>
                  <strong>{percentage(stats.weighted)}</strong>
                  <small>Partial answers count as ½</small>
                </div>
                <div>
                  <span className="stat-label">Answers captured</span>
                  <strong>
                    {stats.answered}
                    <em> / {run.planned}</em>
                  </strong>
                  <small>{stats.failures} operational failures</small>
                </div>
                <div>
                  <span className="stat-label">Not in accuracy</span>
                  <strong>{stats.excluded}</strong>
                  <small>Outdated, unresolved, or failed</small>
                </div>
              </div>
              <section className="distribution" aria-label="Grade distribution">
                <div className="distribution-bar">
                  {Object.entries(stats.counts)
                    .filter(([, count]) => count > 0)
                    .map(([grade, count]) => (
                      <span
                        key={grade}
                        style={{
                          flex: count,
                          background:
                            grades[grade as Grade]?.color ?? "#8a929e",
                        }}
                        title={`${label(grade)}: ${count}`}
                      />
                    ))}
                </div>
                <div className="legend">
                  {Object.entries(stats.counts)
                    .filter(([, count]) => count > 0)
                    .map(([grade, count]) => (
                      <button
                        key={grade}
                        onClick={() =>
                          setFilter(filter === grade ? "all" : grade)
                        }
                      >
                        <span
                          style={{
                            background:
                              grades[grade as Grade]?.color ?? "#8a929e",
                          }}
                        />
                        {label(grade)}
                        <b>{count}</b>
                      </button>
                    ))}
                </div>
              </section>
              <div className="section-heading">
                <h2>
                  Questions & answers <span>{run.results.length}</span>
                </h2>
                <span>Expand a question to see the full response</span>
              </div>
              <div className="toolbar">
                <label className="search">
                  <Search size={16} />
                  <input
                    placeholder="Search questions, answers, or explanations…"
                    value={query}
                    onChange={(event) => setQuery(event.target.value)}
                    aria-label="Search results"
                  />
                  {query && (
                    <button
                      aria-label="Clear search"
                      onClick={() => setQuery("")}
                    >
                      <X size={14} />
                    </button>
                  )}
                </label>
                <select
                  aria-label="Filter by grade"
                  value={filter}
                  onChange={(event) => setFilter(event.target.value)}
                >
                  <option value="all">All grades</option>
                  {Object.entries(stats.counts)
                    .filter(([, count]) => count > 0)
                    .map(([grade, count]) => (
                      <option key={grade} value={grade}>
                        {label(grade)} ({count})
                      </option>
                    ))}
                </select>
              </div>
              <div className="question-list">
                {visible.map(({ result, index }) => (
                  <Question
                    key={`${run.key}-${result.question_id}-${index}`}
                    result={result}
                    run={run}
                    index={index}
                  />
                ))}
                {!visible.length && (
                  <div className="no-results">
                    <Search size={22} />
                    <p>
                      {run.results.length
                        ? "No questions match your filters."
                        : "No outcomes have been saved in this run yet."}
                    </p>
                    {run.results.length > 0 && (
                      <button
                        onClick={() => {
                          setFilter("all");
                          setQuery("");
                        }}
                      >
                        Clear filters
                      </button>
                    )}
                  </div>
                )}
              </div>
              <footer className="results-footer">
                <span>
                  {visible.length} of {run.results.length} questions
                </span>
                <span>{run.filename}</span>
              </footer>
            </>
          )}
        </div>
      </main>
      {dragging && (
        <div className="drop-overlay">
          <Upload size={36} />
          <h2>Drop to open results</h2>
          <p>One run or a handful. JSON files only.</p>
        </div>
      )}
    </div>
  );
}

createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
