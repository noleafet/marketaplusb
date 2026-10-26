'use client';

import PageContainer from '@/components/layout/page-container';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { Icons } from '@/components/icons';
import Link from 'next/link';
import { useState } from 'react';
import { formatDate, relativeTime } from '@/features/grid/api';
import EmptyPanel from '@/features/grid/components/empty-panel';
import MarkdownReport from '@/features/grid/components/markdown-report';
import StatusBadge from '@/features/grid/components/status-badge';
import { useReports } from '@/features/grid/queries';
import type { Report } from '@/features/grid/types';

const KINDS = [
  { value: 'all', label: 'All' },
  { value: 'ranking', label: 'Rankings' },
  { value: 'performance', label: 'Performance' },
  { value: 'risk', label: 'Risk' },
  { value: 'methodology', label: 'Methodology' }
];

export default function ReportsPage() {
  const reports = useReports();
  const [kind, setKind] = useState('all');
  const [selectedId, setSelectedId] = useState<number | null>(null);

  const all = reports.data?.results ?? [];
  const filtered = kind === 'all' ? all : all.filter((report) => report.kind === kind);
  const selected: Report | undefined =
    filtered.find((report) => report.id === selectedId) ?? filtered[0];

  return (
    <PageContainer
      pageTitle='Engine reports'
      pageDescription='Ranking, performance, risk, and methodology reports generated after each analysis run.'
      pageHeaderAction={
        <Button
          variant='outline'
          size='sm'
          nativeButton={false}
          render={<Link href='/dashboard/jobs' aria-label='Job console' />}
        >
          <Icons.clock />
          Job console
        </Button>
      }
    >
      <div className='space-y-4'>
        <Tabs value={kind} onValueChange={(value) => setKind(String(value))}>
          <TabsList>
            {KINDS.map((item) => (
              <TabsTrigger key={item.value} value={item.value}>
                {item.label}
              </TabsTrigger>
            ))}
          </TabsList>
        </Tabs>

        {reports.isLoading ? (
          <div className='text-muted-foreground py-16 text-center text-sm'>Loading reports…</div>
        ) : reports.isError ? (
          <EmptyPanel
            title='Could not load reports'
            description={(reports.error as Error).message}
            action={<Button onClick={() => reports.refetch()}>Retry</Button>}
          />
        ) : filtered.length === 0 ? (
          <EmptyPanel
            title='No reports in this category'
            description='Reports are written at the end of each daily analysis run.'
          />
        ) : (
          <div className='grid gap-4 xl:grid-cols-4'>
            <Card className='xl:col-span-1'>
              <CardHeader>
                <CardTitle className='text-base'>Archive</CardTitle>
                <CardDescription>{filtered.length} reports</CardDescription>
              </CardHeader>
              <CardContent className='max-h-[70vh] space-y-1 overflow-y-auto px-2'>
                {filtered.map((report) => {
                  const active = selected?.id === report.id;
                  return (
                    <button
                      key={report.id}
                      type='button'
                      onClick={() => setSelectedId(report.id)}
                      className={`w-full rounded-lg border px-3 py-2 text-left transition-colors ${
                        active ? 'bg-muted border-border' : 'hover:bg-muted/60 border-transparent'
                      }`}
                    >
                      <div className='flex items-center justify-between gap-2'>
                        <StatusBadge value={report.kind} />
                        <span className='text-muted-foreground text-xs'>
                          {relativeTime(report.created_at)}
                        </span>
                      </div>
                      <div className='mt-1 line-clamp-2 text-sm font-medium'>{report.title}</div>
                    </button>
                  );
                })}
              </CardContent>
            </Card>

            <Card className='xl:col-span-3'>
              {selected ? (
                <>
                  <CardHeader>
                    <div className='flex flex-wrap items-center justify-between gap-2'>
                      <div className='flex items-center gap-2'>
                        <StatusBadge value={selected.kind} />
                        <CardTitle className='text-base'>{selected.title}</CardTitle>
                      </div>
                      <span className='text-muted-foreground text-xs'>
                        {formatDate(selected.created_at)}
                        {selected.analysis ? ` · run ${selected.analysis.slice(0, 8)}` : ''}
                      </span>
                    </div>
                  </CardHeader>
                  <CardContent>
                    <MarkdownReport markdown={selected.body_markdown} />
                  </CardContent>
                </>
              ) : null}
            </Card>
          </div>
        )}

        <p className='text-muted-foreground text-xs'>
          Reports summarize out-of-sample walk-forward results. They are diagnostics, not investment
          advice, and backtested profitability does not guarantee future returns.
        </p>
      </div>
    </PageContainer>
  );
}
