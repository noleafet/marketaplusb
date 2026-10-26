'use client';

import { useId, useMemo } from 'react';
import {
  Area,
  AreaChart,
  CartesianGrid,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis
} from 'recharts';
import { formatDate, formatUsd } from '../api';

type RawPoint = {
  t?: string;
  time?: string;
  equity: number;
  dd?: number | null;
  drawdown?: number | null;
};

export default function EquityChart({
  points,
  capital,
  height = 280
}: {
  points: RawPoint[];
  capital?: number;
  height?: number;
}) {
  const gradientId = useId().replace(/:/g, '');
  const data = useMemo(
    () =>
      points.map((point) => ({
        date: point.t ?? point.time ?? '',
        equity: Number(point.equity),
        drawdown: point.dd ?? point.drawdown ?? null
      })),
    [points]
  );

  if (!data.length) {
    return (
      <div className='text-muted-foreground flex h-40 items-center justify-center text-sm'>
        No equity data yet.
      </div>
    );
  }

  const values = data.map((point) => point.equity);
  const min = Math.min(...values, capital ?? Number.POSITIVE_INFINITY);
  const max = Math.max(...values, capital ?? Number.NEGATIVE_INFINITY);
  const padding = Math.max((max - min) * 0.08, max * 0.005, 1);

  return (
    <div style={{ height }} className='w-full'>
      <ResponsiveContainer width='100%' height='100%'>
        <AreaChart data={data} margin={{ top: 8, right: 12, left: 0, bottom: 0 }}>
          <defs>
            <linearGradient id={gradientId} x1='0' y1='0' x2='0' y2='1'>
              <stop offset='5%' stopColor='var(--primary)' stopOpacity={0.35} />
              <stop offset='95%' stopColor='var(--primary)' stopOpacity={0.02} />
            </linearGradient>
          </defs>
          <CartesianGrid strokeDasharray='3 3' className='stroke-border' vertical={false} />
          <XAxis
            dataKey='date'
            tickLine={false}
            axisLine={false}
            minTickGap={40}
            tick={{ fontSize: 11 }}
            tickFormatter={(value: string) =>
              new Date(value).toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
            }
          />
          <YAxis
            domain={[min - padding, max + padding]}
            tickLine={false}
            axisLine={false}
            width={72}
            tick={{ fontSize: 11 }}
            tickFormatter={(value: number) => formatUsd(value, 0)}
          />
          <Tooltip
            labelFormatter={(value) => formatDate(String(value))}
            formatter={(value) => [formatUsd(Number(value), 2), 'Equity']}
            contentStyle={{
              background: 'var(--popover)',
              border: '1px solid var(--border)',
              borderRadius: 8,
              fontSize: 12,
              color: 'var(--popover-foreground)'
            }}
          />
          {capital ? (
            <ReferenceLine
              y={capital}
              stroke='var(--muted-foreground)'
              strokeDasharray='4 4'
              strokeOpacity={0.6}
            />
          ) : null}
          <Area
            type='monotone'
            dataKey='equity'
            stroke='var(--primary)'
            strokeWidth={2}
            fill={`url(#${gradientId})`}
            dot={false}
            isAnimationActive={false}
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}
