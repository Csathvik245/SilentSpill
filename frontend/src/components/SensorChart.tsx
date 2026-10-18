import { useMemo } from 'react'
import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import type { SensorSample, SensorSeriesResponse } from '../lib/types'

type Props = { data: SensorSeriesResponse | null; replayRows: SensorSample[]; scenarioTime: string | null }
const PARAMETERS = [{ key: 'conductivity', label: 'Conductivity' }, { key: 'turbidity', label: 'Turbidity' }, { key: 'ph', label: 'pH' }] as const
export function SensorChart({ data, replayRows, scenarioTime }: Props) {
  const charts = useMemo(() => PARAMETERS.map(parameter => {
    const points = new Map<string, { timestamp: string; baseline: number | null; simulated: number | null }>()
    const samples = [...(data?.baseline ?? []), ...(replayRows.length ? replayRows : data?.injected ?? [])]
    for (const sample of samples) {
      if (scenarioTime && sample.timestamp > scenarioTime) continue
      const row = points.get(sample.timestamp) ?? { timestamp: sample.timestamp, baseline: null, simulated: null }
      const value = sample.values[parameter.key]
      if (value != null) row[sample.simulated ? 'simulated' : 'baseline'] = value
      points.set(sample.timestamp, row)
    }
    return { ...parameter, rows: [...points.values()].sort((a, b) => a.timestamp.localeCompare(b.timestamp)) }
  }), [data, replayRows, scenarioTime])
  if (!data) return <div className="chart-empty">Sensor history appears after a scenario is loaded.</div>
  return <div className="sensor-charts"><div className="chart-legend"><span><i className="chart-baseline-line" />Real observations</span><span><i className="chart-sim-line" />Simulated</span></div>{charts.map(chart => <section className="sensor-chart" key={chart.key}><h4>{chart.label}</h4>{chart.rows.some(row => row.baseline != null || row.simulated != null) ? <ResponsiveContainer width="100%" height={190}><LineChart data={chart.rows} margin={{ top: 16, right: 8, bottom: 0, left: 0 }}><CartesianGrid stroke="var(--color-line)" vertical={false} /><XAxis dataKey="timestamp" tickFormatter={(value: string) => new Date(value).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', timeZone: 'UTC' })} tick={{ fill: 'var(--color-muted)', fontSize: 12 }} tickLine={false} axisLine={false} minTickGap={55} /><YAxis tick={{ fill: 'var(--color-muted)', fontSize: 12 }} tickLine={false} axisLine={false} width={48} domain={['auto', 'auto']} /><Tooltip labelFormatter={value => new Date(String(value)).toLocaleString([], { timeZone: 'UTC' })} contentStyle={{ background: 'var(--color-panel)', border: '1px solid var(--color-line)', borderRadius: 6, color: 'var(--color-ink)', fontSize: 14 }} /><Line type="monotone" dataKey="baseline" name="Real observations" stroke="var(--color-river)" strokeWidth={1.8} dot={false} connectNulls isAnimationActive={false} /><Line type="monotone" dataKey="simulated" name="Simulated" stroke="var(--color-simulated)" strokeWidth={2} strokeDasharray="5 4" dot={false} connectNulls isAnimationActive={false} /></LineChart></ResponsiveContainer> : <p className="chart-empty">No measurements available for this parameter.</p>}</section>)}<p className="chart-caption">Gauge {data.site_id} · Time shown in UTC{replayRows.length > 0 && ' · Live replay'}</p></div>
}
