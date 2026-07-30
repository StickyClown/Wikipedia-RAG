import {
  Bug,
  Database,
  FileUp,
  MessageSquare,
  Play,
  RotateCw,
  Search,
  SlidersHorizontal,
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
    stage?: string;
    bytes_received?: number;
    documents_total?: number;
    documents_completed?: number;
    documents_failed?: number;
    parser_route?: string;
    chunks_staged?: number;
    chunks_published?: number;
    timings_ms?: Record<string, number>;
  };
  error_code?: string | null;
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
  event_type?: string;
};

type SsePayload = {
  query_run_id: string;
  data: {
    text?: string;
    evidence?: Evidence[];
  };
};

type RetrievalOverrideState = {
  bm25Enabled: boolean;
  denseEnabled: boolean;
  rerankEnabled: boolean;
  fusionMode: "rrf" | "none";
  parentExpansion: "off" | "selective" | "always";
  extendedSearchMode: "off" | "conditional" | "always";
  topK: number;
};

type UploadSessionAccepted = {
  upload_session_id: string;
  upload_url: string;
  expires_at: string;
  required_headers: Record<string, string>;
};

type UploadCompleteResponse = {
  document_id: string;
  document_version_id: string;
  job_id: string;
  status: string;
};

type DocumentPublicMetadata = {
  id: string;
  title: string;
  filename?: string;
  status?: string;
  parser_route?: string | null;
  parser_name?: string | null;
  parser_version?: string | null;
  content_hash?: string | null;
  normalized_hash?: string | null;
  uploaded_at?: string | null;
  upload_completed_at?: string | null;
  ingested_at?: string | null;
  published_at?: string | null;
  public_metadata?: Record<string, unknown>;
};

