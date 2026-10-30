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
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert';
import { Icons } from '@/components/icons';
import Link from 'next/link';
import { useState } from 'react';
import { toast } from 'sonner';
import {
  formatDate,
  formatPercent,
  formatUsd,
  metricsNumber,
  relativeTime
} from '@/features/grid/api';
import EmptyPanel from '@/features/grid/components/empty-panel';
import MetricCard from '@/features/grid/components/metric-card';
import RegimeBanner from '@/features/grid/components/regime-banner';
import StatusBadge from '@/features/grid/components/status-badge';
import { useDashboard, useJobsSummary, useTriggerJob } from '@/features/grid/queries';

const SUITABILITY_ORDER = ['prime', 'viable', 'watch', 'avoid', 'ineligible'] as const;

export default function DashboardPage() {
  const dashboard = useDashboard();
  const jobs = useJobsSummary();
  const trigger = useTriggerJob();
  const [pending, setPending] = useState<string | null>(null);

  const data = dashboard.data;
  const latest = data?.engine.latest_analysis;
  const results = data?.engine.results ?? [];
  const summary = (latest?.summary ?? {}) as Record<string, unknown>;
  const counts = data?.engine.suitability_counts ?? {};
  const paper = data?.paper;
  const recentJobs = jobs.data?.recent_runs ?? data?.jobs.recent ?? [];

  const runJob = (jobType: string, label: string) => {
    setPending(jobType);
    trigger.mutate(
      { jobType },
      {
        onSuccess: (job) => {
          toast.success(`${label} queued`, {
            description: `Job ${job.id.slice(0, 8)} is waiting for a worker.`
          });
        },
        onError: (error: Error) => toast.error(error.message),
        onSettled: () => setPending(null)
      }
    );
  };

  if (dashboard.isLoading) {
    return <PageContainer isLoading pageTitle='Grid suitability' pageDescription='' />;
  }

  if (dashboard.isError) {
    return (
      <PageContainer
        pageTitle='Grid suitability'
        pageDescription='Backtest-driven selection of coins for grid trading on Binance USDⓈ-M futures.'
      >
        <EmptyPanel
          title='The engine API is not reachable'
          description={(dashboard.error as Error).message}
          action={<Button onClick={() => dashboard.refetch()}>Retry</Button>}
        />
      </PageContainer>
    );
  }

  const eligible = Number(summary.eligible ?? 0);
  const requested = Number(summary.requested ?? results.length);
  const netPnl = (paper?.equity ?? 0) - (paper?.capital ?? 0);

  return (
    <PageContainer
      pageTitle='Grid suitability'
      pageDescription='Walk-forward backtests, paper trading, and risk gates for Binance USDⓈ-M futures.'
      pageHeaderAction={
        <div className='flex flex-wrap items-center gap-2'>
          <Button
            variant='outline'
            size='sm'
            disabled={pending !== null}
            onClick={() => runJob('coins_refresh', 'Coin refresh')}
          >
            <Icons.coins />
            Refresh coins
          </Button>
          <Button
            variant='outline'
            size='sm'
            disabled={pending !== null}
            onClick={() => runJob('paper_tick', 'Paper tick')}
          >
            <Icons.flask />
            Tick paper
          </Button>
          <Button
            size='sm'
            disabled={pending !== null}
            onClick={() => runJob('daily_analysis', 'Daily analysis')}
          >
            <Icons.chart />
            Run analysis
          </Button>
        </div>
      }
    >
      <div className='space-y-4'>
        <RegimeBanner />
        <Alert>
          <Icons.shield />
          <AlertTitle>Research and paper trading only</AlertTitle>
          <AlertDescription>
            Live Binance Futures orders are disabled by default and never placed by scheduled jobs.
            Promotion requires passing paper thresholds plus a separate approval record and released
            kill switch.
          </AlertDescription>
        </Alert>

        <div className='grid gap-4 md:grid-cols-2 xl:grid-cols-4'>
          <MetricCard
            title='Latest analysis'
            value={<StatusBadge value={latest?.status ?? 'none'} />}
            hint={
              latest
                ? `${latest.trigger} · ${formatDate(latest.started_at)} · ${latest.duration_seconds?.toFixed(1) ?? '—'}s`
                : 'No analysis run yet'
            }
            icon={<Icons.activity />}
          />
          <MetricCard
            title='Eligible universe'
            value={`${eligible} / ${requested || data?.universe.top100 || 0}`}
            hint={`${data?.universe.coins ?? 0} tracked coins, ${data?.universe.top100 ?? 0} in current top 100`}
            icon={<Icons.coins />}
          />
          <MetricCard
            title='Paper P&L'
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
            hint={`${paper?.active ?? 0} active · ${paper?.promotion_ready ?? 0} promotion-ready · avg ${formatPercent(paper?.avg_return)}`}
            icon={<Icons.flask />}
          />
          <MetricCard
            title='Live readiness'
            value={
              <span className={data?.live.live_ready ? 'text-emerald-600' : 'text-amber-600'}>
                {data?.live.live_ready ? 'Ready' : 'Locked'}
              </span>
            }
            hint={
              data?.live.kill_switch
                ? `Kill switch: ${data.live.kill_reason}`
                : 'Kill switch released; approval gates still apply'
            }
            icon={<Icons.shield />}
          />
        </div>

        <div className='grid gap-4 xl:grid-cols-3'>
          <Card className='xl:col-span-2'>
            <CardHeader className='flex flex-row items-start justify-between'>
              <div>
                <CardTitle>Top ranked coins</CardTitle>
                <CardDescription>
                  Out-of-sample results from the latest completed analysis, ranked by composite
                  suitability score.
                </CardDescription>
              </div>
              <Button
                variant='outline'
                size='sm'
                nativeButton={false}
                render={<Link href='/dashboard/rankings' aria-label='All rankings' />}
              >
                All rankings
                <Icons.arrowRight />
              </Button>
            </CardHeader>
            <CardContent>
              {results.length === 0 ? (
                <EmptyPanel
                  title='No rankings yet'
                  description='Run the daily analysis to sync Binance data and generate walk-forward results.'
                  action={
                    <Button size='sm' onClick={() => runJob('daily_analysis', 'Daily analysis')}>
                      Run analysis
                    </Button>
                  }
                />
              ) : (
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>#</TableHead>
                      <TableHead>Coin</TableHead>
                      <TableHead>Score</TableHead>
                      <TableHead>Class</TableHead>
                      <TableHead className='text-right'>OOS return</TableHead>
                      <TableHead className='text-right'>Max DD</TableHead>
                      <TableHead className='text-right'>Trades</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {results.slice(0, 10).map((result) => (
                      <TableRow key={result.id}>
                        <TableCell className='text-muted-foreground tabular-nums'>
                          {result.rank ?? '—'}
                        </TableCell>
                        <TableCell>
                          <Link
                            href={`/dashboard/coins/${result.coin.symbol}`}
                            className='font-medium hover:underline'
                          >
                            {result.coin.symbol}
                          </Link>
                          <div className='text-muted-foreground text-xs'>{result.coin.name}</div>
                        </TableCell>
                        <TableCell className='font-semibold tabular-nums'>
                          {result.score.toFixed(1)}
                        </TableCell>
                        <TableCell>
                          <StatusBadge value={result.suitability} />
                        </TableCell>
                        <TableCell
                          className={`text-right tabular-nums ${
                            metricsNumber(result.oos_metrics, 'net_return') >= 0
                              ? 'text-emerald-600 dark:text-emerald-400'
                              : 'text-rose-600 dark:text-rose-400'
                          }`}
                        >
                          {formatPercent(metricsNumber(result.oos_metrics, 'net_return'))}
                        </TableCell>
                        <TableCell className='text-right tabular-nums'>
                          {formatPercent(metricsNumber(result.oos_metrics, 'max_drawdown'))}
                        </TableCell>
                        <TableCell className='text-right tabular-nums'>
                          {metricsNumber(result.oos_metrics, 'trades')}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              )}
            </CardContent>
          </Card>

          <div className='space-y-4'>
            <Card>
              <CardHeader>
                <CardTitle>Suitability mix</CardTitle>
                <CardDescription>Latest analysis classification counts.</CardDescription>
              </CardHeader>
              <CardContent className='space-y-3'>
                {SUITABILITY_ORDER.map((key) => {
                  const total = results.length || 1;
                  const value = counts[key] ?? 0;
                  return (
                    <div key={key} className='space-y-1'>
                      <div className='flex items-center justify-between text-sm'>
                        <StatusBadge value={key} />
                        <span className='tabular-nums'>{value}</span>
                      </div>
                      <div className='bg-muted h-1.5 overflow-hidden rounded-full'>
                        <div
                          className='bg-primary h-full rounded-full'
                          style={{ width: `${Math.min(100, (value / total) * 100)}%` }}
                        />
                      </div>
                    </div>
                  );
                })}
              </CardContent>
            </Card>

            <Card>
              <CardHeader className='flex flex-row items-start justify-between'>
                <div>
                  <CardTitle>Paper book</CardTitle>
                  <CardDescription>Simulated, live-market data.</CardDescription>
                </div>
                <Button
                  variant='outline'
                  size='sm'
                  nativeButton={false}
                  render={<Link href='/dashboard/paper' aria-label='Open paper book' />}
                >
                  Open
                </Button>
              </CardHeader>
              <CardContent className='space-y-2 text-sm'>
                <div className='flex justify-between'>
                  <span className='text-muted-foreground'>Equity</span>
                  <span className='font-medium tabular-nums'>{formatUsd(paper?.equity)}</span>
                </div>
                <div className='flex justify-between'>
                  <span className='text-muted-foreground'>Capital</span>
                  <span className='tabular-nums'>{formatUsd(paper?.capital)}</span>
                </div>
                <div className='flex justify-between'>
                  <span className='text-muted-foreground'>Sessions</span>
                  <span className='tabular-nums'>{paper?.sessions ?? 0}</span>
                </div>
                <div className='flex justify-between'>
                  <span className='text-muted-foreground'>Promotion-ready</span>
                  <span className='tabular-nums'>{paper?.promotion_ready ?? 0}</span>
                </div>
              </CardContent>
            </Card>
          </div>
        </div>

        <div className='grid gap-4 xl:grid-cols-3'>
          <Card className='xl:col-span-2'>
            <CardHeader className='flex flex-row items-start justify-between'>
              <div>
                <CardTitle>Recent jobs</CardTitle>
                <CardDescription>
                  Queue activity across scheduler and manual triggers.
                </CardDescription>
              </div>
              <Button
                variant='outline'
                size='sm'
                nativeButton={false}
                render={<Link href='/dashboard/jobs' aria-label='Job console' />}
              >
                Job console
                <Icons.arrowRight />
              </Button>
            </CardHeader>
            <CardContent>
              {recentJobs.length === 0 ? (
                <p className='text-muted-foreground py-6 text-center text-sm'>
                  No jobs have run yet.
                </p>
              ) : (
                <div className='space-y-2'>
                  {recentJobs.slice(0, 6).map((job) => (
                    <div
                      key={job.id}
                      className='flex flex-wrap items-center justify-between gap-2 rounded-lg border px-3 py-2 text-sm'
                    >
                      <div className='flex items-center gap-2'>
                        <StatusBadge value={job.status} />
                        <span className='font-medium'>{job.job_type}</span>
                        <Badge variant='outline'>{job.trigger}</Badge>
                      </div>
                      <div className='text-muted-foreground flex items-center gap-3 text-xs'>
                        <span>{relativeTime(job.enqueued_at)}</span>
                        <span className='tabular-nums'>
                          {job.duration_seconds ? `${job.duration_seconds.toFixed(1)}s` : '—'}
                        </span>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Method guardrails</CardTitle>
              <CardDescription>What the ranking does and does not claim.</CardDescription>
            </CardHeader>
            <CardContent className='text-muted-foreground space-y-2 text-sm'>
              <p>
                Walk-forward folds select parameters on train windows only; OOS windows stay
                untouched.
              </p>
              <p>
                Fees, adverse slippage, funding, margin limits, and conservative intrabar paths are
                included.
              </p>
              <p>
                The grid starts flat, so rankings measure grid edge rather than buy-and-hold beta.
              </p>
              <p>Historical results do not guarantee future profitability.</p>
            </CardContent>
          </Card>
        </div>
      </div>
    </PageContainer>
  );
}
