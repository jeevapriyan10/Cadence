import '@testing-library/jest-dom/vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as apiClient from '../api/client'
import type { RippleResponse } from '../api/types'
import { RipplePanel } from './RipplePanel'

describe('RipplePanel Component', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  afterEach(() => {
    cleanup()
  })

  it('RipplePanel shows its empty state correctly before any solve has occurred', () => {
    // When no solve has occurred (isSolved=false)
    render(<RipplePanel runId="test-run-1" isSolved={false} />)

    expect(screen.getByTestId('ripple-empty')).toBeInTheDocument()
    expect(screen.getByText('Solve a schedule to see impact')).toBeInTheDocument()
    expect(screen.queryByTestId('ripple-kpi-grid')).not.toBeInTheDocument()
  })

  it('shows empty state when runId is null or undefined', () => {
    render(<RipplePanel runId={null} isSolved={false} />)

    expect(screen.getByTestId('ripple-empty')).toBeInTheDocument()
    expect(screen.getByText('Solve a schedule to see impact')).toBeInTheDocument()
  })

  it('renders loading state while RippleReport is fetching', async () => {
    vi.spyOn(apiClient, 'getRipple').mockImplementation(
      () => new Promise(() => {}),
    )

    render(<RipplePanel runId="test-run-1" isSolved={true} />)

    expect(screen.getByTestId('ripple-loading')).toBeInTheDocument()
    expect(
      screen.getByText('Simulating cascade delay propagation...'),
    ).toBeInTheDocument()
  })

  it('renders error state when getRipple fails', async () => {
    vi.spyOn(apiClient, 'getRipple').mockRejectedValueOnce(
      new Error('No solve result found for this run'),
    )

    render(<RipplePanel runId="test-run-1" isSolved={true} />)

    await waitFor(() => {
      expect(screen.getByTestId('ripple-error')).toBeInTheDocument()
      expect(screen.getByText('No solve result found for this run')).toBeInTheDocument()
    })
  })

  it('RipplePanel renders correctly for a mock RippleReport with multiple affected trains, including one showing a cascade_source', async () => {
    const mockRippleReport: RippleResponse = {
      run_id: 'test-run-42',
      total_trains_affected: 3,
      total_delay_minutes: 85,
      per_train_impacts: [
        {
          train_slot_id: 'train-1',
          train_name: 'Express-101',
          directly_affected: true,
          delay_minutes: 40,
          affected_sections: ['SEC-1', 'SEC-2'],
          cascade_source: null,
        },
        {
          train_slot_id: 'train-2',
          train_name: 'Local-202',
          directly_affected: false,
          delay_minutes: 30,
          affected_sections: ['SEC-3'],
          cascade_source: 'Express-104',
        },
        {
          train_slot_id: 'train-3',
          train_name: 'Cargo-505',
          directly_affected: false,
          delay_minutes: 15,
          affected_sections: ['SEC-4'],
          cascade_source: 'Local-202',
        },
      ],
      summary: '3 trains delayed by a total of 85 minutes.',
    }

    vi.spyOn(apiClient, 'getRipple').mockResolvedValueOnce(mockRippleReport)

    render(<RipplePanel runId="test-run-42" isSolved={true} />)

    // Verify prominent KPIs
    await waitFor(() => {
      expect(screen.getByTestId('total-trains-affected')).toHaveTextContent('3')
      expect(screen.getByTestId('total-delay-minutes')).toHaveTextContent('85 min')
    })

    // Verify train list rendered
    const trainItems = screen.getAllByTestId('train-impact-item')
    expect(trainItems).toHaveLength(3)

    // Check train names and delay minutes
    expect(screen.getByText('Express-101')).toBeInTheDocument()
    expect(screen.getByText('+40 min')).toBeInTheDocument()

    expect(screen.getByText('Local-202')).toBeInTheDocument()
    expect(screen.getByText('+30 min')).toBeInTheDocument()

    expect(screen.getByText('Cargo-505')).toBeInTheDocument()
    expect(screen.getByText('+15 min')).toBeInTheDocument()

    // Verify cascade source text
    expect(
      screen.getByText(/delayed due to cascade from Express-104/i),
    ).toBeInTheDocument()
    expect(
      screen.getByText(/delayed due to cascade from Local-202/i),
    ).toBeInTheDocument()

    // Verify direct conflict note
    expect(screen.getByText('Direct possession conflict')).toBeInTheDocument()
  })

  it('renders correctly when report is passed directly via prop', () => {
    const mockReport: RippleResponse = {
      run_id: 'test-run-prop',
      total_trains_affected: 1,
      total_delay_minutes: 25,
      per_train_impacts: [
        {
          train_slot_id: 'slot-1',
          train_name: 'Metro-99',
          directly_affected: false,
          delay_minutes: 25,
          affected_sections: ['SEC-M1'],
          cascade_source: 'Express-104',
        },
      ],
      summary: '1 train delayed',
    }

    render(<RipplePanel report={mockReport} />)

    expect(screen.getByTestId('total-trains-affected')).toHaveTextContent('1')
    expect(screen.getByTestId('total-delay-minutes')).toHaveTextContent('25 min')
    expect(screen.getByText('Metro-99')).toBeInTheDocument()
    expect(
      screen.getByText(/delayed due to cascade from Express-104/i),
    ).toBeInTheDocument()
  })
})