export function App() {
  const [ready, setReady] = useState("checking");
  const [limit, setLimit] = useState(1000);
  const [job, setJob] = useState<Job | null>(null);
  const [question, setQuestion] = useState("Что такое Россия?");
  const [mode, setMode] = useState<"normal" | "extended">("normal");
  const [retrievalProfile, setRetrievalProfile] = useState("sota_mvp");
  const [debugTopK, setDebugTopK] = useState(12);
  const [bm25Enabled, setBm25Enabled] = useState(true);
  const [denseEnabled, setDenseEnabled] = useState(true);
  const [rerankEnabled, setRerankEnabled] = useState(true);
  const [fusionMode, setFusionMode] = useState<"rrf" | "none">("rrf");
  const [parentExpansion, setParentExpansion] = useState<
    "off" | "selective" | "always"
  >("selective");
  const [extendedSearchMode, setExtendedSearchMode] = useState<
    "off" | "conditional" | "always"
  >("conditional");
  const [answer, setAnswer] = useState("");
  const [evidence, setEvidence] = useState<Evidence[]>([]);
  const [queryRunId, setQueryRunId] = useState("");
  const [events, setEvents] = useState<RetrievalEvent[]>([]);
  const [uploadStatus, setUploadStatus] = useState("");
  const [uploadBusy, setUploadBusy] = useState(false);
  const [uploadJob, setUploadJob] = useState<Job | null>(null);
  const [uploadDocument, setUploadDocument] =
    useState<DocumentPublicMetadata | null>(null);
  const [uploadError, setUploadError] = useState("");

  useEffect(() => {
    fetch(`${API_BASE}/ready`)
      .then((response) => response.json())
      .then((data) => setReady(data.status))
      .catch(() => setReady("offline"));
  }, []);

  const imported = useMemo(() => job?.progress?.pages_imported ?? 0, [job]);
  const chunks = useMemo(() => job?.progress?.chunks_indexed ?? 0, [job]);

  async function startImport() {
    const response = await fetch(`${API_BASE}/api/v1/wikipedia/zim-imports`, {
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
      body: JSON.stringify({
        message: question,
        mode,
        stream: true,
        retrieval_profile: retrievalProfile,
        retrieval_overrides: buildRetrievalOverrides({
          bm25Enabled,
          denseEnabled,
          rerankEnabled,
          fusionMode,
          parentExpansion,
          extendedSearchMode,
          topK: debugTopK,
        }),
      }),
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
    const input = event.currentTarget;
    const file = input.files?.[0];
    if (!file) return;
    setUploadBusy(true);
    setUploadError("");
    setUploadJob(null);
    setUploadDocument(null);
    setUploadStatus(`Preparing ${file.name}`);
    try {
      const checksum = await sha256Hex(file);
      const sessionResponse = await fetch(
        `${API_BASE}/api/v1/uploads/sessions`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            filename: file.name,
            content_type: file.type || "application/octet-stream",
            size_bytes: file.size,
            checksum_sha256: checksum,
            parser_profile: "standard",
          }),
        },
      );
      if (!sessionResponse.ok) throw new Error(await sessionResponse.text());
      const session = (await sessionResponse.json()) as UploadSessionAccepted;
      setUploadStatus("Uploading to object storage");
      const putResponse = await fetch(session.upload_url, {
        method: "PUT",
        headers: session.required_headers,
        body: file,
      });
      if (!putResponse.ok) throw new Error(await putResponse.text());
      setUploadStatus("Completing upload session");
      const completeResponse = await fetch(
        `${API_BASE}/api/v1/uploads/sessions/${session.upload_session_id}:complete`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ metadata: { ui_upload: true } }),
        },
      );
      if (!completeResponse.ok) throw new Error(await completeResponse.text());
      const completed =
        (await completeResponse.json()) as UploadCompleteResponse;
      setUploadStatus(`Queued ${file.name}`);
      void pollUploadJob(completed.job_id, completed.document_id);
    } catch (error) {
      setUploadBusy(false);
      setUploadError(error instanceof Error ? error.message : String(error));
      setUploadStatus("");
    } finally {
      input.value = "";
    }
  }

  async function pollUploadJob(jobId: string, documentId: string) {
    const response = await fetch(`${API_BASE}/api/v1/ingestion-jobs/${jobId}`);
    const nextJob = (await response.json()) as Job;
    setUploadJob(nextJob);
    const stage = nextJob.progress?.stage ?? nextJob.status;
    setUploadStatus(`Ingestion ${stage}`);
    if (!["completed", "failed", "cancelled"].includes(nextJob.status)) {
      window.setTimeout(() => pollUploadJob(jobId, documentId), 1500);
      return;
    }
    setUploadBusy(false);
    if (nextJob.status === "completed") {
      const documentResponse = await fetch(
        `${API_BASE}/api/v1/documents/${encodeURIComponent(documentId)}`,
      );
      if (documentResponse.ok) {
        setUploadDocument(
          (await documentResponse.json()) as DocumentPublicMetadata,
        );
      }
    } else {
      setUploadError(
        nextJob.error_code ?? nextJob.error_message ?? nextJob.status,
      );
    }
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
          <input
            type="file"
            accept=".txt,.md,.markdown,.html,.htm,.csv,.tsv,.json,.jsonl,.pdf,.docx,.pptx,.xlsx"
            onChange={uploadFile}
            disabled={uploadBusy}
          />
          {uploadStatus && <p className="upload-status">{uploadStatus}</p>}
          {uploadJob && (
            <div className="progress upload-progress">
              <strong>{uploadJob.status}</strong>
              <span>{uploadJob.progress?.stage ?? "received"}</span>
              <span>
                {uploadJob.progress?.parser_route ?? "parser pending"}
              </span>
              <span>{uploadJob.progress?.bytes_received ?? 0} bytes</span>
              <span>
                {uploadJob.progress?.chunks_staged ?? 0} staged /{" "}
                {uploadJob.progress?.chunks_published ?? 0} published
              </span>
              {uploadJob.error_code && (
                <span className="error">{uploadJob.error_code}</span>
              )}
            </div>
          )}
          {uploadDocument && (
            <dl className="upload-meta">
              <div>
                <dt>Title</dt>
                <dd>{uploadDocument.title}</dd>
              </div>
              <div>
                <dt>Status</dt>
                <dd>{uploadDocument.status ?? "published"}</dd>
              </div>
              <div>
                <dt>Language</dt>
                <dd>{metadataValue(uploadDocument, "detected_language")}</dd>
              </div>
              <div>
                <dt>Document Date</dt>
                <dd>{metadataValue(uploadDocument, "document_date")}</dd>
              </div>
              <div>
                <dt>Parser</dt>
                <dd>{uploadDocument.parser_route ?? "unknown"}</dd>
              </div>
              <div>
                <dt>Uploaded</dt>
                <dd>{formatTimestamp(uploadDocument.uploaded_at)}</dd>
              </div>
            </dl>
          )}
          {uploadError && <p className="error">{uploadError}</p>}
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
          <details className="advanced">
            <summary>
              <SlidersHorizontal size={16} /> Advanced retrieval settings
            </summary>
            <div className="advanced-grid">
              <label>
                Profile
                <select
                  value={retrievalProfile}
                  onChange={(event) => setRetrievalProfile(event.target.value)}
                >
                  <option value="sota_mvp">sota_mvp</option>
                  <option value="test_mock">test_mock</option>
                  <option value="bm25_only">bm25_only</option>
                  <option value="rewrite_off">rewrite_off</option>
                  <option value="parent_expansion_off">
                    parent_expansion_off
                  </option>
                </select>
              </label>
              <label>
                Top K
                <input
                  type="number"
                  min={1}
                  max={50}
                  value={debugTopK}
                  onChange={(event) => setDebugTopK(Number(event.target.value))}
                />
              </label>
              <label className="checkbox">
                <input
                  type="checkbox"
                  checked={bm25Enabled}
                  onChange={(event) => setBm25Enabled(event.target.checked)}
                />
                BM25
              </label>
              <label className="checkbox">
                <input
                  type="checkbox"
                  checked={denseEnabled}
                  onChange={(event) => setDenseEnabled(event.target.checked)}
                />
                Dense
              </label>
              <label className="checkbox">
                <input
                  type="checkbox"
                  checked={rerankEnabled}
                  onChange={(event) => setRerankEnabled(event.target.checked)}
                />
                Rerank
              </label>
              <label>
                Fusion
                <select
                  value={fusionMode}
                  onChange={(event) =>
                    setFusionMode(event.target.value as "rrf" | "none")
                  }
                >
                  <option value="rrf">rrf</option>
                  <option value="none">none</option>
                </select>
              </label>
              <label>
                Parent expansion
                <select
                  value={parentExpansion}
                  onChange={(event) =>
                    setParentExpansion(
                      event.target.value as "off" | "selective" | "always",
                    )
                  }
                >
                  <option value="selective">selective</option>
                  <option value="off">off</option>
                  <option value="always">always</option>
                </select>
              </label>
              <label>
                Extended Search
                <select
                  value={extendedSearchMode}
                  onChange={(event) =>
                    setExtendedSearchMode(
                      event.target.value as "off" | "conditional" | "always",
                    )
                  }
                >
                  <option value="conditional">conditional</option>
                  <option value="off">off</option>
                  <option value="always">always</option>
                </select>
              </label>
            </div>
          </details>
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
                  <a href={item.source_url} target="_blank" rel="noreferrer">
                    [{item.evidence_id}] {item.title}
                  </a>
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
              <DebugStage key={`${event.stage}-${index}`} event={event} />
            ))}
          </div>
        )}
      </section>
    </main>
  );
}

