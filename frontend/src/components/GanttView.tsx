import React, { useEffect, useRef } from 'react'
import { DataSet } from 'vis-data'
import { Timeline } from 'vis-timeline/standalone'
import 'vis-timeline/styles/vis-timeline-graph2d.min.css'
import type { MaintenanceTask, ScheduledBlock, TrackSection } from '../api/types'
import styles from './GanttView.module.css'

export interface GanttViewProps {
  scheduledBlocks: ScheduledBlock[]
  sections?: TrackSection[]
  tasks?: MaintenanceTask[]
  isLoading?: boolean
  onItemClick?: (taskId: string) => void
  onSelectTask?: (taskId: string) => void
}

const PRIORITY_COLORS: Record<number, { bg: string; border: string }> = {
  1: { bg: '#0284c7', border: '#38bdf8' }, // Low / Routine (Cool Blue)
  2: { bg: '#059669', border: '#34d399' }, // Normal (Teal / Emerald)
  3: { bg: '#d97706', border: '#fbbf24' }, // Medium (Amber)
  4: { bg: '#ea580c', border: '#fb923c' }, // High (Orange)
  5: { bg: '#dc2626', border: '#f87171' }, // Critical / Emergency (Crimson)
}

function getPriorityTier(task?: MaintenanceTask): number {
  if (!task) return 1
  if (task.is_emergency) return 5
  const p = task.priority
  if (p >= 5) return 5
  if (p <= 1) return 1
  return p
}

