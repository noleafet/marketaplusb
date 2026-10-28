'use client';

import { Icons } from '@/components/icons';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
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
import { formatDate } from '@/features/grid/api';
import EmptyPanel from '@/features/grid/components/empty-panel';
import StatusBadge from '@/features/grid/components/status-badge';
import { useDashboard, useResults } from '@/features/grid/queries';
import type { CoinResult } from '@/features/grid/types';
import { cn } from '@/lib/utils';
import { useState } from 'react';
import { THRESHOLD_ROWS } from '../catalog';
import type { CatalogTone } from '../catalog';
import {
  formatThreshold,
  paramText,
  percentMetric,
  regimeTone,
  returnTone,
  scoreTone,
  signedPercent,
  toNumber
} from '../format';
import CoinDetailDialog from './coin-detail-dialog';
import { Meter, StatTile, ToneBadge, ToneNote, toneText } from './visuals';

const SUITABILITY_OPTIONS = ['all', 'prime', 'viable', 'watch', 'avoid', 'ineligible'] as const;

const THRESHOLD_KINDS: Record<
  'gate' | 'review' | 'stress',
  { tone: CatalogTone; label: string }
> = {
  gate: { tone: 'good', label: 'hard gate' },
  review: { tone: 'warn', label: 'review flag' },
  stress: { tone: 'violet', label: 'stress test' }
};

export default function CoinSettingsView() {
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
  const dataErrors = Number(summary.data_errors ?? 0);
  const avgScore = toNumber(summary.avg_eligible_score);
  const avgBaseline = toNumber(summary.avg_baseline_score);
  const delta = avgScore !== null && avgBaseline !== null ? avgScore - avgBaseline : null;
  const drawdownLimit = toNumber(thresholds.max_drawdown) ?? 0.35;

  return (
    <div className='space-y-5'>
      <div className='grid gap-2 sm:grid-cols-2 xl:grid-cols-4'>
        <StatTile
          label='Eligible universe'
          value={`${eligible} / ${universe || '—'}`}
          tone={eligible > 0 ? 'good' : 'warn'}
          hint={`${results.length} coin results loaded`}
        />
        <StatTile
          label='Average score'
          value={avgScore !== null ? avgScore.toFixed(1) : '—'}
          tone={scoreTone(avgScore)}
          hint={
            delta !== null ? (
              <span className={toneText[delta >= 0 ? 'good' : 'bad']}>
                {signedPercent(delta / 100, 1)} vs baseline
              </span>
            ) : (
              'Baseline unavailable'
            )
          }
        />
        <StatTile
          label='Review flags'
          value={flagged}
          tone={flagged === 0 ? 'good' : 'warn'}
          hint={flagged === 0 ? 'No warnings raised' : 'Inspect flagged coins'}
        />
        <StatTile
          label='Data errors'
          value={dataErrors}
          tone={dataErrors === 0 ? 'good' : 'bad'}
          hint={dataErrors === 0 ? 'All symbols simulated' : 'Excluded before simulation'}
        />
      </div>

      <Card>
        <CardHeader>
          <div className='flex flex-wrap items-start justify-between gap-2'>
            <div>
              <CardTitle className='text-base'>Runtime thresholds</CardTitle>
              <CardDescription>
                Persisted by the latest completed run. Values are environment-configured and can
                change between runs.
              </CardDescription>
            </div>
            {latest ? <StatusBadge value={latest.status} /> : null}
          </div>
        </CardHeader>
        <CardContent className='space-y-3'>
          {Object.keys(thresholds).length === 0 ? (
            <ToneNote tone='warn' title='No thresholds recorded' icon={<Icons.warning className='size-4' />}>
              Run a new analysis to persist the thresholds used for gating.
            </ToneNote>
          ) : null}
          <div className='grid gap-2 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4'>
            {THRESHOLD_ROWS.map((row) => {
              const kind = THRESHOLD_KINDS[row.kind];
              return (
                <div key={row.key} className='rounded-lg border p-2.5'>
                  <div className='flex items-start justify-between gap-2'>
                    <p className='text-muted-foreground text-[11px] font-medium tracking-wide uppercase'>
                      {row.label}
                    </p>
                    <ToneBadge tone={kind.tone}>{kind.label}</ToneBadge>
                  </div>
                  <p className='mt-1 text-sm font-semibold tabular-nums'>
                    {formatThreshold(thresholds[row.key], row.format)}
                  </p>
                </div>
              );
            })}
          </div>
          {latest ? (
            <p className='text-muted-foreground text-xs'>
              Run {String(latest.id).slice(0, 8)} · {latest.scope} scope ·{' '}
              {String(summary.lookback_days ?? '—')}d lookback ·{' '}
              {String(summary.interval ?? '—')} interval · started{' '}
              {formatDate(latest.started_at)}
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
                    <TableHead className='w-32 text-right'>Score</TableHead>
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
                    const covered = toNumber(regime?.regimes_covered);
                    const scoreDelta = toNumber(row.diagnostics?.score_delta);
                    return (
                      <TableRow key={row.id}>
                        <TableCell>
                          <div className='font-medium'>{row.coin.symbol}</div>
                          <div className='text-muted-foreground text-xs'>{row.coin.name}</div>
                        </TableCell>
                        <TableCell>
                          <StatusBadge value={row.suitability} />
                        </TableCell>
                        <TableCell>
                          <div className='flex items-center justify-end gap-2'>
                            <Meter
                              value={row.score}
                              tone={scoreTone(row.score)}
                              className='w-14'
                            />
                            <span
                              className={cn(
                                'w-9 text-right text-sm font-semibold tabular-nums',
                                toneText[scoreTone(row.score)]
                              )}
                            >
                              {row.score.toFixed(1)}
                            </span>
                          </div>
                          {scoreDelta !== null ? (
                            <p
                              className={cn(
                                'mt-0.5 text-right text-[11px] tabular-nums',
                                toneText[scoreDelta >= 0 ? 'good' : 'bad']
                              )}
                            >
                              {signedPercent(scoreDelta / 100, 1)} vs base
                            </p>
                          ) : null}
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
                        <TableCell>
                          <div className='flex items-center gap-1.5'>
                            <span className='text-muted-foreground text-xs capitalize'>
                              {regime?.dominant_regime?.replace(/_/g, ' ') ?? '—'}
                            </span>
                            <ToneBadge tone={regimeTone(covered)}>
                              {covered !== null ? `${covered}/3` : '—'}
                            </ToneBadge>
                          </div>
                        </TableCell>
                        <TableCell
                          className={cn(
                            'text-right text-xs tabular-nums',
                            toneText[
                              returnTone(toNumber(row.oos_metrics?.cost_stress_return))
                            ]
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
            Showing {rows.length} of {results.length} coin results. Stops and take-profits are ATR
            multiples; the stop distance also includes structural support invalidation.
          </p>
        </CardContent>
      </Card>

      <CoinDetailDialog
        result={detail}
        open={Boolean(detail)}
        onOpenChange={(open) => !open && setDetail(null)}
        drawdownLimit={drawdownLimit}
      />
    </div>
  );
}
