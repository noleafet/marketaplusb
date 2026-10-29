'use client';

import { Icons } from '@/components/icons';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow
} from '@/components/ui/table';
import { formatDate, formatPercent } from '@/features/grid/api';
import { useStrategyValidation } from '@/features/grid/queries';
import type {
  CorrelationPair,
  InformationCoefficient,
  PooledIc,
  StrategyValidation,
  ValidationFinding
} from '@/features/grid/types';
import { cn } from '@/lib/utils';
import type { CatalogTone } from '../catalog';
import { signedPercent } from '../format';
import { Meter, StatTile, ToneBadge, ToneNote, toneText } from './visuals';

const FACTOR_LABELS: Record<string, string> = {
  profitability: 'Profitability',
  risk_adjusted: 'Risk-adjusted return',
  drawdown_control: 'Drawdown control',
  consistency: 'Fold consistency',
  regime: 'Regime robustness',
  stability: 'Structural stability',
  execution: 'Execution quality',
  diversification: 'Diversification'
};

function fixed(value: number | null | undefined, digits = 2): string {
  return value === null || value === undefined || !Number.isFinite(value)
    ? '—'
    : value.toFixed(digits);
}

function signed(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—';
  return `${value >= 0 ? '+' : ''}${value.toFixed(digits)}`;
}

function icTone(entry: { ic?: number | null; significant?: boolean } | undefined): CatalogTone {
  const ic = entry?.ic;
  if (ic === null || ic === undefined) return 'neutral';
  if (entry?.significant) return ic > 0 ? 'good' : 'bad';
  return ic > 0 ? 'info' : 'warn';
}

function correlationTone(value: number | null | undefined, limit: number): CatalogTone {
  if (value === null || value === undefined) return 'neutral';
  if (value >= limit) return 'bad';
  if (value >= limit * 0.75) return 'warn';
  return value <= 0.3 ? 'good' : 'info';
}

function FindingList({
  items,
  kind
}: {
  items: ValidationFinding[];
  kind: ValidationFinding['kind'];
}) {
  const strength = kind === 'strength';
  const tone: CatalogTone = strength ? 'good' : 'bad';
  return (
    <div className='space-y-2'>
      <div className='flex items-center gap-2'>
        {strength ? (
          <Icons.circleCheck className={cn('size-4', toneText.good)} />
        ) : (
          <Icons.warning className={cn('size-4', toneText.bad)} />
        )}
        <p className='text-sm font-medium'>{strength ? 'Strengths' : 'Weaknesses'}</p>
        <ToneBadge tone={tone}>{items.length}</ToneBadge>
      </div>
      {items.length ? (
        items.map((item) => (
          <ToneNote key={item.code} tone={tone} title={item.title}>
            {item.detail}
          </ToneNote>
        ))
      ) : (
        <p className='text-muted-foreground rounded-lg border border-dashed p-3 text-xs'>
          {strength
            ? 'No measured strength yet. Strengths only appear when the data supports them.'
            : 'No weakness detected in the latest run.'}
        </p>
      )}
    </div>
  );
}

function IcRow({
  label,
  current,
  pooled
}: {
  label: string;
  current?: InformationCoefficient;
  pooled?: PooledIc;
}) {
  const tone = icTone(current);
  return (
    <TableRow>
      <TableCell className='font-medium'>{label}</TableCell>
      <TableCell className={cn('text-right tabular-nums', toneText[tone])}>
        {signed(current?.ic)}
      </TableCell>
      <TableCell className='text-muted-foreground text-right tabular-nums'>
        {fixed(current?.t_stat, 1)}
      </TableCell>
      <TableCell className='text-right tabular-nums'>
        {pooled?.runs ? `${signed(pooled.mean_ic)} · ${pooled.runs}r` : '—'}
      </TableCell>
      <TableCell className='w-24'>
        {current?.significant ? (
          <ToneBadge tone={tone}>{(current.ic ?? 0) > 0 ? 'Predictive' : 'Inverted'}</ToneBadge>
        ) : (
          <ToneBadge tone='neutral'>Not significant</ToneBadge>
        )}
      </TableCell>
    </TableRow>
  );
}

