'use client';

import PageContainer from '@/components/layout/page-container';
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { NativeSelect, NativeSelectOption } from '@/components/ui/native-select';
import { Switch } from '@/components/ui/switch';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow
} from '@/components/ui/table';
import { Icons } from '@/components/icons';
import { useMemo, useState } from 'react';
import { toast } from 'sonner';
import { formatDate, relativeTime } from '@/features/grid/api';
import EmptyPanel from '@/features/grid/components/empty-panel';
import MetricCard from '@/features/grid/components/metric-card';
import StatusBadge from '@/features/grid/components/status-badge';
import {
  useCancelJob,
  useJobRuns,
  useJobsSummary,
  useRunSchedule,
  useSchedules,
  useToggleSchedule,
  useTriggerJob
} from '@/features/grid/queries';
import type { JobRun } from '@/features/grid/types';

const STATUSES = ['all', 'queued', 'running', 'succeeded', 'failed', 'cancelled'];

function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return '—';
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const minutes = seconds / 60;
  if (minutes < 60) return `${minutes.toFixed(1)}m`;
  return `${(minutes / 60).toFixed(1)}h`;
}

function RUN_LABEL(jobType: string): string {
  return jobType.replace(/_/g, ' ');
}

export default function JobsPage() {
  const summary = useJobsSummary();
  const schedules = useSchedules();
  const [status, setStatus] = useState('all');
  const runs = useJobRuns({ limit: 100, status: status === 'all' ? undefined : status });
  const trigger = useTriggerJob();
  const runSchedule = useRunSchedule();
  const toggleSchedule = useToggleSchedule();
  const cancelJob = useCancelJob();

  const scheduleRows = schedules.data?.results ?? summary.data?.schedules ?? [];
  const jobTypes = summary.data?.job_types ?? [];
  const rows = runs.data?.results ?? [];
  const busy =
    trigger.isPending || runSchedule.isPending || toggleSchedule.isPending || cancelJob.isPending;

  const counts = useMemo(() => {
    const base: Record<string, number> = {
      queued: summary.data?.queued ?? 0,
      running: summary.data?.running ?? 0,
      ...summary.data?.status_counts
    };
    return base;
  }, [summary.data]);

  const runJob = (jobType: string) => {
    trigger.mutate(
      { jobType },
      {
        onSuccess: (job) =>
          toast.success(`${RUN_LABEL(jobType)} queued`, {
            description: `Job ${job.id.slice(0, 8)} is waiting for a worker.`
          }),
        onError: (error: Error) => toast.error(error.message)
      }
    );
  };

  const runNow = (slug: string, name: string) => {
    runSchedule.mutate(slug, {
      onSuccess: () => toast.success(`${name} queued`),
      onError: (error: Error) => toast.error(error.message)
    });
  };

  const toggle = (slug: string) => {
    toggleSchedule.mutate(slug, {
      onSuccess: (schedule) =>
        toast.success(`${schedule.name} ${schedule.enabled ? 'enabled' : 'disabled'}`),
      onError: (error: Error) => toast.error(error.message)
    });
  };

  const cancel = (job: JobRun) => {
    cancelJob.mutate(job.id, {
      onSuccess: () => toast.success(`Job ${job.id.slice(0, 8)} cancelled`),
      onError: (error: Error) => toast.error(error.message)
    });
  };

  return (
    <PageContainer
      pageTitle='Job console'
      pageDescription='Scheduled and manual engine jobs backed by the database queue. No Redis or Celery required.'
      pageHeaderAction={
        <div className='flex flex-wrap items-center gap-2'>
          <Button
            variant='outline'
            size='sm'
            disabled={busy}
            onClick={() => runJob('coins_refresh')}
          >
            <Icons.coins />
            Refresh coins
          </Button>
          <Button size='sm' disabled={busy} onClick={() => runJob('daily_analysis')}>
            <Icons.chart />
            Run analysis
          </Button>
        </div>
      }
    >
      <div className='space-y-4'>
        <div className='grid gap-4 md:grid-cols-2 xl:grid-cols-4'>
          <MetricCard title='Queued' value={counts.queued ?? 0} icon={<Icons.clock />} />
          <MetricCard title='Running' value={counts.running ?? 0} icon={<Icons.activity />} />
          <MetricCard title='Succeeded' value={counts.succeeded ?? 0} icon={<Icons.checks />} />
          <MetricCard title='Failed' value={counts.failed ?? 0} icon={<Icons.alertCircle />} />
        </div>

        <Alert>
          <Icons.info />
          <AlertTitle>Worker and scheduler processes</AlertTitle>
          <AlertDescription>
            Queued jobs execute only while <code>python manage.py runworker</code> is running.
            Calendar schedules are enqueued by <code>python manage.py runscheduler</code>. Both are
            included in the Podman compose stack.
          </AlertDescription>
        </Alert>

        <Card>
          <CardHeader>
            <CardTitle className='text-base'>Schedules</CardTitle>
            <CardDescription>
              Monthly coin refresh, daily analysis, and paper trading cadence.
            </CardDescription>
          </CardHeader>
          <CardContent className='grid gap-3 md:grid-cols-2'>
            {scheduleRows.length === 0 ? (
              <p className='text-muted-foreground text-sm'>No schedules configured.</p>
            ) : (
              scheduleRows.map((schedule) => (
                <div key={schedule.slug} className='rounded-lg border p-4'>
                  <div className='flex items-start justify-between gap-3'>
                    <div>
                      <div className='flex flex-wrap items-center gap-2'>
                        <span className='font-medium'>{schedule.name}</span>
                        <Badge variant='outline' className='capitalize'>
                          {schedule.kind}
                        </Badge>
                        {!schedule.enabled ? <Badge variant='outline'>Disabled</Badge> : null}
                      </div>
                      <p className='text-muted-foreground mt-1 text-xs'>{schedule.description}</p>
                    </div>
                    <Switch
                      checked={schedule.enabled}
                      disabled={busy}
                      onCheckedChange={() => toggle(schedule.slug)}
                      aria-label={`Toggle ${schedule.name}`}
                    />
                  </div>
                  <div className='text-muted-foreground mt-3 grid grid-cols-2 gap-2 text-xs'>
                    <div>
                      <div>Next run</div>
                      <div className='text-foreground font-medium'>
                        {schedule.enabled ? relativeTime(schedule.next_run_at) : 'paused'}
                      </div>
                      <div>{formatDate(schedule.next_run_at)}</div>
                    </div>
                    <div>
                      <div>Last enqueued</div>
                      <div className='text-foreground font-medium'>
                        {relativeTime(schedule.last_enqueued_at)}
                      </div>
                      <div className='capitalize'>{RUN_LABEL(schedule.job_type)}</div>
                    </div>
                  </div>
                  <div className='mt-3 flex items-center gap-2'>
                    <Button
                      variant='outline'
                      size='sm'
                      disabled={busy}
                      onClick={() => runNow(schedule.slug, schedule.name)}
                    >
                      <Icons.activity />
                      Run now
                    </Button>
                    {schedule.last_job_id ? (
                      <span className='text-muted-foreground text-xs'>
                        last job {schedule.last_job_id.slice(0, 8)}
                      </span>
                    ) : null}
                  </div>
                </div>
              ))
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className='text-base'>Quick triggers</CardTitle>
            <CardDescription>Enqueue any supported engine job immediately.</CardDescription>
          </CardHeader>
          <CardContent className='flex flex-wrap gap-2'>
            {jobTypes.map((item) => (
              <Button
                key={item.job_type}
                variant='outline'
                size='sm'
                disabled={busy}
                onClick={() => runJob(item.job_type)}
                title={item.description}
              >
                <Icons.add />
                {RUN_LABEL(item.job_type)}
              </Button>
            ))}
          </CardContent>
        </Card>

        <Card>
          <CardHeader className='flex flex-row items-start justify-between gap-3'>
            <div>
              <CardTitle className='text-base'>Run history</CardTitle>
              <CardDescription>Polling every five seconds while the page is open.</CardDescription>
            </div>
            <NativeSelect
              value={status}
              onChange={(event) => setStatus(event.target.value)}
              aria-label='Status filter'
            >
              {STATUSES.map((value) => (
                <NativeSelectOption key={value} value={value}>
                  {value === 'all' ? 'All statuses' : value}
                </NativeSelectOption>
              ))}
            </NativeSelect>
          </CardHeader>
          <CardContent className='px-0'>
            {runs.isLoading ? (
              <p className='text-muted-foreground px-6 pb-6 text-sm'>Loading runs…</p>
            ) : runs.isError ? (
              <div className='px-6 pb-6'>
                <EmptyPanel
                  title='Could not load job runs'
                  description={(runs.error as Error).message}
                  action={<Button onClick={() => runs.refetch()}>Retry</Button>}
                />
              </div>
            ) : rows.length === 0 ? (
              <p className='text-muted-foreground px-6 pb-6 text-sm'>
                No job runs match this filter.
              </p>
            ) : (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Job</TableHead>
                    <TableHead>Status</TableHead>
                    <TableHead>Trigger</TableHead>
                    <TableHead>Schedule</TableHead>
                    <TableHead>Enqueued</TableHead>
                    <TableHead className='text-right'>Duration</TableHead>
                    <TableHead>Outcome</TableHead>
                    <TableHead />
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {rows.map((job) => (
                    <TableRow key={job.id}>
                      <TableCell>
                        <div className='font-medium capitalize'>{RUN_LABEL(job.job_type)}</div>
                        <div className='text-muted-foreground font-mono text-xs'>
                          {job.id.slice(0, 8)}
                        </div>
                      </TableCell>
                      <TableCell>
                        <StatusBadge value={job.status} />
                      </TableCell>
                      <TableCell className='capitalize'>{job.trigger}</TableCell>
                      <TableCell className='text-muted-foreground text-xs'>
                        {job.schedule_slug ?? '—'}
                      </TableCell>
                      <TableCell className='text-xs'>
                        <div>{relativeTime(job.enqueued_at)}</div>
                        <div className='text-muted-foreground'>{formatDate(job.enqueued_at)}</div>
                      </TableCell>
                      <TableCell className='text-right tabular-nums'>
                        {formatDuration(job.duration_seconds)}
                      </TableCell>
                      <TableCell className='max-w-xs'>
                        {job.error ? (
                          <span className='text-rose-600 dark:text-rose-400 line-clamp-2 text-xs'>
                            {job.error}
                          </span>
                        ) : job.status === 'succeeded' ? (
                          <details className='text-xs'>
                            <summary className='text-muted-foreground cursor-pointer'>
                              result
                            </summary>
                            <pre className='bg-muted mt-1 max-h-40 overflow-auto rounded p-2 text-[10px]'>
                              {JSON.stringify(job.result, null, 2)}
                            </pre>
                          </details>
                        ) : (
                          <span className='text-muted-foreground text-xs'>—</span>
                        )}
                      </TableCell>
                      <TableCell className='text-right'>
                        {job.status === 'queued' ? (
                          <Button
                            variant='outline'
                            size='sm'
                            disabled={busy}
                            onClick={() => cancel(job)}
                          >
                            Cancel
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
    </PageContainer>
  );
}
