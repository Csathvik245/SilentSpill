export type AgentMode = 'llm' | 'fallback' | 'skipped'
export type Classification = 'incident' | 'storm_runoff' | 'flag_for_review' | 'no_incident'
export type TravelTime = { display: string; hours: number | null; is_lower_bound: boolean; km_with_velocity: number; km_total: number; missing_reach_count: number }
export type Violation = { parameter: string; monitoring_period_end: string | null; violation_desc: string; exceedance_pct: number | null; status: string | null; outfall: string | null; source_fields: string[] }
export type CandidateFacility = {
  facility_id: string; name: string; frs_id: string | null; latitude: number; longitude: number; location_type: 'outfall' | 'facility'; location_note: string
  distance_km_to_reach: number; nearest_reach_nhdplusid: number; upstream_distance_km: number | null; travel_time: TravelTime | null
  numeric_violation_count: number; reporting_violation_count: number; historical_numeric_violation_count: number; violation_window_start: string | null
  violation_window_end: string | null; violation_window_label: string; violations: Violation[]; permit_type: 'individual' | 'general' | null
  permit_type_desc: string | null; permit_note: string | null; facility_type: string | null; small_domestic_general_permit: boolean
}
export type Feature = { type: 'Feature'; geometry: { type: 'LineString'; coordinates: [number, number][] }; properties: Record<string, unknown> }
export type ReachResponse = {
  reach: Feature; max_km: number; upstream_path: { nhdplusid: number; gnis_name: string | null; length_km: number; cumulative_km: number; velocity_mps: number | null }[]
  upstream_geojson: { type: 'FeatureCollection'; features: Feature[] } | null
  gauge: { site_id: string; name: string | null; latitude: number; longitude: number; linked_reach_nhdplusid: number; link_distance_m: number }
}
export type TimelineEntry = { timestamp: string | null; event: string }
export type RelatedPastEvent = { memory_type: 'run' | 'review'; incident_id: string | null; scenario_date: string | null; saved_at: string | null; classification: Classification | null; review_decision: 'confirmed' | 'storm_runoff' | 'false_alarm' | null; text: string }
export type IncidentReport = {
  disclaimer: string; banner: string; summary: string; timeline: TimelineEntry[]; ranked_candidates: CandidateFacility[]; confidence: number
  assumptions: string[]; limitations: string[]; classification: Classification; data_origin: string; report_mode: AgentMode; fallback_reason: string | null
  guardrail_notes: string[]; related_past_events: RelatedPastEvent[]; related_past_events_text: string
}
export type IncidentResponse = {
  incident_id: string; disclaimer: string; triage: { classification: string; confidence: number; reasons: string[] }
  trace: { candidates: CandidateFacility[]; candidate_notes: Record<string, string>; assumptions: string[] }
  compliance: { facts: { facility_id: string; statement: string; value: string; source_field: string; source_file: string }[] }
  report: IncidentReport; agent_modes: Record<'triage' | 'trace' | 'compliance' | 'report', AgentMode>; memory_mode: 'backboard' | 'skipped' | 'failed'; memory_reason: string | null
}
export type ScenarioRunResponse = { incident_id: string; status: 'completed'; case: 'spill' | 'storm'; classification: Classification; report_mode: AgentMode; agent_modes: Record<'triage' | 'trace' | 'compliance' | 'report', AgentMode>; memory_mode: 'backboard' | 'skipped' | 'failed' }
export type SensorSample = { timestamp: string; values: Record<'conductivity' | 'turbidity' | 'ph' | 'discharge', number | null>; simulated: boolean }
export type SensorSeriesResponse = { incident_id: string; site_id: string; baseline: SensorSample[]; injected: SensorSample[] }
export type StreamEvent = {
  incident_id: string; seq: number; kind: 'sensor_tick' | 'plume_detection' | 'agent_output' | 'complete'; scenario_time: string | null
  payload: Record<string, unknown>; bbox: [number, number, number, number] | null; detector: 'hsv' | 'yolo' | 'roboflow' | null
}
