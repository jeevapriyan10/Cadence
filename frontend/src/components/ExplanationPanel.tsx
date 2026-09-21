import React, { useEffect, useState } from 'react'
import { getExplanation as apiGetExplanation } from '../api/client'
import type {
  ConflictingEntityType,
  ExplainResponse,
  Explanation,
  RejectedCandidate,
} from '../api/types'
import styles from './ExplanationPanel.module.css'

export interface ExplanationPanelProps {
  runId: string
  taskId: string | null
  isOpen: boolean
  onClose: () => void
  initialExplanation?: Explanation | ExplainResponse | null
}

function formatTime(s?: string): string {
  if (!s) return ''
  if (/^\d{1,2}:\d{2}/.test(s) && !s.includes('T')) return s
  try {
    const d = new Date(s)
    if (!isNaN(d.getTime())) {
      const hours = String(d.getUTCHours()).padStart(2, '0')
      const mins = String(d.getUTCMinutes()).padStart(2, '0')
      return `${hours}:${mins}`
    }
  } catch {
    // fallback to original string
  }
  return s
}

function formatTimeRange(startStr?: string, endStr?: string): string {
  if (!startStr && !endStr) return 'N/A'
  const s = formatTime(startStr)
  const e = formatTime(endStr)
  return s && e ? `${s} - ${e}` : s || e || 'N/A'
}

function getBadgeClass(type?: ConflictingEntityType): string {
  switch (type) {
    case 'task':
      return styles.badgeTask
    case 'train_slot':
      return styles.badgeTrainSlot
    case 'safety_adjacency':
      return styles.badgeSafetyAdjacency
    default:
      return styles.badgeDefault
  }
}

function normalizeExplanation(
  data?: Explanation | ExplainResponse | null,
): Explanation | null {
  if (!data) return null
  if ('explanation' in data && data.explanation && typeof data.explanation === 'object') {
    return data.explanation as Explanation
  }
  return data as Explanation
}

