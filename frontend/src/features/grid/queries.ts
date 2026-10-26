'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { engineApi } from './api';

export const gridKeys = {
  all: ['grid'] as const,
  dashboard: ['grid', 'dashboard'] as const,
  results: (params: Record<string, unknown> = {}) => ['grid', 'results', params] as const,
  coins: ['grid', 'coins'] as const,
  coinHistory: (symbol: string) => ['grid', 'coin-history', symbol] as const,
  reports: ['grid', 'reports'] as const,
  jobs: ['grid', 'jobs'] as const,
  runs: (params: Record<string, unknown> = {}) => ['grid', 'runs', params] as const,
  schedules: ['grid', 'schedules'] as const,
  paper: ['grid', 'paper'] as const,
  paperDetail: (id: string) => ['grid', 'paper', id] as const,
  paperEquity: (id: string) => ['grid', 'paper', id, 'equity'] as const,
  paperFills: (id: string) => ['grid', 'paper', id, 'fills'] as const,
  paperEvaluations: (id: string) => ['grid', 'paper', id, 'evaluations'] as const,
  live: ['grid', 'live'] as const
};

export function useDashboard() {
  return useQuery({
    queryKey: gridKeys.dashboard,
    queryFn: engineApi.dashboard,
    refetchInterval: 15000
  });
}

export function useResults(params: { limit?: number; search?: string } = {}) {
  return useQuery({
    queryKey: gridKeys.results(params),
    queryFn: () => engineApi.results(params)
  });
}

export function useCoins() {
  return useQuery({
    queryKey: gridKeys.coins,
    queryFn: () => engineApi.coins({ limit: 200 })
  });
}

export function useCoinHistory(symbol: string) {
  return useQuery({
    queryKey: gridKeys.coinHistory(symbol),
    queryFn: () => engineApi.resultsByCoin(symbol),
    enabled: Boolean(symbol)
  });
}

export function useReports() {
  return useQuery({
    queryKey: gridKeys.reports,
    queryFn: () => engineApi.reports()
  });
}

export function useJobsSummary() {
  return useQuery({
    queryKey: gridKeys.jobs,
    queryFn: engineApi.jobsSummary,
    refetchInterval: 10000
  });
}

export function useJobRuns(params: { limit?: number; status?: string } = {}) {
  return useQuery({
    queryKey: gridKeys.runs(params),
    queryFn: () => engineApi.jobRuns(params),
    refetchInterval: 5000
  });
}

export function useSchedules() {
  return useQuery({
    queryKey: gridKeys.schedules,
    queryFn: engineApi.schedules,
    refetchInterval: 15000
  });
}

export function usePaperSessions() {
  return useQuery({
    queryKey: gridKeys.paper,
    queryFn: () => engineApi.paperSessions(),
    refetchInterval: 15000
  });
}

export function usePaperSession(id: string) {
  return useQuery({
    queryKey: gridKeys.paperDetail(id),
    queryFn: () => engineApi.paperSession(id),
    enabled: Boolean(id),
    refetchInterval: 15000
  });
}

export function usePaperEquity(id: string) {
  return useQuery({
    queryKey: gridKeys.paperEquity(id),
    queryFn: () => engineApi.paperEquity(id),
    enabled: Boolean(id),
    refetchInterval: 30000
  });
}

export function usePaperFills(id: string) {
  return useQuery({
    queryKey: gridKeys.paperFills(id),
    queryFn: () => engineApi.paperFills(id),
    enabled: Boolean(id),
    refetchInterval: 15000
  });
}

export function usePaperEvaluations(id: string) {
  return useQuery({
    queryKey: gridKeys.paperEvaluations(id),
    queryFn: () => engineApi.paperEvaluations(id),
    enabled: Boolean(id)
  });
}

export function useLive() {
  return useQuery({
    queryKey: gridKeys.live,
    queryFn: engineApi.live,
    refetchInterval: 15000
  });
}

function useRefresh() {
  const queryClient = useQueryClient();
  return () => queryClient.invalidateQueries({ queryKey: gridKeys.all });
}

export function useTriggerJob() {
  const refresh = useRefresh();
  return useMutation({
    mutationFn: (input: { jobType: string; params?: Record<string, unknown> }) =>
      engineApi.triggerJob(input.jobType, input.params ?? {}),
    onSuccess: refresh
  });
}

export function useRunSchedule() {
  const refresh = useRefresh();
  return useMutation({
    mutationFn: (slug: string) => engineApi.runSchedule(slug),
    onSuccess: refresh
  });
}

export function useToggleSchedule() {
  const refresh = useRefresh();
  return useMutation({
    mutationFn: (slug: string) => engineApi.toggleSchedule(slug),
    onSuccess: refresh
  });
}

export function useSessionAction() {
  const refresh = useRefresh();
  return useMutation({
    mutationFn: (input: {
      id: string;
      action: 'pause' | 'resume' | 'stop' | 'evaluate' | 'tick';
      body?: Record<string, unknown>;
    }) => engineApi.sessionAction(input.id, input.action, input.body),
    onSuccess: refresh
  });
}

export function useTickAll() {
  const refresh = useRefresh();
  return useMutation({
    mutationFn: () => engineApi.tickAll(),
    onSuccess: refresh
  });
}

export function useCancelJob() {
  const refresh = useRefresh();
  return useMutation({
    mutationFn: (id: string) => engineApi.cancelJob(id),
    onSuccess: refresh
  });
}

export function useRequestApproval() {
  const refresh = useRefresh();
  return useMutation({
    mutationFn: engineApi.requestApproval,
    onSuccess: refresh
  });
}

export function useApproveLive() {
  const refresh = useRefresh();
  return useMutation({
    mutationFn: (id: number) => engineApi.approveLive(id, {}),
    onSuccess: refresh
  });
}

export function useRevokeLive() {
  const refresh = useRefresh();
  return useMutation({
    mutationFn: (id: number) => engineApi.revokeLive(id),
    onSuccess: refresh
  });
}

export function useKillSwitch() {
  const refresh = useRefresh();
  return useMutation({
    mutationFn: (input: { engaged: boolean; reason?: string }) =>
      engineApi.killSwitch(input.engaged, input.reason ?? ''),
    onSuccess: refresh
  });
}
