export const grades = {
  correct: { label: "Correct", color: "#25836e" },
  partially_correct: { label: "Partial", color: "#b38b27" },
  incorrect: { label: "Incorrect", color: "#c85a51" },
  outdated: { label: "Outdated", color: "#6974bd" },
  unresolved: { label: "Unresolved", color: "#9b77ac" },
  browser_failure: { label: "Browser failure", color: "#8a929e" },
  agent_failure: { label: "Agent failure", color: "#8a929e" },
  judge_failure: { label: "Evaluation failure", color: "#8a929e" },
} as const;

export type Grade = keyof typeof grades;
export type Result = {
  question_id: string;
  question: string;
  answer: string | null;
  grade: string;
  explanation: string;
};

export type Source = { url?: string; text?: string; quote?: string };
export type Investigation = {
  question_id: string;
  context_comparison?: string;
  current_sources?: Source[];
};
export type ReferenceUpdate = {
  applied?: boolean;
  question_id: string;
  original_expected_answer?: string;
  proposed_expected_answer?: string;
  current_evidence?: Source[];
};
export type Run = {
  key: string;
  filename: string;
  project: string;
  id: string;
  date: string;
  status: string;
  target: string;
  model: string;
  planned: number;
  results: Result[];
  investigations: Investigation[];
  updates: ReferenceUpdate[];
};

function object(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function text(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function sourceList(value: unknown): Source[] {
  return Array.isArray(value)
    ? value.filter(object).map((source) => ({
        url: text(source.url),
        text: text(source.text),
        quote: text(source.quote),
      }))
    : [];
}

export function parseRun(
  value: unknown,
  file: Pick<File, "name" | "lastModified">,
): Run {
  if (!object(value) || !Array.isArray(value.results)) {
    throw new Error("Choose a run result JSON, not a benchmark or log file.");
  }
  const results = value.results.map((entry, index): Result => {
    if (!object(entry) || typeof entry.question !== "string") {
      throw new Error(`Question ${index + 1} is missing its question text.`);
    }
    if (
      entry.answer !== null &&
      entry.answer !== undefined &&
      typeof entry.answer !== "string"
    ) {
      throw new Error(`Question ${index + 1} has an invalid answer.`);
    }
    return {
      question_id: text(
        entry.question_id,
        `question-${String(index + 1).padStart(4, "0")}`,
      ),
      question: entry.question,
      answer: typeof entry.answer === "string" ? entry.answer : null,
      grade: text(entry.grade, text(entry.status, "unresolved")),
      explanation: text(entry.explanation, text(entry.reason)),
    };
  });
  const config = object(value.config) ? value.config : {};
  const models = object(config.models) ? config.models : {};
  const id = text(value.id, file.name);
  return {
    key: id,
    filename: file.name,
    project: "Imported file",
    id,
    date: text(
      value.started_at,
      text(value.created_at, new Date(file.lastModified).toISOString()),
    ),
    status: text(value.status, "unknown"),
    target: text(value.target_url),
    model: text(models.runner, "Not recorded"),
    planned:
      typeof value.planned_questions === "number"
        ? value.planned_questions
        : results.length,
    results,
    investigations: Array.isArray(value.investigations)
      ? value.investigations.filter(object).map((item) => ({
          question_id: text(item.question_id),
          context_comparison: text(item.context_comparison),
          current_sources: sourceList(item.current_sources),
        }))
      : [],
    updates: Array.isArray(value.reference_updates)
      ? value.reference_updates.filter(object).map((item) => ({
          question_id: text(item.question_id),
          original_expected_answer: text(item.original_expected_answer),
          applied: item.applied === true,
          proposed_expected_answer: text(item.proposed_expected_answer),
          current_evidence: sourceList(item.current_evidence),
        }))
      : [],
  };
}

export function label(grade: string): string {
  return grades[grade as Grade]?.label ?? grade.replaceAll("_", " ");
}

export function summary(results: Result[]) {
  const counts = Object.fromEntries(
    Object.keys(grades).map((grade) => [grade, 0]),
  ) as Record<string, number>;
  for (const result of results)
    counts[result.grade] = (counts[result.grade] ?? 0) + 1;
  const graded = counts.correct + counts.partially_correct + counts.incorrect;
  const failures =
    counts.browser_failure + counts.agent_failure + counts.judge_failure;
  return {
    counts,
    graded,
    failures,
    excluded: results.length - graded,
    accuracy: graded ? counts.correct / graded : null,
    weighted: graded
      ? (counts.correct + counts.partially_correct * 0.5) / graded
      : null,
    answered: results.filter((result) => result.answer !== null).length,
  };
}

export function dateLabel(date: string): string {
  const parsed = new Date(date);
  return Number.isNaN(parsed.getTime())
    ? "Unknown date"
    : parsed.toLocaleString(undefined, {
        month: "short",
        day: "numeric",
        hour: "numeric",
        minute: "2-digit",
      });
}

export function safeUrl(value: string): string | undefined {
  try {
    const url = new URL(value);
    return ["http:", "https:"].includes(url.protocol) ? url.href : undefined;
  } catch {
    return undefined;
  }
}
