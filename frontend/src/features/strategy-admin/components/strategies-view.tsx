'use client';

import { Icons } from '@/components/icons';
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle
} from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { NativeSelect, NativeSelectOption } from '@/components/ui/native-select';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow
} from '@/components/ui/table';
import { cn } from '@/lib/utils';
import { formatCompactUsd, formatDate, formatPercent } from '@/features/grid/api';
import EmptyPanel from '@/features/grid/components/empty-panel';
import MetricCard from '@/features/grid/components/metric-card';
import StatusBadge from '@/features/grid/components/status-badge';
import { FACTOR_LABELS, formatGateValue } from '@/features/grid/diagnostics';
import { useCoinResultDetail, useDashboard, useResults } from '@/features/grid/queries';
import type { CoinResult, Diagnostics } from '@/features/grid/types';
import { useState } from 'react';import type { ReactNode } from 'react';
import {
  PARAM_LABELS,
  PARAM_PERCENT_KEYS,
  STRATEGY_SECTIONS,
  THRESHOLD_ROWS
} from '../catalog';
import type { CatalogBlock, CatalogSection } from '../catalog';

const SUITABILITY_OPTIONS = ['all', 'prime', 'viable', 'watch', 'avoid', 'ineligible'] as const;

function percentMetric(metrics: Record<string, unknown> | undefined, key: string): string {
  const value = metrics?.[key];
  const parsed = typeof value === 'string' ? Number(value) : value;
  return typeof parsed === 'number' && Number.isFinite(parsed) ? formatPercent(parsed) : '—';
}

function numberMetric(metrics: Record<string, unknown> | undefined, key: string): string {
  const value = metrics?.[key];
  const parsed = typeof value === 'string' ? Number(value) : value;
  if (typeof parsed === 'number' && Number.isFinite(parsed)) {
    return Number.isInteger(parsed) ? parsed.toLocaleString() : parsed.toFixed(2);
  }
  return '—';
}

function formatThreshold(value: unknown, format: 'usd' | 'percent' | 'number' | 'multiplier'): string {
  const parsed = typeof value === 'string' ? Number(value) : value;
  if (typeof parsed !== 'number' || !Number.isFinite(parsed)) return '—';
  if (format === 'usd') return formatCompactUsd(parsed);
  if (format === 'percent') return formatPercent(parsed, 0);
  if (format === 'multiplier') return `×${parsed.toFixed(2)}`;
  return Number.isInteger(parsed) ? parsed.toLocaleString() : parsed.toFixed(2);
}

function paramValue(key: string, value: unknown): string {
  if (key === 'max_total_drawdown' && value === 0) return 'off';
  if (typeof value === 'number') {
    if (PARAM_PERCENT_KEYS.has(key)) return formatPercent(value, 0);
    return Number.isInteger(value) ? String(value) : value.toFixed(2);
  }
  if (typeof value === 'boolean') return value ? 'yes' : 'no';
  return String(value).replace(/_/g, ' ');
}

const PARAM_ORDER = Object.keys(PARAM_LABELS);

function orderedParams(params: Record<string, number | string>) {
  return Object.entries(params).toSorted(([left], [right]) => {
    const leftIndex = PARAM_ORDER.indexOf(left);
    const rightIndex = PARAM_ORDER.indexOf(right);
    return (leftIndex === -1 ? PARAM_ORDER.length : leftIndex) -
      (rightIndex === -1 ? PARAM_ORDER.length : rightIndex);
  });
}

function paramText(
  params: Record<string, number | string> | undefined,
  key: string,
  suffix = ''
): string {
  const value = params?.[key];
  return value === undefined || value === null || value === '' ? '—' : `${value}${suffix}`;
}

function Stat({
  label,
  value,
  tone
}: {
  label: string;
  value: ReactNode;
  tone?: 'good' | 'bad' | 'muted';
}) {
  return (
    <div className='rounded-lg border p-2.5'>
      <p className='text-muted-foreground text-[11px] font-medium tracking-wide uppercase'>
        {label}
      </p>
      <p
        className={cn(
          'mt-0.5 text-sm font-medium tabular-nums',
          tone === 'good' && 'text-emerald-600 dark:text-emerald-400',
          tone === 'bad' && 'text-rose-600 dark:text-rose-400',
          tone === 'muted' && 'text-muted-foreground'
        )}
      >
        {value}
      </p>
    </div>
  );
}

