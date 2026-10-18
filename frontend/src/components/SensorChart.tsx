import { useMemo } from 'react'
import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import type { SensorSample, SensorSeriesResponse } from '../lib/types'

type Props = { data: SensorSeriesResponse | null; replayRows: SensorSample[]; scenarioTime: string | null }
export function SensorChart({ data, replayRows, scenarioTime }: Props) {
  const chart = useMemo(() => {
    if (!data) return { rows: [], parameter: 'turbidity' }
    const parameter = data.baseline.some((sample) => sample.values.turbidity != null) ? 'turbidity' : data.baseline.some((sample) => sample.values.conductivity != null) ? 'conductivity' : 'ph'
    const map = new Map<string, { timestamp: string; baseline: number | null; simulated: number | null }>()
    const rows = replayRows.length ? replayRows : [...data.baseline, ...data.injected].sort((a, b) => a.timestamp.localeCompare(b.timestamp))
    for (const sample of rows) {
      const value = sample.values[parameter as keyof SensorSample['values']]
      const row = map.get(sample.timestamp) ?? { timestamp: sample.timestamp, baseline: null, simulated: null }
      if (value != null) row[sample.simulated ? 'simulated' : 'baseline'] = value
      map.set(sample.timestamp, row)
    }
    const result = [...map.values()].sort((a, b) => a.timestamp.localeCompare(b.timestamp))
    if (scenarioTime) return { rows: result.filter((row) => row.timestamp <= scenarioTime), parameter }
    return { rows: result, parameter }
  }, [data, replayRows, scenarioTime])

  if (!data) return <div className="chart-empty"><ActivityMark />Sensor history appears after a scenario is loaded.</div>
  if (!chart.rows.length) return <div className="chart-empty"><ActivityMark />Waiting for sensor ticks from the replay.</div>
  return <div className="chart-area"><div className="chart-legend"><span><i className="chart-baseline-line" /> Real baseline</span><span><i className="chart-sim-line" /> Simulated values</span><small>{chart.parameter}</small></div><ResponsiveContainer width="100%" height={215}><LineChart data={chart.rows} margin={{ top: 13, right: 12, bottom: 2, left: -21 }}><CartesianGrid stroke="#243438" vertical={false} /><XAxis dataKey="timestamp" tickFormatter={(value: string) => new Date(value).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', timeZone: 'UTC' })} tick={{ fill: '#80918d', fontSize: 8 }} tickLine={false} axisLine={{ stroke: '#314144' }} minTickGap={35} /><YAxis tick={{ fill: '#80918d', fontSize: 8 }} tickLine={false} axisLine={false} width={38} domain={['auto', 'auto']} /><Tooltip labelFormatter={(value) => new Date(String(value)).toLocaleString([], { timeZone: 'UTC', dateStyle: 'medium', timeStyle: 'short', timeZoneName: 'short' })} contentStyle={{ background: '#101a1e', border: '1px solid #344447', borderRadius: 4, color: '#dce7e3', fontSize: 9 }} itemStyle={{ color: '#bcd0ca' }} /><Line type="monotone" dataKey="baseline" name="Real baseline" stroke="#61c7b6" strokeWidth={1.8} dot={false} connectNulls isAnimationActive animationDuration={240} /><Line type="monotone" dataKey="simulated" name="Simulated values" stroke="#e1a456" strokeWidth={2} strokeDasharray="5 4" dot={false} connectNulls isAnimationActive animationDuration={240} /></LineChart></ResponsiveContainer><p className="chart-caption">{replayRows.length ? 'Live sequence from /stream · time shown in UTC' : `Sensor series for site ${data.site_id} · time shown in UTC`}</p></div>
}
function ActivityMark() { return <span className="chart-placeholder-mark" aria-hidden="true" /> }