export const ExplanationPanel: React.FC<ExplanationPanelProps> = ({
  runId,
  taskId,
  isOpen,
  onClose,
  initialExplanation,
}) => {
  const [fetchedExplanation, setFetchedExplanation] = useState<Explanation | null>(null)
  const [isLoading, setIsLoading] = useState<boolean>(false)
  const [errorMessage, setErrorMessage] = useState<string | null>(null)

  const explanation = initialExplanation
    ? normalizeExplanation(initialExplanation)
    : isOpen && taskId
      ? fetchedExplanation
      : null

  useEffect(() => {
    if (!isOpen || !taskId || initialExplanation) {
      return
    }

    let isMounted = true
    setIsLoading(true)
    setErrorMessage(null)

    apiGetExplanation(runId, taskId)
      .then((res) => {
        if (isMounted) {
          setFetchedExplanation(normalizeExplanation(res))
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
  }, [runId, taskId, isOpen, initialExplanation])

  useEffect(() => {
    if (!isOpen) return
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        onClose()
      }
    }
    window.addEventListener('keydown', handleKeyDown)
    return () => {
      window.removeEventListener('keydown', handleKeyDown)
    }
  }, [isOpen, onClose])

  if (!isOpen) {
    return null
  }

  const handleBackdropClick = (e: React.MouseEvent<HTMLDivElement>) => {
    if (e.target === e.currentTarget) {
      onClose()
    }
  }

  const taskDisplayName = explanation?.task_name || taskId || 'Possession Task'
  const scheduledTimeWindow = explanation
    ? formatTimeRange(explanation.scheduled_start, explanation.scheduled_end)
    : ''
  const rejectedList: RejectedCandidate[] = explanation?.rejected_candidates || []

  return (
    <div
      className={styles.backdrop}
      onClick={handleBackdropClick}
      data-testid="explanation-panel-backdrop"
      role="dialog"
      aria-modal="true"
      aria-labelledby="explanation-title"
    >
      <div className={styles.panel} data-testid="explanation-panel">
        {/* Header */}
        <div className={styles.panelHeader}>
          <div className={styles.headerLeft}>
            <div className={styles.headerSubtitle}>
              <span>Reason Post-Hoc Explainer</span>
              {taskId && <span>&bull; Task ID: {taskId}</span>}
            </div>
            <h2 id="explanation-title" className={styles.taskTitle}>
              {taskDisplayName}
            </h2>
          </div>
          <button
            type="button"
            className={styles.closeButton}
            onClick={onClose}
            aria-label="Close explanation panel"
          >
            ✕
          </button>
        </div>

        {/* Content Body */}
        <div className={styles.panelBody}>
          {isLoading && (
            <div className={styles.stateContainer} data-testid="explanation-loading">
              <div className={styles.spinner}></div>
              <div className={styles.stateTitle}>Reconstructing Scheduling Decisions</div>
              <p className={styles.stateMessage}>
                Querying Reason engine to reconstruct CP-SAT solver branches and constraint conflicts...
              </p>
            </div>
          )}

          {!isLoading && errorMessage && (
            <div className={styles.errorBox} role="alert" data-testid="explanation-error">
              <div className={styles.errorTitle}>Failed to Load Scheduling Explanation</div>
              <div>{errorMessage}</div>
            </div>
          )}

          {!isLoading && !errorMessage && explanation && (
            <>
              {/* Chosen Slot Section */}
              <div className={styles.chosenSection} data-testid="chosen-slot-section">
                <div className={styles.sectionHeader}>
                  <span className={styles.sectionLabel}>Chosen Scheduled Slot</span>
                  {scheduledTimeWindow && (
                    <span className={styles.timeBadge}>{scheduledTimeWindow}</span>
                  )}
                </div>
                <div className={styles.chosenReason}>
                  {explanation.chosen_reason || 'Scheduled at optimal solver window.'}
                </div>
              </div>

              {/* Rejected Candidates Section */}
              <div className={styles.candidatesSection} data-testid="rejected-candidates-section">
                <div className={styles.candidatesHeader}>
                  <span className={styles.candidatesTitle}>Rejected Alternative Slots</span>
                  <span className={styles.candidateCountBadge}>
                    {rejectedList.length} {rejectedList.length === 1 ? 'candidate' : 'candidates'}
                  </span>
                </div>

                {rejectedList.length === 0 ? (
                  <div className={styles.emptyCandidates} data-testid="no-rejected-candidates">
                    No alternative candidate slots were rejected.
                  </div>
                ) : (
                  <div className={styles.candidatesList}>
                    {rejectedList.map((cand, idx) => {
                      const candTime = formatTimeRange(
                        cand.candidate_start,
                        cand.candidate_end,
                      )
                      const entityTypeLabel = cand.conflicting_entity_type
                        ? cand.conflicting_entity_type.replace(/_/g, ' ')
                        : 'conflict'

                      return (
                        <div
                          key={`${cand.candidate_start}-${cand.candidate_end}-${idx}`}
                          className={styles.candidateCard}
                          data-testid="rejected-candidate-item"
                        >
                          <div className={styles.candidateMeta}>
                            <span className={styles.candidateTime}>{candTime}</span>
                            <span
                              className={`${styles.entityBadge} ${getBadgeClass(
                                cand.conflicting_entity_type,
                              )}`}
                            >
                              {entityTypeLabel}: {cand.conflicting_entity_id}
                            </span>
                          </div>
                          <div className={styles.candidateDescription}>
                            <span className={styles.rejectionNotice}>Rejected — </span>
                            conflicts with {entityTypeLabel} '{cand.conflicting_entity_id}' (
                            {cand.conflicting_entity_type}): {cand.rejection_reason}
                          </div>
                        </div>
                      )
                    })}
                  </div>
                )}
              </div>
            </>
          )}
        </div>

        {/* Footer */}
        <div className={styles.panelFooter}>
          <button
            type="button"
            className={styles.dismissButton}
            onClick={onClose}
          >
            Dismiss
          </button>
        </div>
      </div>
    </div>
  )
}

export default ExplanationPanel
