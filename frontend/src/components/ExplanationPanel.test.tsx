import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as apiClient from '../api/client'
import type { ExplainResponse, Explanation } from '../api/types'
import { ExplanationPanel } from './ExplanationPanel'

describe('ExplanationPanel Component', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  afterEach(() => {
    cleanup()
  })

  it('renders nothing when isOpen is false', () => {
    const { container } = render(
      <ExplanationPanel
        runId="run-test-1"
        taskId="task-101"
        isOpen={false}
        onClose={vi.fn()}
      />,
    )
    expect(container.firstChild).toBeNull()
  })

  it('renders loading state while explanation is fetching', async () => {
    // Delay resolution to verify loading state
    vi.spyOn(apiClient, 'getExplanation').mockImplementation(
      () => new Promise(() => {}),
    )

    render(
      <ExplanationPanel
        runId="run-test-1"
        taskId="task-101"
        isOpen={true}
        onClose={vi.fn()}
      />,
    )

    expect(screen.getByTestId('explanation-loading')).toBeInTheDocument()
    expect(screen.getByText('Reconstructing Scheduling Decisions')).toBeInTheDocument()
  })

  it('renders error state when getExplanation fails', async () => {
    vi.spyOn(apiClient, 'getExplanation').mockRejectedValueOnce(
      new Error('Failed to fetch explanation from server'),
    )

    render(
      <ExplanationPanel
        runId="run-test-1"
        taskId="task-101"
        isOpen={true}
        onClose={vi.fn()}
      />,
    )

    await waitFor(() => {
      expect(screen.getByTestId('explanation-error')).toBeInTheDocument()
      expect(screen.getByText('Failed to fetch explanation from server')).toBeInTheDocument()
    })
  })

  it('ExplanationPanel renders rejected_candidates correctly for a mock Explanation with 2+ rejected candidates', async () => {
    const mockExplanation: Explanation = {
      task_id: 'task-101',
      task_name: 'Signal Inspection B12',
      scheduled_start: '2026-09-20T06:00:00Z',
      scheduled_end: '2026-09-20T07:00:00Z',
      chosen_reason: 'Scheduled at earliest possible start time (06:00) with minimal network interference.',
      rejected_candidates: [
        {
          candidate_start: '2026-09-20T07:15:00Z',
          candidate_end: '2026-09-20T08:15:00Z',
          rejection_reason: 'blocking both sections would isolate the loop',
          conflicting_entity_id: 'SEC-404',
          conflicting_entity_type: 'safety_adjacency',
        },
        {
          candidate_start: '2026-09-20T08:30:00Z',
          candidate_end: '2026-09-20T09:30:00Z',
          rejection_reason: 'overlaps with scheduled passenger timetable',
          conflicting_entity_id: 'TRAIN-EXP-101',
          conflicting_entity_type: 'train_slot',
        },
      ],
    }

    const mockResponse: ExplainResponse = {
      run_id: 'run-test-1',
      task_id: 'task-101',
      explanation: mockExplanation,
      summary: 'Summary text',
    }

    vi.spyOn(apiClient, 'getExplanation').mockResolvedValueOnce(mockResponse)

    render(
      <ExplanationPanel
        runId="run-test-1"
        taskId="task-101"
        isOpen={true}
        onClose={vi.fn()}
      />,
    )

    // Verify task name and chosen reason
    await waitFor(() => {
      expect(screen.getByText('Signal Inspection B12')).toBeInTheDocument()
      expect(
        screen.getByText('Scheduled at earliest possible start time (06:00) with minimal network interference.'),
      ).toBeInTheDocument()
    })

    // Verify rejected candidate count badge
    expect(screen.getByText('2 candidates')).toBeInTheDocument()

    // Verify candidates items rendered
    const candidateItems = screen.getAllByTestId('rejected-candidate-item')
    expect(candidateItems).toHaveLength(2)

    // Verify candidate 1 details: time, reason, conflicting_entity_type/id in readable form
    expect(
      screen.getByText(/blocking both sections would isolate the loop/i),
    ).toBeInTheDocument()
    expect(screen.getByText(/safety adjacency: SEC-404/i)).toBeInTheDocument()
    expect(
      screen.getByText(/conflicts with safety adjacency 'SEC-404' \(safety_adjacency\)/i),
    ).toBeInTheDocument()

    // Verify candidate 2 details
    expect(
      screen.getByText(/overlaps with scheduled passenger timetable/i),
    ).toBeInTheDocument()
    expect(screen.getByText(/train slot: TRAIN-EXP-101/i)).toBeInTheDocument()
    expect(
      screen.getByText(/conflicts with train slot 'TRAIN-EXP-101' \(train_slot\)/i),
    ).toBeInTheDocument()
  })

  it('ExplanationPanel renders cleanly for one with zero rejected candidates', async () => {
    const mockExplanation: Explanation = {
      task_id: 'task-zero',
      task_name: 'Overhead Wire Maintenance',
      scheduled_start: '2026-09-20T04:00:00Z',
      scheduled_end: '2026-09-20T05:00:00Z',
      chosen_reason: 'Only available window before morning peak service.',
      rejected_candidates: [],
    }

    const mockResponse: ExplainResponse = {
      run_id: 'run-test-1',
      task_id: 'task-zero',
      explanation: mockExplanation,
      summary: 'Summary text',
    }

    vi.spyOn(apiClient, 'getExplanation').mockResolvedValueOnce(mockResponse)

    render(
      <ExplanationPanel
        runId="run-test-1"
        taskId="task-zero"
        isOpen={true}
        onClose={vi.fn()}
      />,
    )

    await waitFor(() => {
      expect(screen.getByText('Overhead Wire Maintenance')).toBeInTheDocument()
      expect(screen.getByTestId('no-rejected-candidates')).toBeInTheDocument()
      expect(screen.getByText('No alternative candidate slots were rejected.')).toBeInTheDocument()
    })

    expect(screen.getByText('0 candidates')).toBeInTheDocument()
    expect(screen.queryByTestId('rejected-candidate-item')).not.toBeInTheDocument()
  })

  it('calls onClose when close button or dismiss button is clicked', async () => {
    const onClose = vi.fn()
    const mockExplanation: Explanation = {
      task_id: 'task-101',
      task_name: 'Track Alignment',
      scheduled_start: '2026-09-20T06:00:00Z',
      scheduled_end: '2026-09-20T07:00:00Z',
      chosen_reason: 'Earliest slot available.',
      rejected_candidates: [],
    }

    vi.spyOn(apiClient, 'getExplanation').mockResolvedValueOnce({
      run_id: 'run-1',
      task_id: 'task-101',
      explanation: mockExplanation,
      summary: 'Summary',
    })

    render(
      <ExplanationPanel
        runId="run-1"
        taskId="task-101"
        isOpen={true}
        onClose={onClose}
      />,
    )

    await waitFor(() => {
      expect(screen.getByText('Track Alignment')).toBeInTheDocument()
    })

    const closeBtn = screen.getByRole('button', { name: /close explanation panel/i })
    fireEvent.click(closeBtn)
    expect(onClose).toHaveBeenCalledTimes(1)

    const dismissBtn = screen.getByRole('button', { name: /dismiss/i })
    fireEvent.click(dismissBtn)
    expect(onClose).toHaveBeenCalledTimes(2)
  })

  it('calls onClose when Escape key is pressed', async () => {
    const onClose = vi.fn()

    render(
      <ExplanationPanel
        runId="run-1"
        taskId="task-101"
        isOpen={true}
        onClose={onClose}
        initialExplanation={{
          task_id: 'task-101',
          task_name: 'Track Alignment',
          scheduled_start: '2026-09-20T06:00:00Z',
          scheduled_end: '2026-09-20T07:00:00Z',
          chosen_reason: 'Chosen',
          rejected_candidates: [],
        }}
      />,
    )

    fireEvent.keyDown(window, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(1)
  })
})