function buildRetrievalOverrides(state: RetrievalOverrideState) {
  return {
    retrieval: {
      bm25: state.bm25Enabled,
      dense: state.denseEnabled,
      fusion: state.fusionMode,
      rerank: state.rerankEnabled,
      top_k: state.topK,
    },
    postprocess: {
      parent_expansion: state.parentExpansion,
      extended_search: state.extendedSearchMode,
    },
  };
}

async function sha256Hex(file: File) {
  const hashBuffer = await crypto.subtle.digest(
    "SHA-256",
    await file.arrayBuffer(),
  );
  return Array.from(new Uint8Array(hashBuffer))
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}

function metadataValue(document: DocumentPublicMetadata, key: string) {
  const value = document.public_metadata?.[key];
  if (typeof value === "string" || typeof value === "number") {
    return String(value);
  }
  return "";
}

function formatTimestamp(value?: string | null) {
  if (!value) return "";
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

function DebugStage({ event }: { event: RetrievalEvent }) {
  const payload =
    typeof event.payload === "object" && event.payload !== null
      ? (event.payload as Record<string, unknown>)
      : (event as unknown as Record<string, unknown>);
  const stage = String(payload.stage ?? event.stage);
  const rawCandidates = payload.candidates;
  const candidates = Array.isArray(rawCandidates)
    ? rawCandidates
        .filter((candidate): candidate is Record<string, unknown> => {
          return typeof candidate === "object" && candidate !== null;
        })
        .slice(0, 10)
    : [];
  return (
    <section className="debug-stage">
      <h3>{stage}</h3>
      {candidates.length > 0 ? (
        <table>
          <thead>
            <tr>
              <th>Title</th>
              <th>Ranks</th>
              <th>Scores</th>
            </tr>
          </thead>
          <tbody>
            {candidates.map((row, index) => {
              return (
                <tr key={`${stage}-${index}`}>
                  <td>{String(row.title ?? row.chunk_id ?? "")}</td>
                  <td>
                    <code>{JSON.stringify(row.ranks ?? {})}</code>
                  </td>
                  <td>
                    <code>{JSON.stringify(row.scores ?? {})}</code>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      ) : (
        <pre>{JSON.stringify(payload, null, 2)}</pre>
      )}
    </section>
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
