'use client';

import { Icons } from '@/components/icons';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { formatDate, formatPercent, relativeTime } from '@/features/grid/api';
import { useMarketRegime } from '@/features/grid/queries';
import { RECOVERY_CHECK_LABELS, REGIME_EFFECTS, regimeReasonLabel } from '@/features/grid/regime';
import type { MarketRegime, RegimePhase } from '@/features/grid/types';
import { cn } from '@/lib/utils';
import type { CatalogTone } from '../catalog';
import { Meter, toneBadge, ToneBadge, ToneNote, toneSurface, toneText } from './visuals';

const PHASE_TONE: Record<RegimePhase, CatalogTone> = {
  normal: 'good',
  warning: 'warn',
  bearish: 'bad'
};

type SignalRow = {
  key: string;
  name: string;
  tier: 'warning' | 'bearish';
  reading: string;
  threshold: string;
  /** Progress towards the trigger, 0–1; null when the reading is unavailable. */
  progress: number | null;
  active: boolean;
};

function fixed(value: number | null | undefined, digits = 2): string {
  return value === null || value === undefined || !Number.isFinite(value)
    ? '—'
    : value.toFixed(digits);
}

function signalRows(regime: MarketRegime): SignalRow[] {
  const m = regime.metrics;
  const t = regime.thresholds;
  const active = new Set(regime.reasons);
  const drawdown = m.drawdown_pct ?? null;
  const oiChange = m.oi_change_pct ?? null;
  const supportGap =
    m.close_1h && m.support_line && m.support_line > 0 ? m.close_1h / m.support_line - 1 : null;
  const fundingZ = m.funding_z ?? null;
  const rsi = m.rsi_15m ?? null;
  return [
    {
      key: 'guard_armed',
      name: 'RSI overbought (15m)',
      tier: 'warning',
      reading: rsi === null ? '—' : fixed(rsi, 1),
      threshold: `> ${t.rsi_overbought}`,
      progress: rsi === null ? null : rsi / t.rsi_overbought,
      active: active.has('guard_armed') || m.guard_phase === 'armed'
    },
    {
      key: 'funding_crowded',
      name: 'Funding crowding',
      tier: 'warning',
      reading:
        fundingZ === null
          ? '—'
          : `z ${fixed(fundingZ)}${m.trend_up && (m.momentum ?? 0) >= 0 ? ' · trend up' : ' · momentum fading'}`,
      threshold: `z ≥ ${t.funding_z}`,
      progress: fundingZ === null ? null : fundingZ / t.funding_z,
      active: active.has('funding_crowded')
    },
    {
      key: 'trailing_stop',
      name: `Trailing stop (${t.trailing_window_days}d high)`,
      tier: 'bearish',
      reading: drawdown === null ? '—' : `${formatPercent(drawdown, 1)} below`,
      threshold: formatPercent(t.trailing_stop_pct, 0),
      progress: drawdown === null ? null : drawdown / t.trailing_stop_pct,
      active: active.has('trailing_stop')
    },
    {
      key: 'oi_flush',
      name: 'Open-interest flush (6h)',
      tier: 'bearish',
      reading: m.oi_available && oiChange !== null ? formatPercent(oiChange, 1) : 'unavailable',
      threshold: `≤ −${formatPercent(t.oi_flush_pct, 0)} near high`,
      progress:
        m.oi_available && oiChange !== null ? Math.max(0, -oiChange) / t.oi_flush_pct : null,
      active: active.has('oi_flush')
    },
    {
      key: 'support_breakdown',
      name: 'Support (30d, 1h close)',
      tier: 'bearish',
      reading: supportGap === null ? '—' : `${formatPercent(supportGap, 1)} above line`,
      threshold: `line = support − ${t.support_buffer_atr} ATR`,
      // Closer to the line reads as nearer the trigger; 10% above is "far".
      progress: supportGap === null ? null : 1 - Math.min(1, Math.max(0, supportGap) / 0.1),
      active: active.has('support_breakdown')
    },
    {
      key: 'guard_exit',
      name: 'Overbought guard exit',
      tier: 'bearish',
      reading: m.guard_phase ?? '—',
      threshold: `${formatPercent(t.guard_trailing_pct, 0)} off the armed peak`,
      progress: null,
      active: active.has('guard_exit')
    }
  ];
}

function rowTone(row: SignalRow): CatalogTone {
  if (row.active) return row.tier === 'bearish' ? 'bad' : 'warn';
  if (row.progress !== null && row.progress >= 0.75) return 'warn';
  return 'good';
}

