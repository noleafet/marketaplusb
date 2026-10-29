'use client';

import PageContainer from '@/components/layout/page-container';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle
} from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { NativeSelect, NativeSelectOption } from '@/components/ui/native-select';
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
import { useRouter } from 'next/navigation';
import { useMemo, useState } from 'react';
import type { FormEvent } from 'react';
import { toast } from 'sonner';
import { formatCompactUsd, formatPercent, metricsNumber } from '@/features/grid/api';
import EmptyPanel from '@/features/grid/components/empty-panel';
import StatusBadge from '@/features/grid/components/status-badge';
import { FACTOR_LABELS, formatGateValue } from '@/features/grid/diagnostics';
import { useAddCoin, useResults } from '@/features/grid/queries';
import type { CoinResult } from '@/features/grid/types';

export default function RankingsPage() {
  const results = useResults({ limit: 200 });
  const addCoin = useAddCoin();
  const router = useRouter();
  const [search, setSearch] = useState('');
  const [suitability, setSuitability] = useState('all');
  const [addOpen, setAddOpen] = useState(false);
  const [detail, setDetail] = useState<CoinResult | null>(null);
  const [symbol, setSymbol] = useState('');
  const [name, setName] = useState('');

  const submitCoin = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const ticker = symbol.trim().toUpperCase();
    if (!ticker) {
      toast.error('Enter a ticker symbol');
      return;
    }
    addCoin.mutate(
      { symbol: ticker, name: name.trim() || undefined },
      {
        onSuccess: ({ coin }) => {
          setAddOpen(false);
          setSymbol('');
          setName('');
          toast.success(`${coin.symbol} queued for solo analysis`, {
            description: 'Results appear on the coin page once the worker finishes.'
          });
          router.push(`/dashboard/coins/${coin.symbol}`);
        },
        onError: (error: Error) => toast.error(error.message)
      }
    );
  };

  const rows = useMemo(() => {
    const list = results.data?.results ?? [];
    return list.filter((row) => {
      const matchesSearch =
        !search ||
        row.coin.symbol.toLowerCase().includes(search.toLowerCase()) ||
        row.coin.name.toLowerCase().includes(search.toLowerCase());
      const matchesClass = suitability === 'all' || row.suitability === suitability;
      return matchesSearch && matchesClass;
    });
  }, [results.data, search, suitability]);

  return (
    <PageContainer
      pageTitle='Coin rankings'
      pageDescription='Composite suitability scores with out-of-sample performance, exposure quality, and execution costs.'
      pageHeaderAction={
        <div className='flex flex-wrap items-center gap-2'>
          <Button size='sm' onClick={() => setAddOpen(true)}>
            <Icons.add />
            Add coin
          </Button>
          <Button
            variant='outline'
            size='sm'
            nativeButton={false}
            render={<Link href='/dashboard' aria-label='Overview' />}
          >
            <Icons.dashboard />
            Overview
          </Button>
        </div>
      }
    >
      <div className='space-y-4'>
        <Card>
          <CardHeader>
            <CardTitle className='text-base'>Filters</CardTitle>
            <CardDescription>
              {results.data?.count ?? 0} coins evaluated in the latest analysis.
            </CardDescription>
          </CardHeader>
          <CardContent className='flex flex-wrap items-center gap-3'>
            <Input
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder='Search symbol or name'
              className='max-w-xs'
            />
            <NativeSelect
              value={suitability}
              onChange={(event) => setSuitability(event.target.value)}
              aria-label='Suitability filter'
            >
              <NativeSelectOption value='all'>All suitability classes</NativeSelectOption>
              <NativeSelectOption value='prime'>Prime</NativeSelectOption>
              <NativeSelectOption value='viable'>Viable</NativeSelectOption>
              <NativeSelectOption value='watch'>Watch</NativeSelectOption>
              <NativeSelectOption value='avoid'>Avoid</NativeSelectOption>
              <NativeSelectOption value='ineligible'>Ineligible</NativeSelectOption>
            </NativeSelect>
            <span className='text-muted-foreground text-sm'>{rows.length} shown</span>
          </CardContent>
        </Card>

        {results.isLoading ? (
          <div className='text-muted-foreground py-16 text-center text-sm'>Loading rankings…</div>
        ) : results.isError ? (
          <EmptyPanel
            title='Could not load rankings'
            description={(results.error as Error).message}
            action={<Button onClick={() => results.refetch()}>Retry</Button>}
          />
        ) : rows.length === 0 ? (
          <EmptyPanel
            title='No matching coins'
            description='Run the daily analysis or relax the filters.'
          />
        ) : (
          <Card>
            <CardContent className='px-0'>
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>#</TableHead>
                    <TableHead>Coin</TableHead>
                    <TableHead>Class</TableHead>
                    <TableHead className='text-right'>Score</TableHead>
                    <TableHead>Review</TableHead>
                    <TableHead className='text-right'>OOS return</TableHead>
                    <TableHead className='text-right'>Max DD</TableHead>
                    <TableHead className='text-right'>PF</TableHead>
                    <TableHead className='text-right'>Trades</TableHead>
                    <TableHead className='text-right'>ADV</TableHead>
                    <TableHead className='text-right'>ATR%</TableHead>
                    <TableHead className='text-right'>Funding</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {rows.map((row) => {
                    const oos = row.oos_metrics;
                    const features = row.features ?? {};
                    return (
                      <TableRow key={row.id}>
                        <TableCell className='text-muted-foreground tabular-nums'>
                          {row.rank ?? '—'}
                        </TableCell>
                        <TableCell>
                          <Link
                            href={`/dashboard/coins/${row.coin.symbol}`}
                            className='font-medium hover:underline'
                          >
                            {row.coin.symbol}
                          </Link>
                          <div className='text-muted-foreground text-xs'>{row.coin.name}</div>
                        </TableCell>
                        <TableCell>
                          <StatusBadge value={row.suitability} />
                        </TableCell>
                        <TableCell className='text-right font-semibold tabular-nums'>
                          {row.score.toFixed(1)}
                        </TableCell>
                        <TableCell>
                          <Button
                            variant='ghost'
                            size='sm'
                            className='h-7 gap-1 px-2'
                            onClick={() => setDetail(row)}
                            aria-label={`Open diagnostics for ${row.coin.symbol}`}
                          >
                            {row.diagnostics?.flags?.length ? (
                              <>
                                <Icons.warning className='size-3.5 text-amber-500' />
                                <span className='text-xs'>{row.diagnostics.flags.length}</span>
                              </>
                            ) : (
                              <Icons.circleCheck className='size-3.5 text-emerald-500' />
                            )}
                          </Button>
                        </TableCell>
                        <TableCell
                          className={`text-right tabular-nums ${
                            metricsNumber(oos, 'net_return') >= 0
                              ? 'text-emerald-600 dark:text-emerald-400'
                              : 'text-rose-600 dark:text-rose-400'
                          }`}
                        >
                          {formatPercent(metricsNumber(oos, 'net_return'))}
                        </TableCell>
                        <TableCell className='text-right tabular-nums'>
                          {formatPercent(metricsNumber(oos, 'max_drawdown'))}
                        </TableCell>
                        <TableCell className='text-right tabular-nums'>
                          {metricsNumber(oos, 'profit_factor').toFixed(2)}
                        </TableCell>
                        <TableCell className='text-right tabular-nums'>
                          {metricsNumber(oos, 'trades')}
                        </TableCell>
                        <TableCell className='text-right tabular-nums'>
                          {formatCompactUsd(metricsNumber(features, 'adv_usd_median'))}
                        </TableCell>
                        <TableCell className='text-right tabular-nums'>
                          {formatPercent(metricsNumber(features, 'atr_pct'))}
                        </TableCell>
                        <TableCell className='text-right tabular-nums'>
                          {formatPercent(metricsNumber(features, 'funding_mean'), 4)}
                        </TableCell>
                      </TableRow>
                    );
                  })}
                </TableBody>
              </Table>
            </CardContent>
          </Card>
        )}

        <p className='text-muted-foreground text-xs'>
          Eligibility gates run before ranking: liquidity, sample size, positive OOS return,
          drawdown, fold consistency, ATR band, spread, order-book depth, cost stress, and
          profit-factor validation. Eligible score blends profitability 18%, risk-adjusted
          return 18%, drawdown control 15%, consistency 14%, regime 10%, stability 10%,
          execution 10%, and diversification 5%. Past performance is not a guarantee.
        </p>
      </div>

      <Dialog open={Boolean(detail)} onOpenChange={(open) => !open && setDetail(null)}>
        <DialogContent className='max-h-[85vh] overflow-y-auto sm:max-w-2xl'>
          <DialogHeader>
            <DialogTitle>{detail?.coin.symbol} diagnostics</DialogTitle>
            <DialogDescription>
              Score {detail?.score.toFixed(1)} · baseline{' '}
              {detail?.diagnostics?.baseline_score?.toFixed(1) ?? '—'} · change{' '}
              {detail?.diagnostics?.score_delta !== undefined
                ? `${detail.diagnostics.score_delta >= 0 ? '+' : ''}${detail.diagnostics.score_delta.toFixed(1)}`
                : '—'}
            </DialogDescription>
          </DialogHeader>
          {detail ? (
            <div className='space-y-5'>
              <div>
                <h3 className='mb-2 text-sm font-medium'>Review flags</h3>
                {detail.diagnostics?.flags?.length ? (
                  <ul className='space-y-1.5 text-sm'>
                    {detail.diagnostics.flags.map((flag) => (
                      <li key={flag.code} className='flex gap-2'>
                        <Icons.warning
                          className={`mt-0.5 size-3.5 shrink-0 ${
                            flag.severity === 'critical' ? 'text-rose-500' : 'text-amber-500'
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
                <h3 className='mb-2 text-sm font-medium'>Hard eligibility gates</h3>
                <div className='space-y-1.5 text-sm'>
                  {(detail.diagnostics?.hard_gates ?? []).map((gate) => (
                    <div key={gate.name} className='flex items-start justify-between gap-3'>
                      <span className='text-muted-foreground'>
                        {gate.label}
                        {gate.reason ? (
                          <span className='block text-xs text-rose-500'>{gate.reason}</span>
                        ) : null}
                      </span>
                      <span
                        className={`shrink-0 tabular-nums ${
                          gate.passed ? 'text-emerald-600 dark:text-emerald-400' : 'text-rose-600'
                        }`}
                      >
                        {gate.passed ? 'pass' : 'fail'} · {formatGateValue(gate.value, gate.name)}
                      </span>
                    </div>
                  ))}
                </div>
              </div>

              <div>
                <h3 className='mb-2 text-sm font-medium'>Score contributions</h3>
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Factor</TableHead>
                      <TableHead className='text-right'>Score</TableHead>
                      <TableHead className='text-right'>Weight</TableHead>
                      <TableHead className='text-right'>Contribution</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {Object.entries(detail.diagnostics?.score_breakdown?.factors ?? {}).map(
                      ([key, factor]) => (
                        <TableRow key={key}>
                          <TableCell>{FACTOR_LABELS[key] ?? key.replace(/_/g, ' ')}</TableCell>
                          <TableCell className='text-right tabular-nums'>
                            {factor.score.toFixed(1)}
                          </TableCell>
                          <TableCell className='text-right tabular-nums'>
                            {(factor.weight * 100).toFixed(0)}%
                          </TableCell>
                          <TableCell className='text-right tabular-nums'>
                            {factor.contribution.toFixed(1)}
                          </TableCell>
                        </TableRow>
                      )
                    )}
                  </TableBody>
                </Table>
                {detail.diagnostics?.score_breakdown?.penalties?.length ? (
                  <ul className='text-muted-foreground mt-2 space-y-1 text-xs'>
                    {detail.diagnostics.score_breakdown.penalties.map((penalty) => (
                      <li key={penalty.code}>
                        −{penalty.points.toFixed(1)} · {penalty.reason}
                      </li>
                    ))}
                  </ul>
                ) : null}
              </div>

              <div className='grid gap-3 text-sm sm:grid-cols-2'>
                <div>
                  <h3 className='mb-1 text-sm font-medium'>Regime robustness</h3>
                  <p className='text-muted-foreground'>
                    {detail.diagnostics?.regime?.robustness_score?.toFixed(1) ?? '—'} / 100 ·{' '}
                    {detail.diagnostics?.regime?.regimes_covered ?? 0} regimes · dominant{' '}
                    {detail.diagnostics?.regime?.dominant_regime ?? '—'}
                  </p>
                </div>
                <div>
                  <h3 className='mb-1 text-sm font-medium'>Cost stress</h3>
                  <p className='text-muted-foreground'>
                    {detail.diagnostics?.cost_stress?.multiplier ?? 2}× costs ·{' '}
                    {formatPercent(detail.diagnostics?.cost_stress?.stress_return ?? 0)} net ·{' '}
                    {detail.diagnostics?.cost_stress?.profitable ? 'profitable' : 'fails'}
                  </p>
                </div>
                <div>
                  <h3 className='mb-1 text-sm font-medium'>Inventory & recovery</h3>
                  <p className='text-muted-foreground'>
                    peak exposure{' '}
                    {detail.diagnostics?.exposure?.max_exposure_ratio?.toFixed(2) ?? '—'}× ·
                    recovery {detail.diagnostics?.recovery_days?.toFixed(0) ?? '—'}d
                  </p>
                </div>
                <div>
                  <h3 className='mb-1 text-sm font-medium'>Correlation</h3>
                  <p className='text-muted-foreground'>
                    avg {detail.diagnostics?.correlation?.avg_correlation?.toFixed(2) ?? '—'} ·
                    peak {detail.diagnostics?.correlation?.max_correlation?.toFixed(2) ?? '—'}
                  </p>
                </div>
              </div>

              {detail.diagnostics?.eligibility_reasons?.length ? (
                <div>
                  <h3 className='mb-1 text-sm font-medium'>Rejection reasons</h3>
                  <ul className='text-muted-foreground list-disc space-y-1 pl-5 text-sm'>
                    {detail.diagnostics.eligibility_reasons.map((reason) => (
                      <li key={reason}>{reason}</li>
                    ))}
                  </ul>
                </div>
              ) : null}
            </div>
          ) : null}
        </DialogContent>
      </Dialog>

      <Dialog open={addOpen} onOpenChange={setAddOpen}>
        <DialogContent>
          <form className='space-y-4' onSubmit={submitCoin}>
            <DialogHeader>
              <DialogTitle>Add a coin</DialogTitle>
              <DialogDescription>
                Validated against Binance USDⓈ-M USDT perpetuals. The coin runs its own
                walk-forward analysis and does not join the scheduled universe.
              </DialogDescription>
            </DialogHeader>
            <div className='space-y-2'>
              <Label htmlFor='coin-symbol'>Ticker</Label>
              <Input
                id='coin-symbol'
                value={symbol}
                onChange={(event) => setSymbol(event.target.value)}
                placeholder='e.g. TAO'
                maxLength={15}
                autoFocus
              />
            </div>
            <div className='space-y-2'>
              <Label htmlFor='coin-name'>Name (optional)</Label>
              <Input
                id='coin-name'
                value={name}
                onChange={(event) => setName(event.target.value)}
                placeholder='e.g. Bittensor'
                maxLength={160}
              />
            </div>
            <DialogFooter>
              <Button type='button' variant='outline' onClick={() => setAddOpen(false)}>
                Cancel
              </Button>
              <Button type='submit' disabled={addCoin.isPending}>
                {addCoin.isPending ? 'Queuing…' : 'Add and analyse'}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </PageContainer>
  );
}
