import type { RegimePhase } from './types';

export const REGIME_REASON_LABELS: Record<string, string> = {
  guard_armed: 'BTC RSI overbought (guard armed)',
  funding_crowded: 'Funding crowded while momentum fades',
  guard_exit: 'BTC overbought guard exit (trailing or OI flush)',
  trailing_stop: 'BTC fell below its trailing stop',
  oi_flush: 'Open-interest flush near the 24h high',
  support_breakdown: 'BTC 1h close below support',
  recovery_pending: 'Waiting for every recovery check',
  disabled: 'Regime protection switched off'
};

export const RECOVERY_CHECK_LABELS: Record<string, string> = {
  guard_recovery: 'Guard recovery (RSI rising above 35, close above VWAP, OI turning up)',
  above_support: '1h close back above support',
  trailing_clear: 'Drawdown back inside the trailing stop',
  no_oi_flush: 'No open-interest flush',
  min_duration: 'Minimum bearish duration elapsed'
};

export const REGIME_EFFECTS: Record<RegimePhase, string> = {
  normal: 'Grid trading is unrestricted.',
  warning: 'New buys are blocked on every coin; open positions are kept.',
  bearish:
    'Every active paper session was sold to USDT and paused; trading resumes once BTC passes every recovery check.'
};

export function regimeReasonLabel(reason: string): string {
  return REGIME_REASON_LABELS[reason] ?? reason.replaceAll('_', ' ');
}
