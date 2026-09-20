import React, { useState } from 'react'
import {
  generateNetwork as apiGenerateNetwork,
  getNetwork as apiGetNetwork,
  solveSchedule as apiSolveSchedule,
} from './api/client'
import type {
  GenerateNetworkResponse,
  NetworkDetailsResponse,
  ProfileName,
  SolveResponse,
} from './api/types'
import { GanttView } from './components/GanttView'
import styles from './App.module.css'

function getRandomSeed(): number {
  return Math.floor(Math.random() * 90000) + 10000
}

export const App: React.FC = () => {
  const [profileName, setProfileName] = useState<ProfileName>('metro')
  const [seed, setSeed] = useState<number>(getRandomSeed)
  const [isGenerating, setIsGenerating] = useState<boolean>(false)
  const [isSolving, setIsSolving] = useState<boolean>(false)
  const [errorMessage, setErrorMessage] = useState<string | null>(null)

  const [activeRun, setActiveRun] = useState<GenerateNetworkResponse | null>(null)
  const [networkDetails, setNetworkDetails] = useState<NetworkDetailsResponse | null>(null)
  const [solveResult, setSolveResult] = useState<SolveResponse | null>(null)

  const handleRollSeed = () => {
    setSeed(getRandomSeed())
  }

  const handleGenerate = async () => {
    setIsGenerating(true)
    setErrorMessage(null)
    setSolveResult(null)

    try {
      const genResponse = await apiGenerateNetwork({
        profile_name: profileName,
        seed,
      })
      setActiveRun(genResponse)

      // Fetch network entities (sections, tasks, trains) for rich timeline labels & priorities
      try {
        const details = await apiGetNetwork(genResponse.run_id)
        setNetworkDetails(details)
      } catch {
        // Fallback: network is still generated even if details fetch fails
        setNetworkDetails(null)
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err)
      setErrorMessage(msg)
      setActiveRun(null)
      setNetworkDetails(null)
    } finally {
      setIsGenerating(false)
    }
  }

  const handleSolve = async () => {
    if (!activeRun) return

    setIsSolving(true)
    setErrorMessage(null)

    try {
      const result = await apiSolveSchedule({
        run_id: activeRun.run_id,
      })
      setSolveResult(result)
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err)
      setErrorMessage(msg)
    } finally {
      setIsSolving(false)
    }
  }

  const isBusy = isGenerating || isSolving

  return (
    <div className={styles.appContainer}>
      {/* Header */}
      <header className={styles.header}>
        <div className={styles.brandGroup}>
          <svg
            className={styles.brandLogo}
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
            aria-hidden="true"
          >
            <path d="M4 15s1-1 4-1 5 2 8 2 4-1 4-1V3s-1 1-4 1-5-2-8-2-4 1-4 1z" />
            <line x1="4" y1="22" x2="4" y2="15" />
            <path d="M2 20h20" />
            <path d="M6 18l1.5 4" />
            <path d="M18 18l-1.5 4" />
          </svg>
          <div className={styles.titleGroup}>
            <div className={styles.title}>
              Cadence
              <span className={styles.titleBadge}>Board</span>
            </div>
            <div className={styles.tagline}>
              It doesn't predict delays. It decides when the track is free.
            </div>
          </div>
        </div>

        <div className={styles.systemStatus}>
          <span className={styles.statusIndicator}></span>
          <span>Engine Dispatch Online</span>
        </div>
      </header>

      {/* Control Panel */}
      <section className={styles.controlPanel} aria-label="Scheduling Control Panel">
        <div className={styles.controlsGroup}>
          <div className={styles.controlField}>
            <label htmlFor="profile-select" className={styles.fieldLabel}>
              Network Profile
            </label>
            <select
              id="profile-select"
              className={styles.selectInput}
              value={profileName}
              onChange={(e) => setProfileName(e.target.value as ProfileName)}
              disabled={isBusy}
            >
              <option value="metro">metro</option>
              <option value="local">local</option>
              <option value="mainline">mainline</option>
            </select>
          </div>

          <div className={styles.controlField}>
            <label htmlFor="seed-input" className={styles.fieldLabel}>
              Random Seed
            </label>
            <div className={styles.seedInputGroup}>
              <input
                id="seed-input"
                type="number"
                className={styles.numberInput}
                value={seed}
                onChange={(e) => setSeed(Number(e.target.value) || 0)}
                disabled={isBusy}
              />
              <button
                type="button"
                className={styles.iconButton}
                onClick={handleRollSeed}
                disabled={isBusy}
                title="Regenerate random seed"
                aria-label="Regenerate random seed"
              >
                ↻
              </button>
            </div>
          </div>
        </div>

        <div className={styles.actionsGroup}>
          <button
            type="button"
            className={styles.primaryButton}
            onClick={handleGenerate}
            disabled={isBusy}
          >
            {isGenerating ? (
              <>
                <span className={styles.buttonSpinner}></span>
                <span>Generating...</span>
              </>
            ) : (
              <span>Generate Network</span>
            )}
          </button>

          <button
            type="button"
            className={styles.solveButton}
            onClick={handleSolve}
            disabled={isBusy || !activeRun}
          >
            {isSolving ? (
              <>
                <span className={styles.buttonSpinner}></span>
                <span>Solving...</span>
              </>
            ) : (
              <span>Solve Schedule</span>
            )}
          </button>
        </div>
      </section>

      {/* Error Banner */}
      {errorMessage && (
        <div className={styles.errorBanner} role="alert">
          <div className={styles.errorContent}>
            <svg
              className={styles.errorIcon}
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
            >
              <circle cx="12" cy="12" r="10" />
              <line x1="12" y1="8" x2="12" y2="12" />
              <line x1="12" y1="16" x2="12.01" y2="16" />
            </svg>
            <div>{errorMessage}</div>
          </div>
          <button
            type="button"
            className={styles.dismissErrorButton}
            onClick={() => setErrorMessage(null)}
            aria-label="Dismiss error"
          >
            ✕
          </button>
        </div>
      )}

      {/* Run Metadata Bar */}
      {activeRun && (
        <div className={styles.runMetadataBar}>
          <div className={styles.metaItem}>
            <span className={styles.metaLabel}>Run ID:</span>
            <code className={styles.metaCode}>{activeRun.run_id}</code>
          </div>
          <div className={styles.metaItem}>
            <span className={styles.metaLabel}>Profile:</span>
            <span className={styles.metaValue}>{activeRun.profile_name}</span>
          </div>
          <div className={styles.metaItem}>
            <span className={styles.metaLabel}>Sections:</span>
            <span className={styles.metaValue}>{activeRun.section_count}</span>
          </div>
          <div className={styles.metaItem}>
            <span className={styles.metaLabel}>Train Slots:</span>
            <span className={styles.metaValue}>{activeRun.train_count}</span>
          </div>
          <div className={styles.metaItem}>
            <span className={styles.metaLabel}>Tasks:</span>
            <span className={styles.metaValue}>{activeRun.task_count}</span>
          </div>
          {solveResult && (
            <>
              <div className={styles.metaItem}>
                <span className={styles.metaLabel}>Solver:</span>
                <span
                  className={`${styles.metaValue} ${
                    solveResult.status === 'OPTIMAL'
                      ? styles.statusOptimal
                      : solveResult.status === 'FEASIBLE'
                        ? styles.statusFeasible
                        : styles.statusInfeasible
                  }`}
                >
                  {solveResult.status}
                </span>
              </div>
              <div className={styles.metaItem}>
                <span className={styles.metaLabel}>Solve Time:</span>
                <span className={styles.metaValue}>
                  {solveResult.wall_time_seconds.toFixed(3)}s
                </span>
              </div>
              {solveResult.objective_value !== null &&
                solveResult.objective_value !== undefined && (
                  <div className={styles.metaItem}>
                    <span className={styles.metaLabel}>Objective:</span>
                    <span className={styles.metaValue}>{solveResult.objective_value}</span>
                  </div>
                )}
            </>
          )}
        </div>
      )}

      {/* Main Content Area */}
      <main className={styles.mainContent}>
        {solveResult ? (
          <GanttView
            scheduledBlocks={solveResult.scheduled_blocks}
            sections={networkDetails?.sections}
            tasks={networkDetails?.maintenance_tasks}
            isLoading={isSolving}
          />
        ) : (
          <div className={styles.dashboardEmptyState}>
            <svg
              className={styles.emptyGraphic}
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.5"
              strokeLinecap="round"
              strokeLinejoin="round"
            >
              <rect x="2" y="3" width="20" height="14" rx="2" ry="2" />
              <line x1="8" y1="21" x2="16" y2="21" />
              <line x1="12" y1="17" x2="12" y2="21" />
            </svg>
            <div className={styles.emptyStateTitle}>
              {activeRun ? 'Network Ready for Solver' : 'No Active Railway Schedule'}
            </div>
            <p className={styles.emptyStateMessage}>
              {activeRun
                ? `Synthetic network '${activeRun.profile_name}' generated with ${activeRun.section_count} sections and ${activeRun.task_count} tasks. Click 'Solve Schedule' to compute optimal, conflict-free possession blocks with Google OR-Tools CP-SAT.`
                : "Select a network profile and random seed above, then click 'Generate Network' to generate topology, timetable slots, and possession maintenance tasks."}
            </p>
            <div className={styles.stepHint}>
              {activeRun ? 'Step 2 of 2: Run CP-SAT Solver' : 'Step 1 of 2: Generate Network'}
            </div>
          </div>
        )}
      </main>

      {/* Footer */}
      <footer className={styles.footer}>
        <div>
          Cadence &copy; 2026 <strong>jeevapriyan10</strong> &bull; Railway Block-Window Scheduling
          Engine
        </div>
        <div>FastAPI &bull; OR-Tools CP-SAT &bull; React &bull; vis-timeline</div>
      </footer>
    </div>
  )
}

export default App
