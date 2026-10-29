'use client';

import { Icons } from '@/components/icons';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { FieldGroup } from '@/components/ui/field';
import { formatPercent, formatUsd, relativeTime } from '@/features/grid/api';
import { useSetStrategySettings, useStrategySettings } from '@/features/grid/queries';
import type { StrategySettings } from '@/features/grid/types';
import { useAppForm } from '@/lib/form';
import * as z from 'zod';
import { StatTile, ToneBadge, ToneNote } from './visuals';

// Only a missing value gets the generic message; range checks keep their own.
const requiredNumber = (issue: { input?: unknown }) =>
  issue.input === undefined ? 'Enter a number.' : undefined;

const settingsSchema = z.object({
  signal_interval: z.enum(['1h', '4h']),
  fill_model: z.enum(['trade_through', 'touch']),
  trade_through_bps: z
    .number({ error: requiredNumber })
    .min(0, 'Must be at least 0.')
    .max(50, 'Must be at most 50.'),
  overbought_guard_enabled: z.boolean(),
  holdout_days: z
    .number({ error: 'Enter a number.' })
    .int('Whole days only.')
    .refine((value) => value === 0 || (value >= 14 && value <= 180), {
      message: 'Use 0 (off) or 14–180 days.'
    }),
  book_capital: z
    .number({ error: requiredNumber })
    .min(100, 'Must be at least 100.')
    .max(10_000_000, 'Must be at most 10,000,000.'),
  max_sessions: z
    .number({ error: requiredNumber })
    .int('Whole numbers only.')
    .min(1, 'Must be at least 1.')
    .max(50, 'Must be at most 50.'),
  max_book_exposure: z
    .number({ error: requiredNumber })
    .min(0, 'Must be at least 0.')
    .max(5, 'Must be at most 5.'),
  // Edited as a percentage, stored as a fraction.
  daily_loss_limit_pct: z
    .number({ error: requiredNumber })
    .min(0, 'Must be at least 0.')
    .max(50, 'Must be at most 50.'),
  retire_after_failed_runs: z
    .number({ error: 'Enter a number.' })
    .int('Whole numbers only.')
    .min(1, 'Must be at least 1.')
    .max(30, 'Must be at most 30.'),
  max_strategy_correlation: z
    .number({ error: requiredNumber })
    .min(0.1, 'Must be at least 0.1.')
    .max(1, 'Must be at most 1.'),
  rerun_analysis: z.boolean()
});

type SettingsDraft = z.infer<typeof settingsSchema>;

function toDraft(settings: StrategySettings): SettingsDraft {
  return {
    signal_interval: settings.signal_interval,
    fill_model: settings.fill_model,
    trade_through_bps: settings.trade_through_bps,
    overbought_guard_enabled: settings.overbought_guard_enabled,
    holdout_days: settings.holdout_days,
    book_capital: settings.book_capital,
    max_sessions: settings.max_sessions,
    max_book_exposure: settings.max_book_exposure,
    daily_loss_limit_pct: Number((settings.daily_loss_limit_pct * 100).toFixed(4)),
    retire_after_failed_runs: settings.retire_after_failed_runs,
    max_strategy_correlation: settings.max_strategy_correlation,
    rerun_analysis: true
  };
}

