import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App'
import * as apiClient from './api/client'
import type { MaintenanceTask, ScheduledBlock, TrackSection } from './api/types'
import { GanttView } from './components/GanttView'

const { MockTimeline, getLastItemsPassed } = vi.hoisted(() => {
  let lastItems: any = null
  let lastGroups: any = null
  const destroyFn = vi.fn()
  const onFn = vi.fn()

  class MockTimeline {
    container: any
    items: any
    groups: any
    options: any

    constructor(container: any, items: any, groups: any, options: any) {
      this.container = container
      this.items = items
      this.groups = groups
      this.options = options
      lastItems = items
      lastGroups = groups

      if (container && items) {
        const itemArray = typeof items.get === 'function' ? items.get() : items
        itemArray.forEach((it: any) => {
          const el = document.createElement('div')
          el.className = `vis-item ${it.className || ''}`
          el.dataset.id = String(it.id)
          el.innerHTML = it.content
          container.appendChild(el)
        })
      }
    }

    destroy = destroyFn
    on = onFn
  }

  return {
    MockTimeline,
    mockTimelineDestroy: destroyFn,
    mockTimelineOn: onFn,
    getLastItemsPassed: () => lastItems,
    getLastGroupsPassed: () => lastGroups,
    clearState: () => {
      lastItems = null
      lastGroups = null
    },
  }
})

vi.mock('vis-timeline/standalone', async () => {
  const actual = await vi.importActual<any>('vis-timeline/standalone')
  return {
    ...actual,
    Timeline: MockTimeline,
  }
})

