export interface Coin {
  id: number;
  coingecko_id: string;
  symbol: string;
  name: string;
  rank: number | null;
  market_cap_usd: string | null;
  source_last_updated: string | null;
  binance_symbol: string;
  binance_contract_multiplier: number;
  analysis_eligible: boolean;
  analysis_exclusion_reason: string;
  is_stablecoin: boolean;
  in_top100: boolean;
  first_seen_at: string;
  last_seen_in_top100_at: string | null;
  last_updated_at: string;
}

export interface AnalysisRun {
  id: string;
  status: 'running' | 'succeeded' | 'failed';
  trigger: string;
  mode: 'normal' | 'hybrid';
  scope: 'universe' | 'solo';
  params: Record<string, unknown>;
  summary: Record<string, unknown>;
  error: string;
  started_at: string;
  finished_at: string | null;
  data_cutoff: string | null;
  duration_seconds: number | null;
}

export interface EquityPoint {
  t: string;
  equity: number;
  price?: number;
  low?: number;
  high?: number;
  dd?: number;
  fold?: number;
}

export interface ChartPayload {
  equity_curve?: EquityPoint[];
  grid_levels?: number[];
  folds?: Array<Record<string, unknown>>;
  recommended_params?: Record<string, unknown>;
}

export interface Trade {
  time?: string;
  side?: string;
  price?: number;
  qty?: number;
  notional?: number;
  fee?: number;
  realized_pnl?: number;
  reason?: string;
  level?: number | null;
}

export interface GateStatus {
  name: string;
  label: string;
  passed: boolean;
  value: number | string | boolean | null;
  threshold: number | string | boolean | Record<string, unknown> | null;
  reason: string;
}

export interface ReviewFlag {
  code: string;
  severity: 'info' | 'warning' | 'critical';
  message: string;
}

export interface ScoreFactor {
  score: number;
  weight: number;
  contribution: number;
  status: 'strong' | 'ok' | 'watch' | 'weak';
  inputs: Record<string, number | null>;
}

export interface ScorePenalty {
  code: string;
  points: number;
  reason: string;
}

export interface ScoreBreakdown {
  factors: Record<string, ScoreFactor>;
  penalties: ScorePenalty[];
  base_score: number;
  final_score: number;
}

export interface RegimeBucket {
  folds: number;
  returns: number[];
  drawdowns: number[];
  mean_return: number;
  mean_drawdown: number;
  profitable: number;
}

export interface Diagnostics {
  methodology_version?: number;
  hard_gates?: GateStatus[];
  flags?: ReviewFlag[];
  score_breakdown?: ScoreBreakdown;
  baseline_score?: number;
  score_delta?: number;
  eligibility_reasons?: string[];
  pf_validation?: {
    review_required: boolean;
    validation_required: boolean;
    validation_passed: boolean;
    enough_trades: boolean;
    stress_checked: boolean;
    stress_profitable: boolean | null;
    no_stop: boolean;
    review_threshold: number;
    validation_threshold: number;
  };
  correlation?: {
    avg_correlation?: number | null;
    max_correlation?: number | null;
    peers?: Array<{ symbol: string; correlation: number }>;
    exceeds_limit?: boolean;
  };
  liquidity?: Record<string, number | null>;
  regime?: {
    regimes?: Record<string, RegimeBucket>;
    regimes_covered?: number;
    labels?: string[];
    profitable_share?: number;
    dominant_regime?: string;
    dominant_share?: number;
    dependence_penalty?: number;
    robustness_score?: number;
    single_regime?: boolean;
  };
  cost_stress?: {
    multiplier?: number;
    stress_return?: number;
    stress_max_drawdown?: number;
    stress_delta?: number;
    profitable?: boolean;
  };
  sensitivity?: {
    parameter_sensitivity?: number | null;
    mean_neighbor_return?: number | null;
    worst_neighbor_return?: number | null;
    runs?: Array<{ variant: string; net_return: number; max_drawdown: number }>;
  };
  exposure?: {
    max_exposure_ratio?: number;
    avg_exposure_ratio?: number;
    max_unrealized_loss?: number;
    max_unrealized_loss_pct?: number;
  };
  execution?: {
    slippage_paid?: number;
    buy_fills?: number;
    sell_fills?: number;
    cycle_completion_ratio?: number;
  };
  return_stability?: {
    top_trade_share?: number;
    top5_trade_share?: number;
    fold_return_std?: number;
  };
  fold_returns?: number[];
  fold_drawdowns?: number[];
  recovery_days?: number;
  max_drawdown_duration_days?: number;
}

export interface CoinResult {
  id: number;
  analysis: string;
  coin: Coin;
  rank: number | null;
  score: number;
  suitability: 'prime' | 'viable' | 'watch' | 'avoid' | 'ineligible';
  eligible: boolean;
  exclusion_reason: string;
  oos_metrics: Record<string, number | string | boolean | null>;
  in_sample_metrics?: Record<string, number | string | boolean | null>;
  features?: Record<string, number | string | boolean | null>;
  component_scores: Record<string, number | null>;
  diagnostics?: Diagnostics | null;
  params: Record<string, number | string>;
  folds?: Array<Record<string, unknown>>;
  chart?: ChartPayload;
  trade_sample?: Trade[];
  data_start?: string | null;
  data_end?: string | null;
  created_at?: string;
}