function CatalogBlockView({ block }: { block: CatalogBlock }) {
  const className = block.fullWidth ? 'xl:col-span-2' : undefined;
  if (block.kind === 'table') {
    return (
      <Card className={className}>
        <CardHeader>
          <CardTitle className='text-base'>{block.title}</CardTitle>
          <CardDescription>{block.description}</CardDescription>
        </CardHeader>
        <CardContent>
          <div className='overflow-x-auto'>
            <Table>
              <TableHeader>
                <TableRow>
                  {block.columns.map((column) => (
                    <TableHead key={column}>{column}</TableHead>
                  ))}
                </TableRow>
              </TableHeader>
              <TableBody>
                {block.rows.map((row) => (
                  <TableRow key={row[0]}>
                    {row.map((cell, index) => (
                      <TableCell
                        key={`${row[0]}-${index}`}
                        className={cn(
                          'align-top text-sm',
                          index === 0 && 'font-medium',
                          index > 0 && 'text-muted-foreground'
                        )}
                      >
                        {cell}
                      </TableCell>
                    ))}
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        </CardContent>
      </Card>
    );
  }

  return (
    <Card className={className}>
      <CardHeader>
        <CardTitle className='text-base'>{block.title}</CardTitle>
        <CardDescription>{block.description}</CardDescription>
      </CardHeader>
      <CardContent>
        <div className={cn('grid gap-2', block.fullWidth && 'md:grid-cols-2')}>
          {block.rows.map((row) => (
            <div key={row.name} className='rounded-lg border p-3'>
              <div className='flex items-start justify-between gap-3'>
                <p className='text-sm font-medium'>{row.name}</p>
                {row.value ? (
                  <Badge variant='outline' className='shrink-0 font-mono text-[11px]'>
                    {row.value}
                  </Badge>
                ) : null}
              </div>
              <p className='text-muted-foreground mt-1 text-xs leading-relaxed'>{row.detail}</p>
            </div>
          ))}
        </div>
      </CardContent>
    </Card>
  );
}

function SectionHeading({ section, index }: { section: CatalogSection; index: number }) {
  return (
    <div id={section.id} className='scroll-mt-24 space-y-1 border-b pb-3'>
      <p className='text-muted-foreground text-xs font-medium tracking-wide uppercase'>
        Section {index}
      </p>
      <h2 className='text-xl font-semibold tracking-tight'>{section.title}</h2>
      <p className='text-muted-foreground max-w-4xl text-sm'>{section.description}</p>
    </div>
  );
}

function GateList({ diagnostics }: { diagnostics: Diagnostics | null | undefined }) {
  const gates = diagnostics?.hard_gates ?? [];
  if (!gates.length) {
    return <p className='text-muted-foreground text-sm'>No gate diagnostics recorded.</p>;
  }
  return (
    <div className='space-y-1.5 text-sm'>
      {gates.map((gate) => (
        <div key={gate.name} className='flex items-start justify-between gap-3'>
          <span className='text-muted-foreground'>
            {gate.label}
            {gate.reason ? (
              <span className='block text-xs text-rose-500'>{gate.reason}</span>
            ) : null}
          </span>
          <span
            className={cn(
              'shrink-0 tabular-nums',
              gate.passed ? 'text-emerald-600 dark:text-emerald-400' : 'text-rose-600'
            )}
          >
            {gate.passed ? 'pass' : 'fail'} · {formatGateValue(gate.value)}
          </span>
        </div>
      ))}
    </div>
  );
}

function CoinDetailDialog({
  result,
  open,
  onOpenChange
}: {
  result: CoinResult | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const full = useCoinResultDetail(open && result ? result.id : null).data;
  const diagnostics = full?.diagnostics ?? result?.diagnostics;
  const oos = full?.oos_metrics ?? result?.oos_metrics;
  const params = full?.params ?? result?.params ?? {};
  const folds = full?.folds ?? result?.folds ?? [];
  const regime = diagnostics?.regime;
  const costStress = diagnostics?.cost_stress;
  const sensitivity = diagnostics?.sensitivity;
  const exposure = diagnostics?.exposure;
  const execution = diagnostics?.execution;
  const stability = diagnostics?.return_stability;
  const correlation = diagnostics?.correlation;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className='max-h-[85vh] overflow-y-auto sm:max-w-3xl'>
        <DialogHeader>
          <DialogTitle>{result?.coin.symbol ?? 'Coin'} strategy configuration</DialogTitle>
          <DialogDescription>
            {result ? (
              <>
                {result.coin.name} · rank {result.rank ?? '—'} · score {result.score.toFixed(1)} ·{' '}
                {result.eligible ? 'eligible' : 'ineligible'}
              </>
            ) : null}
          </DialogDescription>
        </DialogHeader>

        {result ? (
          <div className='space-y-5'>
            <div>
              <h3 className='mb-2 text-sm font-medium'>Walk-forward result</h3>
              <div className='grid grid-cols-2 gap-2 sm:grid-cols-4'>
                <Stat
                  label='OOS return'
                  value={percentMetric(oos, 'net_return')}
                  tone={Number(oos?.net_return ?? 0) >= 0 ? 'good' : 'bad'}
                />
                <Stat label='Max drawdown' value={percentMetric(oos, 'max_drawdown')} />
                <Stat label='Profit factor' value={numberMetric(oos, 'profit_factor')} />
                <Stat label='OOS fills' value={numberMetric(oos, 'trades')} />
                <Stat
                  label='Cost stress'
                  value={percentMetric(oos, 'cost_stress_return')}
                  tone={costStress?.profitable ? 'good' : 'bad'}
                />
                <Stat
                  label='Regime score'
                  value={regime?.robustness_score !== undefined ? regime.robustness_score.toFixed(1) : '—'}
                />
                <Stat label='Peak exposure' value={`${exposure?.max_exposure_ratio?.toFixed(2) ?? '—'}×`} />
                <Stat
                  label='Recovery'
                  value={diagnostics?.recovery_days !== undefined ? `${diagnostics.recovery_days.toFixed(0)}d` : '—'}
                />
              </div>
            </div>

            <div>
              <h3 className='mb-2 text-sm font-medium'>Recommended parameters</h3>
              <div className='grid grid-cols-2 gap-x-4 gap-y-1.5 sm:grid-cols-3'>
                {orderedParams(params).map(([key, value]) => (
                  <div key={key} className='flex items-baseline justify-between gap-2 text-sm'>
                    <span className='text-muted-foreground'>
                      {PARAM_LABELS[key] ?? key.replace(/_/g, ' ')}
                    </span>
                    <span className='font-medium tabular-nums'>{paramValue(key, value)}</span>
                  </div>
                ))}
              </div>
            </div>

            <div>
              <h3 className='mb-2 text-sm font-medium'>Eligibility gates</h3>
              <GateList diagnostics={diagnostics} />
            </div>

            <div>
              <h3 className='mb-2 text-sm font-medium'>Review flags</h3>
              {diagnostics?.flags?.length ? (
                <ul className='space-y-1.5 text-sm'>
                  {diagnostics.flags.map((flag) => (
                    <li key={flag.code} className='flex gap-2'>
                      <Icons.warning
                        className={cn(
                          'mt-0.5 size-3.5 shrink-0',
                          flag.severity === 'critical' ? 'text-rose-500' : 'text-amber-500'
                        )}
                      />
                      <span>{flag.message}</span>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className='text-muted-foreground text-sm'>No review flags.</p>
              )}
            </div>

            <div className='grid gap-4 sm:grid-cols-2'>
              <div>
                <h3 className='mb-2 text-sm font-medium'>Score contributions</h3>
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Factor</TableHead>
                      <TableHead className='text-right'>Score</TableHead>
                      <TableHead className='text-right'>Weight</TableHead>
                      <TableHead className='text-right'>Points</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {Object.entries(diagnostics?.score_breakdown?.factors ?? {}).map(([key, factor]) => (
                      <TableRow key={key}>
                        <TableCell className='text-sm'>
                          {FACTOR_LABELS[key] ?? key.replace(/_/g, ' ')}
                        </TableCell>
                        <TableCell className='text-right text-sm tabular-nums'>
                          {factor.score.toFixed(1)}
                        </TableCell>
                        <TableCell className='text-right text-sm tabular-nums'>
                          {(factor.weight * 100).toFixed(0)}%
                        </TableCell>
                        <TableCell className='text-right text-sm tabular-nums'>
                          {factor.contribution.toFixed(1)}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
                {diagnostics?.score_breakdown?.penalties?.length ? (
                  <ul className='text-muted-foreground mt-2 space-y-1 text-xs'>
                    {diagnostics.score_breakdown.penalties.map((penalty) => (
                      <li key={penalty.code}>
                        −{penalty.points.toFixed(1)} · {penalty.reason}
                      </li>
                    ))}
                  </ul>
                ) : null}
              </div>

              <div>
                <h3 className='mb-2 text-sm font-medium'>Regime robustness</h3>
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Regime</TableHead>
                      <TableHead className='text-right'>Folds</TableHead>
                      <TableHead className='text-right'>Mean ret.</TableHead>
                      <TableHead className='text-right'>Win share</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {Object.entries(regime?.regimes ?? {}).map(([label, bucket]) => (
                      <TableRow key={label}>
                        <TableCell className='text-sm capitalize'>{label.replace(/_/g, ' ')}</TableCell>
                        <TableCell className='text-right text-sm tabular-nums'>{bucket.folds}</TableCell>
                        <TableCell className='text-right text-sm tabular-nums'>
                          {formatPercent(bucket.mean_return)}
                        </TableCell>
                        <TableCell className='text-right text-sm tabular-nums'>
                          {formatPercent(bucket.profitable, 0)}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
                <p className='text-muted-foreground mt-2 text-xs'>
                  {regime?.regimes_covered ?? 0} regimes · dominant{' '}
                  {regime?.dominant_regime ?? '—'} · dependence{' '}
                  {regime?.dominant_share !== undefined
                    ? formatPercent(regime.dominant_share, 0)
                    : '—'}
                </p>
              </div>
            </div>

            <div className='grid gap-3 sm:grid-cols-2'>
              <div>
                <h3 className='mb-1 text-sm font-medium'>Cost stress</h3>
                <p className='text-muted-foreground text-sm'>
                  {costStress?.multiplier ?? 2}× costs · stressed net{' '}
                  {formatPercent(costStress?.stress_return ?? 0)} · delta{' '}
                  {formatPercent(costStress?.stress_delta ?? 0)} ·{' '}
                  {costStress?.profitable ? 'profitable' : 'fails'}
                </p>
              </div>
              <div>
                <h3 className='mb-1 text-sm font-medium'>Parameter sensitivity</h3>
                <p className='text-muted-foreground text-sm'>
                  score {sensitivity?.parameter_sensitivity?.toFixed(2) ?? '—'} · mean neighbour{' '}
                  {formatPercent(sensitivity?.mean_neighbor_return ?? 0)} · worst{' '}
                  {formatPercent(sensitivity?.worst_neighbor_return ?? 0)} ·{' '}
                  {sensitivity?.runs?.length ?? 0} variants
                </p>
              </div>
              <div>
                <h3 className='mb-1 text-sm font-medium'>Inventory & recovery</h3>
                <p className='text-muted-foreground text-sm'>
                  peak {exposure?.max_exposure_ratio?.toFixed(2) ?? '—'}× · avg{' '}
                  {exposure?.avg_exposure_ratio?.toFixed(2) ?? '—'}× · unrealized loss{' '}
                  {formatPercent(exposure?.max_unrealized_loss_pct ?? 0)} · drawdown duration{' '}
                  {diagnostics?.max_drawdown_duration_days?.toFixed(0) ?? '—'}d
                </p>
              </div>
              <div>
                <h3 className='mb-1 text-sm font-medium'>Execution quality</h3>
                <p className='text-muted-foreground text-sm'>
                  slippage ${execution?.slippage_paid?.toFixed(2) ?? '—'} · buy/sell fills{' '}
                  {execution?.buy_fills ?? '—'}/{execution?.sell_fills ?? '—'} · cycles{' '}
                  {formatPercent(execution?.cycle_completion_ratio ?? 0, 0)}
                </p>
              </div>
              <div>
                <h3 className='mb-1 text-sm font-medium'>Return stability</h3>
                <p className='text-muted-foreground text-sm'>
                  top trade {formatPercent(stability?.top_trade_share ?? 0, 0)} · top five{' '}
                  {formatPercent(stability?.top5_trade_share ?? 0, 0)} · fold spread{' '}
                  {formatPercent(stability?.fold_return_std ?? 0)}
                </p>
              </div>
              <div>
                <h3 className='mb-1 text-sm font-medium'>Correlation</h3>
                <p className='text-muted-foreground text-sm'>
                  avg {correlation?.avg_correlation?.toFixed(2) ?? '—'} · peak{' '}
                  {correlation?.max_correlation?.toFixed(2) ?? '—'}
                  {correlation?.peers?.length
                    ? ` · ${correlation.peers
                        .map((peer) => `${peer.symbol} ${peer.correlation.toFixed(2)}`)
                        .join(', ')}`
                    : ''}
                </p>
              </div>
            </div>

            {diagnostics?.eligibility_reasons?.length ? (
              <div>
                <h3 className='mb-1 text-sm font-medium'>Rejection reasons</h3>
                <ul className='text-muted-foreground list-disc space-y-1 pl-5 text-sm'>
                  {diagnostics.eligibility_reasons.map((reason) => (
                    <li key={reason}>{reason}</li>
                  ))}
                </ul>
              </div>
            ) : null}

            {folds.length ? (
              <div>
                <h3 className='mb-2 text-sm font-medium'>Walk-forward folds</h3>
                <div className='overflow-x-auto'>
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>Fold</TableHead>
                        <TableHead>Test period</TableHead>
                        <TableHead>Regime</TableHead>
                        <TableHead className='text-right'>Return</TableHead>
                        <TableHead className='text-right'>Max DD</TableHead>
                        <TableHead className='text-right'>Stress</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {folds.map((fold, index) => {
                        const regimeLabel = (fold.regime as { label?: string } | undefined)?.label;
                        const test = fold.test_metrics as Record<string, unknown> | undefined;
                        const stress = fold.stress as { net_return?: number } | undefined;
                        return (
                          <TableRow key={index}>
                            <TableCell className='text-sm tabular-nums'>
                              {String(fold.fold ?? index + 1)}
                            </TableCell>
                            <TableCell className='text-muted-foreground text-xs'>
                              {typeof fold.test_start === 'string'
                                ? fold.test_start.slice(0, 10)
                                : '—'}{' '}
                              →{' '}
                              {typeof fold.test_end === 'string'
                                ? fold.test_end.slice(0, 10)
                                : '—'}
                            </TableCell>
                            <TableCell className='text-sm capitalize'>
                              {regimeLabel?.replace(/_/g, ' ') ?? '—'}
                            </TableCell>
                            <TableCell className='text-right text-sm tabular-nums'>
                              {percentMetric(test, 'net_return')}
                            </TableCell>
                            <TableCell className='text-right text-sm tabular-nums'>
                              {percentMetric(test, 'max_drawdown')}
                            </TableCell>
                            <TableCell className='text-right text-sm tabular-nums'>
                              {stress?.net_return !== undefined
                                ? formatPercent(stress.net_return)
                                : '—'}
                            </TableCell>
                          </TableRow>
                        );
                      })}
                    </TableBody>
                  </Table>
                </div>
              </div>
            ) : null}
          </div>
        ) : null}
      </DialogContent>
    </Dialog>
  );
}

export default function StrategiesView() {
  const dashboard = useDashboard();
  const resultsQuery = useResults({ limit: 500 });
  const [search, setSearch] = useState('');
  const [suitability, setSuitability] = useState<(typeof SUITABILITY_OPTIONS)[number]>('all');
  const [detail, setDetail] = useState<CoinResult | null>(null);

  const latest = dashboard.data?.engine.latest_analysis ?? null;
  const summary = (latest?.summary ?? {}) as Record<string, unknown>;
  const thresholds = (summary.thresholds ?? {}) as Record<string, unknown>;
  const results = resultsQuery.data?.results ?? [];

  const needle = search.trim().toLowerCase();
  const rows = results.filter((row) => {
    const matchesSearch =
      !needle ||
      row.coin.symbol.toLowerCase().includes(needle) ||
      row.coin.name.toLowerCase().includes(needle);
    const matchesClass = suitability === 'all' || row.suitability === suitability;
    return matchesSearch && matchesClass;
  });

  const eligible = Number(summary.eligible ?? 0);
  const universe = Number(summary.universe ?? results.length);
  const flagged = Number(summary.flagged ?? 0);
  const avgScore = summary.avg_eligible_score as number | null | undefined;
  const avgBaseline = summary.avg_baseline_score as number | null | undefined;
  const regimeCounts = (summary.regime_counts ?? {}) as Record<string, number>;
  const topRegime = Object.entries(regimeCounts).toSorted((a, b) => b[1] - a[1])[0];

  return (
    <div className='space-y-6'>
      <Alert>
        <Icons.lock />
        <AlertTitle>Internal strategy reference</AlertTitle>
        <AlertDescription>
          This page documents every analysis, grid, backtest, and risk strategy the engine runs,
          plus the parameters selected for each coin in the latest run. It is intentionally not
          linked from the dashboard navigation.
        </AlertDescription>
      </Alert>

      {dashboard.isError ? (
        <Alert variant='destructive'>
          <Icons.warning />
          <AlertTitle>Engine API is not reachable</AlertTitle>
          <AlertDescription>
            {(dashboard.error as Error).message}. Static strategy documentation is still shown
            below; live thresholds and coin settings will be unavailable.
          </AlertDescription>
        </Alert>
      ) : null}

      <div className='grid gap-4 md:grid-cols-2 xl:grid-cols-4'>
        <MetricCard
          title='Latest analysis'
          value={<StatusBadge value={latest?.status ?? 'none'} />}
          hint={
            latest
              ? `${latest.trigger} · ${formatDate(latest.started_at)} · ${latest.duration_seconds?.toFixed(1) ?? '—'}s`
              : 'Loading latest run…'
          }
          icon={<Icons.activity />}
        />
        <MetricCard
          title='Eligible universe'
          value={`${eligible} / ${universe || '—'}`}
          hint={`${results.length} coin results loaded · ${flagged} flagged`}
          icon={<Icons.coins />}
        />
        <MetricCard
          title='Average score'
          value={avgScore !== undefined && avgScore !== null ? avgScore.toFixed(1) : '—'}
          hint={
            avgScore !== undefined &&
            avgScore !== null &&
            avgBaseline !== undefined &&
            avgBaseline !== null
              ? `Baseline ${avgBaseline.toFixed(1)} · change ${(avgScore - avgBaseline >= 0 ? '+' : '') + (avgScore - avgBaseline).toFixed(1)}`
              : 'Baseline comparison unavailable'
          }
          icon={<Icons.chart />}
        />
        <MetricCard
          title='Dominant regime'
          value={topRegime ? topRegime[0].replace(/_/g, ' ') : '—'}
          hint={topRegime ? `${topRegime[1]} coins led by this regime` : 'No regime data yet'}
          icon={<Icons.grid />}
        />
      </div>

      <nav aria-label='Strategy sections' className='flex flex-wrap gap-2'>
        <a
          href='#coin-settings'
          className='bg-primary/10 text-primary hover:bg-primary/15 rounded-full border border-primary/20 px-3 py-1 text-xs font-medium'
        >
          Coin-specific settings
        </a>
        {STRATEGY_SECTIONS.map((section) => (
          <a
            key={section.id}
            href={`#${section.id}`}
            className='bg-muted/50 hover:bg-muted rounded-full border px-3 py-1 text-xs font-medium'
          >
            {section.title}
          </a>
        ))}
      </nav>

      <section className='space-y-4'>
        <div id='coin-settings' className='scroll-mt-24 space-y-1 border-b pb-3'>
          <p className='text-muted-foreground text-xs font-medium tracking-wide uppercase'>
            Section 1 · Live data
          </p>
          <h2 className='text-xl font-semibold tracking-tight'>Coin-specific settings</h2>
          <p className='text-muted-foreground max-w-4xl text-sm'>
            Thresholds used by the latest completed run and the ladder, range, overlay, and
            rebalancing parameters walk-forward selected for each coin. Open a coin to see gates,
            flags, regime coverage, cost stress, sensitivity, and fold-by-fold results.
          </p>
        </div>

        <Card>
          <CardHeader>
            <CardTitle className='text-base'>Runtime thresholds</CardTitle>
            <CardDescription>
              Read from the latest analysis summary. Values are environment-configured and can
              change between runs.
            </CardDescription>
          </CardHeader>
          <CardContent>
            {Object.keys(thresholds).length === 0 ? (
              <p className='text-muted-foreground text-sm'>
                No thresholds recorded on the latest run. Run a new analysis to persist them.
              </p>
            ) : (
              <div className='grid gap-2 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4'>
                {THRESHOLD_ROWS.map((row) => (
                  <div key={row.key} className='rounded-lg border p-2.5'>
                    <p className='text-muted-foreground text-[11px] font-medium tracking-wide uppercase'>
                      {row.label}
                    </p>
                    <p className='mt-0.5 text-sm font-semibold tabular-nums'>
                      {formatThreshold(thresholds[row.key], row.format)}
                    </p>
                  </div>
                ))}
              </div>
            )}
            {latest ? (
              <p className='text-muted-foreground mt-3 text-xs'>
                Run {String(latest.id).slice(0, 8)} · {latest.scope} scope ·{' '}
                {String(summary.lookback_days ?? '—')}d lookback · {String(summary.interval ?? '—')}{' '}
                interval · {String(summary.data_errors ?? 0)} data errors
              </p>
            ) : null}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <div className='flex flex-wrap items-start justify-between gap-3'>
              <div>
                <CardTitle className='text-base'>Recommended parameters per coin</CardTitle>
                <CardDescription>
                  Selected on train windows only; diagnostics come from untouched test folds.
                </CardDescription>
              </div>
              <div className='flex flex-wrap items-center gap-2'>
                <div className='relative'>
                  <Icons.search className='text-muted-foreground pointer-events-none absolute top-1/2 left-2 size-4 -translate-y-1/2' />
                  <Input
                    value={search}
                    onChange={(event) => setSearch(event.target.value)}
                    placeholder='Search symbol or name'
                    aria-label='Search coins'
                    className='h-8 w-56 pl-8'
                  />
                </div>
                <NativeSelect
                  size='sm'
                  aria-label='Filter by suitability class'
                  value={suitability}
                  onChange={(event) =>
                    setSuitability(event.target.value as (typeof SUITABILITY_OPTIONS)[number])
                  }
                  className='w-40'
                >
                  {SUITABILITY_OPTIONS.map((option) => (
                    <NativeSelectOption key={option} value={option}>
                      {option === 'all' ? 'All classes' : option.replace(/_/g, ' ')}
                    </NativeSelectOption>
                  ))}
                </NativeSelect>
              </div>
            </div>
          </CardHeader>
          <CardContent>
            {resultsQuery.isError ? (
              <EmptyPanel
                title='Coin results are not reachable'
                description={(resultsQuery.error as Error).message}
                action={<Button onClick={() => resultsQuery.refetch()}>Retry</Button>}
              />
            ) : resultsQuery.isLoading ? (
              <p className='text-muted-foreground py-6 text-center text-sm'>Loading coin results…</p>
            ) : rows.length === 0 ? (
              <EmptyPanel
                title={results.length === 0 ? 'No analysis results yet' : 'No coins match the filter'}
                description={
                  results.length === 0
                    ? 'Run the daily analysis to generate walk-forward results and per-coin parameters.'
                    : 'Adjust the search text or suitability filter.'
                }
              />
            ) : (
              <div className='overflow-x-auto'>
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Coin</TableHead>
                      <TableHead>Class</TableHead>
                      <TableHead className='text-right'>Score</TableHead>
                      <TableHead>Ladder</TableHead>
                      <TableHead>Range</TableHead>
                      <TableHead>Rebalance</TableHead>
                      <TableHead>Stop / TP</TableHead>
                      <TableHead>Regime</TableHead>
                      <TableHead className='text-right'>Stress</TableHead>
                      <TableHead className='text-right'> </TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {rows.map((row) => {
                      const params = row.params ?? {};
                      const regime = row.diagnostics?.regime;
                      return (
                        <TableRow key={row.id}>
                          <TableCell>
                            <div className='font-medium'>{row.coin.symbol}</div>
                            <div className='text-muted-foreground text-xs'>{row.coin.name}</div>
                          </TableCell>
                          <TableCell>
                            <StatusBadge value={row.suitability} />
                          </TableCell>
                          <TableCell className='text-right font-semibold tabular-nums'>
                            {row.score.toFixed(1)}
                          </TableCell>
                          <TableCell className='text-muted-foreground text-xs'>
                            {paramText(params, 'grid_levels')} ·{' '}
                            {paramText(params, 'spacing_mode')}
                          </TableCell>
                          <TableCell className='text-muted-foreground text-xs'>
                            {paramText(params, 'range_mode').replace(/_/g, ' ')} ·{' '}
                            {paramText(params, 'range_lookback_days', 'd')}
                          </TableCell>
                          <TableCell className='text-muted-foreground text-xs'>
                            {paramText(params, 'rebalance_mode')} ·{' '}
                            {paramText(params, 'rebalance_days', 'd')}
                          </TableCell>
                          <TableCell className='text-muted-foreground text-xs'>
                            {paramText(params, 'stop_atr_multiple')} /{' '}
                            {paramText(params, 'tp_atr_multiple')} ATR
                          </TableCell>
                          <TableCell className='text-muted-foreground text-xs capitalize'>
                            {regime?.dominant_regime?.replace(/_/g, ' ') ?? '—'}
                            {regime?.regimes_covered !== undefined
                              ? ` · ${regime.regimes_covered}`
                              : ''}
                          </TableCell>
                          <TableCell
                            className={cn(
                              'text-right text-xs tabular-nums',
                              Number(row.oos_metrics?.cost_stress_return ?? 0) >= 0
                                ? 'text-emerald-600 dark:text-emerald-400'
                                : 'text-rose-600 dark:text-rose-400'
                            )}
                          >
                            {percentMetric(row.oos_metrics, 'cost_stress_return')}
                          </TableCell>
                          <TableCell className='text-right'>
                            <Button
                              variant='ghost'
                              size='sm'
                              className='h-7 px-2'
                              aria-label={`Open strategy details for ${row.coin.symbol}`}
                              onClick={() => setDetail(row)}
                            >
                              Details
                            </Button>
                          </TableCell>
                        </TableRow>
                      );
                    })}
                  </TableBody>
                </Table>
              </div>
            )}
            <p className='text-muted-foreground mt-3 text-xs'>
              Showing {rows.length} of {results.length} coin results. Stops and take-profits are
              ATR multiples; the stop distance also includes structural support invalidation.
            </p>
          </CardContent>
        </Card>
      </section>

      {STRATEGY_SECTIONS.map((section, index) => (
        <section key={section.id} className='space-y-4'>
          <SectionHeading section={section} index={index + 2} />
          <div className='grid gap-4 xl:grid-cols-2'>
            {section.blocks.map((block) => (
              <CatalogBlockView key={block.title} block={block} />
            ))}
          </div>
        </section>
      ))}

      <CoinDetailDialog
        result={detail}
        open={Boolean(detail)}
        onOpenChange={(open) => !open && setDetail(null)}
      />
    </div>
  );
}