function SettingsForm({
  settings,
  mutation
}: {
  settings: StrategySettings;
  // Owned by the card: a save changes updated_at, which remounts this form.
  mutation: ReturnType<typeof useSetStrategySettings>;
}) {
  const form = useAppForm({
    defaultValues: toDraft(settings),
    validators: { onSubmit: settingsSchema },
    onSubmit: async ({ value }) => {
      const { rerun_analysis, daily_loss_limit_pct, ...rest } = value;
      await mutation.mutateAsync({
        ...rest,
        daily_loss_limit_pct: daily_loss_limit_pct / 100,
        rerun_analysis: rerun_analysis && value.signal_interval !== settings.signal_interval
      });
    }
  });
  const book = settings.book_state ?? {};
  const exposureCap = settings.max_book_exposure;

  return (
    <CardContent>
      <form
        className='space-y-5'
        onSubmit={(event) => {
          event.preventDefault();
          form.handleSubmit();
        }}
      >
        <div className='grid gap-3 sm:grid-cols-2 xl:grid-cols-4'>
          <StatTile
            label='Book exposure'
            value={formatPercent(book.exposure ?? 0, 1)}
            hint={`Cap ${formatPercent(exposureCap, 0)} of ${formatUsd(settings.book_capital)}`}
            tone={(book.exposure ?? 0) >= exposureCap && exposureCap > 0 ? 'bad' : 'info'}
          />
          <StatTile
            label='P&L today'
            value={formatUsd(book.pnl_today ?? 0, 2)}
            hint={`Loss limit ${formatPercent(settings.daily_loss_limit_pct, 1)}`}
            tone={(book.pnl_today ?? 0) < 0 ? 'warn' : 'good'}
          />
          <StatTile
            label='Open sessions'
            value={`${book.sessions ?? 0} / ${settings.max_sessions}`}
            hint={`${formatUsd(settings.session_capital)} each`}
            tone='neutral'
          />
          <StatTile
            label='New buys'
            value={book.halted ? 'Halted' : 'Allowed'}
            hint={
              book.halted
                ? `Daily loss limit hit ${relativeTime(book.halted_at)}`
                : 'No book-level block'
            }
            tone={book.halted ? 'bad' : 'good'}
          />
        </div>

        <div className='grid gap-6 lg:grid-cols-2'>
          <FieldGroup>
            <p className='text-muted-foreground text-[11px] font-medium tracking-wide uppercase'>
              Execution and validation
            </p>
            <form.AppField
              name='signal_interval'
              children={(field) => (
                <field.RadioGroupField
                  label='Signal candles'
                  description='Drives both the daily backtest and paper trading so they measure the same strategy. 1h trades more often; 4h is about 6× cheaper to backtest.'
                  options={[
                    { value: '1h', label: '1 hour' },
                    { value: '4h', label: '4 hours' }
                  ]}
                />
              )}
            />
            <form.Subscribe selector={(state) => state.values.signal_interval}>
              {(interval) =>
                interval !== settings.signal_interval ? (
                  <form.AppField
                    name='rerun_analysis'
                    children={(field) => (
                      <field.CheckboxField
                        label={`Queue a fresh ${interval} analysis after saving`}
                        description='Paper sessions use the latest analysis parameters, which were fitted on the old interval.'
                      />
                    )}
                  />
                ) : null
              }
            </form.Subscribe>
            <form.AppField
              name='fill_model'
              children={(field) => (
                <field.RadioGroupField
                  label='Fill model'
                  description='Trade-through assumes resting orders lose queue priority; touch is optimistic. Every coin reports both.'
                  options={[
                    { value: 'trade_through', label: 'Trade-through' },
                    { value: 'touch', label: 'Touch' }
                  ]}
                />
              )}
            />
            <div className='grid gap-4 sm:grid-cols-2'>
              <form.AppField
                name='trade_through_bps'
                children={(field) => (
                  <field.TextField
                    label='Trade-through (bps)'
                    description='How far price must pass a level to fill.'
                    type='number'
                    step={0.5}
                  />
                )}
              />
              <form.AppField
                name='holdout_days'
                children={(field) => (
                  <field.TextField
                    label='Final holdout (days)'
                    description='Kept out of parameter search and ranking. 0 = off.'
                    type='number'
                    step={1}
                  />
                )}
              />
            </div>
            <form.AppField
              name='overbought_guard_enabled'
              children={(field) => (
                <field.SwitchField
                  label='Overbought guard'
                  description='RSI/MACD/VWAP/open-interest exit on overbought tops in paper trading. On by default; not simulated by the backtest.'
                />
              )}
            />
          </FieldGroup>

          <FieldGroup>
            <p className='text-muted-foreground text-[11px] font-medium tracking-wide uppercase'>
              Portfolio limits
            </p>
            <div className='grid gap-4 sm:grid-cols-2'>
              <form.AppField
                name='book_capital'
                children={(field) => (
                  <field.TextField
                    label='Book capital (USDT)'
                    description='Shared by every paper session.'
                    type='number'
                    step={100}
                  />
                )}
              />
              <form.AppField
                name='max_sessions'
                children={(field) => (
                  <field.TextField
                    label='Max open sessions'
                    description='Each gets book capital ÷ this.'
                    type='number'
                    step={1}
                  />
                )}
              />
              <form.AppField
                name='max_book_exposure'
                children={(field) => (
                  <field.TextField
                    label='Exposure cap (× capital)'
                    description='New buys stop above this open notional.'
                    type='number'
                    step={0.05}
                  />
                )}
              />
              <form.AppField
                name='daily_loss_limit_pct'
                children={(field) => (
                  <field.TextField
                    label='Daily loss limit (%)'
                    description='Halts new buys until the next UTC day. 0 = off.'
                    type='number'
                    step={0.5}
                  />
                )}
              />
              <form.AppField
                name='retire_after_failed_runs'
                children={(field) => (
                  <field.TextField
                    label='Retire after failed runs'
                    description='Consecutive analyses a coin may fail to qualify.'
                    type='number'
                    step={1}
                  />
                )}
              />
              <form.AppField
                name='max_strategy_correlation'
                children={(field) => (
                  <field.TextField
                    label='Max strategy correlation'
                    description='Skip a coin whose grid returns track one already held.'
                    type='number'
                    step={0.05}
                  />
                )}
              />
            </div>
            <form.Subscribe
              selector={(state) => [state.values.book_capital, state.values.max_sessions] as const}
            >
              {([capital, sessions]) => (
                <p className='text-muted-foreground text-[11px] leading-relaxed'>
                  {capital > 0 && sessions > 0
                    ? `New sessions open with ${formatUsd(capital / sessions, 2)} each. `
                    : ''}
                  Limits only block new buys; stops and exits always stay active. Existing sessions
                  keep their capital until they close.
                </p>
              )}
            </form.Subscribe>
          </FieldGroup>
        </div>

        {mutation.isError ? (
          <ToneNote
            tone='bad'
            title='Settings were not saved'
            icon={<Icons.warning className='size-4' />}
          >
            {(mutation.error as Error).message}
          </ToneNote>
        ) : null}
        {mutation.isSuccess && mutation.data?.job_conflict ? (
          <ToneNote
            tone='warn'
            title='Settings saved, analysis not queued'
            icon={<Icons.warning className='size-4' />}
          >
            {mutation.data.job_conflict}
          </ToneNote>
        ) : mutation.isSuccess ? (
          <ToneNote tone='good' title='Settings saved' icon={<Icons.check className='size-4' />}>
            {mutation.data?.job
              ? `Analysis queued on ${mutation.data.signal_interval} candles; paper sessions switch on their next tick.`
              : 'Paper sessions pick up the new settings on their next tick.'}
          </ToneNote>
        ) : null}

        <div className='flex flex-wrap items-center justify-between gap-3'>
          <p className='text-muted-foreground text-[11px]'>
            Last changed {relativeTime(settings.updated_at)}. Saving requires the engine API token.
          </p>
          <div className='flex gap-2'>
            <Button type='button' variant='outline' size='sm' onClick={() => form.reset()}>
              Reset
            </Button>
            <form.AppForm>
              <form.SubmitButton size='sm'>Save settings</form.SubmitButton>
            </form.AppForm>
          </div>
        </div>
      </form>
    </CardContent>
  );
}

