'use client';

import PageContainer from '@/components/layout/page-container';
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Checkbox } from '@/components/ui/checkbox';
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
import { Textarea } from '@/components/ui/textarea';
import { Icons } from '@/components/icons';
import { useMemo, useState } from 'react';
import { toast } from 'sonner';
import { formatDate, formatPercent, formatUsd, relativeTime } from '@/features/grid/api';
import EmptyPanel from '@/features/grid/components/empty-panel';
import MetricCard from '@/features/grid/components/metric-card';
import StatusBadge from '@/features/grid/components/status-badge';
import {
  useApproveLive,
  useKillSwitch,
  useLive,
  usePaperSessions,
  useRequestApproval,
  useRevokeLive
} from '@/features/grid/queries';

function GateRow({ label, passed, detail }: { label: string; passed: boolean; detail: string }) {
  return (
    <div className='flex items-start justify-between gap-3 border-b py-2 last:border-0'>
      <div className='flex items-start gap-2'>
        {passed ? (
          <Icons.circleCheck className='mt-0.5 text-emerald-600 dark:text-emerald-400' />
        ) : (
          <Icons.close className='text-muted-foreground mt-0.5' />
        )}
        <div>
          <div className='text-sm font-medium'>{label}</div>
          <div className='text-muted-foreground text-xs'>{detail}</div>
        </div>
      </div>
      <Badge variant='outline' className={passed ? 'text-emerald-600' : 'text-muted-foreground'}>
        {passed ? 'Pass' : 'Blocked'}
      </Badge>
    </div>
  );
}

