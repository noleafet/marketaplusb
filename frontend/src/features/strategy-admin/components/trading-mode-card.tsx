'use client';

import { Icons } from '@/components/icons';
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogMedia,
  AlertDialogTitle
} from '@/components/ui/alert-dialog';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Switch } from '@/components/ui/switch';
import { relativeTime } from '@/features/grid/api';
import { useSetTradingMode, useTradingMode } from '@/features/grid/queries';
import { cn } from '@/lib/utils';
import { useState } from 'react';
import { toneBadge, ToneBadge, ToneNote, toneSurface, toneText } from './visuals';

export default function TradingModeCard() {
  const modeQuery = useTradingMode();
  const mutation = useSetTradingMode();
  const [confirmOpen, setConfirmOpen] = useState(false);

  const mode = modeQuery.data?.mode ?? 'normal';
  const hybrid = mode === 'hybrid';
  const adxEnabled = Boolean(modeQuery.data?.adx_enabled);
  const busy = mutation.isPending || modeQuery.isLoading;
  const purge = mutation.data?.purge;
  const analysisStale = Boolean(modeQuery.data?.analysis_stale);
  const otherMode = hybrid ? 'normal' : 'hybrid';
  const staleText = modeQuery.data?.latest_analysis
    ? `The ${mode} ranking is older than the ${otherMode} ranking`
    : `No ${mode} analysis yet`;

  const handleModeToggle = (checked: boolean) => {
    if (checked) {
      setConfirmOpen(true);
      return;
    }
    mutation.mutate({ mode: 'normal', adx_enabled: false });
  };

  const confirmHybrid = () => {
    setConfirmOpen(false);
    mutation.mutate({ mode: 'hybrid', confirm: true });
  };

  return (
    <Card className={cn('overflow-hidden', hybrid ? toneSurface.info : toneSurface.neutral)}>
      <CardHeader>
        <div className='flex flex-wrap items-start justify-between gap-3'>
          <div className='min-w-0'>
            <CardTitle className='flex items-center gap-2 text-base'>
              <Icons.adjustments className={cn('size-4', toneText[hybrid ? 'info' : 'neutral'])} />
              Trading mode
            </CardTitle>
            <CardDescription>
              Normal runs the unrestricted grid. Hybrid limits new long entries to the trend filters
              enabled here and reads fully closed candles only.
            </CardDescription>
          </div>
          <Badge
            variant='outline'
            className={cn('shrink-0 capitalize', hybrid ? toneBadge.info : undefined)}
          >
            {mode}
          </Badge>
        </div>
      </CardHeader>
      <CardContent className='grid gap-4 lg:grid-cols-2'>
        <div className='space-y-2'>
          <div className='flex items-start justify-between gap-3 rounded-lg border bg-background/60 p-3'>
            <div className='min-w-0'>
              <p className='text-sm font-medium'>Hybrid trend mode</p>
              <p className='text-muted-foreground text-xs leading-relaxed'>
                {hybrid
                  ? 'New grid buys require every enabled filter to pass. Confirmed exits flatten the position and disarm the ladder.'
                  : 'Normal keeps the existing grid behaviour: no trend filter and no confirmation streak.'}
              </p>
            </div>
            <Switch
              checked={hybrid}
              disabled={busy}
              onCheckedChange={handleModeToggle}
              aria-label='Enable hybrid trend mode'
            />
          </div>
          {hybrid ? (
            <div className='flex items-start justify-between gap-3 rounded-lg border bg-background/60 p-3'>
              <div className='min-w-0'>
                <div className='flex flex-wrap items-center gap-2'>
                  <p className='text-sm font-medium'>ADX trend strength</p>
                  <ToneBadge tone={adxEnabled ? 'info' : 'neutral'}>
                    {adxEnabled ? 'Enabled' : 'Disabled'}
                  </ToneBadge>
                </div>
                <p className='text-muted-foreground text-xs leading-relaxed'>
                  ADX(14) ≥ 25 to enter, new entries pause below 20, and +DI must exceed −DI.
                </p>
              </div>
              <Switch
                checked={adxEnabled}
                disabled={busy}
                onCheckedChange={(checked) =>
                  mutation.mutate({ mode: 'hybrid', adx_enabled: checked })
                }
                aria-label='Enable ADX filter'
              />
            </div>
          ) : null}
        </div>

        <div className='space-y-2'>
          {hybrid ? (
            <div className='flex items-start justify-between gap-3 rounded-lg border bg-background/60 p-3'>
              <div className='min-w-0'>
                <div className='flex flex-wrap items-center gap-2'>
                  <p className='text-sm font-medium'>EMA 100 trend filter</p>
                  <ToneBadge tone='good'>Default on</ToneBadge>
                </div>
                <p className='text-muted-foreground text-xs leading-relaxed'>
                  Trade long while the last closed 1h candle is above EMA 100. Two closed 15m
                  candles below it stop the grid and close the position.
                </p>
              </div>
              <Badge variant='outline' className='text-muted-foreground shrink-0'>
                Always on
              </Badge>
            </div>
          ) : (
            <div className='text-muted-foreground flex items-start gap-2 rounded-lg border border-dashed p-3'>
              <Icons.adjustments className='mt-0.5 size-3.5 shrink-0' />
              <p className='text-xs leading-relaxed'>
                Normal trades unrestricted: no EMA 100 requirement, no ADX gate, no 15m confirmation
                streak. Both filters apply only in hybrid mode.
              </p>
            </div>
          )}

          {analysisStale ? (
            <ToneNote
              tone='warn'
              title='Ranking may be out of date'
              icon={<Icons.warning className='size-4' />}
            >
              <p>{staleText}.</p>
              <Button
                size='sm'
                variant='outline'
                className='mt-2'
                disabled={busy}
                onClick={() => mutation.mutate({ mode, rerun_analysis: true })}
              >
                Run analysis now
              </Button>
            </ToneNote>
          ) : null}
          {mutation.data?.job_conflict ? (
            <ToneNote
              tone='warn'
              title='Analysis not started'
              icon={<Icons.warning className='size-4' />}
            >
              {mutation.data.job_conflict}
            </ToneNote>
          ) : mutation.data?.job ? (
            <ToneNote
              tone='good'
              title='Analysis queued'
              icon={<Icons.shield className='size-4' />}
            >
              Analysis queued
            </ToneNote>
          ) : null}
          {mutation.isError ? (
            <ToneNote
              tone='bad'
              title='Mode change failed'
              icon={<Icons.warning className='size-4' />}
            >
              {(mutation.error as Error).message}
            </ToneNote>
          ) : null}
          {mutation.isSuccess && purge ? (
            <ToneNote
              tone={hybrid ? 'warn' : 'good'}
              title={hybrid ? 'Hybrid mode active' : 'Normal mode active'}
              icon={<Icons.shield className='size-4' />}
            >
              {hybrid
                ? `Cleared ${purge.sessions} paper session(s) and ${purge.positions} open position(s). Realised profit and fees are untouched; live orders were not cancelled.`
                : 'Trend filters are off and the ADX preference was reset. Paper sessions rebuild their ladders on the next tick.'}
            </ToneNote>
          ) : null}
          {modeQuery.isError ? (
            <ToneNote
              tone='bad'
              title='Trading mode is unavailable'
              icon={<Icons.warning className='size-4' />}
            >
              {(modeQuery.error as Error).message}
            </ToneNote>
          ) : null}
          <p className='text-muted-foreground text-[11px] leading-relaxed'>
            {modeQuery.data?.updated_at
              ? `Last changed ${relativeTime(modeQuery.data.updated_at)}. `
              : ''}
            {hybrid ? 'Filters use completed candles only; the newest live candle is ignored.' : ''}
          </p>
        </div>
      </CardContent>

      <AlertDialog open={confirmOpen} onOpenChange={setConfirmOpen}>
        <AlertDialogContent size='default' className='sm:max-w-md'>
          <AlertDialogHeader>
            <AlertDialogMedia className='bg-amber-500/10 text-amber-600 dark:text-amber-400'>
              <Icons.shield />
            </AlertDialogMedia>
            <AlertDialogTitle>Switch to hybrid mode?</AlertDialogTitle>
            <AlertDialogDescription>
              Hybrid limits new long entries to the enabled trend filters. Confirming now will:
            </AlertDialogDescription>
          </AlertDialogHeader>
          <ul className='text-muted-foreground list-disc space-y-1 pl-5 text-sm'>
            <li>Delete every open paper grid order and position from the database.</li>
            <li>Keep realised profit, fees, and funding history untouched.</li>
            <li>Stop new live orders; existing live positions are left to fill.</li>
          </ul>
          <p className='text-muted-foreground text-xs leading-relaxed'>
            Disclaimer: trend filters reduce, but do not eliminate, drawdowns. Clearing open paper
            state cannot be undone. This setting applies to every paper and live session.
          </p>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction onClick={confirmHybrid}>Switch to hybrid</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </Card>
  );
}
