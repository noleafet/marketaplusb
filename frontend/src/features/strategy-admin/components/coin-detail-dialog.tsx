'use client';

import { Icons } from '@/components/icons';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle
} from '@/components/ui/dialog';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow
} from '@/components/ui/table';
import { formatPercent } from '@/features/grid/api';
import { FACTOR_LABELS, formatGateValue } from '@/features/grid/diagnostics';
import { useCoinResultDetail } from '@/features/grid/queries';
import type { CoinResult, Diagnostics, ScoreFactor } from '@/features/grid/types';
import { cn } from '@/lib/utils';
import { PARAM_LABELS } from '../catalog';
import type { CatalogTone } from '../catalog';
import {
  drawdownTone,
  exposureTone,
  numberMetric,
  orderedParams,
  paramValue,
  percentMetric,
  profitFactorTone,
  returnTone,
  scoreTone,
  signedPercent,
  toNumber
} from '../format';
import { Meter, StatTile, ToneBadge, ToneNote, toneSurface, toneText } from './visuals';

const FACTOR_STATUS_TONES: Record<ScoreFactor['status'], CatalogTone> = {
  strong: 'good',
  ok: 'info',
  watch: 'warn',
  weak: 'bad'
};

function GateList({ diagnostics }: { diagnostics: Diagnostics | null | undefined }) {
  const gates = diagnostics?.hard_gates ?? [];
  if (!gates.length) {
    return <p className='text-muted-foreground text-sm'>No gate diagnostics recorded.</p>;
  }
  return (
    <div className='space-y-1.5 text-sm'>
      {gates.map((gate) => (
        <div key={gate.name} className='flex items-start justify-between gap-3'>
          <span className='text-muted-foreground'>
            {gate.label}
            {gate.reason ? (
              <span className='block text-xs text-rose-500'>{gate.reason}</span>
            ) : null}
          </span>
          <span className='flex shrink-0 items-center gap-1.5'>
            <ToneBadge tone={gate.passed ? 'good' : 'bad'}>
              {gate.passed ? 'pass' : 'fail'}
            </ToneBadge>
            <span className='text-xs tabular-nums'>{formatGateValue(gate.value)}</span>
          </span>
        </div>
      ))}
    </div>
  );
}

function DiagnosticNote({
  title,
  tone,
  children
}: {
  title: string;
  tone: CatalogTone;
  children: React.ReactNode;
}) {
  return (
    <div className={cn('rounded-lg border p-2.5', toneSurface[tone])}>
      <p className='text-muted-foreground text-[11px] font-medium tracking-wide uppercase'>
        {title}
      </p>
      <div className='mt-1 space-y-1 text-xs leading-relaxed'>{children}</div>
    </div>
  );
}