export interface Paginated<T> {
  count: number;
  next: string | null;
  previous: string | null;
  results: T[];
}

export interface DashboardSummary {
  generated_at: string;
  engine: {
    latest_analysis: AnalysisRun | null;
    latest_hybrid_analysis: AnalysisRun | null;
    results: CoinResult[];
    suitability_counts: Record<string, number>;
  };
  universe: {
    coins: number;
    top100: number;
    eligible: number;
  };
  paper: {
    sessions: number;
    active: number;
    promotion_ready: number;
    equity: number;
    capital: number;
    avg_return: number;
  };
  jobs: {
    status_counts: Record<string, number>;
    queued: number;
    running: number;
    recent: JobRun[];
  };
  reports: Report[];
  live: LiveStatus;
}

export interface JobRun {
  id: string;
  job_type: string;
  trigger: string;
  status: 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled';
  schedule_slug: string | null;
  params: Record<string, unknown>;
  result: Record<string, unknown>;
  error: string;
  worker: string;
  enqueued_at: string;
  started_at: string | null;
  finished_at: string | null;
  duration_seconds: number | null;
}

export interface JobSchedule {
  slug: string;
  name: string;
  description: string;
  job_type: string;
  kind: string;
  params: Record<string, unknown>;
  enabled: boolean;
  interval_seconds: number | null;
  next_run_at: string | null;
  last_enqueued_at: string | null;
  last_job_id: string | null;
  next_run_in_seconds: number | null;
}

export interface JobSummary {
  now: string;
  status_counts: Record<string, number>;
  queued: number;
  running: number;
  recent_runs: JobRun[];
  schedules: JobSchedule[];
  job_types: Array<{ job_type: string; description: string }>;
}

export interface Report {
  id: number;
  analysis: string | null;
  kind: 'ranking' | 'performance' | 'risk' | 'methodology';
  title: string;
  body_markdown: string;
  payload: Record<string, unknown>;
  created_at: string;
}

export interface PaperSession {
  id: string;
  coin: Coin;
  analysis: string | null;
  status: 'active' | 'paused' | 'stopped';
  bias: 'bullish' | 'bearish';
  promotion_ready: boolean;
  stop_reason: string;
  params: Record<string, number | string>;
  config?: Record<string, number | string>;
  grid_state?: Record<string, unknown>;
  capital: number;
  leverage: number;
  position_qty: number;
  avg_entry: number;
  mark_price: number;
  equity: number;
  peak_equity: number;
  max_drawdown: number;
  net_return: number;
  realized_pnl: number;
  fees_paid: number;
  funding_paid: number;
  fill_count: number;
  closed_lots: number;
  wins: number;
  losses: number;
  started_at: string;
  last_tick_at: string | null;
  last_evaluated_at: string | null;
  updated_at: string;
}

export interface PaperFill {
  id: number;
  time: string;
  side: string;
  price: number;
  qty: number;
  notional: number;
  fee: number;
  realized_pnl: number;
  level: number | null;
  reason: string;
}

export interface PaperEvaluation {
  id: number;
  evaluated_at: string;
  passed: boolean;
  metrics: Record<string, unknown>;
  failures: string[];
  thresholds: Record<string, unknown>;
}

export interface PaperEquityPoint {
  time: string;
  equity: number;
  price: number | null;
  position_qty: number | null;
  drawdown: number | null;
}

export type TradingMode = 'normal' | 'hybrid';

export interface TradingModeState {
  mode: TradingMode;
  adx_enabled: boolean;
  updated_at: string;
}

export interface TradingModeResponse extends TradingModeState {
  purge?: { sessions: number; positions: number };
  live?: LiveStatus;
}

export interface LiveApproval {
  id: number;
  status: 'pending' | 'approved' | 'revoked' | 'rejected';
  requested_by: string;
  requested_at: string;
  reviewed_by: string;
  reviewed_at: string | null;
  notes: string;
  paper_session: string | null;
  paper_symbol: string | null;
  risk_acknowledged: boolean;
}

export interface LiveStatus {
  env_enabled: boolean;
  keys_present: boolean;
  mode: TradingMode;
  hybrid: {
    ema_period: number;
    adx_period: number;
    adx_entry: number;
    adx_exit: number;
    confirm_bars: number;
    adx_enabled: boolean;
  };
  kill_switch: boolean;
  kill_reason: string;
  approval: {
    id: number;
    status: string;
    reviewed_by: string;
    reviewed_at: string | null;
  } | null;
  live_ready: boolean;
  testnet: boolean;
  limits: {
    max_notional_usd: number;
    max_daily_loss_usd: number;
    max_leverage: number;
    symbol_allowlist: string[];
  };
}

export interface LiveState {
  mode: TradingMode;
  kill_switch: boolean;
  kill_reason: string;
  active_approval: LiveApproval | null;
  limits: Record<string, unknown>;
  updated_at: string;
}

export interface LiveOrderAudit {
  id: number;
  created_at: string;
  symbol: string;
  side: string;
  order_type: string;
  qty: number | null;
  price: number | null;
  notional: number | null;
  allowed: boolean;
  reason: string;
  response: Record<string, unknown>;
}

export interface LiveOverview {
  status: LiveStatus;
  state: LiveState;
  approvals: LiveApproval[];
  audits: LiveOrderAudit[];
  note: string;
}