function RegimeBody({ regime }: { regime: MarketRegime }) {
  const tone = PHASE_TONE[regime.phase];
  const rows = signalRows(regime);
  return (
    <CardContent className='space-y-4'>
      <div className={cn('rounded-lg border p-3', toneSurface[tone])}>
        <p className={cn('text-sm font-medium', toneText[tone])}>{REGIME_EFFECTS[regime.phase]}</p>
        <p className='text-muted-foreground mt-1 text-xs'>
          {regime.reasons.length
            ? `Triggered by ${regime.reasons.map(regimeReasonLabel).join(', ')}`
            : 'No warning or bearish signal'}
          {regime.entered_at ? ` · since ${formatDate(regime.entered_at)}` : ''}
          {regime.metrics.price ? ` · BTC ${fixed(regime.metrics.price, 0)}` : ''}
          {` · evaluated ${relativeTime(regime.last_evaluated_at)}`}
        </p>
      </div>

      {regime.last_error ? (
        <ToneNote
          tone='warn'
          title='Last BTC evaluation failed; phase unchanged'
          icon={<Icons.warning className='size-4' />}
        >
          {regime.last_error}
        </ToneNote>
      ) : null}

      <div className='grid gap-4 lg:grid-cols-2'>
        <div className='min-w-0 space-y-2'>
          <p className='text-muted-foreground text-[11px] font-medium tracking-wide uppercase'>
            BTC signals
          </p>
          <ul className='divide-y rounded-lg border'>
            {rows.map((row) => {
              const rowToneValue = rowTone(row);
              return (
                <li key={row.key} className='space-y-1.5 p-2.5'>
                  <div className='flex flex-wrap items-center justify-between gap-x-3 gap-y-1'>
                    <span className='min-w-0 text-sm font-medium'>{row.name}</span>
                    <span className='flex items-center gap-1.5'>
                      <Badge variant='outline' className='text-[10px] capitalize'>
                        {row.tier}
                      </Badge>
                      {row.active ? <ToneBadge tone={rowToneValue}>firing</ToneBadge> : null}
                    </span>
                  </div>
                  <div className='text-muted-foreground flex flex-wrap justify-between gap-x-3 text-xs tabular-nums'>
                    <span>{row.reading}</span>
                    <span>trigger {row.threshold}</span>
                  </div>
                  {row.progress !== null ? (
                    <Meter value={Math.min(1, row.progress)} max={1} tone={rowToneValue} />
                  ) : null}
                </li>
              );
            })}
          </ul>
        </div>

        <div className='min-w-0 space-y-4'>
          {regime.recovery ? (
            <div className='space-y-2'>
              <p className='text-muted-foreground text-[11px] font-medium tracking-wide uppercase'>
                Recovery checklist
              </p>
              <ul className='space-y-1.5'>
                {regime.recovery.map((item) => (
                  <li key={item.check} className='flex items-start gap-2 text-sm'>
                    {item.passed ? (
                      <Icons.check className={cn('mt-0.5 size-4 shrink-0', toneText.good)} />
                    ) : (
                      <Icons.close className={cn('mt-0.5 size-4 shrink-0', toneText.bad)} />
                    )}
                    <span className={item.passed ? undefined : 'text-muted-foreground'}>
                      {RECOVERY_CHECK_LABELS[item.check] ?? item.check}
                    </span>
                  </li>
                ))}
              </ul>
              <p className='text-muted-foreground text-[11px]'>
                Resumes once every check passes and {regime.thresholds.min_bearish_hours}h have
                passed. Only sessions the regime paused resume.
              </p>
            </div>
          ) : null}

          <div className='space-y-2'>
            <p className='text-muted-foreground text-[11px] font-medium tracking-wide uppercase'>
              Recent transitions
            </p>
            {regime.transitions.length ? (
              <ul className='space-y-1.5'>
                {regime.transitions.slice(0, 5).map((item) => (
                  <li
                    key={`${item.time}-${item.to}`}
                    className='flex flex-wrap items-center gap-x-2 gap-y-1 text-xs'
                  >
                    <span className='text-muted-foreground tabular-nums'>
                      {formatDate(item.time)}
                    </span>
                    <span className='capitalize'>{item.from ?? '—'}</span>
                    <Icons.chevronRight className='text-muted-foreground size-3' />
                    <Badge
                      variant='outline'
                      className={cn(
                        'capitalize',
                        item.to ? toneBadge[PHASE_TONE[item.to]] : undefined
                      )}
                    >
                      {item.to ?? '—'}
                    </Badge>
                    {item.reasons.length ? (
                      <span className='text-muted-foreground min-w-0'>
                        {item.reasons.map(regimeReasonLabel).join(', ')}
                      </span>
                    ) : null}
                  </li>
                ))}
              </ul>
            ) : (
              <p className='text-muted-foreground text-xs'>No phase changes recorded yet.</p>
            )}
          </div>
        </div>
      </div>
    </CardContent>
  );
}

export default function MarketRegimeCard() {
  const query = useMarketRegime();
  const regime = query.data;
  const off = regime ? !regime.enabled : false;
  const tone: CatalogTone = !regime || off ? 'neutral' : PHASE_TONE[regime.phase];

  return (
    <Card className={cn('overflow-hidden', toneSurface[tone])}>
      <CardHeader>
        <div className='flex flex-wrap items-start justify-between gap-3'>
          <div className='min-w-0'>
            <CardTitle className='flex items-center gap-2 text-base'>
              <Icons.shield className={cn('size-4', toneText[tone])} />
              BTC market regime
            </CardTitle>
            <CardDescription>
              BTC is checked every paper tick. A warning blocks new buys on every coin; a bearish
              regime sells every active session to USDT and pauses it until BTC recovers. Applies in
              normal and hybrid mode, and the backtest models the same rules.
            </CardDescription>
          </div>
          {regime ? (
            <Badge
              variant='outline'
              className={cn('shrink-0 capitalize', off ? undefined : toneBadge[tone])}
            >
              {off ? 'off' : regime.phase}
            </Badge>
          ) : null}
        </div>
      </CardHeader>
      {query.isError ? (
        <CardContent>
          <ToneNote
            tone='bad'
            title='Regime status is unavailable'
            icon={<Icons.warning className='size-4' />}
          >
            {(query.error as Error).message}
          </ToneNote>
        </CardContent>
      ) : !regime ? (
        <CardContent>
          <p className='text-muted-foreground text-sm'>Loading BTC regime…</p>
        </CardContent>
      ) : off ? (
        <CardContent>
          <p className='text-muted-foreground text-sm'>
            Regime protection is switched off in the settings below. Sessions trade without the BTC
            check.
          </p>
        </CardContent>
      ) : (
        <RegimeBody regime={regime} />
      )}
    </Card>
  );
}