export default function CoinDetailDialog({
  result,
  open,
  onOpenChange,
  drawdownLimit = 0.35
}: {
  result: CoinResult | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  drawdownLimit?: number;
}) {
  const full = useCoinResultDetail(open && result ? result.id : null).data;
  const diagnostics = full?.diagnostics ?? result?.diagnostics;
  const oos = full?.oos_metrics ?? result?.oos_metrics;
  const params = full?.params ?? result?.params ?? {};
  const folds = full?.folds ?? result?.folds ?? [];
  const regime = diagnostics?.regime;
  const costStress = diagnostics?.cost_stress;
  const sensitivity = diagnostics?.sensitivity;
  const exposure = diagnostics?.exposure;
  const execution = diagnostics?.execution;
  const stability = diagnostics?.return_stability;
  const correlation = diagnostics?.correlation;
  const breakdown = diagnostics?.score_breakdown;
  const flags = diagnostics?.flags ?? [];

  const oosReturn = toNumber(oos?.net_return);
  const oosDrawdown = toNumber(oos?.max_drawdown);
  const profitFactor = toNumber(oos?.profit_factor);
  const stressReturn = toNumber(costStress?.stress_return);
  const robustness = toNumber(regime?.robustness_score);
  const exposureRatio = toNumber(exposure?.max_exposure_ratio);
  const recoveryDays = toNumber(diagnostics?.recovery_days);
  const scoreDelta = toNumber(diagnostics?.score_delta);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className='max-h-[85vh] overflow-y-auto sm:max-w-3xl'>
        <DialogHeader>
          <DialogTitle>{result?.coin.symbol ?? 'Coin'} strategy configuration</DialogTitle>
          <DialogDescription>
            {result ? (
              <span className='flex flex-wrap items-center gap-2'>
                <span>{result.coin.name}</span>
                <span className='text-muted-foreground text-xs'>rank {result.rank ?? '—'}</span>
                <ToneBadge tone={scoreTone(result.score)}>score {result.score.toFixed(1)}</ToneBadge>
                <ToneBadge tone={result.eligible ? 'good' : 'neutral'}>
                  {result.eligible ? 'eligible' : 'ineligible'}
                </ToneBadge>
              </span>
            ) : null}
          </DialogDescription>
        </DialogHeader>

        {result ? (
          <div className='space-y-5'>
            {scoreDelta !== null ? (
              <ToneNote
                tone={scoreDelta >= 0 ? 'good' : 'bad'}
                title={`Score ${scoreDelta >= 0 ? 'improved' : 'fell'} versus baseline`}
                icon={
                  scoreDelta >= 0 ? (
                    <Icons.trendingUp className='size-4' />
                  ) : (
                    <Icons.trendingDown className='size-4' />
                  )
                }
              >
                Baseline {breakdown?.base_score?.toFixed(1) ?? '—'} → final{' '}
                {breakdown?.final_score?.toFixed(1) ?? result.score.toFixed(1)} ·{' '}
                {signedPercent(scoreDelta / 100, 1)} after penalties.
              </ToneNote>
            ) : null}

            <div>
              <h3 className='mb-2 text-sm font-medium'>Walk-forward result</h3>
              <div className='grid grid-cols-2 gap-2 sm:grid-cols-4'>
                <StatTile
                  label='OOS return'
                  value={percentMetric(oos, 'net_return')}
                  tone={returnTone(oosReturn)}
                />
                <StatTile
                  label='Max drawdown'
                  value={percentMetric(oos, 'max_drawdown')}
                  tone={drawdownTone(oosDrawdown, drawdownLimit)}
                  hint={`limit ${formatPercent(drawdownLimit, 0)}`}
                />
                <StatTile
                  label='Profit factor'
                  value={numberMetric(oos, 'profit_factor')}
                  tone={profitFactorTone(profitFactor)}
                />
                <StatTile label='OOS fills' value={numberMetric(oos, 'trades')} />
                <StatTile
                  label='Cost stress'
                  value={percentMetric(oos, 'cost_stress_return')}
                  tone={costStress?.profitable ? 'good' : 'bad'}
                />
                <StatTile
                  label='Regime score'
                  value={robustness !== null ? robustness.toFixed(1) : '—'}
                  tone={scoreTone(robustness)}
                />
                <StatTile
                  label='Peak exposure'
                  value={`${exposureRatio?.toFixed(2) ?? '—'}×`}
                  tone={exposureTone(exposureRatio, 1)}
                />
                <StatTile
                  label='Recovery'
                  value={recoveryDays !== null ? `${recoveryDays.toFixed(0)}d` : '—'}
                  tone={
                    recoveryDays === null ? 'neutral' : recoveryDays > 30 ? 'warn' : 'good'
                  }
                />
              </div>
            </div>

            <div>
              <h3 className='mb-2 text-sm font-medium'>Recommended parameters</h3>
              <div className='grid grid-cols-2 gap-x-4 gap-y-1.5 sm:grid-cols-3'>
                {orderedParams(params).map(([key, value]) => (
                  <div key={key} className='flex items-baseline justify-between gap-2 text-sm'>
                    <span className='text-muted-foreground'>
                      {PARAM_LABELS[key] ?? key.replace(/_/g, ' ')}
                    </span>
                    <span className='font-medium tabular-nums'>{paramValue(key, value)}</span>
                  </div>
                ))}
              </div>
            </div>

            <div>
              <h3 className='mb-2 text-sm font-medium'>Eligibility gates</h3>
              <GateList diagnostics={diagnostics} />
            </div>

            <div>
              <h3 className='mb-2 text-sm font-medium'>Review flags</h3>
              {flags.length ? (
                <ul className='space-y-1.5 text-sm'>
                  {flags.map((flag) => {
                    const tone: CatalogTone =
                      flag.severity === 'critical'
                        ? 'bad'
                        : flag.severity === 'warning'
                          ? 'warn'
                          : 'info';
                    return (
                      <li
                        key={flag.code}
                        className={cn('flex items-start gap-2 rounded-md border p-2', toneSurface[tone])}
                      >
                        <Icons.warning className={cn('mt-0.5 size-3.5 shrink-0', toneText[tone])} />
                        <span>
                          {flag.message}
                          <span className='text-muted-foreground block text-[11px] uppercase'>
                            {flag.severity}
                          </span>
                        </span>
                      </li>
                    );
                  })}
                </ul>
              ) : (
                <ToneNote tone='good' title='No review flags' icon={<Icons.check className='size-4' />}>
                  No concentration, cost-stress, or regime warning was raised for this coin.
                </ToneNote>
              )}
            </div>

            <div className='grid gap-4 sm:grid-cols-2'>
              <div>
                <h3 className='mb-2 text-sm font-medium'>Score contributions</h3>
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Factor</TableHead>
                      <TableHead>State</TableHead>
                      <TableHead className='text-right'>Points</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {Object.entries(breakdown?.factors ?? {}).map(([key, factor]) => (
                      <TableRow key={key}>
                        <TableCell className='text-sm'>
                          {FACTOR_LABELS[key] ?? key.replace(/_/g, ' ')}
                          <span className='text-muted-foreground block text-[11px]'>
                            weight {(factor.weight * 100).toFixed(0)}% · score{' '}
                            {factor.score.toFixed(1)}
                          </span>
                        </TableCell>
                        <TableCell>
                          <ToneBadge tone={FACTOR_STATUS_TONES[factor.status]}>
                            {factor.status}
                          </ToneBadge>
                          <Meter
                            value={factor.contribution}
                            max={20}
                            tone={FACTOR_STATUS_TONES[factor.status]}
                            className='mt-1 w-16'
                          />
                        </TableCell>
                        <TableCell className='text-right text-sm tabular-nums'>
                          {factor.contribution.toFixed(1)}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
                {breakdown?.penalties?.length ? (
                  <ul className='mt-2 space-y-1 text-xs'>
                    {breakdown.penalties.map((penalty) => (
                      <li key={penalty.code} className='text-rose-600 dark:text-rose-400'>
                        −{penalty.points.toFixed(1)} · {penalty.reason}
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p className='text-muted-foreground mt-2 text-xs'>No score penalties.</p>
                )}
              </div>

              <div>
                <h3 className='mb-2 text-sm font-medium'>Regime robustness</h3>
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Regime</TableHead>
                      <TableHead className='text-right'>Folds</TableHead>
                      <TableHead className='text-right'>Mean ret.</TableHead>
                      <TableHead>Win share</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {Object.entries(regime?.regimes ?? {}).map(([label, bucket]) => (
                      <TableRow key={label}>
                        <TableCell className='text-sm capitalize'>
                          {label.replace(/_/g, ' ')}
                        </TableCell>
                        <TableCell className='text-right text-sm tabular-nums'>
                          {bucket.folds}
                        </TableCell>
                        <TableCell
                          className={cn(
                            'text-right text-sm tabular-nums',
                            toneText[returnTone(bucket.mean_return)]
                          )}
                        >
                          {formatPercent(bucket.mean_return)}
                        </TableCell>
                        <TableCell>
                          <Meter
                            value={bucket.profitable * 100}
                            tone={bucket.profitable >= 0.5 ? 'good' : 'warn'}
                            label={formatPercent(bucket.profitable, 0)}
                          />
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
                <p className='text-muted-foreground mt-2 text-xs'>
                  {regime?.regimes_covered ?? 0} regimes · dominant{' '}
                  {regime?.dominant_regime?.replace(/_/g, ' ') ?? '—'} · dependence{' '}
                  {regime?.dominant_share !== undefined
                    ? formatPercent(regime.dominant_share, 0)
                    : '—'}
                </p>
              </div>
            </div>

            <div className='grid gap-2 sm:grid-cols-2'>
              <DiagnosticNote
                title='Cost stress'
                tone={costStress?.profitable ? 'good' : 'bad'}
              >
                <p>
                  {costStress?.multiplier ?? 2}× costs · stressed net{' '}
                  <span className={toneText[returnTone(stressReturn)]}>
                    {formatPercent(costStress?.stress_return ?? 0)}
                  </span>{' '}
                  · delta{' '}
                  <span className={toneText[returnTone(toNumber(costStress?.stress_delta))]}>
                    {signedPercent(costStress?.stress_delta ?? 0)}
                  </span>
                </p>
                <ToneBadge tone={costStress?.profitable ? 'good' : 'bad'}>
                  {costStress?.profitable ? 'stays profitable' : 'fails under stress'}
                </ToneBadge>
              </DiagnosticNote>

              <DiagnosticNote
                title='Parameter sensitivity'
                tone={
                  (toNumber(sensitivity?.parameter_sensitivity) ?? 0) > 0.5 ? 'warn' : 'good'
                }
              >
                <p>
                  score {sensitivity?.parameter_sensitivity?.toFixed(2) ?? '—'} · mean neighbour{' '}
                  {formatPercent(sensitivity?.mean_neighbor_return ?? 0)} · worst{' '}
                  {formatPercent(sensitivity?.worst_neighbor_return ?? 0)} ·{' '}
                  {sensitivity?.runs?.length ?? 0} variants
                </p>
              </DiagnosticNote>

              <DiagnosticNote
                title='Inventory & recovery'
                tone={exposureTone(exposureRatio, 1)}
              >
                <p>
                  peak{' '}
                  <span className={toneText[exposureTone(exposureRatio, 1)]}>
                    {exposureRatio?.toFixed(2) ?? '—'}×
                  </span>{' '}
                  · avg {exposure?.avg_exposure_ratio?.toFixed(2) ?? '—'}× · unrealized loss{' '}
                  {formatPercent(exposure?.max_unrealized_loss_pct ?? 0)} · drawdown duration{' '}
                  {diagnostics?.max_drawdown_duration_days?.toFixed(0) ?? '—'}d
                </p>
              </DiagnosticNote>

              <DiagnosticNote
                title='Execution quality'
                tone={(toNumber(execution?.cycle_completion_ratio) ?? 0) >= 0.5 ? 'good' : 'warn'}
              >
                <p>
                  slippage ${execution?.slippage_paid?.toFixed(2) ?? '—'} · buy/sell fills{' '}
                  {execution?.buy_fills ?? '—'}/{execution?.sell_fills ?? '—'} · cycles{' '}
                  {formatPercent(execution?.cycle_completion_ratio ?? 0, 0)}
                </p>
              </DiagnosticNote>

              <DiagnosticNote
                title='Return stability'
                tone={(toNumber(stability?.top_trade_share) ?? 0) > 0.6 ? 'warn' : 'good'}
              >
                <p>
                  top trade {formatPercent(stability?.top_trade_share ?? 0, 0)} · top five{' '}
                  {formatPercent(stability?.top5_trade_share ?? 0, 0)} · fold spread{' '}
                  {formatPercent(stability?.fold_return_std ?? 0)}
                </p>
              </DiagnosticNote>

              <DiagnosticNote
                title='Correlation'
                tone={(toNumber(correlation?.max_correlation) ?? 0) >= 0.85 ? 'warn' : 'good'}
              >
                <p>
                  avg {correlation?.avg_correlation?.toFixed(2) ?? '—'} · peak{' '}
                  {correlation?.max_correlation?.toFixed(2) ?? '—'}
                  {correlation?.peers?.length
                    ? ` · ${correlation.peers
                        .map((peer) => `${peer.symbol} ${peer.correlation.toFixed(2)}`)
                        .join(', ')}`
                    : ''}
                </p>
              </DiagnosticNote>
            </div>

            {diagnostics?.eligibility_reasons?.length ? (
              <div>
                <h3 className='mb-1 text-sm font-medium'>Rejection reasons</h3>
                <ul className='space-y-1'>
                  {diagnostics.eligibility_reasons.map((reason) => (
                    <li
                      key={reason}
                      className='rounded-md border border-rose-500/20 bg-rose-500/5 px-2 py-1 text-sm text-rose-600 dark:text-rose-400'
                    >
                      {reason}
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}

            {folds.length ? (
              <div>
                <h3 className='mb-2 text-sm font-medium'>Walk-forward folds</h3>
                <div className='overflow-x-auto'>
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>Fold</TableHead>
                        <TableHead>Test period</TableHead>
                        <TableHead>Regime</TableHead>
                        <TableHead className='text-right'>Return</TableHead>
                        <TableHead className='text-right'>Max DD</TableHead>
                        <TableHead className='text-right'>Stress</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {folds.map((fold, index) => {
                        const regimeLabel = (fold.regime as { label?: string } | undefined)?.label;
                        const test = fold.test_metrics as Record<string, unknown> | undefined;
                        const stress = fold.stress as { net_return?: number } | undefined;
                        const foldReturn = toNumber(test?.net_return);
                        const foldDrawdown = toNumber(test?.max_drawdown);
                        return (
                          <TableRow key={index}>
                            <TableCell className='text-sm tabular-nums'>
                              {String(fold.fold ?? index + 1)}
                            </TableCell>
                            <TableCell className='text-muted-foreground text-xs'>
                              {typeof fold.test_start === 'string'
                                ? fold.test_start.slice(0, 10)
                                : '—'}{' '}
                              →{' '}
                              {typeof fold.test_end === 'string'
                                ? fold.test_end.slice(0, 10)
                                : '—'}
                            </TableCell>
                            <TableCell className='text-sm capitalize'>
                              {regimeLabel?.replace(/_/g, ' ') ?? '—'}
                            </TableCell>
                            <TableCell
                              className={cn(
                                'text-right text-sm tabular-nums',
                                toneText[returnTone(foldReturn)]
                              )}
                            >
                              {percentMetric(test, 'net_return')}
                            </TableCell>
                            <TableCell
                              className={cn(
                                'text-right text-sm tabular-nums',
                                toneText[drawdownTone(foldDrawdown, drawdownLimit)]
                              )}
                            >
                              {percentMetric(test, 'max_drawdown')}
                            </TableCell>
                            <TableCell
                              className={cn(
                                'text-right text-sm tabular-nums',
                                toneText[returnTone(toNumber(stress?.net_return))]
                              )}
                            >
                              {stress?.net_return !== undefined
                                ? formatPercent(stress.net_return)
                                : '—'}
                            </TableCell>
                          </TableRow>
                        );
                      })}
                    </TableBody>
                  </Table>
                </div>
              </div>
            ) : null}
          </div>
        ) : null}
      </DialogContent>
    </Dialog>
  );
}
