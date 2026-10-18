import type { IncidentResponse, ReachResponse, ScenarioRunResponse, SensorSeriesResponse } from './types'

export const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000').replace(/\/$/, '')

async function request<T>(path: string, init?: RequestInit, signal?: AbortSignal): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, { ...init, signal, headers: { 'Content-Type': 'application/json', ...init?.headers } })
  if (!response.ok) {
    let reason = `${response.status} ${response.statusText}`
    try { const body = await response.json() as { detail?: string }; reason = body.detail ?? reason } catch { /* response body can be plain text */ }
    throw new Error(reason)
  }
  return response.json() as Promise<T>
}

export const getHealth = (signal?: AbortSignal) => request<{ status: 'ok' | 'degraded'; data_ready: Record<string, boolean> }>('/health', undefined, signal)
export const getReach = (signal?: AbortSignal) => request<ReachResponse>('/reach', undefined, signal)
export const runScenario = (caseName: 'spill' | 'storm', signal?: AbortSignal) => request<ScenarioRunResponse>('/scenario/run', { method: 'POST', body: JSON.stringify({ case: caseName }) }, signal)
export const getIncident = (id: string, signal?: AbortSignal) => request<IncidentResponse>(`/incidents/${encodeURIComponent(id)}`, undefined, signal)
export const getSensors = (id: string, signal?: AbortSignal) => request<SensorSeriesResponse>(`/sensors/${encodeURIComponent(id)}`, undefined, signal)
export const streamUrl = (id: string) => `${API_BASE}/stream/${encodeURIComponent(id)}?speed=600`
