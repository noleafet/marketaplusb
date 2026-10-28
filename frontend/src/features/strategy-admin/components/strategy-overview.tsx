'use client';

import { Icons } from '@/components/icons';
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { formatDate, formatPercent } from '@/features/grid/api';
import MetricCard from '@/features/grid/components/metric-card';
import StatusBadge from '@/features/grid/components/status-badge';
import { useDashboard, useResults } from '@/features/grid/queries';
import type { CoinResult } from '@/features/grid/types';
import { cn } from '@/lib/utils';
import Link from 'next/link';
import type { ReactNode } from 'react';
import {
  COIN_SETTINGS_PATH,
  SECTION_PATHS,
  STRATEGY_SECTIONS,
  THRESHOLD_ROWS
} from '../catalog';
import type { CatalogStat, CatalogTone } from '../catalog';
import { signedPercent, scoreTone, toNumber } from '../format';
import {
  DistributionBar,
  Meter,
  StatTile,
  ToneBadge,
  ToneNote,
  toneBar,
  toneText,
  toneTopBorder,
  toneSurface
} from './visuals';

const SUITABILITY_CLASSES: Array<{ key: string; tone: CatalogTone }> = [
  { key: 'prime', tone: 'good' },
  { key: 'viable', tone: 'info' },
  { key: 'watch', tone: 'warn' },
  { key: 'avoid', tone: 'bad' },
  { key: 'ineligible', tone: 'neutral' }
];

const SCORE_BUCKETS: Array<{ label: string; min: number; max: number; tone: CatalogTone }> = [
  { label: '85–100', min: 85, max: 101, tone: 'violet' },
  { label: '70–84', min: 70, max: 85, tone: 'good' },
  { label: '55–69', min: 55, max: 70, tone: 'info' },
  { label: '40–54', min: 40, max: 55, tone: 'warn' },
  { label: '< 40', min: -Infinity, max: 40, tone: 'bad' }
];

const REGIME_TONES: CatalogTone[] = ['info', 'violet', 'warn', 'good', 'bad'];

type SummaryCard = {
  key: string;
  title: string;
  description: string;
  path: string;
  icon: keyof typeof Icons;
  accent: CatalogTone;
  stats: CatalogStat[];
  preview: string[];
};

function regimeCountsFromResults(results: CoinResult[]): Array<[string, number]> {
  const counts: Record<string, number> = {};
  for (const result of results) {
    const label = result.diagnostics?.regime?.dominant_regime;
    if (!label) continue;
    counts[label] = (counts[label] ?? 0) + 1;
  }
  return Object.entries(counts).toSorted((left, right) => right[1] - left[1]);
}