export default function LiveSafetyPage() {
  const live = useLive();
  const sessions = usePaperSessions();
  const requestApproval = useRequestApproval();
  const approveLive = useApproveLive();
  const revokeLive = useRevokeLive();
  const killSwitch = useKillSwitch();

  const [sessionId, setSessionId] = useState('');
  const [notes, setNotes] = useState('');
  const [acknowledged, setAcknowledged] = useState(false);
  const [killReason, setKillReason] = useState('');

  const promotionReady = useMemo(
    () => (sessions.data?.results ?? []).filter((session) => session.promotion_ready),
    [sessions.data]
  );

  const busy =
    requestApproval.isPending ||
    approveLive.isPending ||
    revokeLive.isPending ||
    killSwitch.isPending;

  const status = live.data?.status;
  const state = live.data?.state;
  const approvals = live.data?.approvals ?? [];
  const audits = live.data?.audits ?? [];
  const activeApproval = status?.approval ?? null;

  const submitRequest = () => {
    if (!sessionId) {
      toast.error('Select a promotion-ready paper session.');
      return;
    }
    if (!acknowledged) {
      toast.error('Risk acknowledgement is required.');
      return;
    }
    requestApproval.mutate(
      { paper_session: sessionId, notes, risk_acknowledged: acknowledged },
      {
        onSuccess: () => {
          setNotes('');
          setAcknowledged(false);
          setSessionId('');
          toast.success('Approval request created', {
            description:
              'Live trading remains disabled until approval is granted and the kill switch is released.'
          });
        },
        onError: (error: Error) => toast.error(error.message)
      }
    );
  };

  const approve = (id: number) => {
    approveLive.mutate(id, {
      onSuccess: () =>
        toast.success('Approval recorded', {
          description: 'The environment flag and kill switch still gate live execution.'
        }),
      onError: (error: Error) => toast.error(error.message)
    });
  };

  const revoke = (id: number) => {
    revokeLive.mutate(id, {
      onSuccess: () => toast.warning('Approval revoked; kill switch engaged'),
      onError: (error: Error) => toast.error(error.message)
    });
  };

  const setKill = (engaged: boolean) => {
    if (!engaged && !window.confirm('Release the kill switch? Other live gates still apply.')) {
      return;
    }
    killSwitch.mutate(
      { engaged, reason: killReason },
      {
        onSuccess: () => {
          setKillReason('');
          toast[engaged ? 'warning' : 'success'](
            engaged ? 'Kill switch engaged' : 'Kill switch released'
          );
        },
        onError: (error: Error) => toast.error(error.message)
      }
    );
  };

  if (live.isLoading) {
    return <PageContainer isLoading pageTitle='Live safety' pageDescription='' />;
  }

  if (live.isError || !status || !state) {
    return (
      <PageContainer pageTitle='Live safety' pageDescription=''>
        <EmptyPanel
          title='Could not load live gate status'
          description={live.error ? (live.error as Error).message : 'Unknown error.'}
          action={<Button onClick={() => live.refetch()}>Retry</Button>}
        />
      </PageContainer>
    );
  }

  return (
    <PageContainer
      pageTitle='Live safety'
      pageDescription='Explicit approval, environment gates, notional limits, and a kill switch for Binance USDⓈ-M futures.'
    >
      <div className='space-y-4'>
        <Alert variant={status.live_ready ? 'default' : 'destructive'}>
          <Icons.shield />
          <AlertTitle>
            {status.live_ready ? 'Live gates are open' : 'Live trading is locked'}
          </AlertTitle>
          <AlertDescription>{live.data?.note}</AlertDescription>
        </Alert>

        <div className='grid gap-4 md:grid-cols-2 xl:grid-cols-4'>
          <MetricCard
            title='Live readiness'
            value={
              <span className={status.live_ready ? 'text-emerald-600' : 'text-amber-600'}>
                {status.live_ready ? 'Armed' : 'Locked'}
              </span>
            }
            hint={status.testnet ? 'Binance testnet configuration' : 'Mainnet configuration'}
            icon={<Icons.shield />}
          />
          <MetricCard
            title='Environment flag'
            value={status.env_enabled ? 'Enabled' : 'Disabled'}
            hint='LIVE_TRADING_ENABLED must be explicitly turned on.'
            icon={<Icons.settings />}
          />
          <MetricCard
            title='API keys'
            value={status.keys_present ? 'Present' : 'Missing'}
            hint='Withdrawals must be disabled on the exchange key.'
            icon={<Icons.lock />}
          />
          <MetricCard
            title='Kill switch'
            value={status.kill_switch ? 'Engaged' : 'Released'}
            hint={status.kill_reason || 'No reason recorded'}
            icon={<Icons.warning />}
          />
        </div>

        <div className='grid gap-4 xl:grid-cols-3'>
          <Card>
            <CardHeader>
              <CardTitle className='text-base'>Safety gates</CardTitle>
              <CardDescription>
                All gates must agree before any live order is allowed.
              </CardDescription>
            </CardHeader>
            <CardContent>
              <GateRow
                label='Environment enabled'
                passed={status.env_enabled}
                detail='LIVE_TRADING_ENABLED'
              />
              <GateRow
                label='API keys configured'
                passed={status.keys_present}
                detail='Keys are read from environment only and never stored in the database.'
              />
              <GateRow
                label='Kill switch released'
                passed={!status.kill_switch}
                detail={status.kill_reason || 'Kill switch is released.'}
              />
              <GateRow
                label='Approval active'
                passed={activeApproval?.status === 'approved'}
                detail={
                  activeApproval
                    ? `Approval #${activeApproval.id} · ${activeApproval.status}`
                    : 'No active approval record.'
                }
              />
              <GateRow
                label='Symbol allowlist'
                passed={true}
                detail={
                  status.limits.symbol_allowlist.length
                    ? status.limits.symbol_allowlist.join(', ')
                    : 'Allowlist empty — configure before enabling live.'
                }
              />
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle className='text-base'>Risk limits</CardTitle>
              <CardDescription>
                Enforced by the live adapter guard, not just the UI.
              </CardDescription>
            </CardHeader>
            <CardContent className='space-y-3 text-sm'>
              <div className='flex justify-between'>
                <span className='text-muted-foreground'>Max order notional</span>
                <span className='font-medium tabular-nums'>
                  {formatUsd(status.limits.max_notional_usd)}
                </span>
              </div>
              <div className='flex justify-between'>
                <span className='text-muted-foreground'>Max daily loss</span>
                <span className='font-medium tabular-nums'>
                  {formatUsd(status.limits.max_daily_loss_usd)}
                </span>
              </div>
              <div className='flex justify-between'>
                <span className='text-muted-foreground'>Max leverage</span>
                <span className='font-medium tabular-nums'>
                  {status.limits.max_leverage.toFixed(1)}x
                </span>
              </div>
              <div className='flex justify-between'>
                <span className='text-muted-foreground'>Active approval</span>
                <span className='font-medium tabular-nums'>
                  {activeApproval ? `#${activeApproval.id}` : '—'}
                </span>
              </div>
              <div className='flex justify-between'>
                <span className='text-muted-foreground'>State updated</span>
                <span className='font-medium tabular-nums'>{relativeTime(state.updated_at)}</span>
              </div>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle className='text-base'>Kill switch</CardTitle>
              <CardDescription>Immediately blocks every live order attempt.</CardDescription>
            </CardHeader>
            <CardContent className='space-y-3'>
              <div className='flex items-center justify-between'>
                <span className='text-muted-foreground text-sm'>Current state</span>
                <Badge
                  variant='outline'
                  className={
                    state.kill_switch
                      ? 'border-rose-500/30 bg-rose-500/10 text-rose-600 dark:text-rose-400'
                      : 'border-emerald-500/30 bg-emerald-500/10 text-emerald-600 dark:text-emerald-400'
                  }
                >
                  {state.kill_switch ? 'Engaged' : 'Released'}
                </Badge>
              </div>
              {state.kill_reason ? (
                <p className='text-muted-foreground text-xs'>reason: {state.kill_reason}</p>
              ) : null}
              <Input
                value={killReason}
                onChange={(event) => setKillReason(event.target.value)}
                placeholder='Reason (required when engaging)'
              />
              <div className='flex flex-wrap gap-2'>
                <Button
                  variant='destructive'
                  size='sm'
                  disabled={busy || !killReason.trim()}
                  onClick={() => setKill(true)}
                >
                  <Icons.warning />
                  Engage
                </Button>
                <Button
                  variant='outline'
                  size='sm'
                  disabled={busy || !state.kill_switch}
                  onClick={() => setKill(false)}
                >
                  Release
                </Button>
              </div>
              <p className='text-muted-foreground text-xs'>
                Release requires a fresh acknowledgement and does not override the environment, key,
                or approval gates.
              </p>
            </CardContent>
          </Card>
        </div>

        <div className='grid gap-4 xl:grid-cols-2'>
          <Card>
            <CardHeader>
              <CardTitle className='text-base'>Request promotion review</CardTitle>
              <CardDescription>
                Only paper sessions that pass every profitability and risk threshold are eligible.
              </CardDescription>
            </CardHeader>
            <CardContent className='space-y-3'>
              {promotionReady.length === 0 ? (
                <p className='text-muted-foreground text-sm'>
                  No promotion-ready paper sessions. Let paper trading accumulate history and pass
                  the configured thresholds first.
                </p>
              ) : (
                <>
                  <div className='space-y-1'>
                    <Label htmlFor='live-session'>Paper session</Label>
                    <NativeSelect
                      id='live-session'
                      value={sessionId}
                      onChange={(event) => setSessionId(event.target.value)}
                    >
                      <NativeSelectOption value=''>Select a session</NativeSelectOption>
                      {promotionReady.map((session) => (
                        <NativeSelectOption key={session.id} value={session.id}>
                          {session.coin.symbol} · {formatPercent(session.net_return)} ·{' '}
                          {session.fill_count} fills
                        </NativeSelectOption>
                      ))}
                    </NativeSelect>
                  </div>
                  <div className='space-y-1'>
                    <Label htmlFor='live-notes'>Notes</Label>
                    <Textarea
                      id='live-notes'
                      value={notes}
                      onChange={(event) => setNotes(event.target.value)}
                      placeholder='Review context, limits, and rollback plan'
                      rows={3}
                    />
                  </div>
                  <div className='flex items-start gap-2'>
                    <Checkbox
                      id='live-ack'
                      checked={acknowledged}
                      onCheckedChange={(checked) => setAcknowledged(checked === true)}
                    />
                    <Label
                      htmlFor='live-ack'
                      className='text-muted-foreground text-xs leading-snug'
                    >
                      I understand that live futures trading can lose money, that backtests and
                      paper results do not guarantee live profitability, and that API keys must have
                      withdrawals disabled.
                    </Label>
                  </div>
                  <Button size='sm' disabled={busy} onClick={submitRequest}>
                    <Icons.shield />
                    Request approval
                  </Button>
                </>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle className='text-base'>Approval history</CardTitle>
              <CardDescription>
                Approval records are required in addition to environment gates.
              </CardDescription>
            </CardHeader>
            <CardContent className='px-0'>
              {approvals.length === 0 ? (
                <p className='text-muted-foreground px-6 pb-6 text-sm'>No approval records yet.</p>
              ) : (
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>#</TableHead>
                      <TableHead>Symbol</TableHead>
                      <TableHead>Status</TableHead>
                      <TableHead>Requested</TableHead>
                      <TableHead>Reviewed</TableHead>
                      <TableHead />
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {approvals.map((approval) => (
                      <TableRow key={approval.id}>
                        <TableCell className='text-muted-foreground tabular-nums'>
                          {approval.id}
                        </TableCell>
                        <TableCell className='font-medium'>
                          {approval.paper_symbol ?? '—'}
                        </TableCell>
                        <TableCell>
                          <StatusBadge value={approval.status} />
                        </TableCell>
                        <TableCell className='text-xs'>
                          <div>{relativeTime(approval.requested_at)}</div>
                          <div className='text-muted-foreground'>{approval.requested_by}</div>
                        </TableCell>
                        <TableCell className='text-xs'>
                          {approval.reviewed_at ? (
                            <>
                              <div>{relativeTime(approval.reviewed_at)}</div>
                              <div className='text-muted-foreground'>
                                {approval.reviewed_by || '—'}
                              </div>
                            </>
                          ) : (
                            '—'
                          )}
                        </TableCell>
                        <TableCell className='text-right'>
                          {approval.status === 'pending' ? (
                            <Button size='sm' disabled={busy} onClick={() => approve(approval.id)}>
                              Approve
                            </Button>
                          ) : approval.status === 'approved' ? (
                            <Button
                              variant='outline'
                              size='sm'
                              disabled={busy}
                              onClick={() => revoke(approval.id)}
                            >
                              Revoke
                            </Button>
                          ) : null}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              )}
            </CardContent>
          </Card>
        </div>

        <Card>
          <CardHeader>
            <CardTitle className='text-base'>Order audit trail</CardTitle>
            <CardDescription>
              Live guard decisions. Denied attempts are recorded even though no live order endpoint
              is exposed.
            </CardDescription>
          </CardHeader>
          <CardContent className='px-0'>
            {audits.length === 0 ? (
              <p className='text-muted-foreground px-6 pb-6 text-sm'>
                No live order attempts logged.
              </p>
            ) : (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Time</TableHead>
                    <TableHead>Symbol</TableHead>
                    <TableHead>Type</TableHead>
                    <TableHead className='text-right'>Notional</TableHead>
                    <TableHead>Allowed</TableHead>
                    <TableHead>Reason</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {audits.map((audit) => (
                    <TableRow key={audit.id}>
                      <TableCell className='text-xs'>{formatDate(audit.created_at)}</TableCell>
                      <TableCell className='font-medium'>{audit.symbol || '—'}</TableCell>
                      <TableCell className='capitalize'>{audit.order_type}</TableCell>
                      <TableCell className='text-right tabular-nums'>
                        {audit.notional === null ? '—' : formatUsd(audit.notional, 2)}
                      </TableCell>
                      <TableCell>
                        <Badge
                          variant='outline'
                          className={
                            audit.allowed
                              ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-600 dark:text-emerald-400'
                              : 'border-rose-500/30 bg-rose-500/10 text-rose-600 dark:text-rose-400'
                          }
                        >
                          {audit.allowed ? 'allowed' : 'denied'}
                        </Badge>
                      </TableCell>
                      <TableCell className='text-muted-foreground max-w-md text-xs'>
                        {audit.reason || '—'}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
          </CardContent>
        </Card>

        <p className='text-muted-foreground text-xs'>
          Paper and live strategies share the same logic through separate execution adapters. The
          live adapter is never called by scheduled jobs, and this dashboard does not expose an
          order-entry endpoint.
        </p>
      </div>
    </PageContainer>
  );
}
