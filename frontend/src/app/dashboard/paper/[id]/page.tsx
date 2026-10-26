'use client';

import PageContainer from '@/components/layout/page-container';
import { Badge } from '@/components/ui/badge';
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
import { toast } from 'sonner';
import {
  formatDate,
  formatPercent,
  formatUsd,
  metricsNumber,
  relativeTime
} from '@/features/grid/api';
import EmptyPanel from '@/features/grid/components/empty-panel';
import EquityChart from '@/features/grid/components/equity-chart';
import MetricCard from '@/features/grid/components/metric-card';
import StatusBadge from '@/features/grid/components/status-badge';
import {
  usePaperEquity,
  usePaperEvaluations,
  usePaperFills,
  usePaperSession,
  useSessionAction
} from '@/features/grid/queries';

interface GridOrder {
  side: string;
  qty: number;
}

interface GridState {
  levels?: number[];
  range_low?: number;
  range_high?: number;
  orders?: Record<string, GridOrder>;
  prev_close?: number;
  last_bar_time?: string | null;
  last_rebalance_at?: string | null;
  stopped?: boolean;
  stop_reason?: string;
}

interface EvaluationResult {
  passed: boolean;
  failures: string[];
  metrics: Record<string, number | string>;
}

export default function PaperSessionDetailPage() {
  const params = useParams<{ id: string }>();
  const id = typeof params.id === 'string' ? params.id : '';
  const session = usePaperSession(id);
  const equity = usePaperEquity(id);
  const fills = usePaperFills(id);
  const evaluations = usePaperEvaluations(id);
  const sessionAction = useSessionAction();

  const data = session.data;
  const grid = (data?.grid_state ?? {}) as GridState;
  const levels = grid.levels ?? [];
  const orders = Object.entries(grid.orders ?? {})
    .map(([index, order]) => ({
      index: Number(index),
      price: levels[Number(index)] ?? null,
      side: order.side,
      qty: order.qty
    }))
    .toSorted((a, b) => b.index - a.index);

  const run = (action: 'pause' | 'resume' | 'stop' | 'evaluate' | 'tick') => {
    if (action === 'stop' && !window.confirm('Stop this paper session? This cannot be undone.')) {
      return;
    }
    const labels: Record<string, string> = {
      pause: 'Session paused',
      resume: 'Session resumed',
      stop: 'Session stopped',
      evaluate: 'Evaluation complete',
      tick: 'Tick queued'
    };
    sessionAction.mutate(
      {
        id,
        action,
        body: action === 'stop' ? { reason: 'manual_stop' } : undefined
      },
      {
        onSuccess: (result) => {
          if (action === 'evaluate') {
            const evaluation = (result as { evaluation?: EvaluationResult | null })?.evaluation;
            if (evaluation) {
              toast[evaluation.passed ? 'success' : 'warning'](labels[action], {
                description: evaluation.passed
                  ? 'All paper thresholds passed — promotion review is allowed.'
                  : evaluation.failures.join(' · ')
              });
              return;
            }
          }
          toast.success(labels[action]);
        },
        onError: (error: Error) => toast.error(error.message)
      }
    );
  };

  if (session.isLoading) {
    return <PageContainer isLoading pageTitle='Paper session' pageDescription='' />;
  }

  if (session.isError || !data) {
    return (
      <PageContainer pageTitle='Paper session' pageDescription=''>
        <EmptyPanel
          title='Could not load this session'
          description={session.error ? (session.error as Error).message : 'Session not found.'}
          action={
            <Button
              variant='outline'
              nativeButton={false}
              render={<Link href='/dashboard/paper' aria-label='Back to paper book' />}
            >
              Back to paper book
            </Button>
          }
        />
      </PageContainer>
    );
  }

  const winRate = data.wins + data.losses > 0 ? data.wins / (data.wins + data.losses) : 0;
  const netPnl = data.equity - data.capital;
  const pending = sessionAction.isPending;

  return (
    <PageContainer
      pageTitle={`${data.coin.symbol} paper grid`}
      pageDescription={`${data.coin.name} · Binance ${data.coin.binance_symbol} · started ${formatDate(data.started_at)}`}
      pageHeaderAction={
        <div className='flex flex-wrap items-center gap-2'>
          <Button
            variant='outline'
            size='sm'
            nativeButton={false}
            render={<Link href='/dashboard/paper' aria-label='Paper book' />}
          >
            <Icons.chevronLeft />
            Paper book
          </Button>
          <Button variant='outline' size='sm' disabled={pending} onClick={() => run('tick')}>
            <Icons.activity />
            Tick now
          </Button>
          <Button variant='outline' size='sm' disabled={pending} onClick={() => run('evaluate')}>
            <Icons.checks />
            Evaluate
          </Button>
          {data.status === 'active' ? (
            <Button variant='outline' size='sm' disabled={pending} onClick={() => run('pause')}>
              Pause
            </Button>
          ) : null}
          {data.status === 'paused' ? (
            <Button size='sm' disabled={pending} onClick={() => run('resume')}>
              Resume
            </Button>
          ) : null}
          {data.status !== 'stopped' ? (
            <Button variant='destructive' size='sm' disabled={pending} onClick={() => run('stop')}>
              Stop
            </Button>
          ) : null}
        </div>
      }
    >
      <div className='space-y-4'>
        <div className='flex flex-wrap items-center gap-2'>
          <StatusBadge value={data.status} />
          {data.promotion_ready ? (
            <Badge
              variant='outline'
              className='border-emerald-500/30 bg-emerald-500/10 text-emerald-600 dark:text-emerald-400'
            >
              Promotion ready
            </Badge>
          ) : (
            <Badge variant='outline'>Not promotion ready</Badge>
          )}
          {data.stop_reason ? (
            <span className='text-muted-foreground text-xs'>stop reason: {data.stop_reason}</span>
          ) : null}
        </div>

        <div className='grid gap-4 md:grid-cols-2 xl:grid-cols-4'>
          <MetricCard
            title='Equity'
            value={formatUsd(data.equity)}
            hint={`${formatUsd(netPnl, 2)} vs ${formatUsd(data.capital)} capital`}
            icon={<Icons.activity />}
          />
          <MetricCard
            title='Net return'
            value={
              <span
                className={
                  data.net_return >= 0
                    ? 'text-emerald-600 dark:text-emerald-400'
                    : 'text-rose-600 dark:text-rose-400'
                }
              >
                {formatPercent(data.net_return)}
              </span>
            }
            hint={`Realized ${formatUsd(data.realized_pnl, 2)} · peak ${formatUsd(data.peak_equity)}`}
            icon={<Icons.chart />}
          />
          <MetricCard
            title='Max drawdown'
            value={formatPercent(data.max_drawdown)}
            hint={`Hard stop at 25% · leverage ${data.leverage.toFixed(1)}x`}
            icon={<Icons.trendingDown />}
          />
          <MetricCard
            title='Activity'
            value={`${data.fill_count} fills`}
            hint={`${data.closed_lots} closed lots · win rate ${formatPercent(winRate)}`}
            icon={<Icons.flask />}
          />
        </div>

        <div className='grid gap-4 xl:grid-cols-3'>
          <Card className='xl:col-span-2'>
            <CardHeader className='flex flex-row items-start justify-between'>
              <div>
                <CardTitle>Equity curve</CardTitle>
                <CardDescription>
                  Recorded at each paper tick from live public market data.
                </CardDescription>
              </div>
              <span className='text-muted-foreground text-xs'>
                last tick {relativeTime(data.last_tick_at)}
              </span>
            </CardHeader>
            <CardContent>
              <EquityChart points={equity.data?.points ?? []} capital={data.capital} height={300} />
            </CardContent>
          </Card>

          <div className='space-y-4'>
            <Card>
              <CardHeader>
                <CardTitle className='text-base'>Position</CardTitle>
              </CardHeader>
              <CardContent className='space-y-2 text-sm'>
                <div className='flex justify-between'>
                  <span className='text-muted-foreground'>Mark price</span>
                  <span className='tabular-nums'>{formatUsd(data.mark_price, 4)}</span>
                </div>
                <div className='flex justify-between'>
                  <span className='text-muted-foreground'>Position qty</span>
                  <span className='tabular-nums'>{data.position_qty.toFixed(6)}</span>
                </div>
                <div className='flex justify-between'>
                  <span className='text-muted-foreground'>Avg entry</span>
                  <span className='tabular-nums'>{formatUsd(data.avg_entry, 4)}</span>
                </div>
                <div className='flex justify-between'>
                  <span className='text-muted-foreground'>Realized P&L</span>
                  <span className='tabular-nums'>{formatUsd(data.realized_pnl, 2)}</span>
                </div>
                <div className='flex justify-between'>
                  <span className='text-muted-foreground'>Fees paid</span>
                  <span className='tabular-nums'>{formatUsd(data.fees_paid, 2)}</span>
                </div>
                <div className='flex justify-between'>
                  <span className='text-muted-foreground'>Funding paid</span>
                  <span className='tabular-nums'>{formatUsd(data.funding_paid, 2)}</span>
                </div>
              </CardContent>
            </Card>

            <Card>
              <CardHeader>
                <CardTitle className='text-base'>Grid</CardTitle>
              </CardHeader>
              <CardContent className='space-y-2 text-sm'>
                <div className='flex justify-between'>
                  <span className='text-muted-foreground'>Range</span>
                  <span className='tabular-nums'>
                    {grid.range_low ? formatUsd(grid.range_low, 2) : '—'} –{' '}
                    {grid.range_high ? formatUsd(grid.range_high, 2) : '—'}
                  </span>
                </div>
                <div className='flex justify-between'>
                  <span className='text-muted-foreground'>Levels</span>
                  <span className='tabular-nums'>{levels.length}</span>
                </div>
                <div className='flex justify-between'>
                  <span className='text-muted-foreground'>Open orders</span>
                  <span className='tabular-nums'>{orders.length}</span>
                </div>
                <div className='flex justify-between'>
                  <span className='text-muted-foreground'>Last rebalance</span>
                  <span className='tabular-nums'>{relativeTime(grid.last_rebalance_at)}</span>
                </div>
                <div className='flex justify-between'>
                  <span className='text-muted-foreground'>Model params</span>
                  <span className='tabular-nums'>{data.params.grid_levels ?? '—'} levels</span>
                </div>
                {grid.stopped ? (
                  <p className='text-rose-600 dark:text-rose-400 text-xs'>
                    Strategy stopped: {grid.stop_reason || 'unknown'}
                  </p>
                ) : null}
              </CardContent>
            </Card>
          </div>
        </div>

        <div className='grid gap-4 xl:grid-cols-2'>
          <Card>
            <CardHeader>
              <CardTitle className='text-base'>Open grid orders</CardTitle>
              <CardDescription>
                Resting simulated orders in the persisted grid state.
              </CardDescription>
            </CardHeader>
            <CardContent className='px-0'>
              {orders.length === 0 ? (
                <p className='text-muted-foreground px-6 pb-6 text-sm'>No resting orders.</p>
              ) : (
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Level</TableHead>
                      <TableHead>Side</TableHead>
                      <TableHead className='text-right'>Price</TableHead>
                      <TableHead className='text-right'>Qty</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {orders.map((order) => (
                      <TableRow key={order.index}>
                        <TableCell className='text-muted-foreground tabular-nums'>
                          {order.index}
                        </TableCell>
                        <TableCell>
                          <Badge
                            variant='outline'
                            className={
                              order.side === 'buy'
                                ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-600 dark:text-emerald-400'
                                : 'border-rose-500/30 bg-rose-500/10 text-rose-600 dark:text-rose-400'
                            }
                          >
                            {order.side}
                          </Badge>
                        </TableCell>
                        <TableCell className='text-right tabular-nums'>
                          {order.price ? formatUsd(order.price, 4) : '—'}
                        </TableCell>
                        <TableCell className='text-right tabular-nums'>
                          {order.qty.toFixed(6)}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle className='text-base'>Execution settings</CardTitle>
              <CardDescription>Captured when the session was created.</CardDescription>
            </CardHeader>
            <CardContent className='grid grid-cols-2 gap-3 text-sm'>
              {[
                ['Capital', formatUsd(data.capital)],
                ['Leverage', `${data.leverage.toFixed(1)}x`],
                ['Maker fee', formatPercent(Number(data.config?.maker_fee_rate ?? 0), 3)],
                ['Taker fee', formatPercent(Number(data.config?.taker_fee_rate ?? 0), 3)],
                ['Slippage', `${Number(data.config?.slippage_bps ?? 0).toFixed(1)} bps`],
                ['Interval', String(data.config?.interval ?? '—')],
                ['Grid levels', String(data.params.grid_levels ?? '—')],
                ['Range lookback', `${String(data.params.range_lookback_days ?? '—')}d`],
                ['Rebalance', `${String(data.params.rebalance_days ?? '—')}d`],
                ['ATR multiplier', String(data.params.atr_multiplier ?? '—')],
                ['Stop loss', formatPercent(Number(data.params.stop_loss_pct ?? 0))],
                ['Inventory', formatPercent(Number(data.params.initial_inventory_pct ?? 0))]
              ].map(([label, value]) => (
                <div key={label}>
                  <div className='text-muted-foreground text-xs'>{label}</div>
                  <div className='font-medium tabular-nums'>{value}</div>
                </div>
              ))}
            </CardContent>
          </Card>
        </div>

        <Card>
          <CardHeader>
            <CardTitle className='text-base'>Fills</CardTitle>
            <CardDescription>
              Simulated executions with fee, adverse slippage, and realized P&L.
            </CardDescription>
          </CardHeader>
          <CardContent className='px-0'>
            {(fills.data?.fills ?? []).length === 0 ? (
              <p className='text-muted-foreground px-6 pb-6 text-sm'>
                No fills yet. Resting buy orders fill when price trades through a grid level.
              </p>
            ) : (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Time</TableHead>
                    <TableHead>Side</TableHead>
                    <TableHead className='text-right'>Price</TableHead>
                    <TableHead className='text-right'>Qty</TableHead>
                    <TableHead className='text-right'>Notional</TableHead>
                    <TableHead className='text-right'>Fee</TableHead>
                    <TableHead className='text-right'>Realized P&L</TableHead>
                    <TableHead>Reason</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {(fills.data?.fills ?? []).slice(0, 100).map((fill) => (
                    <TableRow key={fill.id}>
                      <TableCell className='text-xs'>{formatDate(fill.time)}</TableCell>
                      <TableCell>
                        <Badge
                          variant='outline'
                          className={
                            fill.side === 'buy'
                              ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-600 dark:text-emerald-400'
                              : 'border-rose-500/30 bg-rose-500/10 text-rose-600 dark:text-rose-400'
                          }
                        >
                          {fill.side}
                        </Badge>
                      </TableCell>
                      <TableCell className='text-right tabular-nums'>
                        {formatUsd(fill.price, 4)}
                      </TableCell>
                      <TableCell className='text-right tabular-nums'>
                        {fill.qty.toFixed(6)}
                      </TableCell>
                      <TableCell className='text-right tabular-nums'>
                        {formatUsd(fill.notional, 2)}
                      </TableCell>
                      <TableCell className='text-right tabular-nums'>
                        {formatUsd(fill.fee, 4)}
                      </TableCell>
                      <TableCell
                        className={`text-right tabular-nums ${
                          fill.realized_pnl >= 0
                            ? 'text-emerald-600 dark:text-emerald-400'
                            : 'text-rose-600 dark:text-rose-400'
                        }`}
                      >
                        {formatUsd(fill.realized_pnl, 2)}
                      </TableCell>
                      <TableCell className='text-muted-foreground text-xs'>{fill.reason}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className='text-base'>Promotion evaluations</CardTitle>
            <CardDescription>
              Checked against the configured minimum days, net return, drawdown, and fill count.
            </CardDescription>
          </CardHeader>
          <CardContent className='px-0'>
            {(evaluations.data ?? []).length === 0 ? (
              <p className='text-muted-foreground px-6 pb-6 text-sm'>
                No evaluation recorded yet. Tick the session or run the promotion job.
              </p>
            ) : (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Evaluated</TableHead>
                    <TableHead>Result</TableHead>
                    <TableHead className='text-right'>Days</TableHead>
                    <TableHead className='text-right'>Net return</TableHead>
                    <TableHead className='text-right'>Max DD</TableHead>
                    <TableHead className='text-right'>Fills</TableHead>
                    <TableHead>Failures</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {(evaluations.data ?? []).map((evaluation) => (
                    <TableRow key={evaluation.id}>
                      <TableCell className='text-xs'>
                        {formatDate(evaluation.evaluated_at)}
                      </TableCell>
                      <TableCell>
                        <Badge
                          variant='outline'
                          className={
                            evaluation.passed
                              ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-600 dark:text-emerald-400'
                              : 'border-amber-500/30 bg-amber-500/10 text-amber-600 dark:text-amber-400'
                          }
                        >
                          {evaluation.passed ? 'Passed' : 'Failed'}
                        </Badge>
                      </TableCell>
                      <TableCell className='text-right tabular-nums'>
                        {metricsNumber(evaluation.metrics, 'days').toFixed(2)}
                      </TableCell>
                      <TableCell className='text-right tabular-nums'>
                        {formatPercent(metricsNumber(evaluation.metrics, 'net_return'))}
                      </TableCell>
                      <TableCell className='text-right tabular-nums'>
                        {formatPercent(metricsNumber(evaluation.metrics, 'max_drawdown'))}
                      </TableCell>
                      <TableCell className='text-right tabular-nums'>
                        {metricsNumber(evaluation.metrics, 'fill_count')}
                      </TableCell>
                      <TableCell className='text-muted-foreground max-w-sm text-xs'>
                        {evaluation.failures.length ? evaluation.failures.join(' · ') : '—'}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
          </CardContent>
        </Card>
      </div>
    </PageContainer>
  );
}
