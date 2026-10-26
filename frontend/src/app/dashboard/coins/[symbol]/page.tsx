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
  const folds = latest?.folds ?? [];
  const trades = latest?.trade_sample ?? [];

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
          description='Run the daily analysis after the monthly coin refresh to generate walk-forward results.'
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

          <Card>
            <CardHeader>
              <CardTitle>Walk-forward out-of-sample equity</CardTitle>
              <CardDescription>
                Stitched test windows. Starting capital {formatUsd(1000)}; folds never train on
                their test window.
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
                <CardDescription>Test-window metrics per fold.</CardDescription>
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
                        <TableHead className='text-right'>Return</TableHead>
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
                            <TableCell className='text-right tabular-nums'>
                              {formatPercent(metricsNumber(testMetrics, 'net_return'))}
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
            Data window {formatDate(latest.data_start)} — {formatDate(latest.data_end)}. Results are
            historical simulations, not investment advice or a profitability guarantee.
          </p>
        </div>
      )}
    </PageContainer>
  );
}
