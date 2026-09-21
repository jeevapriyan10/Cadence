import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App'
import * as apiClient from './api/client'
import type { MaintenanceTask, ScheduledBlock, TrackSection } from './api/types'
import { GanttView } from './components/GanttView'

const { MockTimeline, getLastItemsPassed, triggerTimelineSelect } = vi.hoisted(() => {
  let lastItems: any = null
  let lastGroups: any = null
  let lastSelectCallback: any = null
  const destroyFn = vi.fn()
  const onFn = vi.fn((event: string, callback: any) => {
    if (event === 'select') {
      lastSelectCallback = callback
    }
  })

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
          const handleClick = () => {
            if (lastSelectCallback) {
              lastSelectCallback({ items: [it.id] })
            }
          }
          el.onclick = handleClick
          el.addEventListener('click', handleClick)
          container.appendChild(el)
        })
      }
    }

    destroy = () => {
      if (this.container) {
        this.container.innerHTML = ''
      }
      destroyFn()
    }
    on = onFn
  }

  return {
    MockTimeline,
    mockTimelineDestroy: destroyFn,
    mockTimelineOn: onFn,
    getLastItemsPassed: () => lastItems,
    getLastGroupsPassed: () => lastGroups,
    triggerTimelineSelect: (itemId: string) => {
      if (lastSelectCallback) {
        lastSelectCallback({ items: [itemId] })
      }
    },
    clearTimelineState: () => {
      lastItems = null
      lastGroups = null
      lastSelectCallback = null
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

  it('RipplePanel shows its empty state correctly before any solve has occurred', async () => {
    vi.spyOn(apiClient, 'generateNetwork').mockResolvedValueOnce({
      run_id: 'test-run-empty-ripple',
      profile_name: 'metro',
      seed: 12345,
      section_count: 3,
      train_count: 5,
      task_count: 2,
    })

    vi.spyOn(apiClient, 'getNetwork').mockResolvedValueOnce({
      run_id: 'test-run-empty-ripple',
      profile_name: 'metro',
      seed: 12345,
      sections: [],
      adjacencies: [],
      train_slots: [],
      maintenance_tasks: [],
    })

    render(<App />)

    const generateBtn = screen.getByRole('button', { name: /generate network/i })
    fireEvent.click(generateBtn)

    await waitFor(() => {
      expect(screen.getByText('test-run-empty-ripple')).toBeInTheDocument()
    })

    // RipplePanel is rendered and shows empty state before solve has occurred
    expect(screen.getByTestId('ripple-panel')).toBeInTheDocument()
    expect(screen.getByTestId('ripple-empty')).toBeInTheDocument()
    expect(screen.getByText('Solve a schedule to see impact')).toBeInTheDocument()
  })

  it('solving a schedule (mocked) triggers both the Gantt view update AND an automatic ripple fetch, without requiring a separate user action', async () => {
    const runId = 'test-run-solve-ripple'
    vi.spyOn(apiClient, 'generateNetwork').mockResolvedValueOnce({
      run_id: runId,
      profile_name: 'metro',
      seed: 42,
      section_count: 4,
      train_count: 8,
      task_count: 3,
    })

    vi.spyOn(apiClient, 'getNetwork').mockResolvedValueOnce({
      run_id: runId,
      profile_name: 'metro',
      seed: 42,
      sections: [{ id: 'sec-1', name: 'Main Track 1' }],
      adjacencies: [],
      train_slots: [],
      maintenance_tasks: [
        {
          id: 'task-1',
          name: 'Track Grinding',
          section_id: 'sec-1',
          duration_minutes: 120,
          earliest_start: '2026-09-20T08:00:00Z',
          latest_end: '2026-09-20T12:00:00Z',
          priority: 3,
          is_emergency: false,
        },
      ],
    })

    const solveSpy = vi.spyOn(apiClient, 'solveSchedule').mockResolvedValueOnce({
      run_id: runId,
      status: 'OPTIMAL',
      scheduled_blocks: [
        {
          id: 'blk-1',
          task_id: 'task-1',
          section_id: 'sec-1',
          start_time: '2026-09-20T08:00:00Z',
          end_time: '2026-09-20T10:00:00Z',
          method: 'CP_SAT',
          created_at: '2026-09-20T07:30:00Z',
        },
      ],
      objective_value: 100,
      wall_time_seconds: 0.035,
    })

    const rippleSpy = vi.spyOn(apiClient, 'getRipple').mockResolvedValueOnce({
      run_id: runId,
      total_trains_affected: 2,
      total_delay_minutes: 50,
      per_train_impacts: [
        {
          train_slot_id: 'train-1',
          train_name: 'Express-104',
          directly_affected: true,
          delay_minutes: 35,
          affected_sections: ['sec-1'],
          cascade_source: null,
        },
        {
          train_slot_id: 'train-2',
          train_name: 'Local-302',
          directly_affected: false,
          delay_minutes: 15,
          affected_sections: ['sec-2'],
          cascade_source: 'Express-104',
        },
      ],
      summary: '2 trains delayed by 50 min.',
    })

    render(<App />)

    // 1. Generate Network
    fireEvent.click(screen.getByRole('button', { name: /generate network/i }))

    await waitFor(() => {
      expect(screen.getByText(runId)).toBeInTheDocument()
    })

    // 2. Click "Solve Schedule"
    const solveBtn = screen.getByRole('button', { name: /solve schedule/i })
    fireEvent.click(solveBtn)

    // Verify solveSchedule called
    await waitFor(() => {
      expect(solveSpy).toHaveBeenCalledWith({ run_id: runId })
    })

    // Verify Gantt view updated with scheduled blocks
    await waitFor(() => {
      expect(screen.getByTestId('gantt-view')).toBeInTheDocument()
      expect(screen.getByText('1 block scheduled')).toBeInTheDocument()
    })

    // Verify automatic ripple fetch triggered without extra user action
    await waitFor(() => {
      expect(rippleSpy).toHaveBeenCalledWith(runId)
      expect(screen.getByTestId('total-trains-affected')).toHaveTextContent('2')
      expect(screen.getByTestId('total-delay-minutes')).toHaveTextContent('50 min')
      expect(screen.getByText('Express-104')).toBeInTheDocument()
      expect(screen.getByText('+35 min')).toBeInTheDocument()
      expect(screen.getByText('Local-302')).toBeInTheDocument()
      expect(
        screen.getByText(/delayed due to cascade from Express-104/i),
      ).toBeInTheDocument()
    })
  })

  it('clicking a Gantt item opens ExplanationPanel and triggers a call to getExplanation with the correct run_id/task_id', async () => {
    const runId = 'test-run-gantt-click'
    vi.spyOn(apiClient, 'generateNetwork').mockResolvedValueOnce({
      run_id: runId,
      profile_name: 'metro',
      seed: 99,
      section_count: 2,
      train_count: 4,
      task_count: 1,
    })

    vi.spyOn(apiClient, 'getNetwork').mockResolvedValueOnce({
      run_id: runId,
      profile_name: 'metro',
      seed: 99,
      sections: [{ id: 'sec-1', name: 'Section 1' }],
      adjacencies: [],
      train_slots: [],
      maintenance_tasks: [
        {
          id: 'task-target-99',
          name: 'Emergency Rail Weld',
          section_id: 'sec-1',
          duration_minutes: 60,
          earliest_start: '2026-09-20T09:00:00Z',
          latest_end: '2026-09-20T11:00:00Z',
          priority: 5,
          is_emergency: true,
        },
      ],
    })

    vi.spyOn(apiClient, 'solveSchedule').mockResolvedValueOnce({
      run_id: runId,
      status: 'OPTIMAL',
      scheduled_blocks: [
        {
          id: 'blk-target-1',
          task_id: 'task-target-99',
          section_id: 'sec-1',
          start_time: '2026-09-20T09:00:00Z',
          end_time: '2026-09-20T10:00:00Z',
          method: 'CP_SAT',
          created_at: '2026-09-20T08:00:00Z',
        },
      ],
      objective_value: 200,
      wall_time_seconds: 0.02,
    })

    vi.spyOn(apiClient, 'getRipple').mockResolvedValueOnce({
      run_id: runId,
      total_trains_affected: 0,
      total_delay_minutes: 0,
      per_train_impacts: [],
      summary: '0 delays',
    })

    const explainSpy = vi.spyOn(apiClient, 'getExplanation').mockResolvedValueOnce({
      run_id: runId,
      task_id: 'task-target-99',
      explanation: {
        task_id: 'task-target-99',
        task_name: 'Emergency Rail Weld',
        scheduled_start: '2026-09-20T09:00:00Z',
        scheduled_end: '2026-09-20T10:00:00Z',
        chosen_reason: 'Emergency high priority window awarded immediately.',
        rejected_candidates: [
          {
            candidate_start: '2026-09-20T10:00:00Z',
            candidate_end: '2026-09-20T11:00:00Z',
            rejection_reason: 'conflicts with scheduled passenger service',
            conflicting_entity_id: 'TR-10',
            conflicting_entity_type: 'train_slot',
          },
        ],
      },
      summary: 'Explanation summary',
    })

    const { container } = render(<App />)

    // Generate network
    fireEvent.click(screen.getByRole('button', { name: /generate network/i }))

    await waitFor(() => {
      expect(screen.getByText(runId)).toBeInTheDocument()
    })

    // Wait for generate to complete so solve button becomes enabled
    const solveBtn = screen.getByRole('button', { name: /solve schedule/i })
    await waitFor(() => {
      expect(solveBtn).not.toBeDisabled()
    })

    // Solve schedule
    fireEvent.click(solveBtn)

    await waitFor(() => {
      expect(screen.getByTestId('gantt-view')).toBeInTheDocument()
      expect(container.querySelector('.vis-item')).not.toBeNull()
    })

    // Find the rendered timeline item and click it
    const itemEl = container.querySelector('.vis-item') as HTMLElement
    expect(itemEl).not.toBeNull()
    fireEvent.click(itemEl)
    triggerTimelineSelect('blk-target-1')

    // Verify ExplanationPanel opened and getExplanation was called with correct run_id and task_id
    await waitFor(() => {
      expect(explainSpy).toHaveBeenCalledTimes(1)
      expect(explainSpy).toHaveBeenCalledWith(runId, 'task-target-99')
      const panel = screen.getByTestId('explanation-panel')
      expect(panel).toBeInTheDocument()
      expect(within(panel).getByText('Emergency Rail Weld')).toBeInTheDocument()
      expect(
        within(panel).getByText('Emergency high priority window awarded immediately.'),
      ).toBeInTheDocument()
      expect(
        within(panel).getByText(/conflicts with scheduled passenger service/i),
      ).toBeInTheDocument()
    })
  })
})

