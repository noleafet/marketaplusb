'use client';

import PageContainer from '@/components/layout/page-container';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow
} from '@/components/ui/table';
import { Icons } from '@/components/icons';
import Link from 'next/link';
import { useParams } from 'next/navigation';
import { formatDate, formatPercent, formatUsd, metricsNumber } from '@/features/grid/api';
import EmptyPanel from '@/features/grid/components/empty-panel';
import EquityChart from '@/features/grid/components/equity-chart';
import MetricCard from '@/features/grid/components/metric-card';
import StatusBadge from '@/features/grid/components/status-badge';
import {
  FACTOR_LABELS,
  factorStatusClass,
  formatGateValue
} from '@/features/grid/diagnostics';
import { useCoinHistory } from '@/features/grid/queries';
import type { CoinResult } from '@/features/grid/types';

const FEATURE_GROUPS: Array<{
  title: string;
  rows: Array<{ key: string; label: string; format: 'percent' | 'usd' | 'number' | 'bps' }>;
}> = [
  {
    title: 'Liquidity',
    rows: [
      { key: 'adv_usd_median', label: 'Median ADV', format: 'usd' },
      { key: 'adv_usd_min', label: 'Min ADV', format: 'usd' },
      { key: 'avg_trades_per_day', label: 'Avg trades / day', format: 'number' }
    ]
  },
  {
    title: 'Volatility & tails',
    rows: [
      { key: 'realized_vol_annual', label: 'Realized vol (annual)', format: 'percent' },
      { key: 'atr_pct', label: 'ATR %', format: 'percent' },
      { key: 'daily_range_pct_median', label: 'Daily range (median)', format: 'percent' },
      { key: 'tail_loss_1pct', label: '1% tail loss', format: 'percent' },
      { key: 'max_gap_pct', label: 'Max gap', format: 'percent' }
    ]
  },
  {
    title: 'Mean reversion',
    rows: [
      { key: 'hurst', label: 'Hurst exponent', format: 'number' },
      { key: 'variance_ratio', label: 'Variance ratio', format: 'number' },
      { key: 'autocorr_lag1', label: 'Autocorrelation (1)', format: 'number' },
      { key: 'mean_reversion_score', label: 'MR score', format: 'number' }
    ]
  },
  {
    title: 'Trend risk',
    rows: [
      { key: 'adx', label: 'ADX', format: 'number' },
      { key: 'trend_r2', label: 'Trend R²', format: 'number' },
      { key: 'trend_slope_annual', label: 'Trend slope (annual)', format: 'percent' },
      { key: 'buy_hold_return', label: 'Buy & hold return', format: 'percent' },
      { key: 'buy_hold_max_drawdown', label: 'Buy & hold max DD', format: 'percent' }
    ]
  },
  {
    title: 'Range stability',
    rows: [
      { key: 'range_containment', label: 'Range containment', format: 'percent' },
      { key: 'range_width_pct', label: 'Range width', format: 'percent' },
      { key: 'range_width_cv', label: 'Range width CV', format: 'number' },
      { key: 'range_stability_score', label: 'Stability score', format: 'number' }
    ]
  },
  {
    title: 'Funding & execution',
    rows: [
      { key: 'funding_mean', label: 'Funding mean (8h)', format: 'percent' },
      { key: 'funding_long_annual_cost', label: 'Funding annual cost', format: 'percent' },
      { key: 'grid_step_bps', label: 'Grid step', format: 'bps' },
      { key: 'round_trip_cost_bps', label: 'Round-trip cost', format: 'bps' },
      { key: 'cost_to_edge', label: 'Cost to edge', format: 'percent' }
    ]
  }
];

function formatFeature(value: number, format: 'percent' | 'usd' | 'number' | 'bps'): string {
  if (!Number.isFinite(value)) return '—';
  if (format === 'percent') return formatPercent(value, 2);
  if (format === 'usd') return formatUsd(value, 0);
  if (format === 'bps') return `${value.toFixed(2)} bps`;
  return value.toLocaleString('en-US', { maximumFractionDigits: 3 });
}

