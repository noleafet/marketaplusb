'use client';

import PageContainer from '@/components/layout/page-container';
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
import { Icons } from '@/components/icons';
import Link from 'next/link';
import { useMemo, useState } from 'react';
import { formatCompactUsd, formatDate } from '@/features/grid/api';
import EmptyPanel from '@/features/grid/components/empty-panel';
import { useCoins } from '@/features/grid/queries';

export default function CoinsPage() {
  const coins = useCoins();
  const [search, setSearch] = useState('');
  const [eligibility, setEligibility] = useState('all');
  const [membership, setMembership] = useState('all');

  const rows = useMemo(() => {
    const list = coins.data?.results ?? [];
    return list.filter((coin) => {
      const matchesSearch =
        !search ||
        coin.symbol.toLowerCase().includes(search.toLowerCase()) ||
        coin.name.toLowerCase().includes(search.toLowerCase());
      const matchesEligibility =
        eligibility === 'all' ||
        (eligibility === 'eligible' ? coin.analysis_eligible : !coin.analysis_eligible);
      const matchesMembership =
        membership === 'all' || (membership === 'current' ? coin.in_top100 : !coin.in_top100);
      return matchesSearch && matchesEligibility && matchesMembership;
    });
  }, [coins.data, search, eligibility, membership]);

  return (
    <PageContainer
      pageTitle='Coin universe'
      pageDescription='Monthly top-100 snapshots are retained forever; new coins are added and dropped coins stay queryable.'
      pageHeaderAction={
        <Button
          variant='outline'
          size='sm'
          nativeButton={false}
          render={<Link href='/dashboard' aria-label='Overview' />}
        >
          <Icons.dashboard />
          Overview
        </Button>
      }
    >
      <div className='space-y-4'>
        <Card>
          <CardHeader>
            <CardTitle className='text-base'>Universe filters</CardTitle>
            <CardDescription>
              {coins.data?.count ?? 0} coins stored. Stablecoins and coins without a Binance USDT
              perpetual are excluded from analysis.
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
              value={eligibility}
              onChange={(event) => setEligibility(event.target.value)}
              aria-label='Eligibility filter'
            >
              <NativeSelectOption value='all'>All eligibility</NativeSelectOption>
              <NativeSelectOption value='eligible'>Eligible</NativeSelectOption>
              <NativeSelectOption value='excluded'>Excluded</NativeSelectOption>
            </NativeSelect>
            <NativeSelect
              value={membership}
              onChange={(event) => setMembership(event.target.value)}
              aria-label='Top-100 membership filter'
            >
              <NativeSelectOption value='all'>All snapshots</NativeSelectOption>
              <NativeSelectOption value='current'>In current top 100</NativeSelectOption>
              <NativeSelectOption value='dropped'>Dropped from top 100</NativeSelectOption>
            </NativeSelect>
            <span className='text-muted-foreground text-sm'>{rows.length} shown</span>
          </CardContent>
        </Card>

        {coins.isLoading ? (
          <div className='text-muted-foreground py-16 text-center text-sm'>Loading coins…</div>
        ) : coins.isError ? (
          <EmptyPanel
            title='Could not load coins'
            description={(coins.error as Error).message}
            action={<Button onClick={() => coins.refetch()}>Retry</Button>}
          />
        ) : rows.length === 0 ? (
          (coins.data?.results.length ?? 0) > 0 ? (
            <EmptyPanel
              title='No coins match these filters'
              description='Clear the search or widen the eligibility and snapshot filters.'
              action={
                <Button
                  variant='outline'
                  onClick={() => {
                    setSearch('');
                    setEligibility('all');
                    setMembership('all');
                  }}
                >
                  Clear filters
                </Button>
              }
            />
          ) : (
            <EmptyPanel
              title='No coins stored'
              description='Trigger the monthly coin refresh job to ingest the top 100.'
            />
          )
        ) : (
          <Card>
            <CardContent className='px-0'>
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>#</TableHead>
                    <TableHead>Coin</TableHead>
                    <TableHead className='text-right'>Market cap</TableHead>
                    <TableHead>Binance contract</TableHead>
                    <TableHead>Top 100</TableHead>
                    <TableHead>Analysis</TableHead>
                    <TableHead>Last updated</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {rows.map((coin) => (
                    <TableRow key={coin.id}>
                      <TableCell className='text-muted-foreground tabular-nums'>
                        {coin.rank ?? '—'}
                      </TableCell>
                      <TableCell>
                        {coin.analysis_eligible ? (
                          <Link
                            href={`/dashboard/coins/${coin.symbol}`}
                            className='font-medium hover:underline'
                          >
                            {coin.symbol}
                          </Link>
                        ) : (
                          <span className='font-medium'>{coin.symbol}</span>
                        )}
                        <div className='text-muted-foreground text-xs'>{coin.name}</div>
                      </TableCell>
                      <TableCell className='text-right tabular-nums'>
                        {formatCompactUsd(coin.market_cap_usd)}
                      </TableCell>
                      <TableCell className='font-mono text-xs'>
                        {coin.binance_symbol || '—'}
                        {coin.binance_contract_multiplier !== 1 ? (
                          <span className='text-muted-foreground'>
                            {' '}
                            ×{coin.binance_contract_multiplier}
                          </span>
                        ) : null}
                      </TableCell>
                      <TableCell>
                        <span
                          className={coin.in_top100 ? 'text-emerald-600' : 'text-muted-foreground'}
                        >
                          {coin.in_top100 ? 'Current' : 'Dropped'}
                        </span>
                      </TableCell>
                      <TableCell>
                        {coin.analysis_eligible ? (
                          <span className='text-emerald-600'>Eligible</span>
                        ) : (
                          <span className='text-muted-foreground text-xs'>
                            {coin.analysis_exclusion_reason || 'Excluded'}
                          </span>
                        )}
                      </TableCell>
                      <TableCell className='text-muted-foreground text-xs'>
                        {formatDate(coin.last_updated_at)}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </CardContent>
          </Card>
        )}
      </div>
    </PageContainer>
  );
}
