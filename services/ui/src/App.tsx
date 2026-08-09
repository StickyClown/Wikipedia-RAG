import {
  BookOpen,
  Bug,
  Database,
  ExternalLink,
  FileUp,
  KeyRound,
  LogIn,
  LogOut,
  MessageSquare,
  Play,
  Plug,
  RotateCw,
  Search,
  ShieldCheck,
  SlidersHorizontal,
  X,
} from "lucide-react";
import {
  FormEvent,
  KeyboardEvent,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { interpolate, Locale, useLocale } from "./i18n";

const API_BASE = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000";

const DEFAULT_DOCUMENT_ACCESS: DocumentAccess = {
  policy: "kb",
  user_ids: [],
  group_ids: [],
};

const SOURCE_KINDS = [
  "confluence_dc",
  "jira_dc",
  "gitlab_self_managed",
  "kiwix_zim",
  "local_folder",
  "internal_crawler",
  "sunduk_mock",
  "docsmart_mock",
] as const;

type SourceKind = (typeof SOURCE_KINDS)[number];

type SourceTemplate = {
  label: string;
  name: string;
  config: Record<string, unknown>;
  credentials: Record<string, unknown>;
};

const SOURCE_TEMPLATES: Record<SourceKind, SourceTemplate> = {
  confluence_dc: {
    label: "Confluence DC",
    name: "Confluence DC",
    config: {
      base_url: "https://confluence.local",
      space: "DOC",
      limit: 25,
    },
    credentials: { username: "", password: "" },
  },
  jira_dc: {
    label: "Jira DC",
    name: "Jira DC",
    config: {
      base_url: "https://jira.local",
      jql: "project = IT ORDER BY updated ASC",
      limit: 25,
      updated_overlap_minutes: 5,
    },
    credentials: { username: "", password: "" },
  },
  gitlab_self_managed: {
    label: "GitLab Self-Managed",
    name: "GitLab Self-Managed",
    config: {
      base_url: "https://gitlab.local",
      project_id: "123",
      ref: "main",
      path_allowlist: ["README.md", "docs"],
      max_files: 50,
    },
    credentials: { token: "" },
  },
  kiwix_zim: {
    label: "Kiwix/ZIM",
    name: "Kiwix ZIM",
    config: {
      zim_dir: "/zim",
      zim_filename: "wikipedia_ru_all.zim",
    },
    credentials: {},
  },
  local_folder: {
    label: "Local Folder",
    name: "Local Folder",
    config: {
      root_path: "/sources/docs",
      extensions: [".md", ".txt", ".html"],
      max_files: 1000,
    },
    credentials: {},
  },
  internal_crawler: {
    label: "Internal Crawler",
    name: "Internal Crawler",
    config: {
      base_url: "https://intranet.local",
      allowed_domains: ["intranet.local"],
      max_pages: 25,
      max_depth: 2,
      exclude_url_patterns: [],
    },
    credentials: { bearer_token: "" },
  },
  sunduk_mock: {
    label: "Sunduk Mock",
    name: "Sunduk Mock",
    config: {
      query: "резервное копирование",
      filters: { department: "IT" },
      limit: 20,
    },
    credentials: {},
  },
  docsmart_mock: {
    label: "DocSmart Mock",
    name: "DocSmart Mock",
    config: {},
    credentials: {},
  },
};

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
  created_at?: string;
};

type QueryRunSummary = {
  id?: string;
  mode?: string;
  status?: string;
  input_text?: string;
  answer?: string | null;
  model_alias?: string | null;
  error_code?: string | null;
  trace_id?: string;
  usage?: Record<string, unknown>;
};

