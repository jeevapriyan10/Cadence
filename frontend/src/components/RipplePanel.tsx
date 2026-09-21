import React, { useEffect, useState } from 'react'
import { getRipple as apiGetRipple } from '../api/client'
import type { RippleResponse, TrainImpact } from '../api/types'
import styles from './RipplePanel.module.css'

export interface RipplePanelProps {
  runId?: string | null
  isSolved?: boolean
  report?: RippleResponse | null
  solveTimestamp?: number
  isLoading?: boolean
}

export const RipplePanel: React.FC<RipplePanelProps> = ({
  runId,
  isSolved = false,
  report: propReport,
  solveTimestamp,
  isLoading: propLoading = false,
}) => {
  const [internalReport, setInternalReport] = useState<RippleResponse | null>(null)
  const [isLoading, setIsLoading] = useState<boolean>(false)
  const [errorMessage, setErrorMessage] = useState<string | null>(null)

  const activeReport =
    propReport !== undefined
      ? propReport
      : isSolved
        ? internalReport
        : null

  const activeLoading = propLoading || isLoading

  useEffect(() => {
    if (propReport !== undefined || !runId || !isSolved) {
      return
    }

    let isMounted = true
    setIsLoading(true)
    setErrorMessage(null)

    apiGetRipple(runId)
      .then((data) => {
        if (isMounted) {
          setInternalReport(data)
        }
      })
      .catch((err) => {
        if (isMounted) {
          const msg = err instanceof Error ? err.message : String(err)
          setErrorMessage(msg)
        }
      })
      .finally(() => {
        if (isMounted) {
          setIsLoading(false)
        }
      })

    return () => {
      isMounted = false
    }
  }, [runId, isSolved, propReport, solveTimestamp])

  return (
    <div className={styles.panelContainer} data-testid="ripple-panel">
      {/* Panel Header */}
      <div className={styles.panelHeader}>
        <div className={styles.titleGroup}>
          <h3 className={styles.panelTitle}>
            Ripple
            <span className={styles.panelBadge}>Cascade Analysis</span>
          </h3>
          <span className={styles.panelSubtitle}>Downstream Delay Propagation</span>
        </div>
      </div>

      {/* Body */}
      <div className={styles.panelBody}>
        {activeLoading && (
          <div className={styles.loadingState} data-testid="ripple-loading">
            <div className={styles.spinner}></div>
            <div className={styles.loadingText}>Simulating cascade delay propagation...</div>
          </div>
        )}

        {!activeLoading && errorMessage && (
          <div className={styles.errorState} role="alert" data-testid="ripple-error">
            <div className={styles.errorTitle}>Ripple Simulation Error</div>
            <div>{errorMessage}</div>
          </div>
        )}

        {!activeLoading && !errorMessage && !activeReport && (
          <div className={styles.emptyState} data-testid="ripple-empty">
            <svg
              className={styles.emptyIcon}
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.5"
              strokeLinecap="round"
              strokeLinejoin="round"
            >
              <circle cx="12" cy="12" r="10" />
              <path d="M12 6v6l4 2" />
            </svg>
            <div className={styles.emptyMessage}>Solve a schedule to see impact</div>
          </div>
        )}

        {!activeLoading && !errorMessage && activeReport && (
          <>
            {/* Prominent KPIs */}
            <div className={styles.kpiGrid} data-testid="ripple-kpi-grid">
              <div className={`${styles.kpiCard} ${styles.kpiTrains}`}>
                <span className={styles.kpiLabel}>Total Trains Affected</span>
                <span className={styles.kpiValue} data-testid="total-trains-affected">
                  {activeReport.total_trains_affected}
                </span>
              </div>
              <div className={`${styles.kpiCard} ${styles.kpiDelay}`}>
                <span className={styles.kpiLabel}>Total Delay</span>
                <span className={styles.kpiValue} data-testid="total-delay-minutes">
                  {activeReport.total_delay_minutes}{' '}
                  <span className={styles.kpiUnit}>min</span>
                </span>
              </div>
            </div>

            {/* Per-Train Impacts List */}
            <div className={styles.impactSection} data-testid="ripple-impacts-section">
              <div className={styles.impactSectionHeader}>
                <span className={styles.impactSectionTitle}>Per-Train Impacts</span>
                <span className={styles.impactCountBadge}>
                  {activeReport.per_train_impacts.length}{' '}
                  {activeReport.per_train_impacts.length === 1 ? 'train' : 'trains'}
                </span>
              </div>

              <div className={styles.impactList} data-testid="ripple-impacts-list">
                {activeReport.per_train_impacts.length === 0 ? (
                  <div className={styles.emptyMessage}>No trains impacted by maintenance blocks.</div>
                ) : (
                  activeReport.per_train_impacts.map((impact: TrainImpact) => (
                    <div
                      key={impact.train_slot_id}
                      className={styles.impactCard}
                      data-testid="train-impact-item"
                    >
                      <div className={styles.cardTop}>
                        <span className={styles.trainName}>{impact.train_name}</span>
                        <span
                          className={`${styles.delayBadge} ${
                            impact.delay_minutes === 0 ? styles.zeroDelayBadge : ''
                          }`}
                        >
                          +{impact.delay_minutes} min
                        </span>
                      </div>

                      {impact.cascade_source ? (
                        <div className={styles.cascadeInfo}>
                          <span className={styles.cascadeTag}>
                            delayed due to cascade from {impact.cascade_source}
                          </span>
                        </div>
                      ) : (
                        <div className={styles.cascadeInfo}>
                          <span className={styles.directTag}>
                            {impact.directly_affected
                              ? 'Direct possession conflict'
                              : 'No direct cascade'}
                          </span>
                        </div>
                      )}

                      {impact.affected_sections && impact.affected_sections.length > 0 && (
                        <div className={styles.sectionsList}>
                          Sections: {impact.affected_sections.join(', ')}
                        </div>
                      )}
                    </div>
                  ))
                )}
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  )
}

export default RipplePanel