export default function StrategyOverview() {
  const dashboard = useDashboard();
  const resultsQuery = useResults({ limit: 500 });

  const latest = dashboard.data?.engine.latest_analysis ?? null;
  const summary = (latest?.summary ?? {}) as Record<string, unknown>;
  const results = resultsQuery.data?.results ?? [];

  const universe = Number(summary.universe ?? results.length);
  const eligible = Number(summary.eligible ?? results.filter((row) => row.eligible).length);
  const flagged = Number(summary.flagged ?? 0);
  const dataErrors = Number(summary.data_errors ?? 0);
  const avgScore = toNumber(summary.avg_eligible_score);
  const avgBaseline = toNumber(summary.avg_baseline_score);
  const delta = avgScore !== null && avgBaseline !== null ? avgScore - avgBaseline : null;

  const classCounts = SUITABILITY_CLASSES.map((entry) => ({
    ...entry,
    count: results.filter((row) => row.suitability === entry.key).length
  }));
  const scoreRows = SCORE_BUCKETS.map((bucket) => ({
    ...bucket,
    count: results.filter((row) => row.score >= bucket.min && row.score < bucket.max).length
  }));
  const maxScoreCount = Math.max(1, ...scoreRows.map((row) => row.count));

  const summaryRegimes = (summary.regime_counts ?? {}) as Record<string, number>;
  const regimeRows: Array<[string, number]> = Object.keys(summaryRegimes).length
    ? Object.entries(summaryRegimes).toSorted((left, right) => right[1] - left[1])
    : regimeCountsFromResults(results);
  const maxRegimeCount = Math.max(1, ...regimeRows.map(([, count]) => count));

  const deltaTone: CatalogTone =
    delta === null ? 'neutral' : delta >= 0 ? 'good' : 'bad';

  const cards: SummaryCard[] = [
    {
      key: 'coin-settings',
      title: 'Coin-specific settings',
      description:
        'Thresholds from the latest completed run plus the ladder, range, overlay, and rebalancing parameters walk-forward selected for each coin.',
      path: COIN_SETTINGS_PATH,
      icon: 'settings',
      accent: 'info',
      stats: [
        { label: 'Live thresholds', value: String(THRESHOLD_ROWS.length), tone: 'info' },
        { label: 'Coin results', value: String(results.length), tone: 'violet' },
        { label: 'Eligible', value: String(eligible), tone: eligible ? 'good' : 'warn' },
        { label: 'Flagged', value: String(flagged), tone: flagged ? 'warn' : 'good' }
      ],
      preview: ['Runtime thresholds', 'Recommended parameters per coin', 'Gates, flags & folds']
    },
    ...STRATEGY_SECTIONS.map<SummaryCard>((section) => ({
      key: section.id,
      title: section.title,
      description: section.description,
      path: SECTION_PATHS[section.id],
      icon: section.icon,
      accent: section.accent,
      stats: section.stats,
      preview: section.blocks.slice(0, 4).map((block) => block.title)
    }))
  ];

  const notes: Array<{
    tone: CatalogTone;
    title: string;
    body: string;
    icon: ReactNode;
  }> = [];

  if (dashboard.isError) {
    notes.push({
      tone: 'bad',
      title: 'Engine API is not reachable',
      body: (dashboard.error as Error).message,
      icon: <Icons.warning className='size-4' />
    });
  } else if (!latest) {
    notes.push({
      tone: 'warn',
      title: 'No analysis run yet',
      body: 'Run the daily analysis to generate walk-forward results and per-coin parameters.',
      icon: <Icons.clock className='size-4' />
    });
  } else {
    const runTone: CatalogTone =
      latest.status === 'succeeded' ? 'good' : latest.status === 'failed' ? 'bad' : 'info';
    notes.push({
      tone: runTone,
      title: `Latest run ${latest.status}`,
      body: `${latest.trigger} trigger · ${formatDate(latest.started_at)} · ${latest.duration_seconds?.toFixed(1) ?? '—'}s`,
      icon: <Icons.activity className='size-4' />
    });
  }

  notes.push(
    dataErrors === 0
      ? {
          tone: 'good',
          title: 'No data errors',
          body: 'Every symbol in the universe produced enough completed candles to simulate.',
          icon: <Icons.circleCheck className='size-4' />
        }
      : {
          tone: 'bad',
          title: `${dataErrors} data errors`,
          body: 'Symbols with missing or insufficient candles were excluded before simulation.',
          icon: <Icons.warning className='size-4' />
        },
    flagged === 0
      ? {
          tone: 'good',
          title: 'No review flags',
          body: 'No eligible coin carries a concentration, cost-stress, or regime warning.',
          icon: <Icons.check className='size-4' />
        }
      : {
          tone: 'warn',
          title: `${flagged} coins carry review flags`,
          body: 'Open a coin on the settings page to inspect gates, flags, and score penalties.',
          icon: <Icons.warning className='size-4' />
        },
    delta === null
      ? {
          tone: 'neutral',
          title: 'Baseline comparison unavailable',
          body: 'The latest run did not persist a baseline score for the eligible cross-section.',
          icon: <Icons.info className='size-4' />
        }
      : {
          tone: deltaTone,
          title: `Average score ${delta >= 0 ? 'above' : 'below'} baseline`,
          body: `${avgScore?.toFixed(1) ?? '—'} versus baseline ${avgBaseline?.toFixed(1) ?? '—'} (${signedPercent(delta / 100, 1)}).`,
          icon: delta >= 0 ? <Icons.trendingUp className='size-4' /> : <Icons.trendingDown className='size-4' />
        }
  );

  return (
    <div className='space-y-6'>
      <Alert>
        <Icons.lock />
        <AlertTitle>Internal strategy reference</AlertTitle>
        <AlertDescription>
          Summary of every analysis, grid, backtest, and risk strategy the engine runs. Each
          section lives on its own page via the sidebar; the header link is the only route back to
          the dashboard.
        </AlertDescription>
      </Alert>

      {dashboard.isError ? (
        <Alert variant='destructive'>
          <Icons.warning />
          <AlertTitle>Live data is unavailable</AlertTitle>
          <AlertDescription>
            Static strategy sections still work. Counts and run health will populate once the
            engine API responds.
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
              : 'No completed run yet'
          }
          icon={<Icons.activity />}
        />
        <MetricCard
          title='Eligible universe'
          value={
            <span className={toneText[eligible > 0 ? 'good' : 'warn']}>
              {eligible}
              <span className='text-muted-foreground text-base font-normal'> / {universe || '—'}</span>
            </span>
          }
          hint={`${results.length} coin results loaded · ${flagged} flagged`}
          icon={<Icons.coins />}
        />
        <MetricCard
          title='Average score'
          value={
            <span className={toneText[scoreTone(avgScore)]}>
              {avgScore !== null ? avgScore.toFixed(1) : '—'}
            </span>
          }
          hint={
            delta !== null ? (
              <>
                Baseline {avgBaseline?.toFixed(1)} ·{' '}
                <span className={cn('font-medium', toneText[deltaTone])}>
                  {delta >= 0 ? '+' : ''}
                  {delta.toFixed(1)}
                </span>
              </>
            ) : (
              'Baseline comparison unavailable'
            )
          }
          icon={<Icons.chart />}
        />
        <MetricCard
          title='Dominant regime'
          value={
            regimeRows[0] ? regimeRows[0][0].replace(/_/g, ' ') : '—'
          }
          hint={regimeRows[0] ? `${regimeRows[0][1]} coins led by this regime` : 'No regime data yet'}
          icon={<Icons.grid />}
        />
      </div>

      <div className='grid gap-4 lg:grid-cols-2 xl:grid-cols-3'>
        <Card>
          <CardHeader>
            <CardTitle className='text-base'>Universe composition</CardTitle>
            <CardDescription>Latest results grouped by suitability class.</CardDescription>
          </CardHeader>
          <CardContent className='space-y-4'>
            <DistributionBar
              segments={classCounts.map((entry) => ({
                label: entry.key,
                value: entry.count,
                tone: entry.tone
              }))}
            />
            <div className='grid grid-cols-2 gap-x-4 gap-y-1.5'>
              {classCounts.map((entry) => (
                <div key={entry.key} className='flex items-center justify-between gap-2 text-sm'>
                  <span className='flex items-center gap-2 capitalize'>
                    <span className={cn('size-2 rounded-full', toneBar[entry.tone])} />
                    {entry.key}
                  </span>
                  <span className='font-medium tabular-nums'>{entry.count}</span>
                </div>
              ))}
            </div>
            <p className='text-muted-foreground text-xs'>
              {results.length
                ? `${formatPercent(results.length ? eligible / results.length : 0, 0)} of loaded results are eligible for ranking.`
                : 'No coin results loaded yet.'}
            </p>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className='text-base'>Score distribution</CardTitle>
            <CardDescription>Every coin result by final score band.</CardDescription>
          </CardHeader>
          <CardContent className='space-y-2.5'>
            {scoreRows.map((bucket) => (
              <div key={bucket.label} className='space-y-1'>
                <div className='flex items-center justify-between text-xs'>
                  <span className='text-muted-foreground tabular-nums'>{bucket.label}</span>
                  <span className='font-medium tabular-nums'>{bucket.count}</span>
                </div>
                <Meter value={bucket.count} max={maxScoreCount} tone={bucket.tone} />
              </div>
            ))}
            {results.length === 0 ? (
              <p className='text-muted-foreground text-xs'>
                Run the daily analysis to populate scores.
              </p>
            ) : null}
          </CardContent>
        </Card>

        <Card className='lg:col-span-2 xl:col-span-1'>
          <CardHeader>
            <CardTitle className='text-base'>Regime mix</CardTitle>
            <CardDescription>Dominant regime per coin in the latest run.</CardDescription>
          </CardHeader>
          <CardContent className='space-y-2.5'>
            {regimeRows.length ? (
              regimeRows.map(([label, count], index) => (
                <div key={label} className='flex items-center gap-3'>
                  <span className='text-muted-foreground w-28 shrink-0 truncate text-xs capitalize'>
                    {label.replace(/_/g, ' ')}
                  </span>
                  <Meter
                    value={count}
                    max={maxRegimeCount}
                    tone={REGIME_TONES[index % REGIME_TONES.length]}
                    className='flex-1'
                  />
                  <span className='w-6 shrink-0 text-right text-xs font-medium tabular-nums'>
                    {count}
                  </span>
                </div>
              ))
            ) : (
              <p className='text-muted-foreground text-xs'>No regime labels recorded yet.</p>
            )}
          </CardContent>
        </Card>
      </div>

      <div className='grid gap-4 lg:grid-cols-3'>
        <Card className='lg:col-span-2'>
          <CardHeader>
            <div className='flex flex-wrap items-center justify-between gap-2'>
              <div>
                <CardTitle className='text-base'>Strategy sections</CardTitle>
                <CardDescription>Five pages, reachable from the sidebar.</CardDescription>
              </div>
              <Badge variant='outline' className='text-muted-foreground'>
                Internal
              </Badge>
            </div>
          </CardHeader>
          <CardContent className='grid gap-3 sm:grid-cols-2'>
            {cards.map((card) => {
              const Icon = Icons[card.icon];
              return (
                <Link
                  key={card.key}
                  href={card.path}
                  aria-label={`Open ${card.title}`}
                  className='group block'
                >
                  <div
                    className={cn(
                      'h-full rounded-xl border p-3.5 transition-shadow hover:shadow-md',
                      toneSurface[card.accent],
                      toneTopBorder[card.accent]
                    )}
                  >
                    <div className='flex items-start gap-3'>
                      <div
                        className={cn(
                          'flex size-9 shrink-0 items-center justify-center rounded-lg border bg-background/60',
                          toneSurface[card.accent]
                        )}
                      >
                        <Icon className={cn('size-4', toneText[card.accent])} />
                      </div>
                      <div className='min-w-0 flex-1'>
                        <div className='flex items-center justify-between gap-2'>
                          <p className='text-sm font-semibold'>{card.title}</p>
                          <Icons.arrowRight className='text-muted-foreground size-4 shrink-0 transition-transform group-hover:translate-x-0.5' />
                        </div>
                        <p className='text-muted-foreground mt-1 line-clamp-3 text-xs leading-relaxed'>
                          {card.description}
                        </p>
                      </div>
                    </div>
                    <div className='mt-3 grid grid-cols-2 gap-1.5'>
                      {card.stats.map((stat) => (
                        <StatTile key={stat.label} {...stat} />
                      ))}
                    </div>
                    <div className='mt-3 flex flex-wrap gap-1.5'>
                      {card.preview.map((item) => (
                        <ToneBadge key={item} tone={card.accent} className='font-normal'>
                          {item}
                        </ToneBadge>
                      ))}
                    </div>
                  </div>
                </Link>
              );
            })}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className='text-base'>Run health</CardTitle>
            <CardDescription>Positive and negative signals from the latest run.</CardDescription>
          </CardHeader>
          <CardContent className='space-y-2'>
            {resultsQuery.isError ? (
              <ToneNote
                tone='bad'
                title='Coin results are not reachable'
                icon={<Icons.warning className='size-4' />}
              >
                {(resultsQuery.error as Error).message}
              </ToneNote>
            ) : null}
            {notes.map((note) => (
              <ToneNote key={note.title} tone={note.tone} title={note.title} icon={note.icon}>
                {note.body}
              </ToneNote>
            ))}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