export default function CoinDetailPage() {
  const params = useParams<{ symbol: string }>();
  const symbol = (params?.symbol ?? '').toString().toUpperCase();
  const history = useCoinHistory(symbol);
  const latest: CoinResult | undefined = history.data?.results?.[0];
  const metrics = latest?.oos_metrics ?? {};
  const features = latest?.features ?? {};
  const chart = latest?.chart;
  const holdout = chart?.holdout;
  const folds = latest?.folds ?? [];
  const trades = latest?.trade_sample ?? [];
  const diagnostics = latest?.diagnostics;
  const breakdown = diagnostics?.score_breakdown;
  const formatMaybePercent = (value: unknown) =>
    typeof value === 'number' && Number.isFinite(value) ? formatPercent(value) : '—';
  const formatMaybeNumber = (value: unknown, digits = 2) =>
    typeof value === 'number' && Number.isFinite(value) ? value.toFixed(digits) : '—';

  return (
    <PageContainer
      pageTitle={latest ? `${latest.coin.symbol} · ${latest.coin.name}` : symbol}
      pageDescription={
        latest
          ? `Binance ${latest.coin.binance_symbol || 'perpetual'} · rank ${latest.coin.rank ?? '—'} · analyzed ${formatDate(latest.data_end)}`
          : 'Loading the latest coin analysis.'
      }
      pageHeaderAction={
        <Button
          variant='outline'
          size='sm'
          nativeButton={false}
          render={<Link href='/dashboard/rankings' aria-label='Rankings' />}
        >
          <Icons.chart />
          Rankings
        </Button>
      }
    >
      {history.isLoading ? (
        <div className='text-muted-foreground py-16 text-center text-sm'>Loading analysis…</div>
      ) : history.isError ? (
        <EmptyPanel
          title='Could not load coin analysis'
          description={(history.error as Error).message}
          action={<Button onClick={() => history.refetch()}>Retry</Button>}
        />
      ) : !latest ? (
        <EmptyPanel
          title='No analysis for this coin'
          description='Walk-forward results appear here after an analysis run. A queued solo analysis shows up once the worker finishes.'
          action={
            <Button
              variant='outline'
              nativeButton={false}
              render={<Link href='/dashboard/rankings' aria-label='Back to rankings' />}
            >
              Back to rankings
            </Button>
          }
        />
      ) : (
        <div className='space-y-4'>
          <div className='grid gap-4 md:grid-cols-2 xl:grid-cols-4'>
            <MetricCard
              title='Suitability'
              value={<StatusBadge value={latest.suitability} />}
              hint={`Composite score ${latest.score.toFixed(1)}`}
              icon={<Icons.chart />}
            />
            <MetricCard
              title='OOS net return'
              value={
                <span
                  className={
                    metricsNumber(metrics, 'net_return') >= 0
                      ? 'text-emerald-600 dark:text-emerald-400'
                      : 'text-rose-600 dark:text-rose-400'
                  }
                >
                  {formatPercent(metricsNumber(metrics, 'net_return'))}
                </span>
              }
              hint={`Max DD ${formatPercent(metricsNumber(metrics, 'max_drawdown'))}`}
              icon={<Icons.trendingUp />}
            />
            <MetricCard
              title='OOS trades'
              value={metricsNumber(metrics, 'trades')}
              hint={`Win rate ${formatPercent(metricsNumber(metrics, 'win_rate'))} · PF ${metricsNumber(metrics, 'profit_factor').toFixed(2)}`}
              icon={<Icons.activity />}
            />
            <MetricCard
              title='Execution'
              value={`${formatPercent(metricsNumber(features, 'cost_to_edge'))}`}
              hint={`Fees ${formatUsd(metricsNumber(metrics, 'fees_paid'), 2)} · funding ${formatUsd(metricsNumber(metrics, 'funding_paid'), 2)}`}
              icon={<Icons.adjustments />}
            />
          </div>

          {diagnostics ? (
            <>
              <div className='grid gap-4 xl:grid-cols-2'>
                <Card>
                  <CardHeader>
                    <CardTitle>Risk review</CardTitle>
                    <CardDescription>
                      Hard gates decide eligibility before any ranking. Flags mark risks that pass
                      the gates but still deserve review.
                    </CardDescription>
                  </CardHeader>
                  <CardContent className='space-y-4'>
                    <div>
                      <p className='text-muted-foreground mb-2 text-xs uppercase'>Review flags</p>
                      {diagnostics.flags?.length ? (
                        <ul className='space-y-1.5 text-sm'>
                          {diagnostics.flags.map((flag) => (
                            <li key={flag.code} className='flex gap-2'>
                              <Icons.warning
                                className={`mt-0.5 size-3.5 shrink-0 ${
                                  flag.severity === 'critical'
                                    ? 'text-rose-500'
                                    : flag.severity === 'warning'
                                      ? 'text-amber-500'
                                      : 'text-muted-foreground'
                                }`}
                              />
                              <span>{flag.message}</span>
                            </li>
                          ))}
                        </ul>
                      ) : (
                        <p className='text-muted-foreground text-sm'>No review flags.</p>
                      )}
                    </div>
                    <div>
                      <p className='text-muted-foreground mb-2 text-xs uppercase'>
                        Hard eligibility gates
                      </p>
                      <div className='space-y-1.5 text-sm'>
                        {(diagnostics.hard_gates ?? []).map((gate) => (
                          <div
                            key={gate.name}
                            className='flex items-start justify-between gap-3'
                          >
                            <span className='text-muted-foreground'>
                              {gate.label}
                              {gate.reason ? (
                                <span className='block text-xs text-rose-500'>{gate.reason}</span>
                              ) : null}
                            </span>
                            <span
                              className={`shrink-0 tabular-nums ${
                                gate.passed
                                  ? 'text-emerald-600 dark:text-emerald-400'
                                  : 'text-rose-600 dark:text-rose-400'
                              }`}
                            >
                              {gate.passed ? 'pass' : 'fail'} · {formatGateValue(gate.value)}
                            </span>
                          </div>
                        ))}
                      </div>
                    </div>
                    {diagnostics.eligibility_reasons?.length ? (
                      <div>
                        <p className='text-muted-foreground mb-2 text-xs uppercase'>
                          Rejection reasons
                        </p>
                        <ul className='list-disc space-y-1 pl-5 text-sm text-rose-600 dark:text-rose-400'>
                          {diagnostics.eligibility_reasons.map((reason) => (
                            <li key={reason}>{reason}</li>
                          ))}
                        </ul>
                      </div>
                    ) : null}
                  </CardContent>
                </Card>

                <Card>
                  <CardHeader>
                    <CardTitle>Score contributions</CardTitle>
                    <CardDescription>
                      Enhanced score {latest.score.toFixed(1)} · baseline{' '}
                      {formatMaybeNumber(diagnostics.baseline_score, 1)} · change{' '}
                      {diagnostics.score_delta !== undefined
                        ? `${diagnostics.score_delta >= 0 ? '+' : ''}${diagnostics.score_delta.toFixed(1)}`
                        : '—'}
                    </CardDescription>
                  </CardHeader>
                  <CardContent className='space-y-3'>
                    {breakdown ? (
                      <>
                        {Object.entries(breakdown.factors ?? {}).map(([key, factor]) => (
                          <div key={key} className='space-y-1'>
                            <div className='flex items-center justify-between text-sm'>
                              <span>{FACTOR_LABELS[key] ?? key.replace(/_/g, ' ')}</span>
                              <span className='text-muted-foreground tabular-nums'>
                                {factor.score.toFixed(1)} · {factor.contribution.toFixed(1)} pts
                              </span>
                            </div>
                            <div className='bg-muted h-1.5 overflow-hidden rounded-full'>
                              <div
                                className={`h-full rounded-full ${factorStatusClass(factor.status)}`}
                                style={{
                                  width: `${Math.min(100, Math.max(0, factor.score))}%`
                                }}
                              />
                            </div>
                          </div>
                        ))}
                        {breakdown.penalties?.length ? (
                          <ul className='text-muted-foreground space-y-1 pt-1 text-xs'>
                            {breakdown.penalties.map((penalty) => (
                              <li key={penalty.code}>
                                −{penalty.points.toFixed(1)} pts · {penalty.reason}
                              </li>
                            ))}
                          </ul>
                        ) : null}
                      </>
                    ) : (
                      <p className='text-muted-foreground text-sm'>
                        No factor breakdown stored for this analysis.
                      </p>
                    )}
                  </CardContent>
                </Card>
              </div>

              <div className='grid gap-4 md:grid-cols-2 xl:grid-cols-3'>
                <Card>
                  <CardHeader>
                    <CardTitle className='text-base'>Regime robustness</CardTitle>
                    <CardDescription>
                      {formatMaybeNumber(diagnostics.regime?.robustness_score, 1)} / 100 ·{' '}
                      {diagnostics.regime?.regimes_covered ?? 0} regimes · dominant{' '}
                      {diagnostics.regime?.dominant_regime ?? '—'}
                    </CardDescription>
                  </CardHeader>
                  <CardContent className='space-y-2 text-sm'>
                    {Object.entries(diagnostics.regime?.regimes ?? {}).map(([label, bucket]) => (
                      <div key={label} className='flex items-center justify-between gap-2'>
                        <span className='text-muted-foreground capitalize'>
                          {label.replace(/_/g, ' ')}
                        </span>
                        <span className='tabular-nums'>
                          {bucket.folds} folds · {formatPercent(bucket.mean_return)}
                        </span>
                      </div>
                    ))}
                    {diagnostics.regime?.single_regime ? (
                      <p className='text-xs text-amber-600 dark:text-amber-400'>
                        All test folds share one regime; robustness is unproven.
                      </p>
                    ) : null}
                  </CardContent>
                </Card>

                <Card>
                  <CardHeader>
                    <CardTitle className='text-base'>Cost stress & sensitivity</CardTitle>
                    <CardDescription>
                      {diagnostics.cost_stress?.multiplier ?? 2}× fees and slippage
                    </CardDescription>
                  </CardHeader>
                  <CardContent className='space-y-2 text-sm'>
                    <div className='flex items-center justify-between gap-2'>
                      <span className='text-muted-foreground'>Stressed net</span>
                      <span className='tabular-nums'>
                        {formatMaybePercent(diagnostics.cost_stress?.stress_return)} ·{' '}
                        {diagnostics.cost_stress?.profitable ? 'profitable' : 'fails'}
                      </span>
                    </div>
                    <div className='flex items-center justify-between gap-2'>
                      <span className='text-muted-foreground'>Stressed max DD</span>
                      <span className='tabular-nums'>
                        {formatMaybePercent(diagnostics.cost_stress?.stress_max_drawdown)}
                      </span>
                    </div>
                    <div className='flex items-center justify-between gap-2'>
                      <span className='text-muted-foreground'>Parameter sensitivity</span>
                      <span className='tabular-nums'>
                        {formatMaybeNumber(diagnostics.sensitivity?.parameter_sensitivity, 2)}
                      </span>
                    </div>
                    <div className='flex items-center justify-between gap-2'>
                      <span className='text-muted-foreground'>Worst neighbour</span>
                      <span className='tabular-nums'>
                        {formatMaybePercent(diagnostics.sensitivity?.worst_neighbor_return)}
                      </span>
                    </div>
                  </CardContent>
                </Card>

                <Card>
                  <CardHeader>
                    <CardTitle className='text-base'>Inventory & recovery</CardTitle>
                    <CardDescription>Open positions are marked into equity and drawdown.</CardDescription>
                  </CardHeader>
                  <CardContent className='space-y-2 text-sm'>
                    <div className='flex items-center justify-between gap-2'>
                      <span className='text-muted-foreground'>Peak exposure</span>
                      <span className='tabular-nums'>
                        {formatMaybeNumber(diagnostics.exposure?.max_exposure_ratio, 2)}×
                      </span>
                    </div>
                    <div className='flex items-center justify-between gap-2'>
                      <span className='text-muted-foreground'>Avg exposure</span>
                      <span className='tabular-nums'>
                        {formatMaybeNumber(diagnostics.exposure?.avg_exposure_ratio, 2)}×
                      </span>
                    </div>
                    <div className='flex items-center justify-between gap-2'>
                      <span className='text-muted-foreground'>Max unrealized loss</span>
                      <span className='tabular-nums'>
                        {formatMaybePercent(diagnostics.exposure?.max_unrealized_loss_pct)}
                      </span>
                    </div>
                    <div className='flex items-center justify-between gap-2'>
                      <span className='text-muted-foreground'>Recovery time</span>
                      <span className='tabular-nums'>
                        {formatMaybeNumber(diagnostics.recovery_days, 0)} d
                      </span>
                    </div>
                    <div className='flex items-center justify-between gap-2'>
                      <span className='text-muted-foreground'>Longest drawdown</span>
                      <span className='tabular-nums'>
                        {formatMaybeNumber(diagnostics.max_drawdown_duration_days, 0)} d
                      </span>
                    </div>
                  </CardContent>
                </Card>

                <Card>
                  <CardHeader>
                    <CardTitle className='text-base'>Execution quality</CardTitle>
                    <CardDescription>Fees, slippage, and completed grid cycles.</CardDescription>
                  </CardHeader>
                  <CardContent className='space-y-2 text-sm'>
                    <div className='flex items-center justify-between gap-2'>
                      <span className='text-muted-foreground'>Slippage paid</span>
                      <span className='tabular-nums'>
                        {formatUsd(metricsNumber(metrics, 'slippage_paid'), 2)}
                      </span>
                    </div>
                    <div className='flex items-center justify-between gap-2'>
                      <span className='text-muted-foreground'>Buy / sell fills</span>
                      <span className='tabular-nums'>
                        {metricsNumber(metrics, 'buy_fills')} /{' '}
                        {metricsNumber(metrics, 'sell_fills')}
                      </span>
                    </div>
                    <div className='flex items-center justify-between gap-2'>
                      <span className='text-muted-foreground'>Cycle completion</span>
                      <span className='tabular-nums'>
                        {formatMaybePercent(diagnostics.execution?.cycle_completion_ratio)}
                      </span>
                    </div>
                    <div className='flex items-center justify-between gap-2'>
                      <span className='text-muted-foreground'>Spread</span>
                      <span className='tabular-nums'>
                        {typeof features.spread_bps === 'number'
                          ? `${features.spread_bps.toFixed(2)} bps`
                          : '—'}
                      </span>
                    </div>
                    <div className='flex items-center justify-between gap-2'>
                      <span className='text-muted-foreground'>Depth cover</span>
                      <span className='tabular-nums'>
                        {formatMaybeNumber(
                          typeof features.depth_cover_ratio === 'number'
                            ? features.depth_cover_ratio
                            : undefined,
                          2
                        )}
                        ×
                      </span>
                    </div>
                  </CardContent>
                </Card>

                <Card>
                  <CardHeader>
                    <CardTitle className='text-base'>Correlation</CardTitle>
                    <CardDescription>Daily-return correlation with eligible peers.</CardDescription>
                  </CardHeader>
                  <CardContent className='space-y-2 text-sm'>
                    <div className='flex items-center justify-between gap-2'>
                      <span className='text-muted-foreground'>Average</span>
                      <span className='tabular-nums'>
                        {formatMaybeNumber(diagnostics.correlation?.avg_correlation, 2)}
                      </span>
                    </div>
                    <div className='flex items-center justify-between gap-2'>
                      <span className='text-muted-foreground'>Peak</span>
                      <span className='tabular-nums'>
                        {formatMaybeNumber(diagnostics.correlation?.max_correlation, 2)}
                      </span>
                    </div>
                    {diagnostics.correlation?.peers?.length ? (
                      <div className='text-muted-foreground flex flex-wrap gap-1 pt-1 text-xs'>
                        {diagnostics.correlation.peers.map((peer) => (
                          <span key={peer.symbol} className='bg-muted rounded px-1.5 py-0.5'>
                            {peer.symbol} {peer.correlation.toFixed(2)}
                          </span>
                        ))}
                      </div>
                    ) : null}
                  </CardContent>
                </Card>

                <Card>
                  <CardHeader>
                    <CardTitle className='text-base'>Return stability</CardTitle>
                    <CardDescription>Concentration and fold-to-fold dispersion.</CardDescription>
                  </CardHeader>
                  <CardContent className='space-y-2 text-sm'>
                    <div className='flex items-center justify-between gap-2'>
                      <span className='text-muted-foreground'>Top trade share</span>
                      <span className='tabular-nums'>
                        {formatMaybePercent(diagnostics.return_stability?.top_trade_share)}
                      </span>
                    </div>
                    <div className='flex items-center justify-between gap-2'>
                      <span className='text-muted-foreground'>Top 5 trade share</span>
                      <span className='tabular-nums'>
                        {formatMaybePercent(diagnostics.return_stability?.top5_trade_share)}
                      </span>
                    </div>
                    <div className='flex items-center justify-between gap-2'>
                      <span className='text-muted-foreground'>Fold return std</span>
                      <span className='tabular-nums'>
                        {formatMaybePercent(diagnostics.return_stability?.fold_return_std)}
                      </span>
                    </div>
                    <div className='flex items-center justify-between gap-2'>
                      <span className='text-muted-foreground'>Worst fold DD</span>
                      <span className='tabular-nums'>
                        {formatMaybePercent(metricsNumber(metrics, 'worst_fold_drawdown'))}
                      </span>
                    </div>
                  </CardContent>
                </Card>
              </div>
            </>
          ) : null}

          <Card>
            <CardHeader>
              <CardTitle>Walk-forward out-of-sample equity</CardTitle>
              <CardDescription>
                Stitched test windows, then the untouched holdout (dashed) run once with the
                deployed parameters. Starting capital {formatUsd(1000)}; no window is used to pick
                its own parameters.
              </CardDescription>
            </CardHeader>
            <CardContent>
              <EquityChart points={chart?.equity_curve ?? []} capital={1000} height={320} />
            </CardContent>
          </Card>

          <div className='grid gap-4 xl:grid-cols-2'>
            <Card>
              <CardHeader>
                <CardTitle>Recommended parameters</CardTitle>
                <CardDescription>Selected by the most recent fold.</CardDescription>
              </CardHeader>
              <CardContent>
                <div className='grid grid-cols-2 gap-3 text-sm'>
                  {Object.entries(latest.params ?? {}).map(([key, value]) => (
                    <div key={key} className='flex items-center justify-between gap-2'>
                      <span className='text-muted-foreground capitalize'>
                        {key.replace(/_/g, ' ')}
                      </span>
                      <span className='font-medium tabular-nums'>{String(value)}</span>
                    </div>
                  ))}
                </div>
                {chart?.grid_levels?.length ? (
                  <div className='mt-4'>
                    <p className='text-muted-foreground mb-2 text-xs uppercase'>Grid levels</p>
                    <div className='flex flex-wrap gap-1'>
                      {chart.grid_levels.slice(0, 40).map((level) => (
                        <span
                          key={level}
                          className='bg-muted rounded px-1.5 py-0.5 font-mono text-[11px]'
                        >
                          {Number(level).toPrecision(6)}
                        </span>
                      ))}
                    </div>
                  </div>
                ) : null}
              </CardContent>
            </Card>

            <Card>
              <CardHeader>
                <CardTitle>Component scores</CardTitle>
                <CardDescription>Cross-sectional percentiles (0–100).</CardDescription>
              </CardHeader>
              <CardContent className='space-y-3'>
                {Object.entries(latest.component_scores ?? {}).map(([key, value]) => (
                  <div key={key} className='space-y-1'>
                    <div className='flex items-center justify-between text-sm'>
                      <span className='capitalize'>{key.replace(/_/g, ' ')}</span>
                      <span className='tabular-nums'>
                        {value === null || value === undefined ? '—' : Number(value).toFixed(1)}
                      </span>
                    </div>
                    <div className='bg-muted h-1.5 overflow-hidden rounded-full'>
                      <div
                        className='bg-primary h-full rounded-full'
                        style={{ width: `${Math.min(100, Number(value ?? 0))}%` }}
                      />
                    </div>
                  </div>
                ))}
              </CardContent>
            </Card>
          </div>

          <div className='grid gap-4 md:grid-cols-2 xl:grid-cols-3'>
            {FEATURE_GROUPS.map((group) => (
              <Card key={group.title}>
                <CardHeader>
                  <CardTitle className='text-base'>{group.title}</CardTitle>
                </CardHeader>
                <CardContent className='space-y-2 text-sm'>
                  {group.rows.map((row) => (
                    <div key={row.key} className='flex items-center justify-between gap-2'>
                      <span className='text-muted-foreground'>{row.label}</span>
                      <span className='font-medium tabular-nums'>
                        {formatFeature(metricsNumber(features, row.key, Number.NaN), row.format)}
                      </span>
                    </div>
                  ))}
                </CardContent>
              </Card>
            ))}
          </div>

          <div className='grid gap-4 xl:grid-cols-2'>
            <Card>
              <CardHeader>
                <CardTitle>Walk-forward folds</CardTitle>
                <CardDescription>
                  Test-window metrics per fold, then the holdout. Scores and ranks use the folds
                  only.
                </CardDescription>
              </CardHeader>
              <CardContent className='px-0'>
                {folds.length === 0 ? (
                  <p className='text-muted-foreground px-4 py-6 text-sm'>No folds available.</p>
                ) : (
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>Fold</TableHead>
                        <TableHead>Test window</TableHead>
                        <TableHead>Regime</TableHead>
                        <TableHead className='text-right'>Return</TableHead>
                        <TableHead className='text-right'>2× cost</TableHead>
                        <TableHead className='text-right'>Max DD</TableHead>
                        <TableHead className='text-right'>Trades</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {folds.map((fold, index) => {
                        const testMetrics = (fold.test_metrics ?? {}) as Record<string, unknown>;
                        return (
                          <TableRow key={index}>
                            <TableCell className='tabular-nums'>
                              {String(fold.fold ?? index + 1)}
                            </TableCell>
                            <TableCell className='text-muted-foreground text-xs'>
                              {formatDate(String(fold.test_start ?? ''))} —{' '}
                              {formatDate(String(fold.test_end ?? ''))}
                            </TableCell>
                            <TableCell className='text-muted-foreground text-xs capitalize'>
                              {String(
                                (fold.regime as { label?: string } | undefined)?.label ?? '—'
                              ).replace(/_/g, ' ')}
                            </TableCell>
                            <TableCell className='text-right tabular-nums'>
                              {formatPercent(metricsNumber(testMetrics, 'net_return'))}
                            </TableCell>
                            <TableCell className='text-right tabular-nums'>
                              {formatMaybePercent(
                                (fold.stress as Record<string, unknown> | undefined)?.net_return
                              )}
                            </TableCell>
                            <TableCell className='text-right tabular-nums'>
                              {formatPercent(metricsNumber(testMetrics, 'max_drawdown'))}
                            </TableCell>
                            <TableCell className='text-right tabular-nums'>
                              {metricsNumber(testMetrics, 'trades')}
                            </TableCell>
                          </TableRow>
                        );
                      })}
                      {holdout?.start ? (
                        <TableRow className='bg-muted/40'>
                          <TableCell className='text-xs font-medium'>Holdout</TableCell>
                          <TableCell className='text-muted-foreground text-xs'>
                            {formatDate(holdout.start)} — {formatDate(holdout.end ?? '')}
                          </TableCell>
                          <TableCell className='text-muted-foreground text-xs'>
                            {holdout.stopped
                              ? `Stopped (${(holdout.stop_reason ?? 'stop').replace(/_/g, ' ')})`
                              : 'Untouched'}
                          </TableCell>
                          <TableCell className='text-right tabular-nums'>
                            {formatPercent(holdout.net_return ?? null)}
                          </TableCell>
                          <TableCell className='text-muted-foreground text-right tabular-nums'>
                            —
                          </TableCell>
                          <TableCell className='text-right tabular-nums'>
                            {formatPercent(holdout.max_drawdown ?? null)}
                          </TableCell>
                          <TableCell className='text-right tabular-nums'>
                            {holdout.trades ?? '—'}
                          </TableCell>
                        </TableRow>
                      ) : null}
                    </TableBody>
                  </Table>
                )}
              </CardContent>
            </Card>

            <Card>
              <CardHeader>
                <CardTitle>Recent simulated trades</CardTitle>
                <CardDescription>
                  Last {Math.min(25, trades.length)} fills from the OOS folds.
                </CardDescription>
              </CardHeader>
              <CardContent className='px-0'>
                {trades.length === 0 ? (
                  <p className='text-muted-foreground px-4 py-6 text-sm'>No trades recorded.</p>
                ) : (
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>Time</TableHead>
                        <TableHead>Side</TableHead>
                        <TableHead className='text-right'>Price</TableHead>
                        <TableHead className='text-right'>Qty</TableHead>
                        <TableHead>Reason</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {trades
                        .slice(-25)
                        .toReversed()
                        .map((trade, index) => (
                          <TableRow key={index}>
                            <TableCell className='text-muted-foreground text-xs'>
                              {formatDate(trade.time)}
                            </TableCell>
                            <TableCell
                              className={
                                trade.side === 'buy'
                                  ? 'text-emerald-600 dark:text-emerald-400'
                                  : 'text-rose-600 dark:text-rose-400'
                              }
                            >
                              {trade.side}
                            </TableCell>
                            <TableCell className='text-right tabular-nums'>
                              {formatUsd(trade.price, 4)}
                            </TableCell>
                            <TableCell className='text-right tabular-nums'>
                              {Number(trade.qty ?? 0).toPrecision(5)}
                            </TableCell>
                            <TableCell className='text-muted-foreground text-xs'>
                              {trade.reason ?? '—'}
                            </TableCell>
                          </TableRow>
                        ))}
                    </TableBody>
                  </Table>
                )}
              </CardContent>
            </Card>
          </div>

          <p className='text-muted-foreground text-xs'>
            Data window {formatDate(latest.data_start)} — {formatDate(latest.data_end)}
            {holdout?.start
              ? ` (walk-forward folds end before the holdout starts ${formatDate(holdout.start)})`
              : ''}
            . Results are
            historical simulations, not investment advice or a profitability guarantee.
          </p>
        </div>
      )}
    </PageContainer>
  );
}