function PairList({ pairs, limit }: { pairs: CorrelationPair[]; limit: number }) {
  if (!pairs.length) {
    return <p className='text-muted-foreground text-xs'>Not enough overlapping days yet.</p>;
  }
  return (
    <div className='space-y-1.5'>
      {pairs.slice(0, 6).map((pair) => (
        <div key={`${pair.a}-${pair.b}`} className='flex items-center gap-2 text-xs'>
          <span className='w-28 shrink-0 truncate font-medium'>
            {pair.a} / {pair.b}
          </span>
          <Meter
            value={Math.max(0, pair.correlation)}
            max={1}
            tone={correlationTone(pair.correlation, limit)}
            label={pair.correlation.toFixed(2)}
            className='flex-1'
          />
        </div>
      ))}
    </div>
  );
}

function ValidationBody({ data }: { data: StrategyValidation }) {
  const scores = data.score_validation;
  const strategies = data.strategy_validation;
  const limit = data.settings.max_strategy_correlation;
  const strengths = data.findings.filter((item) => item.kind === 'strength');
  const weaknesses = data.findings.filter((item) => item.kind === 'weakness');
  const backtestPairs = data.backtest_correlation.pairs ?? [];
  const avgStrategyCorr =
    strategies?.strategy_correlation.avg_pair ?? data.backtest_correlation.avg_pair_correlation;

  return (
    <CardContent className='space-y-6'>
      {!data.analysis ? (
        <ToneNote tone='info' title='No analysis yet' icon={<Icons.info className='size-4' />}>
          Run the daily analysis to validate the score and measure strategy correlation. Results
          appear here automatically.
        </ToneNote>
      ) : (
        <p className='text-muted-foreground text-xs'>
          Latest run {formatDate(data.analysis.started_at)} · {data.analysis.interval ?? '—'}{' '}
          candles · {data.analysis.fill_model?.replace('_', '-') ?? '—'} fills ·{' '}
          {data.analysis.holdout_days ? `${data.analysis.holdout_days}-day holdout` : 'no holdout'}{' '}
          ·{data.analysis.eligible ?? 0}/{data.analysis.universe ?? 0} eligible
        </p>
      )}

      {strategies?.scope === 'universe' ? (
        <ToneNote
          tone='warn'
          title='Measured on every analysed coin'
          icon={<Icons.info className='size-4' />}
        >
          Fewer than three coins qualified in this run, so correlation, BTC beta, cost-stress, and
          holdout diagnostics cover all {strategies.coins ?? 0} analysed strategies. They describe
          the strategy across the universe, not the paper book.
        </ToneNote>
      ) : null}

      <div className='grid gap-4 lg:grid-cols-2'>
        <FindingList items={strengths} kind='strength' />
        <FindingList items={weaknesses} kind='weakness' />
      </div>

      <div className='grid gap-3 sm:grid-cols-2 xl:grid-cols-4'>
        <StatTile
          label='Score → holdout IC'
          value={signed(scores?.score.ic)}
          hint={
            scores
              ? `t = ${fixed(scores.score.t_stat, 1)} · n = ${scores.score.n}`
              : 'Needs a holdout result'
          }
          tone={icTone(scores?.score)}
        />
        <StatTile
          label='Strategy correlation'
          value={fixed(avgStrategyCorr)}
          hint={`Price ${fixed(strategies?.price_correlation.avg_pair)} · limit ${fixed(limit)}`}
          tone={correlationTone(avgStrategyCorr, limit)}
        />
        <StatTile
          label='BTC correlation'
          value={fixed(strategies?.btc_correlation_mean)}
          hint={`Beta ${fixed(strategies?.btc_beta_mean)}`}
          tone={
            strategies?.btc_correlation_mean === null ||
            strategies?.btc_correlation_mean === undefined
              ? 'neutral'
              : strategies.btc_correlation_mean >= 0.5
                ? 'bad'
                : strategies.btc_correlation_mean <= 0.25
                  ? 'good'
                  : 'warn'
          }
        />
        <StatTile
          label='Holdout vs folds'
          value={formatPercent(strategies?.holdout_annualized_mean, 1)}
          hint={`Folds ${formatPercent(strategies?.oos_annualized_mean, 1)} / yr`}
          tone={
            strategies?.holdout_annualized_mean === null ||
            strategies?.holdout_annualized_mean === undefined
              ? 'neutral'
              : strategies.holdout_annualized_mean > 0
                ? 'good'
                : 'bad'
          }
        />
      </div>

      <div className='grid gap-6 xl:grid-cols-2'>
        <div className='space-y-2'>
          <div>
            <p className='text-sm font-medium'>Does the score predict unseen returns?</p>
            <p className='text-muted-foreground text-xs'>
              Rank correlation (IC) of each factor with the untouched holdout return. Pooled values
              average the last runs; |t| ≥ 2 counts as significant.
            </p>
          </div>
          <div className='overflow-x-auto rounded-lg border'>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Signal</TableHead>
                  <TableHead className='text-right'>IC</TableHead>
                  <TableHead className='text-right'>t</TableHead>
                  <TableHead className='text-right'>Pooled</TableHead>
                  <TableHead />
                </TableRow>
              </TableHeader>
              <TableBody>
                <IcRow label='Final score' current={scores?.score} pooled={data.pooled_ic.score} />
                <IcRow label='OOS return' current={scores?.oos_return} />
                {Object.keys(FACTOR_LABELS).map((name) => (
                  <IcRow
                    key={name}
                    label={FACTOR_LABELS[name]}
                    current={scores?.factors?.[name]}
                    pooled={data.pooled_ic[name]}
                  />
                ))}
              </TableBody>
            </Table>
          </div>
          {scores ? (
            <p className='text-muted-foreground text-[11px]'>
              Holdout mean: top third by score {signedPercent(scores.top_tercile_holdout_mean)} ·
              bottom third {signedPercent(scores.bottom_tercile_holdout_mean)} · eligible{' '}
              {signedPercent(scores.eligible_holdout_mean)} vs ineligible{' '}
              {signedPercent(scores.ineligible_holdout_mean)}.
            </p>
          ) : null}
        </div>

        <div className='space-y-4'>
          <div>
            <p className='text-sm font-medium'>How much of the book is one trade?</p>
            <p className='text-muted-foreground text-xs'>
              Correlation of the grids&apos; own daily returns. Backtest pairs come from
              walk-forward and holdout days; paper pairs from live session equity.
            </p>
          </div>
          <div className='grid gap-4 sm:grid-cols-2'>
            <div className='space-y-2 rounded-lg border p-3'>
              <div className='flex items-center justify-between gap-2'>
                <p className='text-xs font-medium'>Backtest · most correlated</p>
                <ToneBadge
                  tone={correlationTone(data.backtest_correlation.max_pair_correlation, limit)}
                >
                  max {fixed(data.backtest_correlation.max_pair_correlation)}
                </ToneBadge>
              </div>
              <PairList pairs={backtestPairs} limit={limit} />
            </div>
            <div className='space-y-2 rounded-lg border p-3'>
              <div className='flex items-center justify-between gap-2'>
                <p className='text-xs font-medium'>Paper · live sessions</p>
                <ToneBadge tone={correlationTone(data.paper_correlation.avg_pair, limit)}>
                  avg {fixed(data.paper_correlation.avg_pair)}
                </ToneBadge>
              </div>
              <PairList pairs={data.paper_correlation.pairs} limit={limit} />
              <p className='text-muted-foreground text-[11px]'>
                {data.paper_correlation.days} days of paper history.
              </p>
            </div>
          </div>
          <div className='grid gap-3 sm:grid-cols-2'>
            <StatTile
              label='Fill-model sensitivity'
              value={signedPercent(strategies?.fill_model_delta_mean)}
              hint='OOS return change under the other fill model'
              tone={Math.abs(strategies?.fill_model_delta_mean ?? 0) >= 0.02 ? 'warn' : 'neutral'}
            />
            <StatTile
              label='Cost-stress pass rate'
              value={formatPercent(strategies?.cost_stress_pass_rate, 0)}
              hint='Eligible coins profitable at 2× costs'
              tone={
                (strategies?.cost_stress_pass_rate ?? 1) >= 0.8
                  ? 'good'
                  : (strategies?.cost_stress_pass_rate ?? 1) < 0.5
                    ? 'bad'
                    : 'warn'
              }
            />
          </div>
        </div>
      </div>

      <div className='space-y-2'>
        <div>
          <p className='text-sm font-medium'>Paper vs backtest</p>
          <p className='text-muted-foreground text-xs'>
            Daily return each open session earns versus what its holdout predicted. A large gap
            means paper and backtest are measuring different things.
          </p>
        </div>
        {data.paper_drift.length ? (
          <div className='overflow-x-auto rounded-lg border'>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Coin</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead className='text-right'>Days</TableHead>
                  <TableHead className='text-right'>Net return</TableHead>
                  <TableHead className='text-right'>Expected / day</TableHead>
                  <TableHead className='text-right'>Actual / day</TableHead>
                  <TableHead className='text-right'>Exposure</TableHead>
                  <TableHead className='text-right'>Failed runs</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {data.paper_drift.map((row) => (
                  <TableRow key={row.symbol}>
                    <TableCell className='font-medium'>{row.symbol}</TableCell>
                    <TableCell className='capitalize'>{row.status}</TableCell>
                    <TableCell className='text-right tabular-nums'>{row.days.toFixed(1)}</TableCell>
                    <TableCell
                      className={cn(
                        'text-right tabular-nums',
                        toneText[row.net_return >= 0 ? 'good' : 'bad']
                      )}
                    >
                      {signedPercent(row.net_return)}
                    </TableCell>
                    <TableCell className='text-right tabular-nums'>
                      {signedPercent(row.expected_daily, 3)}
                    </TableCell>
                    <TableCell className='text-right tabular-nums'>
                      {signedPercent(row.actual_daily, 3)}
                    </TableCell>
                    <TableCell className='text-right tabular-nums'>
                      {formatPercent(row.exposure, 0)}
                    </TableCell>
                    <TableCell
                      className={cn(
                        'text-right tabular-nums',
                        row.failed_runs ? toneText.warn : undefined
                      )}
                    >
                      {row.failed_runs}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        ) : (
          <p className='text-muted-foreground rounded-lg border border-dashed p-3 text-xs'>
            No open paper sessions.
          </p>
        )}
        {data.analysis?.paper_retired?.length ||
        data.analysis?.paper_skipped_correlation?.length ? (
          <p className='text-muted-foreground text-[11px]'>
            {data.analysis.paper_retired.length
              ? `Retired last run: ${data.analysis.paper_retired.join(', ')}. `
              : ''}
            {data.analysis.paper_skipped_correlation.length
              ? `Skipped as correlated: ${data.analysis.paper_skipped_correlation
                  .map(
                    (row) =>
                      `${row.symbol} (${row.correlation.toFixed(2)} with ${row.correlated_with})`
                  )
                  .join(', ')}.`
              : ''}
          </p>
        ) : null}
      </div>
    </CardContent>
  );
}

export default function StrategyValidationCard() {
  const query = useStrategyValidation();

  return (
    <Card>
      <CardHeader>
        <CardTitle className='flex items-center gap-2 text-base'>
          <Icons.flask className='text-muted-foreground size-4' />
          Strategy validation
        </CardTitle>
        <CardDescription>
          Strengths and weaknesses measured on data the ranking never saw: score predictiveness,
          strategy correlation, market beta, and paper-vs-backtest drift.
        </CardDescription>
      </CardHeader>
      {query.isError ? (
        <CardContent>
          <ToneNote
            tone='bad'
            title='Validation is unavailable'
            icon={<Icons.warning className='size-4' />}
          >
            {(query.error as Error).message}
          </ToneNote>
        </CardContent>
      ) : query.data ? (
        <ValidationBody data={query.data} />
      ) : (
        <CardContent>
          <p className='text-muted-foreground text-sm'>Loading validation…</p>
        </CardContent>
      )}
    </Card>
  );
}
