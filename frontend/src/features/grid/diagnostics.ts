import { formatCompactUsd } from './api';

export const FACTOR_LABELS: Record<string, string> = {
  profitability: 'Profitability',
  risk_adjusted: 'Risk-adjusted return',
  drawdown_control: 'Drawdown control',
  consistency: 'Fold consistency',
  regime: 'Regime robustness',
  stability: 'Structural stability',
  execution: 'Execution quality',
  diversification: 'Diversification'
};

// Gates whose observed value is a fraction (return, drawdown, share) or USD.
const PERCENT_GATES = new Set([
  'positive_return',
  'drawdown',
  'fold_consistency',
  'atr',
  'cost_stress'
]);
const USD_GATES = new Set(['liquidity']);

export function formatGateValue(
  value: number | string | boolean | null | undefined,
  gateName?: string
): string {
  if (value === null || value === undefined || value === '') return '—';
  if (typeof value === 'boolean') return value ? 'yes' : 'no';
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) return '—';
    if (gateName && PERCENT_GATES.has(gateName)) return `${(value * 100).toFixed(2)}%`;
    if (gateName && USD_GATES.has(gateName)) return formatCompactUsd(value);
    if (Number.isInteger(value)) return value.toLocaleString();
    return Math.abs(value) >= 100 ? value.toFixed(0) : value.toFixed(3);
  }
  return value;
}

export function factorStatusClass(status: string | undefined): string {
  if (status === 'strong') return 'bg-emerald-500';
  if (status === 'ok') return 'bg-primary';
  if (status === 'watch') return 'bg-amber-500';
  return 'bg-rose-500';
}
