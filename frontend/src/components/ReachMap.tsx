import { useEffect, useRef } from 'react'
import { LngLatBounds, Map as MapLibreMap, NavigationControl, type GeoJSONSource } from 'maplibre-gl'
import { setWorkerUrl } from 'maplibre-gl'
import mapLibreWorkerUrl from 'maplibre-gl/dist/maplibre-gl-worker.mjs?url'
import 'maplibre-gl/dist/maplibre-gl.css'
import type { CandidateFacility, ReachResponse } from '../lib/types'

setWorkerUrl(mapLibreWorkerUrl)

type Props = { reach: ReachResponse | null; facilities: CandidateFacility[]; activeFacility: string | null; pulse: number; onSelect: (id: string) => void }
export function ReachMap({ reach, facilities, activeFacility, pulse, onSelect }: Props) {
  const container = useRef<HTMLDivElement>(null)
  const map = useRef<MapLibreMap | null>(null)
  const initialized = useRef(false)
  const pulseTimeout = useRef<number | undefined>(undefined)
  const selectionTimeout = useRef<number | undefined>(undefined)
  useEffect(() => {
    if (!container.current || !reach) return
    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches
    const instance = new MapLibreMap({
      container: container.current,
      style: { version: 8, sources: { osm: { type: 'raster', tiles: ['https://tile.openstreetmap.org/{z}/{x}/{y}.png'], tileSize: 256, attribution: '© OpenStreetMap contributors' } }, layers: [{ id: 'paper', type: 'raster', source: 'osm' }] },
      center: [reach.gauge.longitude, reach.gauge.latitude], zoom: 5,
    })
    map.current = instance
    instance.addControl(new NavigationControl({ showCompass: false }), 'top-right')
    instance.on('load', () => {
      const features = [...(reach.upstream_geojson?.features ?? [reach.reach])].sort((a, b) => Number(a.properties.cumulative_km ?? 0) - Number(b.properties.cumulative_km ?? 0))
      instance.addSource('upstream-path', { type: 'geojson', lineMetrics: true, data: { type: 'FeatureCollection', features: [] } })
      instance.addLayer({ id: 'river-glow', type: 'line', source: 'upstream-path', paint: { 'line-color': '#42c2cb', 'line-width': 8, 'line-opacity': .12, 'line-opacity-transition': { duration: 300, delay: 0 }, 'line-width-transition': { duration: 360, delay: 0 } } })
      instance.addLayer({ id: 'river-path', type: 'line', source: 'upstream-path', paint: { 'line-color': '#63d5dc', 'line-width': 2.5, 'line-opacity': .95, 'line-opacity-transition': { duration: 300, delay: 0 } } })
      const pointFeatures = facilities.map((item) => ({ type: 'Feature' as const, geometry: { type: 'Point' as const, coordinates: [item.longitude, item.latitude] }, properties: { id: item.facility_id, name: item.name } }))
      pointFeatures.push({ type: 'Feature', geometry: { type: 'Point', coordinates: [reach.gauge.longitude, reach.gauge.latitude] }, properties: { id: reach.gauge.site_id, name: reach.gauge.name ?? '' } })
      instance.addSource('review-points', { type: 'geojson', data: { type: 'FeatureCollection', features: pointFeatures } })
      instance.addLayer({ id: 'point-halo', type: 'circle', source: 'review-points', paint: { 'circle-radius': ['case', ['==', ['get', 'id'], reach.gauge.site_id], 7, 5], 'circle-color': ['case', ['==', ['get', 'id'], reach.gauge.site_id], '#67c7b3', '#e1a456'], 'circle-opacity': .95, 'circle-stroke-color': '#071419', 'circle-stroke-width': 2, 'circle-radius-transition': { duration: 360, delay: 0 }, 'circle-opacity-transition': { duration: 360, delay: 0 } } })
      instance.addLayer({ id: 'selection-pulse', type: 'circle', source: 'review-points', filter: ['==', ['get', 'id'], '__none__'], paint: { 'circle-radius': 5, 'circle-color': '#e1a456', 'circle-opacity': .35, 'circle-stroke-color': '#f1cb8a', 'circle-stroke-width': 1, 'circle-radius-transition': { duration: reduced ? 0 : 360, delay: 0 }, 'circle-opacity-transition': { duration: reduced ? 0 : 360, delay: 0 } } })
      instance.on('click', 'point-halo', (event) => {
        const feature = event.features?.[0]
        const id = feature?.properties?.id
        if (typeof id === 'string' && facilities.some((candidate) => candidate.facility_id === id)) onSelect(id)
      })
      instance.on('mouseenter', 'point-halo', () => { instance.getCanvas().style.cursor = 'pointer' })
      instance.on('mouseleave', 'point-halo', () => { instance.getCanvas().style.cursor = '' })
      const bounds = new LngLatBounds()
      features.forEach((feature) => feature.geometry.coordinates.forEach((coordinate) => bounds.extend(coordinate)))
      bounds.extend([reach.gauge.longitude, reach.gauge.latitude])
      if (facilities.length) facilities.forEach((item) => bounds.extend([item.longitude, item.latitude]))
      if (reduced) instance.fitBounds(bounds, { padding: 70, maxZoom: 12, duration: 0 })
      else {
        instance.fitBounds(bounds, { padding: 70, maxZoom: 12, duration: 0 })
        instance.setZoom(Math.max(4, instance.getZoom() - 4))
        window.setTimeout(() => instance.flyTo({ center: bounds.getCenter(), zoom: Math.min(12, instance.getZoom() + 4), duration: 1000, essential: false }), 150)
      }
      initialized.current = true
      const source = instance.getSource('upstream-path') as GeoJSONSource
      if (reduced || features.length < 2) source.setData({ type: 'FeatureCollection', features })
      else {
        const maxDistance = Math.max(1, ...features.map((feature) => Number(feature.properties.cumulative_km ?? 0)))
        const start = performance.now()
        const paintFrame = (now: number) => {
          if (map.current !== instance) return
          const progress = Math.min(1, (now - start) / 2100)
          const distance = maxDistance * progress
          let visibleCount = 0
          while (visibleCount < features.length && Number(features[visibleCount].properties.cumulative_km ?? 0) <= distance) visibleCount += 1
          source.setData({ type: 'FeatureCollection', features: features.slice(0, Math.max(1, visibleCount)) })
          if (progress < 1) window.requestAnimationFrame(paintFrame)
        }
        window.requestAnimationFrame(paintFrame)
      }
    })
    return () => { window.clearTimeout(pulseTimeout.current); window.clearTimeout(selectionTimeout.current); instance.remove(); map.current = null; initialized.current = false }
  }, [reach, facilities, onSelect])

  useEffect(() => {
    const instance = map.current
    if (!instance || !initialized.current || !activeFacility) return
    const point = facilities.find((candidate) => candidate.facility_id === activeFacility)
    if (point) {
      const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches
      instance.flyTo({ center: [point.longitude, point.latitude], zoom: Math.max(instance.getZoom(), 11), duration: reduced ? 0 : 500 })
      if (instance.getLayer('selection-pulse')) {
        instance.setFilter('selection-pulse', ['==', ['get', 'id'], activeFacility])
        instance.setPaintProperty('selection-pulse', 'circle-radius', reduced ? 9 : 6)
        instance.setPaintProperty('selection-pulse', 'circle-opacity', .55)
        window.clearTimeout(selectionTimeout.current)
        if (!reduced) selectionTimeout.current = window.setTimeout(() => {
          instance.setPaintProperty('selection-pulse', 'circle-radius', 17)
          instance.setPaintProperty('selection-pulse', 'circle-opacity', 0)
          selectionTimeout.current = window.setTimeout(() => {
            instance.setFilter('selection-pulse', ['==', ['get', 'id'], '__none__'])
            instance.setPaintProperty('selection-pulse', 'circle-radius', 5)
            instance.setPaintProperty('selection-pulse', 'circle-opacity', .35)
          }, 400)
        }, 60)
      }
    }
  }, [activeFacility, facilities])

  useEffect(() => {
    const instance = map.current
    if (!instance?.getLayer('river-glow') || !pulse) return
    instance.setPaintProperty('river-glow', 'line-width', 15)
    instance.setPaintProperty('river-glow', 'line-opacity', .32)
    window.clearTimeout(pulseTimeout.current)
    pulseTimeout.current = window.setTimeout(() => { instance.setPaintProperty('river-glow', 'line-width', 8); instance.setPaintProperty('river-glow', 'line-opacity', .12) }, 420)
  }, [pulse])

  return <div ref={container} className="map-canvas" aria-label="Interactive map of the upstream river network" />
}
