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
import { toast } from 'sonner';
import { formatPercent, formatUsd, relativeTime } from '@/features/grid/api';
import EmptyPanel from '@/features/grid/components/empty-panel';
import MetricCard from '@/features/grid/components/metric-card';
import StatusBadge from '@/features/grid/components/status-badge';
import { usePaperSessions, useSetBias, useTickAll, useTriggerJob } from '@/features/grid/queries';

export default function PaperSessionsPage() {
  const sessions = usePaperSessions();
  const tickAll = useTickAll();
  const trigger = useTriggerJob();
  const setBias = useSetBias();

  const markBias = (id: string, bias: 'bullish' | 'bearish') => {
    setBias.mutate(
      { id, bias },
      {
        onSuccess: () =>
          toast.success(bias === 'bearish' ? 'Trading locked' : 'Trading unlocked', {
            description:
              bias === 'bearish'
                ? 'The grid stops opening trades; existing inventory is held.'
                : 'The grid re-centers around the live price on the next tick.'
          }),
        onError: (error: Error) => toast.error(error.message)
      }
    );
  };

  const rows = sessions.data?.results ?? [];
  const active = rows.filter((row) => row.status === 'active');
  const equity = rows.reduce((total, row) => total + row.equity, 0);
  const capital = rows.reduce((total, row) => total + row.capital, 0);
  const netPnl = equity - capital;
  const promotionReady = rows.filter((row) => row.promotion_ready).length;

  const queueTick = () => {
    tickAll.mutate(undefined, {
      onSuccess: () =>
        toast.success('Paper tick queued', {
          description: 'Active sessions will be advanced with live public market data.'
        }),
      onError: (error: Error) => toast.error(error.message)
    });
  };

  const queueEvaluation = () => {
    trigger.mutate(
      { jobType: 'paper_evaluate' },
      {
        onSuccess: () => toast.success('Promotion evaluation queued'),
        onError: (error: Error) => toast.error(error.message)
      }
    );
  };

  return (
    <PageContainer
      pageTitle='Paper sessions'
      pageDescription='Grid strategy running on live Binance public data with simulated fills, fees, slippage, and funding.'
      pageHeaderAction={
        <div className='flex flex-wrap items-center gap-2'>
          <Button
            variant='outline'
            size='sm'
            disabled={trigger.isPending}
            onClick={queueEvaluation}
          >
            <Icons.activity />
            Evaluate
          </Button>
          <Button size='sm' disabled={tickAll.isPending || active.length === 0} onClick={queueTick}>
            <Icons.flask />
            Tick all
          </Button>
        </div>
      }
    >
      <div className='space-y-4'>
        <div className='grid gap-4 md:grid-cols-2 xl:grid-cols-4'>
          <MetricCard
            title='Active sessions'
            value={`${active.length} / ${rows.length}`}
            hint='Paper only — no live orders are placed.'
            icon={<Icons.flask />}
          />
          <MetricCard
            title='Book equity'
            value={formatUsd(equity)}
            hint={`Capital ${formatUsd(capital)}`}
            icon={<Icons.activity />}
          />
          <MetricCard
            title='Net P&L'
            value={
              <span
                className={
                  netPnl >= 0
                    ? 'text-emerald-600 dark:text-emerald-400'
                    : 'text-rose-600 dark:text-rose-400'
                }
              >
                {formatUsd(netPnl, 2)}
              </span>
            }
            hint={`${formatPercent(capital ? netPnl / capital : 0)} on deployed capital`}
            icon={<Icons.chart />}
          />
          <MetricCard
            title='Promotion ready'
            value={promotionReady}
            hint='Sessions that pass every paper threshold and are eligible for approval review.'
            icon={<Icons.shield />}
          />
        </div>

        {sessions.isLoading ? (
          <div className='text-muted-foreground py-16 text-center text-sm'>
            Loading paper sessions…
          </div>
        ) : sessions.isError ? (
          <EmptyPanel
            title='Could not load paper sessions'
            description={(sessions.error as Error).message}
            action={<Button onClick={() => sessions.refetch()}>Retry</Button>}
          />
        ) : rows.length === 0 ? (
          <EmptyPanel
            title='No paper sessions yet'
            description='Run the daily analysis to rank the universe. Prime and viable coins bootstrap paper sessions automatically.'
          />
        ) : (
          <Card>
            <CardHeader>
              <CardTitle className='text-base'>Book</CardTitle>
              <CardDescription>
                Each session starts flat with isolated margin and the walk-forward recommended grid.
              </CardDescription>
            </CardHeader>
            <CardContent className='px-0'>
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Coin</TableHead>
                    <TableHead>Status</TableHead>
                    <TableHead>Bias</TableHead>
                    <TableHead className='text-right'>Equity</TableHead>
                    <TableHead className='text-right'>Net return</TableHead>
                    <TableHead className='text-right'>Max DD</TableHead>
                    <TableHead className='text-right'>Fills</TableHead>
                    <TableHead className='text-right'>W / L</TableHead>
                    <TableHead className='text-right'>Fees</TableHead>
                    <TableHead className='text-right'>Funding</TableHead>
                    <TableHead className='text-right'>Last tick</TableHead>
                    <TableHead />
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {rows.map((row) => (
                    <TableRow key={row.id}>
                      <TableCell>
                        <Link
                          href={`/dashboard/paper/${row.id}`}
                          className='font-medium hover:underline'
                        >
                          {row.coin.symbol}
                        </Link>
                        <div className='text-muted-foreground text-xs'>{row.coin.name}</div>
                      </TableCell>
                      <TableCell>
                        <div className='flex flex-wrap items-center gap-1'>
                          <StatusBadge value={row.status} />
                          {row.promotion_ready ? (
                            <Badge
                              variant='outline'
                              className='border-emerald-500/30 bg-emerald-500/10 text-emerald-600 dark:text-emerald-400'
                            >
                              Ready
                            </Badge>
                          ) : null}
                        </div>
                      </TableCell>
                      <TableCell>
                        <div className='flex items-center gap-0.5'>
                          <Button
                            variant='ghost'
                            size='icon-sm'
                            aria-label={`Mark ${row.coin.symbol} bullish`}
                            aria-pressed={row.bias === 'bullish'}
                            title='Bullish — grid trades normally'
                            disabled={row.status === 'stopped' || setBias.isPending}
                            className={
                              row.bias === 'bullish'
                                ? 'text-emerald-600 dark:text-emerald-400'
                                : 'text-muted-foreground'
                            }
                            onClick={() => markBias(row.id, 'bullish')}
                          >
                            <Icons.trendingUp />
                          </Button>
                          <Button
                            variant='ghost'
                            size='icon-sm'
                            aria-label={`Mark ${row.coin.symbol} bearish`}
                            aria-pressed={row.bias === 'bearish'}
                            title='Bearish — locks grid trading'
                            disabled={row.status === 'stopped' || setBias.isPending}
                            className={
                              row.bias === 'bearish'
                                ? 'text-rose-600 dark:text-rose-400'
                                : 'text-muted-foreground'
                            }
                            onClick={() => markBias(row.id, 'bearish')}
                          >
                            <Icons.trendingDown />
                          </Button>
                          {row.bias === 'bearish' ? (
                            <Badge
                              variant='outline'
                              className='ml-1 border-rose-500/30 bg-rose-500/10 text-rose-600 dark:text-rose-400'
                            >
                              Locked
                            </Badge>
                          ) : null}
                        </div>
                      </TableCell>
                      <TableCell className='text-right tabular-nums'>
                        {formatUsd(row.equity)}
                      </TableCell>
                      <TableCell
                        className={`text-right tabular-nums ${
                          row.net_return >= 0
                            ? 'text-emerald-600 dark:text-emerald-400'
                            : 'text-rose-600 dark:text-rose-400'
                        }`}
                      >
                        {formatPercent(row.net_return)}
                      </TableCell>
                      <TableCell className='text-right tabular-nums'>
                        {formatPercent(row.max_drawdown)}
                      </TableCell>
                      <TableCell className='text-right tabular-nums'>{row.fill_count}</TableCell>
                      <TableCell className='text-right tabular-nums'>
                        {row.wins} / {row.losses}
                      </TableCell>
                      <TableCell className='text-right tabular-nums'>
                        {formatUsd(row.fees_paid, 2)}
                      </TableCell>
                      <TableCell className='text-right tabular-nums'>
                        {formatUsd(row.funding_paid, 2)}
                      </TableCell>
                      <TableCell className='text-muted-foreground text-right text-xs'>
                        {relativeTime(row.last_tick_at)}
                      </TableCell>
                      <TableCell className='text-right'>
                        <Button
                          variant='outline'
                          size='sm'
                          nativeButton={false}
                          render={
                            <Link
                              href={`/dashboard/paper/${row.id}`}
                              aria-label={`Open ${row.coin.symbol} paper session`}
                            />
                          }
                        >
                          Open
                        </Button>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </CardContent>
          </Card>
        )}

        <p className='text-muted-foreground text-xs'>
          Paper fills use live public market data but never touch an exchange account. Promotion
          requires the configured minimum days, net return, drawdown, and fill thresholds. Passing
          thresholds only unlocks an approval review — live trading stays disabled by default.
          Marking a coin bearish locks new grid orders; existing inventory is held and still marked
          to market.
        </p>
      </div>
    </PageContainer>
  );
}
