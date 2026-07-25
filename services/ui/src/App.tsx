import {
  Bug,
  Database,
  FileUp,
  MessageSquare,
  Play,
  RotateCw,
  Search,
} from "lucide-react";
import { FormEvent, useEffect, useMemo, useState } from "react";

const API_BASE = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000";

type Job = {
  id: string;
  status: string;
  progress: {
    pages_imported?: number;
    chunks_indexed?: number;
    pages_seen?: number;
  };
  error_message?: string | null;
};

type Evidence = {
  evidence_id: string;
  title: string;
  section_path: string[];
  content: string;
  source_url: string;
};

type RetrievalEvent = {
  stage: string;
  payload: unknown;
};

type SsePayload = {
  query_run_id: string;
  data: {
    text?: string;
    evidence?: Evidence[];
  };
};

export function App() {
  const [ready, setReady] = useState("checking");
  const [limit, setLimit] = useState(1000);
  const [job, setJob] = useState<Job | null>(null);
  const [question, setQuestion] = useState("Что такое Россия?");
  const [mode, setMode] = useState<"normal" | "extended">("normal");
  const [answer, setAnswer] = useState("");
  const [evidence, setEvidence] = useState<Evidence[]>([]);
  const [queryRunId, setQueryRunId] = useState("");
  const [events, setEvents] = useState<RetrievalEvent[]>([]);
  const [uploadStatus, setUploadStatus] = useState("");

  useEffect(() => {
    fetch(`${API_BASE}/ready`)
      .then((response) => response.json())
      .then((data) => setReady(data.status))
      .catch(() => setReady("offline"));
  }, []);

  const imported = useMemo(() => job?.progress?.pages_imported ?? 0, [job]);
  const chunks = useMemo(() => job?.progress?.chunks_indexed ?? 0, [job]);

  async function startImport() {
    const response = await fetch(`${API_BASE}/api/v1/wikipedia/imports`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ limit }),
    });
    const created = await response.json();
    pollJob(created.job_id);
  }

  async function pollJob(jobId: string) {
    const response = await fetch(`${API_BASE}/api/v1/ingestion-jobs/${jobId}`);
    const nextJob = await response.json();
    setJob(nextJob);
    if (!["completed", "failed", "cancelled"].includes(nextJob.status)) {
      window.setTimeout(() => pollJob(jobId), 1500);
    }
  }

  async function submitChat(event: FormEvent) {
    event.preventDefault();
    setAnswer("");
    setEvidence([]);
    setEvents([]);
    const response = await fetch(`${API_BASE}/api/v1/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: question, mode, stream: true }),
    });
    const reader = response.body?.getReader();
    if (!reader) return;
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const parts = buffer.split("\n\n");
      buffer = parts.pop() ?? "";
      for (const part of parts) {
        const parsed = parseSse(part);
        if (parsed?.event === "message.delta") {
          setAnswer(parsed.data.data.text ?? "");
          setEvidence(parsed.data.data.evidence ?? []);
          setQueryRunId(parsed.data.query_run_id);
        }
        if (parsed?.event === "run.completed") {
          setQueryRunId(parsed.data.query_run_id);
        }
      }
    }
  }

  async function loadDebugger() {
    if (!queryRunId) return;
    const response = await fetch(
      `${API_BASE}/api/v1/query-runs/${queryRunId}/retrieval`,
    );
    const data = await response.json();
    setEvents(data.events);
  }

  async function uploadFile(event: React.ChangeEvent<HTMLInputElement>) {
    const file = event.currentTarget.files?.[0];
    if (!file) return;
    const form = new FormData();
    form.append("file", file);
    const response = await fetch(`${API_BASE}/api/v1/uploads`, {
      method: "POST",
      body: form,
    });
    const data = await response.json();
    setUploadStatus(`Indexed ${data.chunks_indexed} chunks from ${file.name}`);
  }

  return (
    <main>
      <header>
        <div>
          <h1>WikipediaRag</h1>
          <p>Local Russian Wikipedia RAG MVP</p>
        </div>
        <span className={`status ${ready}`}>{ready}</span>
      </header>

      <section className="band grid">
        <div className="panel">
          <h2>
            <Database size={18} /> Wikipedia Import
          </h2>
          <div className="row">
            <input
              type="number"
              min={1}
              max={10000}
              value={limit}
              onChange={(event) => setLimit(Number(event.target.value))}
            />
            <button onClick={startImport}>
              <Play size={16} /> Start
            </button>
          </div>
          {job && (
            <div className="progress">
              <strong>{job.status}</strong>
              <span>{imported} pages</span>
              <span>{chunks} chunks</span>
              {job.error_message && (
                <span className="error">{job.error_message}</span>
              )}
            </div>
          )}
        </div>

        <div className="panel">
          <h2>
            <FileUp size={18} /> Upload
          </h2>
          <input type="file" accept=".txt,.md,.html" onChange={uploadFile} />
          {uploadStatus && <p>{uploadStatus}</p>}
        </div>
      </section>

      <section className="band">
        <form className="chat" onSubmit={submitChat}>
          <h2>
            <MessageSquare size={18} /> Chat
          </h2>
          <textarea
            value={question}
            onChange={(event) => setQuestion(event.target.value)}
          />
          <div className="row">
            <select
              value={mode}
              onChange={(event) =>
                setMode(event.target.value as "normal" | "extended")
              }
            >
              <option value="normal">Normal</option>
              <option value="extended">Extended</option>
            </select>
            <button type="submit">
              <Search size={16} /> Ask
            </button>
            <button type="button" onClick={loadDebugger} disabled={!queryRunId}>
              <Bug size={16} /> Debug
            </button>
            <button type="button" onClick={() => window.location.reload()}>
              <RotateCw size={16} />
            </button>
          </div>
        </form>

        {answer && (
          <div className="answer">
            <h2>Answer</h2>
            <p>{answer}</p>
            <h3>Sources</h3>
            <div className="sources">
              {evidence.map((item) => (
                <article key={item.evidence_id}>
                  <strong>
                    [{item.evidence_id}] {item.title}
                  </strong>
                  <span>{item.section_path.join(" / ")}</span>
                  <p>{item.content}</p>
                </article>
              ))}
            </div>
          </div>
        )}

        {events.length > 0 && (
          <div className="debugger">
            <h2>Retrieval Debugger</h2>
            {events.map((event, index) => (
              <pre key={`${event.stage}-${index}`}>
                {JSON.stringify(event, null, 2)}
              </pre>
            ))}
          </div>
        )}
      </section>
    </main>
  );
}

function parseSse(block: string): { event: string; data: SsePayload } | null {
  const event = block
    .split("\n")
    .find((line) => line.startsWith("event: "))
    ?.replace("event: ", "");
  const data = block
    .split("\n")
    .find((line) => line.startsWith("data: "))
    ?.replace("data: ", "");
  if (!event || !data) return null;
  return { event, data: JSON.parse(data) };
}
