import type {
  AnalysisRun,
  Coin,
  CoinResult,
  DashboardSummary,
  JobRun,
  JobSchedule,
  JobSummary,
  LiveApproval,
  LiveOverview,
  LiveStatus,
  MarketRegime,
  Paginated,
  PaperEquityPoint,
  PaperEvaluation,
  PaperFill,
  PaperSession,
  Report,
  StrategySettings,
  StrategySettingsValues,
  StrategyValidation,
  TradingMode,
  TradingModeResponse
} from './types';

const BASE = '/api/engine';

/** DRF validation errors arrive as { field: [messages] }. */
function fieldErrors(payload: Record<string, unknown>): string {
  return Object.entries(payload)
    .map(([field, messages]) => {
      const text = Array.isArray(messages) ? messages.join(' ') : String(messages);
      return `${field.replace(/_/g, ' ')}: ${text}`;
    })
    .join(' · ');
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE}/${path}`, {
    ...init,
    headers: {
      accept: 'application/json',
      ...(init?.body ? { 'content-type': 'application/json' } : {}),
      ...init?.headers
    },
    cache: 'no-store'
  });
  const text = await response.text();
  let payload: unknown = null;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = text;
    }
  }
  if (!response.ok) {
    const detail =
      payload && typeof payload === 'object' && 'detail' in payload
        ? String((payload as { detail: unknown }).detail)
        : typeof payload === 'string'
          ? payload
          : payload && typeof payload === 'object'
            ? fieldErrors(payload as Record<string, unknown>) ||
              `Request failed (${response.status})`
            : `Request failed (${response.status})`;
    throw new Error(detail);
  }
  return payload as T;
}

function post<T>(path: string, body?: unknown): Promise<T> {
  return request<T>(path, {
    method: 'POST',
    body: body === undefined ? undefined : JSON.stringify(body)
  });
}

export const engineApi = {
  dashboard: () => request<DashboardSummary>('engine/dashboard/'),

  analysisRuns: (limit = 20) => request<Paginated<AnalysisRun>>(`engine/analysis/?limit=${limit}`),
  latestAnalysis: () => request<AnalysisRun>('engine/analysis/latest/'),
  triggerAnalysis: (params: Record<string, unknown> = {}) =>
    post<JobRun>('engine/analysis/trigger/', { params }),

  results: (params: { limit?: number; eligible?: boolean; search?: string } = {}) => {
    const search = new URLSearchParams();
    search.set('limit', String(params.limit ?? 200));
    search.set('compact', 'true');
    if (params.eligible) search.set('eligible', 'true');
    if (params.search) search.set('search', params.search);
    return request<Paginated<CoinResult>>(`engine/results/?${search.toString()}`);
  },
  result: (id: number) => request<CoinResult>(`engine/results/${id}/?compact=false`),
  resultsByCoin: (symbol: string) =>
    request<{ symbol: string; results: CoinResult[] }>(
      `engine/results/by_coin/?symbol=${encodeURIComponent(symbol.toUpperCase())}`
    ),

  coins: (params: { limit?: number; search?: string; eligible?: boolean } = {}) => {
    const search = new URLSearchParams();
    search.set('limit', String(params.limit ?? 200));
    if (params.search) search.set('search', params.search);
    if (params.eligible) search.set('eligible', 'true');
    return request<Paginated<Coin>>(`coins/?${search.toString()}`);
  },
  addCoin: (body: { symbol: string; name?: string }) =>
    // ``job`` is null when an analysis is already running; ``detail`` says so.
    post<{ coin: Coin; job: JobRun | null; detail?: string }>('coins/add/', body),

  reports: (kind?: string) =>
    request<Paginated<Report>>(`engine/reports/?limit=100${kind ? `&kind=${kind}` : ''}`),
  latestReports: () => request<Record<string, Report | null>>('engine/reports/latest/'),

  jobsSummary: () => request<JobSummary>('jobs/summary/'),
  jobRuns: (params: { limit?: number; status?: string; jobType?: string } = {}) => {
    const search = new URLSearchParams();
    search.set('limit', String(params.limit ?? 60));
    if (params.status) search.set('status', params.status);
    if (params.jobType) search.set('job_type', params.jobType);
    return request<Paginated<JobRun>>(`jobs/runs/?${search.toString()}`);
  },
  triggerJob: (jobType: string, params: Record<string, unknown> = {}) =>
    post<JobRun>('jobs/runs/run/', { job_type: jobType, params }),
  schedules: () => request<Paginated<JobSchedule>>('jobs/schedules/'),
  runSchedule: (slug: string, params?: Record<string, unknown>) =>
    post<JobRun>(`jobs/schedules/${slug}/run/`, params ? { params } : {}),
  toggleSchedule: (slug: string) => post<JobSchedule>(`jobs/schedules/${slug}/toggle/`),
  cancelJob: (id: string) => post<JobRun>(`jobs/runs/${id}/cancel/`),

  paperSessions: (status?: string) =>
    request<Paginated<PaperSession>>(
      `paper/sessions/?limit=100${status ? `&status=${status}` : ''}`
    ),
  paperSession: (id: string) => request<PaperSession>(`paper/sessions/${id}/`),
  paperEquity: (id: string) =>
    request<{
      session: string;
      symbol: string;
      points: PaperEquityPoint[];
      capital: number;
    }>(`paper/sessions/${id}/equity/`),
  paperFills: (id: string) =>
    request<{ session: string; symbol: string; fills: PaperFill[] }>(
      `paper/sessions/${id}/fills/?limit=500`
    ),
  paperEvaluations: (id: string) => request<PaperEvaluation[]>(`paper/sessions/${id}/evaluations/`),
  sessionAction: (
    id: string,
    action: 'pause' | 'resume' | 'stop' | 'evaluate' | 'tick',
    body?: Record<string, unknown>
  ) => post<unknown>(`paper/sessions/${id}/${action}/`, body),
  setBias: (id: string, bias: 'bullish' | 'bearish') =>
    post<PaperSession>(`paper/sessions/${id}/bias/`, { bias }),
  tickAll: (sessionIds?: string[]) =>
    post<JobRun>('paper/sessions/tick/', sessionIds ? { session_ids: sessionIds } : {}),

  live: () => request<LiveOverview>('live/status/'),
  tradingMode: () => request<TradingModeResponse>('live/status/mode/'),
  strategySettings: () => request<StrategySettings>('strategy/settings/'),
  setStrategySettings: (body: Partial<StrategySettingsValues> & { rerun_analysis?: boolean }) =>
    post<StrategySettings>('strategy/settings/', body),
  strategyValidation: () => request<StrategyValidation>('strategy/validation/'),
  marketRegime: () => request<MarketRegime>('strategy/regime/'),
  setTradingMode: (body: {
    mode: TradingMode;
    adx_enabled?: boolean;
    confirm?: boolean;
    rerun_analysis?: boolean;
  }) =>
    post<TradingModeResponse>('live/status/mode/', body),
  requestApproval: (body: { paper_session: string; notes?: string; risk_acknowledged: boolean }) =>
    post<LiveApproval>('live/approvals/request_approval/', body),
  approveLive: (id: number, body: { reviewed_by?: string; notes?: string }) =>
    post<{ approval: LiveApproval; live: LiveStatus }>(`live/approvals/${id}/approve/`, body),
  revokeLive: (id: number) =>
    post<{ approval: LiveApproval; live: LiveStatus }>(`live/approvals/${id}/revoke/`),
  killSwitch: (engaged: boolean, reason = '') =>
    post<{ state: unknown; live: LiveStatus }>('live/status/kill-switch/', {
      engaged,
      reason
    })
};

export function metricsNumber(
  metrics: Record<string, unknown> | undefined,
  key: string,
  fallback = 0
): number {
  const value = metrics?.[key];
  const parsed = typeof value === 'string' ? Number(value) : value;
  return typeof parsed === 'number' && Number.isFinite(parsed) ? parsed : fallback;
}

export function formatPercent(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—';
  return `${(value * 100).toFixed(digits)}%`;
}

export function formatUsd(value: number | null | undefined, digits = 0): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—';
  return value.toLocaleString('en-US', {
    style: 'currency',
    currency: 'USD',
    maximumFractionDigits: digits,
    minimumFractionDigits: digits
  });
}

export function formatCompactUsd(value: string | number | null | undefined): string {
  if (value === null || value === undefined) return '—';
  const parsed = typeof value === 'string' ? Number(value) : value;
  if (!Number.isFinite(parsed)) return '—';
  if (parsed >= 1e12) return `$${(parsed / 1e12).toFixed(2)}T`;
  if (parsed >= 1e9) return `$${(parsed / 1e9).toFixed(2)}B`;
  if (parsed >= 1e6) return `$${(parsed / 1e6).toFixed(1)}M`;
  return formatUsd(parsed);
}

export function formatDate(value: string | null | undefined): string {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '—';
  return date.toLocaleString('en-US', {
    month: 'short',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit'
  });
}

export function relativeTime(value: string | null | undefined): string {
  if (!value) return '—';
  const target = new Date(value).getTime();
  if (Number.isNaN(target)) return '—';
  const diff = target - Date.now();
  const abs = Math.abs(diff);
  const minutes = Math.round(abs / 60000);
  if (minutes < 1) return diff >= 0 ? 'now' : 'just now';
  if (minutes < 60) return diff >= 0 ? `in ${minutes}m` : `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 48) return diff >= 0 ? `in ${hours}h` : `${hours}h ago`;
  const days = Math.round(hours / 24);
  return diff >= 0 ? `in ${days}d` : `${days}d ago`;
}