export default function StrategySettingsCard() {
  const query = useStrategySettings();
  const mutation = useSetStrategySettings();
  const settings = query.data;

  return (
    <Card>
      <CardHeader>
        <div className='flex flex-wrap items-start justify-between gap-3'>
          <div className='min-w-0'>
            <CardTitle className='flex items-center gap-2 text-base'>
              <Icons.settings className='text-muted-foreground size-4' />
              Strategy & portfolio settings
            </CardTitle>
            <CardDescription>
              One configuration for the backtest and paper trading, plus the limits that bound the
              whole paper book.
            </CardDescription>
          </div>
          {settings ? (
            <div className='flex flex-wrap gap-1.5'>
              <Badge variant='outline'>{settings.signal_interval} candles</Badge>
              <ToneBadge tone={settings.fill_model === 'touch' ? 'warn' : 'info'}>
                {settings.fill_model === 'touch'
                  ? 'Touch fills'
                  : `Trade-through ${settings.trade_through_bps} bps`}
              </ToneBadge>
              {settings.analysis_stale ? (
                <ToneBadge tone='warn'>
                  Latest analysis used {settings.latest_analysis?.interval}
                </ToneBadge>
              ) : null}
            </div>
          ) : null}
        </div>
      </CardHeader>
      {query.isError ? (
        <CardContent>
          <ToneNote
            tone='bad'
            title='Settings are unavailable'
            icon={<Icons.warning className='size-4' />}
          >
            {(query.error as Error).message}
          </ToneNote>
        </CardContent>
      ) : settings ? (
        <SettingsForm key={settings.updated_at} settings={settings} mutation={mutation} />
      ) : (
        <CardContent>
          <p className='text-muted-foreground text-sm'>Loading settings…</p>
        </CardContent>
      )}
    </Card>
  );
}
