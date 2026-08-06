import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import type { ChartSpec } from '@/lib/copilot'

const SERIES_COLORS = [
  'var(--chart-1)',
  'var(--chart-2)',
  'var(--chart-3)',
  'var(--chart-4)',
  'var(--chart-5)',
]

/** Render an agent-emitted, Recharts-shaped chart spec. */
export function CopilotChart({ spec }: { spec: ChartSpec }) {
  const common = {
    data: spec.data,
    margin: { top: 8, right: 8, bottom: 4, left: 0 },
  }
  const axes = (
    <>
      <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
      <XAxis dataKey={spec.x_key} tick={{ fontSize: 11 }} stroke="var(--muted-foreground)" />
      <YAxis
        tick={{ fontSize: 11 }}
        stroke="var(--muted-foreground)"
        label={
          spec.y_label
            ? { value: spec.y_label, angle: -90, position: 'insideLeft', fontSize: 11 }
            : undefined
        }
      />
      <Tooltip
        contentStyle={{
          background: 'var(--popover)',
          border: '1px solid var(--border)',
          borderRadius: 8,
          fontSize: 12,
        }}
      />
      {spec.series.length > 1 && <Legend wrapperStyle={{ fontSize: 12 }} />}
    </>
  )

  return (
    <figure className="flex flex-col gap-1 rounded-md border p-3">
      <figcaption className="text-sm font-medium">{spec.title}</figcaption>
      <div className="h-56 w-full">
        <ResponsiveContainer>
          {spec.chart_type === 'line' ? (
            <LineChart {...common}>
              {axes}
              {spec.series.map((series, index) => (
                <Line
                  key={series.data_key}
                  type="monotone"
                  dataKey={series.data_key}
                  name={series.label}
                  stroke={SERIES_COLORS[index % SERIES_COLORS.length]}
                  strokeWidth={2}
                  dot={{ r: 3 }}
                />
              ))}
            </LineChart>
          ) : spec.chart_type === 'area' ? (
            <AreaChart {...common}>
              {axes}
              {spec.series.map((series, index) => (
                <Area
                  key={series.data_key}
                  type="monotone"
                  dataKey={series.data_key}
                  name={series.label}
                  stroke={SERIES_COLORS[index % SERIES_COLORS.length]}
                  fill={SERIES_COLORS[index % SERIES_COLORS.length]}
                  fillOpacity={0.25}
                />
              ))}
            </AreaChart>
          ) : (
            <BarChart {...common}>
              {axes}
              {spec.series.map((series, index) => (
                <Bar
                  key={series.data_key}
                  dataKey={series.data_key}
                  name={series.label}
                  fill={SERIES_COLORS[index % SERIES_COLORS.length]}
                  radius={[3, 3, 0, 0]}
                />
              ))}
            </BarChart>
          )}
        </ResponsiveContainer>
      </div>
      <p className="text-xs text-muted-foreground">Source: {spec.source}</p>
    </figure>
  )
}
