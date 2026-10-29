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
  segment?: string;
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
  const holdoutGradientId = `${gradientId}-holdout`;
  // Holdout points are drawn as their own series. The last walk-forward point
  // also starts the holdout series so the two lines join without a gap.
  const { data, holdoutStart } = useMemo(() => {
    const timed = points.filter(
      (point) => !Number.isNaN(new Date(point.t ?? point.time ?? '').getTime())
    );
    const firstHoldout = timed.findIndex((point) => point.segment === 'holdout');
    const rows = timed.map((point, index) => {
      const value = Number(point.equity);
      const isHoldout = firstHoldout >= 0 && index >= firstHoldout;
      return {
        // Epoch milliseconds: a time axis keeps spacing proportional to time,
        // even though folds and holdout are sampled at different densities.
        date: new Date(point.t ?? point.time ?? '').getTime(),
        equity: isHoldout ? null : value,
        holdout: isHoldout || (firstHoldout > 0 && index === firstHoldout - 1) ? value : null,
        drawdown: point.dd ?? point.drawdown ?? null
      };
    });
    return {
      data: rows,
      holdoutStart: firstHoldout >= 0 ? rows[firstHoldout].date : null
    };
  }, [points]);

  if (!data.length) {
    return (
      <div className='text-muted-foreground flex h-40 items-center justify-center text-sm'>
        No equity data yet.
      </div>
    );
  }

  const values = data.map((point) => (point.equity ?? point.holdout) as number);
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
            <linearGradient id={holdoutGradientId} x1='0' y1='0' x2='0' y2='1'>
              <stop offset='5%' stopColor='var(--chart-2)' stopOpacity={0.3} />
              <stop offset='95%' stopColor='var(--chart-2)' stopOpacity={0.02} />
            </linearGradient>
          </defs>
          <CartesianGrid strokeDasharray='3 3' className='stroke-border' vertical={false} />
          <XAxis
            dataKey='date'
            type='number'
            scale='time'
            domain={['dataMin', 'dataMax']}
            tickLine={false}
            axisLine={false}
            minTickGap={40}
            tick={{ fontSize: 11 }}
            tickFormatter={(value: number) =>
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
            labelFormatter={(value) => formatDate(new Date(Number(value)).toISOString())}
            formatter={(value, name) => [
              formatUsd(Number(value), 2),
              name === 'holdout' ? 'Holdout equity' : 'Equity'
            ]}
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
          {holdoutStart ? (
            <ReferenceLine
              x={holdoutStart}
              stroke='var(--muted-foreground)'
              strokeDasharray='2 4'
              label={{
                value: 'Holdout',
                position: 'insideTopRight',
                fontSize: 11,
                fill: 'var(--muted-foreground)'
              }}
            />
          ) : null}
          <Area
            type='monotone'
            dataKey='equity'
            stroke='var(--primary)'
            strokeWidth={2}
            fill={`url(#${gradientId})`}
            dot={false}
            connectNulls={false}
            isAnimationActive={false}
          />
          {holdoutStart ? (
            <Area
              type='monotone'
              dataKey='holdout'
              stroke='var(--chart-2)'
              strokeWidth={2}
              strokeDasharray='5 3'
              fill={`url(#${holdoutGradientId})`}
              dot={false}
              connectNulls={false}
              isAnimationActive={false}
            />
          ) : null}
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}