export const GanttView: React.FC<GanttViewProps> = ({
  scheduledBlocks,
  sections = [],
  tasks = [],
  isLoading = false,
  onItemClick,
  onSelectTask,
}) => {
  const containerRef = useRef<HTMLDivElement | null>(null)
  const timelineRef = useRef<Timeline | null>(null)

  useEffect(() => {
    if (!containerRef.current || isLoading || scheduledBlocks.length === 0) {
      if (timelineRef.current) {
        timelineRef.current.destroy()
        timelineRef.current = null
      }
      return
    }

    // Build task and section lookup maps
    const taskMap = new Map<string, MaintenanceTask>()
    for (const t of tasks) {
      taskMap.set(t.id, t)
    }

    const sectionMap = new Map<string, TrackSection>()
    for (const s of sections) {
      sectionMap.set(s.id, s)
    }

    // Prepare Groups (one row per track section)
    const groupIds = new Set<string>()
    const groupItems: Array<{ id: string; content: string }> = []

    // Priority to declared sections
    for (const s of sections) {
      if (!groupIds.has(s.id)) {
        groupIds.add(s.id)
        groupItems.push({
          id: s.id,
          content: s.name || `Section ${s.id}`,
        })
      }
    }

    // Add any section IDs found in scheduled blocks if not already present
    for (const b of scheduledBlocks) {
      if (!groupIds.has(b.section_id)) {
        groupIds.add(b.section_id)
        groupItems.push({
          id: b.section_id,
          content: `Section ${b.section_id.slice(0, 8)}`,
        })
      }
    }

    const groups = new DataSet(groupItems)

    // Prepare Items
    const timelineItems = scheduledBlocks.map((block) => {
      const task = taskMap.get(block.task_id)
      const section = sectionMap.get(block.section_id)
      const tier = getPriorityTier(task)
      const color = PRIORITY_COLORS[tier] || PRIORITY_COLORS[1]
      const priorityLabel = task?.is_emergency ? 'EMERGENCY' : `P${tier}`
      const title = `Task: ${task?.name || block.task_id} [${priorityLabel}]\nSection: ${section?.name || block.section_id}\nStart: ${new Date(block.start_time).toLocaleString()}\nEnd: ${new Date(block.end_time).toLocaleString()}`

      return {
        id: block.id,
        group: block.section_id,
        content: `<strong>[${priorityLabel}]</strong> ${task?.name || block.task_id.slice(0, 8)}`,
        start: new Date(block.start_time),
        end: new Date(block.end_time),
        title,
        className: `priority-${tier}`,
        style: `background-color: ${color.bg}; border-color: ${color.border}; color: #ffffff;`,
      }
    })

    const items = new DataSet(timelineItems)

    // Compute horizon boundaries from blocks with safe fallback
    const startTimestamps = scheduledBlocks.map((b) => new Date(b.start_time).getTime())
    const endTimestamps = scheduledBlocks.map((b) => new Date(b.end_time).getTime())
    const minStart = Math.min(...startTimestamps)
    const maxEnd = Math.max(...endTimestamps)

    // Timeline Configuration
    const options = {
      stack: false,
      start: new Date(minStart - 30 * 60 * 1000), // 30 min buffer before
      end: new Date(maxEnd + 30 * 60 * 1000), // 30 min buffer after
      editable: false,
      selectable: true,
      multiselect: false,
      orientation: {
        axis: 'top',
        item: 'top',
      },
      zoomMin: 1000 * 60 * 15, // 15 mins
      zoomMax: 1000 * 60 * 60 * 72, // 72 hours
      margin: {
        item: {
          horizontal: 4,
          vertical: 6,
        },
      },
    }

    // Clean up existing timeline before creating a new one
    if (timelineRef.current) {
      timelineRef.current.destroy()
    }

    const timeline = new Timeline(containerRef.current, items, groups, options)
    timelineRef.current = timeline

    // Item selection handler
    timeline.on('select', (properties: { items: (string | number)[] }) => {
      if (properties.items && properties.items.length > 0) {
        const selectedId = String(properties.items[0])
        const block = scheduledBlocks.find((b) => b.id === selectedId)
        if (block) {
          if (onItemClick) {
            onItemClick(block.task_id)
          } else if (onSelectTask) {
            onSelectTask(block.task_id)
          }
        }
      }
    })

    return () => {
      if (timelineRef.current) {
        timelineRef.current.destroy()
        timelineRef.current = null
      }
    }
  }, [scheduledBlocks, sections, tasks, isLoading, onItemClick, onSelectTask])

  if (isLoading) {
    return (
      <div className={styles.ganttContainer} data-testid="gantt-loading">
        <div className={styles.loadingState}>
          <div className={styles.spinner}></div>
          <div>Loading schedule timeline...</div>
        </div>
      </div>
    )
  }

  if (scheduledBlocks.length === 0) {
    return (
      <div className={styles.ganttContainer} data-testid="gantt-empty">
        <div className={styles.emptyState}>
          <svg
            className={styles.emptyIcon}
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeLinecap="round"
            strokeLinejoin="round"
          >
            <rect x="3" y="4" width="18" height="18" rx="2" ry="2" />
            <line x1="16" y1="2" x2="16" y2="6" />
            <line x1="8" y1="2" x2="8" y2="6" />
            <line x1="3" y1="10" x2="21" y2="10" />
          </svg>
          <div className={styles.emptyTitle}>No Scheduled Blocks</div>
          <p className={styles.emptyDesc}>
            No scheduled possession blocks to display. Generate a network and solve the schedule to
            visualize track occupancies.
          </p>
        </div>
      </div>
    )
  }

  return (
    <div className={styles.ganttContainer} data-testid="gantt-view">
      <div className={styles.ganttHeader}>
        <div className={styles.ganttTitleBlock}>
          <span className={styles.ganttTitle}>Track Possession Schedule</span>
          <span className={styles.blockCountBadge}>
            {scheduledBlocks.length} {scheduledBlocks.length === 1 ? 'block' : 'blocks'} scheduled
          </span>
        </div>
        <div className={styles.legend}>
          <span>Priority:</span>
          <div className={styles.legendItem}>
            <span className={`${styles.legendDot} ${styles.dotP1}`}></span>
            <span>P1 Routine</span>
          </div>
          <div className={styles.legendItem}>
            <span className={`${styles.legendDot} ${styles.dotP2}`}></span>
            <span>P2 Normal</span>
          </div>
          <div className={styles.legendItem}>
            <span className={`${styles.legendDot} ${styles.dotP3}`}></span>
            <span>P3 Medium</span>
          </div>
          <div className={styles.legendItem}>
            <span className={`${styles.legendDot} ${styles.dotP4}`}></span>
            <span>P4 High</span>
          </div>
          <div className={styles.legendItem}>
            <span className={`${styles.legendDot} ${styles.dotP5}`}></span>
            <span>P5 Critical / Emergency</span>
          </div>
        </div>
      </div>
      <div
        ref={containerRef}
        className={styles.timelineWrapper}
        data-testid="vis-timeline-container"
      />
    </div>
  )
}

export default GanttView