type SsePayload = {
  query_run_id?: string;
  data?: {
    text?: string;
    evidence?: Evidence[];
    answer?: string;
    code?: string;
    safe_message?: string;
    [key: string]: unknown;
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

type UploadBatchAccepted = {
  batch_id: string;
  knowledge_base_id: string;
  status: string;
  total_items: number;
  items: Array<
    UploadSessionAccepted & {
      filename: string;
      content_type: string;
      size_bytes: number;
      checksum_sha256: string;
    }
  >;
};

type UploadBatchItemStatus = {
  upload_session_id: string;
  filename: string;
  content_type: string;
  size_bytes: number;
  checksum_sha256: string;
  status: string;
  upload_completed_at?: string | null;
  document_id?: string | null;
  document_version_id?: string | null;
  job_id?: string | null;
  job_status?: string | null;
  progress?: Job["progress"];
  error_code?: string | null;
  error_message?: string | null;
};

type UploadBatchStatus = {
  batch_id: string;
  knowledge_base_id: string;
  status: string;
  total_items: number;
  completed_items: number;
  failed_items: number;
  cancelled_items: number;
  pending_items: number;
  items: UploadBatchItemStatus[];
};

type UploadItemState = {
  id: string;
  filename: string;
  size_bytes: number;
  status: string;
  upload_session_id?: string;
  document_id?: string | null;
  job_id?: string | null;
  progress?: Job["progress"];
  error_code?: string | null;
  error_message?: string | null;
};

type AuthSession = {
  authenticated: boolean;
  csrf_token?: string | null;
  active_tenant_id?: string | null;
  tenant_role?: string | null;
  user?: {
    id: string;
    username?: string | null;
    display_name?: string | null;
    platform_role: string;
    password_change_required: boolean;
  } | null;
};

type KnowledgeBase = {
  id: string;
  name: string;
};

type DocumentAccessPolicy = "kb" | "tenant" | "restricted";

type DocumentAccess = {
  policy: DocumentAccessPolicy;
  user_ids: string[];
  group_ids: string[];
};

type AccessGroup = {
  id: string;
  name: string;
  group_type: string;
  external_id?: string | null;
};

type DocumentAccessResponse = {
  document_id: string;
  knowledge_base_id: string;
  document_access: DocumentAccess;
  document_access_origin: string;
};

type SourceAccessResponse = {
  source_id: string;
  knowledge_base_id: string;
  document_access_default: DocumentAccess;
  updated_documents: number;
};

type SourceResponse = {
  id: string;
  knowledge_base_id: string;
  kind: SourceKind | string;
  name: string;
  status: string;
  config: Record<string, unknown>;
  metadata: Record<string, unknown>;
  document_access_default: DocumentAccess;
  refresh_interval_seconds?: number | null;
  last_sync_run_id?: string | null;
  last_sync_status?: string | null;
  last_synced_at?: string | null;
  next_sync_at?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
};

type SourceListResponse = {
  sources: SourceResponse[];
};

type SourceHealthResponse = {
  source_id: string;
  status: string;
  details: Record<string, unknown>;
};

type SourceSyncResponse = {
  source_id: string;
  run_id: string;
  job_id: string;
  status: string;
};

type SourceSyncRunResponse = {
  id: string;
  source_id: string;
  knowledge_base_id: string;
  mode: string;
  status: string;
  stats: Record<string, unknown>;
  error_code?: string | null;
  error_message?: string | null;
  started_at?: string | null;
  completed_at?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
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

type SearchFilters = {
  document_type?: string;
  language?: string;
  date_from?: string;
  date_to?: string;
  source?: string;
};

type SearchResult = {
  chunk_id: string;
  document_id: string;
  document_version_id: string;
  knowledge_base_id: string;
  title: string;
  snippet: string;
  section_path: string[];
  source_url: string;
  source_type: string;
  document_type?: string | null;
  language?: string | null;
  document_date?: string | null;
  locator?: Record<string, unknown> | null;
  score: number;
  ranks: Record<string, number>;
  highlights?: { field: string; fragments: string[] }[];
};

type SearchFacet = {
  field: string;
  buckets: { value: string; count: number }[];
};

type SearchResponse = {
  results: SearchResult[];
  limit: number;
  offset: number;
  has_more: boolean;
  next_cursor?: string | null;
  facets?: SearchFacet[];
};

type DocumentSection = {
  section_id: string;
  parent_section_id?: string | null;
  title: string;
  level: number;
  path: string[];
  ordinal: number;
  locator?: Record<string, unknown>;
  first_chunk_id?: string | null;
  last_chunk_id?: string | null;
};

type DocumentStructure = {
  document_id: string;
  document_version_id?: string | null;
  knowledge_base_id: string;
  title: string;
  source_type: string;
  source_url?: string | null;
  sections: DocumentSection[];
  public_metadata?: Record<string, unknown>;
  document_access: DocumentAccess;
  document_access_origin?: string | null;
};

type DocumentContextChunk = {
  chunk_id: string;
  document_id: string;
  document_version_id?: string | null;
  knowledge_base_id: string;
  title: string;
  section_path: string[];
  content: string;
  source_url: string;
  locator?: Record<string, unknown>;
  prev_chunk_id?: string | null;
  next_chunk_id?: string | null;
  chunk_ordinal?: number | null;
  highlighted: boolean;
};

type DocumentContextResponse = {
  document_id: string;
  document_version_id?: string | null;
  anchor_chunk_id?: string | null;
  section_id?: string | null;
  chunks: DocumentContextChunk[];
  limit: number;
  offset: number;
};

type DocumentSearchResult = {
  chunk_id: string;
  document_id: string;
  document_version_id?: string | null;
  knowledge_base_id: string;
  title: string;
  snippet: string;
  section_path: string[];
  source_url: string;
  locator?: Record<string, unknown>;
  prev_chunk_id?: string | null;
  next_chunk_id?: string | null;
  score: number;
  ranks: Record<string, number>;
};

type DocumentSearchResponse = {
  document_id: string;
  document_version_id?: string | null;
  results: DocumentSearchResult[];
  limit: number;
  offset: number;
  has_more: boolean;
};

type ResearchRunSummary = {
  id: string;
  knowledge_base_id: string;
  knowledge_base_ids?: string[];
  topic: string;
  retrieval_profile: string;
  status: string;
  progress?: Record<string, unknown>;
  stop_reason?: string | null;
  error_code?: string | null;
  active_job_id?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  completed_at?: string | null;
};

type ResearchPlanQuestion = {
  question: string;
  ordinal: number;
  kind: string;
};

type ResearchPlanSummary = {
  id: string;
  knowledge_base_id: string;
  knowledge_base_ids: string[];
  user_id?: string | null;
  topic: string;
  retrieval_profile: string;
  tool_mode: string;
  status: string;
  notes: string;
  question_count: number;
  approved_run_id?: string | null;
  approved_at?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
};

type ResearchPlanDetail = {
  plan: ResearchPlanSummary;
  questions: ResearchPlanQuestion[];
  retrieval_overrides: Record<string, unknown>;
  context_policy: Record<string, unknown>;
};

type ResearchPlanListResponse = {
  plans: ResearchPlanSummary[];
};

type ResearchQuestionRecord = {
  id: string;
  question: string;
  ordinal: number;
  kind: string;
  status: string;
};

type ResearchCoverageRecord = {
  id: string;
  question_id: string;
  status: string;
  required_evidence_count: number;
  linked_evidence_ids: string[];
  reason: string;
  metrics?: Record<string, unknown>;
};

type ResearchEvidenceRecord = {
  id: string;
  question_id?: string | null;
  chunk_id: string;
  document_id?: string | null;
  evidence_ref: string;
  title: string;
  source_url: string;
  section_path: string[];
  content_abstract: string;
  support_status: string;
  score?: number | null;
};

type ResearchReflectionRecord = {
  id: string;
  episode_id?: string | null;
  reflection_type: string;
  body: string;
  metadata?: Record<string, unknown>;
  created_at?: string | null;
};

type ResearchEpisodeRecord = {
  id: string;
  query_run_id?: string | null;
  episode_index: number;
  question_id?: string | null;
  status: string;
  stage: string;
  context_summary?: Record<string, unknown>;
  metrics?: Record<string, unknown>;
};

type ResearchRunDetail = {
  run: ResearchRunSummary;
  questions: ResearchQuestionRecord[];
  coverage: ResearchCoverageRecord[];
  evidence: ResearchEvidenceRecord[];
  reflections: ResearchReflectionRecord[];
  episodes: ResearchEpisodeRecord[];
  final_report: {
    markdown?: string;
    coverage?: { covered: number; total: number };
    latest_reflection?: string;
    stop_reason?: string | null;
    partial_terminal?: boolean;
    failure_taxonomy?: Record<string, unknown>;
    report_format_version?: string;
    sections?: {
      confirmed_findings?: Array<{
        text: string;
        status?: string;
        evidence_refs?: string[];
      }>;
      partial_conflicting_findings?: Array<{
        text: string;
        status?: string;
        evidence_refs?: string[];
      }>;
      unresolved_questions?: string[];
      used_evidence?: ResearchEvidenceRecord[];
      limitations?: string[];
    };
  };
};

type ResearchRunListResponse = {
  runs: ResearchRunSummary[];
};

type WorkspaceTab = "chat" | "search" | "research" | "knowledge";

function HelpTooltip({ text }: { text: string }) {
  return (
    <span className="help-tooltip">
      <span
        className="help-tooltip-trigger"
        role="img"
        tabIndex={0}
        aria-label={text}
      >
        ?
      </span>
      <span className="help-tooltip-content" role="tooltip">
        {text}
      </span>
    </span>
  );
}

export function App() {
  const { locale, setLocale, t } = useLocale();
  const [activeTab, setActiveTab] = useState<WorkspaceTab>("chat");
  const [ready, setReady] = useState("checking");
  const [session, setSession] = useState<AuthSession>({
    authenticated: false,
  });
  const [authUsername, setAuthUsername] = useState("admin");
  const [authPassword, setAuthPassword] = useState("");
  const [authError, setAuthError] = useState("");
  const [knowledgeBases, setKnowledgeBases] = useState<KnowledgeBase[]>([]);
  const [selectedKnowledgeBaseId, setSelectedKnowledgeBaseId] = useState("");
  const [
    selectedRetrievalKnowledgeBaseIds,
    setSelectedRetrievalKnowledgeBaseIds,
  ] = useState<string[]>([]);
  const [newKnowledgeBaseName, setNewKnowledgeBaseName] = useState("");
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
  const [chatBusy, setChatBusy] = useState(false);
  const [chatError, setChatError] = useState("");
  const [events, setEvents] = useState<RetrievalEvent[]>([]);
  const [debuggerRun, setDebuggerRun] = useState<QueryRunSummary | null>(null);
  const [uploadStatus, setUploadStatus] = useState("");
  const [uploadBusy, setUploadBusy] = useState(false);
  const [uploadBatch, setUploadBatch] = useState<UploadBatchStatus | null>(
    null,
  );
  const [uploadItems, setUploadItems] = useState<UploadItemState[]>([]);
  const [uploadDocument, setUploadDocument] =
    useState<DocumentPublicMetadata | null>(null);
  const [uploadError, setUploadError] = useState("");
  const [sources, setSources] = useState<SourceResponse[]>([]);
  const [sourcesLoading, setSourcesLoading] = useState(false);
  const [sourcesError, setSourcesError] = useState("");
  const [sourceStatus, setSourceStatus] = useState("");
  const [sourceBusy, setSourceBusy] = useState<Record<string, boolean>>({});
  const [accessGroups, setAccessGroups] = useState<AccessGroup[]>([]);
  const [canManageAccess, setCanManageAccess] = useState(false);
  const [sourceAccess, setSourceAccess] = useState<DocumentAccess>(
    DEFAULT_DOCUMENT_ACCESS,
  );
  const [sourceRuns, setSourceRuns] = useState<
    Record<string, SourceSyncRunResponse>
  >({});
  const [sourceKind, setSourceKind] = useState<SourceKind>("local_folder");
  const [sourceName, setSourceName] = useState(
    SOURCE_TEMPLATES.local_folder.name,
  );
  const [sourceRefreshInterval, setSourceRefreshInterval] = useState("3600");
  const [sourceConfigText, setSourceConfigText] = useState(
    formatJson(SOURCE_TEMPLATES.local_folder.config),
  );
  const [sourceCredentialsText, setSourceCredentialsText] = useState(
    formatJson(SOURCE_TEMPLATES.local_folder.credentials),
  );
  const [searchQuery, setSearchQuery] = useState("");
  const [searchResults, setSearchResults] = useState<SearchResult[]>([]);
  const [searchBusy, setSearchBusy] = useState(false);
  const [searchError, setSearchError] = useState("");
  const [searchHasMore, setSearchHasMore] = useState(false);
  const [searchNextCursor, setSearchNextCursor] = useState<string | null>(null);
  const [searchFacets, setSearchFacets] = useState<SearchFacet[]>([]);
  const [searchDocumentType, setSearchDocumentType] = useState("");
  const [searchLanguage, setSearchLanguage] = useState("");
  const [searchDateFrom, setSearchDateFrom] = useState("");
  const [searchDateTo, setSearchDateTo] = useState("");
  const [searchSource, setSearchSource] = useState("");
  const [viewerStructure, setViewerStructure] =
    useState<DocumentStructure | null>(null);
  const [viewerContext, setViewerContext] =
    useState<DocumentContextResponse | null>(null);
  const [viewerSearchQuery, setViewerSearchQuery] = useState("");
  const [viewerSearchResults, setViewerSearchResults] = useState<
    DocumentSearchResult[]
  >([]);
  const [viewerBusy, setViewerBusy] = useState(false);
  const [viewerSearchBusy, setViewerSearchBusy] = useState(false);
  const [viewerAccessBusy, setViewerAccessBusy] = useState(false);
  const [viewerError, setViewerError] = useState("");
  const [researchTopic, setResearchTopic] = useState(
    "Сделай глубокое исследование по выбранной базе знаний",
  );
  const [researchKnowledgeBaseIds, setResearchKnowledgeBaseIds] = useState<
    string[]
  >([]);
  const researchPollRef = useRef<{
    timer?: number;
    controller?: AbortController;
    runId?: string;
  }>({});
  const chatControllerRef = useRef<AbortController | null>(null);
  const [researchPlans, setResearchPlans] = useState<ResearchPlanSummary[]>([]);
  const [researchPlanDetail, setResearchPlanDetail] =
    useState<ResearchPlanDetail | null>(null);
  const [researchPlanTopicDraft, setResearchPlanTopicDraft] = useState("");
  const [researchPlanNotesDraft, setResearchPlanNotesDraft] = useState("");
  const [researchPlanQuestionsDraft, setResearchPlanQuestionsDraft] =
    useState("");
  const [researchRuns, setResearchRuns] = useState<ResearchRunSummary[]>([]);
  const [researchDetail, setResearchDetail] =
    useState<ResearchRunDetail | null>(null);
  const [researchBusy, setResearchBusy] = useState(false);
  const [researchError, setResearchError] = useState("");

  useEffect(() => {
    fetch(`${API_BASE}/ready`)
      .then((response) => response.json())
      .then((data) => setReady(data.status))
      .catch(() => setReady("offline"));
  }, []);

  useEffect(
    () => () => {
      chatControllerRef.current?.abort();
      researchPollRef.current.controller?.abort();
      if (researchPollRef.current.timer)
        window.clearTimeout(researchPollRef.current.timer);
    },
    [],
  );

  useEffect(() => {
    let cancelled = false;
    async function loadInitialSession() {
      const response = await fetch(`${API_BASE}/api/v1/auth/session`, {
        credentials: "include",
      });
      if (!response.ok || cancelled) return;
      const nextSession = (await response.json()) as AuthSession;
      setSession(nextSession);
      if (!nextSession.authenticated) return;
      const kbResponse = await fetch(`${API_BASE}/api/v1/knowledge-bases`, {
        credentials: "include",
      });
      if (!kbResponse.ok || cancelled) return;
      const items = (await kbResponse.json()) as KnowledgeBase[];
      setKnowledgeBases(items);
      if (items[0]) {
        setSelectedKnowledgeBaseId(items[0].id);
        setSelectedRetrievalKnowledgeBaseIds([items[0].id]);
        setResearchKnowledgeBaseIds([items[0].id]);
        setActiveTab("chat");
      } else {
        setActiveTab("knowledge");
      }
    }
    void loadInitialSession();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!session.authenticated || !selectedKnowledgeBaseId) {
      setSources([]);
      setAccessGroups([]);
      setCanManageAccess(false);
      setSourcesLoading(false);
      setSourcesError("");
      return;
    }
    let cancelled = false;
    async function loadSelectedSources() {
      setSourcesLoading(true);
      setSourcesError("");
      try {
        const response = await fetch(
          `${API_BASE}/api/v1/knowledge-bases/${encodeURIComponent(selectedKnowledgeBaseId)}/sources`,
          { credentials: "include" },
        );
        if (!response.ok) throw new Error(await response.text());
        const payload = (await response.json()) as SourceListResponse;
        if (!cancelled) setSources(payload.sources);
      } catch (error) {
        if (!cancelled) {
          setSourcesError(
            error instanceof Error ? error.message : String(error),
          );
        }
      } finally {
        if (!cancelled) setSourcesLoading(false);
      }
    }
    async function loadSelectedAccessGroups() {
      try {
        const response = await fetch(
          `${API_BASE}/api/v1/knowledge-bases/${encodeURIComponent(selectedKnowledgeBaseId)}/access-groups`,
          { credentials: "include" },
        );
        if (response.status === 403) {
          if (!cancelled) {
            setAccessGroups([]);
            setCanManageAccess(false);
          }
          return;
        }
        if (!response.ok) throw new Error(await response.text());
        const payload = (await response.json()) as AccessGroup[];
        if (!cancelled) {
          setAccessGroups(payload);
          setCanManageAccess(true);
        }
      } catch {
        if (!cancelled) {
          setAccessGroups([]);
          setCanManageAccess(false);
        }
      }
    }
    void loadSelectedSources();
    void loadSelectedAccessGroups();
    return () => {
      cancelled = true;
    };
  }, [session.authenticated, selectedKnowledgeBaseId]);

  useEffect(() => {
    if (!session.authenticated) {
      setResearchPlans([]);
      setResearchPlanDetail(null);
      setResearchRuns([]);
      setResearchDetail(null);
      return;
    }
    let cancelled = false;
    async function loadInitialResearchState() {
      try {
        const [plansResponse, runsResponse] = await Promise.all([
          fetch(`${API_BASE}/api/v1/research-plans`, {
            credentials: "include",
          }),
          fetch(`${API_BASE}/api/v1/research-runs`, {
            credentials: "include",
          }),
        ]);
        if (!plansResponse.ok || !runsResponse.ok || cancelled) return;
        const plansPayload =
          (await plansResponse.json()) as ResearchPlanListResponse;
        const runsPayload =
          (await runsResponse.json()) as ResearchRunListResponse;
        if (!cancelled) {
          setResearchPlans(plansPayload.plans);
          setResearchRuns(runsPayload.runs);
        }
      } catch {
        if (!cancelled) {
          setResearchPlans([]);
          setResearchRuns([]);
        }
      }
    }
    void loadInitialResearchState();
    return () => {
      cancelled = true;
    };
  }, [session.authenticated]);

  async function loadResearchPlans() {
    if (!session.authenticated) return;
    setResearchError("");
    try {
      const response = await apiFetch("/api/v1/research-plans", {
        credentials: "include",
      });
      if (!response.ok) {
        throw new Error(
          await responseErrorMessage(response, t, "research_not_ready"),
        );
      }
      const payload = (await response.json()) as ResearchPlanListResponse;
      setResearchPlans(payload.plans);
    } catch (error) {
      setResearchError(error instanceof Error ? error.message : String(error));
    }
  }

  const imported = useMemo(() => job?.progress?.pages_imported ?? 0, [job]);
  const chunks = useMemo(() => job?.progress?.chunks_indexed ?? 0, [job]);
  const searchKnowledgeBaseIds =
    selectedRetrievalKnowledgeBaseIds.length > 0
      ? selectedRetrievalKnowledgeBaseIds
      : selectedKnowledgeBaseId
        ? [selectedKnowledgeBaseId]
        : [];

  const workspaceTabs: Array<{ id: WorkspaceTab; label: string }> = [
    { id: "chat", label: t("chat") },
    { id: "search", label: t("search") },
    { id: "research", label: t("research") },
    { id: "knowledge", label: t("knowledge_base") },
  ];

  function handleWorkspaceKeyDown(event: KeyboardEvent<HTMLButtonElement>) {
    const currentIndex = workspaceTabs.findIndex((tab) => tab.id === activeTab);
    let nextIndex = currentIndex;
    if (event.key === "ArrowRight")
      nextIndex = (currentIndex + 1) % workspaceTabs.length;
    if (event.key === "ArrowLeft")
      nextIndex =
        (currentIndex - 1 + workspaceTabs.length) % workspaceTabs.length;
    if (event.key === "Home") nextIndex = 0;
    if (event.key === "End") nextIndex = workspaceTabs.length - 1;
    if (nextIndex === currentIndex) return;
    event.preventDefault();
    setActiveTab(workspaceTabs[nextIndex].id);
    requestAnimationFrame(() => {
      document.getElementById(`tab-${workspaceTabs[nextIndex].id}`)?.focus();
    });
  }

  async function apiFetch(path: string, init: RequestInit = {}) {
    const method = init.method?.toUpperCase() ?? "GET";
    const headers = new Headers(init.headers);
    if (method !== "GET" && session.csrf_token) {
      headers.set("X-CSRF-Token", session.csrf_token);
    }
    return fetch(`${API_BASE}${path}`, {
      ...init,
      credentials: "include",
      headers,
    });
  }

  async function refreshSession() {
    const response = await fetch(`${API_BASE}/api/v1/auth/session`, {
      credentials: "include",
    });
    if (response.ok) {
      const nextSession = (await response.json()) as AuthSession;
      setSession(nextSession);
      if (nextSession.authenticated) {
        await loadKnowledgeBases();
      }
    }
  }

  async function localLogin(event: FormEvent) {
    event.preventDefault();
    setAuthError("");
    const response = await fetch(`${API_BASE}/api/v1/auth/local/login`, {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        username: authUsername,
        password: authPassword,
      }),
    });
    if (!response.ok) {
      setAuthError(await response.text());
      return;
    }
    setAuthPassword("");
    await refreshSession();
  }

  async function oidcLogin() {
    setAuthError("");
    const response = await fetch(`${API_BASE}/api/v1/auth/oidc/start`, {
      method: "POST",
      credentials: "include",
    });
    if (!response.ok) {
      setAuthError(await response.text());
      return;
    }
    const started = (await response.json()) as { authorization_url: string };
    window.location.assign(started.authorization_url);
  }

  async function logout() {
    const response = await apiFetch("/api/v1/auth/logout", { method: "POST" });
    if (response.ok) {
      setSession({ authenticated: false });
      setKnowledgeBases([]);
      setSelectedKnowledgeBaseId("");
      setSelectedRetrievalKnowledgeBaseIds([]);
      setResearchKnowledgeBaseIds([]);
      setSources([]);
      setSourceRuns({});
      setSearchResults([]);
      setSearchError("");
    }
  }

  async function loadKnowledgeBases() {
    const response = await apiFetch("/api/v1/knowledge-bases");
    if (!response.ok) return;
    const items = (await response.json()) as KnowledgeBase[];
    setKnowledgeBases(items);
    if (items.length === 0) setActiveTab("knowledge");
    if (!selectedKnowledgeBaseId && items[0]) {
      setSelectedKnowledgeBaseId(items[0].id);
      setActiveTab("chat");
    }
    if (selectedRetrievalKnowledgeBaseIds.length === 0 && items[0]) {
      setSelectedRetrievalKnowledgeBaseIds([items[0].id]);
    }
    if (researchKnowledgeBaseIds.length === 0 && items[0])
      setResearchKnowledgeBaseIds([items[0].id]);
  }

  async function loadSources(kbId = selectedKnowledgeBaseId) {
    if (!kbId) return;
    setSourcesLoading(true);
    setSourcesError("");
    try {
      const response = await apiFetch(
        `/api/v1/knowledge-bases/${encodeURIComponent(kbId)}/sources`,
      );
      if (!response.ok) {
        throw new Error(
          await responseErrorMessage(response, t, "request_failed"),
        );
      }
      const payload = (await response.json()) as SourceListResponse;
      setSources(payload.sources);
    } catch (error) {
      setSourcesError(error instanceof Error ? error.message : String(error));
    } finally {
      setSourcesLoading(false);
    }
  }

  async function createKnowledgeBase(event: FormEvent) {
    event.preventDefault();
    if (!newKnowledgeBaseName.trim()) return;
    const response = await apiFetch("/api/v1/knowledge-bases", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: newKnowledgeBaseName.trim() }),
    });
    if (response.ok) {
      setNewKnowledgeBaseName("");
      await loadKnowledgeBases();
    }
  }

  function selectSourceKind(kind: SourceKind) {
    const template = SOURCE_TEMPLATES[kind];
    setSourceKind(kind);
    setSourceName(template.name);
    setSourceConfigText(formatJson(template.config));
    setSourceCredentialsText(formatJson(template.credentials));
    setSourceStatus("");
    setSourcesError("");
  }

  async function createSource(event: FormEvent) {
    event.preventDefault();
    if (!selectedKnowledgeBaseId || !sourceName.trim()) return;
    setSourceStatus("");
    setSourcesError("");
    setSourceBusy((busy) => ({ ...busy, create: true }));
    try {
      const refreshInterval = sourceRefreshInterval.trim()
        ? Number(sourceRefreshInterval)
        : null;
      if (
        refreshInterval !== null &&
        (!Number.isInteger(refreshInterval) || refreshInterval < 60)
      ) {
        throw new Error("refresh_interval_seconds must be at least 60");
      }
      const response = await apiFetch(
        `/api/v1/knowledge-bases/${encodeURIComponent(selectedKnowledgeBaseId)}/sources`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            kind: sourceKind,
            name: sourceName.trim(),
            config: parseJsonObject("config", sourceConfigText),
            credentials: parseJsonObject("credentials", sourceCredentialsText),
            refresh_interval_seconds: refreshInterval,
            document_access_default: sourceAccess,
            metadata: { ui_created: true },
          }),
        },
      );
      if (!response.ok) throw new Error(await response.text());
      const created = (await response.json()) as SourceResponse;
      setSourceStatus(`Created ${created.name}`);
      setSourceCredentialsText(
        formatJson(SOURCE_TEMPLATES[sourceKind].credentials),
      );
      await loadSources(selectedKnowledgeBaseId);
    } catch (error) {
      setSourcesError(error instanceof Error ? error.message : String(error));
    } finally {
      setSourceBusy((busy) => ({ ...busy, create: false }));
    }
  }

  async function healthcheckSource(source: SourceResponse) {
    setSourceBusy((busy) => ({ ...busy, [source.id]: true }));
    setSourcesError("");
    try {
      const response = await apiFetch(
        `/api/v1/knowledge-bases/${encodeURIComponent(source.knowledge_base_id)}/sources/${encodeURIComponent(source.id)}:healthcheck`,
        { method: "POST" },
      );
      if (!response.ok) throw new Error(await response.text());
      const health = (await response.json()) as SourceHealthResponse;
      setSourceStatus(`${source.name}: ${health.status}`);
    } catch (error) {
      setSourcesError(error instanceof Error ? error.message : String(error));
    } finally {
      setSourceBusy((busy) => ({ ...busy, [source.id]: false }));
    }
  }

  async function syncSource(
    source: SourceResponse,
    mode: "incremental" | "full",
  ) {
    setSourceBusy((busy) => ({ ...busy, [source.id]: true }));
    setSourcesError("");
    try {
      const response = await apiFetch(
        `/api/v1/knowledge-bases/${encodeURIComponent(source.knowledge_base_id)}/sources/${encodeURIComponent(source.id)}:sync`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ mode }),
        },
      );
      if (!response.ok) throw new Error(await response.text());
      const payload = (await response.json()) as SourceSyncResponse;
      setSourceStatus(`${source.name}: sync ${payload.status}`);
      void pollSourceSyncRun(payload.run_id);
      await loadSources(source.knowledge_base_id);
    } catch (error) {
      setSourcesError(error instanceof Error ? error.message : String(error));
      setSourceBusy((busy) => ({ ...busy, [source.id]: false }));
    }
  }

  async function patchSourceStatus(source: SourceResponse) {
    const nextStatus = source.status === "disabled" ? "active" : "disabled";
    setSourceBusy((busy) => ({ ...busy, [source.id]: true }));
    setSourcesError("");
    try {
      const response = await apiFetch(
        `/api/v1/knowledge-bases/${encodeURIComponent(source.knowledge_base_id)}/sources/${encodeURIComponent(source.id)}`,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ status: nextStatus }),
        },
      );
      if (!response.ok) throw new Error(await response.text());
      setSourceStatus(`${source.name}: ${nextStatus}`);
      await loadSources(source.knowledge_base_id);
    } catch (error) {
      setSourcesError(error instanceof Error ? error.message : String(error));
    } finally {
      setSourceBusy((busy) => ({ ...busy, [source.id]: false }));
    }
  }

  async function patchSourceAccess(
    source: SourceResponse,
    documentAccess: DocumentAccess,
  ) {
    setSourceBusy((busy) => ({ ...busy, [`${source.id}:access`]: true }));
    setSourcesError("");
    try {
      const response = await apiFetch(
        `/api/v1/knowledge-bases/${encodeURIComponent(source.knowledge_base_id)}/sources/${encodeURIComponent(source.id)}/access`,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ...documentAccess, apply_to_existing: true }),
        },
      );
      if (!response.ok) throw new Error(await response.text());
      const payload = (await response.json()) as SourceAccessResponse;
      setSourceStatus(
        `${source.name}: access updated for ${payload.updated_documents} documents`,
      );
      await loadSources(source.knowledge_base_id);
    } catch (error) {
      setSourcesError(error instanceof Error ? error.message : String(error));
    } finally {
      setSourceBusy((busy) => ({ ...busy, [`${source.id}:access`]: false }));
    }
  }

  async function pollSourceSyncRun(runId: string) {
    const response = await apiFetch(
      `/api/v1/source-sync-runs/${encodeURIComponent(runId)}`,
    );
    if (!response.ok) {
      setSourcesError(await response.text());
      return;
    }
    const run = (await response.json()) as SourceSyncRunResponse;
    setSourceRuns((runs) => ({ ...runs, [run.source_id]: run }));
    if (!["completed", "failed", "cancelled"].includes(run.status)) {
      window.setTimeout(() => pollSourceSyncRun(runId), 1500);
      return;
    }
    setSourceBusy((busy) => ({ ...busy, [run.source_id]: false }));
    await loadSources(run.knowledge_base_id);
  }

  async function startImport() {
    const response = await apiFetch("/api/v1/wikipedia/zim-imports", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ limit }),
    });
    const created = await response.json();
    pollJob(created.job_id);
  }

  async function pollJob(jobId: string) {
    const response = await apiFetch(`/api/v1/ingestion-jobs/${jobId}`);
    const nextJob = await response.json();
    setJob(nextJob);
    if (!["completed", "failed", "cancelled"].includes(nextJob.status)) {
      window.setTimeout(() => pollJob(jobId), 1500);
    }
  }

  async function submitChat(event: FormEvent) {
    event.preventDefault();
    if (!question.trim()) {
      setChatError(t("chat_empty"));
      return;
    }
    setAnswer("");
    setEvidence([]);
    setQueryRunId("");
    setEvents([]);
    setDebuggerRun(null);
    setChatError("");
    setChatBusy(true);
    const controller = new AbortController();
    chatControllerRef.current = controller;
    try {
      const response = await apiFetch("/api/v1/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        signal: controller.signal,
        body: JSON.stringify({
          message: question,
          knowledge_base_ids:
            selectedRetrievalKnowledgeBaseIds.length > 0
              ? selectedRetrievalKnowledgeBaseIds
              : selectedKnowledgeBaseId
                ? [selectedKnowledgeBaseId]
                : [],
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
      if (!response.ok) {
        throw new Error(await responseErrorMessage(response, t, "chat_failed"));
      }
      const reader = response.body?.getReader();
      if (!reader) throw new Error(t("chat_stream_unavailable"));
      const decoder = new TextDecoder();
      let buffer = "";
      let terminalEvent = false;
      let hasAnswer = false;
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const parts = buffer.split("\n\n");
        buffer = parts.pop() ?? "";
        for (const part of parts) {
          const parsed = parseSse(part);
          if (!parsed) continue;
          if (parsed.event === "message.delta") {
            setAnswer(parsed.data.data?.text ?? "");
            setEvidence(parsed.data.data?.evidence ?? []);
            hasAnswer = true;
            if (parsed.data.query_run_id)
              setQueryRunId(parsed.data.query_run_id);
          }
          if (parsed.event === "run.failed") {
            terminalEvent = true;
            if (parsed.data.query_run_id)
              setQueryRunId(parsed.data.query_run_id);
            setChatError(sseFailureMessage(parsed.data, t));
          }
          if (parsed.event === "run.completed") {
            terminalEvent = true;
            if (parsed.data.query_run_id)
              setQueryRunId(parsed.data.query_run_id);
            const completedAnswer = parsed.data.data?.answer;
            if (typeof completedAnswer === "string" && !hasAnswer) {
              setAnswer(completedAnswer);
            }
          }
        }
      }
      if (!terminalEvent) setChatError(t("chat_incomplete"));
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") {
        setChatError(t("chat_stopped"));
      } else {
        setChatError(error instanceof Error ? error.message : t("chat_failed"));
      }
    } finally {
      if (chatControllerRef.current === controller) {
        chatControllerRef.current = null;
      }
      setChatBusy(false);
    }
  }

  function stopChat() {
    chatControllerRef.current?.abort();
  }

  async function submitSearch(event: FormEvent) {
    event.preventDefault();
    await runSearch(0);
  }

  async function loadMoreSearch() {
    await runSearch(searchResults.length, searchNextCursor);
  }

  async function runSearch(offset: number, cursor: string | null = null) {
    const query = searchQuery.trim();
    if (!query || searchKnowledgeBaseIds.length === 0) return;
    setSearchBusy(true);
    setSearchError("");
    if (offset === 0) {
      setSearchResults([]);
      setSearchHasMore(false);
      setSearchNextCursor(null);
      setSearchFacets([]);
    }
    try {
      const filters = buildSearchFilters({
        documentType: searchDocumentType,
        language: searchLanguage,
        dateFrom: searchDateFrom,
        dateTo: searchDateTo,
        source: searchSource,
      });
      const response = await apiFetch("/api/v1/search", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          query,
          knowledge_base_ids: searchKnowledgeBaseIds,
          limit: 10,
          offset,
          cursor,
          group_by_document: true,
          include_highlights: true,
          include_facets: true,
          filters,
        }),
      });
      if (!response.ok) {
        throw new Error(
          await responseErrorMessage(response, t, "search_not_ready"),
        );
      }
      const payload = (await response.json()) as SearchResponse;
      setSearchResults((items) =>
        offset === 0 ? payload.results : [...items, ...payload.results],
      );
      setSearchHasMore(payload.has_more);
      setSearchNextCursor(payload.next_cursor ?? null);
      setSearchFacets(payload.facets ?? []);
    } catch (error) {
      setSearchError(error instanceof Error ? error.message : String(error));
    } finally {
      setSearchBusy(false);
    }
  }

  async function loadResearchRuns() {
    if (!session.authenticated) return;
    setResearchError("");
    try {
      const response = await apiFetch("/api/v1/research-runs");
      if (!response.ok) {
        throw new Error(
          await responseErrorMessage(response, t, "request_failed"),
        );
      }
      const payload = (await response.json()) as ResearchRunListResponse;
      setResearchRuns(payload.runs);
    } catch (error) {
      setResearchError(error instanceof Error ? error.message : String(error));
    }
  }

  async function loadResearchPlanDetail(planId: string) {
    setResearchError("");
    try {
      const response = await apiFetch(
        `/api/v1/research-plans/${encodeURIComponent(planId)}`,
      );
      if (!response.ok) {
        throw new Error(
          await responseErrorMessage(response, t, "research_not_ready"),
        );
      }
      const detail = (await response.json()) as ResearchPlanDetail;
      setResearchPlanDetail(detail);
      setResearchPlanTopicDraft(detail.plan.topic);
      setResearchPlanNotesDraft(detail.plan.notes);
      setResearchPlanQuestionsDraft(
        detail.questions
          .slice()
          .sort((left, right) => left.ordinal - right.ordinal)
          .map((item) => item.question)
          .join("\n"),
      );
    } catch (error) {
      setResearchError(error instanceof Error ? error.message : String(error));
    }
  }

  async function loadResearchRunDetail(runId: string) {
    if (researchPollRef.current.runId !== runId) {
      researchPollRef.current.controller?.abort();
      if (researchPollRef.current.timer)
        window.clearTimeout(researchPollRef.current.timer);
      researchPollRef.current = { runId, controller: new AbortController() };
    }
    const poll = researchPollRef.current;
    setResearchError("");
    try {
      const response = await apiFetch(
        `/api/v1/research-runs/${encodeURIComponent(runId)}`,
        { signal: poll.controller?.signal },
      );
      if (!response.ok) {
        throw new Error(
          await responseErrorMessage(response, t, "request_failed"),
        );
      }
      const detail = (await response.json()) as ResearchRunDetail;
      if (researchPollRef.current.runId !== runId) return;
      setResearchDetail(detail);
      if (["received", "running"].includes(detail.run.status)) {
        poll.timer = window.setTimeout(
          () => void loadResearchRunDetail(runId),
          2000,
        );
      } else {
        poll.controller?.abort();
      }
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return;
      setResearchError(error instanceof Error ? error.message : String(error));
    }
  }

  async function createResearchPlanFromDraft() {
    if (!researchTopic.trim() || !selectedKnowledgeBaseId) return;
    setResearchBusy(true);
    setResearchError("");
    try {
      const response = await apiFetch("/api/v1/research-plans", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          topic: researchTopic,
          knowledge_base_id: selectedKnowledgeBaseId,
          knowledge_base_ids: researchKnowledgeBaseIds.slice(0, 3),
          retrieval_profile: retrievalProfile,
          retrieval_overrides: buildRetrievalOverrides({
            bm25Enabled,
            denseEnabled,
            rerankEnabled,
            fusionMode,
            parentExpansion,
            extendedSearchMode: "always",
            topK: debugTopK,
          }),
          notes: "",
        }),
      });
      if (!response.ok) {
        throw new Error(
          await responseErrorMessage(response, t, "research_not_ready"),
        );
      }
      const action = (await response.json()) as { plan_id: string };
      await loadResearchPlans();
      await loadResearchPlanDetail(action.plan_id);
    } catch (error) {
      setResearchError(error instanceof Error ? error.message : String(error));
    } finally {
      setResearchBusy(false);
    }
  }

  async function saveResearchPlan() {
    if (!researchPlanDetail) return;
    setResearchBusy(true);
    setResearchError("");
    try {
      const questions = researchPlanQuestionsDraft
        .split("\n")
        .map((item) => item.trim())
        .filter(Boolean)
        .map((question, index) => ({
          question,
          ordinal: index + 1,
          kind: index === 0 ? "primary" : "derived",
        }));
      const response = await apiFetch(
        `/api/v1/research-plans/${encodeURIComponent(researchPlanDetail.plan.id)}`,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            topic: researchPlanTopicDraft,
            notes: researchPlanNotesDraft,
            questions,
          }),
        },
      );
      if (!response.ok) {
        throw new Error(
          await responseErrorMessage(response, t, "request_failed"),
        );
      }
      await loadResearchPlans();
      await loadResearchPlanDetail(researchPlanDetail.plan.id);
    } catch (error) {
      setResearchError(error instanceof Error ? error.message : String(error));
    } finally {
      setResearchBusy(false);
    }
  }

  async function approveResearchPlan(planId: string) {
    setResearchBusy(true);
    setResearchError("");
    try {
      const response = await apiFetch(
        `/api/v1/research-plans/${encodeURIComponent(planId)}:approve`,
        { method: "POST" },
      );
      if (!response.ok) {
        throw new Error(
          await responseErrorMessage(response, t, "request_failed"),
        );
      }
      const action = (await response.json()) as { run_id?: string | null };
      await loadResearchPlans();
      await loadResearchRuns();
      await loadResearchPlanDetail(planId);
      if (action.run_id) {
        await loadResearchRunDetail(action.run_id);
      }
    } catch (error) {
      setResearchError(error instanceof Error ? error.message : String(error));
    } finally {
      setResearchBusy(false);
    }
  }

  async function submitResearchRun(event: FormEvent) {
    event.preventDefault();
    if (!researchTopic.trim() || !selectedKnowledgeBaseId) return;
    setResearchBusy(true);
    setResearchError("");
    try {
      const response = await apiFetch("/api/v1/research-runs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          topic: researchTopic,
          knowledge_base_id: selectedKnowledgeBaseId,
          knowledge_base_ids: researchKnowledgeBaseIds.slice(0, 3),
          retrieval_profile: retrievalProfile,
          retrieval_overrides: buildRetrievalOverrides({
            bm25Enabled,
            denseEnabled,
            rerankEnabled,
            fusionMode,
            parentExpansion,
            extendedSearchMode: "always",
            topK: debugTopK,
          }),
        }),
      });
      if (!response.ok) {
        throw new Error(
          await responseErrorMessage(response, t, "research_not_ready"),
        );
      }
      const action = (await response.json()) as {
        run_id: string;
        job_id?: string | null;
      };
      await loadResearchRuns();
      await loadResearchRunDetail(action.run_id);
    } catch (error) {
      setResearchError(error instanceof Error ? error.message : String(error));
    } finally {
      setResearchBusy(false);
    }
  }

  async function researchRunAction(
    runId: string,
    action: "pause" | "resume" | "cancel",
  ) {
    setResearchBusy(true);
    setResearchError("");
    try {
      const response = await apiFetch(
        `/api/v1/research-runs/${encodeURIComponent(runId)}:${action}`,
        { method: "POST" },
      );
      if (!response.ok) {
        throw new Error(
          await responseErrorMessage(response, t, "request_failed"),
        );
      }
      await loadResearchRuns();
      await loadResearchRunDetail(runId);
    } catch (error) {
      setResearchError(error instanceof Error ? error.message : String(error));
    } finally {
      setResearchBusy(false);
    }
  }

  async function openResearchDebugger(detail: ResearchRunDetail) {
    setResearchError("");
    try {
      const latest = [...detail.episodes]
        .reverse()
        .find((episode) => episode.query_run_id);
      if (!latest?.query_run_id) return;
      setQueryRunId(latest.query_run_id);
      const response = await apiFetch(
        `/api/v1/query-runs/${latest.query_run_id}/retrieval`,
      );
      if (!response.ok) {
        throw new Error(
          await responseErrorMessage(response, t, "request_failed"),
        );
      }
      const data = await response.json();
      setDebuggerRun(data.run ?? null);
      setEvents(data.events ?? []);
    } catch (error) {
      setResearchError(error instanceof Error ? error.message : String(error));
    }
  }

  async function downloadResearchFile(kind: "markdown" | "csv" | "docx") {
    if (!researchDetail) return;
    const report = researchDetail.final_report;
    const sections = report.sections;
    const slug =
      researchDetail.run.topic
        .toLowerCase()
        .replace(/[^a-z0-9а-яё]+/gi, "-")
        .replace(/^-|-$/g, "")
        .slice(0, 48) || "research";
    let body = report.markdown ?? "";
    let mime = "text/markdown;charset=utf-8";
    let extension = "md";
    if (kind === "csv") {
      const rows = [
        [
          "section",
          "status",
          "text",
          "evidence_refs",
          "evidence_title",
          "source_url",
        ],
      ];
      for (const [section, items] of Object.entries({
        confirmed: sections?.confirmed_findings ?? [],
        partial: sections?.partial_conflicting_findings ?? [],
      })) {
        for (const item of items)
          rows.push([
            section,
            item.status ?? "",
            item.text,
            (item.evidence_refs ?? []).join(" "),
            "",
            "",
          ]);
      }
      body =
        "\ufeff" +
        rows
          .map((row) =>
            row
              .map((value) => `"${String(value).replaceAll('"', '""')}"`)
              .join(","),
          )
          .join("\n");
      mime = "text/csv;charset=utf-8";
      extension = "csv";
    } else if (kind === "docx") {
      const { Document, HeadingLevel, Packer, Paragraph } = await import(
        "docx"
      );
      const doc = new Document({
        sections: [
          {
            children: [
              new Paragraph({
                text: researchDetail.run.topic,
                heading: HeadingLevel.TITLE,
              }),
              ...(report.markdown ?? "")
                .split("\n")
                .map((line) => new Paragraph({ text: line })),
            ],
          },
        ],
      });
      const blob = await Packer.toBlob(doc);
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `${slug}-${researchDetail.run.id.slice(0, 8)}.docx`;
      anchor.click();
      URL.revokeObjectURL(url);
      return;
    }
    const url = URL.createObjectURL(new Blob([body], { type: mime }));
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `${slug}-${researchDetail.run.id.slice(0, 8)}.${extension}`;
    anchor.click();
    URL.revokeObjectURL(url);
  }

  async function openDocumentViewer(item: SearchResult) {
    setViewerBusy(true);
    setViewerError("");
    setViewerSearchResults([]);
    setViewerSearchQuery("");
    try {
      const structureResponse = await apiFetch(
        `/api/v1/documents/${encodeURIComponent(item.document_id)}/structure`,
      );
      if (!structureResponse.ok) {
        throw new Error(
          await responseErrorMessage(structureResponse, t, "request_failed"),
        );
      }
      const structure = (await structureResponse.json()) as DocumentStructure;
      setViewerStructure(structure);
      await loadDocumentContext(item.document_id, { chunkId: item.chunk_id });
    } catch (error) {
      setViewerError(error instanceof Error ? error.message : String(error));
    } finally {
      setViewerBusy(false);
    }
  }

  async function loadDocumentContext(
    documentId: string,
    options: { chunkId?: string; sectionId?: string } = {},
  ) {
    setViewerBusy(true);
    setViewerError("");
    try {
      const params = new URLSearchParams();
      if (options.chunkId) params.set("chunk_id", options.chunkId);
      if (options.sectionId) params.set("section_id", options.sectionId);
      params.set("before", "2");
      params.set("after", "2");
      const response = await apiFetch(
        `/api/v1/documents/${encodeURIComponent(documentId)}/context?${params.toString()}`,
      );
      if (!response.ok) {
        throw new Error(
          await responseErrorMessage(response, t, "request_failed"),
        );
      }
      setViewerContext((await response.json()) as DocumentContextResponse);
    } catch (error) {
      setViewerError(error instanceof Error ? error.message : String(error));
    } finally {
      setViewerBusy(false);
    }
  }

  async function submitDocumentSearch(event: FormEvent) {
    event.preventDefault();
    const query = viewerSearchQuery.trim();
    if (!viewerStructure || !query) return;
    setViewerSearchBusy(true);
    setViewerError("");
    try {
      const response = await apiFetch(
        `/api/v1/documents/${encodeURIComponent(viewerStructure.document_id)}/search`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ query, limit: 10, offset: 0 }),
        },
      );
      if (!response.ok) {
        throw new Error(
          await responseErrorMessage(response, t, "request_failed"),
        );
      }
      const payload = (await response.json()) as DocumentSearchResponse;
      setViewerSearchResults(payload.results);
    } catch (error) {
      setViewerError(error instanceof Error ? error.message : String(error));
    } finally {
      setViewerSearchBusy(false);
    }
  }

  async function patchViewerAccess(documentAccess: DocumentAccess) {
    if (!viewerStructure) return;
    setViewerAccessBusy(true);
    setViewerError("");
    try {
      const response = await apiFetch(
        `/api/v1/documents/${encodeURIComponent(viewerStructure.document_id)}/access`,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(documentAccess),
        },
      );
      if (!response.ok) {
        throw new Error(
          await responseErrorMessage(response, t, "request_failed"),
        );
      }
      const payload = (await response.json()) as DocumentAccessResponse;
      setViewerStructure({
        ...viewerStructure,
        document_access: payload.document_access,
        document_access_origin: payload.document_access_origin,
      });
    } catch (error) {
      setViewerError(error instanceof Error ? error.message : String(error));
    } finally {
      setViewerAccessBusy(false);
    }
  }

  function closeDocumentViewer() {
    setViewerStructure(null);
    setViewerContext(null);
    setViewerSearchResults([]);
    setViewerSearchQuery("");
    setViewerError("");
  }

  async function loadDebugger() {
    if (!queryRunId) return;
    const response = await apiFetch(
      `/api/v1/query-runs/${queryRunId}/retrieval`,
    );
    const data = await response.json();
    setDebuggerRun(data.run ?? null);
    setEvents(data.events);
  }

  function toggleRetrievalKnowledgeBase(kbId: string): void {
    setSelectedRetrievalKnowledgeBaseIds((selected) =>
      selected.includes(kbId)
        ? selected.filter((candidate) => candidate !== kbId)
        : [...selected, kbId],
    );
  }

  function patchUploadItem(id: string, patch: Partial<UploadItemState>): void {
    setUploadItems((items) =>
      items.map((item) => (item.id === id ? { ...item, ...patch } : item)),
    );
  }

  async function uploadFile(event: React.ChangeEvent<HTMLInputElement>) {
    const input = event.currentTarget;
    const files = Array.from(input.files ?? []);
    if (files.length === 0) return;
    const planned = files.map((file, index) => ({
      id: `local-${Date.now()}-${index}`,
      filename: file.name,
      size_bytes: file.size,
      status: "preparing",
    }));
    setUploadBusy(true);
    setUploadError("");
    setUploadBatch(null);
    setUploadItems(planned);
    setUploadDocument(null);
    setUploadStatus(`Preparing ${files.length} file(s)`);
    try {
      const batchItems = [];
      for (const [index, file] of files.entries()) {
        patchUploadItem(planned[index].id, { status: "hashing" });
        batchItems.push({
          filename: file.name,
          content_type: file.type || "application/octet-stream",
          size_bytes: file.size,
          checksum_sha256: await sha256Hex(file),
          parser_profile: "standard",
          metadata: { ui_upload: true },
        });
      }
      const batchResponse = await apiFetch("/api/v1/uploads/batches", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          knowledge_base_id: selectedKnowledgeBaseId || undefined,
          metadata: { ui_upload: true },
          items: batchItems,
        }),
      });
      if (!batchResponse.ok) throw new Error(await batchResponse.text());
      const batch = (await batchResponse.json()) as UploadBatchAccepted;
      setUploadStatus(`Created batch ${batch.batch_id}`);
      for (const [index, item] of batch.items.entries()) {
        const localId = planned[index].id;
        patchUploadItem(localId, {
          status: "uploading",
          upload_session_id: item.upload_session_id,
        });
        const putResponse = await fetch(item.upload_url, {
          method: "PUT",
          headers: item.required_headers,
          body: files[index],
        });
        if (!putResponse.ok) {
          patchUploadItem(localId, {
            status: "failed",
            error_message: await putResponse.text(),
          });
          continue;
        }
        patchUploadItem(localId, { status: "completing" });
        const completeResponse = await apiFetch(
          `/api/v1/uploads/sessions/${item.upload_session_id}:complete`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ metadata: { ui_upload: true } }),
          },
        );
        if (!completeResponse.ok) {
          patchUploadItem(localId, {
            status: "failed",
            error_message: await completeResponse.text(),
          });
          continue;
        }
        const completed =
          (await completeResponse.json()) as UploadCompleteResponse;
        patchUploadItem(localId, {
          status: "queued",
          document_id: completed.document_id,
          job_id: completed.job_id,
        });
      }
      setUploadStatus(`Queued batch ${batch.batch_id}`);
      void pollUploadBatch(batch.batch_id);
    } catch (error) {
      setUploadBusy(false);
      setUploadError(error instanceof Error ? error.message : String(error));
      setUploadStatus("");
    } finally {
      input.value = "";
    }
  }

  async function pollUploadBatch(batchId: string) {
    const response = await apiFetch(
      `/api/v1/uploads/batches/${encodeURIComponent(batchId)}`,
    );
    if (!response.ok) {
      setUploadBusy(false);
      setUploadError(await response.text());
      return;
    }
    const nextBatch = (await response.json()) as UploadBatchStatus;
    setUploadBatch(nextBatch);
    setUploadItems((items) =>
      items.map((item) => {
        const serverItem = nextBatch.items.find(
          (candidate) => candidate.upload_session_id === item.upload_session_id,
        );
        if (!serverItem) return item;
        return {
          ...item,
          filename: serverItem.filename,
          size_bytes: serverItem.size_bytes,
          status: serverItem.status,
          document_id: serverItem.document_id,
          job_id: serverItem.job_id,
          progress: serverItem.progress,
          error_code: serverItem.error_code,
          error_message: serverItem.error_message,
        };
      }),
    );
    setUploadStatus(
      `Batch ${nextBatch.status}: ${nextBatch.completed_items}/${nextBatch.total_items} complete`,
    );
    if (!["completed", "failed", "cancelled"].includes(nextBatch.status)) {
      window.setTimeout(() => pollUploadBatch(batchId), 1500);
      return;
    }
    setUploadBusy(false);
    const firstCompleted = nextBatch.items.find((item) => item.document_id);
    if (firstCompleted?.document_id) {
      const documentResponse = await apiFetch(
        `/api/v1/documents/${encodeURIComponent(firstCompleted.document_id)}`,
      );
      if (documentResponse.ok) {
        setUploadDocument(
          (await documentResponse.json()) as DocumentPublicMetadata,
        );
      }
    }
  }

  async function retryUploadItem(item: UploadItemState) {
    if (!item.job_id || !uploadBatch) return;
    patchUploadItem(item.id, { status: "resume_requested" });
    const response = await apiFetch(
      `/api/v1/ingestion-jobs/${item.job_id}:resume`,
      { method: "POST" },
    );
    if (!response.ok) {
      patchUploadItem(item.id, {
        status: "failed",
        error_message: await response.text(),
      });
      return;
    }
    setUploadBusy(true);
    void pollUploadBatch(uploadBatch.batch_id);
  }

  return (
    <main>
      <header className="app-header">
        <div>
          <h1>WikipediaRag</h1>
          <p>{t("product_tagline")}</p>
        </div>
        <div className="header-actions">
          <span
            className={`status ${ready}`}
            title={t("tooltip_readiness")}
            aria-label={`${t("status")}: ${ready}`}
          >
            <span className="status-dot" aria-hidden="true" />
            {ready === "ok"
              ? t("ready_ok")
              : ready === "offline"
                ? t("ready_offline")
                : t("ready_checking")}
            <HelpTooltip text={t("tooltip_readiness")} />
          </span>
          {session.authenticated ? (
            <>
              <span className="session-label">
                {session.user?.username ?? session.user?.id}
                {session.active_tenant_id ? "" : " · no tenant"}
              </span>
              <div
                className="locale-switch"
                aria-label={locale === "ru" ? "Язык" : "Language"}
              >
                {(["en", "ru"] as Locale[]).map((option) => (
                  <button
                    type="button"
                    key={option}
                    className={locale === option ? "selected" : ""}
                    aria-pressed={locale === option}
                    onClick={() => setLocale(option)}
                  >
                    {option.toUpperCase()}
                  </button>
                ))}
              </div>
              <button type="button" onClick={logout}>
                <LogOut size={16} /> {t("logout")}
              </button>
            </>
          ) : (
            <div
              className="locale-switch"
              aria-label={locale === "ru" ? "Язык" : "Language"}
            >
              {(["en", "ru"] as Locale[]).map((option) => (
                <button
                  type="button"
                  key={option}
                  className={locale === option ? "selected" : ""}
                  aria-pressed={locale === option}
                  onClick={() => setLocale(option)}
                >
                  {option.toUpperCase()}
                </button>
              ))}
            </div>
          )}
        </div>
      </header>

      {!session.authenticated && (
        <section className="band auth-band">
          <form className="auth-panel" onSubmit={localLogin}>
            <h2>
              <KeyRound size={18} /> {t("sign_in")}
            </h2>
            <label>
              {t("username")}
              <input
                value={authUsername}
                onChange={(event) => setAuthUsername(event.target.value)}
                placeholder={t("username")}
                autoComplete="username"
              />
            </label>
            <label>
              {t("password")}
              <input
                value={authPassword}
                onChange={(event) => setAuthPassword(event.target.value)}
                placeholder={t("password")}
                type="password"
                autoComplete="current-password"
              />
            </label>
            <div className="row">
              <button type="submit">
                <LogIn size={16} /> {t("local")}
              </button>
              <button type="button" onClick={oidcLogin}>
                <KeyRound size={16} /> {t("oidc")}
              </button>
            </div>
            {authError && (
              <p className="error" role="alert">
                {authError}
              </p>
            )}
          </form>
        </section>
      )}

      {session.authenticated && (
        <>
          <nav
            className="workspace-tabs"
            aria-label={locale === "ru" ? "Рабочая область" : "Workspace"}
            role="tablist"
          >
            {workspaceTabs.map((tab) => (
              <button
                key={tab.id}
                id={`tab-${tab.id}`}
                type="button"
                role="tab"
                aria-selected={activeTab === tab.id}
                aria-controls={`panel-${tab.id}`}
                tabIndex={activeTab === tab.id ? 0 : -1}
                className={activeTab === tab.id ? "selected" : ""}
                onClick={() => setActiveTab(tab.id)}
                onKeyDown={handleWorkspaceKeyDown}
              >
                {tab.label}
              </button>
            ))}
          </nav>
          <section className="band kb-toolbar">
            <label>
              {t("primary_kb")}
              <select
                aria-label={t("primary_kb")}
                value={selectedKnowledgeBaseId}
                onChange={(event) =>
                  setSelectedKnowledgeBaseId(event.target.value)
                }
              >
                {knowledgeBases.map((kb) => (
                  <option key={kb.id} value={kb.id}>
                    {kb.name}
                  </option>
                ))}
              </select>
            </label>
            <details className="scope-details">
              <summary>
                {interpolate(t("scope_summary"), {
                  count: selectedRetrievalKnowledgeBaseIds.length,
                })}
                <HelpTooltip text={t("tooltip_scope")} />
              </summary>
              <fieldset className="kb-scope">
                <legend>{t("retrieval_scope")}</legend>
                {knowledgeBases.map((kb) => (
                  <label key={kb.id}>
                    <input
                      type="checkbox"
                      aria-label={kb.name}
                      checked={selectedRetrievalKnowledgeBaseIds.includes(
                        kb.id,
                      )}
                      onChange={() => toggleRetrievalKnowledgeBase(kb.id)}
                    />
                    <span>{kb.name}</span>
                  </label>
                ))}
              </fieldset>
            </details>
            {activeTab === "knowledge" && (
              <form className="row kb-create" onSubmit={createKnowledgeBase}>
                <input
                  value={newKnowledgeBaseName}
                  onChange={(event) =>
                    setNewKnowledgeBaseName(event.target.value)
                  }
                  placeholder={t("new_kb_name")}
                  aria-label={t("new_kb_name")}
                />
                <button type="submit">
                  <Database size={16} /> {t("create")}
                </button>
              </form>
            )}
          </section>
        </>
      )}

      {session.authenticated && (
        <>
          <div
            id="panel-knowledge"
            className="tab-panel is-active"
            role="tabpanel"
            aria-labelledby="tab-knowledge"
            hidden={activeTab !== "knowledge"}
          >
            <section className="band grid">
              <div className="panel">
                <h2>
                  <Database size={18} /> {t("wikipedia_import")}
                </h2>
                <div className="row">
                  <input
                    type="number"
                    min={1}
                    max={10000}
                    value={limit}
                    onChange={(event) => setLimit(Number(event.target.value))}
                    aria-label={t("import_limit")}
                    title={t("import_limit")}
                  />
                  <button onClick={startImport}>
                    <Play size={16} /> {t("start_import")}
                  </button>
                </div>
                {job && (
                  <div className="progress">
                    <strong>{statusLabel(job.status, locale)}</strong>
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
                  <FileUp size={18} /> {t("upload")}
                </h2>
                <input
                  type="file"
                  accept=".txt,.md,.markdown,.html,.htm,.csv,.tsv,.json,.jsonl,.pdf,.docx,.pptx,.xlsx"
                  multiple
                  onChange={uploadFile}
                  disabled={uploadBusy}
                  aria-label={t("choose_files")}
                />
                {uploadStatus && (
                  <p className="upload-status">{uploadStatus}</p>
                )}
                {uploadBatch && (
                  <div className="progress upload-progress">
                    <strong>{uploadBatch.status}</strong>
                    <span>{uploadBatch.total_items} files</span>
                    <span>{uploadBatch.completed_items} completed</span>
                    <span>{uploadBatch.failed_items} failed</span>
                    <span>{uploadBatch.pending_items} pending</span>
                  </div>
                )}
                {uploadItems.length > 0 && (
                  <div className="upload-items">
                    {uploadItems.map((item) => (
                      <article key={item.id} className="upload-item">
                        <div>
                          <strong>{item.filename}</strong>
                          <span>{item.size_bytes} bytes</span>
                        </div>
                        <div>
                          <span>{statusLabel(item.status, locale)}</span>
                          <span>{item.progress?.stage ?? "waiting"}</span>
                          <span>
                            {item.progress?.parser_route ?? "parser pending"}
                          </span>
                          <span>
                            {item.progress?.chunks_published ?? 0} published
                          </span>
                        </div>
                        {(item.error_code || item.error_message) && (
                          <p className="error">
                            {item.error_code ?? item.error_message}
                          </p>
                        )}
                        {item.status === "failed" && item.job_id && (
                          <button
                            type="button"
                            onClick={() => void retryUploadItem(item)}
                          >
                            <RotateCw size={16} /> Retry
                          </button>
                        )}
                      </article>
                    ))}
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
                      <dd>
                        {metadataValue(uploadDocument, "detected_language")}
                      </dd>
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
                      <dd>
                        {formatTimestamp(uploadDocument.uploaded_at, locale)}
                      </dd>
                    </div>
                  </dl>
                )}
                {uploadError && <p className="error">{uploadError}</p>}
              </div>

              <div className="panel source-panel">
                <h2>
                  <Plug size={18} /> {t("sources")}
                </h2>
                <details className="source-create-details">
                  <summary>
                    <Plug size={15} /> {t("add_source")}
                  </summary>
                  <form className="source-form" onSubmit={createSource}>
                    <label>
                      {t("connector")}
                      <select
                        value={sourceKind}
                        onChange={(event) =>
                          selectSourceKind(event.target.value as SourceKind)
                        }
                      >
                        {SOURCE_KINDS.map((kind) => (
                          <option key={kind} value={kind}>
                            {sourceKindLabel(kind, locale)}
                          </option>
                        ))}
                      </select>
                    </label>
                    <label>
                      {t("source_name")}
                      <input
                        value={sourceName}
                        onChange={(event) => setSourceName(event.target.value)}
                        placeholder={t("source_name")}
                      />
                    </label>
                    <label>
                      {t("refresh_seconds")}
                      <input
                        type="number"
                        min={60}
                        value={sourceRefreshInterval}
                        onChange={(event) =>
                          setSourceRefreshInterval(event.target.value)
                        }
                      />
                    </label>
                    <details className="source-advanced source-wide">
                      <summary>{t("advanced_configuration")}</summary>
                      <label>
                        {t("config")}
                        <textarea
                          className="source-json"
                          value={sourceConfigText}
                          onChange={(event) =>
                            setSourceConfigText(event.target.value)
                          }
                          spellCheck={false}
                        />
                      </label>
                      <label>
                        {t("credentials")}
                        <textarea
                          className="source-json"
                          value={sourceCredentialsText}
                          onChange={(event) =>
                            setSourceCredentialsText(event.target.value)
                          }
                          spellCheck={false}
                        />
                      </label>
                    </details>
                    {canManageAccess && (
                      <details className="source-permissions source-wide">
                        <summary>{t("permissions")}</summary>
                        <AccessEditor
                          value={sourceAccess}
                          groups={accessGroups}
                          locale={locale}
                          disabled={sourceBusy.create}
                          onChange={setSourceAccess}
                        />
                      </details>
                    )}
                    <div className="source-wide row">
                      <button
                        type="submit"
                        disabled={!selectedKnowledgeBaseId || sourceBusy.create}
                      >
                        <Plug size={16} /> {t("add_source")}
                      </button>
                      <button
                        type="button"
                        onClick={() => void loadSources()}
                        disabled={!selectedKnowledgeBaseId || sourcesLoading}
                      >
                        <RotateCw size={16} /> {t("refresh")}
                      </button>
                    </div>
                  </form>
                </details>
                {(sourceStatus || sourcesLoading) && (
                  <p className="upload-status">
                    {sourcesLoading ? t("loading") : sourceStatus}
                  </p>
                )}
                {sourcesError && (
                  <p className="error" role="alert">
                    {sourcesError}
                  </p>
                )}
                <div className="source-list">
                  {sources.length === 0 && !sourcesLoading && (
                    <p className="empty-state">{t("no_sources")}</p>
                  )}
                  {sources.map((source) => {
                    const run = sourceRuns[source.id];
                    return (
                      <article key={source.id} className="source-item">
                        <div className="source-item-header">
                          <div>
                            <h3>{source.name}</h3>
                            <div className="search-meta">
                              <span>
                                {sourceKindLabel(source.kind, locale)}
                              </span>
                              <span>{statusLabel(source.status, locale)}</span>
                              <span>
                                {accessLabel(
                                  source.document_access_default,
                                  locale,
                                )}
                              </span>
                              <span>
                                {source.refresh_interval_seconds
                                  ? `${source.refresh_interval_seconds}s`
                                  : "manual"}
                              </span>
                            </div>
                          </div>
                          <span className={`source-badge ${source.status}`}>
                            {statusLabel(
                              source.last_sync_status ?? source.status,
                              locale,
                            )}
                          </span>
                        </div>
                        <dl className="source-meta">
                          <div>
                            <dt>{t("last_sync")}</dt>
                            <dd>
                              {formatTimestamp(source.last_synced_at, locale)}
                            </dd>
                          </div>
                          <div>
                            <dt>{t("next_sync")}</dt>
                            <dd>
                              {formatTimestamp(source.next_sync_at, locale)}
                            </dd>
                          </div>
                          <div>
                            <dt>{t("config")}</dt>
                            <dd>
                              {formatCompactJson(
                                redactSensitive(source.config),
                              )}
                            </dd>
                          </div>
                        </dl>
                        {canManageAccess && (
                          <details className="source-permissions">
                            <summary>{t("permissions")}</summary>
                            <AccessEditor
                              value={normalizeDocumentAccess(
                                source.document_access_default,
                              )}
                              groups={accessGroups}
                              locale={locale}
                              disabled={sourceBusy[`${source.id}:access`]}
                              saveLabel={t("apply_access")}
                              onSave={(access) =>
                                void patchSourceAccess(source, access)
                              }
                            />
                          </details>
                        )}
                        {run && (
                          <div className="source-run">
                            <ShieldCheck size={15} />
                            <span>{run.mode}</span>
                            <span>{statusLabel(run.status, locale)}</span>
                            <span>{formatCompactJson(run.stats)}</span>
                            {run.error_code && (
                              <span className="error">{run.error_code}</span>
                            )}
                          </div>
                        )}
                        <div className="source-actions">
                          <button
                            type="button"
                            title={t("tooltip_health")}
                            onClick={() => void healthcheckSource(source)}
                            disabled={sourceBusy[source.id]}
                          >
                            <ShieldCheck size={15} /> {t("health")}
                          </button>
                          <button
                            type="button"
                            title={t("tooltip_sync")}
                            onClick={() =>
                              void syncSource(source, "incremental")
                            }
                            disabled={
                              sourceBusy[source.id] ||
                              source.status === "disabled"
                            }
                          >
                            <RotateCw size={15} /> {t("sync")}
                          </button>
                          <button
                            type="button"
                            title={t("tooltip_full_sync")}
                            onClick={() => void syncSource(source, "full")}
                            disabled={
                              sourceBusy[source.id] ||
                              source.status === "disabled"
                            }
                          >
                            <RotateCw size={15} /> {t("full_sync")}
                          </button>
                          <button
                            type="button"
                            onClick={() => void patchSourceStatus(source)}
                            disabled={sourceBusy[source.id]}
                          >
                            {source.status === "disabled"
                              ? t("enable")
                              : t("disable")}
                          </button>
                        </div>
                      </article>
                    );
                  })}
                </div>
              </div>
            </section>
          </div>

          <div
            id="panel-search"
            className="tab-panel"
            role="tabpanel"
            aria-labelledby="tab-search"
            hidden={activeTab !== "search"}
          >
            <section className="band">
              <form className="search-panel" onSubmit={submitSearch}>
                <h2>
                  <Search size={18} /> {t("search")}
                </h2>
                <div className="search-query">
                  <input
                    value={searchQuery}
                    onChange={(event) => setSearchQuery(event.target.value)}
                    placeholder={t("search_documents")}
                    aria-label={t("search_documents")}
                  />
                  <button
                    type="submit"
                    disabled={
                      searchBusy ||
                      !searchQuery.trim() ||
                      searchKnowledgeBaseIds.length === 0
                    }
                  >
                    <Search size={16} /> {t("search")}
                  </button>
                </div>
                <details className="filter-details">
                  <summary>{t("filters")}</summary>
                  <div className="search-filters">
                    <label>
                      {t("document_type")}
                      <input
                        value={searchDocumentType}
                        onChange={(event) =>
                          setSearchDocumentType(event.target.value)
                        }
                        placeholder="pdf, text, html"
                      />
                    </label>
                    <label>
                      {t("language")}
                      <input
                        value={searchLanguage}
                        onChange={(event) =>
                          setSearchLanguage(event.target.value)
                        }
                        placeholder="ru"
                      />
                    </label>
                    <label>
                      {t("date_from")}
                      <input
                        type="date"
                        value={searchDateFrom}
                        onChange={(event) =>
                          setSearchDateFrom(event.target.value)
                        }
                      />
                    </label>
                    <label>
                      {t("date_to")}
                      <input
                        type="date"
                        value={searchDateTo}
                        onChange={(event) =>
                          setSearchDateTo(event.target.value)
                        }
                      />
                    </label>
                    <label>
                      {t("source")}
                      <input
                        value={searchSource}
                        onChange={(event) =>
                          setSearchSource(event.target.value)
                        }
                        placeholder="upload, wikipedia, url"
                      />
                    </label>
                  </div>
                </details>
              </form>

              <div className="search-results">
                {searchError && (
                  <p className="error" role="alert">
                    {searchError}
                  </p>
                )}
                {searchFacets.length > 0 && (
                  <div className="search-facets">
                    {searchFacets.map((facet) => (
                      <div key={facet.field}>
                        <strong>{formatFacetName(facet.field)}</strong>
                        <span>
                          {facet.buckets
                            .slice(0, 4)
                            .map(
                              (bucket) => `${bucket.value} (${bucket.count})`,
                            )
                            .join(", ")}
                        </span>
                      </div>
                    ))}
                  </div>
                )}
                {!searchBusy &&
                  !searchError &&
                  searchQuery &&
                  searchResults.length === 0 && (
                    <p className="empty-state">{t("no_results")}</p>
                  )}
                {searchResults.map((item, index) => (
                  <article
                    key={`${item.chunk_id}-${index}`}
                    className="search-result"
                  >
                    <div>
                      <h3>{item.title}</h3>
                      <p>
                        {item.highlights?.[0]?.fragments?.[0] ?? item.snippet}
                      </p>
                    </div>
                    <div className="search-meta">
                      <span>
                        {knowledgeBaseName(
                          knowledgeBases,
                          item.knowledge_base_id,
                        )}
                      </span>
                      <span>
                        {item.section_path.join(" / ") || "No section"}
                      </span>
                      <span>{item.document_date ?? "No date"}</span>
                      <span>{item.document_type ?? item.source_type}</span>
                      <span>{item.language ?? "No language"}</span>
                      <span>{formatScore(item.score)}</span>
                    </div>
                    <div className="search-actions">
                      <button
                        type="button"
                        onClick={() => void openDocumentViewer(item)}
                      >
                        <BookOpen size={15} />{" "}
                        {locale === "ru"
                          ? "Открыть просмотр"
                          : "Open in viewer"}
                      </button>
                      {item.source_url ? (
                        <a
                          href={item.source_url}
                          target="_blank"
                          rel="noreferrer"
                        >
                          <ExternalLink size={15} /> {t("source")}
                        </a>
                      ) : (
                        <span>Document {item.document_id}</span>
                      )}
                    </div>
                  </article>
                ))}
                {searchHasMore && (
                  <button
                    type="button"
                    onClick={loadMoreSearch}
                    disabled={searchBusy}
                  >
                    {searchBusy
                      ? t("loading")
                      : locale === "ru"
                        ? "Загрузить ещё"
                        : "Load more"}
                  </button>
                )}
              </div>
              {viewerStructure && (
                <DocumentViewer
                  structure={viewerStructure}
                  locale={locale}
                  context={viewerContext}
                  searchQuery={viewerSearchQuery}
                  searchResults={viewerSearchResults}
                  busy={viewerBusy}
                  searchBusy={viewerSearchBusy}
                  error={viewerError}
                  accessGroups={accessGroups}
                  canManageAccess={canManageAccess}
                  accessBusy={viewerAccessBusy}
                  onClose={closeDocumentViewer}
                  onUpdateAccess={(access) => void patchViewerAccess(access)}
                  onSearchQueryChange={setViewerSearchQuery}
                  onSearch={submitDocumentSearch}
                  onOpenChunk={(chunkId) =>
                    void loadDocumentContext(viewerStructure.document_id, {
                      chunkId,
                    })
                  }
                  onOpenSection={(sectionId) =>
                    void loadDocumentContext(viewerStructure.document_id, {
                      sectionId,
                    })
                  }
                />
              )}
            </section>
          </div>

          <div
            id="panel-research"
            className="tab-panel"
            role="tabpanel"
            aria-labelledby="tab-research"
            hidden={activeTab !== "research"}
          >
            <section className="band research-panel">
              <form className="research-create" onSubmit={submitResearchRun}>
                <h2>
                  <Database size={18} /> {t("deep_research")}
                </h2>
                <textarea
                  value={researchTopic}
                  onChange={(event) => setResearchTopic(event.target.value)}
                  placeholder={t("research_topic")}
                  aria-label={t("research_topic")}
                />
                <fieldset className="research-kb-scope">
                  <legend>Knowledge bases (up to 3)</legend>
                  {knowledgeBases.map((item) => (
                    <label key={item.id}>
                      <input
                        type="checkbox"
                        checked={researchKnowledgeBaseIds.includes(item.id)}
                        onChange={() =>
                          setResearchKnowledgeBaseIds((current) =>
                            current.includes(item.id)
                              ? current.filter((id) => id !== item.id)
                              : current.length < 3
                                ? [...current, item.id]
                                : current,
                          )
                        }
                      />
                      {item.name}
                    </label>
                  ))}
                </fieldset>
                <div className="row">
                  <button
                    type="submit"
                    disabled={
                      researchBusy ||
                      !researchTopic.trim() ||
                      !selectedKnowledgeBaseId
                    }
                  >
                    <Play size={16} /> {t("quick_run")}
                  </button>
                  <button
                    type="button"
                    disabled={
                      researchBusy ||
                      !researchTopic.trim() ||
                      !selectedKnowledgeBaseId
                    }
                    onClick={() => void createResearchPlanFromDraft()}
                  >
                    <Database size={16} /> {t("create_plan")}
                  </button>
                  <button
                    type="button"
                    onClick={() => {
                      void loadResearchPlans();
                      void loadResearchRuns();
                    }}
                  >
                    <RotateCw size={16} /> {t("refresh")}
                  </button>
                  <span>
                    {t("single_kb")}:{" "}
                    {knowledgeBases.find(
                      (item) => item.id === selectedKnowledgeBaseId,
                    )?.name ?? t("not_selected")}
                  </span>
                </div>
                {researchError && (
                  <p className="error" role="alert">
                    {researchError}
                  </p>
                )}
              </form>

              <div className="research-layout">
                <aside className="research-sidebar">
                  <div className="research-runs">
                    <h3>{t("plans")}</h3>
                    {researchPlans.length === 0 && <p>{t("no_plans")}</p>}
                    {researchPlans.map((plan) => (
                      <button
                        type="button"
                        key={plan.id}
                        className={
                          researchPlanDetail?.plan.id === plan.id
                            ? "selected"
                            : ""
                        }
                        onClick={() => void loadResearchPlanDetail(plan.id)}
                      >
                        <strong>{statusLabel(plan.status, locale)}</strong>
                        <span>{plan.topic}</span>
                        <code>{plan.id.slice(0, 8)}</code>
                      </button>
                    ))}
                  </div>

                  <div className="research-runs">
                    <h3>{t("runs")}</h3>
                    {researchRuns.length === 0 && <p>{t("no_runs")}</p>}
                    {researchRuns.map((run) => (
                      <button
                        type="button"
                        key={run.id}
                        className={
                          researchDetail?.run.id === run.id ? "selected" : ""
                        }
                        onClick={() => void loadResearchRunDetail(run.id)}
                      >
                        <strong>{statusLabel(run.status, locale)}</strong>
                        <span>{run.topic}</span>
                        <code>{run.id.slice(0, 8)}</code>
                      </button>
                    ))}
                  </div>
                </aside>

                {researchPlanDetail && (
                  <article className="research-detail">
                    <div className="research-header">
                      <div>
                        <h3>{researchPlanDetail.plan.topic}</h3>
                        <p>
                          {statusLabel(researchPlanDetail.plan.status, locale)}{" "}
                          · {researchPlanDetail.plan.retrieval_profile}
                        </p>
                      </div>
                      <div className="row">
                        <button
                          type="button"
                          disabled={
                            researchBusy ||
                            researchPlanDetail.plan.status !== "draft"
                          }
                          onClick={() => void saveResearchPlan()}
                        >
                          {t("save_plan")}
                        </button>
                        <button
                          type="button"
                          disabled={
                            researchBusy ||
                            researchPlanDetail.plan.status !== "draft"
                          }
                          onClick={() =>
                            void approveResearchPlan(researchPlanDetail.plan.id)
                          }
                        >
                          {t("approve_run")}
                        </button>
                      </div>
                    </div>
                    <div className="research-metrics">
                      <span>
                        {researchPlanDetail.questions.length} {t("questions")}
                      </span>
                      <span>{researchPlanDetail.plan.tool_mode}</span>
                      {researchPlanDetail.plan.approved_run_id && (
                        <span>
                          {t("runs")}{" "}
                          {researchPlanDetail.plan.approved_run_id.slice(0, 8)}
                        </span>
                      )}
                    </div>
                    <section>
                      <label
                        className="field-label"
                        htmlFor="research-plan-topic"
                      >
                        {t("plan_topic")}
                      </label>
                      <textarea
                        id="research-plan-topic"
                        value={researchPlanTopicDraft}
                        aria-label={t("plan_topic")}
                        disabled={researchPlanDetail.plan.status !== "draft"}
                        onChange={(event) =>
                          setResearchPlanTopicDraft(event.target.value)
                        }
                      />
                    </section>
                    <section>
                      <label
                        className="field-label"
                        htmlFor="research-plan-questions"
                      >
                        {t("plan_questions")}
                      </label>
                      <textarea
                        id="research-plan-questions"
                        value={researchPlanQuestionsDraft}
                        aria-label={t("plan_questions")}
                        disabled={researchPlanDetail.plan.status !== "draft"}
                        onChange={(event) =>
                          setResearchPlanQuestionsDraft(event.target.value)
                        }
                      />
                    </section>
                    <section>
                      <label
                        className="field-label"
                        htmlFor="research-plan-notes"
                      >
                        {t("notes")}
                      </label>
                      <textarea
                        id="research-plan-notes"
                        value={researchPlanNotesDraft}
                        aria-label={t("notes")}
                        disabled={researchPlanDetail.plan.status !== "draft"}
                        onChange={(event) =>
                          setResearchPlanNotesDraft(event.target.value)
                        }
                      />
                    </section>
                  </article>
                )}

                {researchDetail && (
                  <article className="research-detail">
                    <div className="research-header">
                      <div>
                        <h3>{researchDetail.run.topic}</h3>
                        <p>
                          {statusLabel(researchDetail.run.status, locale)} ·{" "}
                          {String(researchDetail.run.progress?.stage ?? "")}
                        </p>
                      </div>
                      <div className="row">
                        <button
                          type="button"
                          disabled={
                            researchBusy ||
                            !["received", "running"].includes(
                              researchDetail.run.status,
                            )
                          }
                          onClick={() =>
                            void researchRunAction(
                              researchDetail.run.id,
                              "pause",
                            )
                          }
                        >
                          {t("pause")}
                        </button>
                        <button
                          type="button"
                          disabled={
                            researchBusy ||
                            researchDetail.run.status !== "paused"
                          }
                          onClick={() =>
                            void researchRunAction(
                              researchDetail.run.id,
                              "resume",
                            )
                          }
                        >
                          {t("resume")}
                        </button>
                        <button
                          type="button"
                          disabled={
                            researchBusy ||
                            !["received", "running", "paused"].includes(
                              researchDetail.run.status,
                            )
                          }
                          onClick={() =>
                            void researchRunAction(
                              researchDetail.run.id,
                              "cancel",
                            )
                          }
                        >
                          {t("cancel")}
                        </button>
                        <button
                          type="button"
                          disabled={
                            !researchDetail.episodes.some(
                              (episode) => episode.query_run_id,
                            )
                          }
                          onClick={() =>
                            void openResearchDebugger(researchDetail)
                          }
                        >
                          <Bug size={16} /> {t("debug")}
                        </button>
                      </div>
                    </div>

                    <div className="research-metrics">
                      <span>
                        {t("coverage")}{" "}
                        {researchDetail.final_report.coverage?.covered ?? 0}/
                        {researchDetail.final_report.coverage?.total ?? 0}
                      </span>
                      <span>
                        {researchDetail.evidence.length} {t("evidence_memory")}
                      </span>
                      <span>
                        {researchDetail.episodes.length} {t("episodes")}
                      </span>
                      {researchDetail.final_report.partial_terminal && (
                        <span>{t("partial_terminal")}</span>
                      )}
                    </div>

                    <div className="research-columns">
                      <section>
                        <h4>{t("coverage")}</h4>
                        {researchDetail.coverage.map((item) => {
                          const question = researchDetail.questions.find(
                            (row) => row.id === item.question_id,
                          );
                          return (
                            <div className="research-card" key={item.id}>
                              <strong>
                                {statusLabel(item.status, locale)}
                              </strong>
                              <p>{question?.question ?? item.question_id}</p>
                              <code>{item.reason}</code>
                            </div>
                          );
                        })}
                      </section>
                      <section>
                        <h4>{t("evidence_memory")}</h4>
                        {researchDetail.evidence.slice(0, 8).map((item) => (
                          <div
                            className="research-card"
                            id={`evidence-${item.evidence_ref}`}
                            key={item.id}
                          >
                            <strong>
                              [{item.evidence_ref}] {item.title}
                            </strong>
                            <p>{item.content_abstract}</p>
                            <a
                              href={item.source_url}
                              target="_blank"
                              rel="noreferrer"
                            >
                              Source
                            </a>
                          </div>
                        ))}
                      </section>
                    </div>

                    {researchDetail.reflections.length > 0 && (
                      <section>
                        <h4>{t("latest_reflection")}</h4>
                        <p>
                          {
                            researchDetail.reflections[
                              researchDetail.reflections.length - 1
                            ].body
                          }
                        </p>
                      </section>
                    )}

                    {researchDetail.final_report.failure_taxonomy && (
                      <section>
                        <h4>{t("failure_taxonomy")}</h4>
                        <pre>
                          {JSON.stringify(
                            researchDetail.final_report.failure_taxonomy,
                            null,
                            2,
                          )}
                        </pre>
                      </section>
                    )}

                    {researchDetail.final_report.markdown && (
                      <section>
                        <h4>{t("report")}</h4>
                        <div className="research-report-actions">
                          <button
                            type="button"
                            onClick={() =>
                              void downloadResearchFile("markdown")
                            }
                          >
                            Markdown
                          </button>
                          <button
                            type="button"
                            onClick={() => void downloadResearchFile("docx")}
                          >
                            Word
                          </button>
                          <button
                            type="button"
                            onClick={() => void downloadResearchFile("csv")}
                          >
                            CSV
                          </button>
                        </div>
                        {researchDetail.final_report.sections ? (
                          <div className="structured-report">
                            {(
                              [
                                "confirmed_findings",
                                "partial_conflicting_findings",
                              ] as const
                            ).map((key) => (
                              <section key={key}>
                                <h5>
                                  {key === "confirmed_findings"
                                    ? "Confirmed findings"
                                    : "Partial / conflicting findings"}
                                </h5>
                                {(
                                  researchDetail.final_report.sections?.[key] ??
                                  []
                                ).map((finding, index) => (
                                  <p key={`${key}-${index}`}>
                                    {finding.text}{" "}
                                    {(finding.evidence_refs ?? []).map(
                                      (ref) => (
                                        <button
                                          type="button"
                                          className="citation-button"
                                          key={ref}
                                          onClick={() =>
                                            document
                                              .getElementById(`evidence-${ref}`)
                                              ?.scrollIntoView({
                                                behavior: "smooth",
                                                block: "center",
                                              })
                                          }
                                        >
                                          {ref}
                                        </button>
                                      ),
                                    )}
                                  </p>
                                ))}
                              </section>
                            ))}
                            <section>
                              <h5>Unresolved questions</h5>
                              {(
                                researchDetail.final_report.sections
                                  .unresolved_questions ?? []
                              ).map((item) => (
                                <p key={item}>{item}</p>
                              ))}
                            </section>
                            <section>
                              <h5>Limitations</h5>
                              {(
                                researchDetail.final_report.sections
                                  .limitations ?? []
                              ).map((item) => (
                                <p key={item}>{item}</p>
                              ))}
                            </section>
                          </div>
                        ) : (
                          <pre>{researchDetail.final_report.markdown}</pre>
                        )}
                      </section>
                    )}
                  </article>
                )}
              </div>
            </section>
          </div>

          <div
            id="panel-chat"
            className="tab-panel"
            role="tabpanel"
            aria-labelledby="tab-chat"
            hidden={activeTab !== "chat"}
          >
            <section className="band">
              <form className="chat" onSubmit={submitChat}>
                <h2>
                  <MessageSquare size={18} /> {t("chat")}
                </h2>
                <textarea
                  value={question}
                  onChange={(event) => setQuestion(event.target.value)}
                  aria-label={t("chat")}
                />
                <details className="advanced">
                  <summary>
                    <SlidersHorizontal size={16} /> {t("advanced_retrieval")}
                  </summary>
                  <div className="advanced-grid">
                    <label>
                      {t("profile")}
                      <HelpTooltip text={t("tooltip_profile")} />
                      <select
                        aria-label={t("profile")}
                        value={retrievalProfile}
                        onChange={(event) =>
                          setRetrievalProfile(event.target.value)
                        }
                      >
                        <option value="sota_mvp">sota_mvp</option>
                        <option value="test_mock">test_mock</option>
                        <option value="upload_mock">upload_mock</option>
                        <option value="upload_sota_mvp">upload_sota_mvp</option>
                        <option value="bm25_only">bm25_only</option>
                        <option value="rewrite_off">rewrite_off</option>
                        <option value="parent_expansion_off">
                          parent_expansion_off
                        </option>
                      </select>
                    </label>
                    <label>
                      {t("top_k")}
                      <HelpTooltip text={t("tooltip_top_k")} />
                      <input
                        type="number"
                        min={1}
                        max={50}
                        value={debugTopK}
                        aria-label={t("top_k")}
                        onChange={(event) =>
                          setDebugTopK(Number(event.target.value))
                        }
                      />
                    </label>
                    <label className="checkbox">
                      <input
                        type="checkbox"
                        aria-label={t("bm25")}
                        checked={bm25Enabled}
                        onChange={(event) =>
                          setBm25Enabled(event.target.checked)
                        }
                      />
                      {t("bm25")}
                    </label>
                    <label className="checkbox">
                      <input
                        type="checkbox"
                        aria-label={t("dense")}
                        checked={denseEnabled}
                        onChange={(event) =>
                          setDenseEnabled(event.target.checked)
                        }
                      />
                      {t("dense")}
                    </label>
                    <label className="checkbox">
                      <input
                        type="checkbox"
                        aria-label={t("rerank")}
                        checked={rerankEnabled}
                        onChange={(event) =>
                          setRerankEnabled(event.target.checked)
                        }
                      />
                      {t("rerank")}
                    </label>
                    <label>
                      {t("fusion")}
                      <HelpTooltip text={t("tooltip_fusion")} />
                      <select
                        aria-label={t("fusion")}
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
                      {t("parent_expansion")}
                      <HelpTooltip text={t("tooltip_parent")} />
                      <select
                        aria-label={t("parent_expansion")}
                        value={parentExpansion}
                        onChange={(event) =>
                          setParentExpansion(
                            event.target.value as
                              | "off"
                              | "selective"
                              | "always",
                          )
                        }
                      >
                        <option value="selective">selective</option>
                        <option value="off">off</option>
                        <option value="always">always</option>
                      </select>
                    </label>
                    <label>
                      {t("extended_search")}
                      <HelpTooltip text={t("tooltip_extended")} />
                      <select
                        aria-label={t("extended_search")}
                        value={extendedSearchMode}
                        onChange={(event) =>
                          setExtendedSearchMode(
                            event.target.value as
                              | "off"
                              | "conditional"
                              | "always",
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
                  <label className="compact-field">
                    {t("mode")}
                    <select
                      aria-label={t("mode")}
                      value={mode}
                      onChange={(event) =>
                        setMode(event.target.value as "normal" | "extended")
                      }
                    >
                      <option value="normal">{t("normal")}</option>
                      <option value="extended">{t("extended")}</option>
                    </select>
                  </label>
                  <button type="submit" disabled={chatBusy}>
                    <Search size={16} /> {chatBusy ? t("loading") : t("ask")}
                  </button>
                  {chatBusy && (
                    <button type="button" onClick={stopChat}>
                      <X size={16} /> {t("chat_stop")}
                    </button>
                  )}
                  <button
                    type="button"
                    onClick={loadDebugger}
                    disabled={!queryRunId}
                    title={t("tooltip_debug")}
                  >
                    <Bug size={16} /> {t("debug")}
                  </button>
                  <button
                    type="button"
                    onClick={() => window.location.reload()}
                    aria-label={t("reset_app")}
                    title={t("tooltip_reload")}
                  >
                    <RotateCw size={16} />
                  </button>
                </div>
                {chatBusy && (
                  <p className="status" role="status" aria-live="polite">
                    {t("chat_waiting")}
                  </p>
                )}
                {chatError && (
                  <p className="error" role="alert" aria-live="assertive">
                    {chatError}
                  </p>
                )}
              </form>

              {answer && (
                <div className="answer">
                  <h2>{t("answer")}</h2>
                  <p>{answer}</p>
                  <h3>{t("sources")}</h3>
                  <div className="sources">
                    {evidence.map((item) => (
                      <article key={item.evidence_id}>
                        <a
                          href={item.source_url}
                          target="_blank"
                          rel="noreferrer"
                        >
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
                  <h2>{t("debug")} · Retrieval</h2>
                  <RetrievalDebugger run={debuggerRun} events={events} />
                </div>
              )}
            </section>
          </div>
        </>
      )}
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

function buildSearchFilters(values: {
  documentType: string;
  language: string;
  dateFrom: string;
  dateTo: string;
  source: string;
}): SearchFilters {
  const filters: SearchFilters = {};
  if (values.documentType.trim()) {
    filters.document_type = values.documentType.trim();
  }
  if (values.language.trim()) {
    filters.language = values.language.trim();
  }
  if (values.dateFrom) {
    filters.date_from = values.dateFrom;
  }
  if (values.dateTo) {
    filters.date_to = values.dateTo;
  }
  if (values.source.trim()) {
    filters.source = values.source.trim();
  }
  return filters;
}

function knowledgeBaseName(
  knowledgeBases: KnowledgeBase[],
  id: string,
): string {
  return knowledgeBases.find((kb) => kb.id === id)?.name ?? id;
}

type Translate = (key: string, fallback?: string) => string;

function safeErrorCodePayload(raw: string): { code?: string } {
  try {
    const parsed: unknown = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object") return {};
    const root = parsed as Record<string, unknown>;
    const direct = root.error;
    const detail = root.detail;
    const envelope =
      direct && typeof direct === "object"
        ? direct
        : detail && typeof detail === "object"
          ? (detail as Record<string, unknown>).error
          : undefined;
    if (!envelope || typeof envelope !== "object") return {};
    const code = (envelope as Record<string, unknown>).code;
    return typeof code === "string" && code ? { code } : {};
  } catch {
    return {};
  }
}

function localizedError(
  code: string | undefined,
  translate: Translate,
  fallbackKey: string,
): string {
  const key =
    code === "KB_NOT_READY"
      ? fallbackKey
      : code === "CONFLICT"
        ? "conflict"
        : code === "REQUEST_VALIDATION_FAILED"
          ? "request_validation_failed"
          : fallbackKey;
  const message = translate(key);
  if (!code) return message;
  return `${message} (${translate("error_code")}: ${code})`;
}

async function responseErrorMessage(
  response: Response,
  translate: Translate,
  fallbackKey: string,
): Promise<string> {
  const raw = await response.text();
  const { code } = safeErrorCodePayload(raw);
  return localizedError(code, translate, fallbackKey);
}

function sseFailureMessage(payload: SsePayload, translate: Translate): string {
  const code = payload.data?.code;
  return localizedError(
    typeof code === "string" ? code : undefined,
    translate,
    "chat_failed",
  );
}

function formatFacetName(field: string) {
  const labels: Record<string, string> = {
    source_type: "Source",
    document_type: "Type",
    language: "Language",
    knowledge_base_id: "KB",
  };
  return labels[field] ?? field;
}

function formatScore(value: number) {
  if (!Number.isFinite(value)) return "score 0";
  return `score ${value.toFixed(value >= 10 ? 0 : 3)}`;
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

function formatTimestamp(
  value: string | null | undefined,
  locale: Locale = "en",
) {
  if (!value) return "";
  return new Intl.DateTimeFormat(locale === "ru" ? "ru-RU" : "en-US", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

function formatJson(value: Record<string, unknown>) {
  return JSON.stringify(value, null, 2);
}

function formatCompactJson(value: Record<string, unknown>) {
  const text = JSON.stringify(value);
  if (!text || text === "{}") return "";
  return text.length > 140 ? `${text.slice(0, 137)}...` : text;
}

function redactSensitive(
  value: Record<string, unknown>,
): Record<string, unknown> {
  const secretTokens = ["secret", "password", "token", "cookie", "credential"];
  return Object.fromEntries(
    Object.entries(value).map(([key, item]) => {
      if (secretTokens.some((token) => key.toLowerCase().includes(token))) {
        return [key, "<redacted>"];
      }
      if (typeof item === "object" && item !== null && !Array.isArray(item)) {
        return [key, redactSensitive(item as Record<string, unknown>)];
      }
      return [key, item];
    }),
  );
}

function parseJsonObject(label: string, text: string): Record<string, unknown> {
  const trimmed = text.trim();
  if (!trimmed) return {};
  const parsed: unknown = JSON.parse(trimmed);
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    throw new Error(`${label} must be a JSON object`);
  }
  return parsed as Record<string, unknown>;
}

function sourceKindLabel(kind: string, locale: Locale = "en") {
  if (!SOURCE_KINDS.includes(kind as SourceKind)) return kind;
  if (locale === "ru") {
    const labels: Record<SourceKind, string> = {
      confluence_dc: "Confluence DC",
      jira_dc: "Jira DC",
      gitlab_self_managed: "GitLab Self-Managed",
      kiwix_zim: "Kiwix/ZIM",
      local_folder: "Локальная папка",
      internal_crawler: "Внутренний crawler",
      sunduk_mock: "Sunduk Mock",
      docsmart_mock: "DocSmart Mock",
    };
    return labels[kind as SourceKind];
  }
  return SOURCE_TEMPLATES[kind as SourceKind].label;
}

function statusLabel(value: string | null | undefined, locale: Locale = "en") {
  const status = value ?? "";
  if (locale !== "ru") return status;
  const labels: Record<string, string> = {
    active: "активен",
    approved: "утверждён",
    cancelled: "отменён",
    completed: "завершён",
    completed_partial: "завершён частично",
    disabled: "отключён",
    failed: "ошибка",
    paused: "на паузе",
    queued: "в очереди",
    received: "получен",
    running: "выполняется",
    pending: "ожидает",
    partial: "частично",
  };
  return labels[status] ?? status;
}

function normalizeDocumentAccess(raw: unknown): DocumentAccess {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    return DEFAULT_DOCUMENT_ACCESS;
  }
  const value = raw as Record<string, unknown>;
  const policy =
    value.policy === "tenant" || value.policy === "restricted"
      ? value.policy
      : "kb";
  return {
    policy,
    user_ids: stringList(value.user_ids),
    group_ids: stringList(value.group_ids),
  };
}

function stringList(raw: unknown): string[] {
  if (!Array.isArray(raw)) return [];
  return raw.map((item) => String(item).trim()).filter(Boolean);
}

function parseIdList(text: string): string[] {
  return text
    .split(/[\s,]+/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function accessLabel(
  access: DocumentAccess | undefined,
  locale: Locale = "en",
) {
  const normalized = normalizeDocumentAccess(access);
  if (normalized.policy === "tenant")
    return locale === "ru" ? "Тенант" : "Tenant";
  if (normalized.policy === "restricted")
    return locale === "ru" ? "Ограниченный" : "Restricted";
  return locale === "ru" ? "База" : "KB";
}

function AccessEditor({
  value,
  groups,
  locale = "en",
  disabled,
  saveLabel,
  onChange,
  onSave,
}: {
  value: DocumentAccess;
  groups: AccessGroup[];
  locale?: Locale;
  disabled?: boolean;
  saveLabel?: string;
  onChange?: (access: DocumentAccess) => void;
  onSave?: (access: DocumentAccess) => void;
}) {
  const [policy, setPolicy] = useState<DocumentAccessPolicy>(
    normalizeDocumentAccess(value).policy,
  );
  const [userIdsText, setUserIdsText] = useState(
    normalizeDocumentAccess(value).user_ids.join(", "),
  );
  const [groupIds, setGroupIds] = useState<string[]>(
    normalizeDocumentAccess(value).group_ids,
  );

  useEffect(() => {
    const normalized = normalizeDocumentAccess(value);
    setPolicy(normalized.policy);
    setUserIdsText(normalized.user_ids.join(", "));
    setGroupIds(normalized.group_ids);
  }, [value]);

  const currentAccess = (): DocumentAccess => {
    if (policy !== "restricted") {
      return { policy, user_ids: [], group_ids: [] };
    }
    return {
      policy,
      user_ids: parseIdList(userIdsText),
      group_ids: groupIds,
    };
  };

  function emit(
    nextPolicy: DocumentAccessPolicy,
    nextUserIdsText = userIdsText,
    nextGroupIds = groupIds,
  ) {
    const nextAccess =
      nextPolicy === "restricted"
        ? {
            policy: nextPolicy,
            user_ids: parseIdList(nextUserIdsText),
            group_ids: nextGroupIds,
          }
        : { policy: nextPolicy, user_ids: [], group_ids: [] };
    onChange?.(nextAccess);
  }

  return (
    <div className="access-editor">
      <label>
        {locale === "ru" ? "Видимость" : "Visibility"}
        <select
          value={policy}
          disabled={disabled}
          onChange={(event) => {
            const nextPolicy = event.target.value as DocumentAccessPolicy;
            setPolicy(nextPolicy);
            emit(nextPolicy);
          }}
        >
          <option value="kb">KB</option>
          <option value="tenant">
            {locale === "ru" ? "Тенант" : "Tenant"}
          </option>
          <option value="restricted">
            {locale === "ru" ? "Ограниченный" : "Restricted"}
          </option>
        </select>
      </label>
      {policy === "restricted" && (
        <>
          <label>
            {locale === "ru" ? "Группы" : "Groups"}
            <select
              multiple
              value={groupIds}
              disabled={disabled}
              onChange={(event) => {
                const nextGroupIds = Array.from(
                  event.currentTarget.selectedOptions,
                ).map((option) => option.value);
                setGroupIds(nextGroupIds);
                emit(policy, userIdsText, nextGroupIds);
              }}
            >
              {groups.map((group) => (
                <option key={group.id} value={group.id}>
                  {group.name} ({group.group_type})
                </option>
              ))}
            </select>
          </label>
          <label>
            {locale === "ru" ? "ID пользователей" : "User IDs"}
            <input
              value={userIdsText}
              disabled={disabled}
              onChange={(event) => {
                setUserIdsText(event.target.value);
                emit(policy, event.target.value, groupIds);
              }}
              placeholder={
                locale === "ru"
                  ? "user-id, другой-user-id"
                  : "user-id, another-user-id"
              }
            />
          </label>
        </>
      )}
      {onSave && (
        <button
          type="button"
          disabled={disabled}
          onClick={() => onSave(currentAccess())}
        >
          <ShieldCheck size={15} />{" "}
          {saveLabel ?? (locale === "ru" ? "Сохранить доступ" : "Save access")}
        </button>
      )}
    </div>
  );
}

function DocumentViewer({
  structure,
  locale,
  context,
  searchQuery,
  searchResults,
  busy,
  searchBusy,
  error,
  accessGroups,
  canManageAccess,
  accessBusy,
  onClose,
  onUpdateAccess,
  onSearchQueryChange,
  onSearch,
  onOpenChunk,
  onOpenSection,
}: {
  structure: DocumentStructure;
  locale: Locale;
  context: DocumentContextResponse | null;
  searchQuery: string;
  searchResults: DocumentSearchResult[];
  busy: boolean;
  searchBusy: boolean;
  error: string;
  accessGroups: AccessGroup[];
  canManageAccess: boolean;
  accessBusy: boolean;
  onClose: () => void;
  onUpdateAccess: (access: DocumentAccess) => void;
  onSearchQueryChange: (value: string) => void;
  onSearch: (event: FormEvent) => void;
  onOpenChunk: (chunkId: string) => void;
  onOpenSection: (sectionId: string) => void;
}) {
  return (
    <section className="document-viewer" aria-live="polite">
      <div className="document-viewer-header">
        <div>
          <h2>
            <BookOpen size={18} /> {structure.title}
          </h2>
          <div className="search-meta">
            <span>{structure.source_type}</span>
            <span>
              {structure.sections.length}{" "}
              {locale === "ru" ? "разделов" : "sections"}
            </span>
            {structure.document_version_id && (
              <span>{structure.document_version_id}</span>
            )}
            <span>{accessLabel(structure.document_access, locale)}</span>
            {structure.document_access_origin && (
              <span>{structure.document_access_origin}</span>
            )}
          </div>
        </div>
        <div className="row">
          {structure.source_url && (
            <a href={structure.source_url} target="_blank" rel="noreferrer">
              <ExternalLink size={15} />{" "}
              {locale === "ru" ? "Источник" : "Source"}
            </a>
          )}
          <button type="button" onClick={onClose}>
            <X size={16} /> {locale === "ru" ? "Закрыть" : "Close"}
          </button>
        </div>
      </div>
      {canManageAccess && (
        <AccessEditor
          value={normalizeDocumentAccess(structure.document_access)}
          groups={accessGroups}
          locale={locale}
          disabled={accessBusy}
          saveLabel={
            locale === "ru"
              ? "Сохранить доступ к документу"
              : "Save document access"
          }
          onSave={onUpdateAccess}
        />
      )}
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      <div className="document-viewer-grid">
        <nav className="document-toc">
          {structure.sections.map((section) => (
            <button
              key={section.section_id}
              type="button"
              style={{
                paddingLeft: `${Math.max(0, section.level - 1) * 14 + 12}px`,
              }}
              onClick={() => onOpenSection(section.section_id)}
            >
              {section.title}
            </button>
          ))}
        </nav>
        <div className="document-main">
          <form className="document-search" onSubmit={onSearch}>
            <input
              value={searchQuery}
              onChange={(event) => onSearchQueryChange(event.target.value)}
              placeholder={
                locale === "ru"
                  ? "Поиск внутри этого документа"
                  : "Search inside this document"
              }
              aria-label={
                locale === "ru"
                  ? "Поиск внутри этого документа"
                  : "Search inside this document"
              }
            />
            <button type="submit" disabled={searchBusy || !searchQuery.trim()}>
              <Search size={15} /> {locale === "ru" ? "Поиск" : "Search"}
            </button>
          </form>
          {searchResults.length > 0 && (
            <div className="document-search-results">
              {searchResults.map((item) => (
                <button
                  key={item.chunk_id}
                  type="button"
                  onClick={() => onOpenChunk(item.chunk_id)}
                >
                  <strong>{item.section_path.join(" / ") || item.title}</strong>
                  <span>{item.snippet}</span>
                </button>
              ))}
            </div>
          )}
          {busy && (
            <p className="empty-state" role="status" aria-live="polite">
              {locale === "ru" ? "Загрузка контекста…" : "Loading context…"}
            </p>
          )}
          {context && context.chunks.length === 0 && !busy && (
            <p className="empty-state">
              {locale === "ru"
                ? "Текстовый контекст отсутствует"
                : "No text context"}
            </p>
          )}
          {context && context.chunks.length > 0 && (
            <div className="document-context">
              {context.chunks.map((chunk) => (
                <article
                  key={chunk.chunk_id}
                  className={
                    chunk.highlighted
                      ? "document-chunk highlighted"
                      : "document-chunk"
                  }
                >
                  <div className="search-meta">
                    <span>{chunk.section_path.join(" / ") || chunk.title}</span>
                    <span>
                      {chunk.chunk_ordinal
                        ? `#${chunk.chunk_ordinal}`
                        : chunk.chunk_id}
                    </span>
                    {chunk.locator && (
                      <span>{formatLocator(chunk.locator)}</span>
                    )}
                  </div>
                  <p>{chunk.content}</p>
                </article>
              ))}
            </div>
          )}
        </div>
      </div>
    </section>
  );
}

function formatLocator(locator: Record<string, unknown>) {
  const entries = Object.entries(locator).filter(([, value]) => {
    return typeof value === "string" || typeof value === "number";
  });
  return entries.map(([key, value]) => `${key}: ${value}`).join(" · ");
}

function RetrievalDebugger({
  run,
  events,
}: {
  run: QueryRunSummary | null;
  events: RetrievalEvent[];
}) {
  const payloads = events.map(eventPayload);
  const transforms = payloads
    .filter((payload) => String(payload.stage ?? "") === "query_transform")
    .flatMap((payload) => {
      const raw = payload.transforms;
      return Array.isArray(raw) ? raw : [];
    })
    .filter((item): item is Record<string, unknown> => {
      return typeof item === "object" && item !== null;
    });
  const candidates = payloads.flatMap(candidateRows);
  const queryYields = payloads.filter((payload) => {
    return payload.stage === "harness_tool" && payload.tool === "search";
  });
  const decisions = payloads.filter((payload) => {
    return Boolean(payload.decision || payload.reason || payload.reason_codes);
  });
  const context = payloads.find((payload) => {
    return (
      payload.stable_stage === "context_selection" ||
      payload.stage === "context"
    );
  });
  const answerEvent = payloads.find(
    (payload) => payload.stage === "answer_generation",
  );
  const citationEvent = payloads.find(
    (payload) => payload.stage === "citation_validation",
  );
  const feedbackEvents = payloads.filter(
    (payload) => payload.stage === "feedback" || payload.stage === "evaluation",
  );

  return (
    <>
      <div className="debug-summary">
        <span>{run?.status ?? "unknown"}</span>
        <span>{run?.mode ?? ""}</span>
        <span>{run?.model_alias ?? ""}</span>
        <code>{run?.trace_id ?? ""}</code>
      </div>

      <section className="debug-stage">
        <h3>Timeline</h3>
        <table>
          <thead>
            <tr>
              <th>Stage</th>
              <th>Subquery</th>
              <th>Count</th>
              <th>Latency</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {payloads.map((payload, index) => (
              <tr key={`timeline-${index}`}>
                <td>{stageName(payload)}</td>
                <td>{queryContextValue(payload, "subquery_id")}</td>
                <td>{String(payload.count ?? "")}</td>
                <td>
                  {String(payload.latency_ms ?? payload.stage_latency_ms ?? "")}
                </td>
                <td>
                  {String(
                    payload.status ?? payload.decision ?? payload.reason ?? "",
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      {transforms.length > 0 && (
        <section className="debug-stage">
          <h3>Query Transforms</h3>
          <table>
            <thead>
              <tr>
                <th>Order</th>
                <th>ID</th>
                <th>Type</th>
                <th>Status</th>
                <th>Output Subqueries</th>
                <th>Value</th>
              </tr>
            </thead>
            <tbody>
              {transforms.map((transform, index) => (
                <tr key={`transform-${index}`}>
                  <td>{String(transform.order ?? index + 1)}</td>
                  <td>{String(transform.transform_id ?? "")}</td>
                  <td>{String(transform.type ?? "")}</td>
                  <td>
                    {String(
                      transform.status ?? (transform.changed ? "changed" : ""),
                    )}
                  </td>
                  <td>
                    <code>{queryRefIds(transform.query_refs)}</code>
                  </td>
                  <td>
                    <code>
                      {JSON.stringify(
                        transform.text ??
                          transform.queries ??
                          transform.reason ??
                          "",
                      )}
                    </code>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}

      {queryYields.length > 0 && (
        <section className="debug-stage">
          <h3>Query Yield</h3>
          <table>
            <thead>
              <tr>
                <th>Step</th>
                <th>Subquery</th>
                <th>Transform</th>
                <th>New</th>
                <th>Selected</th>
                <th>Coverage</th>
                <th>Max BM25</th>
                <th>Max Dense</th>
                <th>Max Fusion</th>
                <th>Max Rerank</th>
                <th>Latency</th>
              </tr>
            </thead>
            <tbody>
              {queryYields.map((payload, index) => (
                <tr key={`query-yield-${index}`}>
                  <td>{String(payload.step ?? "")}</td>
                  <td>{queryContextValue(payload, "subquery_id")}</td>
                  <td>{queryContextValue(payload, "transform_id")}</td>
                  <td>{String(payload.new_evidence ?? "")}</td>
                  <td>{String(payload.selected_count ?? "")}</td>
                  <td>{formatNumber(payload.coverage)}</td>
                  <td>{formatNumber(payload.max_bm25_score)}</td>
                  <td>{formatNumber(payload.max_dense_score)}</td>
                  <td>{formatNumber(payload.max_fusion_score)}</td>
                  <td>{formatNumber(payload.max_rerank_score)}</td>
                  <td>{String(payload.latency_ms ?? "")}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}

      {candidates.length > 0 && (
        <section className="debug-stage">
          <h3>Candidate Movement</h3>
          <table>
            <thead>
              <tr>
                <th>Stage</th>
                <th>Subquery</th>
                <th>Chunk</th>
                <th>Document</th>
                <th>KB</th>
                <th>BM25</th>
                <th>Dense</th>
                <th>Fusion</th>
                <th>Rerank</th>
                <th>Final</th>
              </tr>
            </thead>
            <tbody>
              {candidates.map((row, index) => (
                <tr key={`candidate-${index}`}>
                  <td>{row.stage}</td>
                  <td>{row.subqueryId}</td>
                  <td>{row.chunkId}</td>
                  <td>{row.documentId}</td>
                  <td>{row.kbId}</td>
                  <td>{rankScore(row, "bm25")}</td>
                  <td>{rankScore(row, "dense")}</td>
                  <td>{rankScore(row, "fusion")}</td>
                  <td>{rankScore(row, "rerank")}</td>
                  <td>{row.ranks.final ?? ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}

      {decisions.length > 0 && (
        <section className="debug-stage">
          <h3>Decisions</h3>
          <table>
            <thead>
              <tr>
                <th>Stage</th>
                <th>Chunk</th>
                <th>Decision</th>
                <th>Reason</th>
              </tr>
            </thead>
            <tbody>
              {decisions.map((payload, index) => (
                <tr key={`decision-${index}`}>
                  <td>{stageName(payload)}</td>
                  <td>{String(payload.chunk_id ?? "")}</td>
                  <td>{String(payload.decision ?? "")}</td>
                  <td>
                    <code>
                      {JSON.stringify(
                        payload.reason_codes ?? payload.reason ?? "",
                      )}
                    </code>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}

      {(context ||
        answerEvent ||
        citationEvent ||
        feedbackEvents.length > 0) && (
        <section className="debug-stage">
          <h3>Final Context</h3>
          <pre>
            {JSON.stringify(
              {
                context,
                answer_generation: answerEvent,
                citation_validation: citationEvent,
                feedback: feedbackEvents,
              },
              null,
              2,
            )}
          </pre>
        </section>
      )}

      {payloads.map((payload, index) => (
        <DebugStage key={`raw-${index}`} payload={payload} />
      ))}
    </>
  );
}

function DebugStage({ payload }: { payload: Record<string, unknown> }) {
  const stage = String(payload.stage ?? "");
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

function eventPayload(event: RetrievalEvent): Record<string, unknown> {
  return typeof event.payload === "object" && event.payload !== null
    ? (event.payload as Record<string, unknown>)
    : (event as unknown as Record<string, unknown>);
}

function stageName(payload: Record<string, unknown>): string {
  return String(payload.stable_stage ?? payload.stage ?? "");
}

type CandidateRow = {
  stage: string;
  subqueryId: string;
  transformId: string;
  chunkId: string;
  documentId: string;
  kbId: string;
  ranks: Record<string, number>;
  scores: Record<string, number>;
};

function candidateRows(payload: Record<string, unknown>): CandidateRow[] {
  const raw = payload.candidates;
  const payloadSubqueryId = queryContextValue(payload, "subquery_id");
  const payloadTransformId = queryContextValue(payload, "transform_id");
  if (!Array.isArray(raw)) return [];
  return raw
    .filter((candidate): candidate is Record<string, unknown> => {
      return typeof candidate === "object" && candidate !== null;
    })
    .map((candidate) => ({
      stage: stageName(payload),
      subqueryId: String(
        candidate.subquery_id ??
          queryContextValue(candidate, "subquery_id") ??
          payloadSubqueryId,
      ),
      transformId: String(
        candidate.transform_id ??
          queryContextValue(candidate, "transform_id") ??
          payloadTransformId,
      ),
      chunkId: String(candidate.chunk_id ?? ""),
      documentId: String(candidate.document_id ?? ""),
      kbId: String(candidate.knowledge_base_id ?? ""),
      ranks: numericRecord(candidate.ranks),
      scores: numericRecord(candidate.scores),
    }));
}

function queryContextValue(
  payload: Record<string, unknown>,
  key: string,
): string {
  const direct = payload[key];
  if (typeof direct === "string") return direct;
  const context = payload.query_context;
  if (typeof context === "object" && context !== null) {
    const value = (context as Record<string, unknown>)[key];
    if (typeof value === "string") return value;
  }
  return "";
}

function queryRefIds(value: unknown): string {
  if (!Array.isArray(value)) return "";
  const ids = value
    .filter((item): item is Record<string, unknown> => {
      return typeof item === "object" && item !== null;
    })
    .map((item) => String(item.subquery_id ?? ""))
    .filter(Boolean);
  return ids.join(", ");
}

function formatNumber(value: unknown): string {
  return typeof value === "number" ? value.toFixed(3) : "";
}

function numericRecord(value: unknown): Record<string, number> {
  if (typeof value !== "object" || value === null) return {};
  return Object.fromEntries(
    Object.entries(value as Record<string, unknown>).filter(
      (entry): entry is [string, number] => typeof entry[1] === "number",
    ),
  );
}

function rankScore(row: CandidateRow, key: string): string {
  const rank = row.ranks[key];
  const score = row.scores[key] ?? row.scores[`rrf_${key}`];
  if (rank === undefined && score === undefined) return "";
  return `${rank ?? ""}${score === undefined ? "" : ` / ${score.toFixed(3)}`}`;
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
