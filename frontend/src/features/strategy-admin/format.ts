import { formatCompactUsd, formatPercent } from '@/features/grid/api';
import { PARAM_LABELS, PARAM_PERCENT_KEYS } from './catalog';
import type { CatalogTone } from './catalog';

export type Tone = CatalogTone;

export function percentMetric(
  metrics: Record<string, unknown> | undefined,
  key: string
): string {
  const value = metrics?.[key];
  const parsed = typeof value === 'string' ? Number(value) : value;
  return typeof parsed === 'number' && Number.isFinite(parsed) ? formatPercent(parsed) : '—';
}

export function numberMetric(
  metrics: Record<string, unknown> | undefined,
  key: string
): string {
  const value = metrics?.[key];
  const parsed = typeof value === 'string' ? Number(value) : value;
  if (typeof parsed === 'number' && Number.isFinite(parsed)) {
    return Number.isInteger(parsed) ? parsed.toLocaleString() : parsed.toFixed(2);
  }
  return '—';
}

export function formatThreshold(
  value: unknown,
  format: 'usd' | 'percent' | 'number' | 'multiplier'
): string {
  const parsed = typeof value === 'string' ? Number(value) : value;
  if (typeof parsed !== 'number' || !Number.isFinite(parsed)) return '—';
  if (format === 'usd') return formatCompactUsd(parsed);
  if (format === 'percent') return formatPercent(parsed, 0);
  if (format === 'multiplier') return `×${parsed.toFixed(2)}`;
  return Number.isInteger(parsed) ? parsed.toLocaleString() : parsed.toFixed(2);
}

export function paramValue(key: string, value: unknown): string {
  if (key === 'max_total_drawdown' && value === 0) return 'off';
  if (typeof value === 'number') {
    if (PARAM_PERCENT_KEYS.has(key)) return formatPercent(value, 0);
    return Number.isInteger(value) ? String(value) : value.toFixed(2);
  }
  if (typeof value === 'boolean') return value ? 'yes' : 'no';
  return String(value).replace(/_/g, ' ');
}

export const PARAM_ORDER = Object.keys(PARAM_LABELS);

export function orderedParams(params: Record<string, number | string>) {
  return Object.entries(params).toSorted(([left], [right]) => {
    const leftIndex = PARAM_ORDER.indexOf(left);
    const rightIndex = PARAM_ORDER.indexOf(right);
    return (
      (leftIndex === -1 ? PARAM_ORDER.length : leftIndex) -
      (rightIndex === -1 ? PARAM_ORDER.length : rightIndex)
    );
  });
}

export function paramText(
  params: Record<string, number | string> | undefined,
  key: string,
  suffix = ''
): string {
  const value = params?.[key];
  return value === undefined || value === null || value === '' ? '—' : `${value}${suffix}`;
}

export function toNumber(value: unknown): number | null {
  const parsed = typeof value === 'string' ? Number(value) : value;
  return typeof parsed === 'number' && Number.isFinite(parsed) ? parsed : null;
}

export function signedPercent(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—';
  const formatted = formatPercent(value, digits);
  return value > 0 ? `+${formatted}` : formatted;
}

export function scoreTone(score: number | null | undefined): Tone {
  if (score === null || score === undefined || !Number.isFinite(score)) return 'neutral';
  if (score >= 85) return 'violet';
  if (score >= 70) return 'good';
  if (score >= 55) return 'info';
  if (score >= 40) return 'warn';
  return 'bad';
}

export function returnTone(value: number | null | undefined): Tone {
  if (value === null || value === undefined || !Number.isFinite(value)) return 'neutral';
  if (value > 0) return 'good';
  if (value < 0) return 'bad';
  return 'neutral';
}

export function drawdownTone(value: number | null | undefined, limit: number): Tone {
  if (value === null || value === undefined || !Number.isFinite(value)) return 'neutral';
  if (value <= limit * 0.6) return 'good';
  if (value <= limit) return 'warn';
  return 'bad';
}

export function profitFactorTone(value: number | null | undefined): Tone {
  if (value === null || value === undefined || !Number.isFinite(value)) return 'neutral';
  if (value >= 1.5) return 'good';
  if (value >= 1) return 'warn';
  return 'bad';
}

export function exposureTone(value: number | null | undefined, cap: number): Tone {
  if (value === null || value === undefined || !Number.isFinite(value)) return 'neutral';
  if (value <= cap) return 'good';
  if (value <= cap * 1.5) return 'warn';
  return 'bad';
}

export function regimeTone(covered: number | null | undefined): Tone {
  if (covered === null || covered === undefined) return 'neutral';
  if (covered >= 3) return 'good';
  if (covered === 2) return 'info';
  if (covered === 1) return 'warn';
  return 'bad';
}