describe('Cadence Board & GanttView Integration', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  afterEach(() => {
    cleanup()
  })

  it('App renders without crashing', () => {
    render(<App />)
    expect(screen.getByText('Cadence')).toBeInTheDocument()
    expect(
      screen.getByText("It doesn't predict delays. It decides when the track is free."),
    ).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /generate network/i })).toBeInTheDocument()
  })

  it('the profile dropdown has metro/local/mainline options', () => {
    render(<App />)
    const select = screen.getByLabelText(/network profile/i) as HTMLSelectElement
    expect(select).toBeInTheDocument()

    const options = Array.from(select.options).map((opt) => opt.value)
    expect(options).toEqual(['metro', 'local', 'mainline'])
  })

  it('clicking "Generate Network" (with fetch mocked) calls the API client\'s generateNetwork function', async () => {
    const generateSpy = vi.spyOn(apiClient, 'generateNetwork').mockResolvedValueOnce({
      run_id: 'test-run-123',
      profile_name: 'metro',
      seed: 42,
      section_count: 5,
      train_count: 10,
      task_count: 8,
    })

    const getNetworkSpy = vi.spyOn(apiClient, 'getNetwork').mockResolvedValueOnce({
      run_id: 'test-run-123',
      profile_name: 'metro',
      seed: 42,
      sections: [],
      adjacencies: [],
      train_slots: [],
      maintenance_tasks: [],
    })

    render(<App />)

    const generateBtn = screen.getByRole('button', { name: /generate network/i })
    fireEvent.click(generateBtn)

    await waitFor(() => {
      expect(generateSpy).toHaveBeenCalledTimes(1)
    })

    expect(generateSpy).toHaveBeenCalledWith({
      profile_name: 'metro',
      seed: expect.any(Number),
    })

    await waitFor(() => {
      expect(getNetworkSpy).toHaveBeenCalledWith('test-run-123')
      expect(screen.getByText('test-run-123')).toBeInTheDocument()
    })
  })

  it('shows clear error banner when backend request fails', async () => {
    vi.spyOn(apiClient, 'generateNetwork').mockRejectedValueOnce(
      new apiClient.ApiError(
        'Network request failed to http://localhost:8000/networks/generate: Failed to fetch. Please ensure the Cadence backend is running at http://localhost:8000.',
        0,
      ),
    )

    render(<App />)

    const generateBtn = screen.getByRole('button', { name: /generate network/i })
    fireEvent.click(generateBtn)

    await waitFor(() => {
      expect(screen.getByRole('alert')).toBeInTheDocument()
      expect(
        screen.getByText(/Please ensure the Cadence backend is running/i),
      ).toBeInTheDocument()
    })

    // Dismiss error button works
    const dismissBtn = screen.getByRole('button', { name: /dismiss error/i })
    fireEvent.click(dismissBtn)

    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('GanttView renders without crashing when given an empty scheduled_blocks array', () => {
    render(<GanttView scheduledBlocks={[]} sections={[]} />)
    expect(screen.getByTestId('gantt-empty')).toBeInTheDocument()
    expect(screen.getByText('No Scheduled Blocks')).toBeInTheDocument()
    expect(
      screen.getByText(/No scheduled possession blocks to display/i),
    ).toBeInTheDocument()
  })

  it('GanttView renders the correct number of timeline items when given a small mock schedule', () => {
    const mockSections: TrackSection[] = [
      { id: 'sec-1', name: 'Section North' },
      { id: 'sec-2', name: 'Section South' },
    ]

    const mockTasks: MaintenanceTask[] = [
      {
        id: 'task-1',
        name: 'Rail Grinding',
        section_id: 'sec-1',
        duration_minutes: 120,
        earliest_start: '2026-09-20T10:00:00Z',
        latest_end: '2026-09-20T14:00:00Z',
        priority: 4,
        is_emergency: false,
      },
      {
        id: 'task-2',
        name: 'Track Alignment',
        section_id: 'sec-2',
        duration_minutes: 90,
        earliest_start: '2026-09-20T11:00:00Z',
        latest_end: '2026-09-20T15:00:00Z',
        priority: 2,
        is_emergency: false,
      },
      {
        id: 'task-3',
        name: 'Signal Repair',
        section_id: 'sec-1',
        duration_minutes: 60,
        earliest_start: '2026-09-20T14:00:00Z',
        latest_end: '2026-09-20T16:00:00Z',
        priority: 5,
        is_emergency: true,
      },
    ]

    const mockBlocks: ScheduledBlock[] = [
      {
        id: 'blk-1',
        task_id: 'task-1',
        section_id: 'sec-1',
        start_time: '2026-09-20T10:30:00Z',
        end_time: '2026-09-20T12:30:00Z',
        method: 'CP_SAT',
        created_at: '2026-09-20T10:00:00Z',
      },
      {
        id: 'blk-2',
        task_id: 'task-2',
        section_id: 'sec-2',
        start_time: '2026-09-20T11:00:00Z',
        end_time: '2026-09-20T12:30:00Z',
        method: 'CP_SAT',
        created_at: '2026-09-20T10:00:00Z',
      },
      {
        id: 'blk-3',
        task_id: 'task-3',
        section_id: 'sec-1',
        start_time: '2026-09-20T14:00:00Z',
        end_time: '2026-09-20T15:00:00Z',
        method: 'CP_SAT',
        created_at: '2026-09-20T10:00:00Z',
      },
    ]

    const { container } = render(
      <GanttView
        scheduledBlocks={mockBlocks}
        sections={mockSections}
        tasks={mockTasks}
      />,
    )

    expect(screen.getByTestId('gantt-view')).toBeInTheDocument()
    expect(screen.getByText('3 blocks scheduled')).toBeInTheDocument()

    // Verify timeline items rendered
    const itemsInDom = container.querySelectorAll('.vis-item')
    expect(itemsInDom.length).toBe(3)

    // Verify DataSet received exactly 3 items
    const lastItems = getLastItemsPassed()
    expect(lastItems).not.toBeNull()
    const rawItems = typeof lastItems.get === 'function' ? lastItems.get() : lastItems
    expect(rawItems.length).toBe(3)

    // Verify priority color classes
    expect(rawItems[0].className).toBe('priority-4')
    expect(rawItems[1].className).toBe('priority-2')
    expect(rawItems[2].className).toBe('priority-5')
  })
})
